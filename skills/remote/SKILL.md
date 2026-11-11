---
name: remote
description: Configure and verify a Gongga session whose shells run on another machine over SSH. Use for 远端 / 远程主机 / remote host / set_session --remote / runner deployment. Covers installing the matching gongga distribution, writing the machine's ~/.agents/remotes.toml, checking the real SSH connection, and what a session with a remote can and cannot do. This is not a general-purpose SSH or PowerShell-remoting skill.
compatibility: Needs Python 3.11+ (tomllib), an OpenSSH client, and on Windows a Git Bash (not the Store alias)
metadata:
  entry: remote
---

# Gongga remote sessions

Use this skill from a **local** session to set up another machine. A remote session has
one local server: its model, credentials, home, database and transcript stay here. SSH
starts `gongga-runner` on the peer; it is not another Gongga server and listens on no
port. The peer needs Bash (Git Bash on Windows), not Cargo or API credentials.

The script beside this file is self-contained Python 3.11+ and uses the machine's
`ssh`/`sftp`. It does not read the old `remote` skill's roster, `~/.ssh/config`, or a
PowerShell Remoting setup. Host-key checking stays enabled. Never disable it to make a
connection work.

Every command prints one JSON line on stdout and its progress on stderr, so nothing
that parses the answer has to skip past narration. A command returns when its work is
done: `install` says when it is building, uploading and greeting, and skips the upload
when that exact content-addressed distribution is already on the peer.

## Set up one endpoint

Find this skill's own directory -- `@skill(...)`'s expansion names it in parentheses,
and so does the `path` column of `skill` -- then call its `remote` launcher.
These examples use `R="$SKILL_DIR/remote"`, not a command assumed to be on `PATH`.
In gongga, `gongga-cli skill remote <command>` is the same launcher, shorter.

```bash
SKILL_DIR=<the directory this SKILL.md is in>
R="$SKILL_DIR/remote"
```

0. **See what this server already has.** `list` reads the local config; `--check` also
   greets each remote's runner and asks the running server what it makes of that peer.
   Two answers, never one: the greeting says the peer answers, and the server's
   verdict says whether *this* server can speak to it -- the server is the one that
   speaks the protocol, so it is the one that decides.

   ```bash
   "$R" list --check
   ```

   A remote is usable when `server.usable` is true. `usable` false with
   `reason: "protocol"` names both versions: the peer speaks one, this server speaks
   another, and the peer needs the matching distribution installed again. `usable` is
   null when the question could not be answered -- no server running, a server that has
   not read the file yet, or a refusal that is not about the protocol -- and a null is
   not a verdict.

1. **Build and install the distribution.** Run from the Gongga checkout:

   ```bash
   "$R" install --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519 \
       --remote build --write
   ```

   This builds `gongga-bundle` locally, installs a content-addressed distribution under
   the peer's `~/.gongga/bin/`, proves the runner greeting, and -- with `--write` --
   points this machine's remote at the new distribution, changing nothing but that
   remote's `runner` line. A remote that names another machine is refused, not
   rewritten. It needs no `gongga` entry on the peer's `PATH`.

   A Windows bundle cannot run on Linux, nor the reverse. For a different peer OS,
   build for that OS and pass `--artifact /path/to/gongga`; the script does not pretend
   to cross-compile. `--artifact` is also the way to test an existing build. Read the
   reported peer OS in the result and do not call a Windows/Git Bash check a POSIX one.

2. **Write the endpoint into this machine's remotes file.** `install --write` has
   already done this; to write it by hand, or to change one field of a remote that
   exists, use the `runner` printed by `install`:

   ```bash
   "$R" configure --remote build --host 192.168.1.10 --user build \
       --identity ~/.ssh/id_ed25519 \
       --runner '/home/build/.gongga/bin/gongga-HASH runner' --write
   ```

   It writes `~/.agents/remotes.toml` (`$GONGGA_AGENTS_HOME`, or `--remotes <path>`),
   **not** the home's `config.toml`: that file is the pinned content's and is merged
   three ways on every install, so a host name, a private key path and a runner path
   must not live there. `~/.agents/` is never a repository, which is what `keys.toml`
   already relies on. Without `--write`, the command only prints the table. An
   existing different remote is never overwritten. The installer also reports `cwd`:
   use a `C:/...` path for a Windows peer and `/...` for a POSIX peer; do not use Git
   Bash's `/c/...` as the runner's cwd. The file is read at server startup, so restart
   that server afterwards. When changing server code in development, use the `evolve`
   skill rather than leaving an old server alive.

   The equivalent manual configuration, in `~/.agents/remotes.toml`:

   ```toml
   [remotes.build]
   host = "build@192.168.1.10"
   port = 22
   identity = "C:/keys/build_ed25519"
   runner = "/home/build/.gongga/bin/gongga-HASH runner"
   ```

   A home from before this file existed has its tables in `config.toml`; the server
   says so at startup, and `<home>/migrate/3-to-4.py --home <home> --db <home>/gongga.db`
   moves them.

   `identity` is a path on the **local** machine. `runner` is a command on the peer;
   its default is `gongga runner`, which requires the distribution on the peer's
   `PATH`. An absolute runner path from `install` avoids PATH and login-shell
   differences. Paths containing whitespace are not supported by the current runner
   command spelling, so the installer rejects them.

