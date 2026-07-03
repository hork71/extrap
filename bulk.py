#!/usr/bin/env python3
"""
load_inventory.py

Reads a structure.json file (list of servers, each with an extraPackages
array) and loads it into Elasticsearch using two complementary models.
Tuned for fleets of ~thousands of servers (developed/tested against a
~2700-server daily import).

  OPTION A - "current state"
      Indices: `servers`, `server_packages`
      Every run overwrites/refreshes these indices to reflect *today's*
      reality, and anything not present in today's run (decommissioned
      servers, removed packages) is deleted. This is what your Kibana
      "current fleet" dashboards should point at, and it's also where
      "weekly installs by owner/email/sl" style questions get answered
      from - package.installtime is already on every row, no history
      needed for that.

  OPTION B - "history" (fleet-trend over time)
      Data stream: `inventory_history_rollup`
      Every run appends ONE SMALL AGGREGATED DOC PER (group, owner,
      email, sl, managed) COMBINATION - server_count, package_count,
      unique_package_count - not a full per-package replay. At
      thousands of servers, replaying every package row daily would
      turn into tens/hundreds of millions of near-duplicate documents
      within a year for no analytical benefit (most packages don't
      change day to day, and installtime-based analysis doesn't need
      it anyway). The rollup stays tiny and scales fine indefinitely.

      If you genuinely need full per-package row history (e.g. audit /
      compliance requirements), pass --history-detail - it appends full
      rows to `server_packages_history` like before, but be deliberate
      about retention (--history-retention-days) given the volume.

Run this once a day (e.g. via cron) and it keeps both models in sync.

Usage:
    pip install elasticsearch>=8.0.0

    # Update both the current-state indices and append a history rollup:
    python load_inventory.py structure.json --host https://localhost:9200 \
        --api-key <base64-api-key> --mode both

    # Only refresh the current-state indices:
    python load_inventory.py structure.json --mode current ...

    # Only append the history rollup:
    python load_inventory.py structure.json --mode history ...

    # First run / reset current-state indices with the mappings below:
    python load_inventory.py structure.json --mode current --recreate ...

    # Opt into full per-package row history (high volume - see notes above):
    python load_inventory.py structure.json --mode history --history-detail \
        --history-retention-days 90 ...

    # Dry run (no ES connection, just validate + print counts):
    python load_inventory.py structure.json --dry-run
"""

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# Date handling
# ---------------------------------------------------------------------------
# Source timestamps look like: "10/26/17 12:10:44 PM CEST"
# Python's %Z does not reliably parse timezone *abbreviations* (they're
# ambiguous globally), so we strip the abbreviation ourselves and map it to
# a fixed UTC offset. Extend this table if your data contains other zones.
TZ_OFFSETS = {
    "CET":  timedelta(hours=1),
    "CEST": timedelta(hours=2),
    "UTC":  timedelta(hours=0),
    "GMT":  timedelta(hours=0),
    "BST":  timedelta(hours=1),
    "EST":  timedelta(hours=-5),
    "EDT":  timedelta(hours=-4),
}

INSTALLTIME_FORMAT = "%m/%d/%y %I:%M:%S %p"


def parse_installtime(raw):
    """
    Convert '10/26/17 12:10:44 PM CEST' -> ISO-8601 UTC string, e.g.
    '2017-10-26T10:10:44Z'.
    """
    if not raw:
        return None

    parts = raw.rsplit(" ", 1)
    if len(parts) != 2:
        return None

    dt_part, tz_abbr = parts
    offset = TZ_OFFSETS.get(tz_abbr.upper())
    if offset is None:
        print(f"  ! unknown timezone abbreviation '{tz_abbr}' in '{raw}', "
              f"skipping installtime for this record", file=sys.stderr)
        return None

    try:
        naive_dt = datetime.strptime(dt_part, INSTALLTIME_FORMAT)
    except ValueError:
        print(f"  ! could not parse datetime '{dt_part}' in '{raw}'",
              file=sys.stderr)
        return None

    aware_dt = (naive_dt - offset).replace(tzinfo=timezone.utc)
    return aware_dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def today_utc_date():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def now_utc_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Field definitions shared across mappings
