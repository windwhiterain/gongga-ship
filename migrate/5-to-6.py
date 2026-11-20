#!/usr/bin/env python3
"""Take a gongga home from version 5 to version 6.

Version 6 reserves the first two context slots for the head: the prompt at seq
1 and the tool set at seq 2, with ordinary messages from seq 3. A session built
by version 5 wrote its first message at seq 1 and stamped the head after it, so
its prompt sat at 3 (or 4, 5, 6) and its tools one slot earlier. The server used
to walk the session to find them; at version 6 it reads those slots directly,
so a version 5 session has to be reshaped once to keep its head.

The server runs this at startup, before it opens the database:

    python 5-to-6.py --home ~/.gongga --db ~/.gongga/gongga.db

For each session the head is moved to slots 1 and 2, and the rows it displaced
(the user's opening line, and any inject/tool rows before it) keep their order
and follow at seq 3 onward. Nothing after the head is touched: the head sat at
seq 6 at the latest, while monitor marks and compact checkpoints name seqs far
beyond that, so rewriting only the head region leaves every external seq
reference valid. The rows themselves -- id, payload, turn, step -- are carried
across unchanged; only their seq moves.

The BEFORE UPDATE trigger that forbids rewriting a context row is dropped for
this step and left for the server to recreate when it opens the database; the
migrator runs before that open. Running the step twice is a no-op: a session
whose head is already in slots 1 and 2 is left alone.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

HEAD_SYSTEM_SEQ = 1
HEAD_TOOLS_SEQ = 2


def tables(conn):
    """The tables this database has."""
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def session_ids(conn):
    """Every session that has messages."""
    return [row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")]


def reshaped(conn, session_id):
    """The head rows of one session in their new order, or None if already placed.

    None means no work: either the session has no head to move, or it already
    has the prompt at 1 and the tools at 2. A session missing one half of the
    head is left as it is -- inventing the other half is not a migration.
    """
    rows = conn.execute(
        "SELECT id, seq, kind FROM messages WHERE session_id = ? ORDER BY seq, id",
        (session_id,),
    ).fetchall()
    system = [r for r in rows if r[2] == "system"]
    tools = [r for r in rows if r[2] == "tools"]
    if not system or not tools:
        return None
    prompt, schema = system[0], tools[0]
    if prompt[1] == HEAD_SYSTEM_SEQ and schema[1] == HEAD_TOOLS_SEQ:
        return None

    # The head region ends at the later of the two old head rows. Only rows in
    # that region move; everything after it keeps its seq and its references.
    end = max(prompt[1], schema[1])
    region = [r for r in rows if r[1] <= end]
    # Prompt first, tools second, then everyone else in the order they had.
    ordered = [prompt, schema] + [r for r in region if r[0] not in (prompt[0], schema[0])]
    if len(ordered) != len(region):
        return None
    return [(r[0], seq) for seq, r in enumerate(ordered, start=1)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    conn = sqlite3.connect(str(Path(args.db)))
    moved = 0
    try:
        if "messages" not in tables(conn):
            print("5-to-6: no messages table; nothing to migrate")
            return 0
        # The append-only guard would reject every seq rewrite. Drop it here;
        # the server recreates it with CREATE TRIGGER IF NOT EXISTS on open.
        conn.execute("DROP TRIGGER IF EXISTS context_no_update")
        for sid in session_ids(conn):
            plan = reshaped(conn, sid)
            if plan is None:
                continue
            # Two writes per row: park at negative seqs first so a row never
            # transiently occupies another row's slot mid-swap (seq is not
            # unique, but ORDER BY during the swap must still be unambiguous).
            for row_id, _ in plan:
                conn.execute(
                    "UPDATE messages SET seq = -seq WHERE id = ? AND seq > 0",
                    (row_id,),
                )
            for row_id, new_seq in plan:
                conn.execute(
                    "UPDATE messages SET seq = ? WHERE id = ?",
                    (new_seq, row_id),
                )
            moved += 1
        conn.commit()
    finally:
        conn.close()

    print(f"5-to-6: moved the head of {moved} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
