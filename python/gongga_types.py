"""SPEC's declared shapes, as Python types, and the one reader that builds them.

SPEC spells the shapes its commands take and answer in `## Server Cmd`:

    ModelConfig := {model, thinking, ctx_limit, cache_tier, live_compact, prefix_cache_plan}
    Workspace := {cwd, worktree}
    SessionConfig := {model_config, workspace, keep_alive}

Those lines are the interface, so they are named here once rather than retyped in every
skill that sends one. A skill says `gongga_types.SessionConfig` and gets this file's
declaration; a skill that typed its own field list drifted from SPEC the moment the
shapes moved -- `workspace.remote` was dropped by protocol 16 -- and the drift stayed
invisible until the server refused the config it was handed.

# Invariant

One field list per shape, and it is SPEC's. `config_of_session` is the only place in
this package that turns what a server published about a session into a `SessionConfig`:
a second such reader is a second answer to "what is this session's config", and the
wrong one is the one nobody re-reads.
"""

from __future__ import annotations

import collections.abc
import typing

#: SPEC `Session := path # sticky`.
Session = str

#: SPEC `Skill := path # abstract naming space`.
Skill = str

#: SPEC `Channel := path`.
Channel = str

#: SPEC `Thinking = provider_default()`: a level the model declares, in the roster's
#: spelling. A closed set per model, so a plain string here rather than an enum.
Thinking = str

#: SPEC `Range`: `begin:end` over sequence numbers, as `get_messages` reads it.
Range = str

#: The prefix SPEC gives a stated machine (`crates/server/src/space.rs::PREFIX`).
REMOTE_PREFIX = "remote://"


class Model(typing.TypedDict):
    """SPEC `Model := {provider, model} # sticky`."""

    provider: str
    model: str


class LiveCompact(typing.TypedDict, total=False):
    """SPEC `LiveCompact := {enabled, interval_tokens, mode} # sticky`.

    # Invariant

    Every field may be absent, and absent is `default()`: the home's `[agent]` setting
    answers a session that stated nothing. Writing `False` where SPEC means "unset"
    would be a session that chose against its home.
    """

    enabled: bool
    interval_tokens: int
    #: `cut` or `mask`.
    mode: str


class ModelConfig(typing.TypedDict, total=False):
    """SPEC `ModelConfig := {model, thinking, ctx_limit, cache_tier, live_compact, prefix_cache_plan}`.

    # Invariant

    `thinking`, `ctx_limit` and `cache_tier` are re-defaulted when the model changes, so
    a reader that has just written a new model leaves them out: a value it states is a
    value the server must not recompute for the model it drew.
    """

    model: Model
    thinking: Thinking
    #: A share of the window, spelled the way a caller writes it: `"70%"`.
    ctx_limit: str
    cache_tier: str
    live_compact: LiveCompact
    prefix_cache_plan: bool


class Workspace(typing.TypedDict, total=False):
    """SPEC `Workspace := {cwd, worktree}`.

    # Invariant

    `cwd` is the whole statement about placement: a `remote://<name>/<absolute path>`
    is that peer, and a bare one is the machine the session already records. There is no
    field beside it that names a machine -- protocol 16 removed `remote`, and a config
    carrying one is refused (`deny_unknown_fields`, `crates/server/src/session_config.rs`).
    """

    cwd: str
    worktree: str


class SessionConfig(typing.TypedDict, total=False):
    """SPEC `SessionConfig := {model_config, workspace, keep_alive} # sticky`.

    # Invariant

    An unset setting is an absent key, never `None`: absent is what "the default" means
    to the server, and `set_session` reads a stated `null` as "clear this" only for the
    fields that take one. See `docs/notes/server-session-config.md`.
    """

    model_config: ModelConfig
    workspace: Workspace
    keep_alive: bool


def spelled(remote: typing.Optional[str], cwd: str) -> str:
    """A machine and a directory as one `cwd`, in SPEC's own spelling.

    # Invariant

    The Python spelling of `crates/server/src/space.rs::landed`: this machine is
    `remote://local/`, so a session that records no peer states its directory bare and a
    session on a peer states `remote://<name>/<path>`. Two writers of this string would
    disagree about which machine a directory is on, and no path says that by itself.
    """
    if not remote:
        return cwd
    return f"{REMOTE_PREFIX}{remote}/{cwd}"


def config_of_session(row: collections.abc.Mapping) -> SessionConfig:
    """SPEC's `SessionConfig` from one session row the server published.

    `row` is an entry of `gongga.Client.sessions` -- the greeting's session list and the
    `session` deltas that follow it -- so nothing here reads the database. What it reads
    is the server's own statement about a session, which is why a column rename cannot
    reach it: a column is the table's business, and the published row is the answer.

    # Invariant

    Every key is SPEC's, and a setting the row does not carry is left out rather than
    written as `None` -- absent is "the default", the one thing `new_session` and
    `set_session` both read that way. `cwd` and the published `remote` are one fact
    ([`spelled`]), because SPEC keeps placement in `cwd` alone.
    """
    config: SessionConfig = {}

    model_config: ModelConfig = {}
    spelled_model = str(row.get("model") or "").strip()
    provider, slash, model = spelled_model.partition("/")
    # A row that does not spell a provider names no model this file can state: leaving
    # the key out keeps the session's own, where inventing a provider would move it.
    if slash and model:
        model_config["model"] = {"provider": provider, "model": model}

    thinking = str(row.get("thinking_effort") or "").strip()
    if thinking:
        model_config["thinking"] = thinking

    percent = _percent(row.get("context_limit"))
    if percent:
        model_config["ctx_limit"] = percent

    tier = str(row.get("cache_tier") or "").strip()
    if tier:
        model_config["cache_tier"] = tier

    live = row.get("live_compact")
    if isinstance(live, collections.abc.Mapping):
        stated: LiveCompact = {}
        if live.get("enabled") is not None:
            stated["enabled"] = bool(live["enabled"])
        if live.get("interval_tokens") is not None:
            stated["interval_tokens"] = int(live["interval_tokens"])
        mode = str(live.get("mode") or "").strip()
        if mode:
            stated["mode"] = mode
        if stated:
            model_config["live_compact"] = stated

    # The one setting the published row keeps only in its raw-column map: it is read when
    # a session's head is stamped, so the protocol row has no field of its own for it.
    plan = _state(row, "prefix_cache_plan")
    if isinstance(plan, bool):
        model_config["prefix_cache_plan"] = plan

    if model_config:
        config["model_config"] = model_config

    workspace: Workspace = {}
    cwd = str(row.get("cwd") or "").strip()
    if cwd or row.get("remote"):
        workspace["cwd"] = spelled(row.get("remote"), cwd)
    # `worktree` is deliberately not read: one is made with the session that asked for it
    # and refused afterwards (`docs/notes/server-worktree.md`), so handing a child its
    # parent's name would ask for a second worktree of one branch.
    if workspace:
        config["workspace"] = workspace

    config["keep_alive"] = bool(row.get("keep_alive"))
    return config


def _percent(value: typing.Any) -> typing.Optional[str]:
    """SPEC's `ctx_limit` from the row's stored limit: `"70%"`, or `None` when unset."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        return f"{int(value)}%"
    except (TypeError, ValueError):
        return None


def _state(row: collections.abc.Mapping, column: str) -> typing.Any:
    """One of a session row's own columns, as the published row carries them."""
    state = row.get("state")
    if not isinstance(state, collections.abc.Mapping):
        return None
    return state.get(column)