#!/usr/bin/env python3
"""Take a gongga home from version 1 to version 2.

Version 1's `messages.content` carried one payload with two jobs: the entry as
the model was told it, and the entry as the reader shows it. Version 2 splits
them. `content` becomes the item a request sends -- one standard OpenAI
Responses input item, written by the server and never reshaped on its way out --
and `display` keeps the payload as version 1 stored it, which is what the reader
has always read.

The server runs it at startup, before it opens the database:

    python 1-to-2.py --home ~/.gongga --db ~/.gongga/gongga.db

It changes the database and nothing else -- the version file belongs to the
server, which writes it only once a migrator has exited zero. Running it twice
has to be a no-op, because a step that failed once is run again.

Deriving `content` from `display` is not this script's job: the rule that says
what a row sends lives in the server, next to the code that writes rows, and a
second spelling of it here would drift. This script only clears the ground --
the new column arrives empty and the server fills it as it opens.
"""

import argparse
import sqlite3
import sys
from pathlib import Path


def tables(conn):
    """The tables this database has."""
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(conn, table):
    """The columns `table` has."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def split_the_payload(conn, done):
    """Rename what only the reader needs, and open the ground for what is sent.

    `display` inherits the NOT NULL that `content` carried, so every row keeps a
    payload; `content` arrives nullable, because a notice and the prompt in force
    are rows no request is built from.
    """
    have = columns(conn, "messages")
    if "display" in have:
        return
    if "content" not in have:
        # Neither spelling: a table this step has nothing to say about.
        print("1-to-2: messages has no content column; leaving it alone", file=sys.stderr)
        return
    conn.execute("ALTER TABLE messages RENAME COLUMN content TO display")
    conn.execute("ALTER TABLE messages ADD COLUMN content TEXT")
    rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    done.append(f"split messages.content ({rows} row(s) awaiting their item)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    # A home whose database predates every table has nothing to migrate; the
    # server creates the schema when it opens it.
    conn = sqlite3.connect(str(Path(args.db)))
    try:
        if "messages" not in tables(conn):
            print("1-to-2: no messages table; nothing to migrate")
            return 0
        done = []
        split_the_payload(conn, done)
        conn.commit()
    finally:
        conn.close()

    print("1-to-2: " + (", ".join(done) if done else "already version 2"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
