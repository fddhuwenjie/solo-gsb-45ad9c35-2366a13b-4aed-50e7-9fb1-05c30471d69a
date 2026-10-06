"""SQLite 连接与初始化。"""
import os
import sqlite3

from flask import g

DB_PATH = os.environ.get("KILN_DB_PATH", "/data/kiln.db")
SCHEMA_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "schema.sql")

# 固定窑位：窑编号 -> 窑位编号集合
SEED_KILNS = [
    ("K1", "1 号窑", list(range(1, 9))),
    ("K2", "2 号窑", list(range(1, 9))),
]


def get_db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        g.db = conn
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """建表并写入固定窑位（已存在则跳过）。"""
    conn = sqlite3.connect(DB_PATH)
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        for code, name, positions in SEED_KILNS:
            conn.execute(
                "INSERT OR IGNORE INTO kilns(code, name) VALUES (?, ?)", (code, name)
            )
            kiln_id = conn.execute(
                "SELECT id FROM kilns WHERE code = ?", (code,)
            ).fetchone()[0]
            for pos_no in positions:
                conn.execute(
                    "INSERT OR IGNORE INTO kiln_positions(kiln_id, pos_no) VALUES (?, ?)",
                    (kiln_id, pos_no),
                )
        conn.commit()
    finally:
        conn.close()
