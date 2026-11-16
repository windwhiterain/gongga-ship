#!/usr/bin/env python3
"""The waiter an `ask` leaves in a channel: read the answer, hand it over.

    python answer.py <mentor> <first row> <caller> <channel>

The subscription an `ask` arms types that line into the channel when the mentor's turn
ends, so this runs in a process of the session's own and while no turn is running: it
reads the mentor's transcript from the row the question made, sends what it found to the
caller with `send_message`, and then forgets the channel it ran in.

Why a channel and not the asking call: waiting is not a command's business, because a
command that blocks holds the turn it was asked in. SPEC gives waiting to `exec`'s
channel, so the wait lives here, in a program, and the subscription is what starts it.

Exit: 0 the answer (or its absence) reached the caller; 2 nothing did, and why is on stderr.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _client

#: The spelling of "this is the whole transcript", as `get_messages` takes it.
ALL_FROM = "{}:"


def _refuse(message):
    print(message, file=sys.stderr)
    return 2


def _unwrapped(message):
    """The event one message carries, from the stream or from a replay.

    `gongga.py` keeps what it read as it read it: the stream wraps an event as `{"t":
    "event", "v": {"seq": n, "event": {...}}}`, and a replay wraps a stored row holding
    that same event as `{"t": "row", "v": {...}}`. Both carry one event.
    """
    if message.get("t") in ("event", "row"):
        payload = message.get("v")
        if isinstance(payload, dict) and isinstance(payload.get("event"), dict):
            return payload["event"]
    return message


def _rows(reply):
    """One reply's events, each with its envelope off."""
    return [_unwrapped(message) for message in reply.events if isinstance(message, dict)]


def _answered(rows, mentor):
    """The last thing the mentor said, out of the rows one question made.

    The stored row is the one `done` is true on: the pieces a stream sends while a turn
    writes are the same text half-written, and the transcript keeps the whole of it.
    """
    said = [
        row["text"].strip()
        for row in rows
        if row.get("t") == "response"
        and row.get("session") == mentor
        and row.get("part") == "output"
        and row.get("done") is True
        and isinstance(row.get("text"), str)
        and row["text"].strip()
    ]
    return said[-1] if said else None


def _forget(client, channel, caller):
    """Ends the channel this ran in: a waiter is one question's scratch program.

    Best effort and last: the call ends the program that is making it, so no answer is
    waited for. A channel left behind is still nameable, which is what makes this safe.
    """
    try:
        client.call("del_channel", id=channel, session=caller)
    except Exception:  # noqa: BLE001 - the program is being ended by this very call
        pass


def main(argv):
    if len(argv) != 5:
        return _refuse("answer.py takes <mentor> <first row> <caller> <channel>")
    mentor, first, caller, channel = argv[1], argv[2], argv[3], argv[4]
    try:
        client = _client.connect()
    except _client.Unusable as unusable:
        return _refuse(str(unusable))
    try:
        reply = client.call(
            "get_messages", session=mentor, range=ALL_FROM.format(first), replay="true"
        )
        if not reply.ok:
            return _refuse(reply.message or f"cannot read the transcript of {mentor}")
        answer = _answered(_rows(reply), mentor)
        if answer is None:
            body = (
                f"{mentor}'s turn ended, and nothing from row {first} on reads as an answer "
                f"yet: the question may have landed in a later turn. Read the transcript "
                f"yourself: get_messages --session {mentor} --range {first}:"
            )
        else:
            body = f"{mentor} answered (asked through self-control `ask`):\n\n{answer}"
        sent = client.call("send_message", session=caller, message=body)
        if not sent.ok:
            return _refuse(sent.message or f"the answer did not reach {caller}")
    finally:
        try:
            _forget(client, channel, caller)
        finally:
            client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
