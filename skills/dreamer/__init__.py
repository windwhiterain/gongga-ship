"""Spawn a disposable dreamer, or mute a heartbeat that is not needed.

A keepalive notice is a question, not an order: this skill is one of its answers.
`dream()` clones this session into a child, mutes this session's heartbeat, and
hands the clone its own notice. `mute_keep_alive()` only mutes -- for a session
that has a long, quiet stretch of work ahead and does not want another notice.

Both run from this session's shell. The caller is `$GONGGA_SESSION`, and the
commands go through `GONGGA_CLI`, the command layer the server exports. Nothing
here writes the database: a skill that did would put a row between a call and
its result, which is a transcript no provider accepts.
"""

import json as _json
import os as _os
import subprocess as _subprocess
import sys as _sys
import time as _time
import typing as _typing

COMMAND_VARIABLE = "GONGGA_CLI"

DREAM_NOTICE = (
    "(dream notice: you are a dream, a disposable copy of the session that spawned you. "
    "Your only work is to organize the workspace's documents: consolidate, deduplicate "
    "and retire stale notes under .agents/notes/ and docs/notes/. Write no code, write no "
    "tests, and make no git writes. The files you leave are the memory; when they are "
    "in order, delete this dreamer session with del_session and end the turn.)"
)


def _caller():
    name = (_os.environ.get("GONGGA_SESSION") or "").strip()
    if not name:
        raise RuntimeError(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )
    return name


def _command_layer():
    cli = (_os.environ.get(COMMAND_VARIABLE) or "").strip()
    if not cli:
        raise RuntimeError(
            f"{COMMAND_VARIABLE} is not set: only a shell the server started has it "
            f'(check with: echo "${COMMAND_VARIABLE}")'
        )
    return cli


def _command_words(cli, payload):
    # On Windows `GONGGA_CLI` names a `#!` script that only a shell can start.
    if _os.name == "nt" and cli.lower().endswith((".py", ".pyw")):
        return [_sys.executable, cli, payload]
    return [cli, payload]


def _call(payload):
    cli = _command_layer()
    words = _command_words(cli, _json.dumps(payload))
    done = _subprocess.run(words, capture_output=True, text=True)
    for line in (done.stdout or "").splitlines():
        try:
            row = _json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and "cmd_result" in row:
            answer = row["cmd_result"]
            if isinstance(answer, dict) and answer.get("ok"):
                return answer
            raise RuntimeError((answer or {}).get("message") or f"command refused: {payload}")
    detail = (done.stderr or "").strip() or f"command exited {done.returncode}"
    raise RuntimeError(f"{detail}: {payload}")


def mute_keep_alive() -> str:
    """Mute this session's keepalive until a message arrives."""
    _call({"name": "mute_keepalive", "args": {"mute": True}})
    return f"keepalive muted for {_caller()}; a message to this session clears it"


def dream() -> str:
    """Clone this session into a dreamer, mute here, and start the dreamer's turn."""
    session = _caller()
    name = f"{session}/dream/{int(_time.time() * 1000)}"
    copied = _call(
        {
            "name": "clone_session",
            "args": {"id": session, "new_id": name},
        }
    )
    _call({"name": "mute_keepalive", "args": {"mute": True}})
    sent = _call(
        {
            "name": "send_message",
            "args": {"session": name, "message": DREAM_NOTICE},
        }
    )
    return _json.dumps(
        {
            "session": session,
            "dreamer": name,
            "rows": copied.get("rows"),
            "notice": sent.get("message"),
            "you": (
                f"{name} is tidying the documents with this session's prefix. "
                "This session's heartbeat is muted until the next message; keep working."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )
