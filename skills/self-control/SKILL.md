---
name: self-control
description: The server's command schemas as this build publishes them -- the twenty commands a session drives itself with (send_message, new_session, set_session, del_session, ls_session, clone_session, interrupt_session, cancel, subscribe, ls_subscription, del_subscription, trigger_event, new_channel, input_channel, ls_channel, rename_channel, del_channel, get_messages, events, list_paths), each with its exact arguments and a callable example, plus the one convenience this skill publishes, `ask`, which puts a question to another session and waits for its answer. Read it when you are about to call one of these and need its argument names, which ones are required, and what the answer carries.
compatibility: the command schemas need nothing but the server; `ask` needs Python 3 and the client the server writes at <home>/lib/gongga.py
---

# Self control: the server's commands

This file is the schema reference for the commands a session uses to drive itself: its
own turn and the turns of others, its sessions, its channels, its transcript, and the
event stream. Every command below is listed with the arguments the server actually
publishes (`crates/server/src/commands.rs`, `server_catalog`), and each one has a
`gongga-cli` call you can paste.

It is not a second copy of SPEC: naming, `config` shapes and the last word on meaning
are SPEC's (`## Server Cmd`). It is also not about when to use the harness versus
changing it: two sessions working in parallel is `@skill(subagent)`, replacing the
server itself is `@skill(evolve)`. And `ls_skill` / `run_skill` are the `@skill(skill)`
skill's business, so they are not repeated here.

## The commands, by subject

| subject | commands |
|---|---|
| a turn | `send_message`, `interrupt_session`, `cancel` |
| sessions | `new_session`, `set_session`, `del_session`, `ls_session`, `clone_session` |
| channels | `new_channel`, `input_channel`, `ls_channel`, `rename_channel`, `del_channel` |
| reading | `get_messages`, `events` |
| subscriptions | `subscribe`, `ls_subscription`, `del_subscription`, `trigger_event` |
| paths | `list_paths` |

## How a command is called, and how its arguments are written

Four doors, one catalog:

1. **The `gongga_cli` tool** -- one tool that takes the command itself: call it with
   `cmd`, SPEC's `Cmd`, `{"name": "<cmd>", "args": {...}}`. The schema of what you are
   running is this file's, not the tool list's, and the server runs it with your session
   as the asking one. This is the door to reach for by default:

   ```json
   {"cmd": {"name": "ls_channel", "args": {"path": "/"}}}
   ```

2. **A tool call named after the command** -- a `[tool.<name>]` row the config offers by
   that command's own name. The schema is not in the tool list; it is in this file. The
   server routes the name to the command layer and runs it with your session as the
   asking one.
3. **`gongga-cli '<json>'`** in a shell -- one JSON Cmd, `{"name": "<cmd>", "args":
   {...}}`. It prints one event per line, the last being `cmd_result` (`ok`, `message`,
   `data`). Exit: `0` ran, `1` the server refused, `2` the line itself was wrong, `3`
   nothing answered.
4. **Python** -- `server.call("<cmd>", **arguments)`, one `Reply` per call. "The Python
   client", below.

**In the JSON form an argument is spelled by name, always.** The distinction between a
*named* argument (`--message <text>`) and a *positional* one (`<path>`) belongs to the
command line a person types in a window: there, positional arguments bind in the order
published, then named ones as `--name value`. The examples here are JSON, so neither
order nor spelling can go wrong.

**Every argument travels as a string.** A JSON boolean, number or object in place of a
string is sent as its own JSON text, which is how `config` and a subscription's `cmd`
travel: `true` arrives as the text `true`, and `70` as the text `70`.

**`session` arguments carry the kind `session`.** A window fills a missing one from its
sticky `session` entry (`set_sticky --type session --value <name>`) and the two tool
doors fill it with the calling session; `gongga-cli` and `server.call` do not -- they
send exactly the arguments they are given. So every `session` argument below is
required in practice unless the command says it falls back to the caller.

**The server checks required arguments before it runs the command** and refuses with the
missing names: `send_message is missing required argument(s): session`. An argument the
catalog does not declare is *not* refused -- that is how `set_session` reads a few
settings it does not advertise (see there).

**A path takes what is under it, by components.** Sessions, channels and subscriptions
are named by paths: `work/a` is under `work`, and `workab` is not. An empty path means
"every one" wherever a path is a filter.

