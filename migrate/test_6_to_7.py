#!/usr/bin/env python3
"""Tests for the version 6 -> 7 migrator.

Version 7 splits the one-row prompt head into five rows and moves ordinary
messages from seq >= 3 to seq >= 6. The test builds a version 6 database with a
real head, ordinary rows, and every stored seq reference, runs the migrator, and
checks that the head is split, the messages and their references moved together,
and a second run changes nothing.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATOR = HERE / "6-to-7.py"

# The parts of a version 6 database the migrator touches.
SCHEMA = """
CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    monitor_seq INTEGER NOT NULL DEFAULT 0,
    live_compact_origin_seq INTEGER
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    content TEXT,
    display TEXT NOT NULL,
    call_id TEXT,
    turn INTEGER,
    step INTEGER,
    created_at INTEGER NOT NULL
);
CREATE TABLE live_compact_checkpoints (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL,
    segment_after_seq INTEGER NOT NULL,
    request_seq INTEGER NOT NULL,
    round_start_seq INTEGER NOT NULL,
    cutoff_seq INTEGER NOT NULL,
    result_seq INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE TABLE live_compact_artifacts (
    session_id INTEGER NOT NULL,
    message_seq INTEGER NOT NULL,
    checkpoint_id INTEGER NOT NULL,
    PRIMARY KEY (session_id, message_seq)
);
CREATE TRIGGER context_no_update BEFORE UPDATE ON messages
BEGIN
    SELECT RAISE(ABORT, 'append only');
END;
"""

PROMPT = {
    "instructions": "the system prose",
    "index": "the expanded index",
    "agents_md": "the standing rules",
    "tail": "the card",
    "parameters": "shared args",
    "rooted": ["@file(a.md)"],
    "index_explicit": ["@file(a.md)"],
    "index_cached": ["@skill(b)"],
    "cwd": "/here",
    "model": "m",
    "remote": None,
}


class SixToSeven(unittest.TestCase):
    def build(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = Path(tmp.name)
        db = home / "gongga.db"
        conn = sqlite3.connect(db)
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT INTO sessions (id, name, monitor_seq, live_compact_origin_seq) "
            "VALUES (1, 's', 4, 3)"
        )
        # The version 6 head: prompt at 1, tools at 2, messages from 3.
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, kind, content, display, created_at) "
            "VALUES (1, 1, 1, 'system', NULL, ?, 100)",
            (json.dumps(PROMPT),),
        )
        conn.execute(
            "INSERT INTO messages (id, session_id, seq, kind, content, display, created_at) "
            "VALUES (2, 1, 2, 'tools', NULL, '{\"tools\":[]}', 100)"
        )
        for seq in (3, 4, 5, 6):
            conn.execute(
                "INSERT INTO messages (id, session_id, seq, kind, content, display, turn, step, created_at) "
                "VALUES (?, 1, ?, 'text', '{}', ?, 1, 1, 100)",
                (seq + 10, seq, json.dumps({"role": "user", "text": f"m{seq}"})),
            )
        conn.execute(
            "INSERT INTO live_compact_checkpoints "
            "(id, session_id, segment_after_seq, request_seq, round_start_seq, cutoff_seq, result_seq, created_at) "
            "VALUES (1, 1, 3, 4, 5, 5, 6, 100)"
        )
        conn.execute(
            "INSERT INTO live_compact_artifacts (session_id, message_seq, checkpoint_id) "
            "VALUES (1, 5, 1)"
        )
        # Three apart, so a single `message_seq + 3` would land the first on the
        # second: `(session_id, message_seq)` is a primary key, and a real home has
        # hundreds of these at neighbours' distance.
        conn.execute(
            "INSERT INTO live_compact_artifacts (session_id, message_seq, checkpoint_id) "
            "VALUES (1, 8, 1)"
        )
        conn.commit()
        conn.close()
        return home, db

    def run_migrator(self, home, db):
        result = subprocess.run(
            [sys.executable, str(MIGRATOR), "--home", str(home), "--db", str(db)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_head_split_and_seq_shift(self):
        home, db = self.build()
        self.run_migrator(home, db)
        conn = sqlite3.connect(db)
        try:
            kinds = dict(
                conn.execute(
                    "SELECT kind, display FROM messages WHERE session_id = 1 ORDER BY seq"
                ).fetchall()
            )
            # Five head rows at 1-5 in order, then ordinary messages at 6-9.
            rows = conn.execute(
                "SELECT seq, kind FROM messages WHERE session_id = 1 ORDER BY seq"
            ).fetchall()
            self.assertEqual(
                rows,
                [
                    (1, "instructions"),
                    (2, "tools"),
                    (3, "agents_md"),
                    (4, "index"),
                    (5, "info"),
                    (6, "text"),
                    (7, "text"),
                    (8, "text"),
                    (9, "text"),
                ],
            )
            # The prompt's fields survive the split, field for field.
            rebuilt = {}
            for kind in ("instructions", "agents_md", "index", "info"):
                rebuilt.update(json.loads(kinds[kind]))
            for key, value in PROMPT.items():
                self.assertEqual(rebuilt.get(key), value, key)
            # The rows keep their ids: the old system row became instructions.
            self.assertEqual(
                conn.execute(
                    "SELECT id FROM messages WHERE seq = 1 AND kind = 'instructions'"
                ).fetchone()[0],
                1,
            )
        finally:
            conn.close()

    def test_all_stored_seq_refs_move(self):
        home, db = self.build()
        self.run_migrator(home, db)
        conn = sqlite3.connect(db)
        try:
            self.assertEqual(
                conn.execute(
                    "SELECT monitor_seq, live_compact_origin_seq FROM sessions WHERE id = 1"
                ).fetchone(),
                (7, 6),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT segment_after_seq, request_seq, round_start_seq, cutoff_seq, result_seq "
                    "FROM live_compact_checkpoints WHERE id = 1"
                ).fetchone(),
                (6, 7, 8, 8, 9),
            )
            self.assertEqual(
                [
                    row[0]
                    for row in conn.execute(
                        "SELECT message_seq FROM live_compact_artifacts "
                        "WHERE session_id = 1 ORDER BY message_seq"
                    )
                ],
                [8, 11],
            )
        finally:
            conn.close()

    def test_idempotent(self):
        home, db = self.build()
        self.run_migrator(home, db)
        conn = sqlite3.connect(db)
        first = conn.execute(
            "SELECT id, seq, kind, display FROM messages ORDER BY id"
        ).fetchall()
        conn.close()
        self.run_migrator(home, db)
        conn = sqlite3.connect(db)
        second = conn.execute(
            "SELECT id, seq, kind, display FROM messages ORDER BY id"
        ).fetchall()
        conn.close()
        self.assertEqual(first, second, "a second run changes nothing")


if __name__ == "__main__":
    unittest.main()
