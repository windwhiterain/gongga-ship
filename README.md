# gongga ship

Everything the gongga harness *ships*: the default config and the skills. Nothing
here runs by itself -- it is content a harness install reads, edits and replaces.

gongga pins one commit of this repository (`crates/server/ship.toml`) and installs
that commit's content at startup:

| here | installed at | who it belongs to |
| --- | --- | --- |
| `config/config.toml` | `<home>/config.toml` | one harness home |
| `skills/harness/<name>` | `<home>/skills/<name>` | one harness home |
| `skills/general/<name>` | `~/.agents/skills/<name>` | this machine, every home |

The install destination is a working tree, and the install is a git three-way
merge: base = the content of the pin that last installed here, ours = what is on
disk now, theirs = the new pin's content. So an upstream wording fix lands in a
home whose reader had also edited the same file, and a real conflict is reported
rather than overwritten. Nothing in here is force-pushed over a user's sentence.

The connection key is deliberately absent: it lives in `<home>/server.key`, which
no tool here ever reads or rewrites. That separation is what makes `config.toml`
safe to be a merge target at all.

## Two buckets, and why they are not one

A harness skill speaks about *this server*: `compact` folds rows out of gongga's
database, `evolve` replaces the process it is running on, `image` uses the
harness's own attachment channel, `self-control` governs the turn. Those belong to
a home, because a home is the thing they act on -- and because two harnesses of
different versions on one machine should not be reading one copy.

A general skill speaks about the world outside: `remote` ssh's to another box,
`proxy` and `latency` reason about the network this machine has, `computer_use`
drives a desktop, `web-search` reads a provider's REST endpoint. Those belong to
the machine, next to `llm.toml` and `~/.agents/agents.md`, which the harness also
reads rather than owns.

Skills are found by the `index_skill` tool, which lists name, description, path
and bucket from both roots. Do not grep the directories: that re-implements the
tool in every session.

## Paths inside a skill

A skill body must never assume it lives in a repository. Write the install path:

- harness bucket: `$GONGGA_HOME/skills/<name>/...` -- the harness exports
  `GONGGA_HOME`, `GONGGA_CLI` and `GONGGA_SERVER` into every shell it runs.
- general bucket: `~/.agents/skills/<name>/...`

Both spellings are runnable exactly as written, which is the only rule that keeps
a skill honest after a session's cwd has moved somewhere else.

## Changing what a running harness sees

An edit here is invisible until the pin moves: the harness installs the commit it
was built with. Bump `crates/server/ship.toml` in gongga to ship a change. Nothing
recompiles -- the pin is data -- but gongga restarts to pick it up, because a
running server already installed the old one.
