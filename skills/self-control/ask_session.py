#!/usr/bin/env python3
"""Ask another session a question and wait for its answer.

    ask_session <session> "<question>"

The question goes to that session the way `send_message` does, and the answer
comes back from the place that keeps it: the session's own transcript, where
the turn that answered it ends. This command reads that transcript instead of
asking the caller to poll.

Why a command at all: asking is two steps with a name to remember in between,
and a name remembered wrong is a question sent to the wrong session -- or, with
the positional form of `send_message`, the session name read as the question.
The session is a positional argument here, and the question is what follows it.

This file is also the package's internal module: every top-level name carries a
leading `_`, so `gongga_skill`'s discovery publishes nothing from it. The
package's public `ask` is this file's `_ask` re-published by `__init__.py`;
running this file directly keeps the `ask_session` command working.

Nothing is invented: the answer is the other session's own words.

Exit: 0 the answer was received, 2 the question could not be asked or no
answer arrived.
"""

import argparse
import json
import os
import subprocess
import sys
import time

# `gongga-cli` first: the bundle `gongga` carries a window, and a window
# refuses client-side commands. A script wants the one that answers.
CANDIDATES = ("gongga-cli", "gongga")
# How long to wait for the answer. A turn is a model's round trip, so this is
# measured in minutes; a question unanswered by then is reported as
# unanswered rather than waited on forever.
TIMEOUT_SECONDS = 900
# How long between reads of the transcript.
POLL_SECONDS = 1.0


class _Refused(Exception):
    """A refusal whose message is the user's: the form that caught it prints it.

    The script form writes it to stderr and leaves with 2; the package form
    re-raises it as RuntimeError, which the package runner prints the same way.
    """


def _die(message):
    raise _Refused(message)


def _which(name):
    """`shutil.which` without the import: this file must run on a bare Python."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        for path in (candidate, candidate + ".exe"):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


def _command_layer():
    """The program that talks to this server, by name or by the path the server gave."""
    named = os.environ.get("GONGGA_CLI")
    if named and os.path.isfile(named):
        return named
    for candidate in CANDIDATES:
        found = _which(candidate)
        if found:
            return found
    _die(
        "no command layer found: this needs `gongga-cli` on PATH, or $GONGGA_CLI "
        "pointing at it (a shell the server started has both)"
    )


def _run(cli, args):
    """Runs the command layer and returns its events."""
    try:
        done = subprocess.run([cli] + args, capture_output=True, text=True)
    except OSError as e:
        _die(f"cannot run {cli}: {e}")
    events = []
    for line in done.stdout.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            events.append(row)
    if done.returncode != 0:
        detail = (done.stderr or "").strip() or (done.stdout or "").strip()
        _die(f"{' '.join(args)} failed: {detail[:400]}")
    return events


def _outcome(events):
    """The one line the command layer ends with: ok or not, and what it said."""
    for row in reversed(events):
        if row.get("t") == "cmd_result":
            return row.get("ok"), row.get("message", "")
    return None, ""


def _read(cli, session, since):
    """The transcript of `session` after `since`, and where it ends."""
    events = _run(cli, ["get_messages", f"{since + 1}:", "--session", session])
    ok, message = _outcome(events)
    if not ok:
        _die(message or f"cannot read the transcript of {session}")
    last = since
    status = "idle"
    for row in events:
        if row.get("t") == "cmd_result":
            data = row.get("data")
            if isinstance(data, dict):
                if isinstance(data.get("last_seq"), int):
                    last = data["last_seq"]
                if isinstance(data.get("status"), str):
                    status = data["status"]
    return events, last, status


def _deliver(cli, session, question):
    """Puts the question into the session's hands."""
    events = _run(cli, ["send_message", "--name", session, "--message", question])
    ok, message = _outcome(events)
    if not ok:
        _die(message or f"the question did not reach {session}")


def _answer_in(events):
    """What the session said since the question, as its own words."""
    said = []
    for row in events:
        if row.get("t") != "response":
            continue
        if row.get("part") != "output":
            continue
        text = row.get("text")
        if isinstance(text, str) and text.strip():
            said.append(text.strip())
    return said


def _wait_for_answer(cli, session, since, timeout):
    """The session's turn ends, and what it said is in the transcript."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        events, last, status = _read(cli, session, since)
        said = _answer_in(events)
        if said and status != "running":
            return said[-1]
        time.sleep(POLL_SECONDS)
    return None


def _ask(session, question, timeout=TIMEOUT_SECONDS):
    """The question delivered and the answer waited for; the answer, or a refusal.

    The cursor is taken before the question is sent, so the answer must be a
    turn that ends after the question, not one that ended before it.
    """
    session = (session or "").strip()
    question = (question or "").strip()
    if not session:
        _die("name the session to ask")
    if not question:
        _die("ask something: the question is the rest of the command")
    cli = _command_layer()
    _, since, _ = _read(cli, session, 0)
    _deliver(cli, session, question)
    answer = _wait_for_answer(cli, session, since, timeout)
    if answer is None:
        _die(f"{session} did not answer within {timeout:g}s")
    return answer


def _main():
    parser = argparse.ArgumentParser(
        prog="ask_session",
        description="Ask another session a question and wait for its answer.",
    )
    parser.add_argument("session", help="the session to ask, e.g. shell/mentor")
    parser.add_argument("question", help="what to ask it")
    parser.add_argument(
        "--timeout",
        type=float,
        default=TIMEOUT_SECONDS,
        help="seconds to wait for the answer",
    )
    args = parser.parse_args()
    try:
        print(_ask(args.session, args.question, args.timeout))
    except _Refused as refused:
        print(f"ask_session: {refused}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
