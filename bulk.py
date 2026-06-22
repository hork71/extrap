from elasticsearch import Elasticsearch, helpers
import json

es = Elasticsearch(
    "http://localhost:9200",
    basic_auth=("elastic", "password")
)

with open("structure.json") as f:
    data = json.load(f)

actions = []

for server in data:

    hostname = (
        server.get("name")
        or server.get("naam")
    )

    document = {
        "uuid": server["uuid"],
        "hostname": hostname,
        "group": server["group"],
        "owner": server["owner"],
        "email": server["email"],
        "managed": server["managed"],
        "lifecycle_status": server["sl"],
        "packages": server["extraPackages"]
    }

    actions.append({
        "_index": "server_inventory",
        "_id": server["uuid"],
        "_source": document
    })

helpers.bulk(es, actions)

print("Done")
