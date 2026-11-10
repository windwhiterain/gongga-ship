#!/usr/bin/env python3
"""Tests for `new_session`'s mapping: source row + overrides -> set_session words.

The point of these is the pairing invariant -- a directory is copied only when
the target machine is the source machine -- and the copy/override table, so the
checks that matter are the word lists, not a server: `mapping()` is pure, and
`new_session(dry_run=True)` needs nothing but a throwaway database.

    python3 test_new_session.py [--schema-source crates/server/src/store.rs]

The standalone schema is the minimum `new_session.py` reads. The release gate
passes `--schema-source crates/server/src/store.rs`, so the same checks also run
against the server's current schema instead of a fixture that can drift.
"""

import gc
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import new_session as ns

SCHEMA = """
create table sessions (
    id integer primary key,
    name text not null unique,
    status text not null default 'idle',
    model text not null,
    model_name text,
    cwd text not null,
    created_at integer not null,
    updated_at integer not null,
    context_limit integer,
    thinking_effort text,
    remote text,
    cache_tier text,
    cache_keepalive integer not null default 0,
    mode text
);
"""

#: The home row: every field set, on this machine (remote NULL).
ME = {
    "name": "me",
    "model": "fake/flash",
    "thinking_effort": "low",
    "context_limit": 20,
    "cache_tier": "short",
    "cache_keepalive": 1,
    "mode": "explore",
    "remote": None,
    "cwd": "C:/work",
}

#: A row that lives on another machine.
BOXED = dict(ME, name="boxed", remote="box", cwd="C:/work")


def server_schema(path):
    """The schema constant, extracted rather than copied from the server."""
    source = open(path, encoding="utf-8").read()
    opening = 'const SCHEMA: &str = r#"'
    start = source.find(opening)
    if start < 0:
        raise ValueError(f"no schema constant in {path}")
    start += len(opening)
    end = source.find('"#;', start)
    if end < 0:
        raise ValueError(f"schema constant in {path} is not terminated")
    return source[start:end]