3. **Verify transport and process ownership before using the session.**

   ```bash
   "$R" check --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519 \
       --runner '/home/build/.gongga/bin/gongga-HASH runner'
   "$R" verify --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519 \
       --runner '/home/build/.gongga/bin/gongga-HASH runner'
   ```

   `check` proves the runner greeting. `verify` additionally proves cwd/export
   persistence, exact command status, an invalid-directory refusal, and termination of
   a busy shell through the runner protocol. It closes its test shells and leaves no
   scratch directory. Do not treat a successful SSH login or a `discarded` message alone as proof
   that the peer's process is gone.

### When the peer is behind

The protocol number belongs to the server: it speaks it, and it refuses a peer it
cannot talk to before any shell is opened -- "the endpoint's runner speaks protocol 1,
and this server speaks protocol 2". The runner installed on the peer carries that
number, so a server that moved on leaves the peer behind. Nothing in this script
judges the number: it reads what the runner says and asks the server for the verdict,
so the script and the server cannot disagree about it.

The repair is a new distribution on the peer and a new `runner` line here. `install
--write` does both and reports that the server must be restarted, because the remotes
file is read at startup.

## Use the endpoint

A session with a remote runs its programs on the peer: `exec` starts a channel there
by default, and `remote` names another machine from `[remotes]` when a call asks for one.
It has one directory, and that directory belongs to the peer. The word is `remote`
everywhere: `set_session --remote`, `configure --remote`, `[remotes.<name>]`.

The agent is told where it stands. The session's start card (`dynamic_tail_remote`
in the shipped config) names the peer beside the directory, so a model asked to run
something knows that a plain path in a message is a path there, and that `exec` already
runs on the peer. The harness itself stays on the server machine: `gongga-cli`, the
home's skills and the roster are not on the peer.

```bash
gongga-cli set_session --id build --model provider/model --remote build --cwd /home/build/project
gongga-cli send_message --session build --message 'Run pwd with exec; it runs on the peer. The harness stays on the server machine.'
```

`--cwd` belongs to the **peer** and must already exist there; omit it to use the peer's
default directory. It is never checked against or replaced with the server's local
cwd. The server stores the endpoint-reported cwd after the shell connects. Use a
Windows path for a Windows peer and a POSIX path for a POSIX peer. A relative remote
path is relative to the runner's starting directory, so an absolute path is clearer.

`--remote none` moves a session back to this machine; omitting the flag leaves the
machine as it is. Both end every channel on the machine being left. A move asked for
while a turn is running is queued, not refused: it is applied when that turn ends, and
the command says so.

The project is not synchronised. Clone/copy it on the peer first, using ordinary SSH,
SFTP or Git from a local shell. Keep remote sessions separate even when they name the
same peer: each has its own shells and state. The install script uploads only the
runner, not the checkout. If you need a local file on the peer, use `ssh`/`sftp`
explicitly and verify its size, or ask the remote shell to `git clone` it.

## What works, and what is refused

Everything the harness does on the server machine keeps working in a session with a
remote, because the harness stays there: `gongga-cli`, the home's skills (`heir`,
`subagent`, `evolve`, `image`), the roster and the key never leave this machine. `exec`
on the peer is how the peer's files are read and edited.

- A channel is named by its `id`, not by a machine: `exec` reads the output nobody has
  read yet, `ls_channel` lists the live ones, `del_channel` ends one. The answer says
  which machine holds it; a session with a remote has its default channel on the peer.
- A reference is read on the machine that printed it, so `@image(...)` and `@file(...)`
  in an `exec` result on the peer are the peer's, and the same reference in a result on
  this machine is this machine's. The prompt's index reads the peer's `AGENTS.md`.

Nothing in a local session's tool list mentions a remote: `exec` is offered there
without its `remote` argument, so a local call cannot name a peer. A model that wants
to know whether this session has a peer asks for this skill; that is the one place the
answer is written. A local session that wants to work on another machine either moves
there (`set_session --remote`, which is a command and takes effect when its turn ends,
not in the turn that asked) or drives the peer through SSH from `exec`; this skill's own
launcher only installs and verifies an endpoint, it runs nothing there.

The project is not synchronised either way: a file on the peer is not here, and a
checkout is not copied for you.

## Inspect and remove

`get_messages` reads the local transcript as usual. `ls_channel` only shows live channels; one that ended reports through its channel-end notice. `list --check` was
in step 0: it changes nothing and answers whether each remote still answers. If setup fails,
read the command's error and the local server log rather than falling back to a local
shell.

Remove only the distribution and remote you named:

```bash
"$R" remove --distribution '/home/build/.gongga/bin/gongga-HASH' \
    --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519
"$R" configure --remote build --remove --write
```

Close sessions using that remote first. Do not remove a distribution while its runner
is still serving another remote.
