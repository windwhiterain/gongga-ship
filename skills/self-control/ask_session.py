#!/usr/bin/env python3
"""Ask another session a question and wait for its answer.

    ask_session <session> "<question>"

The question goes to that session the way `send_message` does, and the answer
comes back from the place that keeps it: the session's own transcript, read the
way a window reads it (`get_messages --replay true`). This module reads that
transcript instead of asking the caller to poll.

It talks to the server through the harness's own client (`_client`), so nothing
here spawns a command line or re-parses printed JSON: a `Reply` already says
whether the command ran and what it carried. SKILL.md §0b is the rule.

Why a verb at all: asking is two steps with a name to remember in between, and a
name remembered wrong is a question sent to the wrong session -- or, with the
positional form of `send_message`, the session name read as the question. The
session is a positional argument here, and the question is what follows it.

`ask` is the API and `main` is the script form of it: the answer printed, and a
refusal as exit code 2. SKILL.md §4b is the prose this implements.

Exit: 0 the answer was received, 2 the question could not be asked or no
answer arrived.
"""

import argparse
import sys
import time

import _client

#: How long to wait for the answer. A turn is a model's round trip, so this is
#: measured in minutes; a question unanswered by then is reported as unanswered
#: rather than waited on forever.
TIMEOUT_SECONDS = 900
#: How long between reads of the transcript.
POLL_SECONDS = 1.0


class _Refused(Exception):
    """A refusal whose message is the user's: the form that caught it prints it.

    The script leaves with 2; the package form re-raises it as RuntimeError, which
    the package runner prints the same way.
    """


def _die(message):
    raise _Refused(message)


def _unwrapped(message):
    """The event one message carries, from the stream or from the transcript.

    `python/gongga.py` keeps what it read as it read it: the stream wraps an event
    as `{"t": "event", "v": {"seq": n, "event": {...}}}`, and a replay wraps a
    stored row holding that same event as `{"t": "row", "v": {"seq": n, "event":
    {...}}}`. Both carry one event, and `_wait` needs the stored one, so both are
    unwrapped here.
    """
    if message.get("t") in ("event", "row"):
        payload = message.get("v")
        if isinstance(payload, dict) and isinstance(payload.get("event"), dict):
            return payload["event"]
    return message


def _rows(reply):
    """One reply's events, each with its envelope off."""
    return [_unwrapped(message) for message in reply.events if isinstance(message, dict)]


def _end_of(reply):
    """Where the transcript ends, as the reply's `data` says; 0 when it does not."""
    data = reply.data if isinstance(reply.data, dict) else {}
    end = data.get("last_seq")
    return end if isinstance(end, int) else 0


def _tail(client, session):
    """Where the transcript ends now, before the question is sent."""
    reply = client.call("get_messages", session=session, range="-1:-1", replay="true")
    if not reply.ok:
        _die(reply.message or f"cannot read the transcript of {session}")
    return _end_of(reply)


def _read(client, session, since):
    """The transcript of `session` after `since`, where it ends, and its status.

    `replay` is what makes a read show the session's own rows -- the operator's
    message as an input line, the answer as an output line -- instead of only what
    streams past while the call is open. Its `data` carries the two numbers this
    module needs: the last row, and whether the session is still running.
    """
    reply = client.call("get_messages", session=session, range=f"{since + 1}:", replay="true")
    if not reply.ok:
        _die(reply.message or f"cannot read the transcript of {session}")
    data = reply.data if isinstance(reply.data, dict) else {}
    end = data.get("last_seq")
    return _rows(reply), (end if isinstance(end, int) else since), data.get("status")


def _deliver(client, session, question):
    """Puts the question into the session's hands."""
    reply = client.call("send_message", session=session, message=question)
    if not reply.ok:
        _die(reply.message or f"the question did not reach {session}")


def _answer_in(rows, session):
    """What the session said since the question, and whether the question landed.

    Two filters, both needed. The stream carries other sessions' rows too, so the
    session name picks ours. And the operator's row is the marker rather than its
    text: a message is expanded before it is stored (`@skill(...)` gains its path),
    so what is read back is not the bytes that were sent -- but a row after the
    cursor taken before asking is the question this call delivered, and an output
    before it belongs to a turn that started earlier.
    """
    asked, said = False, []
    for row in rows:
        if row.get("t") != "response" or row.get("session") != session:
            continue
        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        if row.get("part") == "input":
            asked, said = True, []
        elif row.get("part") == "output":
            said.append(text.strip())
    return asked, said


def _wait(client, session, since, timeout, poll):
    """The session's turn ends, and what it said since the question is the answer.

    # Invariant

    The answer is the last output row the transcript *stores*, read once the session
    is no longer running. A turn still being written sends pieces, each its own event
    with `done` false, so the newest piece is never the answer; the stored row that
    holds the same text says `done` true, and it is what the window shows afterwards.

    The stored row is read from the transcript again rather than kept from the poll
    that first saw it: the cursor counts rows, so the poll that saw the row while it
    was still open moved past it, and a later read of the window never carries it.
    The re-read starts at the row the question produced.

    The marker is remembered, not re-read: the cursor moves past the question on the
    poll that sees it, so a window that asks again would never see it, and a question
    answered one poll later would never be answered at all -- measured: the operator's
    row in the first window (`asked`, nothing said), the answer in the second
    (`said`, `asked` gone), and the loop to the deadline.
    """
    deadline = time.time() + timeout
    asked, said = False, []
    # Where the transcript stood before the question: the rows this ask is about are
    # the ones the question made, and the stored answer is the last of them.
    after = since
    while True:
        rows, since, status = _read(client, session, since)
        marker, texts = _answer_in(rows, session)
        if marker:
            asked, said = True, []
        said.extend(texts)
        if asked and said and status != "running":
            final = client.call(
                "get_messages", session=session, range=f"{after + 1}:", replay="true"
            )
            whole = [
                event["text"].strip()
                for event in (_rows(final) if final.ok else [])
                if event.get("t") == "response"
                and event.get("session") == session
                and event.get("part") == "output"
                and event.get("done") is True
                and isinstance(event.get("text"), str)
                and event["text"].strip()
            ]
            return whole[-1] if whole else said[-1]
        if time.time() >= deadline:
            return None
        time.sleep(poll)


def ask(session, question, timeout=TIMEOUT_SECONDS, poll=POLL_SECONDS):
    """The question delivered and the answer waited for; the answer, or a refusal.

    The cursor is taken before the question is sent, so the answer has to come
    after the row the question produced, not from a turn that ended before it.
    """
    session = (session or "").strip()
    question = (question or "").strip()
    if not session:
        _die("name the session to ask")
    if not question:
        _die("ask something: the question is the rest of the command")
    try:
        client = _client.connect()
    except _client.Unusable as unusable:
        _die(str(unusable))
    try:
        since = _tail(client, session)
        _deliver(client, session, question)
        answer = _wait(client, session, since, timeout, poll)
    finally:
        client.close()
    if answer is None:
        _die(f"{session} did not answer within {timeout:g}s")
    return answer


def _main():
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
    try:
        print(ask(args.session, args.question, args.timeout))
    except _Refused as refused:
        print(f"ask_session: {refused}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main())
