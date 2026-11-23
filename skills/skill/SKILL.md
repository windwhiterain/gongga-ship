---
name: skill
description: How a call reaches a skill — SPEC's `Cmd.skill` field, which every command carries, and `ls_skill`, which lists every skill one session can open. Read it before writing a skill call by guess — what `skill` names and why `harness` is a bucket while `har` is nothing, what a row contains, why the session is the caller's and never an argument, how a call's arguments reach the function, which skills are read instead of run, and what each refusal sentence means.
---

# One skill namespace, one field that addresses it

SPEC's `## Python Cmd` gives skills a namespace, and `## Tool` gives every call the field
that addresses it:

```python
Skill := path # abstract naming space
fn ls_skill(path: path, session: Session)
Cmd := {name: str, args: dict[str, any], skill: Skill = none}
```

`ls_skill` answers **what exists and what to call it by**. A call with `skill` set **runs
one of them**: `name` is the function the package publishes, `args` are that function's
keyword arguments, and the answer is what it printed with the exit code it left with.
`skill` omitted is a command the server answers itself. There is no command that wraps
another call — the field is the whole door.

Nothing else re-reads the namespace: an `@skill(...)` reference, `ls_skill` and a skill
call all walk the same roots and print the same name, so a name one lists is a name the
other takes. A skill that is a Python package can also be imported from a skill's own
Python — same functions, same binding rules — which is what SPEC's `## Skill` means by
"API can be called by harness cmd or python import".

