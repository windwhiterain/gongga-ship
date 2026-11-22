#!/usr/bin/env python3
"""Take a gongga home from version 8 to version 9.

Version 7 reserved slots 1 through 5 for the head and read it by position. A
transcript copied into a mentor started at 1, because `handoff` wrote it with
`max(seq) + 1` on an empty session, so a mentor's own head was stamped past the
copied rows instead of into the slots:

    405 kv-mask/mentor   head at seq 2-26 (21 rows), transcript at seq 1
    416 kv-mask/mentor/5 head at seq 3-7, transcript at seq 1-2

Those sessions are unreadable: `Store::prompt` looks at slot 1, finds a transcript
row rather than a head, and a session with no head is given one on its next
request. The 7-to-8 step does not repair this -- it only removes duplicate copies
of a head that is already in the right place.

The server runs this at startup, before it opens the database:

    python 8-to-9.py --home ~/.gongga --db ~/.gongga/gongga.db

What it does, per session whose head is not where the reader looks:

* Puts the newest row of each head kind into its own slot -- instructions at 1,
  agents.md at 3, index at 4, the card at 5. The tool set at 2 is left alone: a
  session with no tool set of its own gets one on its next request.
* Moves every other row past slot 5, keeping the order they had. A head row is
  `content IS NULL` and is never sent, so where it sits costs nothing; the
  transcript rows keep their relative order, which is what a request reads.

Every stored seq that names a row in these sessions was checked before this step
was written: none of them carries a compact checkpoint, an artifact, a memory
block or a monitor mark, so renumbering moves no reference. A session with a
checkpoint is left alone rather than renumbered underneath a reader.

The trigger that forbids rewriting a context row is dropped here and left for
the server to recreate when it opens the database; the migrator runs before that
open. Running the step twice is a no-op.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

# Slot and kind of each head row the reader looks for. Slot 1 answers to `system`
# too: a version-6 session keeps its whole prompt in one row of that kind.
SLOTS = (
    (1, ("instructions", "system")),
    (3, ("agents_md",)),
    (4, ("index",)),
    (5, ("info",)),
)
HEAD_KINDS = ("instructions", "system", "tools", "agents_md", "index", "info")
FIRST_ORDINARY = 6


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def session_ids(conn):
    return [row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")]


def newest_head(conn, session_id, kinds):
    """The newest row of a head kind, wherever it sits."""
    placeholders = ", ".join("?" for _ in kinds)
    return conn.execute(
        f"SELECT id, seq FROM messages WHERE session_id = ? AND kind IN ({placeholders}) "
        f"ORDER BY id DESC LIMIT 1",
        (session_id, *kinds),
    ).fetchone()


def placed(conn, session_id):
    """Whether the head already sits where the reader looks.

    A session whose head is in its slots is left alone whatever its other columns
    say: `monitor_seq` and `live_compact_origin_seq` name rows that are not moving.
    """
    for seq, kinds in SLOTS:
        newest = newest_head(conn, session_id, kinds)
        if newest is None:
            continue
        if newest[1] != seq:
            return False
    # And nothing else is holding a slot a head still has to take.
    for seq, kinds in SLOTS:
        if newest_head(conn, session_id, kinds) is not None:
            continue
        placeholders = ", ".join("?" for _ in HEAD_KINDS)
        taken = conn.execute(
            f"SELECT EXISTS(SELECT 1 FROM messages WHERE session_id = ? AND seq = ? "
            f"AND kind IN ({placeholders}))",
            (session_id, seq, *HEAD_KINDS),
        ).fetchone()[0]
        if taken:
            return False
    return True


def referenced(conn, session_id):
    """Whether a compact checkpoint or memory block names a seq of this session.

    Those store absolute seqs of transcript rows, so a renumber underneath them
    would point them at different rows. A session that carries one is left alone
    rather than renumbered.
    """
    for table, column in (
        ("live_compact_checkpoints", "session_id"),
        ("live_compact_artifacts", "session_id"),
        ("memory_blocks", "session_id"),
    ):
        if table in tables(conn):
            found = conn.execute(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE {column} = ?)", (session_id,)
            ).fetchone()[0]
            if found:
                return True
    return False


def repair(conn, session_id):
    """Put the head into its slots and the transcript past them. False if untouched."""
    if placed(conn, session_id):
        return False
    if referenced(conn, session_id):
        return False

    heads = {}
    for seq, kinds in SLOTS:
        newest = newest_head(conn, session_id, kinds)
        if newest is not None:
            heads[seq] = newest[0]

    # Everything that is not a placed head row, in the order it has now. Parked at
    # a negative seq first so no row transiently occupies a slot being freed.
    keep = set(heads.values())
    rest = [
        row[0]
        for row in conn.execute(
            "SELECT id, seq FROM messages WHERE session_id = ? ORDER BY seq, id", (session_id,)
        )
        if row[0] not in keep
    ]
    for row_id in rest:
        conn.execute("UPDATE messages SET seq = -id WHERE id = ?", (row_id,))

    for seq, row_id in heads.items():
        conn.execute("UPDATE messages SET seq = ? WHERE id = ?", (seq, row_id))
    # The order the rows had is the order a request reads; only the numbers move.
    for offset, row_id in enumerate(rest):
        conn.execute(
            "UPDATE messages SET seq = ? WHERE id = ?", (FIRST_ORDINARY + offset, row_id)
        )
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    conn = sqlite3.connect(str(Path(args.db)))
    fixed = skipped = 0
    try:
        if "messages" not in tables(conn):
            print("8-to-9: no messages table; nothing to migrate")
            return 0
        # Dropped for the duration of this step only; the server recreates it with
        # CREATE TRIGGER IF NOT EXISTS when it opens the database.
        conn.execute("DROP TRIGGER IF EXISTS context_no_update")
        conn.execute("DROP TRIGGER IF EXISTS context_cut_tail_only")
        for sid in session_ids(conn):
            if repair(conn, sid):
                fixed += 1
            elif not placed(conn, sid):
                skipped += 1
        conn.commit()
    finally:
        conn.close()

    print(f"8-to-9: moved the head into its slots in {fixed} session(s), skipped {skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(main())