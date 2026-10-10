#!/usr/bin/env python3
"""
Keep the SQLite database in the repo as a text dump (diffs well in git).

  python3 persist.py load   # data/audit.sql -> arenal_audit.db
  python3 persist.py dump   # arenal_audit.db -> data/audit.sql
"""
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
DB = HERE / "arenal_audit.db"
SQL = HERE / "data" / "audit.sql"


def load():
    if not SQL.exists():
        print("no saved data yet, starting fresh")
        return
    if DB.exists():
        DB.unlink()
    conn = sqlite3.connect(DB)
    conn.executescript(SQL.read_text(encoding="utf-8"))
    conn.close()
    print(f"loaded {SQL.stat().st_size // 1024} KB of saved data")


def dump():
    if not DB.exists():
        print("no database to save")
        return
    SQL.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB)
    SQL.write_text("\n".join(conn.iterdump()) + "\n", encoding="utf-8")
    conn.close()
    print(f"saved {SQL.stat().st_size // 1024} KB")


if __name__ == "__main__":
    {"load": load, "dump": dump}[sys.argv[1]]()
