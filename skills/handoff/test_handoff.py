#!/usr/bin/env python3
"""Tests for the `handoff` package, against a throwaway database.

The point of these is the destructive path: nothing may be deleted before the
copy in the mentor has been checked, and an emptied session must still be able
to run. So the checks that matter are the refusal and the rollback, not the
happy path.

    python3 test_handoff.py

The standalone schema is the minimum the package touches. The release gate
passes `--schema-source crates/server/src/store.rs`, so the same checks also run
against the server's current schema instead of a fixture that can drift.
"""

import gc
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
#: The package contract: a skill is asked one JSON request on its stdin.
CONTRACT = os.path.join(HERE, "..", "..", "python", "gongga_skill.py")

SCHEMA = """
create table sessions (
    id integer primary key,
    name text not null unique,
    status text not null default 'idle',
    model text not null,
    cwd text not null,
    created_at integer not null,
    updated_at integer not null,
    model_name text,
    context_limit integer,
    thinking_effort integer,
    context_tokens integer not null default 0
);
create table messages (
    id integer primary key,
    session_id integer not null,
    seq integer not null,
    kind text not null,
    content text,
    display text not null,
    call_id text,
    created_at integer not null
);
"""


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
        raise ValueError(f"unterminated schema constant in {path}")
    return source[start:end]


def wire_item(kind, payload):
    """The distinct model and reader shapes written by the current server."""
    if kind == "text":
        role = payload.get("role") or "user"
        text = payload.get("text") or ""
        if role == "assistant":
            return {"role": role, "content": [{"type": "output_text", "text": text}]}
        return {"role": role, "content": text}
    if kind == "tool_call":
        return {"type": "function_call", **payload}
    if kind == "tool_result":
        return {
            "type": "function_call_output",
            "call_id": payload["call_id"],
            "output": payload["output"],
        }
    return payload


def command_stub(tmp):
    """A stand-in for the harness's command, recording what it is asked to send.

    The script hands the handoff note to `send_message` rather than writing a row
    itself, so what is checked here is the argument it hands over. The stub is a
    shell script on POSIX and a `.cmd` on Windows: an interpreter is what a test
    has, and a real `gongga-cli` is an executable the fixture does not have.
    """
    log = os.path.join(tmp, "sent.jsonl")
    stub = os.path.join(tmp, "stub_command.py")
    with open(stub, "w", encoding="utf-8") as f:
        f.write(
            "import json\n"
            "import sys\n"
            f"with open({log!r}, 'a', encoding='utf-8') as out:\n"
            "    out.write(json.dumps(sys.argv[1:], ensure_ascii=False) + '\\n')\n"
            "print(json.dumps({'t': 'cmd_result', 'id': 1, 'ok': True, "
            "'message': 'message queued for the next step boundary'}))\n"
        )
    if os.name == "nt":
        path = os.path.join(tmp, "gongga-cli.cmd")
        with open(path, "w", encoding="utf-8", newline="\r\n") as f:
            f.write(f'@"{sys.executable}" "{stub}" %*\n')
    else:
        path = os.path.join(tmp, "gongga-cli")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n')
        os.chmod(path, 0o755)
    return path, log


class Home:
    """A home directory whose database the script under test can be pointed at."""

    def __init__(self, tmp):
        self.path = tmp
        self.db = os.path.join(tmp, "gongga.db")
        self.cli, self.log = command_stub(tmp)
        sqlite3.connect(self.db).close()
        self.c = sqlite3.connect(self.db)
        self.c.executescript(SCHEMA)
        self.c.row_factory = sqlite3.Row

    def sent(self):
        """Every command the script handed to the command layer, oldest first."""
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def session(self, name, model="p/m1", cwd="C:/work", tokens=777000):
        self.c.execute(
            "insert into sessions (name, model, cwd, created_at, updated_at, context_tokens)"
            " values (?, ?, ?, 1, 1, ?)",
            (name, model, cwd, tokens),
        )
        self.c.commit()
        return self.c.execute("select id from sessions where name=?", (name,)).fetchone()["id"]

    def add(self, sid, kind, payload, seq=None):
        if seq is None:
            seq = self.c.execute(
                "select coalesce(max(seq),0)+1 from messages where session_id=?", (sid,)
            ).fetchone()[0]
        content = json.dumps(wire_item(kind, payload), ensure_ascii=False)
        display = json.dumps(payload, ensure_ascii=False)
        call_id = payload.get("call_id") if kind in ("tool_call", "tool_result") else None
        self.c.execute(
            """insert into messages
                (session_id, seq, kind, content, display, call_id, created_at)
                values (?, ?, ?, ?, ?, ?, 1)""",
            (sid, seq, kind, content, display, call_id),
        )
        self.c.commit()
        return seq

    def rows(self, sid):
        return self.c.execute(
            """select seq, kind, content, display, call_id from messages
                where session_id=? order by seq""",
            (sid,),
        ).fetchall()

    def close(self):
        self.c.close()