# ---------------------------------------------------------------------------
SERVER_FIELDS = {
    "naam": {"type": "keyword"},
    "group": {"type": "keyword"},
    "email": {"type": "keyword"},
    "owner": {"type": "keyword"},
    "sl": {"type": "keyword"},
    "managed": {"type": "boolean"},
    "uuid": {"type": "keyword"},
}

PACKAGE_FIELDS = {
    "name": {"type": "keyword"},
    "version": {"type": "keyword"},
    "release": {"type": "keyword"},
    "arch": {"type": "keyword"},
    "installtime": {"type": "date"},
}

# ---------------------------------------------------------------------------
# OPTION A - current-state index mappings
# ---------------------------------------------------------------------------
SERVERS_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "properties": {
            **SERVER_FIELDS,
            "package_count": {"type": "integer"},
            "snapshot_date": {"type": "date", "format": "yyyy-MM-dd"},
            "extraPackages": {
                "type": "nested",
                "properties": PACKAGE_FIELDS,
            },
        }
    }
}

SERVER_PACKAGES_MAPPING = {
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 1,
    },
    "mappings": {
        "properties": {
            "server": {"properties": SERVER_FIELDS},
            "package": {"properties": PACKAGE_FIELDS},
            "snapshot_date": {"type": "date", "format": "yyyy-MM-dd"},
        }
    }
}

# ---------------------------------------------------------------------------
# OPTION B - history mappings (data streams require a "@timestamp" field)
# ---------------------------------------------------------------------------

# Default, recommended history model: one tiny aggregated doc per
# (group, owner, email, sl, managed) combination per run. Rows/day stays
# proportional to the number of distinct combinations, NOT the number of
# servers or packages - scales fine at any fleet size.
ROLLUP_HISTORY_MAPPING = {
    "group": {"type": "keyword"},
    "owner": {"type": "keyword"},
    "email": {"type": "keyword"},
    "sl": {"type": "keyword"},
    "managed": {"type": "boolean"},
    "server_count": {"type": "integer"},
    "package_count": {"type": "integer"},
    "unique_package_count": {"type": "integer"},
    "@timestamp": {"type": "date"},
}

# Opt-in, high-volume model: full per-package row replayed every run.
# Only use this if you have a specific need for row-level history (e.g.
# audit trail of exactly which package/version was on which server on
# which day) - at thousands of servers this grows fast, budget ILM
# retention accordingly.
SERVER_PACKAGES_HISTORY_MAPPING = {
    "server": {"properties": SERVER_FIELDS},
    "package": {"properties": PACKAGE_FIELDS},
    "@timestamp": {"type": "date"},
}


# ---------------------------------------------------------------------------
# Document builders - OPTION A (current state)
# ---------------------------------------------------------------------------
def build_server_doc(server, snapshot_date):
    packages = server.get("extraPackages", []) or []
    nested_packages = [
        {
            "name": pkg.get("name"),
            "version": pkg.get("version"),
            "release": pkg.get("release"),
            "arch": pkg.get("arch"),
            "installtime": parse_installtime(pkg.get("installtime")),
        }
        for pkg in packages
    ]

    doc = {
        "naam": server.get("naam"),
        "group": server.get("group"),
        "email": server.get("email"),
        "owner": server.get("owner"),
        "sl": server.get("sl"),
        "managed": server.get("managed"),
        "uuid": server.get("uuid"),
        "package_count": len(packages),
        "snapshot_date": snapshot_date,
        "extraPackages": nested_packages,
    }
    doc_id = server.get("uuid")
    return doc_id, doc


