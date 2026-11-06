#!/usr/bin/env python3
"""Open a new session that runs on my own configuration: `new_session`'s engine.

    new_session(name, source=None, model=None, thinking=None, ctx_limit=None,
                cache_tier=None, cache_keepalive=None, mode=None, remote=None,
                cwd=None, dry_run=False)

`mapping()` is the pure part -- a source row plus the caller's overrides answers
the `set_session` words -- and it is what the unit test drives. `new_session()`
adds the two impure edges: reading the source row from this home's sqlite, and
running the words through the command layer.

The three invariants live in SKILL.md §4c; `mapping()` is where two of them are
enforced: the machine and its directory travel together (`cwd` is copied only
when the target machine is the source machine), and the name is the caller's
own, never the source's (`--must_create` refuses to join another transcript).
The third -- an empty transcript -- holds because the words carry configuration
only: no message, no copy, no subscription.
"""

import os
import shlex
import sqlite3

import ask_session

#: The home's record of what a session runs on. §4c names the fields; a NULL
#: means "the server's configuration", and is copied by omitting the flag,
#: never by inventing a value.
COLUMNS = (
    "name",
    "model",
    "thinking_effort",
    "context_limit",
    "cache_tier",
    "cache_keepalive",
    "mode",
    "remote",
    "cwd",
)


def _machine(spelling):
    """One machine out of a remote spelling: None for this machine's own names."""
    if spelling is None:
        return None
    spelling = str(spelling).strip()
    return None if spelling.lower() in ("", "none") else spelling


def _given(value):
    """Whether the caller named a value: None and blank are both "not named";
    False and 0 are names (a boolean and a percentage can be said outright)."""
    return value is not None and str(value).strip() != ""


def mapping(source, name, model=None, thinking=None, ctx_limit=None,
            cache_tier=None, cache_keepalive=None, mode=None, remote=None,
            cwd=None):
    """The `set_session` words for the fresh session: the source row's value where
    the caller named nothing, the caller's where named.

    `source` is one row of the home's `sessions` table, as a dict keyed by
    COLUMNS. Returns the words after `set_session`, `name` first and
    `--must_create true` last: a taken name must refuse, never join.

    # Invariants (SKILL.md §4c)

    - Machine and directory travel together: `cwd` is copied only when the
      target machine is the source machine; a `--cwd` is read on the machine
      it lands on.
    - The name is not copied: only the configuration is.
    - The new session is empty: the words carry no transcript, no message.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("name the new session")

    recorded = _machine(source.get("remote"))
    wanted = recorded if remote is None else _machine(remote)
    same_machine = wanted == recorded
    place = cwd if cwd is not None else (source.get("cwd") if same_machine else None)

    chosen_model = model if _given(model) else (source.get("model") or "").strip()
    if not chosen_model:
        raise ValueError(f"the source session {source.get('name')!r} runs on no model to copy, and none was named")

    words = [name, "--model", chosen_model]
    # A source NULL is "the server's configuration": copied by saying nothing.
    chosen = thinking if _given(thinking) else source.get("thinking_effort")
    if chosen:
        words += ["--thinking", str(chosen)]
    chosen = ctx_limit if _given(ctx_limit) else source.get("context_limit")
    if chosen is not None:
        words += ["--ctx_limit", chosen if isinstance(chosen, str) else f"{chosen}%"]
    chosen = cache_tier if _given(cache_tier) else source.get("cache_tier")
    if chosen:
        words += ["--cache_tier", str(chosen)]
    chosen = cache_keepalive if cache_keepalive is not None else source.get("cache_keepalive")
    if chosen is not None:
        words += ["--cache_keepalive", "true" if chosen else "false"]
    chosen = mode if _given(mode) else source.get("mode")
    if chosen:
        words += ["--mode", str(chosen)]
    if wanted:
        words += ["--remote", wanted]
    if place:
        words += ["--cwd", str(place)]
    words += ["--must_create", "true"]
    return words


def _source_row(home, name):
    """The source session's row out of this home's sqlite, read-only, or None."""
    if not home:
        raise RuntimeError("GONGGA_HOME is not set: only a shell the server started knows its home")
    database = os.path.join(home, "gongga.db")
    if not os.path.isfile(database):
        raise RuntimeError(f"no gongga.db under {home}: this is not a server's home")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        cursor = connection.execute(
            "select " + ", ".join(COLUMNS) + " from sessions where name = ?", (name,)
        )
        row = cursor.fetchone()
    finally:
        connection.close()
    return None if row is None else dict(zip(COLUMNS, row))


def new_session(name, source=None, model=None, thinking=None, ctx_limit=None,
                cache_tier=None, cache_keepalive=None, mode=None, remote=None,
                cwd=None, dry_run=False):
    """Create a session with my configuration; answer its name, or the planned
    command when `dry_run` -- the caller's own row is the only record of what
    "my configuration" is, so without a source there is nothing to copy and no
    session is created.
    """
    name = (name or "").strip()
    if not name:
        raise RuntimeError("name the new session")
    if source is None:
        source = os.environ.get("GONGGA_SESSION", "").strip()
        if not source:
            raise RuntimeError(
                "no source session: $GONGGA_SESSION is not set and --source named no one"
            )
    row = _source_row(os.environ.get("GONGGA_HOME", ""), source)
    if row is None:
        raise RuntimeError(f"no session named {source!r}: nothing to copy from")
    words = mapping(row, name, model=model, thinking=thinking, ctx_limit=ctx_limit,
                    cache_tier=cache_tier, cache_keepalive=cache_keepalive, mode=mode,
                    remote=remote, cwd=cwd)
    if dry_run:
        return "set_session " + shlex.join(words)
    try:
        events = ask_session._run(ask_session._command_layer(), ["set_session", *words])
    except ask_session._Refused as refused:
        raise RuntimeError(str(refused))
    ok, message = ask_session._outcome(events)
    if not ok:
        raise RuntimeError(message or "set_session was refused without a message")
    return name
