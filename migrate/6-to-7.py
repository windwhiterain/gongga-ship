#!/usr/bin/env python3
"""Take a gongga home from version 6 to version 7.

Version 6 kept the whole prompt in one `system` row at seq 1 and the tool set at
seq 2, with ordinary messages from seq 3. Version 7 keeps five head rows -- the
instructions at 1, the tool set at 2, agents.md at 3, the index at 4 and the rest
at 5 -- with ordinary messages from seq 6. The five rows are what the reader sees
as five lines and what the sender rebuilds the wire head from.

The server runs this at startup, before it opens the database:

    python 6-to-7.py --home ~/.gongga --db ~/.gongga/gongga.db

For each session whose head is still the single version-6 row, that row is split
into the four text rows (the tool set at 2 is left alone), every ordinary message
at seq >= 3 moves to seq >= 6, and every stored seq that names such a message is
moved with it: the session's monitor and compact origins, the compact checkpoints'
five seq columns, and the compact artifacts' message_seq. Rows keep their id,
payload, turn and step; only their seq moves.

Nothing else changes: a version-7 session (instructions at 1, no system row) is
left alone, so running the step twice is a no-op. The BEFORE UPDATE trigger that
forbids rewriting a context row is dropped here and left for the server to
recreate when it opens the database; the migrator runs before that open.
"""

import argparse
import json
import sqlite3
import sys
from pathlib import Path

HEAD_INSTRUCTIONS_SEQ = 1
HEAD_TOOLS_SEQ = 2
HEAD_AGENTS_SEQ = 3
HEAD_INDEX_SEQ = 4
HEAD_INFO_SEQ = 5
SHIFT = 3  # ordinary messages move from seq >= 3 to seq >= 6


def tables(conn):
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def session_ids(conn):
    return [row[0] for row in conn.execute("SELECT DISTINCT session_id FROM messages")]


def head_row(conn, session_id):
    """The version-6 system row that still needs splitting, or None."""
    row = conn.execute(
        "SELECT id, display FROM messages WHERE session_id = ? AND seq = 1 AND kind = 'system'",
        (session_id,),
    ).fetchone()
    return row


def split_payload(display):
    """The four head-row payloads from a version-6 Prompt JSON, or None."""
    try:
        prompt = json.loads(display)
    except (TypeError, ValueError):
        return None
    if not isinstance(prompt, dict):
        return None
    pick = lambda key: prompt.get(key)
    instructions = {"instructions": pick("instructions")}
    agents = {"agents_md": pick("agents_md")}
    index = {
        "index": pick("index"),
        "rooted": pick("rooted"),
        "index_explicit": pick("index_explicit"),
        "index_cached": pick("index_cached"),
    }
    info = {
        "tail": pick("tail"),
        "parameters": pick("parameters"),
        "cwd": pick("cwd"),
        "model": pick("model"),
        "remote": pick("remote"),
    }
    return [("instructions", instructions), ("agents_md", agents), ("index", index), ("info", info)]


def migrate_session(conn, session_id, now):
    """Split one session's head and shift its ordinary rows. False if nothing to do."""
    head = head_row(conn, session_id)
    if head is None:
        return False
    parts = split_payload(head[1])
    if parts is None:
        return False

    # Every row that keeps its identity but moves: the ordinary messages at
    # seq >= 3. Park them at negative seqs first so no row transiently occupies
    # a head slot that another write is about to fill.
    movers = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM messages WHERE session_id = ? AND seq >= 3", (session_id,)
        )
    ]
    for row_id in movers:
        conn.execute("UPDATE messages SET seq = -seq WHERE id = ? AND seq > 0", (row_id,))

    # The old system row becomes the instructions row in place (its id survives).
    (kind, payload), agents, index, info = parts[0], parts[1], parts[2], parts[3]
    conn.execute(
        "UPDATE messages SET kind = 'instructions', display = ?, content = NULL WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False), head[0]),
    )
    # Three new head rows at 3, 4, 5. `content` is NULL: a record, not a message
    # the transcript walks -- the same as the row they were split from.
    for seq, (kind, payload) in [
        (HEAD_AGENTS_SEQ, agents),
        (HEAD_INDEX_SEQ, index),
        (HEAD_INFO_SEQ, info),
    ]:
        conn.execute(
            "INSERT INTO messages (session_id, seq, kind, content, display, call_id, turn, step, created_at) "
            "VALUES (?, ?, ?, NULL, ?, NULL, NULL, NULL, ?)",
            (session_id, seq, kind, json.dumps(payload, ensure_ascii=False), now),
        )

    # Restore the parked messages at seq + SHIFT.
    for row_id in sorted(movers):
        conn.execute("UPDATE messages SET seq = -seq + ? WHERE id = ? AND seq < 0", (SHIFT, row_id))

    # Stored seqs that name ordinary messages move with them. Values at 0/NULL
    # mean "none", and 1/2 name the head, so only >= 3 is shifted.
    conn.execute(
        "UPDATE sessions SET monitor_seq = monitor_seq + ? "
        "WHERE id = ? AND monitor_seq >= 3",
        (SHIFT, session_id),
    )
    conn.execute(
        "UPDATE sessions SET live_compact_origin_seq = live_compact_origin_seq + ? "
        "WHERE id = ? AND live_compact_origin_seq >= 3",
        (SHIFT, session_id),
    )
    if "live_compact_checkpoints" in tables(conn):
        for column in ("segment_after_seq", "request_seq", "round_start_seq", "cutoff_seq", "result_seq"):
            conn.execute(
                f"UPDATE live_compact_checkpoints SET {column} = {column} + ? "
                f"WHERE session_id = ? AND {column} >= 3",
                (SHIFT, session_id),
            )
    if "live_compact_artifacts" in tables(conn):
        # `(session_id, message_seq)` is the primary key, so a plain `+ ?` meets a
        # row that has not moved yet: 449 + 3 lands on a 452 that still exists.
        # Park them at negative seqs first, the same way the messages above are
        # parked, then shift from there.
        conn.execute(
            "UPDATE live_compact_artifacts SET message_seq = -message_seq "
            "WHERE session_id = ? AND message_seq >= 3",
            (session_id,),
        )
        conn.execute(
            "UPDATE live_compact_artifacts SET message_seq = -message_seq + ? "
            "WHERE session_id = ? AND message_seq < 0",
            (SHIFT, session_id),
        )
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    conn = sqlite3.connect(str(Path(args.db)))
    moved = 0
    try:
        if "messages" not in tables(conn):
            print("6-to-7: no messages table; nothing to migrate")
            return 0
        now = conn.execute("SELECT COALESCE(MAX(created_at), 0) FROM messages").fetchone()[0]
        # The append-only guard would reject every seq rewrite. Drop it here;
        # the server recreates it with CREATE TRIGGER IF NOT EXISTS on open.
        conn.execute("DROP TRIGGER IF EXISTS context_no_update")
        for sid in session_ids(conn):
            if migrate_session(conn, sid, now):
                moved += 1
        conn.commit()
    finally:
        conn.close()

    print(f"6-to-7: split the head of {moved} session(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
