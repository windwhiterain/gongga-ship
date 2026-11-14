"""Read a session and hand off its transcript.

Handing off is a decision about work only the agent has seen, so the server does
not take it: it measures the context and says so (`Context notice`), and these
functions are how the agent hands off once its materials are in order.

    read(after=None, limit=40, preview=False)   # the messages that are actually sent
    handoff(note, mentor=None)                  # archive, verify, cut, and continue

Both read the caller's own session from `GONGGA_SESSION`, which the server exports
to every shell it starts. There is no `session` parameter: a caller that may name
any session is a caller that can empty someone else's transcript, and no check on
the name is as strong as not having the parameter.

`handoff` empties the current session so it continues with a fresh context: it
copies the whole transcript into a mentor session, verifies the copy, cuts the
original rows from the tail (context only has append/cut), and answers the call
in flight with the continuation's next instructions. The work continues from the
handoff note; no clean state is needed.

The note is a message: it goes to the session with `send_message`, exactly as an
operator's would, so the server reads it as a first user message -- its references
become index roots, and nothing has to teach the server what a note is.

Reading returns what a conversation is made of -- the model's own messages and
anything injected into the session. Tool calls, tool results and reasoning are
left out: they are most of the rows and least of the meaning.

Every top-level name that is not one of the two above carries a leading `_`, because
the package runner publishes every callable that does not (see
`python/gongga_skill.py::functions`).
"""

import json as _json
import os as _os
import sqlite3 as _sqlite3
import subprocess as _subprocess
import sys as _sys
import time as _time

DEFAULT_LIMIT = 40
PREVIEW_CHARS = 120
# The server writes are short but frequent; give a racing write time to clear
# instead of failing the one operation that must not fail halfway.
BUSY_TIMEOUT_MS = 5000
# The command layer, exported by the server to every shell it starts.
COMMAND_VARIABLE = "GONGGA_CLI"


def _die(message):
    """A refusal, in the shape a caller can catch and a package runner can print."""
    raise RuntimeError(message)


def _caller():
    """The session whose shell this is, or a refusal that names the missing export."""
    name = (_os.environ.get("GONGGA_SESSION") or "").strip()
    if not name:
        _die(
            "GONGGA_SESSION is not set: only a shell the server started knows which "
            'session it belongs to (check with: echo "$GONGGA_SESSION")'
        )
    return name


