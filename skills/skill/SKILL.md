---
name: skill
description: The two skill commands SPEC gives — `ls_skill`, which lists every skill one session can open, and `run_skill`, which runs one skill: the function a Python package publishes. Read it before writing either call by guess — which arguments are required, what an `id` is and why `harness` is a bucket while `har` is nothing, what a row contains, why `session` must be spelled out every time, how a call’s arguments reach the skill, which skills are read instead of run, and what each refusal sentence means.
---

# The two commands over one skill namespace

SPEC's `## Python Cmd` gives skills a namespace and two calls into it:

```python
Skill := path # abstract naming space
fn ls_skill(path: path, session: Session)
fn run_skill(id: Skill, cmd: Cmd, session: Session)
```

`ls_skill` answers **what exists and what to call it by**. `run_skill` **runs one of
them** and answers with what it printed and the exit code it left with. Nothing else
re-reads the namespace: an `@skill(...)` reference and these two commands all walk the same
roots and print the same `id`, so a path one lists is a path the other takes. A skill that is a Python package can also be imported from a skill's own
Python — same functions, same binding rules — which is what SPEC's `## Skill` means by
"API can be called by harness cmd or python import".

Both are server commands, and both take the session they work as. That session is not a
label: its directory **is** the `project` layer, and the machine it stands on decides
whether that layer is read at all.

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

An `id` is that `name`, and nothing above it. Which layer a row came from is a **field of
the row** (`bucket`), not part of the address: a caller holding a name should not have to
know which of three directories answers to it. So `run_skill self-control` is the whole
spelling — `ls_skill` prints the name, `run_skill` takes it back unchanged. A name two
layers both hold is two rows with one name; `run_skill` runs the nearer one, which is the
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

Copy the `name` into `run_skill`; open the `path` to read the body. The `path` column is the
only place a root appears as a real directory, which is why nothing else here needs to
spell one: a home moves, a bucket does not.

Two things worth knowing about the answer:

- **Nothing matched is a sentence, not an empty array**: `main: no skill matches "har"`.
- A session that does not exist is an error. A session whose directory is on another machine
  is not: it simply has no `project` rows, because that checkout is not on this disk.

## run_skill

```
run_skill(id, cmd, session)   # server command; all three required
```

| argument | positional | required | what it is |
| --- | --- | --- | --- |
| `id` | first | **yes** | the skill, spelled the way `ls_skill` spells it: its name |
| `cmd` | second | **yes** | the call: a word on its own, or the whole call as `{"name": "<function>", "args": {...}}` — the function and its arguments |
| `session` | third | **yes** | the session it runs as: its directory is where the package is imported, and the call is that session's work |

`cmd` is SPEC's `Cmd := {name, args}`: a name, then its arguments. Anything that does not
begin with `{` is one word and no more; an object carries a `name` and its `args`, and those
`args` are the function's named parameters:

- `{"name": "new_subagent", "args": {"config": {...}, "role": "impl"}}`. Their keys are
  the function's parameter names and their values are the arguments themselves, so order
  does not matter and nothing is re-spelled as `--name value`.

From a shell it is one `gongga-cli` call, and the call is the same object: `gongga-cli '{"name": "run_skill", "args": {"id": "self-control", "cmd": {"name": "ask", "args": {"session": "<another session>", "question": "<what to ask>"}}, "session": "<session>"}}'`.
A shell is not inside a session the server knows, so it spells `session` out; the
`skill` tool fills it with the caller, which is why the tool's own `cmd` has no
`session` in it. Nothing on either path reads a flag back: the arguments are a function's
parameters, not a program's flags.

A package is asked in one request, and that request is the contract: `cmd.name` is the
function, `cmd.args` is an object of its named parameters, and the harness hands both to
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

What the function answers is what this call answers: `None` means it ran with nothing to
print, a string prints as it is, an `int` is an exit code, and anything else is printed as
JSON. Either way the call ends with `[exit status <n>]`, and a non-zero code is the skill
failing, not this command. Output is capped like any other tool result.

### Which skills it accepts, and what each refusal means

`run_skill` runs a **package**: the function `cmd.name` names, with `cmd.args` as its
keyword arguments. A directory with no `__init__.py` is a **body** -- a `SKILL.md` and
nothing to start -- and is refused, because there is nothing to run and reading is what it
is for. The row `ls_skill` prints does not say which a skill is; its `SKILL.md` says, and
each refusal repeats it in the caller's terms. The sentences, as the server writes them,
so a reader can tell which mistake was made:

