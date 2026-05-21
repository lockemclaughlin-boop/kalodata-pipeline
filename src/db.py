"""
SQLite schema + helpers. One row per run, one row per product per run, one row
per generation step. Keeps the pipeline resumable and gives the UI a history view.

Phase 3 build target.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    filters_json    TEXT NOT NULL,
    status          TEXT NOT NULL,    -- running | completed | failed | cancelled
    total_cost_usd  REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS products (
    run_id          TEXT NOT NULL,
    product_id      TEXT NOT NULL,
    title           TEXT,
    kalodata_url    TEXT,
    gmv_usd         REAL,
    units_sold      INTEGER,
    selected        INTEGER NOT NULL DEFAULT 0,
    source_photo    TEXT,
    staged_image    TEXT,
    video_path      TEXT,
    status          TEXT NOT NULL,    -- pending | staged | generating | completed | failed
    error           TEXT,
    cost_usd        REAL DEFAULT 0,
    PRIMARY KEY (run_id, product_id),
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn
