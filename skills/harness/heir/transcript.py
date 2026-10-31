#!/usr/bin/env python3
"""Read a session and start training an heir from its transcript.

Training an heir is a decision about work only the agent has seen, so the server
does not take it: it measures the context and says so (`Context notice`), and
this script is how the agent starts immediately.

    transcript.py read     [--session S] [--after SEQ] [--limit N] [--preview]
    transcript.py heir     --session S --mentor M --note TEXT

`heir` empties the current session so it continues as the heir: it copies the
whole transcript into a mentor session, verifies the copy, deletes the original
rows, and answers the call in flight with the heir's next instructions. The
heir continues the current work from its first lesson; no clean state is needed.

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
        value = json.loads(row["display"])
    except ValueError:
        return ""
    return value.get("text") or value.get("handover") or ""


def cmd_read(args):
    conn = connect()
    sid = session_id(conn, args.session)
    note = conn.execute(
        "select handover from sessions where id = ?", (sid,)
    ).fetchone()[0]
    after = 0 if args.after is None else args.after

    rows = conn.execute(
        """select seq, kind, display from messages
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
            role = json.loads(row["display"]).get("role") or "user"
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

    live = conn.execute(
        "select count(*) from messages where session_id = ? and kind = 'text' and seq > ?",
        (sid, after),
    ).fetchone()[0]
    last = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (sid,)
    ).fetchone()[0]

    print(
        json.dumps(
            {
                "session": args.session,
                "handover": note,
                "last_seq": last,
                "live_messages": live,
                "shown_messages": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def learner_message(session, mentor, kept):
    """What the heir reads as the answer to the command that started it."""
    return (
        f"You are now the heir in `{session}`, and `{mentor}` holds the complete transcript. "
        f"This session is empty {'down to the call you are reading this from' if kept else 'for your next request'}. "
        "Your first lesson is the note opening that request: it carries the current objective, "
        "state, failed attempts, and next action. Everything else remains readable in the mentor.\n"
        f"  read it:  harness {{command: \"get_messages\", arguments: {{session: \"{mentor}\", range: \"1:\"}}}}\n"
        f"  ask it:   harness {{command: \"send_message\", arguments: {{session: \"{mentor}\", message: \"...\"}}}}\n"
        "Read before you ask. Ask the mentor only when you need a judgement about the earlier work. "
        "Nothing about the task changed and no clean state is required: continue the current work "
        "from the lesson's next action."
    )


def mentor_message(learner):
    """The mentor's duty while the heir continues the current work."""
    return (
        f"You are the mentor of `{learner}`: its complete transcript was copied into "
        "this session row for row, and this row answers its last call. The heir is "
        "continuing the current work there. Answer only what it asks, by `get_messages` "
        "on this session or `send_message` to it; do not start work of your own here."
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
        """insert into sessions (name, status, model, cwd, created_at, updated_at, provider)
            values (?, 'idle', ?, ?, ?, ?, ?)""",
        (name, source["model"], source["cwd"], now, now, source["provider"]),
    )
    return conn.execute("select id from sessions where name = ?", (name,)).fetchone()["id"], True


def cmd_heir(args):
    if not args.note.strip():
        die(
            "refusing to train an heir with an empty lesson: the mentor holds the archive, "
            "but this lesson is what the heir actually sends"
        )
    if args.session == args.mentor:
        die(
            "the mentor has to be another session: copying a transcript into itself and "
            "then deleting it would delete the archive with the original"
        )
    conn = connect()
    sid = session_id(conn, args.session)
    source = conn.execute(
        "select model, cwd, provider, context_tokens, context_limit from sessions where id = ?",
        (sid,),
    ).fetchone()
    rows = conn.execute(
        """select seq, kind, content, display, call_id, created_at
            from messages where session_id = ? order by seq""",
        (sid,),
    ).fetchall()
    if not rows:
        die(f"{args.session} has no transcript for the heir's mentor; nothing was written")

    # The server runs a call the moment it streams, so the last row is this
    # command's own call and its result does not exist yet. That pair stays -- a
    # result without its call is a request no provider accepts -- and everything
    # before it is the past, which goes to the mentor.
    kept = rows[-1] if rows[-1]["kind"] == "tool_call" else None
    kept_call_id = json.loads(kept["content"])["call_id"] if kept is not None else None
    moved = rows[:-1] if kept is not None else rows
    through = moved[-1]["seq"] if moved else 0
    marker = (
        f"[heir] `{args.session}` started training an heir through seq {through} "
        f"({len(moved)} rows), keeping the call in flight. The heir may read this mentor "
        "with get_messages and ask it with send_message."
    )

    with conn:
        mid, created = ensure_mentor(conn, args.mentor, source)
        base = conn.execute(
            "select coalesce(max(seq), 0) from messages where session_id = ?", (mid,)
        ).fetchone()[0]
        now = int(time.time() * 1000)
        conn.execute(
            """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                values (?, ?, 'text', ?, ?, NULL, ?)""",
            (
                mid,
                base + 1,
                json.dumps({"role": "user", "content": marker}),
                json.dumps({"role": "user", "text": marker}),
                now,
            ),
        )
        for offset, row in enumerate(rows):
            conn.execute(
                """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                    values (?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    base + 2 + offset,
                    row["kind"],
                    row["content"],
                    row["display"],
                    row["call_id"],
                    row["created_at"],
                ),
            )

        # The archive is checked before the original is touched: an heir that
        # deleted first would turn a half-written copy into lost work.
        copied = conn.execute(
            """select kind, content, display, call_id from messages
                where session_id = ? and seq > ? order by seq""",
            (mid, base + 1),
        ).fetchall()
        if len(copied) != len(rows):
            die(f"the copy has {len(copied)} row(s) and the original has {len(rows)}")
        for got, original in zip(copied, rows):
            if (got["kind"], got["content"], got["display"], got["call_id"]) != (
                original["kind"],
                original["content"],
                original["display"],
                original["call_id"],
            ):
                die(
                    f"the copy differs at seq {original['seq']}; nothing was deleted "
                    f"(mentor {args.mentor} holds {len(copied)} copied row(s))"
                )

        # The copied call is a question with no answer, in a session that will now
        # be read back as a transcript. Give it its result, and tell the mentor
        # what it has become: this is the row that says so.
        if kept is not None:
            call_id = json.loads(kept["display"])["call_id"]
            output = mentor_message(args.session)
            conn.execute(
                """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                    values (?, ?, 'tool_result', ?, ?, ?, ?)""",
                (
                    mid,
                    base + 2 + len(rows),
                    json.dumps(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output,
                        }
                    ),
                    json.dumps({"call_id": call_id, "images": [], "output": output}),
                    call_id,
                    now,
                ),
            )

        if kept is None:
            conn.execute("delete from messages where session_id = ?", (sid,))
        else:
            conn.execute("delete from messages where session_id = ? and seq < ?", (sid, kept["seq"]))
        # context_tokens counted a transcript that no longer exists: leaving it
        # would have the server measure this session as still full and remind on
        # every single step.
        conn.execute(
            """update sessions
                  set handover = ?, context_tokens = 0, updated_at = ?
                where id = ?""",
            (args.note, now, sid),
        )
        # What this handover did, in the table the server keeps its own events in,
        # so "how many times did this session compact" is a count of rows rather
        # than an inference from two columns and a mentor's name. Same
        # transaction as the action, so a failed handover cannot leave a row
        # claiming it happened; read by nothing that decides what is sent, so a
        # wrong row is a wrong report, not a wrong turn.
        try:
            conn.execute(
                """create table if not exists events (
                    id         INTEGER PRIMARY KEY,
                    session_id INTEGER,
                    seq        INTEGER NOT NULL,
                    kind       TEXT NOT NULL,
                    payload    TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            conn.execute(
                """insert into events (session_id, seq, kind, payload, created_at)
                    values (?, (select coalesce(max(seq), 0) + 1 from events), 'handover', ?, ?)""",
                (
                sid,
                    json.dumps(
                        {
                            "mechanism": "heir",
                            "mentor": args.mentor,
                            "mentor_created": created,
                            "rows": len(rows),
                            "rows_moved": len(moved),
                            "through_seq": through,
                            "kept_call": kept_call_id,
                            "note_chars": len(args.note),
                            "context_tokens": source["context_tokens"],
                            "context_limit": source["context_limit"],
                        }
                    ),
                    now,
                ),
            )
        except sqlite3.Error as e:
            # The handover is the work; the row is the record of it. A home too
            # old to have the table, or a write that fails, must not undo the
            # copy -- and it must not be silent either.
            print(
                f"warning: heir training succeeded but was not recorded ({e})",
                file=sys.stderr,
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
    read.add_argument("--after", type=int, help="defaults to the beginning of the transcript")
    read.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    read.add_argument("--preview", action="store_true", help="one line per message")
    read.set_defaults(func=cmd_read)

    heir = sub.add_parser(
        "heir",
        help="start training an heir: save the transcript with --mentor, verify it, and empty this session",
    )
    heir.add_argument("--session", required=True)
    heir.add_argument("--mentor", required=True, help="any session name; created if it does not exist")
    heir.add_argument("--note", required=True, help="the heir's first lesson: objective, state, failures and next action")
    heir.set_defaults(func=cmd_heir)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
