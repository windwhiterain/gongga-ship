#!/usr/bin/env python3
"""Tests for the version 0 -> 1 migrator.

The migrator is the only thing standing between a home written by an older
gongga and a server that refuses to open it, so it is tested against the shapes
a version 0 database actually has: the newest one, and one that predates every
column it adds.
"""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIGRATOR = HERE / "0-to-1.py"

# `sessions` and `detached_jobs` as version 0 wrote them.
VERSION_0 = """
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    model      TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    handover TEXT,
    handover_upto INTEGER NOT NULL DEFAULT 0,
    llm TEXT,
    context_limit INTEGER,
    thinking_effort TEXT,
    context_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE detached_jobs (
    id         INTEGER PRIMARY KEY,
    session    TEXT NOT NULL,
    shell      TEXT NOT NULL,
    job        INTEGER NOT NULL,
    pid        INTEGER NOT NULL,
    cmd        TEXT NOT NULL,
    log        TEXT NOT NULL,
    started_at INTEGER NOT NULL
);
"""

# Older still: the note was called a summary and the threshold a compact one.
# This is what a home written months ago actually holds.
ANCIENT = """
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    model      TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    summary TEXT,
    summarised_upto INTEGER NOT NULL DEFAULT 0,
    llm TEXT,
    compact_threshold INTEGER,
    context_tokens INTEGER NOT NULL DEFAULT 0,
    thinking_effort TEXT
);
"""

# Older still: no note, no tuning, no measured context.
BEFORE_VERSION_0 = """
CREATE TABLE sessions (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    status     TEXT NOT NULL,
    model      TEXT NOT NULL,
    cwd        TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
"""


def run(home, db):
    """Runs the migrator the way the server does; returns (code, output)."""
    out = subprocess.run(
        [sys.executable, str(MIGRATOR), "--home", str(home), "--db", str(db)],
        capture_output=True,
        text=True,
    )
    return out.returncode, (out.stdout + out.stderr).strip()


def columns(db, table):
    conn = sqlite3.connect(str(db))
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


class Home(unittest.TestCase):
    """A throwaway home, with the database version 0 left behind."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.home = Path(self.dir.name)
        self.db = self.home / "gongga.db"
        conn = sqlite3.connect(str(self.db))
        conn.executescript(VERSION_0)
        conn.execute(
            "INSERT INTO sessions (name, status, model, cwd, created_at, updated_at, "
            "handover, handover_upto) VALUES ('main', 'idle', 'm', 'cwd', 1, 1, 'the note', 42)"
        )
        conn.execute(
            "INSERT INTO detached_jobs (session, shell, job, pid, cmd, log, started_at) "
            "VALUES ('main', 'svc', 3, 4242, 'sleep 1', 'log', 7)"
        )
        conn.commit()
        conn.close()
        self.addCleanup(self.dir.cleanup)


class TestVersion0(Home):
    def test_the_note_survives_and_the_boundary_does_not(self):
        code, out = run(self.home, self.db)
        self.assertEqual(code, 0, out)
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute("SELECT handover FROM sessions").fetchone()
        finally:
            conn.close()
        # The note is what a session wakes up to; the seq it used to stop at is
        # not carried, because version 1 sends every row it has.
        self.assertEqual(row, ("the note",), out)
        self.assertNotIn("handover_through", columns(self.db, "sessions"))

    def test_the_old_column_is_gone(self):
        run(self.home, self.db)
        self.assertNotIn("handover_upto", columns(self.db, "sessions"))

    def test_a_detached_job_gets_the_name_it_is_matched_by(self):
        run(self.home, self.db)
        conn = sqlite3.connect(str(self.db))
        try:
            name = conn.execute("SELECT name FROM detached_jobs").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(name, "svc-3")

    def test_every_column_version_one_reads_is_there(self):
        run(self.home, self.db)
        self.assertEqual(
            columns(self.db, "sessions")
            & {
                "handover",
                "llm",
                "context_limit",
                "context_tokens",
                "thinking_effort",
            },
            {
                "handover",
                "llm",
                "context_limit",
                "context_tokens",
                "thinking_effort",
            },
        )

    def test_running_it_twice_changes_nothing(self):
        run(self.home, self.db)
        code, out = run(self.home, self.db)
        self.assertEqual(code, 0, out)
        self.assertIn("already version 1", out, "a step that failed once is run again")
        conn = sqlite3.connect(str(self.db))
        try:
            row = conn.execute("SELECT handover FROM sessions").fetchone()
        finally:
            conn.close()
        self.assertEqual(row[0], "the note")


class TestShapes(unittest.TestCase):
    """The databases a home may be holding that are older than version 0."""

    def db_with(self, script):
        dir = tempfile.TemporaryDirectory()
        self.addCleanup(dir.cleanup)
        db = Path(dir.name) / "gongga.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(script)
        conn.commit()
        conn.close()
        return Path(dir.name), db

    def test_a_note_older_than_the_column_is_carried_into_it(self):
        home, db = self.db_with(ANCIENT)
        conn = sqlite3.connect(str(db))
        conn.execute(
            "INSERT INTO sessions (name, status, model, cwd, created_at, updated_at, "
            "summary, summarised_upto, compact_threshold) "
            "VALUES ('main', 'idle', 'm', 'cwd', 1, 1, 'the old note', 42, 30)"
        )
        conn.commit()
        conn.close()

        code, out = run(home, db)
        self.assertEqual(code, 0, out)
        conn = sqlite3.connect(str(db))
        try:
            row = conn.execute("SELECT handover, context_limit FROM sessions").fetchone()
        finally:
            conn.close()
        # The note is data, not decoration: losing it sends the whole transcript
        # again. The seq it stopped at is dropped with its column.
        self.assertEqual(row, ("the old note", 30), out)
        self.assertNotIn("summarised_upto", columns(db, "sessions"))
        self.assertNotIn("summarised_upto", columns(db, "sessions"))
        self.assertNotIn("compact_threshold", columns(db, "sessions"))

    def test_a_database_without_the_note_columns_grows_them(self):
        home, db = self.db_with(BEFORE_VERSION_0)
        code, out = run(home, db)
        self.assertEqual(code, 0, out)
        self.assertIn("handover", columns(db, "sessions"))
        self.assertIn("thinking_effort", columns(db, "sessions"))

    def test_a_database_with_no_schema_at_all_is_left_alone(self):
        home, db = self.db_with("")
        code, out = run(home, db)
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to migrate", out)


if __name__ == "__main__":
    unittest.main()
