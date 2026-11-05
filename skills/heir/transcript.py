#!/usr/bin/env python3
"""Read a session and start training an heir from its transcript.

Training an heir is a decision about work only the agent has seen, so the server
does not take it: it measures the context and says so (`Context notice`), and
this script is how the agent starts immediately.

    transcript.py read     [--after SEQ] [--limit N] [--preview]
    transcript.py heir     [--mentor M] --note TEXT

Both read the caller's own session from `GONGGA_SESSION`, which the server exports
to every shell it starts. There is no `--session`: a caller that may name any
session is a caller that can empty someone else's transcript, and no check on the
name is as strong as not having the parameter.

`heir` empties the current session so it continues as the heir: it copies the
whole transcript into a mentor session, verifies the copy, deletes the original
rows, and answers the call in flight with the heir's next instructions. The
heir continues the current work from its first lesson; no clean state is needed.

The lesson is a message: it goes to the session with `send_message`, exactly as
an operator's would, so the server reads it as a first user message -- its
references become index roots, and nothing has to teach the server what a lesson
is.

Reading returns what a conversation is made of -- the model's own messages and
anything injected into the session. Tool calls, tool results and reasoning are
left out: they are most of the rows and least of the meaning.
"""

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time

DEFAULT_LIMIT = 40
PREVIEW_CHARS = 120
# The server writes are short but frequent; give a racing write time to clear
# instead of failing the one operation that must not fail halfway.
BUSY_TIMEOUT_MS = 5000
# The command layer, exported by the server to every shell it starts.
COMMAND_VARIABLE = "GONGGA_CLI"


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
    return value.get("text") or ""


def cmd_read(args):
    conn = connect()
    sid = session_id(conn, args.session)
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
                "last_seq": last,
                "live_messages": live,
                "shown_messages": len(messages),
                "messages": messages,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_layer():
    """This server's own command, as the shell it started was given it."""
    return os.environ.get(COMMAND_VARIABLE)


def command_result(stdout):
    """The terminal outcome of the one command the CLI ran, as its events report it."""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("t") == "cmd_result":
            return event
    return None


def send_lesson(session, lesson):
    """Puts the heir's first lesson in the session as an ordinary message.

    # Invariant

    A failure here never loses the lesson: it is printed, which is what the call's
    result carries into the heir's own transcript -- the session is already empty,
    so that result is the only copy left.

    Returns what the command said it did, because whether a message is delivered now
    or at the next step boundary is the server's answer, not this script's.
    """
    cli = command_layer()
    detail = None
    if not cli:
        detail = (
            f"{COMMAND_VARIABLE} is not set: only a shell the server started has it "
            f'(check with: echo "${COMMAND_VARIABLE}")'
        )
    else:
        try:
            sent = subprocess.run(
                [cli, "send_message", "--name", session, lesson],
                capture_output=True,
                text=True,
            )
        except OSError as e:
            sent = None
            detail = str(e)
        if sent is not None:
            outcome = command_result(sent.stdout)
            if sent.returncode == 0 and outcome is not None and outcome.get("ok"):
                return outcome.get("message") or ""
            detail = (
                (outcome or {}).get("message")
                or (sent.stderr or "").strip()
                or f"the command exited {sent.returncode}"
            )
    die(
        f"the transcript was copied to a mentor and emptied, but the lesson could not be "
        f"sent to {session!r} ({detail}). It is repeated here because this result is now "
        f"the only place it exists; continue from it, and from now on carry it as your "
        f"own message:\n\n{lesson}"
    )


def learner_message(session, mentor, kept):
    """What the heir reads as the answer to the command that started it."""
    return (
        f"You are now the heir in `{session}`, and `{mentor}` holds the complete transcript. "
        f"This session is empty {'down to the call you are reading this from' if kept else 'for your next request'}. "
        "Your first lesson is the message that follows this one: it carries the current objective, "
        "state, failed attempts, and next action. Everything else remains readable in the mentor.\n"
        f"  read it:  gongga-cli get_messages --session {mentor} 1:\n"
        f"  ask it:   gongga-cli skill self-control {mentor} \"<your question>\"\n"
        "Ask before you redo. The mentor holds the earlier attempts, the failures and why they "
        "failed, so one question costs one turn where a repeated search costs the whole search "
        "again. The command waits for the answer and prints it; the mentor also sends it to "
        "this session, so keep working and read it when it lands.\n"
        "Nothing about the task changed and no clean state is required: continue the current work "
        "from the lesson's next action."
    )


def mentor_message(learner):
    """The mentor's duty while the heir continues the current work."""
    return (
        f"You are the mentor of `{learner}`: its complete transcript was copied into "
        "this session row for row, and this row answers its last call. The heir is "
        "continuing the current work there. Answer only what it asks, and answer it in one "
        f"message: read its questions with `gongga-cli get_messages --session {learner} 1:`, "
        f"reply with `gongga-cli send_message --message \"<your answer>\" --name {learner}`. "
        "Put the whole answer in your own turn as well: that turn is what the heir's "
        "`ask_session` reads, and a message alone leaves that reader with your receipt. "
        "Do not start work of your own here."
    )