## Sessions

### `send_message` -- put a message into a session and run a turn

| argument | spelling | required | meaning |
|---|---|---|---|
| `message` | named (`--message`) | yes | the text, with `@file(...)`, `@image(...)`, `@skill(...)` resolved once before it is stored |
| `session` | named (`--session`), kind `session` | yes | the target; there is no implicit default, not even the caller |

A free session starts a turn now (`message accepted by <name>`). A session already
running gets the message at its next step boundary and is told to stop waiting
(`message queued for <name>; the running command has been told to stop waiting`). While
the server is stopping, the message is recorded for the next start instead.

```bash
gongga-cli '{"name":"send_message","args":{"session":"work/child","message":"the fix is in crates/server"}}'
```

### `new_session` -- start a session under a name nobody holds

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | named (`--id`), kind `session`, sticky | yes | the name; a path, rendered as a hierarchy |
| `config` | named (`--config`) | no | SPEC's `SessionConfig` as one JSON object |
| `mode` | named (`--mode`), kind `mode` | no | `work` (the default) or `explore` |

SPEC's `new_session` is `set_session` that refuses a name already taken. That is why
`must_create` is not published for this command: refusing a name in use is what the
command means. The refusal is explicit -- it never joins the other session's transcript.

#### The `config` object

One JSON object, SPEC's `SessionConfig`:

```json
{
  "model_config": {
    "model": {"provider": "deepseek-official", "model": "deepseek-flash"},
    "thinking": "high",
    "ctx_limit": "70%",
    "cache_tier": "1h"
  },
  "workspace": {"remote": "none", "cwd": "C:/resource/gongga"},
  "keep_alive": true
}
```

| field | takes | notes |
|---|---|---|
| `model_config.model` | `{provider, model}`, or `{"model": "<provider>/<model>"}` | flattened to the `provider/model` path the session row stores; a provider with no model is refused, a model with no provider is looked up in the roster, which takes the first provider that has it |
| `model_config.thinking` | a level the model declares, or `default` | checked against the model this session runs on, so a typo fails now; `default` clears an earlier level |
| `model_config.ctx_limit` | `"70%"`, `"70"`, or `0` | percent of the model's context window; `0` means never notice. Needs the model to declare a window |
| `model_config.cache_tier` | a tier the model declares, or `none` | validated against the roster entry, so a tier from another provider is refused here |
| `workspace.remote` | a remote's name, or `none` | `none` is this machine; a name not in the roster is refused before anything is created |
| `workspace.cwd` | a path | read on the machine the session lands on; moving to another machine does not carry the source's directory |
| `keep_alive` | `true` / `false` | SPEC's `keep_alive`: send a message at the step boundary when the cache's life is nearly over |

Every field is optional, at every level. For `new_session` an omitted field takes the
configured default; for `set_session` (next) it keeps what the session already had.

```bash
# a fresh session on my own model, in this checkout
gongga-cli '{"name":"new_session","args":{"id":"work/child","config":{"model_config":{"model":{"provider":"deepseek-official","model":"deepseek-flash"},"ctx_limit":"70%"},"workspace":{"cwd":"C:/resource/gongga"}}}}'
```

```bash
# explore only: refusals instead of writes
gongga-cli '{"name":"new_session","args":{"id":"work/reader","mode":"explore"}}'
```

### `set_session` -- create the session if it does not exist, and apply settings

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | named (`--id`), kind `session`, sticky | yes | the session to create or re-tune |
| `config` | named (`--config`) | no | SPEC's `SessionConfig`, as a **delta**: omitted fields are kept |
| `mode` | named (`--mode`), kind `mode` | no | `work` or `explore`; omitted keeps the session's mode |
| `must_create` | named (`--must_create`) | no | `true` refuses the command when the name is already taken |

What the command actually applies is a flat set of settings, and `config` is flattened
into exactly those (`crates/server/src/session_config.rs`). They are therefore also
readable as flat arguments, although the catalog does not advertise them, so a client
completing from the catalog will never offer them:

| flat argument | same as |
|---|---|
| `model` | `config.model_config.model`, spelled `provider/model` |
| `thinking` | `config.model_config.thinking` |
| `ctx_limit` | `config.model_config.ctx_limit` |
| `cache_tier` | `config.model_config.cache_tier` |
| `keep_alive` | `config.keep_alive` |
| `remote` | `config.workspace.remote` |
| `cwd` | `config.workspace.cwd` |

