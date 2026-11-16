#!/usr/bin/env python3
"""Ask another session a question; a subscription brings the answer back.

    ask(session, question)

Two steps, and the caller remembers neither:

1. a subscription of the caller's own is armed on the mentor's `turn_end`, whose
   command is one `send_message` to the caller carrying `{event.answer}` -- the turn's
   own words, put there by the server;
2. the question goes to the mentor with `send_message`.

Then the call is over. When the mentor's turn ends, the subscription's `send_message`
lands in the caller's transcript with the answer in it. Nothing blocks: not this
command, and not the turn that asked it.

The question is also *why* the answer is a turn's: the subscription is one-shot, so it
fires on the first turn end after it was armed. A mentor that was already in the middle
of a turn may end that one first -- `send_message` reaches it at a step boundary, so
that turn often answers too, but not always. The message names the event row and the
row the question was asked at, so a reader can tell the two apart.

Waiting is the `wait` tool's, not a command's: an `ask` that polled the transcript in
its own process held a turn for 97 s and was ended by the very message it was waiting
for. A subscription is what a turn can leave behind, so nothing polls and nothing sleeps.

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


def _armed(client, caller, session, first):
    """Arms the callback: one match sends the mentor's own words to the asker.

    # Invariant

    The pattern is anchored at both ends, for the reason `subscribe` matches
    unanchored: `^turn_end:main$` must not also fire for `main/impl`. The name is a
    session's path, so it is escaped before it is a pattern.

    `{event.answer}` is the turn's answer, which the server now keeps whole. The
    message also names the event row and the row the question was asked at: the
    subscription is one-shot, and a turn that was already running ends first.
    """
    reply = client.call(
        "subscribe",
        pattern=f"^turn_end:{re.escape(session)}$",
        cmds=[
            {
                "name": "send_message",
                "args": {
                    "session": caller,
                    "message": (
                        f"{session} ended a turn (event row {{event.id}}); the question was "
                        f"asked at row {first}:\n\n{{event.answer}}"
                    ),
                },
            }
        ],
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


def _disarm(client, caller, armed):
    """Takes back the subscription an ask that could not finish had already armed.

    Best effort: the refusal the caller needs is the one that started this, so nothing
    here is allowed to replace it with its own.
    """
    if armed is None:
        return
    try:
        client.call("del_subscription", path=str(armed), session=caller)
    except Exception:  # noqa: BLE001 - the caller's own refusal is the one to report
        pass


def ask(session, question):
    """The question delivered, an answer armed; a sentence saying what is armed.

    The cursor is taken before the question is sent, so the row it names is the one
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
            f"{session} is the session asking: its own turn cannot answer a question it is "
            "in the middle of, so ask another session"
        )
    try:
        client = _client.connect()
    except _client.Unusable as unusable:
        _die(str(unusable))
    armed = None
    try:
        first = _tail(client, session) + 1
        armed = _armed(client, caller, session, first)
        _deliver(client, session, question)
    except BaseException:  # noqa: BLE001 - a subscription armed for nobody is litter
        _disarm(client, caller, armed)
        raise
    finally:
        client.close()
    return (
        f"asked {session}; its turn end sends you the answer (subscription `{armed}`). "
        f"You can read it yourself any time: get_messages --session {session} --range {first}:"
    )