def established(conn, name):
    """Whether `name` is a session that already holds rows.

    A mentor an earlier handover used is an archive, not a scratch pad: training
    another heir into it would put two archives in one transcript, and the heir
    reads that transcript from its first row.
    """
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is None:
        return False
    count = conn.execute(
        "select count(*) from messages where session_id = ?", (row["id"],)
    ).fetchone()[0]
    return count > 0


def free_name(conn, wanted):
    """`wanted` itself when it holds no rows, else `wanted/2`, `wanted/3`, ...

    A segment is added rather than a suffix, because a name is a path: the archive
    of the second handover belongs under the first one's name, where the tree in
    the session list already shows it.
    """
    if not established(conn, wanted):
        return wanted
    n = 2
    while established(conn, f"{wanted}/{n}"):
        n += 1
    return f"{wanted}/{n}"


def ensure_mentor(conn, name, source):
    """The mentor's id, creating the session with the learner's model and directory.

    A mentor is an ordinary session: the agent reads it and talks to it with the
    same commands every client has, which is the whole reason it is a session and
    not a file. `name` is already the free one -- see `free_name`.
    """
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"], False
    now = int(time.time() * 1000)
    # `model_name` is the bare name and is NOT NULL; `model` is the pair. A row
    # written without the first failed the constraint outright.
    bare = str(source["model"]).split("/")[-1]
    conn.execute(
        """insert into sessions (name, status, model_name, model, cwd, created_at, updated_at)
            values (?, 'idle', ?, ?, ?, ?, ?)""",
        (name, bare, source["model"], source["cwd"], now, now),
    )
    return conn.execute("select id from sessions where name = ?", (name,)).fetchone()["id"], True


def cmd_heir(args):
    if not args.note.strip():
        die(
            "refusing to train an heir with an empty lesson: the mentor holds the archive, "
            "but this lesson is what the heir actually sends"
        )
    conn = connect()
    # The mentor a handover makes room for: the asked-for name when nothing holds it,
    # otherwise the next one under it. Never the session being emptied -- `--mentor`
    # is held under this session's own path, which cannot be the session itself --
    # and never an archive an earlier handover left behind.
    mentor = free_name(conn, args.mentor)
    sid = session_id(conn, args.session)
    source = conn.execute(
        "select model, cwd, context_tokens, context_limit from sessions where id = ?",
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
        mid, created = ensure_mentor(conn, mentor, source)
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
                    f"(mentor {mentor} holds {len(copied)} copied row(s))"
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
            "update sessions set context_tokens = 0, updated_at = ? where id = ?",
            (now, sid),
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
                            "mentor": mentor,
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

    # The session is empty now, so this is the first user message it will carry.
    # Sent from here rather than written into the database: a row inserted between
    # the call in flight and its result is a transcript no provider accepts, and
    # `send_message` is what puts a lesson where an operator's message would go.
    delivery = send_lesson(args.session, args.note)

    print(
        json.dumps(
            {
                "session": args.session,
                "mentor": mentor,
                "mentor_created": created,
                "rows_copied": len(rows),
                "through_seq": through,
                "kept_call": json.loads(kept["content"])["call_id"] if kept is not None else None,
                "mentor_rows": len(copied) + 1 + (1 if kept is not None else 0),
                "deleted": len(moved),
                "lesson_chars": len(args.note),
                "lesson_delivery": delivery,
                "you": learner_message(args.session, mentor, kept is not None),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    read = sub.add_parser("read", help="show the messages that are actually sent")
    read.add_argument("--after", type=int, help="defaults to the beginning of the transcript")
    read.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    read.add_argument("--preview", action="store_true", help="one line per message")
    read.set_defaults(func=cmd_read)

    heir = sub.add_parser(
        "heir",
        help="start training an heir: save the transcript with --mentor, verify it, and empty this session",
    )
    heir.add_argument(
        "--mentor",
        help="where the archive goes: `<your session>/mentor` when omitted. A name that "
        "already holds a transcript is not reused -- `2`, `3`, ... is added under it -- "
        "so one archive never becomes two.",
    )
    heir.add_argument("--note", required=True, help="the heir's first lesson: objective, state, failures and next action")
    heir.set_defaults(func=cmd_heir)

    args = parser.parse_args()
    # The caller is the session whose shell this is, and the server says so: a name
    # typed here would be a claim, and the one command that must never be able to
    # empty a transcript has no business accepting claims about whose it is.
    args.session = os.environ.get("GONGGA_SESSION", "")
    if not args.session:
        die(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )
    if getattr(args, "mentor", None) is None:
        # A segment, not a prefix: the archive hangs under the session it came from,
        # where the session list already draws it as a child.
        args.mentor = f"{args.session}/mentor"
    else:
        # An archive is written where the caller can see it and nowhere else. The
        # rule is structural -- under this session's path -- so it needs no list of
        # reserved names: `@monitor` training an heir gets `@monitor/mentor`, which
        # is its own child, and nobody can make it write outside its own subtree.
        under = f"{args.session}/"
        if not args.mentor.startswith(under) or args.mentor == under:
            die(
                f"--mentor {args.mentor!r} is not under {args.session!r}: an archive is "
                f"a child of the session that made it, e.g. {args.session}/mentor. "
                "Writing one anywhere else would put this transcript in a session "
                "nobody asked for."
            )
    args.func(args)


if __name__ == "__main__":
    main()
