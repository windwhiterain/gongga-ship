# gongga ship

Everything the gongga harness *ships*: the default config and the skills. Nothing
here runs by itself -- it is content a harness install reads, edits and replaces.

gongga pins one commit of this repository (`crates/server/ship.toml`) and installs
that commit's content at startup:

| here | installed at | who it belongs to |
| --- | --- | --- |
| `config/config.toml` | `<home>/config.toml` | one harness home |
| `skills/<name>` | `<home>/skills/<name>` | one harness home |

One level: a skill directory here is a skill directory there. The machine's skills
are not installed from here at all -- they are the community clone's, which
`agents::link_skills` links into `~/.agents/skills`. See `## Two buckets` below.

The install destination is a working tree, and the install is a git three-way
merge: base = the content of the pin that last installed here, ours = what is on
disk now, theirs = the new pin's content. So an upstream wording fix lands in a
home whose reader had also edited the same file, and a real conflict is reported
rather than overwritten. Nothing in here is force-pushed over a user's sentence.

The connection key is deliberately absent: it lives in `<home>/server.key`, which
no tool here ever reads or rewrites. That separation is what makes `config.toml`
safe to be a merge target at all.

## Two buckets, and why they are not one

A harness skill speaks about *this server*: `heir` trains a successor from the
current transcript and its mentor session, `image` uses the harness's own
attachment channel, `self-control` governs the turn, `subagent` hands work to
another session. Those belong to a home, because a home is the thing they act
on -- and because two harnesses of different versions on one machine should not
be reading one copy.

`evolve` is not here, although it is a harness skill: it walks up from its own
path to a `Cargo.toml`, builds the workspace and stages a binary under `.dev/`.
Installed into `$GONGGA_HOME` it would find no repository, so it lives in the
gongga checkout's own `.agents/skills/evolve/` instead -- content that travels
with the code it acts on, not with a pin.

A general skill speaks about the world outside: `remote` ssh's to another box,
`proxy` and `latency` reason about the network this machine has, `computer_use`
drives a desktop, `web-search` reads a provider's REST endpoint. Those belong to
the machine, next to `providers.toml`, which the harness reads rather than owns. A
standing rule about this machine is a skill here too: the harness reads no rules
file into a prompt, so prose that is always true is still prose somebody has to
ask for.

Skills are found by the `index_skill` tool, which lists name, description, path
and bucket from both roots. Do not grep the directories: that re-implements the
tool in every session.

## Migrators

`migrate/<from>-to-<to>.py` holds one version step each: `0-to-1.py` takes a
version 0 home to version 1, and nothing else. A home two versions behind runs
the two steps it is missing, in order.

They are the one thing here that is *not* installed by the install pass. A
server that finds a home behind its own version asks for one by path -- the pin
first, then the newest commit in this repository that carries it, then a local
checkout the pin names -- fetches it into `<home>/migrate/`, and runs it. So
this directory is append-only: `0-to-1.py` has to survive long after version 1
stopped being the newest, because that is exactly the file a home left behind
asks for by name.

A migrator takes `--home` and `--db`, changes the database, and exits zero. It
has to be safe to run twice, because a step that failed is run again on the next
start.

## Paths inside a skill

A skill body must never assume it lives in a repository. Write the install path:

- this bucket: `$GONGGA_HOME/skills/<name>/...` -- the harness exports
  `GONGGA_HOME`, `GONGGA_SERVER` and `GONGGA_SESSION` into every shell and script
  it runs, and a `PATH` whose first entry is the home's `bin`, where `gongga-cli`
  is the command layer and `gongga-server` the program behind it.
- the machine's: `~/.agents/skills/<name>/...`
- a project's own, if it has one: `<that repository>/.agents/skills/<name>/...`

Every spelling is runnable exactly as written, which is the only rule that keeps
a skill honest after a session's cwd has moved somewhere else.

## Changing what a running harness sees

An edit here is invisible until the pin moves: the harness installs the commit it
was built with. Bump `crates/server/ship.toml` in gongga to ship a change. Nothing
recompiles -- the pin is data -- but gongga restarts to pick it up, because a
running server already installed the old one.
