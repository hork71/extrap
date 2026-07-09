#!/usr/bin/env python3
"""
gitlab_mr_monthly_report_pygitlab.py

Same report as gitlab_mr_monthly_report.py, but built on the `python-gitlab`
library instead of raw `requests` calls. python-gitlab handles pagination,
auth headers, and object modeling for you.

Install:
    pip install python-gitlab

Usage examples
--------------
python gitlab_mr_monthly_report_pygitlab.py \
    --url https://gitlab.com \
    --project mygroup/myproject \
    --token $GITLAB_TOKEN

python gitlab_mr_monthly_report_pygitlab.py \
    --url https://gitlab.com \
    --project 12345678 \
    --token $GITLAB_TOKEN \
    --group-by merged \
    --since 2025-01-01 \
    --until 2025-12-31 \
    --approvals \
    --csv mrs_2025.csv

Notes
-----
- `--project` accepts the numeric project ID or the path
  (e.g. "group/subgroup/project") — python-gitlab handles encoding.
- `--approvals` calls mr.approvals.get() per MR. On GitLab CE/Free this
  endpoint exists but approval rules are a paid feature, so approved_by
  may always be empty there ("-" will be shown, not an error).
- A personal/project access token with at least `read_api` scope is
  required for private projects.
"""

import argparse
import csv
import sys
from collections import defaultdict
from datetime import datetime

try:
    import gitlab
except ImportError:
    sys.exit("This script requires python-gitlab: pip install python-gitlab")


def parse_args():
    p = argparse.ArgumentParser(
        description="Report GitLab merge requests grouped by month with status and merge/approval info (python-gitlab version)."
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
    p.add_argument("--insecure", action="store_true", help="Disable TLS certificate verification")
    return p.parse_args()


def month_key(iso_str):
    if not iso_str:
        return None
    dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
    return dt.strftime("%Y-%m")


def in_range(iso_str, since, until):
    if not iso_str:
        return since is None and until is None
    dt = datetime.strptime(iso_str[:10], "%Y-%m-%d")
    if since and dt < since:
        return False
    if until and dt > until:
        return False
    return True


def main():
    args = parse_args()
    since = datetime.strptime(args.since, "%Y-%m-%d") if args.since else None
    until = datetime.strptime(args.until, "%Y-%m-%d") if args.until else None

    gl = gitlab.Gitlab(args.url, private_token=args.token, ssl_verify=not args.insecure)
    try:
        gl.auth()
    except gitlab.exceptions.GitlabAuthenticationError:
        sys.exit("Authentication failed. Check your --token.")

    try:
        project = gl.projects.get(args.project)
    except gitlab.exceptions.GitlabGetError as e:
        sys.exit(f"Could not fetch project '{args.project}': {e}")

    print(f"Fetching merge requests for project '{project.path_with_namespace}' (state={args.state})...", file=sys.stderr)

    list_kwargs = {"order_by": "created_at", "sort": "asc", "all": True}
    if args.state != "all":
        list_kwargs["state"] = args.state

    mrs = project.mergerequests.list(**list_kwargs)

    date_field_map = {"created": "created_at", "merged": "merged_at", "updated": "updated_at"}
    date_field = date_field_map[args.group_by]

    rows = []
    for mr in mrs:
        group_date = getattr(mr, date_field, None)

        if args.group_by == "merged" and not group_date:
            continue

        if not in_range(group_date, since, until):
            continue

        approvers = "-"
        if args.approvals:
            try:
                approval_info = mr.approvals.get()
                approved_by = getattr(approval_info, "approved_by", None) or []
                if approved_by:
                    names = [a["user"]["username"] for a in approved_by]
                    approvers = ", ".join(names)
                else:
                    approvers = "-"
            except gitlab.exceptions.GitlabGetError:
                approvers = "n/a"  # endpoint not available (permissions/tier)

        merged_by = getattr(mr, "merged_by", None)
        merged_by_username = merged_by["username"] if merged_by else "-"

        rows.append(
            {
                "month": month_key(group_date) or "(no date)",
                "iid": mr.iid,
                "title": mr.title,
                "state": mr.state,
                "author": mr.author["username"] if getattr(mr, "author", None) else "-",
                "created_at": (mr.created_at or "")[:10],
                "merged_at": (mr.merged_at or "-")[:10] if getattr(mr, "merged_at", None) else "-",
                "merged_by": merged_by_username,
                "approved_by": approvers,
                "web_url": mr.web_url,
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
