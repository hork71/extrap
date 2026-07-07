CREATE TABLE servers (
    id UUID PRIMARY KEY,
    hostname VARCHAR(255) NOT NULL,
    server_group VARCHAR(100),
    email VARCHAR(255),
    owner VARCHAR(100),
    lifecycle_status VARCHAR(20),
    managed BOOLEAN
);

CREATE TABLE packages (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE
);

CREATE TABLE package_versions (
    id BIGSERIAL PRIMARY KEY,
    package_id BIGINT NOT NULL REFERENCES packages(id),

    version VARCHAR(100) NOT NULL,
    release VARCHAR(100) NOT NULL,
    arch VARCHAR(50) NOT NULL,

    UNIQUE (
        package_id,
        version,
        release,
        arch
    )
);

CREATE TABLE server_packages (
    server_id UUID NOT NULL REFERENCES servers(id),
    package_version_id BIGINT NOT NULL REFERENCES package_versions(id),

    install_time TIMESTAMPTZ,

    PRIMARY KEY (
        server_id,
        package_version_id
    )
);

CREATE TABLE inventory_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    source VARCHAR(100),
    server_count INTEGER DEFAULT 0
);

ALTER TABLE servers
ADD COLUMN inventory_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
    CHECK (inventory_status IN ('ACTIVE','MISSING','DECOMMISSIONED'));

ALTER TABLE servers
ADD COLUMN last_seen TIMESTAMPTZ;

ALTER TABLE servers
ADD COLUMN last_inventory_run BIGINT
REFERENCES inventory_runs(id);
