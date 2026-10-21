-- 0017_nodes: `nox node` (spec wave C §1.2). A node is a paired device (`paired_devices`,
-- channel 'node', role 'node') that holds a client certificate of this installation's authority.
--
-- Nothing secret is stored here. The node's private key never leaves the node; `spki_sha256` is
-- the hash of its *public* key, `serial` the certificate's serial number. A certificate is
-- refused by the node listener as soon as its row is revoked (the denylist) or its device is.

-- What the core knows about each node beyond the pairing row.
CREATE TABLE IF NOT EXISTS nodes (
    device_id        TEXT PRIMARY KEY REFERENCES paired_devices(id),
    os               TEXT NOT NULL DEFAULT '',
    arch             TEXT NOT NULL DEFAULT '',
    version          TEXT NOT NULL DEFAULT '',
    -- A paired browser device on the same machine (its dashboard), for "origin" routing (C3).
    linked_device_id TEXT,
    -- SHA-256 of the capability manifest the node reported last.
    manifest_hash    TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL
);

-- Every client certificate ever issued. A revoked serial never works again (denylist), even
-- before it expires and even if the node was offline when it was revoked.
CREATE TABLE IF NOT EXISTS node_certs (
    serial         TEXT PRIMARY KEY,
    device_id      TEXT NOT NULL REFERENCES paired_devices(id),
    spki_sha256    TEXT NOT NULL,
    issued_at      TEXT NOT NULL,
    not_after      TEXT NOT NULL,
    revoked_at     TEXT,
    revoked_reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_node_certs_device ON node_certs(device_id);

-- Capability groups per node. `enabled_on_node` is what the node itself reports (only the device
-- can widen it); `narrowed` is the core's switch, which can only take a group away.
CREATE TABLE IF NOT EXISTS node_capabilities (
    device_id       TEXT NOT NULL REFERENCES paired_devices(id),
    grp             TEXT NOT NULL,
    enabled_on_node INTEGER NOT NULL DEFAULT 0,
    narrowed        INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (device_id, grp)
);