Three rules that make the delta safe:

- **An omitted argument is a decision, not a default.** The session keeps what it had,
  at every level of the object.
- **`none` is a statement.** `remote: "none"` and `cache_tier: "none"` clear those two;
  leaving them out keeps them.
- **Writing a model re-defaults what belongs to a model.** A new model re-resolves the
  thinking level and validates the cache tier against itself.

Workspace changes are moves, not edits. A `cwd` is read on the destination's side; a
`remote` change lands the session on that machine; a move asked for while a turn is
running is queued and happens when that turn ends (`move_pending` in the answer says
so). Changing `mode` to `explore` or back injects a notice the agent reads at its next
step boundary, and `explore` -> `work` also opens the live-compact checkpoint SPEC asks
for.

The answer is a sentence and:

```json
{"session": "work/child", "move_pending": false, "cwd": "C:/resource/gongga",
 "remote": null, "model": "deepseek-official/deepseek-flash", "context_limit": 70,
 "thinking_effort": "high", "mode": "work"}
```

```bash
gongga-cli '{"name":"set_session","args":{"id":"work/child","config":{"model_config":{"thinking":"low"},"keep_alive":false}}}'
```

```bash
# the flat form still lands, for a caller that already knows the handler
gongga-cli '{"name":"set_session","args":{"id":"work/child","ctx_limit":"40%"}}'
```

### `del_session` -- delete a session and everything under that path

| argument | spelling | required | meaning |
|---|---|---|---|
| `path` | named (`--path`), kind `session` | yes | one session, or a directory whose sessions all go |

Deletes the session's channels (their programs end), its background jobs, its
subscriptions, its pending task rows and its transcript. A running turn is interrupted
first, so nothing is left owing a reply. A path deletes every session below it and
answers `deleted N session(s) under "<path>": ...`.

```bash
gongga-cli '{"name":"del_session","args":{"path":"work/child"}}'
```

### `ls_session` -- list sessions by the prefix of their name

| argument | spelling | required | meaning |
|---|---|---|---|
| `path` | positional | no | the name prefix; omitted is every session |

No session argument: this is the server's whole list. The answer is a JSON array in the
`message` (there is no `data`), one row per session:

```json
[{"name": "work/child", "status": "Idle", "model": "deepseek-official/deepseek-flash",
  "cwd": "C:/resource/gongga", "context_limit": 70, "thinking_effort": "high",
  "created_at": "...", "updated_at": "..."}]
```

`status` is the server's own word for the session's turn and is what a waiter reads to
tell "still running" from "finished".

```bash
gongga-cli '{"name":"ls_session","args":{"path":"work/"}}'
```

### `clone_session` -- copy a transcript into a free name

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | named (`--id`), kind `session` | yes | the session to copy; it keeps its own rows |
| `new_id` | positional (`<new_id>`) | yes | the name the copy takes; it must be free |

Row for row at the same `seq`, so the copy reads exactly as the source does; its next
turn is its own. Row count and the last `seq` are in the answer.

```bash
gongga-cli '{"name":"clone_session","args":{"id":"mentor","new_id":"work/mentor-copy"}}'
```

### `interrupt_session` -- stop the turn one session is running

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | positional (`<id>`), kind `session` | yes | whose turn stops |

A tool that is waiting returns early with a result, the stream stops, and everything
received is kept in the context. Nothing is owed to the next start: continuing a turn is
`cancel`'s flag, not this one's. Channels (background programs) keep running. An idle
session answers `is idle`; one that exists but is not running answers that there is
nothing to interrupt, which is still success.

```bash
gongga-cli '{"name":"interrupt_session","args":{"id":"work/child"}}'
```

### `cancel` -- interrupt every turn and shut the server down

| argument | spelling | required | meaning |
|---|---|---|---|
| `keep_alive` | named (`--keep_alive`) | no | `true` leaves every channel's program running for the next start to adopt |

This is the **server's** stop, not your turn's: every running turn is interrupted, the
stop is announced on the stream, and the process goes away.

- A session that is **not waiting** is flagged so the next start continues that turn.
- A session that **is waiting** is flagged to resume waiting for the time that is left.
- Without `keep_alive`, every channel's program is ended with the server. With it, they
  are detached and adopted on the next start; one that cannot be adopted produces a
  channel-end notice.