| answer | what to do about it |
| --- | --- |
| `no skill is "<id>" here; ls_skill lists <ids>` | the `id` names no row for that session: a wrong name, or a layer this machine does not read. Run `ls_skill` and copy a row it printed. |
| `<name> runs only as a Python call: make it a package -- a directory holding `__init__.py`, whose public functions are the names `run_skill` takes ... <path> is the prose that describes it` | there is nothing to start: open that `SKILL.md` and do what the prose says. This file is one. |
| `run_skill needs a cmd: the words the skill runs with, ...` | `cmd` was empty. Spell the call: a word, or `{"name": ..., "args": ...}`. |
| `<name> has no function "<fn>"; it publishes: ...` | right package, wrong function: take a name from the list it prints. |
| `[exit status <n>]` under `ok: false` | the skill ran and failed. Its own output is above that line, and the code is the skill's, not this command's. |

## Two shapes of skill, two ways to use them

| shape | what makes one | how you use it |
| --- | --- | --- |
| **body** | only a `SKILL.md`: no `__init__.py` | read the `path` `ls_skill` gives and do what the prose says. `run_skill` refuses it, naming that path. This file is one. |
| **package** | an `__init__.py` in the skill's own directory | `run_skill` runs the function `cmd.name` names, with `cmd.args` as its keyword arguments; a skill's own Python can import the package instead. |

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

**This is a convention, not a check.** The command layer has an internal clock, but
`run_skill` is exempt from it on purpose (a skill is bounded by the skill runner's own
bound instead -- `CALL_TIMEOUT_MS`, 120 s by default), so nothing refuses a blocking skill;
it just wastes a turn until that bound stops it. A skill that needs something to happen
leaves it to the event stream: arm a subscription (`subscribe`) whose commands include a
`send_message` back to you, and let the server put what happened into it -- `{event.answer}`,
`{event.outcome}`. That is `self-control`'s `ask`, and it starts no program at all. A
channel (`new_channel`) is for a program that must run, not for waiting on a notification.

## Examples

`gongga-cli` takes one JSON object per call, which is the call as it is typed. Name every
argument: `path` is the first positional and optional, which is exactly the spelling that
cannot be told apart from the required argument that follows it, and `session` is not filled
from a sticky value on these two commands — say it each time.

```bash
# every skill this session can open
gongga-cli '{"name":"ls_skill","args":{"session":"<session>"}}'

# the home layer only, then one row of it
gongga-cli '{"name":"ls_skill","args":{"path":"harness","session":"<session>"}}'
gongga-cli '{"name":"ls_skill","args":{"path":"self-control","session":"<session>"}}'

# a package skill, its arguments by name
gongga-cli '{"name": "run_skill", "args": {"id": "self-control", "cmd": {"name": "ask", "args": {"session": "<another session>", "question": "<what to ask>"}}, "session": "<session>"}}'

# the same call with no arguments: cmd spelled as the bare function name
gongga-cli '{"name": "run_skill", "args": {"id": "<name>", "cmd": "<function>", "session": "<session>"}}'
```

A package's call is an object of named arguments, never a list, because its arguments are
a function's parameters rather than a program's flags. There is no flag parser on this
path, and every skill this content repository ships is a package.

From a skill's own Python the same two commands are methods on the connection (see
`docs/notes/python-client.md`), with the one wire rule that matters here: an argument that is
not a string travels as JSON, which is what makes `{"name": ..., "args": ...}` the object
these commands parse.

```python
server.ls_skill(path="harness", session="main")
server.run_skill(id="subagent",
                 cmd={"name": "new_subagent", "args": {"config": self_config(), "role": "impl"}},
                 session="main")
```

## References

- `SPEC.md`: `## Python Cmd` for the two signatures, `## Skill` for a skill being a package
  callable by command or by import, `## Sticky` for why these two spell `session` outright.
- `docs/notes/server-commands.md`: "ls_skill", "run_skill", and "How an argument is spelled,
  and what a `path` means" for the component rule.
- `docs/notes/server-tools-skill.md`: the roots, the two shapes, and how a skill is
  started.
- `lib/gongga_skill.py` in the home (this repository's `python/gongga_skill.py`): the
  publishing, binding and answer rules that both readers share.
- `crates/server/src/commands.rs`: the two schemas whose argument names, order and
  requirements this file copies.
