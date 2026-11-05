#!/usr/bin/env python3
"""Start a subagent: a session under the caller's name, whose turns the caller hears about.

    spawn <role> [--remote R] [--cwd PATH] [--model M] [--context-limit N%]

The child lands where the caller works: its model, its remote and its working
directory are the caller's own -- the server exports `GONGGA_MODEL` and
`GONGGA_REMOTE` to every shell it starts, and the shell's own directory is the
session's -- and `--remote`/`--cwd` override them. A flagless `set_session` is
no read any more: sticky-filled arguments would turn it from a read into a
move, which is why these facts come from the environment instead.

SPEC's `## Skill: subagent` names four steps. Two of them are one command here,
because the second is worthless when it is a step somebody has to remember:

1. **A new session under the caller session's name.** `set_session <name>
   --must_create true` takes the name and refuses a name that already exists, so choosing and
   claiming are one step: a probe followed by a create leaves a window in which
   another caller takes the name, and the loser silently joins the winner's
   transcript.
2. **A subscription to that session's turn end.** Armed before this command
   answers, with the command that carries the news back: `subscribe` runs what
   the row carries in the shell of the session it was run from, so the caller's
   own `send_message` is what reaches the caller. A parent left to arm this
   itself would poll instead, which is the habit `subscribe` exists to replace.

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
    """Runs one command, returning (accepted, the message, the `data` it answered).

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
    data = None
    for event in events:
        if event.get("t") == "cmd_result":
            message = event.get("message") or ""
            data = event.get("data")
    if done.returncode != 0 and not message:
        why = (done.stderr or done.stdout).strip().splitlines()
        die(f"{args[0]} left with {done.returncode}: {why[-1] if why else 'no answer'}")
    return done.returncode == 0, message, data


def create(args):
    """One `set_session <name> --must_create`, returning (taken, what the server said)."""
    accepted, message, _data = command(["set_session", *args])
    if TAKEN in message:
        return True, message
    if not accepted or not message.startswith("session is now"):
        die(f"could not create a session: {message or 'no answer from the server'}")
    return False, message


#: The shell the callback runs in. Its own: the same name is the same queue, and a
#: callback behind the parent's long command is news that arrives minutes late.
NOTICE_SHELL = "notice"


def relay():
    """The command a match runs: the event, sent back to the session that armed it.

    # Invariant

    The address is `$GONGGA_SESSION`, which the server exports into every shell of a
    session, so the news goes to whoever armed this and never to the subject of the
    pattern. The three `GONGGA_EVENT_*` names are the prefix the server puts on this
    one command line; they are gone with it. Their values are expanded, never
    re-parsed, so a payload full of quotes is carried as data.
    """
    return (
        'gongga-cli send_message --name "$GONGGA_SESSION" '
        '--message "$GONGGA_EVENT_KIND $GONGGA_EVENT_SESSION: $GONGGA_EVENT_PAYLOAD"'
    )


def subscribe(name):
    """Arms the caller's ears for `name`'s turn ends, returning what was armed.

    # Invariant

    The pattern is anchored at both ends. `subscribe` matches unanchored, so
    `turn_end:main/impl` would also fire for `main/impl/2`: a parent with two
    children would hear about the wrong one and wait on the right one for nothing.
    The name is a session's, not a regex's, so it is escaped before it is a pattern.

    The action is part of arming, not a second step: a subscription whose row
    carries nothing fires and tells nobody, and the server refuses to write one.
    """
    pattern = f"^turn_end:{re.escape(name)}$"
    accepted, message, _data = command(
        [
            "subscribe",
            name,
            pattern,
            "--persistant",
            "true",
            "--bash",
            NOTICE_SHELL,
            "--cmd",
            relay(),
        ]
    )
    if not accepted:
        return None, message
    return pattern, message


def caller_place(parent):
    """The caller session's own `remote` and `cwd`, as the shell's own facts.

    # Invariant

    The server exports a session's model and remote to every shell it starts,
    and a shell's own directory is its session's: that is what makes a child
    inherit its parent's machine without the parent having to state it, and
    what keeps this from guessing -- a wrong guess would start the child in a
    directory that does not exist on the machine it lands on.
    """
    remote = os.environ.get("GONGGA_REMOTE", "").strip()
    return {"remote": remote or None, "cwd": os.getcwd()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", help="one segment: what this subagent is for")
    parser.add_argument(
        "--remote", help="the machine its shells run on; defaults to the caller's own"
    )
    parser.add_argument(
        "--cwd", help="its working directory; defaults to the caller's own"
    )
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

    # Where the caller works is where the child starts: the caller's own row is
    # the only place that knows its machine and its directory.
    place = caller_place(parent)
    env_model = os.environ.get("GONGGA_MODEL", "").strip()
    # A remote names a machine; `none` names the server's own.
    recorded = (place.get("remote") or "").strip()
    recorded = None if recorded.lower() in ("", "none") else recorded
    # No `--remote` means the caller's own machine; `--remote none` names the
    # server's own, which is a different machine and says so.
    stated = (args.remote or "").strip()
    wanted = recorded if args.remote is None else (
        None if stated.lower() in ("", "none") else stated
    )
    # A directory is the caller's to give away only on the caller's own machine.
    same_machine = wanted == recorded
    cwd = args.cwd if args.cwd is not None else (place.get("cwd") if same_machine else None)

    extra = []
    if wanted:
        extra += ["--remote", wanted]
    # The working directory is the shell's own, stated so a sticky `cwd` entry
    # cannot land the child somewhere its caller never worked.
    if cwd:
        extra += ["--cwd", cwd]
    if args.context_limit:
        extra += ["--ctx_limit", args.context_limit]

    for attempt in range(ATTEMPTS):
        name = f"{parent}/{role}" if attempt == 0 else f"{parent}/{role}/{attempt + 1}"
        # The model is the caller's own unless one is named: stated, a sticky
        # entry cannot silently swap it for another project's.
        # Optional arguments keep their flags (SPEC: order-omission is the
        # non-optional arguments' to give), so the model is named, not positional.
        stated = args.model or env_model
        model = ["--model", stated] if stated else []
        taken, _ = create([name, *model, "--must_create", "true", *extra])
        if taken:
            print(f"spawn: {name} is taken; trying the next segment", file=sys.stderr)
            continue
        pattern, said = subscribe(name)
        if pattern is None:
            # A child nobody hears from is worse than no child: it runs work the
            # parent will never be told is finished. Undo it, and say what is left
            # behind if the undo fails too.
            _, undone, _data = command(["del_session", name])
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