Background jobs a tool started (the detached ones) are released by this stop: they are
processes of their own, they outlive the server, and the next start adopts them. Any
other stop -- a signal, a killed process -- ends every one it started.

SPEC's rule that matters for you: the flag is all a restart gets. Nothing appends "you
were interrupted, continue", so anything the next start must know has to be in the
transcript (a `send_message` to yourself) or in a file it will read.

```bash
gongga-cli '{"name":"cancel","args":{"keep_alive":true}}'
```

## Channels

A channel is a program owned by a session: it outlives the turn, keeps its working
directory and environment, and is named by a path in the session's namespace
(`/bash/default`). The `exec` tool is the usual door; these commands are the same layer.

### `new_channel` -- start a program under this session

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | positional (`<id>`) | yes | the name it is called by: a path in this session's namespace |
| `name` | positional (`<name>`) | yes | the program: a path, or a name the config's `[channel.<name>]` rows know (that row supplies the spawn words) |
| `session` | named (`--session`), kind `session` | yes | the owner; the channel starts in that session's directory, environment and machine |

A name already in use is refused -- `exec` speaks to that one. The answer carries the
pid.

```bash
gongga-cli '{"name":"new_channel","args":{"id":"/bash/default","name":"bash","session":"work/child"}}'
```

### `input_channel` -- write a line in, read the unread part of what comes back

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | positional (`<id>`) | yes | the channel to write into |
| `input` | positional (`<input>`) | yes | the words; the `[channel.<name>]` row decides how a line is framed |
| `session` | named (`--session`), kind `session` | yes | the owner |
| `wait` | named (`--wait`) | no | milliseconds to wait for output, default `30000`; a limit on waiting, not on the program |
| `until` | named (`--until`) | no | a marker to stop at; the window ends before it |

The answer is SPEC's `Result`, as the message and as `data`:

```json
{"id": "/bash/default", "output": "...", "time": "0.412s", "matched": false, "exit": null}
```

What did not arrive stays unread for the next call. `matched` says whether `until` was
there, and `exit` is the program's exit status once it has ended.

```bash
gongga-cli '{"name":"input_channel","args":{"id":"/bash/default","input":"ls","session":"work/child","wait":"5000","until":"$ "}}'
```

### `ls_channel` -- a session's channels

| argument | spelling | required | meaning |
|---|---|---|---|
| `path` | positional | no | a name prefix; omitted is every channel the session has |
| `session` | named (`--session`), kind `session` | yes | whose channels |

A JSON array in the `message`: `id`, `session`, `program`, `remote`, `pid`, `dir`,
`started_at`, `ended_at`, `exit`, and `unread` (the lines nobody has read). Nothing
matching answers a sentence instead.

```bash
gongga-cli '{"name":"ls_channel","args":{"session":"work/child"}}'
```

### `rename_channel` -- give a channel another name

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | positional (`<id>`) | yes | the name it has now |
| `new_id` | positional (`<new_id>`) | yes | the name it takes |
| `session` | named (`--session`), kind `session` | yes | the owner |

Nothing moves on disk: the name is the server's namespace, not a path. A name already
taken is refused.

```bash
gongga-cli '{"name":"rename_channel","args":{"id":"/bash/default","new_id":"/bash/main","session":"work/child"}}'
```

### `del_channel` -- end a channel's program, and forget the name

| argument | spelling | required | meaning |
|---|---|---|---|
| `id` | positional (`<id>`) | yes | one channel, or a path whose channels all go |
| `session` | named (`--session`), kind `session` | yes | the owner |

A channel that had already ended is only forgotten, which is still success.

```bash
gongga-cli '{"name":"del_channel","args":{"id":"/bash/main","session":"work/child"}}'
```

## Reading

### `get_messages` -- a range of the transcript

| argument | spelling | required | meaning |
|---|---|---|---|
| `range` | positional (`<range>`) | no | which rows, as `begin:end` over sequence numbers; omitted is the whole transcript |
| `session` | named (`--session`), kind `session` | yes | whose transcript |
| `replay` | named (`--replay`) | no | `true` sends it as a window draws it, with the stream position the answer is a picture of |

How a range reads:

| written | means |
|---|---|
| `5` | row 5 only |
| `5:10` | rows 5 through 10, both ends inclusive |
| `5:` | from row 5 to the newest |
| `:10` | the first ten |
| `-1` | the newest row |
| `-5:` | the newest five |

