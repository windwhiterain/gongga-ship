#!/usr/bin/env python3
"""Take a gongga home from version 0 to version 1.

One migrator covers one step. This one is everything version 1 needs of version
0, so a home two versions behind runs this and then the one after it.

The server runs it at startup, before it opens the database:

    python 0-to-1.py --home ~/.gongga --db ~/.gongga/gongga.db

It changes the database and nothing else -- the version file belongs to the
server, which writes it only once a migrator has exited zero. Running it twice
has to be a no-op, because a step that failed once is run again.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# What version 0 called what version 1 reads, oldest spelling first. A fold is
# two pieces of data -- the note, and the seq it takes over -- and losing either
# one sends the whole transcript again and asks for work already done. A
# threshold is a percentage of the model's window in both spellings.
RENAMES = (
    ("handover_upto", "handover_through"),
    ("summarised_upto", "handover_through"),
    ("summary", "handover"),
    ("compact_threshold", "context_limit"),
)

# What a version-1 `sessions` has, and the statement that adds it. A column a
# database never had is one no `CREATE TABLE IF NOT EXISTS` will add later.
SESSION_COLUMNS = (
    ("handover", "ALTER TABLE sessions ADD COLUMN handover TEXT"),
    (
        "handover_through",
        "ALTER TABLE sessions ADD COLUMN handover_through INTEGER NOT NULL DEFAULT 0",
    ),
    ("llm", "ALTER TABLE sessions ADD COLUMN llm TEXT"),
    ("context_limit", "ALTER TABLE sessions ADD COLUMN context_limit INTEGER"),
    (
        "context_tokens",
        "ALTER TABLE sessions ADD COLUMN context_tokens INTEGER NOT NULL DEFAULT 0",
    ),
    ("thinking_effort", "ALTER TABLE sessions ADD COLUMN thinking_effort TEXT"),
)


def tables(conn):
    """The tables this database has."""
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def columns(conn, table):
    """The columns `table` has."""
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_missing(conn, table, column, ddl, done):
    """Adds `column` when it is not there; says whether it was added."""
    if column in columns(conn, table):
        return
    conn.execute(ddl)
    done.append(f"{table}.{column}")


def take_what_the_old_columns_hold(conn, done):
    """Move a fold and a threshold from the names version 0 used to version 1's.

    Only blanks are filled: a session version 1 has already folded is not moved
    back to a boundary an old column still remembers.
    """
    have = columns(conn, "sessions")
    for old, new in RENAMES:
        if old not in have or new not in have:
            continue
        # One predicate for both spellings: a seq is 0 when unset, a note and a
        # threshold are NULL, and nothing is carried out of an empty one.
        moved = conn.execute(
            f"UPDATE sessions SET {new} = {old} WHERE ({new} IS NULL OR {new} = 0) "
            f"AND {old} IS NOT NULL AND {old} <> 0 AND {old} <> ''"
        ).rowcount
        try:
            conn.execute(f"ALTER TABLE sessions DROP COLUMN {old}")
        except sqlite3.OperationalError as e:
            # An old python's sqlite cannot drop a column; a leftover one
            # misleads nobody who reads version 1's code.
            print(f"0-to-1: left {old} in place ({e})", file=sys.stderr)
            continue
        if moved:
            done.append(f"{old} -> {new} ({moved} session(s))")


def name_detached_jobs(conn, done):
    """Give every detached job the name `check_bash` and `del_bash` match on."""
    if "detached_jobs" not in tables(conn):
        return
    add_missing(
        conn,
        "detached_jobs",
        "name",
        "ALTER TABLE detached_jobs ADD COLUMN name TEXT NOT NULL DEFAULT ''",
        done,
    )
    named = conn.execute(
        "UPDATE detached_jobs SET name = shell || '-' || job WHERE name = ''"
    ).rowcount
    if named:
        done.append(f"named {named} detached job(s)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    # A home whose database predates every table has nothing to migrate; the
    # server creates the schema when it opens it.
    conn = sqlite3.connect(str(Path(args.db)))
    try:
        if "sessions" not in tables(conn):
            print("0-to-1: no sessions table; nothing to migrate")
            return 0
        done = []
        for column, ddl in SESSION_COLUMNS:
            add_missing(conn, "sessions", column, ddl, done)
        take_what_the_old_columns_hold(conn, done)
        name_detached_jobs(conn, done)
        conn.commit()
    finally:
        conn.close()

    print("0-to-1: " + (", ".join(done) if done else "already version 1"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
