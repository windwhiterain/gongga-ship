"""Start a subagent: a session under the caller's own name, whose turns the caller hears about.

    config = self_config()                          # SPEC's SessionConfig, as the caller's row says
    config["model_config"] = pick_config("flash")    # what the child should change, on that object
    name = new_subagent(config, role="impl")         # -> <parent>/impl

# Invariant

A child differs from its caller only where the caller said so. `self_config` reads
the caller's own row and answers it as SPEC's nested `SessionConfig`, so naming a
different model, thinking level, context budget, cache tier, machine or directory
is a key set on one dict -- not a flag list this skill would have to keep in step
with the server's. The dict is the whole statement: what it does not name, the
child takes from the configured defaults.

SPEC's `## Skill: subagent` names four steps, and `new_subagent` does the first two,
because the second is worthless when it is a step somebody has to remember:

1. **A new session under the caller session's name.** `new_session` refuses a name
   that already exists (SPEC: it is `set_session` that must create), so choosing and
   claiming are one step: a probe followed by a create leaves a window in which
   another caller takes the name, and the loser silently joins the winner's
   transcript.
2. **A subscription to that session's turn end.** Armed before this call answers, with
   the command that carries the news back. SPEC's `subscribe` puts a field of the
   matched event into a string argument, so the parent's own `send_message` says what
   the child said -- `{event.answer}` is the turn's answer, whole. No process of its
   own: the news is a row the server writes, so a restart still delivers it. A parent
   left to arm this itself would poll instead, which is the habit `subscribe` exists
   to replace.

The name is a path, `<parent>/<role>`, with `/2`, `/3`, ... added when it is taken:
two callers asking for `main/impl` get `main/impl` and `main/impl/2`, and neither
gets the other's context. The parent is `GONGGA_SESSION`, which the server exports
to every shell it starts. There is no `parent` argument: a session that could name
its parent could plant work in somebody else's subtree.

The name is the answer, one string, so `name = new_subagent(config)` is the whole
interface -- the command line prints it, a caller keeps it. What a human wants to
know -- which name was skipped, which subscription was armed -- goes to stderr.
"""

import json
import os
import re
import sys
import typing

#: The one connection this run talks through, dialled on first use (`_connection`).
_CONNECTION = None

# How many names to try before giving up. Each attempt is one round trip, and a
# parent with a thousand siblings is a bug in the caller, not a case to serve.
ATTEMPTS = 1000
# What the server says when it refuses a name that exists. Its answer is the only
# signal that separates "name taken" from a real failure, so it is named once, here.
TAKEN = "--must_create refuses"



def _die(message):
    """A refusal, in the shape a caller can catch and a command line can print."""
    raise RuntimeError(message)


def _library():
    """The directory holding this home's `gongga.py`, or None when nothing names one.

    # Invariant

    The module beside *this* home, never an installed copy elsewhere: the client
    and the server it dials are one build, and the server is what wrote it. Two
    places name that directory, and a skill gets one of them from whatever started
    it -- `GONGGA_SKILL_LIB` from a skill runner, `GONGGA_HOME` from a shell the
    server started.
    """
    for directory in (
        os.environ.get("GONGGA_SKILL_LIB"),
        # The content's Python surface is the checkout's, in place: protocol 12 moved it
        # out of a home's `lib`, which an older build used to copy. Same lookup as
        # `self-control/_client.py`, the one other reader of this directory.
        os.path.join(os.environ.get("GONGGA_HOME") or "", "ship", "python"),
    ):
        if directory and os.path.isfile(os.path.join(directory, "gongga.py")):
            return directory
    return None


def _module():
    """The harness's own client module, imported from the home that serves this shell."""
    directory = _library()
    if directory is None:
        _die(
            "no gongga.py to import: this needs GONGGA_HOME (a shell the server "
            "started has it) or GONGGA_SKILL_LIB"
        )
    if directory not in sys.path:
        sys.path.insert(0, directory)
    try:
        import gongga
    except Exception as unusable:
        _die(f"could not import {os.path.join(directory, 'gongga.py')}: {unusable}")
    return gongga


def _connection(gongga):
    """This home's server, dialled once and kept for the length of the run.

    # Invariant

    One connection for the whole run: a spawn is several calls (the create, the
    subscription, an undo), and a handshake before each is a round trip before
    each. A connection the server has since dropped fails its call, which is a
    refusal the caller sees -- not something to paper over by redialling.
    """
    global _CONNECTION
    if _CONNECTION is None:
        try:
            _CONNECTION = gongga.connect()
        except gongga.Unreachable as unreachable:
            _die(str(unreachable))
        except OSError as missing:
            _die(f"no key to dial with: {missing}")
    return _CONNECTION


def _command(cmd):
    """Runs one Cmd, returning (accepted, the message, the `data` it answered).

    # Invariant

    In process, through the home's own client -- never through a command line. On
    Windows `gongga-cli` on PATH is the client, whose answer to a command is that
    the command belongs to `gongga-cli`; reading printed lines back would also make
    this a second copy of a contract `gongga.connect` already answers with a
    `Reply`. An argument travels as the wire spells it, so SPEC's `config` and
    `subscribe`'s `cmd` are dicts here.

    A refusal is the caller's answer too: the client reports it as `ok` false with
    the server's own sentence, so "it ran and said no" and "it said yes" keep one
    shape.
    """
    gongga = _module()
    server = _connection(gongga)
    try:
        reply = server.call(cmd["name"], **cmd.get("args", {}))
    except gongga.Unreachable as unreachable:
        _die(str(unreachable))
    except OSError as missing:
        _die(f"no key to dial with: {missing}")
    return reply.ok, reply.message, reply.data