def build_package_docs(server, snapshot_date):
    server_fields = {
        "naam": server.get("naam"),
        "group": server.get("group"),
        "email": server.get("email"),
        "owner": server.get("owner"),
        "sl": server.get("sl"),
        "managed": server.get("managed"),
        "uuid": server.get("uuid"),
    }

    for pkg in server.get("extraPackages", []) or []:
        doc = {
            "server": server_fields,
            "package": {
                "name": pkg.get("name"),
                "version": pkg.get("version"),
                "release": pkg.get("release"),
                "arch": pkg.get("arch"),
                "installtime": parse_installtime(pkg.get("installtime")),
            },
            "snapshot_date": snapshot_date,
        }
        # deterministic id so re-running the loader updates rather than
        # duplicates rows: <server_uuid>_<package_name>_<version>_<release>_<arch>
        doc_id = "_".join(str(x) for x in [
            server.get("uuid"), pkg.get("name"), pkg.get("version"),
            pkg.get("release"), pkg.get("arch"),
        ])
        yield doc_id, doc


# ---------------------------------------------------------------------------
# Document builders - OPTION B (history)
# ---------------------------------------------------------------------------
def build_rollup_docs(servers, timestamp):
    """
    One doc per (group, owner, email, sl, managed) combination, with
    server/package counts. This is what keeps history cheap at scale.
    """
    buckets = defaultdict(lambda: {
        "server_count": 0, "package_count": 0, "package_names": set()
    })

    for server in servers:
        key = (
            server.get("group"), server.get("owner"),
            server.get("email"), server.get("sl"), server.get("managed"),
        )
        packages = server.get("extraPackages", []) or []
        bucket = buckets[key]
        bucket["server_count"] += 1
        bucket["package_count"] += len(packages)
        for pkg in packages:
            bucket["package_names"].add(pkg.get("name"))

    docs = []
    for (group, owner, email, sl, managed), bucket in buckets.items():
        doc = {
            "group": group,
            "owner": owner,
            "email": email,
            "sl": sl,
            "managed": managed,
            "server_count": bucket["server_count"],
            "package_count": bucket["package_count"],
            "unique_package_count": len(bucket["package_names"]),
            "@timestamp": timestamp,
        }
        docs.append((None, doc))
    return docs


def to_history_detail_doc(current_doc, timestamp):
    """Strip snapshot_date and add @timestamp, for --history-detail mode."""
    doc = dict(current_doc)
    doc.pop("snapshot_date", None)
    doc["@timestamp"] = timestamp
    return doc


# ---------------------------------------------------------------------------
# Elasticsearch helpers
# ---------------------------------------------------------------------------
def ensure_current_index(es, index, mapping, recreate):
    exists = es.indices.exists(index=index)
    if recreate and exists:
        print(f"Deleting existing index '{index}'")
        es.indices.delete(index=index)
        exists = False
    if not exists:
        print(f"Creating index '{index}'")
        es.indices.create(index=index, body=mapping)


def ensure_ilm_policy(es, policy_name, retention_days):
    print(f"Ensuring ILM policy '{policy_name}' (delete after "
          f"{retention_days}d)")
    es.ilm.put_lifecycle(
        name=policy_name,
        body={
            "policy": {
                "phases": {
                    "hot": {"min_age": "0ms", "actions": {"rollover": {
                        "max_age": "7d"
                    }}},
                    "delete": {
                        "min_age": f"{retention_days}d",
                        "actions": {"delete": {}},
                    },
                }
            }
        },
    )


def ensure_history_data_stream(es, name, properties, ilm_policy_name=None):
    template = {"mappings": {"properties": properties}}
    if ilm_policy_name:
        template["settings"] = {"index.lifecycle.name": ilm_policy_name}

    print(f"Ensuring index template + data stream for '{name}'")
    es.indices.put_index_template(
        name=f"{name}-template",
        body={
            "index_patterns": [name],
            "data_stream": {},
            "template": template,
        },
    )


