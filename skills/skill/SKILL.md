---
name: skill
description: The two skill commands SPEC gives — `ls_skill`, which lists every skill one session can open as `<bucket>/<name>` rows, and `run_skill`, which runs one skill: the function a Python package publishes, or the entry a script skill declares. Read it before writing either call by guess — which arguments are required, why `harness` is a subtree while `har` is nothing, what a row contains, why `session` must be spelled out every time, how a call’s arguments reach the skill, which skills are read instead of run, and what each refusal sentence means.
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

## The three buckets, and the id that names exactly one row

| bucket | whose | where its skills live | installed by |
| --- | --- | --- | --- |
| `project` | this checkout | `.agents/skills` inside the session's own directory | the repository, so it travels with the code |
| `harness` | this harness's content | the home's `skills` | the content repository, at the newest commit whose `protocol` this build reads |
| `general` | this machine | `~/.agents/skills` | the community clone, plus anything you put there yourself |

**Nearer wins.** `project` beats `harness` beats `general`, in that order, and it is the
same order `@skill(...)` expands in: a name installed in more than one layer is the first
one read. Every skill is a directory that holds a `SKILL.md`; its `name` is the
frontmatter's `name:` when it has one and the directory's name otherwise.

An `id` is `<bucket>/<name>` — SPEC's `Skill := path` spelled out. The bucket is part of
the word because a name is not always enough: two layers can hold the same name, and those
are two different skills. `run_skill` takes the `id` and never the bare name for exactly
this reason: a path names one row, a name may name two, and `ls_skill` prints both rows
rather than hiding one behind the other.

## ls_skill

```
ls_skill(path?, session)      # server command
```

| argument | positional | required | what it is |
| --- | --- | --- | --- |
| `path` | first | **no** | where to look, as the beginning of an `id`: `harness`, or `harness/self-control`, or nothing for every skill there is |
| `session` | second | **yes** | whose skills: its directory is the `project` layer, and the machine it stands on decides whether that layer is read at all |

`path` is matched **by component, not by string**: `a/b` is under `a`, `ab` is not, and an
empty `path` is every path. So `harness` is the whole home layer, `harness/self-control` is
one row, `harness/` works as well (a trailing slash is stripped), and `har` matches nothing.
An `id` is `<bucket>/<name>`, two components, so a third spelling
(`harness/self-control/anything`) matches no row.

The answer is a JSON array with one object per skill:

```json
[{"bucket": "harness",
  "description": "one line saying what it is for: the frontmatter's",
  "id": "harness/prefix-cache",
  "name": "prefix-cache",
  "path": "<absolute path of that SKILL.md>"}]
```

Copy the `id` into `run_skill`; open the `path` to read the body. The `path` column is the
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
| `id` | first | **yes** | the skill, spelled the way `ls_skill` spells it: `<bucket>/<name>` |
| `cmd` | second | **yes** | the call: a word on its own, or the whole call as `{"name": "<function>", "args": {...}}` — the function and its arguments, or an entry’s argv |
| `session` | third | **yes** | the session it runs as: its directory is where the package is imported, and the call is that session's work |

`cmd` is SPEC's `Cmd := {name, args}`: a name, then its arguments. Anything that does not
begin with `{` is one word and no more; an object carries a `name` and its `args`, and those
`args` are decided by the shape of the skill:

- **a package** takes an object: `{"name": "new_subagent", "args": {"config": {...},
  "role": "impl"}}`. Its keys are the function's parameter names and its values are the
  arguments themselves, so order does not matter and nothing is re-spelled as `--name
  value`.
- **an entry** takes a list: `["pick_config", "--tier", "flash"]`. Every item is one word
  of the script's argv as written -- a subcommand and its flags are that script's own
  spelling, which its `SKILL.md` documents.

A package is asked in one request, and that request is the contract: `cmd.name` is the
function, `cmd.args` is an object of its named parameters, and the harness hands both to
`lib/gongga_skill.py` on stdin — `{"skill": "<the package's directory>", "function":
"<name>", "args": {...}}`, with no command line at all. For an entry skill there is nothing
to bind: every word is the script's argv. `lib/gongga_skill.py` (this repository's
`python/gongga_skill.py`) is the contract both readers share, and it is worth reading once,
because the binding rules are the part a caller otherwise guesses:

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
JSON. An entry skill's answer is simply what it printed. Either way the call ends with
`[exit status <n>]`, and a non-zero code is the skill failing, not this command. Output is
capped like any other tool result.

### Which skills it accepts, and what each refusal means

