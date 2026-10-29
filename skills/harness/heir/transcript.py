#!/usr/bin/env python3
"""Read, fold, and hand over a gongga session's transcript, straight from the database.

Folding is a decision about work only the agent has seen, so the server does
not take it: it measures the context and says so (`Context notice`), and this
script is how the agent acts on it.

    transcript.py read     [--session S] [--after SEQ] [--limit N] [--preview]
    transcript.py collapse --session S --through SEQ --note TEXT
    transcript.py heir     --session S --mentor M --note TEXT

`collapse` is the conservative step: rows stay in the database and only stop
being sent. `heir` is the one that actually empties a session -- it copies the
whole transcript into another session (the mentor), verifies the copy, deletes
the original rows, and injects a note telling the agent where its past went and
how to reach it.

Reading returns what a conversation is made of -- the model's own messages and
anything injected into the session. Tool calls, tool results and reasoning are
left out: they are most of the rows and least of the meaning.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

DEFAULT_LIMIT = 40
PREVIEW_CHARS = 120
# The server writes are short but frequent; give a racing write time to clear
# instead of failing the one operation that must not fail halfway.
BUSY_TIMEOUT_MS = 5000


def die(message):
    print(message, file=sys.stderr)
    sys.exit(1)


def connect():
    # The server exports its home into the shell; a session running elsewhere
    # is already a mistake worth naming rather than guessing at.
    home = os.environ.get("GONGGA_HOME")
    if not home:
        die(
            "GONGGA_HOME is not set. This tool edits the running server's database, so it "
            "only works from a shell that server started; check with: "
            "echo \"$GONGGA_HOME\" (and $GONGGA_SERVER for the URL)"
        )
    path = os.path.join(home, "gongga.db")
    if not os.path.exists(path):
        die(f"no database at {path}")
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def session_id(conn, name):
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is None:
        known = [r["name"] for r in conn.execute("select name from sessions")]
        die(f"no session named {name!r}; this home has: {', '.join(known) or 'none'}")
    return row["id"]


def body(row):
    """The text of a stored message row, as `get_messages` renders it."""
    try:
        value = json.loads(row["content"])
    except ValueError:
        return ""
    return value.get("text") or value.get("handover") or ""


def cmd_read(args):
    conn = connect()
    sid = session_id(conn, args.session)
    note, through = conn.execute(
        "select handover, handover_through from sessions where id = ?", (sid,)
    ).fetchone()
    after = through if args.after is None else args.after

    rows = conn.execute(
        """select seq, kind, content from messages
            where session_id = ? and kind = 'text' and seq > ?
            order by seq""",
        (sid, after),
    ).fetchall()
    # Only the tail: the newest messages are where a cut gets chosen.
    rows = rows[-args.limit :] if args.limit else rows

    messages = []
    for row in rows:
        text = body(row)
        role = "user"
        try:
            role = json.loads(row["content"]).get("role") or "user"
        except ValueError:
            pass
        shown = text
        if args.preview:
            flat = text.replace("\n", " ").replace("\r", " ")
            shown = flat[:PREVIEW_CHARS]
            if len(flat) > PREVIEW_CHARS:
                shown += f"... ({len(text)} chars)"
        messages.append(
            {"seq": row["seq"], "role": role, "chars": len(text), "text": shown}
        )

    folded = conn.execute(
        "select count(*) from messages where session_id = ? and kind = 'text' and seq <= ?",
        (sid, through),
    ).fetchone()[0]
    live = conn.execute(
        "select count(*) from messages where session_id = ? and kind = 'text' and seq > ?",
        (sid, through),
    ).fetchone()[0]
    last = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (sid,)
    ).fetchone()[0]

    print(
        json.dumps(
            {
                "session": args.session,
                "handover": note,
                "handover_through": through,
                "last_seq": last,
                "folded_messages": folded,
                "live_messages": live,
                "shown_messages": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def first_carried(tail, cut):
    """The first row a request would actually carry after `cut`, as `(seq, kind)`.

    `notice` and `system` rows are skipped because the read path never sends them,
    so neither can be the start that decides whether a cut is legal.
    """
    for seq, kind in tail:
        if seq <= cut or kind in ("notice", "system"):
            continue
        return seq, kind
    return None


def newest_valid_cut(tail, requested, already):
    """The largest cut at or below `requested`, above `already`, that starts cleanly.

    This is the advice that matters: the newest legal cut is usually a call or a
    message a little below what was asked for, not the last text row before the
    boundary the note already covers.
    """
    if not tail:
        return None
    for candidate in range(min(requested, tail[-1][0]), already, -1):
        opening = first_carried(tail, candidate)
        if opening is None or opening[1] != "tool_result":
            return candidate
    return None


def cmd_collapse(args):
    if not args.note.strip():
        die(
            "refusing to fold with an empty note: that removes this stretch from every "
            "later request while the transcript still looks complete"
        )
    conn = connect()
    sid = session_id(conn, args.session)
    _, previous_through = conn.execute(
        "select handover, handover_through from sessions where id = ?", (sid,)
    ).fetchone()
    last = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (sid,)
    ).fetchone()[0]
    # `<=`, not `<`. A cut the note already covers folds nothing, and without this the
    # command still rewrites the note, zeroes the measured size and prints success --
    # which is how a session can fold six times, be told six times that it worked, and
    # stay over its budget the whole while.
    if args.through <= previous_through:
        die(
            f"--through {args.through} folds nothing: the note already covers through seq "
            f"{previous_through}, and the newest row is {last}. Fold above {previous_through}, "
            "or hand the whole transcript over with `heir`."
        )
    if args.through > last:
        die(f"--through {args.through} is past the last row ({last}); nothing folded")

    # The API rejects a function_call_output whose function_call is absent, so the
    # first row a request would carry after the cut has to be able to start one.
    tail = [(row["seq"], row["kind"]) for row in conn.execute(
        "select seq, kind from messages where session_id = ? and seq > ? order by seq",
        (sid, previous_through),
    ).fetchall()]
    opening = first_carried(tail, args.through)
    if opening is not None and opening[1] == "tool_result":
        suggested = newest_valid_cut(tail, args.through, previous_through)
        blocked = (
            f"--through {args.through} would leave a tool result (seq {opening[0]}) without the "
            "call that produced it, and the provider rejects that."
        )
        if suggested is None:
            die(
                f"{blocked} Nothing above the current note (through {previous_through}, newest row "
                f"{last}) can be cut without splitting a pair: fold nothing, or hand the whole "
                "transcript over with `heir`."
            )
        die(
            f"{blocked} Fold through {suggested} instead -- the newest cut at or below it that "
            f"starts a request cleanly, above the note's current {previous_through}."
        )

    folded = conn.execute(
        "select count(*) from messages where session_id = ? and seq <= ?", (sid, args.through)
    ).fetchone()[0]
    live = conn.execute(
        "select count(*) from messages where session_id = ? and seq > ?", (sid, args.through)
    ).fetchone()[0]

    with conn:
        conn.execute(
            "update sessions set handover = ?, handover_through = ? where id = ?",
            (args.note, args.through, sid),
        )
        # The provider's last measurement described the prefix just folded, so it
        # says nothing about the next request. Leaving it alone makes the server
        # think the context is still full and remind on every single step.
        conn.execute("update sessions set context_tokens = 0 where id = ?", (sid,))

    print(
        f"folded {folded} row(s) through seq {args.through} into a "
        f"{len(args.note)} character note; {live} row(s) still sent"
    )


def learner_message(session, mentor, kept):
    """What the emptied session reads back as the answer to its own command.

    No message is injected into the session any more: the call in flight survives
    the handover precisely so that its result can carry this, which is what the
    model was going to read anyway -- one row instead of a forged user turn.
    """
    return (
        f"Your transcript through the handover is in `{mentor}` now, and this session "
        f"`{session}` is emptied {'down to the call you are reading this from' if kept else 'to nothing'}. "
        "You are its heir: the handover note "
        "opening the next request is the only part of the past the server still carries "
        "for you, and everything else is readable, just not here.\n"
        f"  read it:  harness {{command: \"get_messages\", arguments: {{session: \"{mentor}\", range: \"1:\"}}}}\n"
        f"  ask it:   harness {{command: \"send_message\", arguments: {{session: \"{mentor}\", message: \"...\"}}}}\n"
        "Read before you ask: what was done and decided is in those rows, and no model "
        "can tell you better than the record itself. Ask the mentor when the question is "
        "a judgement -- was this the right call, what did the failed attempt mean -- "
        "because it holds the whole exchange and you hold the note of it.\n"
        "Nothing about the task changed. Continue from the note's next step."
    )


def mentor_message(learner):
    """The other half of the copied pair, and the mentor's notice of what it is."""
    return (
        f"You are the mentor of `{learner}`: its transcript through the handover was "
        "copied into this session row for row, and this row is the answer to its last "
        "call. Answer only what that session asks, by `get_messages` on this session or "
        "`send_message` to it; do not start work of your own here."
    )