def set_refresh_interval(es, index, value):
    try:
        es.indices.put_settings(index=index,
                                 body={"index": {"refresh_interval": value}})
    except Exception as exc:  # noqa: BLE001 - best-effort tuning, never fatal
        print(f"  ! could not set refresh_interval on '{index}': {exc}",
              file=sys.stderr)


def bulk_index(es, parallel_bulk, index, docs, op_type="index",
                chunk_size=2000, thread_count=4):
    def gen_actions():
        for doc_id, doc in docs:
            action = {"_op_type": op_type, "_index": index, "_source": doc}
            if op_type != "create" and doc_id is not None:
                action["_id"] = doc_id
            yield action

    success = 0
    errors = []
    for ok, item in parallel_bulk(es, gen_actions(), chunk_size=chunk_size,
                                   thread_count=thread_count,
                                   raise_on_error=False):
        if ok:
            success += 1
        else:
            errors.append(item)

    print(f"{index}: {success} indexed, {len(errors)} errors")
    for err in errors[:5]:
        print("  ", err)
    return success, errors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("json_file", help="Path to structure.json")
    parser.add_argument("--host", default="https://localhost:9200",
                         help="Elasticsearch URL")
    parser.add_argument("--api-key", default=None,
                         help="Elasticsearch API key (base64 'id:api_key' "
                              "or the encoded form ES gives you)")
    parser.add_argument("--user", default=None, help="Basic auth username")
    parser.add_argument("--password", default=None, help="Basic auth password")
    parser.add_argument("--ca-cert", default=None,
                         help="Path to CA cert if using a self-signed cluster")

    parser.add_argument("--mode", choices=["current", "history", "both"],
                         default="both",
                         help="current = refresh option-A indices only; "
                              "history = append option-B rollup only; "
                              "both = do both (default)")

    parser.add_argument("--servers-index", default="servers")
    parser.add_argument("--packages-index", default="server_packages")
    parser.add_argument("--rollup-history-stream",
                         default="inventory_history_rollup")
    parser.add_argument("--packages-history-stream",
                         default="server_packages_history")

    parser.add_argument("--snapshot-date", default=None,
                         help="Override the snapshot date (YYYY-MM-DD), "
                              "default: today (UTC)")
    parser.add_argument("--recreate", action="store_true",
                         help="Delete and recreate the OPTION A indices "
                              "with the mappings above before loading "
                              "(DESTRUCTIVE - only affects --mode current)")
    parser.add_argument("--history-detail", action="store_true",
                         help="Also replay full per-package rows into "
                              "server_packages_history (HIGH VOLUME at "
                              "thousands of servers - see module docstring)")
    parser.add_argument("--history-retention-days", type=int, default=None,
                         help="If set, attach an ILM policy to the history "
                              "data stream(s) that deletes docs older than "
                              "this many days")
    parser.add_argument("--bulk-chunk-size", type=int, default=2000,
                         help="Docs per bulk request chunk (default: 2000)")
    parser.add_argument("--bulk-thread-count", type=int, default=4,
                         help="Parallel bulk worker threads (default: 4)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse and report counts, but don't touch ES")
    args = parser.parse_args()

    snapshot_date = args.snapshot_date or today_utc_date()
    timestamp = now_utc_iso()

    with open(args.json_file, "r", encoding="utf-8") as f:
        servers = json.load(f)

    server_docs = [build_server_doc(s, snapshot_date) for s in servers]
    package_docs = []
    for server in servers:
        package_docs.extend(build_package_docs(server, snapshot_date))
    rollup_docs = build_rollup_docs(servers, timestamp)

    print(f"Parsed {len(server_docs)} servers, {len(package_docs)} package "
          f"rows, {len(rollup_docs)} history-rollup buckets from "
          f"{args.json_file} (snapshot_date={snapshot_date})")

    if args.dry_run:
        print("Dry run - not connecting to Elasticsearch.")
        if server_docs:
            print("Sample current-state server doc:")
            print(json.dumps(server_docs[0][1], indent=2))
        if package_docs:
            print("Sample current-state package doc:")
            print(json.dumps(package_docs[0][1], indent=2))
        if rollup_docs:
            print("Sample history-rollup doc:")
            print(json.dumps(rollup_docs[0][1], indent=2))
        return

    try:
        from elasticsearch import Elasticsearch
        from elasticsearch.helpers import parallel_bulk
    except ImportError:
        print("The 'elasticsearch' package is required for a real load.\n"
              "Install it with: pip install elasticsearch>=8.0.0",
              file=sys.stderr)
        sys.exit(1)

    es_kwargs = {"hosts": [args.host]}
    if args.api_key:
        es_kwargs["api_key"] = args.api_key
    elif args.user and args.password:
        es_kwargs["basic_auth"] = (args.user, args.password)
    if args.ca_cert:
        es_kwargs["ca_certs"] = args.ca_cert

    es = Elasticsearch(**es_kwargs)

    # -----------------------------------------------------------------
    # OPTION A - current state
    # -----------------------------------------------------------------
    if args.mode in ("current", "both"):
        ensure_current_index(es, args.servers_index, SERVERS_MAPPING,
                              args.recreate)
        ensure_current_index(es, args.packages_index, SERVER_PACKAGES_MAPPING,
                              args.recreate)

        # Speed up the bulk load: don't refresh (make docs searchable)
        # after every chunk, only once at the end.
        set_refresh_interval(es, args.servers_index, "-1")
        set_refresh_interval(es, args.packages_index, "-1")
        try:
            bulk_index(es, parallel_bulk, args.servers_index, server_docs,
                       chunk_size=args.bulk_chunk_size,
                       thread_count=args.bulk_thread_count)
            bulk_index(es, parallel_bulk, args.packages_index, package_docs,
                       chunk_size=args.bulk_chunk_size,
                       thread_count=args.bulk_thread_count)
        finally:
            set_refresh_interval(es, args.servers_index, "1s")
            set_refresh_interval(es, args.packages_index, "1s")

        for index in (args.servers_index, args.packages_index):
            resp = es.delete_by_query(
                index=index,
                body={
                    "query": {
                        "bool": {
                            "must_not": [
                                {"term": {"snapshot_date": snapshot_date}}
                            ]
                        }
                    }
                },
                slices="auto",  # parallelize the delete at this doc volume
            )
            deleted = resp.get("deleted", 0)
            print(f"{index}: removed {deleted} stale doc(s) not in "
                  f"today's snapshot")

    # -----------------------------------------------------------------
    # OPTION B - history
    # -----------------------------------------------------------------
    if args.mode in ("history", "both"):
        ilm_policy_name = None
        if args.history_retention_days:
            ilm_policy_name = "inventory-history-retention"
            ensure_ilm_policy(es, ilm_policy_name,
                               args.history_retention_days)

        ensure_history_data_stream(
            es, args.rollup_history_stream, ROLLUP_HISTORY_MAPPING,
            ilm_policy_name)
        bulk_index(es, parallel_bulk, args.rollup_history_stream,
                   rollup_docs, op_type="create",
                   chunk_size=args.bulk_chunk_size,
                   thread_count=args.bulk_thread_count)

        if args.history_detail:
            print("--history-detail set: also replaying full per-package "
                  "rows into history (high volume, see module docstring)")
            ensure_history_data_stream(
                es, args.packages_history_stream,
                SERVER_PACKAGES_HISTORY_MAPPING, ilm_policy_name)
            history_package_docs = [
                (None, to_history_detail_doc(doc, timestamp))
                for _, doc in package_docs
            ]
            bulk_index(es, parallel_bulk, args.packages_history_stream,
                       history_package_docs, op_type="create",
                       chunk_size=args.bulk_chunk_size,
                       thread_count=args.bulk_thread_count)


if __name__ == "__main__":
    main()
