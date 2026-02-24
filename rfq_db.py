"""
rfq_db.py — SQLite database layer for the RFQ Bid Management System.

Schema
------
rfqs          — one row per loaded RFQ
rfq_items     — one row per line item within an RFQ
bidders       — master list of unique bidder names
bids          — one row per (item, bidder) pair
"""

import sqlite3
import os
from datetime import datetime

# Default DB path sits next to this script
_DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rfq_database.db")


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def get_conn(db_path=None):
    path = db_path or _DEFAULT_DB
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS rfqs (
    rfq_id       TEXT    PRIMARY KEY,
    creator      TEXT,
    station      TEXT,
    project      TEXT,
    rfq_date     TEXT,
    source_file  TEXT,
    sheet_name   TEXT,
    is_potential INTEGER DEFAULT 0,   -- 1 = potential RFQ (no bids)
    notes        TEXT,
    loaded_at    TEXT    DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rfq_items (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id        TEXT    NOT NULL REFERENCES rfqs(rfq_id) ON DELETE CASCADE,
    item_number   TEXT,
    item_type     TEXT,
    specification TEXT,
    size          TEXT,
    unit          TEXT,
    quantity      REAL
);
CREATE INDEX IF NOT EXISTS idx_items_rfq    ON rfq_items(rfq_id);
CREATE INDEX IF NOT EXISTS idx_items_type   ON rfq_items(item_type);

CREATE TABLE IF NOT EXISTS bidders (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT    UNIQUE NOT NULL
);

CREATE TABLE IF NOT EXISTS bids (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    rfq_id       TEXT    NOT NULL REFERENCES rfqs(rfq_id)       ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES rfq_items(id)      ON DELETE CASCADE,
    bidder_id    INTEGER NOT NULL REFERENCES bidders(id),
    unit_price   REAL,
    ext_price    REAL
);
CREATE INDEX IF NOT EXISTS idx_bids_rfq     ON bids(rfq_id);
CREATE INDEX IF NOT EXISTS idx_bids_bidder  ON bids(bidder_id);
CREATE INDEX IF NOT EXISTS idx_bids_item    ON bids(item_id);
"""


def init_db(db_path=None):
    """Create all tables if they don't exist yet."""
    conn = get_conn(db_path)
    conn.executescript(DDL)
    # Migrate: add project column to existing databases that pre-date this field
    try:
        conn.execute("ALTER TABLE rfqs ADD COLUMN project TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists
    conn.close()


# ---------------------------------------------------------------------------
# RFQ operations
# ---------------------------------------------------------------------------

def rfq_exists(rfq_id, db_path=None):
    conn = get_conn(db_path)
    row = conn.execute("SELECT 1 FROM rfqs WHERE rfq_id=?", (rfq_id,)).fetchone()
    conn.close()
    return row is not None


def delete_rfq(rfq_id, db_path=None):
    """Delete an RFQ and all cascaded items/bids."""
    conn = get_conn(db_path)
    conn.execute("DELETE FROM rfqs WHERE rfq_id=?", (rfq_id,))
    conn.commit()
    conn.close()


def insert_rfq(rfq_id, creator, station, project, rfq_date, source_file, sheet_name,
               is_potential=False, notes=None, db_path=None):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO rfqs (rfq_id, creator, station, project, rfq_date, source_file,
                             sheet_name, is_potential, notes)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (rfq_id, creator, station, project, rfq_date, source_file, sheet_name,
         1 if is_potential else 0, notes)
    )
    conn.commit()
    conn.close()


def get_all_rfqs(db_path=None):
    conn = get_conn(db_path)
    rows = conn.execute(
        """SELECT r.rfq_id, r.creator, r.station, r.project, r.rfq_date,
                  r.source_file, r.sheet_name, r.is_potential, r.loaded_at,
                  COUNT(DISTINCT i.id)  AS item_count,
                  COUNT(DISTINCT b.bidder_id) AS bidder_count
           FROM rfqs r
           LEFT JOIN rfq_items i ON i.rfq_id = r.rfq_id
           LEFT JOIN bids      b ON b.rfq_id = r.rfq_id
           GROUP BY r.rfq_id
           ORDER BY r.rfq_date DESC, r.loaded_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_rfq_detail(rfq_id, db_path=None):
    conn = get_conn(db_path)
    rfq = conn.execute("SELECT * FROM rfqs WHERE rfq_id=?", (rfq_id,)).fetchone()
    if not rfq:
        conn.close()
        return None
    items = conn.execute(
        "SELECT * FROM rfq_items WHERE rfq_id=? ORDER BY CAST(item_number AS REAL), item_number",
        (rfq_id,)
    ).fetchall()
    item_ids = [i["id"] for i in items]
    bids_all = []
    if item_ids:
        placeholders = ",".join("?" * len(item_ids))
        bids_all = conn.execute(
            f"""SELECT b.item_id, d.name AS bidder, b.unit_price, b.ext_price
                FROM bids b JOIN bidders d ON d.id=b.bidder_id
                WHERE b.item_id IN ({placeholders})""",
            item_ids
        ).fetchall()
    conn.close()

    # Organise bids by item_id
    bids_by_item = {}
    for b in bids_all:
        bids_by_item.setdefault(b["item_id"], []).append(dict(b))

    result = dict(rfq)
    result["items"] = []
    for item in items:
        d = dict(item)
        d["bids"] = bids_by_item.get(item["id"], [])
        result["items"].append(d)
    return result


# ---------------------------------------------------------------------------
# Bidder operations
# ---------------------------------------------------------------------------

def get_or_create_bidder(name, conn):
    row = conn.execute("SELECT id FROM bidders WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO bidders (name) VALUES (?)", (name,))
    return cur.lastrowid


def get_all_bidders(db_path=None):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT name FROM bidders ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


# ---------------------------------------------------------------------------
# Bulk load (called after parsing)
# ---------------------------------------------------------------------------

def load_parsed_rfq(rfq_id, creator, station, project, rfq_date, source_file, sheet_name,
                    parsed_data, is_potential=False, notes=None, db_path=None):
    """
    Insert an RFQ with all its items and bids in a single transaction.
    If the RFQ already exists it is deleted first (reload behaviour).
    """
    conn = get_conn(db_path)
    try:
        # Remove existing data for this RFQ
        conn.execute("DELETE FROM rfqs WHERE rfq_id=?", (rfq_id,))

        # Insert RFQ header
        conn.execute(
            """INSERT INTO rfqs (rfq_id, creator, station, project, rfq_date, source_file,
                                 sheet_name, is_potential, notes)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (rfq_id, creator, station, project, rfq_date, source_file, sheet_name,
             1 if is_potential else 0, notes)
        )

        for item in parsed_data.get("items", []):
            cur = conn.execute(
                """INSERT INTO rfq_items (rfq_id, item_number, item_type, specification,
                                          size, unit, quantity)
                   VALUES (?,?,?,?,?,?,?)""",
                (rfq_id, item["item_number"], item["item_type"], item["specification"],
                 item.get("size"), item.get("unit"), item.get("quantity"))
            )
            item_id = cur.lastrowid

            for bidder_name, price_data in item.get("bids", {}).items():
                up = price_data.get("unit_price")
                ep = price_data.get("ext_price")
                if up is None and ep is None:
                    continue          # bidder did not quote this item
                bidder_id = get_or_create_bidder(bidder_name, conn)
                conn.execute(
                    """INSERT INTO bids (rfq_id, item_id, bidder_id, unit_price, ext_price)
                       VALUES (?,?,?,?,?)""",
                    (rfq_id, item_id, bidder_id, up, ep)
                )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Analytics helpers (used by the AI query layer)
# ---------------------------------------------------------------------------

def get_schema_summary(db_path=None):
    """Return a textual summary of the database schema for use in AI prompts."""
    return """
SQLite database: rfq_database.db

Tables:
  rfqs(rfq_id TEXT PK, creator TEXT, station TEXT, rfq_date TEXT,
       source_file TEXT, sheet_name TEXT, is_potential INT, notes TEXT, loaded_at TEXT)

  rfq_items(id INT PK, rfq_id TEXT FK, item_number TEXT, item_type TEXT,
            specification TEXT, size TEXT, unit TEXT, quantity REAL)

  bidders(id INT PK, name TEXT UNIQUE)

  bids(id INT PK, rfq_id TEXT FK, item_id INT FK, bidder_id INT FK,
       unit_price REAL, ext_price REAL)

Useful joins:
  rfq_items JOIN bids ON bids.item_id = rfq_items.id
  bids JOIN bidders ON bidders.id = bids.bidder_id

item_type values: PIPE, ELL, TEE, GASKET, VALVE, FLANGE, BOLT, NUT, etc.
"""


def run_query(sql, db_path=None):
    """Execute an arbitrary SELECT and return list-of-dicts."""
    conn = get_conn(db_path)
    try:
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_context_for_ai(db_path=None):
    """Return a compact JSON-friendly context block for AI queries."""
    conn = get_conn(db_path)
    rfqs    = [dict(r) for r in conn.execute("SELECT rfq_id, station, rfq_date, is_potential FROM rfqs").fetchall()]
    bidders = [r[0] for r in conn.execute("SELECT name FROM bidders ORDER BY name").fetchall()]
    types   = [r[0] for r in conn.execute(
        "SELECT DISTINCT item_type FROM rfq_items WHERE item_type IS NOT NULL AND item_type!='' ORDER BY item_type"
    ).fetchall()]
    conn.close()
    return {"rfqs": rfqs, "bidders": bidders, "item_types": types}
