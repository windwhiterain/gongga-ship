#!/usr/bin/env python3
"""Take a gongga home from version 7 to version 8.

Version 7 kept the head in five rows: instructions at 1, the tool set at 2,
agents.md at 3, the index at 4 and the rest at 5, and `Store::prompt` rebuilt the
wire head from them. A build up to this version answered "no head" whenever those
rows did not deserialize, which the 6-to-7 migrator caused by copying absent keys
out as an explicit `null`. The caller answers "no head" by writing one, so that
build appended a fresh head on every single request:

    225 keep_alive   137 copies of the head     396 linux   63 copies

Those rows carry no `content`, so nothing was ever sent twice: what they cost is
seq identity -- several rows share one seq, and a range read returns all of them --
and the bytes of the transcript itself.

The server runs this at startup, before it opens the database:

    python 7-to-8.py --home ~/.gongga --db ~/.gongga/gongga.db

What it does, per session: keeps the newest row of each head slot and deletes the
older copies. The newest is what the last request sent, so the head in force is
the one preserved, and its seq does not move, so every stored seq that names a row
still names it. A head that sits outside its slots -- one copied by `handoff` into
a mentor, whose transcript starts at 1 and runs on -- is left alone, because moving
it would renumber transcript this step does not own.

The triggers that forbid rewriting a context row and cutting anywhere but the tail
are dropped here and left for the server to recreate when it opens the database;
the migrator runs before that open. Running the step twice is a no-op.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Slot and kind of each head row. Slot 1 answers to `system` too: a version-6
# session keeps its whole prompt in one row of that kind.
SLOTS = (
    (1, ("instructions", "system")),
    (3, ("agents_md",)),
    (4, ("index",)),
    (5, ("info",)),
)


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def session_ids(conn):
    return [row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")]


def dedupe(conn, session_id):
    """Delete every head copy but the newest of each slot. Answers rows deleted."""
    deleted = 0
    for seq, kinds in SLOTS:
        placeholders = ", ".join("?" for _ in kinds)
        rows = conn.execute(
            f"SELECT id FROM messages WHERE session_id = ? AND seq = ? "
            f"AND kind IN ({placeholders}) ORDER BY id DESC",
            (session_id, seq, *kinds),
        ).fetchall()
        for row in rows[1:]:
            conn.execute("DELETE FROM messages WHERE id = ?", (row[0],))
            deleted += 1
    return deleted


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    conn = sqlite3.connect(str(Path(args.db)))
    deleted = 0
    try:
        if "messages" not in tables(conn):
            print("7-to-8: no messages table; nothing to migrate")
            return 0
        # Dropped for the duration of this step only; the server recreates both with
        # CREATE TRIGGER IF NOT EXISTS when it opens the database.
        conn.execute("DROP TRIGGER IF EXISTS context_no_update")
        conn.execute("DROP TRIGGER IF EXISTS context_cut_tail_only")
        for sid in session_ids(conn):
            deleted += dedupe(conn, sid)
        conn.commit()
    finally:
        conn.close()

    print(f"7-to-8: dropped {deleted} duplicated head row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())