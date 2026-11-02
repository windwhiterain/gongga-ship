---
name: remote
description: Configure and verify a Gongga session whose shells run on another machine over SSH. Use for 远端 / 远程主机 / remote host / set_session --target / runner deployment. Covers installing the matching gongga distribution, writing [targets.<name>], checking the real SSH connection, and current remote-session limits. This is not a general-purpose SSH or PowerShell-remoting skill.
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

Find this skill's installed path with `index_skill`, then call its `remote` launcher.
These examples use `R=/path/from/index_skill/remote`, not a command assumed to be on
`PATH`.

0. **See what this server already has.** `list` reads the local config; `--check` also
   greets each target's runner, which is the cheapest proof that a peer still answers:

   ```bash
   "$R" list --check
   ```

1. **Build and install the distribution.** Run from the Gongga checkout:

   ```bash
   "$R" install --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519
   ```

   This builds `gongga-bundle` locally, installs a content-addressed distribution under
   the peer's `~/.gongga/bin/`, proves the runner greeting, and prints the exact target
   configuration. It needs no `gongga` entry on the peer's `PATH`.

   A Windows bundle cannot run on Linux, nor the reverse. For a different peer OS,
   build for that OS and pass `--artifact /path/to/gongga`; the script does not pretend
   to cross-compile. `--artifact` is also the way to test an existing build. Read the
   reported peer OS in the result and do not call a Windows/Git Bash check a POSIX one.

2. **Write the endpoint into the local server's config.** Use the `runner` printed by
   `install`:

   ```bash
   "$R" configure --target build --host 192.168.1.10 --user build \
       --identity ~/.ssh/id_ed25519 \
       --runner '/home/build/.gongga/bin/gongga-HASH runner' --write
   ```

   `--home` defaults to `$GONGGA_HOME`, then `~/.gongga`. Without `--write`, the command
   only prints TOML. An existing different target is never overwritten. The installer
   also reports `cwd`: use a `C:/...` path for a Windows peer and `/...` for a POSIX
   peer; do not use Git Bash's `/c/...` as the runner's cwd. Configuration
   is read at server startup, so restart that server afterwards. When changing server
   code in development, use the `evolve` skill rather than leaving an old server alive.

   The equivalent manual configuration is:

   ```toml
   [targets.build]
   host = "build@192.168.1.10"
   port = 22
   identity = "C:/keys/build_ed25519"
   runner = "/home/build/.gongga/bin/gongga-HASH runner"
   ```

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

## Use the endpoint

A target is fixed when the session is created. Use a new session to change machines;
a model tool call cannot choose an endpoint.

```bash
gongga-cli set_session --session build --target build \
    --model provider/model --cwd /home/build/project
gongga-cli send_message --session build --message \
    'Run pwd, inspect the checkout, then run the focused tests.'
```

`--cwd` belongs to the **peer** and must already exist there; omit it to use the peer's
default directory. It is never checked against or replaced with the server's local
cwd. The server stores the endpoint-reported cwd after the shell connects. Use a
Windows path for a Windows peer and a POSIX path for a POSIX peer. A relative remote
path is relative to the runner's starting directory, so an absolute path is clearer.

The project is not synchronised. Clone/copy it on the peer first, using ordinary SSH,
SFTP or Git from a local shell. Keep remote sessions separate even when they name the
same peer: each has its own shells and state. The install script uploads only the
runner, not the checkout. If you need a local file on the peer, use `ssh`/`sftp`
explicitly and verify its size, or ask the remote shell to `git clone` it.

## What works, and what is still refused

Foreground `bash`, `check_bash`, `del_bash` and `list_bash` work. A command that exceeds
its waiting timeout continues in its retired shell; deleting that name now ends both
the retired and replacement shells. Transport loss never replays a command.

This milestone explicitly refuses operations that would otherwise touch the wrong
machine:

- `background: true` and `detached: true`;
- `index_skill`, and `@skill:` or `@image:` in messages to a remote session;
- attaching an image printed by a remote tool;
- spilling capped output to a readable file (the output is capped without a path).

Consequently, do not ask a remote session to use `evolve`, `heir`, `subagent`, `image`,
or other skills that need local harness files or `gongga-cli`. The local setup session
can use those skills itself. `@file:` remains a literal peer-side path, not a promise
that a local file was uploaded. Remote harness callback, remote file/skills/images,
background jobs and Windows-peer production hardening are not silently emulated.

## Inspect and remove

`get_messages` reads the local transcript as usual. `list_bash` only shows live named
shells; a retired command reports through its completion message. `list --check` was
in step 0: it changes nothing and answers whether each target still answers. If setup fails,
read the command's error and the local server log rather than falling back to a local
shell.

Remove only the distribution and target you named:

```bash
"$R" remove --distribution '/home/build/.gongga/bin/gongga-HASH' \
    --host 192.168.1.10 --user build --identity ~/.ssh/id_ed25519
"$R" configure --target build --remove --write
```

Close sessions using that target first. Do not remove a distribution while its runner
is still serving another target.