def run(home, function, cli=True, session="main", **args):
    """Runs the package as the shell of `session` would.

    The caller's name is not an argument any more: it is what the server exports to
    the shell it started, so a test says which shell by setting the same variable --
    and `session=None` is a shell that lost it, which must be refused.

    A package skill is asked one request on stdin (`python/gongga_skill.py`), so the
    call's arguments are the function's own keyword arguments: nothing here is
    spelled as a command line.
    """
    env = dict(os.environ, GONGGA_HOME=home.path)
    # Unset on purpose, to reach the failure path a shell that lost it would.
    env.pop("GONGGA_CLI", None)
    env.pop("GONGGA_SESSION", None)
    if session is not None:
        env["GONGGA_SESSION"] = session
    if cli:
        env["GONGGA_CLI"] = home.cli
    request = json.dumps({"skill": HERE, "function": function, "args": args})
    return subprocess.run(
        [sys.executable, CONTRACT],
        input=request,
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

        p = run(h, "handoff", mentor="main/m", note="   ")
        assert p.returncode != 0 and "empty note" in p.stderr, p.stderr
        assert len(h.rows(sid)) == 1, "a refused handoff must not delete"

        # The caller is the shell's session and nothing else: a shell that lost the
        # variable cannot say whose transcript it is, so it is refused rather than
        # guessed at.
        p = run(h, "handoff", note="note", session=None)
        assert p.returncode != 0 and "GONGGA_SESSION" in p.stderr, p.stderr
        assert len(h.rows(sid)) == 1, "a refused handoff must not delete"

        # An archive is a child of the session that made it: a name outside that
        # subtree is refused, which is what keeps a handover from being aimed at
        # somebody else's transcript -- `@monitor` included.
        for outside in ("md", "main2/m", "@monitor", "@monitor/mentor", "main"):
            p = run(h, "handoff", mentor=outside, note="note")
            assert p.returncode != 0 and "is not under" in p.stderr, (outside, p.stderr)
            assert len(h.rows(sid)) == 1, f"{outside} must not delete anything"

        p = run(h, "handoff", mentor="unknown/m", note="note", session="unknown")
        assert p.returncode != 0 and "no session named" in p.stderr, p.stderr

        empty = h.session("empty")
        p = run(h, "handoff", mentor="empty/m", note="note", session="empty")
        assert p.returncode != 0 and "no transcript" in p.stderr, p.stderr
        h.close()


def test_handoff_moves_every_row_then_empties():
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
        note = "用户要 A；B 失败因为 C；现在在 D"
        p = run(h, "handoff", mentor="main/mentor-main", note=note)
        assert p.returncode == 0, p.stderr
        report = json.loads(p.stdout)
        assert report["mentor_created"] is True and report["rows_copied"] == 4, report

        mid = h.c.execute("select id from sessions where name='main/mentor-main'").fetchone()["id"]
        mentor = h.rows(mid)
        assert len(mentor) == 5, [dict(r) for r in mentor]
        assert "handed off" in json.loads(mentor[0]["display"])["text"]
        assert [(r["kind"], r["content"], r["display"], r["call_id"]) for r in mentor[1:]] == [
            (
                k,
                json.dumps(wire_item(k, v), ensure_ascii=False),
                json.dumps(v, ensure_ascii=False),
                v.get("call_id") if k in ("tool_call", "tool_result") else None,
            )
            for k, v in source
        ], "every row must arrive with its wire item, reader payload and call id"

        # Nothing is left but the row the server appends when this command
        # returns: no row of the past, and no note stored beside the transcript.
        mine = h.rows(sid)
        assert mine == [], [dict(r) for r in mine]

        # The note went out as the message an operator would send, to this
        # session, and the measurement of a transcript that no longer exists is
        # zeroed so the next step does not remind about it.
        assert [json.loads(e[0]) for e in h.sent()] == [{"name": "send_message", "args": {"session": "main", "message": note}}], h.sent()
        s = h.c.execute("select context_tokens from sessions where id=?", (sid,)).fetchone()
        assert s["context_tokens"] == 0, dict(s)
        h.add(sid, "text", {"role": "user", "text": "醒来第一句"})
        assert [json.loads(r["display"])["text"] for r in h.rows(sid)] == ["醒来第一句"], "seq restarts at 1"
        m = h.c.execute("select model, cwd from sessions where id=?", (mid,)).fetchone()
        assert (m["model"], m["cwd"]) == ("p/m1", "C:/work"), dict(m)

        # A second handover asks for the same mentor and gets a new name under it:
        # one archive per handover, because the continuation reads its mentor from row one
        # and two generations in one transcript cannot be told apart.
        keep = h.add(mid, "text", {"role": "user", "text": "mentor 自己的旧行"}, seq=900)
        h.add(sid, "text", {"role": "user", "text": "第二轮"})
        before = len(h.rows(mid))
        p = run(h, "handoff", mentor="main/mentor-main", note="第二份")
        assert p.returncode == 0, p.stderr
        second = json.loads(p.stdout)
        # Two rows this time: what the emptied session woke up to, and what came
        # after it. No call was in flight, so the archive takes both and the
        # session starts from seq 1 again.
        assert second["rows_copied"] == 2 and second["kept_call"] is None, second
        assert second["mentor"] == "main/mentor-main/2", second
        assert second["mentor_created"] is True, second
        assert [json.loads(e[0])["name"] for e in h.sent()] == ["send_message"] * 2, h.sent()
        assert json.loads(h.sent()[-1][0])["args"]["message"] == "第二份", h.sent()
        # The first archive is exactly as it was left: not one row gained, and its
        # own row is still there.
        assert len(h.rows(mid)) == before, (before, len(h.rows(mid)))
        again = h.c.execute("select content from messages where session_id=? and seq=900", (mid,)).fetchone()
        assert "mentor 自己的旧行" in again["content"], "the old archive must not be rewritten"
        assert keep == 900
        # And the second archive holds the second handover, names in the report.
        second_mid = h.c.execute("select id from sessions where name='main/mentor-main/2'").fetchone()["id"]
        assert len(h.rows(second_mid)) == 1 + 2, len(h.rows(second_mid))
        h.close()




def test_handoff_keeps_the_call_in_flight():
    """The command that runs the handover is still open, so its pair survives."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "清空你自己"})
        h.add(sid, "tool_call", {"call_id": "c-live", "name": "bash", "arguments": "{\"command\":\"transcript handoff\"}"})

        p = run(h, "handoff", mentor="main/archive", note="要做 X；Y 失败因为 Z")
        assert p.returncode == 0, p.stderr
        report = json.loads(p.stdout)
        assert report["kept_call"] == "c-live", report
        assert report["rows_copied"] == 2 and report["deleted"] == 1, report
        assert report["through_seq"] == 1 and report["mentor_rows"] == 4, report
        assert "archive" in report["you"] and "get_messages" in report["you"], report
        assert "continue the current work" in report["you"], report
        assert "no clean state is required" in report["you"], report

        # The session keeps exactly the call whose result the server has yet to write.
        # A cut only takes the tail, so the call is cut with the history and appended
        # back at the head: it is the only row left, at seq 1.
        mine = h.rows(sid)
        assert [r["kind"] for r in mine] == ["tool_call"], [dict(r) for r in mine]
        assert mine[0]["seq"] == 1, [dict(r) for r in mine]
        assert [json.loads(e[0]) for e in h.sent()] == [{"name": "send_message", "args": {"session": "main", "message": "要做 X；Y 失败因为 Z"}}], h.sent()

        # The archive holds the whole exchange, and the copied call is answered
        # there: a mentor reads its own transcript back as a valid request.
        mid = h.c.execute("select id from sessions where name='main/archive'").fetchone()["id"]
        mentor = h.rows(mid)
        assert [r["kind"] for r in mentor] == ["text", "text", "tool_call", "tool_result"], [dict(r) for r in mentor]
        closing = json.loads(mentor[-1]["content"])
        assert closing["call_id"] == "c-live", closing
        assert "mentor of `main`" in closing["output"], closing

        # The result the server appends when the command returns closes the pair.
        h.add(sid, "tool_result", {"call_id": "c-live", "output": p.stdout, "images": []})
        left = h.rows(sid)
        assert [r["kind"] for r in left] == ["tool_call", "tool_result"], [dict(r) for r in left]
        assert [r["seq"] for r in left] == [1, 2], [dict(r) for r in left]
        h.close()

def test_a_bad_copy_rolls_everything_back():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "如果被篡改就别删"})
        h.add(sid, "text", {"role": "assistant", "text": "这行不该消失"})
        run(h, "handoff", mentor="main/m1", note="第一轮")
        mid = h.c.execute("select id from sessions where name='main/m1'").fetchone()["id"]
        # The next handover asks for the same mentor and is given `m1/2`: the
        # tampering below has to follow the name the script will really use.
        target = h.c.execute(
            """insert into sessions (name, status, model_name, model, cwd, created_at, updated_at)
                values ('main/m1/2', 'idle', 'm1', 'p/m1', 'C:/work', 0, 0)"""
        ).lastrowid
        h.c.commit()
        mid = target
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
        p = run(h, "handoff", mentor="main/m1", note="第三份")
        assert p.returncode != 0 and "the copy differs" in p.stderr, p.stderr
        assert len(h.rows(sid)) == mine_before, "the learner kept every row"
        assert len(h.rows(mid)) == mentor_before, "the mentor gained nothing"
        # Only the first, successful handover reached the command layer.
        assert [json.loads(e[0]) for e in h.sent()] == [{"name": "send_message", "args": {"session": "main", "message": "第一轮"}}], h.sent()
        h.close()


def test_read_shows_every_row_that_is_actually_sent():
    """Nothing is hidden behind a note: what is sent is what is in the table."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "第一句"})
        h.add(sid, "tool_call", {"call_id": "c", "name": "bash", "arguments": "{}"})
        h.add(sid, "tool_result", {"call_id": "c", "output": "ok", "images": []})
        h.add(sid, "text", {"role": "assistant", "text": "第二句"})

        out = json.loads(run(h, "read").stdout)
        assert out["live_messages"] == 2 and out["shown_messages"] == 2, out
        assert [message["text"] for message in out["messages"]] == ["第一句", "第二句"], out
        assert out["last_seq"] == 4, out

        # A note is a message like any other, so reading has nothing to add to it.
        h.add(sid, "text", {"role": "user", "text": "第一课"})
        out = json.loads(run(h, "read").stdout)
        assert [message["text"] for message in out["messages"]][-1] == "第一课", out
        assert "handover" not in out, out
        h.close()


def test_a_note_that_cannot_be_sent_is_printed():
    """A shell without the command layer empties the session and says so.

    The note is the continuation's only copy at that point, so losing it silently would be
    the one unrecoverable failure this command has.
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        h = Home(tmp)
        sid = h.session("main")
        h.add(sid, "text", {"role": "user", "text": "清空你自己"})

        note = "用户要 A；下一步读 docs/notes/x.md"
        p = run(h, "handoff", mentor="main/m", note=note, cli=False)
        assert p.returncode != 0, p.stdout
        assert "GONGGA_CLI" in p.stderr, p.stderr
        assert note in p.stderr, "the note has to survive in the result"
        # Emptied before the send, as it must be: the mentor holds the archive.
        assert h.rows(sid) == [], [dict(r) for r in h.rows(sid)]
        h.close()


def test_shipped_prompt_organizes_before_handoff():
    # Two levels: this skill sits at `skills/<name>` since the buckets went flat.
    config = open(os.path.join(HERE, "../../config/config.toml"), encoding="utf-8").read()
    assert "Hand off soon" in config
    assert "First put the handoff materials in order" in config
    assert "Failing tests and unfinished work are valid state" in config
    assert "Start training your heir now" not in config


def main():
    global SCHEMA
    arguments = sys.argv[1:]
    if arguments:
        if len(arguments) != 2 or arguments[0] != "--schema-source":
            print("usage: test_handoff.py [--schema-source crates/server/src/store.rs]", file=sys.stderr)
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