`run_skill` runs whatever the row it names is: a **package** runs the function `cmd.name`
names, with `cmd.args` as its keyword arguments; an **entry** skill starts the file its
frontmatter declares, with the words of `cmd.args` as its argv; and a **body** -- a
`SKILL.md` and neither of the other two -- is refused, because there is nothing to start
and reading is what it is for. The row `ls_skill` prints does not say which of the three a
skill is; its `SKILL.md` says, and each refusal repeats it
in the caller's terms. The sentences, as the server writes them, so a reader can tell which
mistake was made:

| answer | what to do about it |
| --- | --- |
| `no skill is "<id>" here; ls_skill lists <ids>` | the `id` names no row for that session: wrong bucket, wrong name, or a layer this machine does not read. Run `ls_skill` and copy a row it printed. |
| `<name> is a body and nothing to run: read <path> and do what it says` | there is nothing to start: open that `SKILL.md` and do what the prose says. This file is one. |
| `run_skill needs a cmd: the words the skill runs with, ...` | `cmd` was empty. Spell the call: a word, or `{"name": ..., "args": ...}`. |
| `<name> has no function "<fn>"; it publishes: ...` | a package skill, right package, wrong function: take a name from the list it prints. |
| `[exit status <n>]` under `ok: false` | the skill ran and failed. Its own output is above that line, and the code is the skill's, not this command's. |

## Three shapes of skill, three ways to use them

| shape | what makes one | how you use it |
| --- | --- | --- |
| **body** | only a `SKILL.md`: no `__init__.py`, no `metadata.entry` | read the `path` `ls_skill` gives and do what the prose says. `run_skill` refuses it, naming that path. This file is one. |
| **entry** | `metadata.entry:` in the frontmatter, naming a file inside that skill's own directory | `run_skill` starts that file, and `cmd`'s words are its argv -- no shell, so a word holding a space, a quote or a `$` arrives whole. |
| **package** | an `__init__.py` in the skill's own directory | `run_skill` runs the function `cmd.name` names, with `cmd.args` as its keyword arguments; a skill's own Python can import the package instead. |

A directory can hold both a declared entry and an `__init__.py`. `run_skill` runs the
**package** then, because discovering the functions is the point of that form (the reason is
written in `crates/skill/src/entry.rs`), and the declared entry is what the local runner
`gongga-cli skill <name>` starts. A skill that wants one answer declares one form.

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
gongga-cli '{"name":"ls_skill","args":{"path":"harness/self-control","session":"<session>"}}'

# an entry skill: every word of cmd is the entry's argv
gongga-cli '{"name":"run_skill","args":{"id":"general/model_pool","cmd":{"args":["pick_config","--tier","flash"]},"session":"<session>"}}'

# a package skill, its arguments by name
gongga-cli '{"name":"run_skill","args":{"id":"harness/self-control","cmd":{"name":"ask","args":{"session":"<another session>","question":"<what to ask>"}},"session":"<session>"}}'

# the same call with no arguments: cmd spelled as the bare function name
gongga-cli '{"name":"run_skill","args":{"id":"<bucket>/<name>","cmd":"<function>","session":"<session>"}}'
```

The `model_pool` call is the shape to aim at for an entry: as installed it declares a script
rather than an `__init__.py`, so `pick_config --tier flash` are the words that entry is
started with — a subcommand and one of its flags, which its own `SKILL.md` is what documents.
A package is the other shape: an object of named arguments, never a list, because its
arguments are a function's parameters rather than a program's flags.

From a skill's own Python the same two commands are methods on the connection (see
`docs/notes/python-client.md`), with the one wire rule that matters here: an argument that is
not a string travels as JSON, which is what makes `{"name": ..., "args": ...}` the object
these commands parse.

```python
server.ls_skill(path="harness", session="main")
server.run_skill(id="general/model_pool",
                 cmd={"args": ["pick_config", "--tier", "flash"]},
                 session="main")
```

## References

- `SPEC.md`: `## Python Cmd` for the two signatures, `## Skill` for a skill being a package
  callable by command or by import, `## Sticky` for why these two spell `session` outright.
- `docs/notes/server-commands.md`: "ls_skill", "run_skill", and "How an argument is spelled,
  and what a `path` means" for the component rule.
- `docs/notes/server-tools-skill.md`: the roots, the three shapes, and how a skill is
  started.
- `lib/gongga_skill.py` in the home (this repository's `python/gongga_skill.py`): the
  publishing, binding and answer rules that both readers share.
- `crates/server/src/commands.rs`: the two schemas whose argument names, order and
  requirements this file copies.
