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
LAYER = "gongga_cli.py"

DREAM_NOTICE = (
    "(dream notice: you are a dream, a disposable copy of the session that spawned you. "
    "Your only work is to organize the workspace's documents: consolidate, deduplicate "
    "and retire stale notes under .agents/notes/ and docs/notes/. Write no code, write no "
    "tests, and make no git writes. The files you leave are the memory. Do not delete "
    "this session: the server discards a dreamer when its turn truly ends. End the turn.)"
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


def _posix_script(path):
    """Whether `path` is a `#!` script: `CreateProcess` cannot start one on Windows."""
    try:
        with open(path, "rb") as program:
            return program.read(2) == b"#!"
    except OSError:
        return False


def _layer_beside_contract():
    """The command layer beside the contract that started this skill process."""
    running = _os.path.abspath(_sys.argv[0]) if _sys.argv and _sys.argv[0] else ""
    return _os.path.join(_os.path.dirname(running), LAYER) if running else ""


def _command_words(cli, command):
    """The argv that runs one JSON command without letting a shell parse it."""
    if _os.name != "nt" or not _posix_script(cli):
        return [cli, command]
    layer = _layer_beside_contract()
    if layer and _os.path.isfile(layer):
        return [_sys.executable, layer, command]
    return [cli, command]


def _command_result(stdout):
    """The terminal `cmd_result`, through the envelopes a command layer prints."""
    for line in stdout.splitlines():
        try:
            envelope = _json.loads(line)
        except ValueError:
            continue
        if not isinstance(envelope, dict):
            continue
        if envelope.get("t") == "cmd_result":
            return envelope
        if envelope.get("t") not in ("event", "row"):
            continue
        carried = envelope.get("v")
        event = carried.get("event") if isinstance(carried, dict) else None
        if isinstance(event, dict) and event.get("t") == "cmd_result":
            return event
    return None


def _call(payload):
    cli = _command_layer()
    words = _command_words(cli, _json.dumps(payload))
    done = _subprocess.run(words, capture_output=True, text=True)
    outcome = _command_result(done.stdout or "")
    if done.returncode == 0 and isinstance(outcome, dict) and outcome.get("ok"):
        return outcome
    detail = (
        (outcome or {}).get("message")
        if isinstance(outcome, dict)
        else None
    ) or (done.stderr or "").strip() or f"command exited {done.returncode}"
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
            "args": {"id": session, "new_id": name, "dream_of": True},
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
