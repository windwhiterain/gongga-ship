#!/usr/bin/env python3
"""Start a subagent session under a name that cannot be someone else's.

    spawn <role> [--cwd PATH] [--model M] [--context-limit N%]

The name is a path: `<parent>/<role>`, with `/2`, `/3`, ... added when that name is
taken. The parent is `GONGGA_SESSION`, which the server exports to every shell it
starts -- never an argument, so a subagent started from `main` is `main/impl`, one
started from *that* is `main/impl/review`, and the name cannot be made to claim a
parent it did not come from.

Two guarantees, and they are why this is a script rather than a line of advice:

- **The name is taken, not checked.** `set_session --must_create true` refuses a
  name that already exists, so the choice and the creation are one step. A probe
  followed by a create would leave a window in which another caller takes the name,
  and the loser would silently join the winner's transcript.
- **A collision is a new segment, never a share.** Two callers asking for
  `main/impl` get `main/impl` and `main/impl/2`; neither gets the other's context.
- **The parent is the caller's own session.** There is no `--parent`: a session
  that could name its parent could plant work in somebody else's subtree, which is
  the same lie `set_session` was fixed to refuse.

The name goes to stdout on one line, so `name=$(spawn impl --cwd .)` is the whole
interface. Everything a human wants to know -- which name was skipped, which model
was drawn -- goes to stderr.
"""

import argparse
import json
import os
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


def cli():
    found = next((name for name in CANDIDATES if shutil.which(name)), None)
    if found is None:
        die("neither gongga-cli nor gongga is on PATH")
    return found


def set_session(args):
    """Runs one `set_session`, returning (created, message).

    A non-zero exit is not swallowed: the caller reads the message to tell a taken
    name from everything else, and anything else is fatal.
    """
    done = subprocess.run(
        [cli(), "set_session", *args], capture_output=True, text=True
    )
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
    return message


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", help="one segment: what this subagent is for")
    parser.add_argument("--cwd", help="its working directory; defaults to the server's")
    parser.add_argument("--model", help="<provider>/<model name>; omitted leaves it to the server")
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
    parent = os.environ.get("GONGGA_SESSION", "")
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
        message = set_session(["--session", name, "--must_create", "true", *extra])
        if TAKEN in message:
            print(f"spawn: {name} is taken; trying the next segment", file=sys.stderr)
            continue
        if not message.startswith("session is now"):
            die(f"could not create {name}: {message or 'no answer from the server'}")
        print(name)
        return
    die(f"gave up after {ATTEMPTS} names under {parent}/{role}: nothing was created")


if __name__ == "__main__":
    main()
