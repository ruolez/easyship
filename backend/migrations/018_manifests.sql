-- End-of-day carrier manifests (USPS SCAN forms). One row per manifest the
-- carrier issued; shipments point at the manifest that covered them, which is
-- also what keeps a label from being manifested twice.
CREATE TABLE manifests (
    id SERIAL PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_label TEXT,
    carrier TEXT,
    provider_manifest_id TEXT,
    ref_number TEXT,
    shipment_count INT NOT NULL DEFAULT 0,
    document_path TEXT,
    status TEXT NOT NULL DEFAULT 'creating' CHECK (status IN ('creating', 'ready', 'failed')),
    error_message TEXT,
    raw JSONB,
    created_by INT REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE shipments ADD COLUMN manifest_id INT REFERENCES manifests(id);

-- The eligible-parcels query only ever scans unmanifested, labeled rows.
CREATE INDEX idx_shipments_manifestable
    ON shipments (provider, label_created_at)
    WHERE manifest_id IS NULL AND status IN ('label_created', 'fulfilled');
