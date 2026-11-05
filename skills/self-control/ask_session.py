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


def die(message):
    print(f"ask_session: {message}", file=sys.stderr)
    raise SystemExit(2)


def which(name):
    """`shutil.which` without the import: this file must run on a bare Python."""
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, name)
        for path in (candidate, candidate + ".exe"):
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None


def command_layer():
    """The program that talks to this server, by name or by the path the server gave."""
    named = os.environ.get("GONGGA_CLI")
    if named and os.path.isfile(named):
        return named
    for candidate in CANDIDATES:
        found = which(candidate)
        if found:
            return found
    die(
        "no command layer found: this needs `gongga-cli` on PATH, or $GONGGA_CLI "
        "pointing at it (a shell the server started has both)"
    )


def run(cli, args):
    """Runs the command layer and returns its events."""
    try:
        done = subprocess.run([cli] + args, capture_output=True, text=True)
    except OSError as e:
        die(f"cannot run {cli}: {e}")
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
        die(f"{' '.join(args)} failed: {detail[:400]}")
    return events


def outcome(events):
    """The one line the command layer ends with: ok or not, and what it said."""
    for row in reversed(events):
        if row.get("t") == "cmd_result":
            return row.get("ok"), row.get("message", "")
    return None, ""


def read(cli, session, since):
    """The transcript of `session` after `since`, and where it ends."""
    events = run(cli, ["get_messages", f"{since + 1}:", "--session", session])
    ok, message = outcome(events)
    if not ok:
        die(message or f"cannot read the transcript of {session}")
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


def ask(cli, session, question):
    """Puts the question into the session's hands."""
    events = run(cli, ["send_message", "--name", session, "--message", question])
    ok, message = outcome(events)
    if not ok:
        die(message or f"the question did not reach {session}")


def answer_in(events):
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


def wait_for_answer(cli, session, since, timeout):
    """The session's turn ends, and what it said is in the transcript."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        events, last, status = read(cli, session, since)
        said = answer_in(events)
        if said and status != "running":
            return said[-1]
        time.sleep(POLL_SECONDS)
    return None


def main():
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

    session = args.session.strip()
    question = args.question.strip()
    if not session:
        die("name the session to ask")
    if not question:
        die("ask something: the question is the rest of the command")

    cli = command_layer()
    # The cursor first: the answer must be a turn that ends after the question
    # was asked, not one that ended before it.
    _, since, _ = read(cli, session, 0)

    ask(cli, session, question)
    answer = wait_for_answer(cli, session, since, args.timeout)
    if answer is None:
        die(f"{session} did not answer within {args.timeout:g}s")
    print(answer)
    return 0


if __name__ == "__main__":
    sys.exit(main())