The session a skill call runs as is **the caller's**, never an argument: the shell's
`$GONGGA_SESSION`, the model's own session. That session is not a label: its directory
**is** the `project` layer, and the machine it stands on decides whether that layer is
read at all. A function that takes a `session` parameter (`self-control`'s `ask`) means
something else by it — the session it asks — and receives it as its own argument.

## The three buckets, and the name a row carries

| bucket | whose | where its skills live | installed by |
| --- | --- | --- | --- |
| `project` | this checkout | `.agents/skills` inside the session's own directory | the repository, so it travels with the code |
| `harness` | this harness's content | the home's `skills` | the content repository, at the newest commit whose `protocol` this build reads |
| `general` | this machine | `~/.agents/skills` | the community clone, plus anything you put there yourself |

**Nearer wins.** `project` beats `harness` beats `general`, in that order, and it is the
same order `@skill(...)` expands in: a name installed in more than one layer is the first
one read. Every skill is a directory that holds a `SKILL.md`; its `name` is the
frontmatter's `name:` when it has one and the directory's name otherwise.

A `skill` is that `name`, and nothing above it. Which layer a row came from is a **field of
the row** (`bucket`), not part of the address: a caller holding a name should not have to
know which of three directories answers to it. So `"skill": "self-control"` is the whole
spelling — `ls_skill` prints the name, the call takes it back unchanged. A name two
layers both hold is two rows with one name; the call runs the nearer one, which is the
same row `@skill(...)` reads, and `ls_skill <bucket>` shows both.

The name is the whole address: `harness/file` is not a longer spelling of `file`, it is a
name nobody has, and it is refused with the list `ls_skill` prints. That is what lets a
call name a skill without knowing which layer answered — and it is why `ls_skill` takes a
bucket's own word (`harness`) as a **filter** instead: where a row came from is worth
looking at, not worth typing into a call.

## ls_skill

```
ls_skill(path?, session)      # server command
```

| argument | positional | required | what it is |
| --- | --- | --- | --- |
| `path` | first | **no** | where to look: a skill's name, or a bucket's word (`harness`, `general`, `project`), or nothing for every skill there is |
| `session` | second | **yes** | whose skills: its directory is the `project` layer, and the machine it stands on decides whether that layer is read at all |

`path` is matched **by component, not by string**: `a/b` is under `a`, `ab` is not, and an
empty `path` is every path. So `harness` is the whole home layer, `self-control` is one
row, `harness/` works as well (a trailing slash is stripped), and `har` matches nothing.


The answer is a JSON array with one object per skill:

```json
[{"bucket": "harness",
  "description": "one line saying what it is for: the frontmatter's",
  "name": "prefix-cache",
  "path": "<absolute path of that SKILL.md>"}]
```

Copy the `name` into a call's `skill`; open the `path` to read the body. The `path` column
is the only place a root appears as a real directory, which is why nothing else here needs
to spell one: a home moves, a bucket does not.

Two things worth knowing about the answer:

- **Nothing matched is a sentence, not an empty array**: `main: no skill matches "har"`.
- A session that does not exist is an error. A session whose directory is on another machine
  is not: it simply has no `project` rows, because that checkout is not on this disk.

## A skill call

```
{"name": "<function>", "skill": "<name>", "args": {...}}
```

| field | required | what it is |
| --- | --- | --- |
| `name` | **yes** | the function the package publishes |
| `skill` | **yes** for a skill call | the skill, spelled the way `ls_skill` spells it: its name |
| `args` | no | the function's named parameters, an object; omitted, the function takes none |

`args`' keys are the function's parameter names and their values are the arguments
themselves, so order does not matter and nothing is re-spelled as `--name value`:

- `{"name": "new_subagent", "skill": "subagent", "args": {"config": {...}, "role": "impl"}}`.

From a shell it is one `gongga-cli` call, and the call is the same object:
`gongga-cli '{"name":"ask","skill":"self-control","args":{"session":"<another session>","question":"<what to ask>"}}'`.
The session it runs as is the shell's own (`$GONGGA_SESSION`); the `session` inside `args`
here is `ask`'s own parameter, the session it asks. Nothing on either path reads a flag
back: the arguments are a function's parameters, not a program's flags.

The server publishes one catalog row per function a package skill publishes, and the row
carries its `skill`. So an operator's line `/ask{"session":"...","question":"..."}`
completes from the function's own arguments, and the client fills `skill` from the row —
the wire still states which skill answered. A `skill` the call states wins over the row's.

A package is asked in one request, and that request is the contract: `name` is the
function, `args` is an object of its named parameters, and the harness hands both to
`lib/gongga_skill.py` on stdin — `{"skill": "<the package's directory>", "function":
"<name>", "args": {...}}`, with no command line at all. `lib/gongga_skill.py` (this
repository's `python/gongga_skill.py`) is the contract both readers share, and it is worth
reading once, because the binding rules are the part a caller otherwise guesses:

- an argument arrives as the value it was written as: an object stays an object, a list a
  list, `true` is a `bool`, `null` is `None`, `3` is the number. No annotation converts
  anything, so a parameter annotated `dict` is handed the dict itself.
- a name that is no parameter of the function is refused with the signature it does have, and
  a required parameter left out is refused in Python's own words — either way as one line,
  not as a traceback. A name the package does not publish is refused with the list of what it
  does publish.
- which names a package publishes: everything its `__init__.py` defines at global scope that
  is callable and does not begin with `_`. An imported function counts as published too, so a
  helper stays out of the API by being named `_helper`, or by importing its module and
  calling it through the module. `__all__` is not consulted.
- the shared arguments every call carries (`description`, `max_output`) are the call
  layer's own and are taken off before the function binds: a function has no parameter of
  those names.

What the function answers is what this call answers: `None` means it ran with nothing to
print, a string prints as it is, an `int` is an exit code, and anything else is printed as
JSON. Either way the call ends with `[exit status <n>]`, and a non-zero code is the skill
failing, not this command. Output is capped like any other tool result.

### Which skills it accepts, and what each refusal means

A skill call runs a **package**: the function `name` names, with `args` as its keyword
arguments. A directory with no `__init__.py` is a **body** -- a `SKILL.md` and nothing to
start -- and is refused, because there is nothing to run and reading is what it is for.
The row `ls_skill` prints does not say which a skill is; its `SKILL.md` says, and each
refusal repeats it in the caller's terms. The sentences, as the server writes them, so a
reader can tell which mistake was made:

| answer | what to do about it |
| --- | --- |
| `no skill is "<name>" here; ls_skill lists <names>` | the `skill` names no row for that session: a wrong name, or a layer this machine does not read. Run `ls_skill` and copy a row it printed. |
| `<name> runs only as a Python call: make it a package -- a directory holding `__init__.py`, whose public functions are the names a skill call takes ... <path> is the prose that describes it` | there is nothing to start: open that `SKILL.md` and do what the prose says. This file is one. |
| `<function> runs as the session that called it: run it from a session's shell, where `$GONGGA_SESSION` names one` | the call came from a client that named no session. Export `GONGGA_SESSION`, or call it from a session's own shell. |
| `<name> has no function "<fn>"; it publishes: ...` | right package, wrong function: take a name from the list it prints. |
| `[exit status <n>]` under `ok: false` | the skill ran and failed. Its own output is above that line, and the code is the skill's, not this command's. |

## Two shapes of skill, two ways to use them

| shape | what makes one | how you use it |
| --- | --- | --- |
| **body** | only a `SKILL.md`: no `__init__.py` | read the `path` `ls_skill` gives and do what the prose says. A skill call refuses it, naming that path. This file is one. |
| **package** | an `__init__.py` in the skill's own directory | a call with `skill` set runs the function `name` names, with `args` as its keyword arguments; a skill's own Python can import the package instead. |

There is one form, not three: SPEC's `## Skill` makes every skill a Python call, so a
skill with no package is a skill nothing can run. An implementation that is not Python
goes inside a function of one.

### A skill must not block

A call to a skill holds the turn it was asked in, for as long as the function runs. Waiting
is `exec`'s (its channel's `wait`/`until`) and the `wait` tool's; a skill that sits on a
clock -- polling a transcript, sleeping until something arrives -- holds that turn while
nothing about the turn moves, and a message arriving ends it rather than being read by it.

Measured: a skill that polled another session's transcript held a turn for 97 s, and the
very message it waited for is what ended the call.

**This is a convention, not a check.** The command layer has an internal clock, but a call
that states a `skill` is exempt from it on purpose (a skill is bounded by the skill runner's
own bound instead -- `CALL_TIMEOUT_MS`, 120 s by default), so nothing refuses a blocking
skill; it just wastes a turn until that bound stops it. A skill that needs something to
happen leaves it to the event stream: arm a subscription (`subscribe`) whose commands
include a `send_message` back to you, and let the server put what happened into it --
`{event.answer}`, `{event.outcome}`. That is `self-control`'s `ask`, and it starts no
program at all. A channel (`new_channel`) is for a program that must run, not for waiting
on a notification.

## Examples

`gongga-cli` takes one JSON object per call, which is the call as it is typed. `ls_skill`
takes the session it lists for; a skill call takes none — it runs as the caller's own.

```bash
# every skill this session can open
gongga-cli '{"name":"ls_skill","args":{"session":"<session>"}}'

# the home layer only, then one row of it
gongga-cli '{"name":"ls_skill","args":{"path":"harness","session":"<session>"}}'
gongga-cli '{"name":"ls_skill","args":{"path":"self-control","session":"<session>"}}'

# a package skill, its arguments by name
gongga-cli '{"name":"ask","skill":"self-control","args":{"session":"<another session>","question":"<what to ask>"}}'

# a function that takes nothing
gongga-cli '{"name":"<function>","skill":"<name>","args":{}}'
```

A package's call is an object of named arguments, never a list, because its arguments are
a function's parameters rather than a program's flags. There is no flag parser on this
path, and every skill this content repository ships is a package.

From a skill's own Python the same calls are methods on the connection (see
`docs/notes/python-client.md`): a name the greeting published as a skill's function carries
its skill already, and `call` takes `skill` for one it did not.

```python
server.ls_skill(path="harness", session="main")
server.new_subagent(config=self_config(), role="impl")          # the row carries its skill
server.call("show", skill="image", paths=["shot.png"])          # or state it
```

## References

- `SPEC.md`: `## Tool` for `Cmd := {name, args, skill}`, `## Python Cmd` for `ls_skill`,
  `## Skill` for a skill being a package callable by command or by import.
- `docs/notes/server-commands.md`: "ls_skill", the skill door, and "How an argument is
  spelled, and what a `path` means" for the component rule.
- `docs/notes/server-tools-skill.md`: the roots, the two shapes, and how a skill is
  started.
- `lib/gongga_skill.py` in the home (this repository's `python/gongga_skill.py`): the
  publishing, binding and answer rules that both readers share.
- `crates/server/src/skill_commands.rs`: the rows a package's functions publish, whose
  argument names and requirements this file's call shape copies.
