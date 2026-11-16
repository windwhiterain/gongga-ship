#!/usr/bin/env python3
"""Ask another session a question; a channel waits, and hands the answer over.

    ask(session, question)

Three steps, and the caller remembers none of them:

1. a channel of the session's own is started under a name the server minted
   (`new_channel_id`, so no two asks race for one name);
2. a subscription is armed on the mentor's `turn_end`, whose action types one line into
   that channel -- `python answer.py <mentor> <first row> <caller> <channel>`;
3. the question goes to the mentor with `send_message`.

Then the call is over. When the mentor's turn ends, `answer.py` runs **in the channel**,
reads the mentor's transcript from the row the question made, and `send_message`s the
answer to the caller. Nothing blocks: not this command, and not the turn that asked it.

Why a channel at all: waiting is `exec`'s, not a command's. Measured, an `ask` that
polled the transcript in its own process held a turn for 97 s and was ended by the very
message it was waiting for. A channel is a program that outlives the turn, which is what
waiting needs; the subscription is what starts it, so nothing polls.

It talks to the server through the harness's own client (`_client`), so nothing here
spawns a command line or re-parses printed JSON: a `Reply` already says whether the
command ran and what it carried.

`ask` is the API, and `__init__.py` beside this file publishes it as the skill's one
function. SKILL.md "The one convenience: ask" is the prose this implements. A refusal is
`_Refused`, which the package form re-raises as `RuntimeError` so the runner prints one
line.
"""

import os
import re
import shlex
import sys

import _client
import answer

#: The variable the server exports into every process it starts for a session.
SESSION_VAR = "GONGGA_SESSION"
#: The namespace ask's channels are minted in, so they read as what they are.
NAMESPACE = "/ask"
#: The program a waiting channel runs: a shell, because the subscription types a line.
PROGRAM = "bash"
#: The waiter beside this file, run by that line.
WAITER = "answer.py"


class _Refused(Exception):
    """A refusal whose message is the user's: the form that caught it prints it.

    The package form re-raises it as RuntimeError, which the package runner prints.
    """


def _die(message):
    raise _Refused(message)


def _caller():
    """The session this call is the work of: the one the answer has to reach."""
    name = (os.environ.get(SESSION_VAR) or "").strip()
    if not name:
        _die(
            f"ask reports to the session that asked, and this process has no {SESSION_VAR}: "
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


def _minted(client, caller):
    """A channel name nobody in `caller` holds, from the server that can tell."""
    reply = client.call("new_channel_id", prefix=NAMESPACE, session=caller)
    if not reply.ok:
        _die(reply.message or f"cannot mint a channel name in {caller}")
    data = reply.data if isinstance(reply.data, dict) else {}
    name = data.get("id")
    if not isinstance(name, str) or not name:
        _die(f"the server minted no channel name in {caller}")
    return name


def _started(client, caller, channel):
    """Starts the waiting channel: a program of its own, outliving this call.

    It is started on **this** machine (`remote: none`), not on the session's: the waiter
    beside this file is here, and an answer is read from the transcript, which is here too.
    A session whose workspace is elsewhere therefore asks from the same channel.
    """
    reply = client.call(
        "new_channel", id=channel, name=PROGRAM, remote="none", session=caller
    )
    if not reply.ok:
        _die(reply.message or f"cannot start channel {channel} in {caller}")


def _line(session, first, caller, channel):
    """The one line the subscription types into the channel when the turn ends.

    What the waiter needs travels in the environment rather than as arguments, and the
    channel id travels without its leading slash: a shell on Windows rewrites a word that
    begins with `/` into a Windows path, whether the word is an argument or an assignment
    value -- measured, `/ask/1` reached `answer.py` as `C:/SoftWare/git/Git/ask/1`, and
    the channel was left behind.
    """
    waiter = os.path.join(os.path.dirname(os.path.abspath(__file__)), WAITER)
    carried = {
        answer.MENTOR_VAR: session,
        answer.FIRST_VAR: str(first),
        answer.CALLER_VAR: caller,
        # The id travels without its leading `/`: a shell on Windows rewrites a word that
        # begins with one into a Windows path, and it does that to an assignment too --
        # measured, `/ask/1` arrived as `C:/SoftWare/git/Git/ask/1`.
        answer.CHANNEL_VAR: channel.lstrip("/"),
    }
    prefix = " ".join(
        f"{name}={shlex.quote(value)}" for name, value in carried.items()
    )
    return f"{prefix} {shlex.quote(sys.executable)} {shlex.quote(waiter)}"


def _armed(client, caller, session, channel, line):
    """Arms the callback: one match of that session's turn end runs the waiter."""
    reply = client.call(
        "subscribe",
        pattern=f"^turn_end:{re.escape(session)}$",
        cmd={
            "name": "input_channel",
            "args": {"id": channel, "input": line, "session": caller, "wait": 0},
        },
        session=caller,
    )
    if not reply.ok:
        _die(reply.message or f"cannot arm a callback on {session}'s turn end")
    data = reply.data if isinstance(reply.data, dict) else {}
    return data.get("subscription")


def _deliver(client, session, question):
    """Puts the question into the session's hands."""
    reply = client.call("send_message", session=session, message=question)
    if not reply.ok:
        _die(reply.message or f"the question did not reach {session}")


def _undo(client, caller, channel, armed):
    """Takes back what an ask that could not finish had already set up.

    Best effort: the refusal the caller needs is the one that started this, so nothing
    here is allowed to replace it with its own.
    """
    try:
        if armed is not None:
            client.call("del_subscription", path=str(armed), session=caller)
        client.call("del_channel", id=channel, session=caller)
    except Exception:  # noqa: BLE001 - the caller's own refusal is the one to report
        pass


def ask(session, question):
    """The question delivered, a channel waiting; a sentence saying what is armed.

    The cursor is taken before the question is sent, so the row the waiter reads from is
    the one this question made, never a turn that ended before it.
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
            f"{session} is the session asking: its own turn cannot answer a question it is "
            "in the middle of, so ask another session"
        )
    try:
        client = _client.connect()
    except _client.Unusable as unusable:
        _die(str(unusable))
    channel, armed = None, None
    try:
        first = _tail(client, session) + 1
        channel = _minted(client, caller)
        _started(client, caller, channel)
        armed = _armed(client, caller, session, channel, _line(session, first, caller, channel))
        _deliver(client, session, question)
    except BaseException:  # noqa: BLE001 - a channel started for nobody is litter
        if channel is not None:
            _undo(client, caller, channel, armed)
        raise
    finally:
        client.close()
    return (
        f"asked {session}; channel {channel} waits for that turn to end and will send you the "
        f"answer (subscription `{armed}`). You can read it yourself any time: "
        f"get_messages --session {session} --range {first}:"
    )
