#!/usr/bin/env python3
"""Take a gongga home from version 2 to version 3.

Version 2 called the machine a session's shells run on a "target", and spelled the
setting `set_session --target`. Version 3 calls it a remote and spells it
`--remote`, because `local` is not one of the names: a session on this machine
names none at all. The word changed everywhere the operator reads it, so the column
that records it changes with them rather than leaving one row of jargon behind.

The server runs it at startup, before it opens the database:

    python 2-to-3.py --home ~/.gongga --db ~/.gongga/gongga.db

It changes the database and nothing else -- the version file belongs to the server,
which writes it only once a migrator has exited zero. Running it twice has to be a
no-op, because a step that failed once is run again.

The config file is not rewritten here. `config.toml` is the operator's own, and it
is read by a loader that already accepts the old table name: `[targets.<name>]` is
where `[remotes.<name>]` lived, and the next edit of that file can say so.
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def columns(conn, table):
    """The columns `table` has."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def tables(conn):
    """The tables this database has."""
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def rename_the_column(conn, done):
    """`sessions.target` becomes `sessions.remote`, values and all."""
    if "sessions" not in tables(conn):
        print("2-to-3: no sessions table; nothing to migrate", file=sys.stderr)
        return
    have = columns(conn, "sessions")
    if "remote" in have:
        return
    if "target" not in have:
        # A home that never had the concept: the schema creates it on open.
        print("2-to-3: sessions has no target column; leaving it alone", file=sys.stderr)
        return
    named = conn.execute("SELECT COUNT(*) FROM sessions WHERE target IS NOT NULL").fetchone()[0]
    conn.execute("ALTER TABLE sessions RENAME COLUMN target TO remote")
    done.append(f"sessions.target is sessions.remote ({named} session(s) keep their machine)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    conn = sqlite3.connect(str(Path(args.db)))
    try:
        done = []
        rename_the_column(conn, done)
        conn.commit()
    finally:
        conn.close()

    print("2-to-3: " + (", ".join(done) if done else "already version 3"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
