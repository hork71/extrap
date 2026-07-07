import json
import csv

with open('/mnt/user-data/uploads/pdrift.json') as f:
    data = json.load(f)

rows = []
for pkg in data['buckets']:
    pkg_name = pkg['key']
    total = pkg['doc_count']
    versions = pkg['versions']['buckets']
    num_versions = len(versions)
    for v in versions:
        pct = round(v['doc_count'] / total * 100, 2) if total else 0
        rows.append({
            'package': pkg_name,
            'version': v['key'],
            'server_count': v['doc_count'],
            'pct_of_package': pct,
            'total_versions_seen': num_versions,
            'drift_flag': 'YES' if num_versions > 1 else 'NO'
        })

with open('package_drift.csv', 'w', newline='') as f:
    writer = csv.DictWriter(f, fieldnames=rows[0].keys())
    writer.writeheader()
    writer.writerows(rows)

print(f"Wrote {len(rows)} rows")
