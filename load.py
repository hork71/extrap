#!/usr/bin/env python3
"""
load_inventory.py

Reads a structure.json file (list of servers, each with an extraPackages
array) and loads it into two Elasticsearch indices:

  1. `servers`          - one document per server, extraPackages kept as a
                           nested array (for precise per-server/package
                           correlated queries).
  2. `server_packages`  - one document per (server, package) pair, with the
                           server's fields denormalized onto every row
                           (this is the index Kibana dashboards should use).

Usage:
    pip install elasticsearch>=8.0.0
    python load_inventory.py structure.json \
        --host https://localhost:9200 \
        --api-key <base64-api-key>          # or use --user/--password

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


# ---------------------------------------------------------------------------
# Index mappings
# ---------------------------------------------------------------------------
SERVERS_MAPPING = {
    "mappings": {
        "properties": {
            "naam": {"type": "keyword"},
            "group": {"type": "keyword"},
            "email": {"type": "keyword"},
            "owner": {"type": "keyword"},
            "sl": {"type": "keyword"},
            "managed": {"type": "boolean"},
            "uuid": {"type": "keyword"},
            "package_count": {"type": "integer"},
            "extraPackages": {
                "type": "nested",
                "properties": {
                    "name": {"type": "keyword"},
                    "version": {"type": "keyword"},
                    "release": {"type": "keyword"},
                    "arch": {"type": "keyword"},
                    "installtime": {"type": "date"},
                },
            },
        }
    }
}

SERVER_PACKAGES_MAPPING = {
    "mappings": {
        "properties": {
            "server": {
                "properties": {
                    "naam": {"type": "keyword"},
                    "group": {"type": "keyword"},
                    "email": {"type": "keyword"},
                    "owner": {"type": "keyword"},
                    "sl": {"type": "keyword"},
                    "managed": {"type": "boolean"},
                    "uuid": {"type": "keyword"},
                }
            },
            "package": {
                "properties": {
                    "name": {"type": "keyword"},
                    "version": {"type": "keyword"},
                    "release": {"type": "keyword"},
                    "arch": {"type": "keyword"},
                    "installtime": {"type": "date"},
                }
            },
        }
    }
}


# ---------------------------------------------------------------------------
# Document builders
# ---------------------------------------------------------------------------
def build_server_doc(server):
    packages = server.get("extraPackages", []) or []
    nested_packages = []
    for pkg in packages:
        nested_packages.append({
            "name": pkg.get("name"),
            "version": pkg.get("version"),
            "release": pkg.get("release"),
            "arch": pkg.get("arch"),
            "installtime": parse_installtime(pkg.get("installtime")),
        })

    doc = {
        "naam": server.get("naam"),
        "group": server.get("group"),
        "email": server.get("email"),
        "owner": server.get("owner"),
        "sl": server.get("sl"),
        "managed": server.get("managed"),
        "uuid": server.get("uuid"),
        "package_count": len(packages),
        "extraPackages": nested_packages,
    }
    doc_id = server.get("uuid")
    return doc_id, doc


def build_package_docs(server):
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
        }
        # deterministic id so re-running the loader updates rather than
        # duplicates rows: <server_uuid>_<package_name>_<version>_<release>_<arch>
        doc_id = "_".join(str(x) for x in [
            server.get("uuid"), pkg.get("name"), pkg.get("version"),
            pkg.get("release"), pkg.get("arch"),
        ])
        yield doc_id, doc


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
    parser.add_argument("--servers-index", default="servers")
    parser.add_argument("--packages-index", default="server_packages")
    parser.add_argument("--recreate", action="store_true",
                         help="Delete and recreate indices with the mappings "
                              "above before loading (DESTRUCTIVE)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Parse and report counts, but don't touch ES")
    args = parser.parse_args()

    with open(args.json_file, "r", encoding="utf-8") as f:
        servers = json.load(f)

    server_docs = []
    package_docs = []
    for server in servers:
        server_docs.append(build_server_doc(server))
        package_docs.extend(build_package_docs(server))

    print(f"Parsed {len(server_docs)} servers and {len(package_docs)} "
          f"package rows from {args.json_file}")

    if args.dry_run:
        print("Dry run - not connecting to Elasticsearch.")
        if server_docs:
            print("Sample server doc:")
            print(json.dumps(server_docs[0][1], indent=2))
        if package_docs:
            print("Sample package doc:")
            print(json.dumps(package_docs[0][1], indent=2))
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

    if args.recreate:
        for index, mapping in (
            (args.servers_index, SERVERS_MAPPING),
            (args.packages_index, SERVER_PACKAGES_MAPPING),
        ):
            if es.indices.exists(index=index):
                print(f"Deleting existing index '{index}'")
                es.indices.delete(index=index)
            print(f"Creating index '{index}'")
            es.indices.create(index=index, body=mapping)
    else:
        for index, mapping in (
            (args.servers_index, SERVERS_MAPPING),
            (args.packages_index, SERVER_PACKAGES_MAPPING),
        ):
            if not es.indices.exists(index=index):
                print(f"Creating index '{index}'")
                es.indices.create(index=index, body=mapping)

    def gen_actions(index, docs):
        for doc_id, doc in docs:
            yield {
                "_op_type": "index",
                "_index": index,
                "_id": doc_id,
                "_source": doc,
            }

    success, errors = bulk(es, gen_actions(args.servers_index, server_docs),
                            raise_on_error=False)
    print(f"servers index: {success} indexed, {len(errors)} errors")
    for err in errors[:5]:
        print("  ", err)

    success, errors = bulk(es, gen_actions(args.packages_index, package_docs),
                            raise_on_error=False)
    print(f"server_packages index: {success} indexed, {len(errors)} errors")
    for err in errors[:5]:
        print("  ", err)


if __name__ == "__main__":
    main()
