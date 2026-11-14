"""One path space: the machine a path lives on, and how to read, write and run there.

The Python mirror of the server's `Space` (`crates/server/src/space.rs`): one value
decides the machine, and a caller asks it to read bytes, write bytes or run a program
without ever asking which machine it is on. This machine is the process's own
filesystem and subprocess; a peer is its runner, spoken over SSH exactly as the server
speaks it (`docs/notes/server-remote.md`).

    import gongga_space
    space = gongga_space.Space.of_env()      # the session's machine, or this one
    data = space.read_bytes("/path/file")
    space.write_bytes("/path/file", data)
    run = space.run("/path", "git", ["status"])

The file primitives are the seam the `file` skill reads and writes through, so its
matching, BOM and all-or-nothing logic is one implementation that runs here while the
bytes travel to whichever machine owns the path. A peer's bytes move through the
runner's own `Read` and `Append` (base64, binary-safe); a whole-file write is an
`Append` to a temp beside the target and one `mv`, because the protocol has no rename
and `Write` never closes a shell's stdin -- a `cat` would wait forever.

Only the standard library and the local OpenSSH client are needed. The transport
wheels here (`Peer`, `run`, `hello`, ...) are the one copy the `remote` skill imports
too, so a shell, a file read and an install reach a peer the same way.
"""

from __future__ import annotations

import base64
import json
import os
import posixpath
import re
import sqlite3
import stat
import subprocess
import tempfile
import tomllib
import types
import uuid
from pathlib import Path

#: The runner a peer runs when its table names none: two argv words on that machine.
DEFAULT_RUNNER = "gongga runner"

#: One `Append` frame's payload. A line has no size the protocol promises, so a big
#: file travels in several rather than one frame a reader might refuse.
APPEND_CHUNK = 256 * 1024


class SpaceError(ValueError):
    """A call this space cannot make: a machine it cannot reach, a path it cannot open.

    # Invariant

    A `ValueError`, so the skill contract reads it as a refusal (one line, exit 2) and
    not as a crash: a peer that is not configured or a file that is not there is the
    caller's answer, not a bug in this module.
    """


class Failure(RuntimeError):
    """A transport step that failed, in the `remote` skill's own shape."""


# -- transport wheels (the one copy; `remote/_remote.py` imports these) ----------