MAPPING_CASES = [
    # (source, overrides, expected words) -- table-driven: one row per decision.
    ("default copies every field",
     ME, {},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("NULL fields are copied by omitting the flag",
     dict(ME, thinking_effort=None, context_limit=None, cache_tier=None, mode=None),
     {},
     ["--id", "fresh", "--model", "fake/flash", "--keep_alive", "true",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("a NULL cache_keepalive column still copies as false",
     dict(ME, cache_keepalive=0),
     {},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "false", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("--remote to another machine sends no directory",
     ME, {"remote": "box"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--remote", "box", "--must_create", "true"]),
    ("--remote none to a boxed source sends neither",
     BOXED, {"remote": "none"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--must_create", "true"]),
    ("a --cwd is read on the machine it lands on",
     BOXED, {"cwd": "D:/there"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--remote", "box", "--cwd", "D:/there", "--must_create", "true"]),
    ("the same machine again copies the directory",
     BOXED, {"remote": "box"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--remote", "box", "--cwd", "C:/work", "--must_create", "true"]),
    ("--model swaps the model and copies the rest",
     ME, {"model": "other/model"},
     ["--id", "fresh", "--model", "other/model", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("--ctx_limit keeps the caller's spelling",
     ME, {"ctx_limit": "55%"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "55%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("--keep_alive false overrides a true source",
     ME, {"keep_alive": False},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "low", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "false", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
    ("--thinking default passes through and clears",
     ME, {"thinking": "default"},
     ["--id", "fresh", "--model", "fake/flash", "--thinking", "default", "--ctx_limit", "20%",
      "--cache_tier", "short", "--keep_alive", "true", "--mode", "explore",
      "--cwd", "C:/work", "--must_create", "true"]),
]


def test_mapping():
    for label, source, overrides, expected in MAPPING_CASES:
        got = ns.mapping(source, "fresh", **overrides)
        assert got == expected, f"{label}:\n  got {got}\n  want {expected}"


def test_mapping_refuses_without_a_model():
    for source in [dict(ME, model=""), dict(ME, model=None)]:
        try:
            ns.mapping(source, "fresh")
        except ValueError:
            continue
        raise AssertionError(f"a model-less source must be refused: {source}")


class Fixture:
    """A throwaway home: the sessions table and two rows, nothing else."""

    def __init__(self, schema):
        self.dir = tempfile.mkdtemp(prefix="new-session-test-")
        self.db = sqlite3.connect(os.path.join(self.dir, "gongga.db"))
        self.db.executescript(schema)
        columns = ", ".join(ns.COLUMNS)
        for row in (ME, BOXED):
            marks = ", ".join("?" for _ in ns.COLUMNS)
            # The timestamps are the schema's NOT NULL columns; their value is the
            # test's least concern, only their presence is.
            self.db.execute(
                f"insert into sessions (created_at, updated_at, {columns}) "
                f"values (0, 0, {marks})",
                tuple(row.get(column) for column in ns.COLUMNS),
            )
        self.db.commit()
        self.db.close()

    def scrub(self):
        gc.collect()
        for name in os.listdir(self.dir):
            os.remove(os.path.join(self.dir, name))
        os.rmdir(self.dir)


def test_new_session_reads_the_caller_row():
    """With no --source, $GONGGA_SESSION names the row the words copy."""
    home = Fixture(SCHEMA)
    saved = dict(os.environ)
    try:
        os.environ["GONGGA_HOME"] = home.dir
        os.environ["GONGGA_SESSION"] = "me"
        line = ns.new_session("fresh2", dry_run=True)
        assert line.startswith("set_session --id fresh2 --model fake/flash"), line
        assert "--cwd C:/work" in line, line
    finally:
        os.environ.clear()
        os.environ.update(saved)
        home.scrub()


def test_new_session_source_names_another_row():
    home = Fixture(SCHEMA)
    saved = dict(os.environ)
    try:
        os.environ["GONGGA_HOME"] = home.dir
        os.environ["GONGGA_SESSION"] = "me"
        line = ns.new_session("fresh2", source="boxed", dry_run=True)
        assert "--remote box" in line, line
        assert "--cwd C:/work" in line, line
    finally:
        os.environ.clear()
        os.environ.update(saved)
        home.scrub()


def test_new_session_without_a_source_refuses():
    home = Fixture(SCHEMA)
    saved = dict(os.environ)
    try:
        os.environ["GONGGA_HOME"] = home.dir
        os.environ.pop("GONGGA_SESSION", None)
        cases = [
            # No source named at all: the caller's own session is missing too.
            ({}, "no source session"),
            # A named source that is not a session: the row to copy does not exist.
            ({"source": "ghost"}, "no session named 'ghost'"),
        ]
        for kwargs, words in cases:
            try:
                ns.new_session("fresh2", dry_run=True, **kwargs)
            except RuntimeError as refused:
                assert words in str(refused), (words, str(refused))
                continue
            raise AssertionError(f"a source-less call must refuse, not create: {kwargs}")
    finally:
        os.environ.clear()
        os.environ.update(saved)
        home.scrub()


def test_dry_run_creates_nothing():
    home = Fixture(SCHEMA)
    saved = dict(os.environ)
    try:
        os.environ["GONGGA_HOME"] = home.dir
        os.environ["GONGGA_SESSION"] = "me"
        ns.new_session("fresh2", dry_run=True)
        check = sqlite3.connect(os.path.join(home.dir, "gongga.db"))
        rows = check.execute("select count(*) from sessions where name='fresh2'").fetchone()[0]
        check.close()
        assert rows == 0, "dry_run answered a line and touched nothing"
    finally:
        os.environ.clear()
        os.environ.update(saved)
        home.scrub()


def main():
    global SCHEMA
    arguments = sys.argv[1:]
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--schema-source":
            print("usage: test_new_session.py [--schema-source crates/server/src/store.rs]", file=sys.stderr)
            return 2
        SCHEMA = server_schema(arguments[1])
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = []
    for t in tests:
        try:
            t()
            print(f"  ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001 - a test runner, not the code under test
            print(f"  FAIL {t.__name__}: {type(e).__name__}: {e}")
            failed.append(t.__name__)
        # sqlite handles reach the connection through un-collected cursors, and
        # Windows will not delete a directory the database still has open.
        gc.collect()
    print("FAILURES:", failed or "none")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