def _connect():
    """The home's database, read-write, with the row factory `session` and `read` use.

    # Invariant

    The server exports its home into the shell; a session running elsewhere is
    already a mistake worth naming rather than guessing at.
    """
    home = _os.environ.get("GONGGA_HOME")
    if not home:
        _die(
            "GONGGA_HOME is not set. This edits the running server's database, so it "
            "only works from a shell that server started; check with: "
            'echo "$GONGGA_HOME" (and $GONGGA_SERVER for the URL)'
        )
    path = _os.path.join(home, "gongga.db")
    if not _os.path.exists(path):
        _die(f"no database at {path}")
    conn = _sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = _sqlite3.Row
    conn.execute(f"pragma busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _session_id(conn, name):
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is None:
        known = [r["name"] for r in conn.execute("select name from sessions")]
        _die(f"no session named {name!r}; this home has: {', '.join(known) or 'none'}")
    return row["id"]


def _body(row):
    """The text of a stored message row, as `get_messages` renders it."""
    try:
        value = _json.loads(row["display"])
    except ValueError:
        return ""
    return value.get("text") or ""


def read(after=None, limit=DEFAULT_LIMIT, preview=False):
    """The messages this session actually sends, newest `limit` of them, as JSON text.

    `after` is a sequence number: only rows past it are read, and nothing is the
    beginning of the transcript. `limit` of 0 or None is every row. `preview`
    flattens each message to one line, which is what choosing a cut wants.
    """
    session = _caller()
    conn = _connect()
    sid = _session_id(conn, session)
    after = 0 if after is None else after
    limit = DEFAULT_LIMIT if limit is None else limit

    rows = conn.execute(
        """select seq, kind, display from messages
            where session_id = ? and kind = 'text' and seq > ?
            order by seq""",
        (sid, after),
    ).fetchall()
    # Only the tail: the newest messages are where a cut gets chosen.
    rows = rows[-limit:] if limit else rows

    messages = []
    for row in rows:
        text = _body(row)
        role = "user"
        try:
            role = _json.loads(row["display"]).get("role") or "user"
        except ValueError:
            pass
        shown = text
        if preview:
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
    conn.close()

    return _json.dumps(
        {
            "session": session,
            "last_seq": last,
            "live_messages": live,
            "shown_messages": len(messages),
            "messages": messages,
        },
        ensure_ascii=False,
        indent=2,
    )


def _command_layer():
    """This server's own command, as the shell it started was given it."""
    return _os.environ.get(COMMAND_VARIABLE)


def _command_result(stdout):
    """The terminal outcome of the one command the command layer ran, as its events report it."""
    for line in stdout.splitlines():
        try:
            event = _json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("t") == "cmd_result":
            return event
    return None


def _send_note(session, note):
    """Puts the handoff note in the session as an ordinary message.

    # Invariant

    A failure here never loses the note: it is printed, which is what the call's
    result carries into the continuation's own transcript -- the session is already
    empty, so that result is the only copy left.

    Answers what the command said it did, because whether a message is delivered now
    or at the next step boundary is the server's answer, not this function's.

    The command layer is spawned rather than imported: it is how a session talks to
    its server from a shell, and this is one command.
    """
    cli = _command_layer()
    detail = None
    if not cli:
        detail = (
            f"{COMMAND_VARIABLE} is not set: only a shell the server started has it "
            f'(check with: echo "${COMMAND_VARIABLE}")'
        )
    else:
        try:
            sent = _subprocess.run(
                [
                    cli,
                    _json.dumps(
                        {"name": "send_message", "args": {"session": session, "message": note}}
                    ),
                ],
                capture_output=True,
                text=True,
            )
        except OSError as error:
            sent = None
            detail = str(error)
        if sent is not None:
            outcome = _command_result(sent.stdout)
            if sent.returncode == 0 and outcome is not None and outcome.get("ok"):
                return outcome.get("message") or ""
            detail = (
                (outcome or {}).get("message")
                or (sent.stderr or "").strip()
                or f"the command exited {sent.returncode}"
            )
    _die(
        f"the transcript was copied to a mentor and emptied, but the note could not be "
        f"sent to {session!r} ({detail}). It is repeated here because this result is now "
        f"the only place it exists; continue from it, and from now on carry it as your "
        f"own message:\n\n{note}"
    )


def _continuation_message(session, mentor, kept):
    """What the continuation reads as the answer to the call that started it."""
    return (
        f"You are continuing `{session}` with a fresh context, and `{mentor}` holds the complete transcript. "
        f"This session is empty {'down to the call you are reading this from' if kept else 'for your next request'}. "
        "Your handoff note is the message that follows this one: it carries the current objective, "
        "state, failed attempts, and next action. Everything else remains readable in the mentor.\n"
        f"  read it:  gongga-cli '{_json.dumps({'name': 'get_messages', 'args': {'session': mentor, 'range': '1:'}})}'\n"
        f"  ask it:   gongga-cli '{_json.dumps({'name': 'run_skill', 'args': {'id': 'harness/self-control', 'cmd': {'name': 'ask', 'args': {'session': mentor, 'question': '<your question>'}}}})}'\n"
        "Ask before you redo. The mentor holds the earlier attempts, the failures and why they "
        "failed, so one question costs one turn where a repeated search costs the whole search "
        "again. The call waits for the answer and prints it; the mentor also sends it to "
        "this session, so keep working and read it when it lands.\n"
        "Nothing about the task changed and no clean state is required: continue the current work "
        "from the note's next action.\n"
        "The note separates what was observed from what was concluded: a claim marked as settled "
        "carries the command and the output that settle it. Anything it lists as `未结案` is yours "
        "to finish -- it is not a fact, and asking the mentor is cheaper than trusting it."
    )


def _mentor_message(continuation):
    """The mentor's duty while the continuation carries on the current work."""
    return (
        f"You are the mentor of `{continuation}`: its complete transcript was copied into "
        "this session row for row, and this row answers its last call. It is "
        "continuing the current work there. Answer only what it asks, and answer it in one "
        f"message: read its questions with `gongga-cli '{_json.dumps({'name': 'get_messages', 'args': {'session': continuation, 'range': '1:'}})}'`, "
        f"reply with `gongga-cli '{_json.dumps({'name': 'send_message', 'args': {'message': '<your answer>', 'session': continuation}})}'`. "
        "Put the whole answer in your own turn as well: that turn is what the continuation's "
        "`ask` reads, and a message alone leaves that reader with your receipt. "
        "Do not start work of your own here."
    )


def _established(conn, name):
    """Whether `name` is a session that already holds rows.

    A mentor an earlier handover used is an archive, not a scratch pad: handing
    off into it again would put two archives in one transcript, and the continuation
    reads that transcript from its first row.
    """
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is None:
        return False
    count = conn.execute(
        "select count(*) from messages where session_id = ?", (row["id"],)
    ).fetchone()[0]
    return count > 0


def _free_name(conn, wanted):
    """`wanted` itself when it holds no rows, else `wanted/2`, `wanted/3`, ...

    A segment is added rather than a suffix, because a name is a path: the archive
    of the second handover belongs under the first one's name, where the tree in
    the session list already shows it.
    """
    if not _established(conn, wanted):
        return wanted
    n = 2
    while _established(conn, f"{wanted}/{n}"):
        n += 1
    return f"{wanted}/{n}"


def _ensure_mentor(conn, name, source):
    """The mentor's id, creating the session with the continuation's model and directory.

    A mentor is an ordinary session: the agent reads it and talks to it with the
    same commands every client has, which is the whole reason it is a session and
    not a file. `name` is already the free one -- see `_free_name`.
    """
    row = conn.execute("select id from sessions where name = ?", (name,)).fetchone()
    if row is not None:
        return row["id"], False
    now = int(_time.time() * 1000)
    # `model_name` is the bare name and is NOT NULL; `model` is the pair. A row
    # written without the first failed the constraint outright.
    bare = str(source["model"]).split("/")[-1]
    conn.execute(
        """insert into sessions (name, status, model_name, model, cwd, created_at, updated_at)
            values (?, 'idle', ?, ?, ?, ?, ?)""",
        (name, bare, source["model"], source["cwd"], now, now),
    )
    return (
        conn.execute("select id from sessions where name = ?", (name,)).fetchone()["id"],
        True,
    )


def handoff(note, mentor=None):
    """Archive this session's transcript in `mentor`, verify it, empty this one.

    `mentor` is where the archive goes, and it is always a child of this session:
    `<session>/mentor` when omitted, and a name under this session's own path when
    given. A child that already holds a transcript is not reused -- `2`, `3`, ... is
    added under it -- so one archive never becomes two.

    Answers what the handover did as JSON text, including `you`: the instructions the
    continuation reads as this call's result.
    """
    session = _caller()
    # A segment, not a prefix: the archive hangs under the session it came from,
    # where the session list already draws it as a child.
    mentor = mentor if mentor else f"{session}/mentor"
    # An archive is written where the caller can see it and nowhere else. The
    # rule is structural -- under this session's path -- so it needs no list of
    # reserved names: `@monitor` handing off gets `@monitor/mentor`, which
    # is its own child, and nobody can make it write outside its own subtree.
    under = f"{session}/"
    if not mentor.startswith(under) or mentor == under:
        _die(
            f"mentor {mentor!r} is not under {session!r}: an archive is a child of the "
            f"session that made it, e.g. {session}/mentor. Writing one anywhere else "
            "would put this transcript in a session nobody asked for."
        )
    if not note.strip():
        _die(
            "refusing to hand off with an empty note: the mentor holds the archive, "
            "but this note is what the continuation actually sends"
        )

    conn = _connect()
    # The mentor a handover makes room for: the asked-for name when nothing holds it,
    # otherwise the next one under it. Never the session being emptied -- `mentor`
    # is held under this session's own path, which cannot be the session itself --
    # and never an archive an earlier handover left behind.
    mentor = _free_name(conn, mentor)
    sid = _session_id(conn, session)
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
        conn.close()
        _die(f"{session} has no transcript to hand off; nothing was written")

    # The server runs a call the moment it streams, so the last row is this
    # call and its result does not exist yet. That pair stays -- a
    # result without its call is a request no provider accepts -- and everything
    # before it is the past, which goes to the mentor.
    kept = rows[-1] if rows[-1]["kind"] == "tool_call" else None
    kept_call_id = _json.loads(kept["content"])["call_id"] if kept is not None else None
    moved = rows[:-1] if kept is not None else rows
    through = moved[-1]["seq"] if moved else 0
    marker = (
        f"[handoff] `{session}` handed off through seq {through} "
        f"({len(moved)} rows), keeping the call in flight. The continuation may read this mentor "
        "with get_messages and ask it with send_message."
    )

    with conn:
        mid, created = _ensure_mentor(conn, mentor, source)
        base = conn.execute(
            "select coalesce(max(seq), 0) from messages where session_id = ?", (mid,)
        ).fetchone()[0]
        now = int(_time.time() * 1000)
        conn.execute(
            """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                values (?, ?, 'text', ?, ?, NULL, ?)""",
            (
                mid,
                base + 1,
                _json.dumps({"role": "user", "content": marker}),
                _json.dumps({"role": "user", "text": marker}),
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

        # The archive is checked before the original is touched: a handoff that
        # deleted first would turn a half-written copy into lost work.
        copied = conn.execute(
            """select kind, content, display, call_id from messages
                where session_id = ? and seq > ? order by seq""",
            (mid, base + 1),
        ).fetchall()
        if len(copied) != len(rows):
            _die(f"the copy has {len(copied)} row(s) and the original has {len(rows)}")
        for got, original in zip(copied, rows):
            if (got["kind"], got["content"], got["display"], got["call_id"]) != (
                original["kind"],
                original["content"],
                original["display"],
                original["call_id"],
            ):
                _die(
                    f"the copy differs at seq {original['seq']}; nothing was deleted "
                    f"(mentor {mentor} holds {len(copied)} copied row(s))"
                )

        # The copied call is a question with no answer, in a session that will now
        # be read back as a transcript. Give it its result, and tell the mentor
        # what it has become: this is the row that says so.
        if kept is not None:
            call_id = _json.loads(kept["display"])["call_id"]
            output = _mentor_message(session)
            conn.execute(
                """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                    values (?, ?, 'tool_result', ?, ?, ?, ?)""",
                (
                    mid,
                    base + 2 + len(rows),
                    _json.dumps(
                        {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output,
                        }
                    ),
                    _json.dumps({"call_id": call_id, "images": [], "output": output}),
                    call_id,
                    now,
                ),
            )

        # Context only has append/cut, and a cut may only take the tail: the
        # `context_cut_tail_only` trigger aborts deleting any row below the
        # session's newest. So the transcript is cut one row at a time, newest
        # first. The call in flight is itself the tail, so it is cut with the rest
        # and appended back verbatim: its `call_id` is what the result the server
        # is about to write pairs with, and a result without its call is a request
        # no provider accepts.
        for row in reversed(rows):
            conn.execute(
                "delete from messages where session_id = ? and seq = ?",
                (sid, row["seq"]),
            )
        if kept is not None:
            conn.execute(
                """insert into messages (session_id, seq, kind, content, display, call_id, created_at)
                    values (?, (select coalesce(max(seq), 0) + 1 from messages where session_id = ?),
                            ?, ?, ?, ?, ?)""",
                (
                    sid,
                    sid,
                    kept["kind"],
                    kept["content"],
                    kept["display"],
                    kept["call_id"],
                    kept["created_at"],
                ),
            )
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
                    _json.dumps(
                        {
                            "mechanism": "handoff",
                            "mentor": mentor,
                            "mentor_created": created,
                            "rows": len(rows),
                            "rows_moved": len(moved),
                            "through_seq": through,
                            "kept_call": kept_call_id,
                            "note_chars": len(note),
                            "context_tokens": source["context_tokens"],
                            "context_limit": source["context_limit"],
                        }
                    ),
                    now,
                ),
            )
        except _sqlite3.Error as error:
            # The handover is the work; the row is the record of it. A home too
            # old to have the table, or a write that fails, must not undo the
            # copy -- and it must not be silent either.
            print(
                f"warning: handoff succeeded but was not recorded ({error})",
                file=_sys.stderr,
            )

    conn.close()

    # The session is empty now, so this is the first user message it will carry.
    # Sent from here rather than written into the database: a row inserted between
    # the call in flight and its result is a transcript no provider accepts, and
    # `send_message` is what puts a note where an operator's message would go.
    delivery = _send_note(session, note)

    return _json.dumps(
        {
            "session": session,
            "mentor": mentor,
            "mentor_created": created,
            "rows_copied": len(rows),
            "through_seq": through,
            "kept_call": _json.loads(kept["content"])["call_id"] if kept is not None else None,
            "mentor_rows": len(copied) + 1 + (1 if kept is not None else 0),
            "deleted": len(moved),
            "note_chars": len(note),
            "note_delivery": delivery,
            "you": _continuation_message(session, mentor, kept is not None),
        },
        ensure_ascii=False,
        indent=2,
    )