def _caller():
    """The session whose shell this is, or a refusal that names the missing export."""
    name = (os.environ.get("GONGGA_SESSION") or "").strip()
    if not name:
        _die(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )
    return name


def _published(name):
    """One session row as the server publishes it.

    # Invariant

    The server's own statement, not a `select` of this skill's own: what a session's
    configuration is, is a question the harness answers once (`gongga_types`), and a
    field list retyped here drifted from SPEC as soon as SPEC moved one -- the
    `workspace.remote` this skill used to write is refused by the server now.
    """
    gongga = _module()
    server = _connection(gongga)
    try:
        server.pump(0)
    except Exception:
        pass
    for row in server.sessions:
        if row.get("name") == name:
            return row
    # Not in the last published list: ask, once, rather than read the database.
    reply = _command({"name": "ls_session", "args": {"path": name}})
    if not reply[0]:
        _die(f"could not read {name}: {reply[1]}")
    for row in server.sessions:
        if row.get("name") == name:
            return row
    _die(f"the server publishes no session named {name}")


def self_config(session: typing.Optional[str] = None) -> "gongga.SessionConfig":
    """The caller's own settings as SPEC's `SessionConfig`, ready to change.

    `session` is for a caller reading somebody else's row; the default is this
    shell's own session, `GONGGA_SESSION`. A setting the session does not state is
    left out of the dict rather than written as `None`, so "unchanged" and "the
    model's default" stay one thing to the caller and to the server.
    """
    gongga = _module()
    name = (session or "").strip() or _caller()
    return gongga.config_of_session(_published(name))


#: The shell the callback runs in. Its own: the same name is the same queue, and a
#: callback behind the parent's long command is news that arrives minutes late.
def _subscribe(name):
    """Arms the caller's ears for `name`'s turn ends, returning what was armed.

    # Invariant

    The pattern is anchored at both ends. `subscribe` matches unanchored, so
    `turn_end:main/impl` would also fire for `main/impl/2`: a parent with two
    children would hear about the wrong one and wait on the right one for nothing.
    The name is a session's, not a regex's, so it is escaped before it is a pattern.

    The action carries what the child said: SPEC's `subscribe` puts a field of the
    matched event into a string argument, so `{event.answer}` is that turn's own answer
    -- the whole of it, since the server stores no head of it. It is one `send_message`
    to the parent and no process of its own: the news is a row the server writes, which
    a restart still delivers.
    """
    pattern = f"^turn_end:{re.escape(name)}$"
    parent = os.environ.get("GONGGA_SESSION", "")
    action = {
        "name": "send_message",
        "args": {
            "session": parent,
            "message": (
                f"{name} finished its turn ({{event.outcome}}, "
                f"{{event.steps}} step(s), {{event.elapsed_ms}}ms):\n\n{{event.answer}}"
            ),
        },
    }
    accepted, message, _data = _command(
        {
            "name": "subscribe",
            "args": {
                "pattern": pattern,
                "cmds": [action],
                "id": name,
                "session": parent,
                "persistant": "true",
            },
        }
    )
    if not accepted:
        return None, message
    return pattern, message


def new_subagent(config: dict, role: str = "worker"):
    """Create the child session and arm its turn-end subscription; answer its name.

    `config` is SPEC's `SessionConfig` as one dict -- in practice `self_config()`
    with the keys this child should differ in set on it, which is how a child gets
    a model its caller does not run. `role` is one name segment, and the child is
    `<parent>/<role>`, with `/2` and onwards added while that name is taken.
    """
    if not isinstance(config, dict):
        _die(
            f"a config is SPEC's SessionConfig, one dict, not {type(config).__name__}: "
            "start from self_config()"
        )
    role = (role or "").strip().strip("/")
    if not role or "/" in role:
        _die(f"a role is one segment, not {role!r}: the path is built from it")
    parent = _caller()
    # Sent as the object it is: SPEC's `Cmd.args` is `dict[str, any]` end to end, so the
    # server reads `config` as the table it was given. Serialised here it would arrive as
    # a string and be refused. Nothing is dropped or nulled on the way: an absent key is
    # what "the default" means, and a stated `null` is a statement of its own.
    try:
        json.dumps(config)
    except (TypeError, ValueError) as unusable:
        _die(f"the config is not JSON: {unusable}")
    payload = config

    for attempt in range(ATTEMPTS):
        name = f"{parent}/{role}" if attempt == 0 else f"{parent}/{role}/{attempt + 1}"
        accepted, message, _data = _command(
            {"name": "new_session", "args": {"id": name, "config": payload}}
        )
        if TAKEN in message:
            print(f"new_subagent: {name} is taken; trying the next segment", file=sys.stderr)
            continue
        if not accepted:
            _die(f"could not create a session: {message or 'no answer from the server'}")
        pattern, said = _subscribe(name)
        if pattern is None:
            # A child nobody hears from is worse than no child: it runs work the
            # parent will never be told is finished. Undo it, and say what is left
            # behind if the undo fails too.
            _, undone, _data = _command({"name": "del_session", "args": {"path": name}})
            _die(
                f"could not subscribe {parent} to {name}'s turn ends ({said}); "
                + (
                    f"deleted {name} again, nothing was created"
                    if undone.startswith("deleted")
                    else f"and could not delete {name}: it exists and is unheard"
                )
            )
        print(f"new_subagent: {said}", file=sys.stderr)
        return name
    _die(f"gave up after {ATTEMPTS} names under {parent}/{role}: nothing was created")
