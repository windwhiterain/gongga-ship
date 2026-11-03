#!/usr/bin/env python3
"""Start a subagent: a session under the caller's name, whose turns the caller hears about.

    spawn <role> [--cwd PATH] [--model M] [--context-limit N%]

SPEC's `## Skill: subagent` names four steps. Two of them are one command here,
because the second is worthless when it is a step somebody has to remember:

1. **A new session under the caller session's name.** `set_session --must_create
   true` takes the name and refuses a name that already exists, so choosing and
   claiming are one step: a probe followed by a create leaves a window in which
   another caller takes the name, and the loser silently joins the winner's
   transcript.
2. **A subscription to that session's turn end.** Armed before this command
   answers, and armed for the session that ran it -- `subscribe` notices its
   caller, never the subject of its pattern. A parent left to arm this itself
   would poll instead, which is the habit `subscribe` exists to replace.

The name is a path, `<parent>/<role>`, with `/2`, `/3`, ... added when it is taken:
two callers asking for `main/impl` get `main/impl` and `main/impl/2`, and neither
gets the other's context. The parent is `GONGGA_SESSION`, which the server exports
to every shell it starts. There is no `--parent`: a session that could name its
parent could plant work in somebody else's subtree.

The name goes to stdout on one line, so `name=$(spawn.py impl --cwd .)` is the whole
interface. What a human wants to know -- which name was skipped, which subscription
was armed -- goes to stderr.

Exit: 0 the session exists and its turn ends will be noticed, 2 nothing was created.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

# `gongga-cli` first: the bundle `gongga` carries a window, and a window refuses
# client-side commands. A script wants the one that answers.
CANDIDATES = ("gongga-cli", "gongga")
# How many names to try before giving up. Each attempt is one round trip, and a
# parent with a thousand siblings is a bug in the caller, not a case to serve.
ATTEMPTS = 1000
# What the server says when `--must_create` refuses. Its answer is the only signal
# that separates "name taken" from a real failure, so it is named once, here.
TAKEN = "--must_create refuses"


def die(message):
    print(f"spawn: {message}", file=sys.stderr)
    raise SystemExit(2)


def program():
    found = next((name for name in CANDIDATES if shutil.which(name)), None)
    if found is None:
        die("neither gongga-cli nor gongga is on PATH")
    return found


def command(args):
    """Runs one command, returning (accepted, the message it answered with).

    A server's refusal is the caller's answer too: `gongga-cli` leaves non-zero and
    prints the same sentence to stderr, so the two are one fact and are read as one.
    """
    done = subprocess.run([program(), *args], capture_output=True, text=True)
    events = []
    for line in done.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    message = ""
    for event in events:
        if event.get("t") == "cmd_result":
            message = event.get("message") or ""
    if done.returncode != 0 and not message:
        why = (done.stderr or done.stdout).strip().splitlines()
        die(f"{args[0]} left with {done.returncode}: {why[-1] if why else 'no answer'}")
    return done.returncode == 0, message


def create(args):
    """One `set_session --must_create`, returning (taken, what the server said)."""
    accepted, message = command(["set_session", *args])
    if TAKEN in message:
        return True, message
    if not accepted or not message.startswith("session is now"):
        die(f"could not create a session: {message or 'no answer from the server'}")
    return False, message


def subscribe(name):
    """Arms the caller's ears for `name`'s turn ends, returning what was armed.

    # Invariant

    The pattern is anchored at both ends. `subscribe` matches unanchored, so
    `turn_end:main/impl` would also fire for `main/impl/2`: a parent with two
    children would hear about the wrong one and wait on the right one for nothing.
    The name is a session's, not a regex's, so it is escaped before it is a pattern.
    """
    pattern = f"^turn_end:{re.escape(name)}$"
    accepted, message = command(
        ["subscribe", "--event", pattern, "--persistant", "true"]
    )
    if not accepted:
        return None, message
    return pattern, message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", help="one segment: what this subagent is for")
    parser.add_argument("--cwd", help="its working directory; defaults to the server's")
    parser.add_argument(
        "--model", help="<provider>/<model id>; omitted leaves it to the server"
    )
    parser.add_argument(
        "--context-limit",
        dest="context_limit",
        help="the share of its context window at which it gets a `Context notice`, e.g. `20%%`",
    )
    args = parser.parse_args()

    role = args.role.strip().strip("/")
    if not role or "/" in role:
        die(f"a role is one segment, not {args.role!r}: the path is built from it")
    # The parent is the session whose shell this is, and the server says so. A name
    # typed here would be a claim about where this work came from, and the whole
    # point of the path is that the claim cannot be made.
    parent = os.environ.get("GONGGA_SESSION", "").strip()
    if not parent:
        die(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )

    extra = []
    if args.cwd:
        extra += ["--cwd", args.cwd]
    if args.model:
        extra += ["--model", args.model]
    if args.context_limit:
        extra += ["--context_limit", args.context_limit]

    for attempt in range(ATTEMPTS):
        name = f"{parent}/{role}" if attempt == 0 else f"{parent}/{role}/{attempt + 1}"
        taken, _ = create(["--session", name, "--must_create", "true", *extra])
        if taken:
            print(f"spawn: {name} is taken; trying the next segment", file=sys.stderr)
            continue
        pattern, said = subscribe(name)
        if pattern is None:
            # A child nobody hears from is worse than no child: it runs work the
            # parent will never be told is finished. Undo it, and say what is left
            # behind if the undo fails too.
            _, undone = command(["del_session", "--session", name])
            die(
                f"could not subscribe {parent} to {name}'s turn ends ({said}); "
                + (
                    f"deleted {name} again, nothing was created"
                    if undone.startswith("deleted")
                    else f"and could not delete {name}: it exists and is unheard"
                )
            )
        print(f"spawn: {said}", file=sys.stderr)
        print(name)
        return
    die(f"gave up after {ATTEMPTS} names under {parent}/{role}: nothing was created")


if __name__ == "__main__":
    main()
