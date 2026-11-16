#!/usr/bin/env python3
"""Ask another session a question, and be told when its answer is there.

    ask(session, question)

The question goes to that session the way `send_message` does. The answer is not
carried back here: an answer lives in the transcript of the session that said it, and
this module arranges for you to be woken when it exists -- it arms a subscription on
that session's `turn_end` whose action is a `send_message` to you, naming the row the
answer starts at.

Nothing here waits. SPEC gives waiting to `exec`'s channel and to `wait`, and a command
that blocks holds the turn it was asked in: measured, an `ask` that polled the
transcript held a turn for 97 s and was ended by the very message it was waiting for --
the ping and the answer are one event, so a blocking ask can never be the one that
receives it. The ping *is* the wait: it arrives at your next step boundary, and the
answer is read from the transcript.

It talks to the server through the harness's own client (`_client`), so nothing here
spawns a command line or re-parses printed JSON: a `Reply` already says whether the
command ran and what it carried. SKILL.md "The Python client inside a script" is the
rule.

Why a function at all: asking is three steps with a name to remember between each --
read the cursor, arm the ping, send the question -- and a name remembered wrong is a
question sent to the wrong session. `ask` names both of its parameters, so neither can
be read as the other.

`ask` is the API, and `__init__.py` beside this file publishes it as the skill's one
function. SKILL.md "The one convenience: ask" is the prose this implements. A refusal
is `_Refused`, which the package form re-raises as `RuntimeError` so the runner prints
one line.
"""

import os
import re

import _client

#: The variable the server exports into every process it starts for a session.
SESSION_VAR = "GONGGA_SESSION"


class _Refused(Exception):
    """A refusal whose message is the user's: the form that caught it prints it.

    The package form re-raises it as RuntimeError, which the package runner prints.
    """


def _die(message):
    raise _Refused(message)


def _caller():
    """The session this call is the work of: the one the ping has to reach."""
    name = (os.environ.get(SESSION_VAR) or "").strip()
    if not name:
        _die(
            f"ask pings the session that asked, and this process has no {SESSION_VAR}: "
            "run it as a session's work, where the server exports it"
        )
    return name


def _tail(client, session):
    """Where the transcript of `session` ends now, before the question is sent."""
    reply = client.call("get_messages", session=session, range="-1:-1", replay="true")
    if not reply.ok:
        _die(reply.message or f"cannot read the transcript of {session}")
    data = reply.data if isinstance(reply.data, dict) else {}
    end = data.get("last_seq")
    return end if isinstance(end, int) else 0


def _arm(client, caller, session, since):
    """Tells `caller` when `session`'s turn ends, naming the row to read.

    The pattern is anchored and escaped: `^turn_end:m/entor$` is that one session's
    turn ending, and no other session whose name contains that one as a prefix.
    """
    ping = (
        f"{session} finished its turn: the answer to your question is in its transcript, "
        f"from row {since + 1}. Read it with: get_messages --session {session} "
        f"--range {since + 1}:"
    )
    reply = client.call(
        "subscribe",
        pattern=f"^turn_end:{re.escape(session)}$",
        cmd={"name": "send_message", "args": {"session": caller, "message": ping}},
        session=caller,
    )
    if not reply.ok:
        _die(reply.message or f"cannot arm a ping on {session}'s turn end")
    data = reply.data if isinstance(reply.data, dict) else {}
    return data.get("subscription")


def _deliver(client, session, question):
    """Puts the question into the session's hands."""
    reply = client.call("send_message", session=session, message=question)
    if not reply.ok:
        _die(reply.message or f"the question did not reach {session}")


def _disarm(client, armed, caller):
    """Drops the ping of a question that never went out: nothing stays armed."""
    if armed is None:
        return
    client.call("del_subscription", path=str(armed), session=caller)


def ask(session, question):
    """The question delivered and the ping armed; a sentence saying what to read, where.

    The cursor is taken before the question is sent, so the range it names is the rows
    this question made, never a turn that ended before it.
    """
    session = (session or "").strip()
    question = (question or "").strip()
    if not session:
        _die("name the session to ask")
    if not question:
        _die("ask something: the question is the second parameter")
    caller = _caller()
    if session == caller:
        _die(
            f"{session} is the session asking: its own turn cannot answer a question it "
            "is in the middle of, so ask another session"
        )
    try:
        client = _client.connect()
    except _client.Unusable as unusable:
        _die(str(unusable))
    try:
        since = _tail(client, session)
        armed = _arm(client, caller, session, since)
        try:
            _deliver(client, session, question)
        except _Refused:
            _disarm(client, armed, caller)
            raise
    finally:
        client.close()
    return (
        f"asked {session}; subscription `{armed}` pings you when that turn ends, and the "
        f"answer is in its transcript from row {since + 1}: "
        f"get_messages --session {session} --range {since + 1}:"
    )