def run(argv, *, stdin=None, timeout=60):
    """One local program to its end, with its bytes and status.

    # Invariant

    A child that inherits this process's stdin holds the caller's terminal open after
    it exits; only an explicit batch is passed through, every other child gets a
    closed stdin.
    """
    fed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        return subprocess.run(
            argv,
            **fed,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as error:
        raise Failure(f"{argv[0]} is not installed or not on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise Failure(f"{argv[0]} did not answer within {timeout:g}s") from error


def text(data):
    return data.decode("utf-8", "replace").strip()


def checked(result, what):
    if result.returncode:
        detail = text(result.stderr) or text(result.stdout) or f"exit {result.returncode}"
        raise Failure(f"{what}: {detail}")
    return result


def require_program(name):
    import shutil

    if shutil.which(name) is None:
        raise Failure(f"{name} is required on this machine")


def peer_quote(word):
    """One word as the peer's shell must receive it (`endpoint.rs::peer_quote`).

    # Invariant

    `ssh` joins what follows the host into one line the peer's shell parses, so a word
    carrying a space or a quote arrives as several unless it is quoted here.
    """
    if any(ch.isspace() for ch in word) or "'" in word:
        return "'" + word.replace("'", r"'\''") + "'"
    return word


def runner_words(runner):
    words = runner.split()
    if not words:
        raise Failure("runner must contain non-empty command words")
    return words


def runner_command(runner):
    return " ".join(peer_quote(word) for word in runner_words(runner))


def hello(frame):
    """The greeting a runner sends before anything else.

    # Invariant

    The protocol number is read here, never judged: the server is the one that speaks
    it and refuses a peer it cannot talk to, so a second copy of that number here is
    exactly what drifts. What is read is reported; the verdict belongs to the server.
    """
    if frame.get("event") != "hello" or type(frame.get("protocol")) is not int:
        raise Failure(f"unexpected runner greeting: {frame}")
    return frame


class Peer:
    """One machine reached over SSH/SFTP, spelled the way the server spells it.

    # Invariant

    The SSH options are the server's (`endpoint.rs::peer_command`): BatchMode stops a
    hang, the keepalive probes find an idle connection dead. A shell, a file read and
    an install reach a peer the same way, so a target that works for one works for all.
    """

    def __init__(self, args):
        if not args.host:
            raise Failure("a peer needs a host")
        self.destination = f"{args.user}@{args.host}" if args.user else args.host
        self.port = args.port
        self.identity = os.path.expanduser(args.identity) if args.identity else None

    def base(self, program):
        argv = [
            program,
            "-o", "BatchMode=yes",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "TCPKeepAlive=yes",
            "-o", "ConnectTimeout=10",
        ]
        if self.port != 22:
            argv += ["-p" if program == "ssh" else "-P", str(self.port)]
        if self.identity:
            argv += ["-i", self.identity]
        return argv

    def ssh(self, command=None, *, stdin=None, timeout=60):
        argv = self.base("ssh") + [self.destination]
        if command is not None:
            argv += ["--", command]
        return run(argv, stdin=stdin, timeout=timeout)

    def sftp(self, batch, *, timeout=300):
        argv = self.base("sftp") + ["-b", "-", self.destination]
        return run(argv, stdin=batch.encode(), timeout=timeout)


# -- this machine's remotes ----------------------------------------------------


def remotes_path():
    """Where this machine keeps its remotes: an override, else `~/.agents/remotes.toml`.

    The same file the server reads (`remotes.rs::path`), never the home's config.toml.
    """
    over = os.environ.get("GONGGA_REMOTES_CATALOG")
    if over:
        return Path(over).expanduser()
    return Path.home() / ".agents" / "remotes.toml"


def read_remotes():
    """Every `[remotes.<name>]` table, under either spelling; no file is no remote."""
    path = remotes_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as bad:
        raise SpaceError(f"could not read {path}: {bad}") from None
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as bad:
        raise SpaceError(f"{path} is not TOML: {bad}") from None
    tables = data.get("remotes") or data.get("targets") or {}
    return {name: row for name, row in tables.items() if isinstance(row, dict)}


def session_cwd():
    """The calling session's directory, read-only from its home's row, or None.

    # Invariant

    The server exports `GONGGA_REMOTE` but not the directory, and a relative path on a
    peer must resolve against the session's directory there. Read read-only and not
    held, the way `subagent` reads its caller's row: a second writer of the row the
    server owns is not this module's to be.
    """
    name = (os.environ.get("GONGGA_SESSION") or "").strip()
    home = (os.environ.get("GONGGA_HOME") or "").strip()
    if not name or not home:
        return None
    database = os.path.join(os.path.expanduser(home), "gongga.db")
    if not os.path.isfile(database):
        return None
    try:
        connection = sqlite3.connect(
            "file:" + database.replace("\\", "/") + "?mode=ro", uri=True
        )
        try:
            row = connection.execute(
                "select cwd from sessions where name = ?", (name,)
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


# -- one runner connection -----------------------------------------------------


class _Connection:
    """One runner process: a greeting, then requests by id and the events between.

    Single-threaded: a request drains frames until its own reply, buffering any shell
    events that arrived first, so a one-shot run reads them back in order afterwards.
    """

    def __init__(self, argv):
        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError as bad:
            raise SpaceError(f"could not start the runner: {bad}") from None
        self._id = 0
        self._events = []
        try:
            hello(self._frame())
        except BaseException:
            self.close()
            raise

    def _frame(self):
        line = self.process.stdout.readline()
        if not line:
            detail = text(self.process.stderr.read() or b"")
            raise SpaceError(f"the runner closed without answering{(': ' + detail) if detail else ''}")
        try:
            return json.loads(line)
        except ValueError as bad:
            raise SpaceError(f"the runner sent a line that is not JSON: {bad}") from None

    def _send(self, obj):
        try:
            self.process.stdin.write((json.dumps(obj) + "\n").encode())
            self.process.stdin.flush()
        except OSError as bad:
            raise SpaceError(f"could not write to the runner: {bad}") from None

    def request(self, op):
        self._id += 1
        rid = self._id
        self._send({"id": rid, **op})
        while True:
            frame = self._frame()
            if "event" in frame:
                self._events.append(frame)
                continue
            if frame.get("id") != rid:
                raise SpaceError(f"the runner answered id {frame.get('id')}, asked {rid}")
            if frame.get("err"):
                raise SpaceError(frame["err"])
            return frame.get("ok") or {}

    def next_event(self):
        if self._events:
            return self._events.pop(0)
        return self._frame()

    def close(self):
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.communicate()


class Run:
    """What one program printed and exited with, on one path space.

    # Invariant

    The exit status is the program's own: `None` is a peer that ended without saying,
    never a guess of 0, which a caller would read as "it worked".
    """

    def __init__(self, exit, stdout, stderr):
        self.exit = exit
        self.stdout = stdout
        self.stderr = stderr

    @property
    def succeeded(self):
        return self.exit == 0

    def stdout_text(self):
        return self.stdout.decode("utf-8", "replace")


def _chunks(data, size):
    for at in range(0, len(data), size):
        yield data[at:at + size]


# -- the space -----------------------------------------------------------------


class Space:
    """Where a path lives: this machine, or the peer it is on.

    # Invariant

    One value decides both, so no caller asks which machine it is on: a path handed to
    a method is resolved by the machine that owns it. A name that resolves to no
    machine is refused, never a quiet local fallback (`remotes.rs`).
    """

    def __init__(self, name=None, table=None):
        self.name = name
        self.table = table or {}
        self._conn = None

    @classmethod
    def local(cls):
        return cls()

    @classmethod
    def named(cls, name):
        name = (name or "").strip()
        if not name or name.lower() in ("local", "none"):
            return cls.local()
        tables = read_remotes()
        if name not in tables:
            raise SpaceError(
                f"no remote named {name!r} in {remotes_path()}; "
                "configure it with the remote skill, or name one that is there"
            )
        return cls(name, tables[name])

    @classmethod
    def of_env(cls):
        """The session's machine (`GONGGA_REMOTE`), or this one when it names none."""
        return cls.named(os.environ.get("GONGGA_REMOTE"))

    @property
    def is_local(self):
        return self.name is None

    def describe(self):
        if self.is_local:
            return "local"
        host = self.table.get("host", "")
        runner = self.table.get("runner") or DEFAULT_RUNNER
        return f"ssh {host} ({runner})" if host else f"direct runner ({runner})"

    def resolve(self, path, cwd=None):
        """One path absolute in this space's spelling, relative to `cwd` when it is not.

        A peer's path is spelled with `/` throughout (`runner.rs::spelling`); a local
        path is this OS's own. The directory is the caller's, never inferred here.
        """
        path = str(path)
        if self.is_local:
            return os.path.abspath(os.path.join(cwd or os.getcwd(), path))
        norm = path.replace("\\", "/")
        if posixpath.isabs(norm) or re.match(r"^[A-Za-z]:", norm):
            return norm
        base = (cwd or "").replace("\\", "/").rstrip("/")
        return posixpath.join(base, norm) if base else norm

    # -- the runner connection, dialled once and kept for the length of the call --

    def _connection(self):
        if self.is_local:
            raise SpaceError("the local space has no runner")
        if self._conn is None:
            runner = self.table.get("runner") or DEFAULT_RUNNER
            host = (self.table.get("host") or "").strip()
            if host:
                peer = Peer(types.SimpleNamespace(
                    host=host,
                    user=None,
                    port=self.table.get("port") or 22,
                    identity=self.table.get("identity"),
                ))
                argv = peer.base("ssh") + [peer.destination, "--"]
                argv += [peer_quote(word) for word in runner_words(runner)]
            else:
                # No host is a runner this machine starts: the peer without ssh.
                argv = runner_words(runner)
            self._conn = _Connection(argv)
        return self._conn

    def close(self):
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- the primitives ---------------------------------------------------------

    def run(self, cwd, program, args, stdin=None):
        """Run one program to its end, standing in `cwd`, and answer what it printed.

        # Invariant

        `cwd` and `program` are spelled for the machine that owns them. No clock here:
        a caller with a deadline wraps this call in one, because only it knows what
        its command is worth waiting for (`space.rs`).
        """
        if self.is_local:
            return self._run_local(cwd, program, args, stdin)
        return self._run_remote(cwd, program, args, stdin)

    def _run_local(self, cwd, program, args, stdin):
        fed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
        try:
            result = subprocess.run(
                [program, *args],
                cwd=cwd or None,
                **fed,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except FileNotFoundError as bad:
            raise SpaceError(f"could not start {program}: {bad}") from None
        return Run(result.returncode, result.stdout, result.stderr)

    def _run_remote(self, cwd, program, args, stdin):
        conn = self._connection()
        shell = 1
        at = (cwd or "").strip() or None
        conn.request({"op": "open", "shell": shell, "cwd": at,
                      "program": program, "args": list(args)})
        if stdin:
            conn.request({"op": "write", "shell": shell,
                          "data": base64.b64encode(stdin).decode()})
        out, err, code = bytearray(), bytearray(), None
        while True:
            event = conn.next_event()
            kind = event.get("event")
            if kind == "out" and event.get("shell") == shell:
                data = base64.b64decode(event.get("data", ""))
                (out if event.get("stream") == "stdout" else err).extend(data)
            elif kind == "end" and event.get("shell") == shell:
                code = event.get("code")
                break
        conn.request({"op": "close", "shell": shell})
        return Run(code, bytes(out), bytes(err))

    def read_bytes(self, path, limit=0):
        """The whole of one file (or its first `limit` bytes), as bytes.

        A file that is not there is a refusal naming it, on either machine.
        """
        if self.is_local:
            try:
                with open(path, "rb") as stream:
                    return stream.read(limit if limit > 0 else -1)
            except OSError as bad:
                raise SpaceError(f"{path}: {bad.strerror or bad}") from None
        conn = self._connection()
        out, offset = bytearray(), 0
        while True:
            reply = conn.request({"op": "read", "path": path, "offset": offset})
            chunk = base64.b64decode(reply.get("data", ""))
            out.extend(chunk)
            offset = reply.get("offset", offset + len(chunk))
            if limit > 0 and len(out) >= limit:
                return bytes(out[:limit])
            if reply.get("eof") or not chunk:
                return bytes(out)

    def write_bytes(self, path, data):
        """Replace one file whole, atomically, creating its parents.

        # Invariant

        A reader never sees half a file and a refused call leaves the disk alone: here
        a temp beside the target and `os.replace`; on a peer an `Append` to a temp and
        one `mv`, because the protocol has no rename and a shell's stdin never closes.
        """
        if self.is_local:
            self._write_local(path, data)
        else:
            self._write_remote(path, data)

    def _write_local(self, path, data):
        path = os.path.abspath(path)
        parent = os.path.dirname(path) or "."
        try:
            os.makedirs(parent, exist_ok=True)
            handle, temp = tempfile.mkstemp(prefix=".gongga-", dir=parent)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                if os.path.exists(path):
                    os.chmod(temp, stat.S_IMODE(os.stat(path).st_mode))
                os.replace(temp, path)
            except BaseException:
                try:
                    os.unlink(temp)
                except OSError:
                    pass
                raise
        except OSError as bad:
            raise SpaceError(f"{path}: {bad.strerror or bad}") from None

    def _write_remote(self, path, data):
        conn = self._connection()
        norm = path.replace("\\", "/")
        parent = norm.rsplit("/", 1)[0] if "/" in norm else "."
        temp = f"{parent}/.gongga-{uuid.uuid4().hex}"
        try:
            for chunk in _chunks(data, APPEND_CHUNK):
                conn.request({"op": "append", "path": temp,
                              "data": base64.b64encode(chunk).decode()})
            snippet = (
                'set -e\n'
                'temp="$1"; target="$2"\n'
                'trap \'rm -f "$temp"\' EXIT\n'
                'if [ -e "$target" ]; then '
                'chmod --reference="$target" "$temp" 2>/dev/null || true; fi\n'
                'mv -f "$temp" "$target"\n'
                'trap - EXIT\n'
            )
            result = self._run_remote(None, "bash",
                                      ["-c", snippet, "gongga-write", temp, norm], None)
            if not result.succeeded:
                detail = result.stderr.decode("utf-8", "replace").strip()
                raise SpaceError(
                    f"could not write {path} on {self.describe()}: {detail or 'the move failed'}"
                )
        except BaseException:
            self._remove_remote(temp)
            raise

    def _remove_remote(self, path):
        try:
            self._run_remote(None, "bash", ["-c", 'rm -f "$1"', "gongga-rm", path], None)
        except SpaceError:
            pass

    def stat(self, path):
        """What this machine says about one path; `exists: False` is an answer."""
        if self.is_local:
            try:
                meta = os.stat(path)
            except FileNotFoundError:
                return {"path": path, "exists": False, "is_file": False,
                        "is_dir": False, "size": 0}
            except OSError as bad:
                raise SpaceError(f"{path}: {bad.strerror or bad}") from None
            return {"path": path, "exists": True,
                    "is_file": stat.S_ISREG(meta.st_mode),
                    "is_dir": stat.S_ISDIR(meta.st_mode), "size": meta.st_size}
        return self._connection().request({"op": "stat", "path": path})
