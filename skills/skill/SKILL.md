---
name: skill
description: The two skill commands SPEC gives — `ls_skill`, which lists every skill one session can open as `<bucket>/<name>` rows, and `run_skill`, which calls one function of a skill that is a Python package. Read it before writing either call by guess — which arguments are required, why `harness` is a subtree while `har` is nothing, what a row contains, why `session` must be spelled out every time, which skills are read instead of run, which are started by the `skill` tool instead, and what each refusal sentence means.
---

# The two commands over one skill namespace

SPEC's `## Python Cmd` gives skills a namespace and two calls into it:

```python
Skill := path # abstract naming space
fn ls_skill(path: path, session: Session)
fn run_skill(id: Skill, cmd: Cmd, session: Session)
```

`ls_skill` answers **what exists and what to call it by**. `run_skill` **calls one
function of one of them** and answers with what the function returned. Nothing else
re-reads the namespace: the `skill` tool's table, an `@skill(...)` reference and these two
commands all walk the same roots and print the same `id`, so a path one lists is a path the
other takes. A skill that is a Python package can also be imported from a skill's own
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
this reason — a path names one row, a name may name two, and the `skill` tool refuses to
pick between them ("is installed more than once and is never resolved silently").

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
| `cmd` | second | **yes** | the call: a function name on its own, or the whole call as `{"name": "<function>", "args": {...}}` |
| `session` | third | **yes** | the session it runs as: its directory is where the package is imported, and the call is that session's work |

`cmd` is the same shape `subscribe` uses to name the command a match runs, and the same
parser reads it: anything that does not begin with `{` is one function name with no
arguments; an object needs a `name`, and each of its `args` entries reaches the function as
`--key value`.

That is the whole translation: `run_skill` builds the words
`["<function>", "--<key>", "<value>", ...]` and hands them to the `skill` tool, which
imports the package from its own directory and binds those words to the function's
parameters. `python/gongga_skill.py` is the contract both readers share, and it is worth
reading once, because the binding rules are the part a caller otherwise guesses:

- a value arrives as a **word**, never as a Python object: an argument written `3` arrives
  as `"3"`, a list or an object arrives as their JSON text. Only a parameter annotated
  `int`, `float` or `bool` is converted; anything else is handed over as written.
- `--key value` fills a parameter by name, which is why the order of an `args` object does
  not matter, and why a word that itself begins with `--` can still be passed.
- a parameter that cannot be filled is refused with the function's signature, and a name the
  package does not publish is refused with the list of what it does publish — not with a
  traceback.
- which names a package publishes: everything its `__init__.py` defines at global scope that
  is callable and does not begin with `_`. An imported function counts as published too, so a
  helper stays out of the API by being named `_helper`, or by importing its module and
  calling it through the module. `__all__` is not consulted.

What the function answers is what this call answers, rendered the way `skill` renders an
entry: `None` means it ran with nothing to print, a string prints as it is, an `int` is an
exit code, and anything else is printed as JSON. Output is capped like any other tool result.

### Which skills it accepts, and what each refusal means

`run_skill` calls functions, so its target has to be a **Python package**: a skill whose own
directory holds an `__init__.py`. A body has no functions to choose between, and a script
entry has exactly one thing to run, so both are refused here and pointed at the door that
opens — the `skill` tool, which starts what a skill declares. The row `ls_skill` prints does
not say which of the three shapes a skill has; `SKILL.md` says, and each refusal repeats it
in the caller's terms. The sentences, as the server writes them, so a reader can tell which
mistake was made:

| answer | what to do about it |
| --- | --- |
| `no skill is "<id>" here; ls_skill lists <ids>` | the `id` names no row for that session: wrong bucket, wrong name, or a layer this machine does not read. Run `ls_skill` and copy a row it printed. |
| ``<id> is not a Python package with functions: skill --name <name> starts what it declares`` | the skill is a body or a script entry. Read its `path` if it is a body; start its declared entry with those same words if it is a script. |
| `run_skill calls a function: run_skill <id> {"name": "<function>", "args": {}}` | `cmd` was empty: the call has to name the function. |
| `skill` answering `<name> has no function "<fn>"; it publishes: ...` | the package is right and the name is wrong; take one from the list it prints. |

## Three shapes of skill, three ways to use them

| shape | what makes one | how you use it |
| --- | --- | --- |
| **body** | only a `SKILL.md`: no `__init__.py`, no `metadata.entry` | read the `path` `ls_skill` gives and do what the prose says. `skill` on it answers "is a body and nothing to run: read `<path>` and do what it says". This file is one. |
| **entry** | `metadata.entry:` in the frontmatter, naming a file inside that skill's own directory | the `skill` tool starts that file with your words as argv. `run_skill` refuses it and names this call instead. |
| **package** | an `__init__.py` in the skill's own directory | `run_skill` one of its functions, or import it from a skill's own Python. |

The two forms can sit in one directory, and each caller picks differently, so say it
plainly: `run_skill` refuses a skill whose frontmatter declares an entry, even when that
directory also holds an `__init__.py`; the `skill` tool, which is what starts it, runs the
package whenever `__init__.py` is there, entry declared or not. Its reason, written in
`crates/skill/src/entry.rs`, is that discovering the functions is the point of that form. So
a skill that wants its functions reachable by `run_skill` declares no entry.

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

# one function of a package skill: the whole call, its arguments by name
gongga-cli '{"name":"run_skill","args":{"id":"general/model_pool","cmd":{"name":"pick_config","args":{"tier":"flash"}},"session":"<session>"}}'

# the same call with no arguments: cmd spelled as the bare function name
gongga-cli '{"name":"run_skill","args":{"id":"<bucket>/<name>","cmd":"<function>","session":"<session>"}}'
```

The `model_pool` call is the shape to aim at, and it also shows the table above working: as
installed, that skill declares an entry script and a set of modules rather than an
`__init__.py`, so the answer comes back
`general/model_pool is not a Python package with functions: skill --name model_pool starts
what it declares`. The words are right for that entry — `pick_config`, `--tier flash` are its
subcommand and one of its flags — and its own `SKILL.md` is what says which words it takes;
they just reach it through `skill`, not through `run_skill`.

From a skill's own Python the same two commands are methods on the connection (see
`docs/notes/python-client.md`), with the one wire rule that matters here: an argument that is
not a string travels as JSON, which is what makes `{"name": ..., "args": ...}` the object
these commands parse.

```python
server.ls_skill(path="harness", session="main")
server.run_skill(id="general/model_pool",
                 cmd={"name": "pick_config", "args": {"tier": "flash"}},
                 session="main")
```

## References

- `SPEC.md`: `## Python Cmd` for the two signatures, `## Skill` for a skill being a package
  callable by command or by import, `## Sticky` for why these two spell `session` outright.
- `docs/notes/server-commands.md`: "ls_skill", "run_skill", and "How an argument is spelled,
  and what a `path` means" for the component rule.
- `docs/notes/server-tools-skill.md`: the table, the roots, and how an entry is started.
- `python/gongga_skill.py`: the publishing, binding and answer rules that both readers share.
- `crates/server/src/commands.rs`: the two schemas whose argument names, order and
  requirements this file copies.