A sequence number starts at 1 and a negative one counts back from the tail, so `0` is a
typo either way and is refused. A range past the end is fewer rows, not an error. Each
row arrives as the event it was published as, before the result, and the result carries
`last_seq` (where the transcript ends) and `status`.

`replay: "true"` is the window's reading: rows as a window draws them, the operator's
own messages as input lines, and the answer still being written. A person reading does
not need it; anything that waits for an answer does.

```bash
gongga-cli '{"name":"get_messages","args":{"session":"mentor","range":"-5:"}}'
```

### `events` -- the server's event log as a cursor

| argument | spelling | required | meaning |
|---|---|---|---|
| `since` | named (`--since`) | no | rows after this log id, exclusive; omitted is from the beginning |
| `kind` | named (`--kind`) | no | only this kind, e.g. `turn_end`, `tool_result`, `step_timing` |
| `session` | named (`--session`), kind `session` | no | only rows about this session; rows that belong to no session are then left out |
| `limit` | named (`--limit`) | no | at most this many rows, oldest first; default `200`, at most `5000` |

Each row arrives as a log record `{id, seq, session, kind, payload, at}`, and the result
carries `{count, last_id, since}`. Continue by passing the `last_id` you last saw as
`since`. There is no live tail here: a wait on what happens next is a `subscribe`.

```bash
gongga-cli '{"name":"events","args":{"since":"4711","kind":"turn_end","session":"work/child"}}'
```

## Subscriptions

A subscription names a pattern over events and the one command a match runs. Arming one
answers at once; it never waits for a match.

### `subscribe` -- arm a subscription

| argument | spelling | required | meaning |
|---|---|---|---|
| `pattern` | positional (`<pattern>`) | yes | an unanchored regular expression over event names |
| `cmd` | positional (`<cmd>`) | yes | the command a match runs: a name, or the whole call as an object |
| `session` | positional | no | the session a match runs in; omitted is the calling session |
| `id` | named (`--id`) | no | the name it is armed and dropped by; arming a name again replaces what it held. Omitted is a free name, reported in the answer |
| `persistant` | named (`--persistant`) | no | `true` keeps it armed after a match, so every later match is news too; without it one match is the whole subscription |

The pattern is matched against an event's name -- a log row's `kind`, or one of the
stream's own names (`status`, `usage`, `sessions`, `log`, `cmd_result`) -- and against
`name:session` when the event is about a session, so `turn_end:work/child` is that one
session's turn ending. Anchors are yours to write: `^turn_end$` is exactly one name.

`persistant` is spelled this way in the argument and in `ls_subscription`'s answer; it
is not a typo to correct.

The answer:

```json
{"id": 3, "session": "work/child", "subscription": "done",
 "event": "turn_end:work/child", "cmd": "send_message", "persistant": false}
```

```bash
gongga-cli '{"name":"subscribe","args":{"pattern":"^turn_end:work/child$","cmd":{"name":"send_message","args":{"session":"work/parent","message":"child finished"}},"id":"done"}}'
```

A match runs the command in the named session through the same layer a tool call uses,
so anything in this file can be the action.

### `ls_subscription` -- a session's armed subscriptions

| argument | spelling | required | meaning |
|---|---|---|---|
| `path` | positional | no | a name prefix; omitted is every one the session has |
| `session` | named (`--session`), kind `session` | yes | whose subscriptions |

Unlike `subscribe` and `del_subscription`, this one has **no caller fallback**: the
session must be named, because it lists for somebody specified. The answer is a JSON
array in the `message`: `subscription`, `pattern`, `persistant`, `action`.

```bash
gongga-cli '{"name":"ls_subscription","args":{"session":"work/child"}}'
```

### `del_subscription` -- drop one, or every one under a path

| argument | spelling | required | meaning |
|---|---|---|---|
| `path` | positional (`<path>`) | yes | the name it was armed by, or the path whose subscriptions all go |
| `session` | named (`--session`), kind `session` | no | the session that armed it; omitted is the calling session |

A path that matches nothing is a refusal, not silence.

```bash
gongga-cli '{"name":"del_subscription","args":{"path":"done","session":"work/child"}}'
```

### `trigger_event` -- publish an event under a name of your choosing

