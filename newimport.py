import json
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values


def parse_install_time(value):
    """Convert RPM install time to datetime."""

    if not value:
        return None

    value = value.rsplit(" ", 1)[0]

    return datetime.strptime(
        value,
        "%m/%d/%y %I:%M:%S %p"
    )


conn = psycopg2.connect(
    host="localhost",
    database="inventory",
    user="postgres",
    password="secret"
)

conn.autocommit = False

cur = conn.cursor()

#
# ----------------------------------------------------
# Create inventory run
# ----------------------------------------------------
#

cur.execute("""
INSERT INTO inventory_runs(source)
VALUES(%s)
RETURNING id;
""", ("JSON Import",))

inventory_run_id = cur.fetchone()[0]

print(f"Inventory run {inventory_run_id}")

#
# ----------------------------------------------------
# Load JSON
# ----------------------------------------------------
#

with open("structure.json") as f:
    servers = json.load(f)

#
# ----------------------------------------------------
# Import servers
# ----------------------------------------------------
#

for server in servers:

    hostname = (
        server.get("name")
        or server.get("naam")
    )

    #
    # UPSERT SERVER
    #

    cur.execute("""
    INSERT INTO servers (

        id,
        hostname,
        server_group,
        email,
        owner,
        lifecycle_status,
        managed,

        inventory_status,
        last_seen,
        last_inventory_run

    )
    VALUES (
        %s,%s,%s,%s,%s,%s,%s,
        'ACTIVE',
        NOW(),
        %s
    )

    ON CONFLICT (id)

    DO UPDATE SET

        hostname = EXCLUDED.hostname,
        server_group = EXCLUDED.server_group,
        email = EXCLUDED.email,
        owner = EXCLUDED.owner,
        lifecycle_status = EXCLUDED.lifecycle_status,
        managed = EXCLUDED.managed,

        inventory_status = 'ACTIVE',
        last_seen = NOW(),
        last_inventory_run = EXCLUDED.last_inventory_run;
    """,
    (
        server["uuid"],
        hostname,
        server["group"],
        server["email"],
        server["owner"],
        server["sl"],
        server["managed"],
        inventory_run_id
    ))

    #
    # Remove previous package inventory
    #

    cur.execute("""
    DELETE FROM server_packages
    WHERE server_id=%s
    """,
    (server["uuid"],))

    #
    # Import packages
    #

    for pkg in server["extraPackages"]:

        #
        # package
        #

        cur.execute("""
        INSERT INTO packages(name)
        VALUES(%s)
        ON CONFLICT(name)
        DO NOTHING
        RETURNING id;
        """,
        (pkg["name"],))

        row = cur.fetchone()

        if row:
            package_id = row[0]
        else:
            cur.execute("""
            SELECT id
            FROM packages
            WHERE name=%s
            """,
            (pkg["name"],))

            package_id = cur.fetchone()[0]

        #
        # package version
        #

        cur.execute("""
        INSERT INTO package_versions(

            package_id,
            version,
            release,
            arch

        )
        VALUES(%s,%s,%s,%s)

        ON CONFLICT(
            package_id,
            version,
            release,
            arch
        )

        DO NOTHING

        RETURNING id;
        """,
        (
            package_id,
            pkg["version"],
            pkg["release"],
            pkg["arch"]
        ))

        row = cur.fetchone()

        if row:
            package_version_id = row[0]
        else:

            cur.execute("""
            SELECT id

            FROM package_versions

            WHERE

                package_id=%s
                AND version=%s
                AND release=%s
                AND arch=%s
            """,
            (
                package_id,
                pkg["version"],
                pkg["release"],
                pkg["arch"]
            ))

            package_version_id = cur.fetchone()[0]

        #
        # Link server/package
        #

        cur.execute("""
        INSERT INTO server_packages(

            server_id,
            package_version_id,
            install_time

        )

        VALUES(%s,%s,%s)

        ON CONFLICT(
            server_id,
            package_version_id
        )

        DO UPDATE SET

            install_time = EXCLUDED.install_time;
        """,
        (
            server["uuid"],
            package_version_id,
            parse_install_time(pkg["installtime"])
        ))

#
# ----------------------------------------------------
# Mark missing servers
# ----------------------------------------------------
#

cur.execute("""
UPDATE servers

SET inventory_status='MISSING'

WHERE

    inventory_status='ACTIVE'

    AND last_inventory_run <> %s;
""",
(inventory_run_id,))

#
# ----------------------------------------------------
# Finish inventory run
# ----------------------------------------------------
#

cur.execute("""
UPDATE inventory_runs

SET

    completed_at = NOW(),
    server_count = %s

WHERE id=%s;
""",
(
    len(servers),
    inventory_run_id
))

conn.commit()

cur.close()
conn.close()

print("Inventory import completed.")
