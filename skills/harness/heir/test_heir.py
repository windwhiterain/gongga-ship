#!/usr/bin/env python3
"""Tests for `heir` in `transcript.py`, against a throwaway database.

The point of these is the destructive path: nothing may be deleted before the
copy in the mentor has been checked, and an emptied session must still be able
to run. So the checks that matter are the refusal and the rollback, not the
happy path.

    python3 test_heir.py

The schema here is the minimum `transcript.py` touches, written out on purpose:
if the server adds a column the script needs, the script fails first and this
fixture tells you which columns were assumed.
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "transcript")

SCHEMA = """
create table sessions (
    id integer primary key,
    name text not null unique,
    status text not null default 'idle',
    model text not null,
    cwd text not null,
    created_at integer not null,
    updated_at integer not null,
    handover text,
    handover_upto integer not null default 0,
    llm text,
    context_limit integer,
    thinking_effort integer,
    context_tokens integer not null default 0
);
create table messages (
    id integer primary key,
    session_id integer not null,
    seq integer not null,
    kind text not null,
    content text not null,
    created_at integer not null
);
"""


class Home:
    """A home directory whose database the script under test can be pointed at."""

    def __init__(self, tmp):
        self.path = tmp
        self.db = os.path.join(tmp, "gongga.db")
        sqlite3.connect(self.db).close()
        self.c = sqlite3.connect(self.db)
        self.c.executescript(SCHEMA)
        self.c.row_factory = sqlite3.Row

    def session(self, name, model="p/m1", cwd="C:/work", tokens=777000):
        self.c.execute(
            "insert into sessions (name, model, cwd, created_at, updated_at, llm, context_tokens)"
            " values (?, ?, ?, 1, 1, ?, ?)",
            (name, model, cwd, model, tokens),
        )
        self.c.commit()
        return self.c.execute("select id from sessions where name=?", (name,)).fetchone()["id"]

    def add(self, sid, kind, payload, seq=None):
        if seq is None:
            seq = self.c.execute(
                "select coalesce(max(seq),0)+1 from messages where session_id=?", (sid,)
            ).fetchone()[0]
        self.c.execute(
            "insert into messages (session_id, seq, kind, content, created_at) values (?, ?, ?, ?, 1)",
            (sid, seq, kind, json.dumps(payload, ensure_ascii=False)),
        )
        self.c.commit()
        return seq

    def rows(self, sid):
        return self.c.execute(
            "select seq, kind, content from messages where session_id=? order by seq", (sid,)
        ).fetchall()

    def close(self):
        self.c.close()


def run(home, *args):
    env = dict(os.environ, GONGGA_HOME=home.path)
    python = sys.executable
    return subprocess.run(
        [python, os.path.join(HERE, "transcript.py"), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def transcript(*payloads):
    """The shapes the server writes: text, a call, its result, and text again."""
    return list(payloads)


def test_guards():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "hi"})

        p = run(h, "heir", "--session", "main", "--mentor", "m", "--note", "   ")
        assert p.returncode != 0 and "empty handover" in p.stderr, p.stderr
        assert len(h.rows(sid)) == 1, "a refused heir must not delete"

        p = run(h, "heir", "--session", "main", "--mentor", "main", "--note", "note")
        assert p.returncode != 0 and "another session" in p.stderr, p.stderr

        p = run(h, "heir", "--session", "unknown", "--mentor", "m", "--note", "note")
        assert p.returncode != 0 and "no session named" in p.stderr, p.stderr

        empty = h.session("empty")
        p = run(h, "heir", "--session", "empty", "--mentor", "m", "--note", "note")
        assert p.returncode != 0 and "no transcript" in p.stderr, p.stderr
        h.close()


def test_heir_moves_every_row_then_empties():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        source = transcript(
            ("text", {"role": "user", "text": "改服务端"}),
            ("tool_call", {"call_id": "c1", "name": "bash", "arguments": "{\"command\":\"cargo test\"}"}),
            ("tool_result", {"call_id": "c1", "output": "407 passed", "images": []}),
            ("text", {"role": "assistant", "text": "绿了"}),
        )
        for kind, payload in source:
            h.add(sid, kind, payload)
        h.c.execute("update sessions set handover='旧笔记', handover_upto=3 where id=?", (sid,))
        h.c.commit()

        note = "用户要 A；B 失败因为 C；现在在 D"
        p = run(h, "heir", "--session", "main", "--mentor", "mentor-main", "--note", note)
        assert p.returncode == 0, p.stderr
        report = json.loads(p.stdout)
        assert report["mentor_created"] is True and report["rows_copied"] == 4, report

        mid = h.c.execute("select id from sessions where name='mentor-main'").fetchone()["id"]
        mentor = h.rows(mid)
        assert len(mentor) == 5, [dict(r) for r in mentor]
        assert "handed its transcript over" in json.loads(mentor[0]["content"])["text"]
        assert [(r["kind"], r["content"]) for r in mentor[1:]] == [
            (k, json.dumps(v, ensure_ascii=False)) for k, v in source
        ], "every row must arrive byte for byte"

        mine = h.rows(sid)
        assert len(mine) == 1 and mine[0]["seq"] == 1, [dict(r) for r in mine]
        guide = json.loads(mine[0]["content"])["text"]
        assert "mentor-main" in guide and "get_messages" in guide and "send_message" in guide

        s = h.c.execute("select handover, handover_upto, context_tokens from sessions where id=?", (sid,)).fetchone()
        assert s["handover"] == note and s["handover_upto"] == 0 and s["context_tokens"] == 0, dict(s)
        m = h.c.execute("select model, cwd from sessions where id=?", (mid,)).fetchone()
        assert (m["model"], m["cwd"]) == ("p/m1", "C:/work"), dict(m)

        # A second handover into the same mentor appends; the archive is a session.
        keep = h.add(mid, "text", {"role": "user", "text": "mentor 自己的旧行"}, seq=900)
        h.add(sid, "text", {"role": "user", "text": "第二轮"})
        before = len(h.rows(mid))
        p = run(h, "heir", "--session", "main", "--mentor", "mentor-main", "--note", "第二份")
        assert p.returncode == 0, p.stderr
        second = json.loads(p.stdout)
        assert second["rows_copied"] == 2 and second["mentor_created"] is False, second
        assert len(h.rows(mid)) == before + 1 + 2, (before, len(h.rows(mid)))
        again = h.c.execute("select content from messages where session_id=? and seq=900", (mid,)).fetchone()
        assert "mentor 自己的旧行" in again["content"], "appending must not rewrite what is there"
        assert keep == 900
        h.close()


def test_a_bad_copy_rolls_everything_back():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "如果被篡改就别删"})
        h.add(sid, "text", {"role": "assistant", "text": "这行不该消失"})
        run(h, "heir", "--session", "main", "--mentor", "m1", "--note", "第一轮")
        mid = h.c.execute("select id from sessions where name='m1'").fetchone()["id"]
        sid = h.c.execute("select id from sessions where name='main'").fetchone()["id"]
        h.add(sid, "text", {"role": "user", "text": "第二轮"})

        # Whatever tampers with the copy, the delete must not follow it.
        h.c.execute(
            f"""create trigger tamper after insert on messages
                when new.session_id = {int(mid)} and new.kind = 'text'
                begin
                  update messages set content = '{{"role":"user","text":"TAMPERED"}}' where id = new.id;
                end"""
        )
        h.c.commit()
        mentor_before = len(h.rows(mid))
        mine_before = len(h.rows(sid))
        p = run(h, "heir", "--session", "main", "--mentor", "m1", "--note", "第三份")
        assert p.returncode != 0 and "the copy differs" in p.stderr, p.stderr
        assert len(h.rows(sid)) == mine_before, "the learner kept every row"
        assert len(h.rows(mid)) == mentor_before, "the mentor gained nothing"
        s = h.c.execute("select handover from sessions where id=?", (sid,)).fetchone()
        assert s["handover"] == "第一轮", s["handover"]
        h.close()


def test_read_and_collapse_still_work():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "第一句"})
        h.add(sid, "tool_call", {"call_id": "c", "name": "bash", "arguments": "{}"})
        h.add(sid, "tool_result", {"call_id": "c", "output": "ok", "images": []})
        h.add(sid, "text", {"role": "assistant", "text": "第二句"})

        out = json.loads(run(h, "read", "--session", "main").stdout)
        assert out["live_messages"] == 2 and out["shown_messages"] == 2, out
        assert out["handover"] is None and out["handover_upto"] == 0

        # A cut through seq 2 would leave seq 3 -- a tool result -- without its call,
        # and the refusal has to name a boundary that works instead.
        p = run(h, "collapse", "--session", "main", "--upto", "2", "--note", "笔记")
        assert p.returncode != 0 and "tool result" in p.stderr, p.stderr
        assert "through 1" in p.stderr, p.stderr

        p = run(h, "collapse", "--session", "main", "--upto", "1", "--note", "笔记")
        assert p.returncode == 0, p.stderr
        out = json.loads(run(h, "read", "--session", "main").stdout)
        assert out["handover"] == "笔记" and out["live_messages"] == 1, out
        h.close()


def main():
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