| argument | spelling | required | meaning |
|---|---|---|---|
| `event` | positional (`<event>`) | yes | the name it is published under; every pattern matching it fires |

The row lands in the event log like any other, which is how a matcher sees it. Its
payload is empty.

```bash
gongga-cli '{"name":"trigger_event","args":{"event":"work/child:built"}}'
```

## Paths

### `list_paths` -- one directory on a machine

| argument | spelling | required | meaning |
|---|---|---|---|
| `remote` | named (`--remote`) | no | which machine's filesystem; omitted is this one |
| `path` | named (`--path`) | no | what to list, as that machine spells it; empty is where its shell starts |

For a front-end completing a `cwd`; an agent reads its own shell's directories better by
running `ls` in it. The answer is `{path, remote, entries: [{name, dir}, ...]}`.

```bash
gongga-cli '{"name":"list_paths","args":{"path":"C:/resource"}}'
```

## The one convenience: `ask`

**Another session -- a mentor, a parent -- holds what you have already forgotten.**
Searching again is that whole search paid twice; one question is one turn. `ask` is the
only thing this skill publishes, and it does both halves in one call: it puts the
question in the other session's hands with `send_message`, then waits for that session's
turn to end and answers with what it said.

The answer is read from the transcript the way a window reads it (`get_messages` with
`replay: "true"`): the operator's line and the answer's own lines are distinct rows, so
there is no guessing about who said what. The cursor is taken *before* the question is
sent, so the answer has to come after the question's own row, never from a turn that
ended earlier.

```python
import sys
sys.path.append(SKILL_DIR)   # the directory this file is in: `@skill(...)` prints its path
from ask_session import ask
answer = ask("mentor", "how did I write the snapshot step?")   # the answer, or a refusal
```

```bash
SKILL_DIR=<the directory holding this file>   # the parent of the path in `@skill(...)`
"$SKILL_DIR/ask_session.py" mentor "how did I write the snapshot step?"
```

In a window the shorter form is the `skill` line -- `skill self-control ask mentor
"<question>"` -- where the first word names the function this package publishes and the
rest bind to its parameters.

- **The session is the first argument and the question follows it.** Not an accident:
  `send_message` takes its text first, so a form that put the session last would send the
  session name as the message, to an empty session name.
- **Ask something specific.** Say what you want answered and why now. "Is this fine?"
  earns one word; the step that has you stuck earns an answer.
- **Timeout**: 900 seconds by default (`--timeout <seconds>` in the script form).
- **Exit**: `0` the answer was received and printed, `2` the question could not be asked
  or no answer arrived in time. The package form raises `RuntimeError` instead.

The answer also reaches your own session as a message (`send_message` goes to the other
session, and its `send_message` comes back), so you can go on working and read it at the
next step boundary. `ask` is that wait done for you, first.

## The Python client inside a script

**The server writes a client into its home, and a script imports it:**
`<home>/lib/gongga.py` (`docs/notes/server-pylib.md`). One method per command, one
`Reply` per answer, an exception when something is refused. `_client.py` beside this file
finds that module -- `GONGGA_SKILL_LIB` from a skill runner, `GONGGA_HOME` from a shell
the server started -- and imports it:

```python
import _client
server = _client.connect()                      # or catch _client.Unusable
reply = server.call("get_messages", session="mentor", range="-1:-1", replay="true")
reply.ok, reply.message, reply.data, reply.events
reply.raise_for_status()                        # a refusal becomes gongga.GonggaError
gongga = _client.module()                       # the client module: exceptions, connect()
server.close()                                  # or: with gongga.connect() as server:
```

| what you get | what it is |
|---|---|
| `reply.ok` / `.message` / `.data` / `.events` | ran or refused, the server's own words, its structured answer, the events seen during the call |
| `reply.raise_for_status()` | a refusal, raised as `gongga.GonggaError` carrying the server's sentence |
| `server.<command>(**arguments)` | the same call by method name, for the names in the greeting's catalog |
| `server.catalog` / `server.sessions` / `server.models` | the published commands, the session rows, the usable roster |
| `gongga.Unreachable` | nothing answered at all (no server, wrong key), which is not a refusal |

**No `subprocess` plus `gongga-cli` inside a skill's script.** The command line is the
shape a person reads; a script wants the return value, and `Reply` already separates
success, refusal, data and events. Re-parsing printed JSON is a second copy of one
contract, and the copy is the one that falls behind.
