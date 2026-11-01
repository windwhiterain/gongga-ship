#!/usr/bin/env python3
"""Wait until a session's turn is over, then say how it ended.

    wait_turn <session> [--timeout S] [--interval S] [--quiet]

`gongga wait` is a primitive: it prints the next log row of a kind and exits. It
cannot know whether the turn already finished before it connected, so waiting on a
finished turn would hang. That judgement is policy, and it lives here.

How the policy works:

1. Ask the session what it is doing (`get_messages` carries `status`).
2. Already idle? Answer at once. This is the common case for a script: the turn
   was short and the process started after it ended.
3. Otherwise `wait --kind turn_end` on that session, bounded by `--interval`, and
   ask again. A `wait` that came back -- or timed out -- is followed by a fresh
   read, so a turn that ended in the gap between the two commands is caught by the
   next read rather than waited out forever.

Exit codes: 0 the session is idle (look at `outcome` for how the turn went), 1 it
stopped with an error, 124 the overall `--timeout` ran out.

The turn's own verdict is *data*, not an exit code -- the log row says `done`,
`cancelled` or `failed`, and interpreting that is the caller's business.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

# `gongga-cli` first, deliberately. It is the one-shot command client; `gongga` is
# the bundle that also carries a window, and a window refuses client commands
# ("this client has no handler for it") because it draws every session already.
# A script wants the one that answers, whichever else happens to be on PATH.
CANDIDATES = ("gongga-cli", "gongga")


def cli(*args, timeout=None):
    """Runs one command and returns its JSON events, one line each.

    A refusal is the caller's answer too: swallowing it turns a version skew or a
    wrong session name into "nothing happened", which is exactly the kind of quiet
    wrong answer this whole mechanism exists to stop.
    """
    found = next((name for name in CANDIDATES if shutil.which(name)), None)
    if found is None:
        die("neither gongga-cli nor gongga is on PATH")
    try:
        done = subprocess.run(
            [found, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return None  # the bound on `wait` ran out; the caller re-reads state
    if done.returncode != 0:
        why = (done.stderr or done.stdout).strip().splitlines()
        die(why[-1] if why else f"{found} {args[0]} left with {done.returncode}")
    return [json.loads(line) for line in done.stdout.splitlines() if line.strip()]


def die(message):
    print(f"wait_turn: {message}", file=sys.stderr)
    raise SystemExit(2)


def status_of(session):
    """The session's status as the transcript reports it.

    A session that does not exist is the CLI's refusal to report, not a `None` to
    paper over: "no session named 'alhpa'" is the answer, and inventing a wait on
    it would hang for the whole timeout on a typo.
    """
    for event in cli("get_messages", "--session", session, "--range", "-1:"):
        if event.get("t") == "cmd_result":
            return (event.get("data") or {}).get("status")
    die(f"could not read the state of {session!r}")


def turn_end(session, interval):
    """One `wait`, bounded. Returns the row if it arrived, None on the bound."""
    events = cli(
        "wait", "--kind", "turn_end", "--session", session, timeout=interval + 5
    )
    if events is None:
        return None
    for event in events:
        if event.get("t") == "cmd_result":
            return None
        if event.get("kind") == "turn_end":
            return event
    return None


def main():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("session")
    parser.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        help="give up after this many seconds (default 1800)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=15.0,
        help="bound on one wait before the state is read again (default 15)",
    )
    parser.add_argument("--quiet", action="store_true", help="print nothing")
    args = parser.parse_args()

    deadline = _now() + args.timeout
    seen = None
    while True:
        status = status_of(args.session)
        if status in ("idle", "error"):
            answer = {"session": args.session, "status": status}
            if seen is not None:
                # How the turn actually went is the row's to say, not the status's:
                # a cancelled turn is idle and a failed one says so.
                answer["turn"] = (seen.get("payload") or {}).get("outcome")
            if not args.quiet:
                print(json.dumps(answer, sort_keys=True))
            return 1 if status == "error" else 0
        if _now() >= deadline:
            print(f"wait_turn: still running after {args.timeout:.0f}s", file=sys.stderr)
            return 124
        # A bound on one wait, so a turn that ended between the read above and this
        # call is caught by the next read instead of being waited out forever.
        bound = max(1.0, min(args.interval, deadline - _now()))
        seen = turn_end(args.session, bound) or seen


def _now():
    import time

    return time.monotonic()


if __name__ == "__main__":
    raise SystemExit(main())
