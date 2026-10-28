#!/usr/bin/env python3
"""Read, fold, and hand over a gongga session's transcript, straight from the database.

Compaction is a decision about work only the agent has seen, so the server does
not take it: it measures the context and says so (`Context notice`), and this
script is how the agent acts on it.

    transcript.py read     [--session S] [--after SEQ] [--limit N] [--preview]
    transcript.py collapse --session S --upto SEQ --summary TEXT
    transcript.py heir     --session S --mentor M --summary TEXT [--guide TEXT]

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
    return value.get("text") or value.get("summary") or ""


def cmd_read(args):
    conn = connect()
    sid = session_id(conn, args.session)
    summary, upto = conn.execute(
        "select summary, summarised_upto from sessions where id = ?", (sid,)
    ).fetchone()
    after = upto if args.after is None else args.after

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
        (sid, upto),
    ).fetchone()[0]
    live = conn.execute(
        "select count(*) from messages where session_id = ? and kind = 'text' and seq > ?",
        (sid, upto),
    ).fetchone()[0]
    last = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (sid,)
    ).fetchone()[0]

    print(
        json.dumps(
            {
                "session": args.session,
                "summary": summary,
                "summary_upto": upto,
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


def cmd_collapse(args):
    if not args.summary.strip():
        die(
            "refusing to fold with an empty summary: that removes this stretch from every "
            "later request while the transcript still looks complete"
        )
    conn = connect()
    sid = session_id(conn, args.session)
    _, previous_upto = conn.execute(
        "select summary, summarised_upto from sessions where id = ?", (sid,)
    ).fetchone()
    if args.upto < previous_upto:
        die(
            f"--upto {args.upto} is behind the current summary (which covers through "
            f"{previous_upto}); folding backwards would put already-folded rows back into "
            "every request"
        )
    last = conn.execute(
        "select coalesce(max(seq), 0) from messages where session_id = ?", (sid,)
    ).fetchone()[0]
    if args.upto > last:
        die(f"--upto {args.upto} is past the last row ({last}); nothing folded")

    # The API rejects a function_call_output whose function_call is absent, so the
    # first row that stays live must be able to start a request on its own.
    first_live = conn.execute(
        """select seq, kind from messages
            where session_id = ? and seq > ? order by seq limit 1""",
        (sid, args.upto),
    ).fetchone()
    if first_live is not None and first_live["kind"] == "tool_result":
        safe = conn.execute(
            """select seq from messages
                where session_id = ? and seq < ? and kind = 'text'
                order by seq desc limit 1""",
            (sid, first_live["seq"]),
        ).fetchone()
        where = f"through {safe['seq']}" if safe else "a message before it"
        die(
            f"--upto {args.upto} would leave a tool result (seq {first_live['seq']}) without "
            f"the call that produced it, and the provider rejects that. Fold {where} instead."
        )

    folded = conn.execute(
        "select count(*) from messages where session_id = ? and seq <= ?", (sid, args.upto)
    ).fetchone()[0]
    live = conn.execute(
        "select count(*) from messages where session_id = ? and seq > ?", (sid, args.upto)
    ).fetchone()[0]

    with conn:
        conn.execute(
            "update sessions set summary = ?, summarised_upto = ? where id = ?",
            (args.summary, args.upto, sid),
        )
        # The provider's last measurement described the prefix just folded, so it
        # says nothing about the next request. Leaving it alone makes the server
        # think the context is still full and remind on every single step.
        conn.execute("update sessions set context_tokens = 0 where id = ?", (sid,))

    print(
        f"folded {folded} row(s) through seq {args.upto} into a "
        f"{len(args.summary)} character summary; {live} row(s) still sent"
    )


def default_guide(session, mentor):
    """The note a cleared session wakes up to: what is gone, and where it is."""
    return (
        f"Your transcript was handed over to the session `{mentor}` -- all of it, "
        f"rows and tool output included -- and this session `{session}` was emptied.\n"
        "The handover note opening this request is the only part of the past the "
        "server still carries for you. Everything else is readable, just not here:\n"
        f"  read it:  harness {{command: \"get_messages\", arguments: {{session: \"{mentor}\", range: \"1:\"}}}}\n"
        f"  ask it:   harness {{command: \"send_message\", arguments: {{session: \"{mentor}\", message: \"...\"}}}}\n"
        "Read before you ask: what you did and what you decided is in those rows, and "
        "no model can tell you better than the record itself. Ask the mentor when the "
        "question is a judgement -- was this the right call, what did the failed attempt "
        "mean -- because it has the whole exchange and you have the summary of it.\n"
        "Nothing about the task changed. Continue from the handover note's next step."
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
    if not args.summary.strip():
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

    through = rows[-1]["seq"]
    guide = args.guide or default_guide(args.session, args.mentor)
    marker = (
        f"[heir] `{args.session}` handed its transcript over through seq {through} "
        f"({len(rows)} rows) and was emptied. It may read this session with get_messages "
        "and ask it with send_message."
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

        conn.execute("delete from messages where session_id = ?", (sid,))
        # Seq counts up from the rows that exist, so an emptied session starts again at
        # 1 and the summary must cover none of it -- otherwise the first new row would
        # look already folded and never reach the model.
        conn.execute(
            """update sessions
                  set summary = ?, summarised_upto = 0, context_tokens = 0, updated_at = ?
                where id = ?""",
            (args.summary, now, sid),
        )
        conn.execute(
            """insert into messages (session_id, seq, kind, content, created_at)
                values (?, 1, 'text', ?, ?)""",
            (sid, json.dumps({"role": "user", "text": guide}), now),
        )

    print(
        json.dumps(
            {
                "session": args.session,
                "mentor": args.mentor,
                "mentor_created": created,
                "rows_copied": len(rows),
                "through_seq": through,
                "mentor_rows": len(copied) + 1,
                "deleted": len(rows),
                "handover_chars": len(args.summary),
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
    read.add_argument("--after", type=int, help="defaults to the end of the summary; 0 reaches folded history")
    read.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    read.add_argument("--preview", action="store_true", help="one line per message")
    read.set_defaults(func=cmd_read)

    collapse = sub.add_parser("collapse", help="fold everything through --upto into --summary")
    collapse.add_argument("--session", required=True)
    collapse.add_argument("--upto", type=int, required=True)
    collapse.add_argument("--summary", required=True)
    collapse.set_defaults(func=cmd_collapse)

    heir = sub.add_parser(
        "heir",
        help="copy the whole transcript into --mentor, verify it, empty this session, inject the guide",
    )
    heir.add_argument("--session", required=True)
    heir.add_argument("--mentor", required=True, help="any session name; created if it does not exist")
    heir.add_argument("--summary", required=True, help="the handover note this session wakes up to")
    heir.add_argument("--guide", help="replaces the injected note; default names --mentor and the two commands")
    heir.set_defaults(func=cmd_heir)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
