-- ===========================================================================
-- Health domain plugin schema.
--
-- NOTE: there is NO identity table here. Users and tenants are kernel concepts
-- and live in core/schema.sql; this domain only *references* app_user(user_id).
-- A domain plugin must never define identity tables (docs/05 A2, docs/07 C6).
--
-- An earlier draft of this file defined its own user table. That design was
-- abandoned when identity moved into the kernel, and nothing here replaces it.
-- ===========================================================================

CREATE TABLE IF NOT EXISTS medical_report (
    report_id         TEXT PRIMARY KEY,
    user_id           TEXT NOT NULL REFERENCES app_user(user_id),
    -- Which intake produced this report. NULL for rows entered by hand, which have no intake.
    -- Pointing this way (domain -> kernel) is what keeps the kernel free of domain knowledge.
    -- The relation is 1:N: one file can yield several reports, e.g. a checkup covering multiple
    -- departments in v2.2 - which is why this is a column here and not a report_id on the task.
    ingestion_task_id TEXT REFERENCES ingestion_task(task_id),
    report_type       TEXT NOT NULL,          -- 'ultrasound' | 'gastroscopy' | 'lab' | ...
    check_time        TIMESTAMP NOT NULL,
    institution       TEXT,
    note              TEXT,
    created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Reverse lookup: deleting a report must be able to find the intake it came from.
CREATE INDEX IF NOT EXISTS idx_report_task ON medical_report(ingestion_task_id);

CREATE INDEX IF NOT EXISTS idx_report_user_time ON medical_report(user_id, check_time);

-- One row per extracted indicator. Provenance columns are NOT optional decoration:
-- a medical value with no traceable source is a liability (docs/02 D6).
CREATE TABLE IF NOT EXISTS medical_index (
    index_id     TEXT PRIMARY KEY,
    report_id    TEXT NOT NULL REFERENCES medical_report(report_id) ON DELETE CASCADE,
    index_name   TEXT NOT NULL,              -- '结石直径' / '总胆红素' / ...
    index_value  REAL,                       -- numeric value when parseable
    value_text   TEXT,                       -- non-numeric or composite values, verbatim
    unit         TEXT,
    ref_range    TEXT,
    is_verified  INTEGER NOT NULL DEFAULT 0, -- 0 = AI extracted, not human verified
    source       TEXT NOT NULL DEFAULT 'manual'
                 CHECK (source IN ('manual', 'parsed', 'ocr')),
    raw_text     TEXT,                       -- the sentence this value was read from
    verified_at  TIMESTAMP,
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_index_name ON medical_index(index_name);
CREATE INDEX IF NOT EXISTS idx_index_report ON medical_index(report_id);

-- NOTE for SQLite: ON DELETE CASCADE only fires when the connection enables
-- `PRAGMA foreign_keys = ON`. storage/db.py must set it on every connection.