def ensure_mentor(conn, name, source):
    """The mentor's id, creating the session with the learner's model and directory.

    A mentor is an ordinary session: the agent reads it and talks to it with the
    same commands every client has, which is the whole reason it is a session and
    not a file.
    """
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"], False
    now = int(time.time() * 1000)
    conn.execute(
        """insert into sessions (name, status, model, cwd, created_at, updated_at, llm)
            values (?, 'idle', ?, ?, ?, ?, ?)""",
        (name, source["model"], source["cwd"], now, now, source["llm"]),
    )
    return conn.execute("select id from sessions where name = ?", (name,)).fetchone()["id"], True


def cmd_heir(args):
    if not args.note.strip():
        die(
            "refusing to empty a session with an empty handover note: the copy in the "
            "mentor is the archive, but this note is what you will actually be sending"
        )
    if args.session == args.mentor:
        die(
            "the mentor has to be another session: copying a transcript into itself and "
            "then deleting it would delete the archive with the original"
        )
    conn = connect()
    sid = session_id(conn, args.session)
    source = conn.execute(
        "select model, cwd, llm from sessions where id = ?", (sid,)
    ).fetchone()
    rows = conn.execute(
        "select seq, kind, content, created_at from messages where session_id = ? order by seq",
        (sid,),
    ).fetchall()
    if not rows:
        die(f"{args.session} has no transcript to hand over; nothing was written")

    # The server runs a call the moment it streams, so the last row is this
    # command's own call and its result does not exist yet. That pair stays -- a
    # result without its call is a request no provider accepts -- and everything
    # before it is the past, which goes to the mentor.
    kept = rows[-1] if rows[-1]["kind"] == "tool_call" else None
    moved = rows[:-1] if kept is not None else rows
    through = moved[-1]["seq"] if moved else 0
    marker = (
        f"[heir] `{args.session}` handed its transcript over through seq {through} "
        f"({len(moved)} rows), keeping the call in flight. It may read this session "
        "with get_messages and ask it with send_message."
    )

    with conn:
        mid, created = ensure_mentor(conn, args.mentor, source)
        base = conn.execute(
            "select coalesce(max(seq), 0) from messages where session_id = ?", (mid,)
        ).fetchone()[0]
        now = int(time.time() * 1000)
        conn.execute(
            """insert into messages (session_id, seq, kind, content, created_at)
                values (?, ?, 'text', ?, ?)""",
            (mid, base + 1, json.dumps({"role": "user", "text": marker}), now),
        )
        for offset, row in enumerate(rows):
            conn.execute(
                """insert into messages (session_id, seq, kind, content, created_at)
                    values (?, ?, ?, ?, ?)""",
                (mid, base + 2 + offset, row["kind"], row["content"], row["created_at"]),
            )

        # The archive is checked before the original is touched: an heir that
        # deleted first would turn a half-written copy into lost work.
        copied = conn.execute(
            "select kind, content from messages where session_id = ? and seq > ? order by seq",
            (mid, base + 1),
        ).fetchall()
        if len(copied) != len(rows):
            die(f"the copy has {len(copied)} row(s) and the original has {len(rows)}")
        for got, original in zip(copied, rows):
            if (got["kind"], got["content"]) != (original["kind"], original["content"]):
                die(
                    f"the copy differs at seq {original['seq']}; nothing was deleted "
                    f"(mentor {args.mentor} holds {len(copied)} copied row(s))"
                )

        # The copied call is a question with no answer, in a session that will now
        # be read back as a transcript. Give it its result, and tell the mentor
        # what it has become: this is the row that says so.
        if kept is not None:
            conn.execute(
                """insert into messages (session_id, seq, kind, content, created_at)
                    values (?, ?, 'tool_result', ?, ?)""",
                (
                    mid,
                    base + 2 + len(rows),
                    json.dumps(
                        {
                            "call_id": json.loads(kept["content"])["call_id"],
                            "output": mentor_message(args.session),
                            "images": [],
                        }
                    ),
                    now,
                ),
            )

        if kept is None:
            conn.execute("delete from messages where session_id = ?", (sid,))
        else:
            conn.execute("delete from messages where session_id = ? and seq < ?", (sid, kept["seq"]))
        # With the pair kept, the note covers exactly what was folded away and the
        # surviving seqs are above it. With nothing kept, `seq` counts up from the
        # rows that exist -- so it restarts at 1, and a note claiming it covers
        # through the old last seq would fold every row this session ever sends.
        # context_tokens is the count of a prefix that is gone either way.
        conn.execute(
            """update sessions
                  set handover = ?, handover_through = ?, context_tokens = 0, updated_at = ?
                where id = ?""",
            (args.note, through if kept is not None else 0, now, sid),
        )

    print(
        json.dumps(
            {
                "session": args.session,
                "mentor": args.mentor,
                "mentor_created": created,
                "rows_copied": len(rows),
                "through_seq": through,
                "kept_call": json.loads(kept["content"])["call_id"] if kept is not None else None,
                "mentor_rows": len(copied) + 1 + (1 if kept is not None else 0),
                "deleted": len(moved),
                "handover_chars": len(args.note),
                "you": learner_message(args.session, args.mentor, kept is not None),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="show the messages that are actually sent")
    read.add_argument("--session", required=True)
    read.add_argument("--after", type=int, help="defaults to the end of the note; 0 reaches folded history")
    read.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    read.add_argument("--preview", action="store_true", help="one line per message")
    read.set_defaults(func=cmd_read)

    collapse = sub.add_parser("collapse", help="fold everything through --through into --note")
    collapse.add_argument("--session", required=True)
    collapse.add_argument("--through", type=int, required=True)
    collapse.add_argument("--note", required=True)
    collapse.set_defaults(func=cmd_collapse)

    heir = sub.add_parser(
        "heir",
        help="copy the whole transcript into --mentor, verify it, empty this session down to this call",
    )
    heir.add_argument("--session", required=True)
    heir.add_argument("--mentor", required=True, help="any session name; created if it does not exist")
    heir.add_argument("--note", required=True, help="the handover note this session wakes up to")
    heir.set_defaults(func=cmd_heir)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
