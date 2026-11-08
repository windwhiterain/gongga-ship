"""Start a subagent: a session under the caller's name, whose turns the caller hears about.

    spawn(role, remote=None, cwd=None, model=None, context_limit=None)

The child is a copy of its caller, not a second session configured from scratch:
every setting this call was not told to change -- model, thinking level, context
budget, cache tier and its keepalive, mode, machine, directory -- comes from the
caller's own row. That table lives in `self-control`'s `new_session`, and this file
runs it rather than restating it: a copy of an eight-field table is a copy that
drifts, and a child that looks like its parent and is not is worse than a child
that is told less.

The child lands where the caller works: its machine is the caller's (`GONGGA_REMOTE`
in the shell the server started) and its directory is the shell's own, stated so a
sticky `cwd` cannot move it. `remote`/`cwd` override them, and a directory is only
inherited on the machine it means something on.

SPEC's `## Skill: subagent` names four steps. Two of them are one call here,
because the second is worthless when it is a step somebody has to remember:

1. **A new session under the caller session's name.** `set_session --name <name>
   --must_create true` takes the name and refuses a name that already exists, so choosing and
   claiming are one step: a probe followed by a create leaves a window in which
   another caller takes the name, and the loser silently joins the winner's
   transcript.
2. **A subscription to that session's turn end.** Armed before this call answers,
   with the command that carries the news back: `subscribe` runs what the row
   carries in the shell of the session it was run from, so the caller's own
   `send_message` is what reaches the caller. A parent left to arm this itself
   would poll instead, which is the habit `subscribe` exists to replace.

The name is a path, `<parent>/<role>`, with `/2`, `/3`, ... added when it is taken:
two callers asking for `main/impl` get `main/impl` and `main/impl/2`, and neither
gets the other's context. The parent is `GONGGA_SESSION`, which the server exports
to every shell it starts. There is no `parent`: a session that could name its
parent could plant work in somebody else's subtree.

The name is the answer, one string, so `name = spawn("impl", cwd=".")` is the whole
interface -- the command line prints it, a caller keeps it. What a human wants to
know -- which name was skipped, which subscription was armed -- goes to stderr.
"""

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
#: The skill that owns the copy table, and the entry that applies it. A child is
#: the caller's settings with a different name and its own transcript, so the
#: table is the sibling skill's (`self-control` §4c) and not a second copy here.
COPY_SKILL = "self-control"
COPY_ENTRY = "new_session"


def _die(message):
    """A refusal, in the shape a caller can catch and a command line can print."""
    raise RuntimeError(message)


def _program():
    found = next((name for name in CANDIDATES if shutil.which(name)), None)
    if found is None:
        _die("neither gongga-cli nor gongga is on PATH")
    return found


def _command(args):
    """Runs one command, returning (accepted, the message, the `data` it answered).

    A server's refusal is the caller's answer too: `gongga-cli` leaves non-zero and
    prints the same sentence to stderr, so the two are one fact and are read as one.
    """
    done = subprocess.run([_program(), *args], capture_output=True, text=True)
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
        _die(f"{args[0]} left with {done.returncode}: {why[-1] if why else 'no answer'}")
    return done.returncode == 0, message, data


def _copy(name, parent, overrides):
    """The child, created by the entry that owns the copy table; (taken, what it said).

    # Invariant

    The words are the name, the source session and the caller's overrides --
    nothing else. Every setting not named here is the source row's, decided in one
    place (`self-control` §4c), so a setting added to that table reaches children
    without a line changing here.

    The entry is asked through the server (`skill`), which is how one skill uses
    another: the server resolves the name through the same three roots the `skill`
    tool uses, and runs it with this session's environment -- its home, its key,
    its name.
    """
    words = [COPY_ENTRY, name, "--source", parent, *overrides]
    accepted, message, _data = _command(
        ["skill", "--session", parent, "--name", COPY_SKILL, "--args", json.dumps(words)]
    )
    if TAKEN in message:
        return True, message
    if not accepted:
        # The one failure worth explaining: this skill is installed, the one whose
        # table it copies is not, and the server's words alone would read as a typo.
        if "no skill named" in message:
            _die(
                f"could not create a session: {COPY_SKILL} is not installed, and spawn "
                f"copies the caller's configuration through its {COPY_ENTRY} -- the two "
                f"ship together ({message})"
            )
        _die(f"could not create a session: {message or 'no answer from the server'}")
    return False, message


