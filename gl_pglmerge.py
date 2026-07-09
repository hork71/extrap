#!/usr/bin/env python3
"""
gitlab_mr_monthly_report.py

List all merge requests for a GitLab project, grouped by month, showing
status information per MR and — for merged MRs — who merged/approved it.

Requires only the standard library + `requests`:
    pip install requests

Usage examples
--------------
# Basic: all MRs in project 12345678, grouped by month of creation
python gitlab_mr_monthly_report.py --url https://gitlab.com --project 12345678 --token $GITLAB_TOKEN

# Use project path instead of numeric ID, group by merge date instead of created date,
# restrict to a date range, and write CSV
python gitlab_mr_monthly_report.py \
    --url https://gitlab.com \
    --project mygroup/mysubgroup/myproject \
    --token $GITLAB_TOKEN \
    --group-by merged \
    --since 2025-01-01 \
    --until 2025-12-31 \
    --csv mrs_2025.csv

Notes
-----
- `--project` accepts either the numeric project ID or the URL-encoded
  path (e.g. "mygroup/myproject" — the script will URL-encode it for you).
- Approval information (`--approvals`) uses the
  /merge_requests/:iid/approvals endpoint. On GitLab CE/Free instances
  this endpoint exists but approval *rules* are a paid feature, so the
  "approved_by" list may always be empty there — the script won't error,
  it will just show "-" for approvers on those instances.
- A personal/project access token with at least `read_api` scope is
  required for private projects.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime
from urllib.parse import quote

try:
    import requests
except ImportError:
    sys.exit("This script requires the 'requests' package: pip install requests")


def parse_args():
    p = argparse.ArgumentParser(
        description="Report GitLab merge requests grouped by month with status and merge/approval info."
    )
    p.add_argument("--url", required=True, help="Base GitLab URL, e.g. https://gitlab.com")
    p.add_argument("--project", required=True, help="Project ID (numeric) or path (e.g. group/subgroup/project)")
    p.add_argument("--token", required=True, help="Personal/project access token (needs read_api scope)")
    p.add_argument(
        "--state",
        default="all",
        choices=["all", "opened", "closed", "merged", "locked"],
        help="Filter MRs by state (default: all)",
    )
    p.add_argument(
        "--group-by",
        default="created",
        choices=["created", "merged", "updated"],
        help="Which date field to group MRs by month on (default: created)",
    )
    p.add_argument("--since", help="Only include MRs on/after this date (YYYY-MM-DD), applied to the group-by field")
    p.add_argument("--until", help="Only include MRs on/before this date (YYYY-MM-DD), applied to the group-by field")
    p.add_argument(
        "--approvals",
        action="store_true",
        help="Fetch approval info per MR (extra API call per MR — slower, but shows who approved it)",
    )
    p.add_argument("--csv", metavar="FILE", help="Write results to a CSV file instead of (or in addition to) printing")
    p.add_argument("--no-print", action="store_true", help="Suppress console table output (useful with --csv)")
    p.add_argument("--per-page", type=int, default=100, help="API page size (default 100, max 100)")
    p.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    return p.parse_args()


def api_get(session, base_url, path, params=None):
    """GET with pagination handling. Returns a list of all items across pages."""
    url = f"{base_url}/api/v4{path}"
    results = []
    page = 1
    params = dict(params or {})
    params["page"] = page
    while True:
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 401:
            sys.exit("Authentication failed (401). Check your --token.")
        if resp.status_code == 403:
            sys.exit("Forbidden (403). Token may lack required scope/permissions for this project.")
        if resp.status_code == 404:
            sys.exit(f"Not found (404) for {url}. Check --project and --url.")
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            return data  # single-object endpoint

        next_page = resp.headers.get("X-Next-Page")
        if not next_page:
            break
        params["page"] = next_page
    return results


def single_get(session, base_url, path, params=None):
    """GET a single (non-paginated) resource. Returns None on 404/403."""
    url = f"{base_url}/api/v4{path}"
    resp = session.get(url, params=params or {}, timeout=30)
    if resp.status_code in (403, 404):
        return None
    resp.raise_for_status()
    return resp.json()


def month_key(iso_str):
    if not iso_str:
        return None
    dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
    return dt.strftime("%Y-%m")


def in_range(iso_str, since, until):
    if not iso_str:
        return since is None and until is None  # no date -> only include if no filter set
    dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
    if since and dt < since:
        return False
    if until and dt > until:
        return False
    return True


def main():
    args = parse_args()
    base_url = args.url.rstrip("/")
    project = args.project if args.project.isdigit() else quote(args.project, safe="")

    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else None

    session = requests.Session()
    session.headers.update({"PRIVATE-TOKEN": args.token})
    if args.insecure:
        session.verify = False
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    print(f"Fetching merge requests for project '{args.project}' (state={args.state})...", file=sys.stderr)

    mrs = api_get(
        session,
        base_url,
        f"/projects/{project}/merge_requests",
        params={"state": args.state, "per_page": args.per_page, "order_by": "created_at", "sort": "asc"},
    )

    date_field_map = {"created": "created_at", "merged": "merged_at", "updated": "updated_at"}
    date_field = date_field_map[args.group_by]

    rows = []
    for mr in mrs:
        group_date = mr.get(date_field)

        # If grouping by "merged" but MR isn't merged, skip it (no merge date to group on)
        if args.group_by == "merged" and not group_date:
            continue

        if not in_range(group_date, since, until):
            continue

        approvers = "-"
        if args.approvals:
            approvals_data = single_get(
                session, base_url, f"/projects/{project}/merge_requests/{mr['iid']}/approvals"
            )
            if approvals_data and approvals_data.get("approved_by"):
                names = [a["user"]["username"] for a in approvals_data["approved_by"]]
                approvers = ", ".join(names) if names else "-"
            elif approvals_data is not None:
                approvers = "-"  # endpoint reachable but nobody approved / not tracked
            else:
                approvers = "n/a"  # endpoint not accessible on this tier/instance

        merged_by = mr.get("merged_by", {})
        merged_by_username = merged_by["username"] if merged_by else "-"

        rows.append(
            {
                "month": month_key(group_date) or "(no date)",
                "iid": mr["iid"],
                "title": mr["title"],
                "state": mr["state"],
                "author": mr["author"]["username"] if mr.get("author") else "-",
                "created_at": mr.get("created_at", "")[:10],
                "merged_at": (mr.get("merged_at") or "-")[:10] if mr.get("merged_at") else "-",
                "merged_by": merged_by_username,
                "approved_by": approvers,
                "web_url": mr.get("web_url", ""),
            }
        )

    grouped = defaultdict(list)
    for row in rows:
        grouped[row["month"]].append(row)

    if not args.no_print:
        for month in sorted(grouped.keys()):
            month_rows = grouped[month]
            print(f"\n=== {month} ({len(month_rows)} MR{'s' if len(month_rows) != 1 else ''}) ===")
            for r in month_rows:
                print(f"  !{r['iid']:<5} [{r['state']:<8}] {r['title'][:60]:<60}")
                print(f"          author: {r['author']:<15} created: {r['created_at']}")
                if r["state"] == "merged":
                    print(f"          merged_at: {r['merged_at']:<12} merged_by: {r['merged_by']}")
                    if args.approvals:
                        print(f"          approved_by: {r['approved_by']}")
                print(f"          {r['web_url']}")

        total = len(rows)
        merged_count = sum(1 for r in rows if r["state"] == "merged")
        print(f"\nTotal MRs: {total} | Merged: {merged_count} | Months: {len(grouped)}")

    if args.csv:
        fieldnames = [
            "month",
            "iid",
            "title",
            "state",
            "author",
            "created_at",
            "merged_at",
            "merged_by",
            "approved_by",
            "web_url",
        ]
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for month in sorted(grouped.keys()):
                for r in grouped[month]:
                    writer.writerow(r)
        print(f"\nCSV written to {args.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
