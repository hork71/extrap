#!/usr/bin/env python3
"""
load_inventory.py

Reads a structure.json file (list of servers, each with an extraPackages
array) and loads it into Elasticsearch using two complementary models:

  OPTION A - "current state"
      Indices: `servers`, `server_packages`
      Every run overwrites/refreshes these indices to reflect *today's*
      reality, and anything not present in today's run (decommissioned
      servers, removed packages) is deleted. This is what your Kibana
      "current fleet" dashboards should point at.

  OPTION B - "history" (trend over time)
      Data streams: `servers_history`, `server_packages_history`
      Every run appends a new snapshot dated by --snapshot-date (default:
      today, UTC). Nothing is ever deleted here (until your retention/ILM
      policy expires it) - this is what "how has this changed over the
      last 90 days" trend charts should point at.

Run this once a day (e.g. via cron) and it keeps both models in sync.

Usage:
    pip install elasticsearch>=8.0.0

    # Update both the current-state indices and append a history snapshot:
    python load_inventory.py structure.json --host https://localhost:9200 \
        --api-key <base64-api-key> --mode both

    # Only refresh the current-state indices:
    python load_inventory.py structure.json --mode current ...

    # Only append a history snapshot:
    python load_inventory.py structure.json --mode history ...

    # First run / reset current-state indices with the mappings below:
    python load_inventory.py structure.json --mode current --recreate ...

    # Optionally auto-expire history snapshots after N days via ILM:
    python load_inventory.py structure.json --mode history \
        --history-retention-days 180 ...

    # Dry run (no ES connection, just validate + print counts):
    python load_inventory.py structure.json --dry-run
"""

import argparse
import json
import sys
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

    Returns None (and lets the caller decide whether to drop the field)
    if the value can't be parsed, rather than silently indexing garbage.
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
    "mappings": {
        "properties": {
            "server": {"properties": SERVER_FIELDS},
            "package": {"properties": PACKAGE_FIELDS},
            "snapshot_date": {"type": "date", "format": "yyyy-MM-dd"},
        }
    }
}

# ---------------------------------------------------------------------------
# OPTION B - history data stream mappings
# Data streams require a "@timestamp" date field.
# ---------------------------------------------------------------------------
SERVERS_HISTORY_MAPPING = {
    **SERVER_FIELDS,
    "package_count": {"type": "integer"},
    "@timestamp": {"type": "date"},
}

SERVER_PACKAGES_HISTORY_MAPPING = {
    "server": {"properties": SERVER_FIELDS},
    "package": {"properties": PACKAGE_FIELDS},
    "@timestamp": {"type": "date"},
}


# ---------------------------------------------------------------------------
# Document builders
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


def to_history_doc(current_doc, timestamp):
    """Strip snapshot_date and add @timestamp for the history data stream."""
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
                        "max_age": "1d"
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
    """
    Create (or update) the composable index template backing a data stream.
    The data stream itself is created automatically on first write.
    """
    template = {
        "mappings": {"properties": properties},
    }
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


def bulk_index(es, bulk, index, docs, op_type="index"):
    def gen_actions():
        for doc_id, doc in docs:
            action = {"_op_type": op_type, "_index": index, "_source": doc}
            if op_type != "create" and doc_id is not None:
                action["_id"] = doc_id
            yield action

    success, errors = bulk(es, gen_actions(), raise_on_error=False)
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
                              "history = append option-B snapshot only; "
                              "both = do both (default)")

    parser.add_argument("--servers-index", default="servers")
    parser.add_argument("--packages-index", default="server_packages")
    parser.add_argument("--servers-history-stream", default="servers_history")
    parser.add_argument("--packages-history-stream",
                         default="server_packages_history")

    parser.add_argument("--snapshot-date", default=None,
                         help="Override the snapshot date (YYYY-MM-DD), "
                              "default: today (UTC)")
    parser.add_argument("--recreate", action="store_true",
                         help="Delete and recreate the OPTION A indices "
                              "with the mappings above before loading "
                              "(DESTRUCTIVE - only affects --mode current)")
    parser.add_argument("--history-retention-days", type=int, default=None,
                         help="If set, attach an ILM policy to the history "
                              "data streams that deletes snapshots older "
                              "than this many days")
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

    print(f"Parsed {len(server_docs)} servers and {len(package_docs)} "
          f"package rows from {args.json_file} (snapshot_date={snapshot_date})")

    if args.dry_run:
        print("Dry run - not connecting to Elasticsearch.")
        if server_docs:
            print("Sample current-state server doc:")
            print(json.dumps(server_docs[0][1], indent=2))
        if package_docs:
            print("Sample current-state package doc:")
            print(json.dumps(package_docs[0][1], indent=2))
            print("Sample history package doc:")
            print(json.dumps(
                to_history_doc(package_docs[0][1], timestamp), indent=2))
        return

    try:
        from elasticsearch import Elasticsearch
        from elasticsearch.helpers import bulk
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
    # OPTION A - current state: refresh indices, then drop anything
    # that wasn't part of today's snapshot (decommissioned servers /
    # removed packages disappear from the dashboard automatically).
    # -----------------------------------------------------------------
    if args.mode in ("current", "both"):
        ensure_current_index(es, args.servers_index, SERVERS_MAPPING,
                              args.recreate)
        ensure_current_index(es, args.packages_index, SERVER_PACKAGES_MAPPING,
                              args.recreate)

        bulk_index(es, bulk, args.servers_index, server_docs)
        bulk_index(es, bulk, args.packages_index, package_docs)

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
            )
            deleted = resp.get("deleted", 0)
            print(f"{index}: removed {deleted} stale doc(s) not in "
                  f"today's snapshot")

    # -----------------------------------------------------------------
    # OPTION B - history: append this snapshot to the data streams.
    # Nothing is deleted here except via the optional ILM policy.
    # -----------------------------------------------------------------
    if args.mode in ("history", "both"):
        ilm_policy_name = None
        if args.history_retention_days:
            ilm_policy_name = "inventory-history-retention"
            ensure_ilm_policy(es, ilm_policy_name,
                               args.history_retention_days)

        ensure_history_data_stream(
            es, args.servers_history_stream, SERVERS_HISTORY_MAPPING,
            ilm_policy_name)
        ensure_history_data_stream(
            es, args.packages_history_stream, SERVER_PACKAGES_HISTORY_MAPPING,
            ilm_policy_name)

        history_server_docs = [
            (None, to_history_doc(doc, timestamp)) for _, doc in server_docs
        ]
        history_package_docs = [
            (None, to_history_doc(doc, timestamp)) for _, doc in package_docs
        ]

        # Data streams only accept the "create" op_type.
        bulk_index(es, bulk, args.servers_history_stream,
                   history_server_docs, op_type="create")
        bulk_index(es, bulk, args.packages_history_stream,
                   history_package_docs, op_type="create")


if __name__ == "__main__":
    main()
