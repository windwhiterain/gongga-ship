#!/usr/bin/env python3
"""Tests for the version 8 -> 9 migrator.

Version 9 puts a misplaced head back into slots 1 through 5 and moves the
transcript past them. `handoff` wrote a copied transcript from seq 1, so a
mentor's own head was stamped at 2 and later, where the reader -- which looks at
slot 1 -- never found it.

The test builds a database with a transcript at 1, a head behind it, and every
stored seq reference the migrator checks; then it runs the migrator and checks the
head moved, the transcript kept its order, a session that carries a checkpoint was
left alone, and a second run changes nothing.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATOR = HERE / "8-to-9.py"

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
    segment_after_seq INTEGER NOT NULL
);
CREATE TABLE live_compact_artifacts (
    session_id INTEGER NOT NULL,
    message_seq INTEGER NOT NULL
);
""".strip()


def head(kind, payload):
    """A head row's payload: stored, never sent, so `content` is NULL."""
    return None, json.dumps(payload)


class Migrator(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = Path(self.tmp.name) / "gongga.db"
        self.conn = sqlite3.connect(self.db)
        self.conn.executescript(SCHEMA)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def session(self, name, monitor=0, origin=None):
        cur = self.conn.execute(
            "INSERT INTO sessions (name, monitor_seq, live_compact_origin_seq) VALUES (?, ?, ?)",
            (name, monitor, origin),
        )
        return cur.lastrowid

    def add(self, sid, seq, kind, content, display):
        self.conn.execute(
            "INSERT INTO messages (session_id, seq, kind, content, display, call_id, turn, step,"
            " created_at) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0)",
            (sid, seq, kind, content, display),
        )

    def transcript(self, sid, start):
        """Three ordinary rows a request would actually send."""
        for offset, text in enumerate(["first", "second", "third"]):
            self.add(
                sid,
                start + offset,
                "text",
                json.dumps({"role": "user", "content": text}),
                json.dumps({"role": "user", "text": text}),
            )

    def misplaced_head(self, sid, at):
        """A head stamped after a copied transcript, which is what handoff left."""
        content, display = head("instructions", {"instructions": ""})
        self.add(sid, at, "instructions", content, display)
        content, display = head("agents_md", {"agents_md": "the rules"})
        self.add(sid, at + 1, "agents_md", content, display)
        content, display = head("index", {"index": "the index", "rooted": []})
        self.add(sid, at + 2, "index", content, display)
        content, display = head("info", {"tail": "the card", "cwd": "/here"})
        self.add(sid, at + 3, "info", content, display)

    def run_migrator(self):
        done = subprocess.run(
            [sys.executable, str(MIGRATOR), "--home", self.tmp.name, "--db", str(self.db)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    def rows(self, sid, kind=None):
        sql = "SELECT seq, kind, content, display FROM messages WHERE session_id = ?"
        args = [sid]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY seq"
        return self.conn.execute(sql, args).fetchall()

    def test_a_head_behind_a_transcript_moves_into_its_slots(self):
        sid = self.session("main/mentor", monitor=1)
        self.transcript(sid, 1)          # a copied transcript: seq 1, 2, 3
        self.misplaced_head(sid, 4)      # its own head stamped after it
        self.conn.commit()

        self.run_migrator()

        instructions = self.rows(sid, "instructions")
        self.assertEqual(instructions[0][0], 1, "the head takes slot 1")
        self.assertIsNone(instructions[0][2], "a head row is still not sent")
        for kind, slot in (("agents_md", 3), ("index", 4), ("info", 5)):
            found = self.rows(sid, kind)
            self.assertEqual(found[0][0], slot, f"{kind} takes slot {slot}")
        # The transcript follows, in the order it had.
        texts = [json.loads(display)["text"] for seq, kind, content, display in self.rows(sid) if content]
        self.assertEqual(texts, ["first", "second", "third"])
        first_text = next(seq for seq, kind, content, display in self.rows(sid) if content)
        self.assertEqual(first_text, 6, "the transcript starts after the head slots")

    def test_a_session_with_a_checkpoint_is_left_alone(self):
        sid = self.session("main/live", monitor=5, origin=4)
        self.conn.execute(
            "INSERT INTO live_compact_checkpoints (session_id, segment_after_seq) VALUES (?, 4)",
            (sid,),
        )
        self.conn.execute(
            "INSERT INTO live_compact_artifacts (session_id, message_seq) VALUES (?, 3)", (sid,)
        )
        self.transcript(sid, 1)
        self.misplaced_head(sid, 4)
        self.conn.commit()
        before = self.rows(sid)

        self.run_migrator()

        self.assertEqual(self.rows(sid), before, "a renumber would move a stored reference")

    def test_the_step_is_a_no_op_the_second_time(self):
        sid = self.session("main/mentor")
        self.transcript(sid, 1)
        self.misplaced_head(sid, 4)
        self.conn.commit()
        self.run_migrator()
        settled = self.rows(sid)

        self.run_migrator()

        self.assertEqual(self.rows(sid), settled)

    def test_a_session_already_in_slots_is_untouched(self):
        sid = self.session("main/ordinary")
        # A head in its own slots, with the tool set where it belongs, and the
        # transcript after them: nothing here needs a step.
        for kind, payload in (
            ("instructions", {"instructions": ""}),
            ("tools", {"tools": []}),
            ("agents_md", {"agents_md": "the rules"}),
            ("index", {"index": "the index", "rooted": []}),
            ("info", {"tail": "the card", "cwd": "/here"}),
        ):
            slot = {
                "instructions": 1,
                "tools": 2,
                "agents_md": 3,
                "index": 4,
                "info": 5,
            }[kind]
            content, display = head(kind, payload)
            self.add(sid, slot, kind, content, display)
        self.transcript(sid, 6)
        self.conn.commit()
        before = self.rows(sid)

        self.run_migrator()

        self.assertEqual(self.rows(sid), before)


if __name__ == "__main__":
    unittest.main()