#: The shell the callback runs in. Its own: the same name is the same queue, and a
#: callback behind the parent's long command is news that arrives minutes late.
def _subscribe(name):
    """Arms the caller's ears for `name`'s turn ends, returning what was armed.

    # Invariant

    The pattern is anchored at both ends. `subscribe` matches unanchored, so
    `turn_end:main/impl` would also fire for `main/impl/2`: a parent with two
    children would hear about the wrong one and wait on the right one for nothing.
    The name is a session's, not a regex's, so it is escaped before it is a pattern.

    The action is a command, sent as one: SPEC's `subscribe(id?, pattern, cmd,
    session)` runs the command in the subscriber's session, so the news is a
    `send_message` to the parent -- the same call the parent would make itself --
    and the log row it leaves behind is where the event's own fields are read.
    """
    pattern = f"^turn_end:{re.escape(name)}$"
    parent = os.environ.get("GONGGA_SESSION", "")
    action = json.dumps(
        {
            "name": "send_message",
            "args": {"name": parent, "message": f"{name} finished its turn"},
        }
    )
    accepted, message, _data = _command(
        [
            "subscribe",
            "--pattern",
            pattern,
            "--cmd",
            action,
            "--subscription",
            name,
            "--session",
            parent,
            "--persistant",
            "true",
        ]
    )
    if not accepted:
        return None, message
    return pattern, message


def _caller_place(parent):
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


def spawn(role, remote=None, cwd=None, model=None, context_limit=None):
    """Create the child session and arm its turn-end subscription; answer its name."""
    role = (role or "").strip().strip("/")
    if not role or "/" in role:
        _die(f"a role is one segment, not {role!r}: the path is built from it")
    # The parent is the session whose shell this is, and the server says so. A name
    # typed here would be a claim about where this work came from, and the whole
    # point of the path is that the claim cannot be made.
    parent = os.environ.get("GONGGA_SESSION", "").strip()
    if not parent:
        _die(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )

    # Where the caller works is where the child starts: the shell's own machine and
    # directory, which the server exports and the filesystem answers.
    place = _caller_place(parent)
    # A remote names a machine; `none` names the server's own.
    recorded = (place.get("remote") or "").strip()
    recorded = None if recorded.lower() in ("", "none") else recorded
    # No `--remote` means the caller's own machine; `--remote none` names the
    # server's own, which is a different machine and says so.
    named_remote = (remote or "").strip()
    wanted = recorded if remote is None else (
        None if named_remote.lower() in ("", "none") else named_remote
    )
    # A directory is the caller's to give away only on the caller's own machine.
    same_machine = wanted == recorded
    cwd = cwd if cwd is not None else (place.get("cwd") if same_machine else None)

    # What the caller named, and only that: the rest is the copy's to carry.
    overrides = []
    if model:
        overrides += ["--model", model]
    if context_limit:
        overrides += ["--ctx_limit", context_limit]
    if wanted:
        overrides += ["--remote", wanted]
    # The working directory is the shell's own, stated so a sticky `cwd` entry
    # cannot land the child somewhere its caller never worked.
    if cwd:
        overrides += ["--cwd", cwd]

    for attempt in range(ATTEMPTS):
        name = f"{parent}/{role}" if attempt == 0 else f"{parent}/{role}/{attempt + 1}"
        taken, _ = _copy(name, parent, overrides)
        if taken:
            print(f"spawn: {name} is taken; trying the next segment", file=sys.stderr)
            continue
        pattern, said = _subscribe(name)
        if pattern is None:
            # A child nobody hears from is worse than no child: it runs work the
            # parent will never be told is finished. Undo it, and say what is left
            # behind if the undo fails too.
            _, undone, _data = _command(["del_session", "--name", name])
            _die(
                f"could not subscribe {parent} to {name}'s turn ends ({said}); "
                + (
                    f"deleted {name} again, nothing was created"
                    if undone.startswith("deleted")
                    else f"and could not delete {name}: it exists and is unheard"
                )
            )
        print(f"spawn: {said}", file=sys.stderr)
        return name
    _die(f"gave up after {ATTEMPTS} names under {parent}/{role}: nothing was created")

