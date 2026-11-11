#!/usr/bin/env python3
"""Deploy and verify Gongga's SSH execution runner, without a second server.

Only Python's standard library and the local OpenSSH client are needed. No roster,
SSH config override, remote home convention or host-check bypass is assumed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tomllib

# Never let an SSH login profile inject frames into a runner or probe.
BASH = "bash --norc --noprofile"


class Failure(RuntimeError):
    pass


def emit(kind: str, **fields) -> None:
    print(json.dumps({"result": kind, **fields}, ensure_ascii=False))


def note(message: str) -> None:
    # Progress is narration for the person waiting, so it goes to stderr: stdout
    # carries the one JSON answer, and a reader parsing it must not skip past prose.
    print(message, file=sys.stderr, flush=True)


def human_size(count: int) -> str:
    value = float(count)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def run(argv: list[str], *, stdin=None, timeout: float = 60) -> subprocess.CompletedProcess:
    # A child that inherits this process's stdin holds the caller's terminal open
    # after it exits, and the shell that started this script then waits for a
    # command that has already printed its answer. Only an explicit batch is
    # passed through; every other child gets a closed stdin.
    fed = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    try:
        result = subprocess.run(
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
    return result


def text(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def require_program(name: str) -> None:
    if shutil.which(name) is None:
        raise Failure(f"{name} is required on this machine")


class Peer:
    def __init__(self, args: argparse.Namespace):
        if not args.host:
            raise Failure("--host is required")
        self.destination = f"{args.user}@{args.host}" if args.user else args.host
        self.port = args.port
        self.identity = os.path.expanduser(args.identity) if args.identity else None

    def base(self, program: str) -> list[str]:
        argv = [program, "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
        if self.port != 22:
            argv += ["-p" if program == "ssh" else "-P", str(self.port)]
        if self.identity:
            argv += ["-i", self.identity]
        return argv

    def ssh(self, command: str | None = None, *, stdin=None, timeout: float = 60):
        argv = self.base("ssh") + [self.destination]
        if command is not None:
            argv += ["--", command]
        return run(argv, stdin=stdin, timeout=timeout)

    def sftp(self, batch: str, *, timeout: float = 300):
        argv = self.base("sftp") + ["-b", "-", self.destination]
        return run(argv, stdin=batch.encode(), timeout=timeout)


def checked(result: subprocess.CompletedProcess, what: str) -> subprocess.CompletedProcess:
    if result.returncode:
        detail = text(result.stderr) or text(result.stdout) or f"exit {result.returncode}"
        raise Failure(f"{what}: {detail}")
    return result


def shell_quote(value: str) -> str:
    # Quote a word inside the explicitly invoked POSIX probe script.
    return "'" + value.replace("'", "'\\''") + "'"


def sftp_quote(value: str) -> str:
    # SFTP is not a POSIX shell: backslash is an escape in its quoted parser.
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def local_sftp_path(path: Path) -> str:
    """The local half of an sftp put.

    # Invariant

    The local side is read by the local OS, so it is spelled the way that OS spells
    it: C:/dir/file on Windows. The MSYS form /c/dir/file is a Git Bash path and means
    nothing to sftp, which then reports a Windows file as missing -- an install that
    failed at the upload over a path that only looked right.
    """
    return str(path).replace("\\", "/")


def runner_command(runner: str) -> str:
    words = runner.split()
    if not words or any(any(ch.isspace() for ch in word) for word in words):
        raise Failure("runner must contain non-empty command words without whitespace")
    return " ".join(shlex.quote(word) for word in words)


def hello(frame: dict) -> dict:
    """The greeting a runner sends before anything else.

    # Invariant

    The protocol number is read here, never judged: the server is the one that
    speaks it and the one that refuses a peer it cannot talk to, so a second
    copy of that number in this script is exactly what drifts. What is read is
    reported; the verdict belongs to the server.
    """
    if frame.get("event") != "hello" or type(frame.get("protocol")) is not int:
        raise Failure(f"unexpected runner greeting: {frame}")
    return frame


def probe(peer: Peer, runner: str, timeout: float = 30):
    # A runner is intentionally long-lived: greeting, not EOF, is readiness.
    # Read one frame, then terminate only this test process.
    command = runner_command(runner)
    process = subprocess.Popen(
        peer.base("ssh") + [peer.destination, "--", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    import threading

    answer = []
    reader = threading.Thread(target=lambda: answer.append(process.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        process.kill()
        process.communicate()
        raise Failure(f"runner did not greet within {timeout:g}s")
    process.kill()
    _, errors = process.communicate(timeout=5)
    line = answer[0] if answer else b""
    try:
        greeting = json.loads(line)
    except json.JSONDecodeError as error:
        raise Failure(f"runner did not return JSON greeting: {line!r} {text(errors)}") from error
    return hello(greeting)


def source_size(peer: Peer, destination: str) -> int | None:
    result = peer.ssh(f"{BASH} -c {shlex.quote('wc -c < ' + shlex.quote(destination))}")
    if result.returncode != 0:
        return None
    try:
        return int(text(result.stdout))
    except ValueError:
        return None


def distribution(args: argparse.Namespace) -> tuple[Path, str]:
    artifact = Path(args.artifact).expanduser().resolve() if args.artifact else None
    if artifact is None:
        root = Path(args.build_from).expanduser().resolve()
        if not (root / "Cargo.toml").is_file():
            raise Failure(f"{root} is not a Gongga checkout")
        profile = "release" if args.release else "debug"
        command = ["cargo", "build", "-p", "gongga-bundle"]
        if args.release:
            command.append("--release")
        note("building gongga-bundle; a cold tree takes minutes")
        checked(run(command, timeout=1200), "building gongga-bundle")
        artifact = root / "target" / profile / ("gongga.exe" if os.name == "nt" else "gongga")
    if not artifact.is_file():
        raise Failure(f"distribution does not exist: {artifact}")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()[:16]
    return artifact, digest


def peer_home(peer: Peer) -> tuple[str, str]:
    result = checked(peer.ssh(f"{BASH} -c 'pwd -W'", timeout=20), "reading the peer home")
    windows = text(result.stdout).replace("\\", "/").rstrip("/")
    if len(windows) > 2 and windows[1] == ":":
        # Shell cwd is /c/... while SFTP receives C:/...; these are different
        # namespaces even though both belong to the same Windows peer.
        return f"/{windows[0].lower()}{windows[2:]}", windows
    result = checked(peer.ssh(f"{BASH} -c pwd", timeout=20), "reading the peer home")
    posix = text(result.stdout).rstrip("/")
    if not posix.startswith("/") or len(posix) < 2:
        raise Failure(f"peer did not report a usable home directory: {posix!r}")
    return posix, posix


def home_posix(peer: Peer) -> str:
    return peer_home(peer)[0]


def write_runner(path: Path, name: str, args: argparse.Namespace, runner: str) -> str:
    """Point one remote's runner at a distribution, leaving the rest of it alone.

    # Invariant

    Only the runner line changes. A host, a port and a key are the operator's
    decisions, so a remote that names another machine is refused rather than
    rewritten: an install that moved a remote would be a different operation
    wearing this one's name.

    The file is read at server startup, so a runner written here is not in use
    until that server is restarted. The caller says so.
    """
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = text.splitlines(keepends=True)
    headers = {f"[remotes.{name}]", f"[targets.{name}]"}
    start = None
    end = len(lines)
    for index, line in enumerate(lines):
        if line.strip() in headers:
            start = index
            for follower in range(index + 1, len(lines)):
                if lines[follower].lstrip().startswith("["):
                    end = follower
                    break
            break
    host = f"{args.user}@{args.host}" if args.user else args.host
    identity = os.path.expanduser(args.identity) if args.identity else None
    if start is None:
        body = remote_toml(name, args, runner).splitlines(keepends=True)
        if lines and lines[-1].strip():
            body.insert(0, "\n")
        lines.extend(body)
    else:
        table = {}
        for line in lines[start + 1:end]:
            key, _, value = line.partition("=")
            table[key.strip()] = value.strip().strip('"')
        if table.get("host", host) != host:
            raise Failure(
                f"remote {name!r} names {table.get('host')!r}, not {host!r}; edit it deliberately"
            )
        if table.get("identity", identity) != identity:
            raise Failure(
                f"remote {name!r} names another key; edit it deliberately"
            )
        replaced = False
        for index in range(start + 1, end):
            if lines[index].split("=", 1)[0].strip() == "runner":
                lines[index] = f"runner = {toml_string(runner)}\n"
                replaced = True
                break
        if not replaced:
            lines.insert(end, f"runner = {toml_string(runner)}\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.remote.tmp")
    temporary.write_text("".join(lines), encoding="utf-8", newline="\n")
    temporary.replace(path)
    return str(path)


def install(args: argparse.Namespace) -> None:
    require_program("ssh")
    require_program("sftp")
    artifact, digest = distribution(args)
    peer = Peer(args)
    shell_home, sftp_home = peer_home(peer)
    windows_peer = len(sftp_home) > 2 and sftp_home[1] == ":"
    suffix = ".exe" if windows_peer else ""
    root = f"{shell_home}/.gongga/bin"
    final = f"{root}/gongga-{digest}{suffix}"
    temporary = f"{root}/.gongga-{digest}{suffix}.upload"
    # This peer's SFTP server joins even C:/... to the account home. A
    # home-relative POSIX path is the one spelling both sides agree on.
    sftp_temporary = f".gongga/bin/.gongga-{digest}{suffix}.upload"
    for path in (root, final, temporary):
        if any(ch.isspace() for ch in path):
            raise Failure("the peer home contains whitespace; set a runner path without whitespace")
    checked(peer.ssh(f"{BASH} -c {shlex.quote('mkdir -p ' + shlex.quote(root))}"), "creating the distribution directory")
    # An existing content-addressed binary can be running. Replace only after
    # the new bytes are fully uploaded; never overwrite it in place.
    # Through the explicit POSIX shell like every other probe here: a peer whose
    # login shell is PowerShell does not know `test`, and an exit status read from
    # that shell would call every installed distribution missing.
    exists = peer.ssh(f"{BASH} -c {shlex.quote(f'test -x {shlex.quote(final)}')}")
    if exists.returncode != 0:
        size = artifact.stat().st_size
        note(f"uploading {human_size(size)} to {peer.destination}:{final}")
        checked(sftp_put_result(peer, artifact, sftp_temporary), "uploading distribution")
        uploaded = source_size(peer, temporary)
        if uploaded != size:
            raise Failure(f"uploaded size {uploaded} != local size {size}")
        note("uploaded; activating")
        checked(peer.ssh(f"{BASH} -c {shlex.quote(f'mv -f {shlex.quote(temporary)} {shlex.quote(final)}')}"), "activating distribution")
    else:
        note(f"{final} is already installed; skipping the upload")
    shell_final = f"{sftp_home}/.gongga/bin/gongga-{digest}{suffix}"
    runner = f"{shell_final} runner"
    note("greeting the runner")
    greeting = probe(peer, runner, timeout=45)
    written = None
    if args.write:
        if not args.remote:
            raise Failure("--write needs --remote: the name of the remote to point at this distribution")
        written = write_runner(remotes_path(args.remotes), args.remote, args, runner)
    emit(
        "installed",
        peer=peer.destination,
        distribution=shell_final,
        sha256=digest,
        runner=runner,
        shell_distribution=final,
        protocol=greeting["protocol"],
        cwd=peer_home(peer)[1],
        config=remote_toml(args.remote or "build", args, runner),
        remotes=written,
        restart_required=bool(written),
    )


def sftp_put_result(peer: Peer, source: Path, destination: str):
    return peer.sftp(
        f"put {sftp_quote(local_sftp_path(source))} {sftp_quote(destination)}\n"
        f"chmod 755 {sftp_quote(destination)}\n"
    )


def tables(data: dict) -> dict:
    """The remote tables a config holds, under either spelling."""
    return data.get("remotes") or data.get("targets") or {}


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def remote_toml(name: str, args: argparse.Namespace, runner: str) -> str:
    host = f"{args.user}@{args.host}" if args.user else args.host
    lines = [f"[remotes.{name}]", f"host = {toml_string(host)}"]
    if args.port != 22:
        lines.append(f"port = {args.port}")
    if args.identity:
        lines.append(f"identity = {toml_string(os.path.expanduser(args.identity))}")
    lines.append(f"runner = {toml_string(runner)}")
    return "\n".join(lines) + "\n"


def remotes_path(value: str | None) -> Path:
    """Where this machine keeps its remotes: an override, else `~/.agents/remotes.toml`.

    Not the home's `config.toml`. That file is the pinned content's and is merged
    three ways on every install, so it must not carry a host name, a private key
    path or a runner path -- the three things a remote is made of.
    """
    if value:
        return Path(value).expanduser().resolve()
    base = os.environ.get("GONGGA_AGENTS_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".agents"
    return (root / "remotes.toml").resolve()


def configure(args: argparse.Namespace) -> None:
    if not args.remote or args.remote == "local" or not args.remote.replace("_", "a").replace("-", "a").isalnum():
        raise Failure("a remote name must be a non-empty TOML bare key other than local")
    path = remotes_path(args.remotes)
    if args.remove:
        if not args.write:
            raise Failure("--remove requires --write")
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if args.remote not in tables(data):
            raise Failure(f"remote {args.remote!r} is not configured in {path}")
        args.write = True
    else:
        rendered = remote_toml(args.remote, args, args.runner or "gongga runner")
        if not args.write:
            print(rendered, end="")
            return
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old = tables(data).get(args.remote)
        desired = tomllib.loads(rendered)["remotes"][args.remote]
        if old is not None and old != desired:
            raise Failure(f"remote {args.remote!r} already differs in {path}; edit it deliberately")
        if old == desired:
            emit("unchanged", remotes=str(path), remote=args.remote)
            return
    # Preserve every other setting and comment. Only this remote's exact table header
    # and body are replaced; no generic TOML reformatting is attempted. The old
    # `[targets.<name>]` spelling is what the loader aliases, so writing this remote
    # replaces that table rather than adding a second.
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    headers = {f"[remotes.{args.remote}]", f"[targets.{args.remote}]"}
    output = []
    index = 0
    found = False
    while index < len(lines):
        if lines[index].strip() in headers:
            found = True
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith("["):
                index += 1
            if not args.remove:
                output.append(rendered)
            continue
        output.append(lines[index])
        index += 1
    if not found and not args.remove:
        if output and output[-1].strip():
            output.append("\n")
        output.append(rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".toml.remote.tmp")
    temporary.write_text("".join(output), encoding="utf-8", newline="\n")
    temporary.replace(path)
    emit("removed" if args.remove else "configured", remotes=str(path), remote=args.remote, restart_required=True)


def runner_verify(peer: Peer, runner: str, timeout: float = 45) -> None:
    """Check state, exact status and close through the runner, not SSH's status."""
    import base64
    import queue
    import threading
    import uuid

    process = subprocess.Popen(
        peer.base("ssh") + [peer.destination, "--", runner_command(runner)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    replies = {}
    replies_ready = threading.Condition()
    events = queue.Queue()
    failures = queue.Queue()

    def read_output():
        try:
            for raw in iter(process.stdout.readline, b""):
                if os.environ.get("GONGGA_REMOTE_DEBUG"):
                    print("FRAME", raw, file=sys.stderr, flush=True)
                frame = json.loads(raw)
                if "event" in frame:
                    events.put(frame)
                elif "id" in frame:
                    with replies_ready:
                        replies[frame["id"]] = frame
                        replies_ready.notify_all()
                else:
                    failures.put(RuntimeError(f"frame has neither event nor id: {frame}"))
            failures.put(RuntimeError("runner closed its output"))
            with replies_ready:
                replies_ready.notify_all()
        except Exception as error:
            failures.put(error)
            with replies_ready:
                replies_ready.notify_all()

    threading.Thread(target=read_output, daemon=True).start()
    next_id = 0

    def frame():
        try:
            return events.get(timeout=timeout)
        except queue.Empty:
            if not failures.empty():
                raise Failure(str(failures.get()))
            raise Failure(f"runner did not answer within {timeout:g}s")

    def request(op):
        nonlocal next_id
        next_id += 1
        wanted = next_id
        if os.environ.get("GONGGA_REMOTE_DEBUG"):
            print("REQUEST", op, file=sys.stderr, flush=True)
        kind, fields = next(iter(op.items()))
        process.stdin.write((json.dumps({"id": wanted, "op": kind, **fields}) + "\n").encode())
        process.stdin.flush()
        with replies_ready:
            if not replies_ready.wait_for(lambda: wanted in replies or not failures.empty(), timeout):
                raise Failure(f"request {wanted} did not answer within {timeout:g}s")
            if wanted not in replies:
                raise Failure(str(failures.get()))
        reply = replies.pop(wanted)
        if "err" in reply:
            raise Failure(reply["err"])
        return reply["ok"]

    def output():
        return events.get(timeout=timeout)

    marker = f"__GONGGA_SKILL_VERIFY_{uuid.uuid4().hex}__"

    def command(script):
        wrapped = (
            script + "\n__gongga_status=$?\n"
            f"printf '\\n{marker}%d\\n' \"$__gongga_status\"\n"
            f"printf '\\n{marker}%d\\n' \"$__gongga_status\" >&2\n"
        )
        request({"write": {"shell": 71, "data": base64.b64encode(wrapped.encode()).decode()}})
        streams = {name: bytearray() for name in ("stdout", "stderr")}
        statuses = {}
        while len(statuses) < 2:
            event = output()
            kind = event.get("event")
            if kind == "end":
                raise Failure(f"shell ended before command sentinel: {event}")
            if kind != "out" or event.get("shell") != 71:
                continue
            name = event["stream"]
            streams[name] += base64.b64decode(event["data"])
            for line in bytes(streams[name]).splitlines(keepends=True):
                if line.startswith(marker.encode()):
                    statuses[name] = int(line[len(marker):].strip())
        return statuses["stdout"], bytes(streams["stdout"]).decode("utf-8", "replace")

    try:
        first = frame()
        hello(first)
        _, cwd = peer_home(peer)
        if not (cwd.startswith("/") or (len(cwd) > 2 and cwd[1] == ":")):
            raise Failure(f"peer did not report a usable cwd: {cwd!r}")
        request({"open": {"shell": 71, "cwd": cwd, "program": None}})
        status, out = command("export GONGGA_REMOTE_VERIFY=kept; printf 'cwd=%s\\n' \"$PWD\"")
        if status != 0 or "cwd=" not in out:
            raise Failure(f"cwd was not set on the peer: {status} {out}")
        status, out = command("printf 'state=%s\\n' \"$GONGGA_REMOTE_VERIFY\"")
        if status != 0 or "state=kept" not in out:
            raise Failure(f"shell state was not preserved: {status} {out}")
        status, _ = command("(exit 7)")
        if status != 7:
            raise Failure(f"expected shell exit 7, got {status}")
        try:
            request({"open": {"shell": 72, "cwd": "/definitely/not/a/gongga-directory", "program": None}})
        except Failure:
            pass
        else:
            raise Failure("an invalid peer directory was accepted")
        request({"write": {"shell": 71, "data": base64.b64encode(b"sleep 60\n").decode()}})
        closed = request({"close": {"shell": 71}})
        if not closed.get("confirmed"):
            raise Failure(f"busy-shell close was not confirmed: {closed}")
    except (BrokenPipeError, KeyError, ValueError, json.JSONDecodeError) as error:
        raise Failure(f"invalid runner response: {error}") from error
    finally:
        try:
            process.stdin.close()
            process.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.communicate()


def verify(args: argparse.Namespace) -> None:
    peer = Peer(args)
    runner = args.runner or "gongga runner"
    greeting = probe(peer, runner)
    runner_verify(peer, runner)
    emit("verified", peer=peer.destination, runner=runner, protocol=greeting["protocol"], cwd_state=True, exit_status=7, invalid_cwd_refused=True, busy_close_confirmed=True)


def check(args: argparse.Namespace) -> None:
    peer = Peer(args)
    runner = args.runner or "gongga runner"
    greeting = probe(peer, runner)
    emit("connected", peer=peer.destination, runner=runner, protocol=greeting["protocol"])


def cli_command() -> list[str] | None:
    """The command layer of this machine, if it has one.

    # Invariant

    The home's own copy comes first: it is the build whose server answers, and
    the build that knows the protocol this machine speaks.
    """
    home = Path(os.environ.get("GONGGA_HOME") or (Path.home() / ".gongga"))
    name = "gongga-cli.exe" if os.name == "nt" else "gongga-cli"
    beside = home / "bin" / name
    if beside.is_file():
        return [str(beside)]
    found = shutil.which("gongga-cli")
    return [found] if found else None


def server_verdict(name: str, timeout: float = 30) -> dict:
    """What the running server says about one remote.

    # Invariant

    The server speaks the protocol, so its answer is the answer; this script
    only carries it. Only a refusal to speak to the peer is read as "not
    usable": a server that is not there, a key it will not take, or a peer with
    no directory to list are not verdicts about compatibility, so they leave the
    question open.

    The vehicle is `list_paths`, because that is the command that opens an
    endpoint today. A command of its own would answer the same thing without
    the listing.
    """
    command = cli_command()
    if command is None:
        return {"asked": False, "reason": "this machine has no command layer"}
    try:
        result = run(command + [json.dumps({"name": "list_paths", "args": {"remote": name}})], timeout=timeout)
    except Failure as error:
        return {"asked": True, "usable": None, "reason": str(error)}
    answer = None
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(frame, dict) and frame.get("t") == "cmd_result":
            answer = frame
    if answer is None:
        return {"asked": True, "usable": None, "reason": "the server did not answer"}
    if answer.get("ok"):
        return {"asked": True, "usable": True}
    message = str(answer.get("message") or "")
    if "no remote named" in message:
        return {
            "asked": True,
            "usable": None,
            "reason": "the server does not know this remote; it reads the remotes file at startup",
            "message": message,
        }
    numbers = re.findall(r"speaks protocol (\d+), and this server speaks protocol (\d+)", message)
    if numbers:
        peer, local = numbers[0]
        return {
            "asked": True,
            "usable": False,
            "reason": "protocol",
            "peer_protocol": int(peer),
            "server_protocol": int(local),
            "message": message,
        }
    return {"asked": True, "usable": None, "reason": "the server refused for another reason", "message": message}


def server_started_before(path: Path) -> bool:
    """Whether the server on this machine started before the remotes file changed.

    # Invariant

    The file is read at server startup, so a server older than the file answers
    about the configuration it started with. Saying so is the difference between
    a verdict and a stale one.
    """
    home = Path(os.environ.get("GONGGA_HOME") or (Path.home() / ".gongga"))
    lock = home / "server.lock"
    if not lock.is_file() or not path.is_file():
        return False
    return lock.stat().st_mtime < path.stat().st_mtime


def reach(table: dict, timeout: float = 20) -> dict:
    """Whether one configured remote answers, answered as data rather than raised."""
    host = str(table.get("host") or "")
    if not host:
        return {"connected": False, "error": "the remote names no host"}
    user, _, address = host.rpartition("@")
    chosen = argparse.Namespace(
        host=address,
        user=user or None,
        port=int(table.get("port", 22)),
        identity=table.get("identity"),
    )
    try:
        greeting = probe(Peer(chosen), str(table.get("runner") or "gongga runner"), timeout=timeout)
    except Failure as error:
        return {"connected": False, "error": str(error)}
    return {"connected": True, "greeting": True, "protocol": greeting["protocol"]}


def list_remotes(args: argparse.Namespace) -> None:
    path = remotes_path(args.remotes)
    if not path.is_file():
        raise Failure(f"no remotes file at {path}; `install` prints the table to add")
    configured = tables(tomllib.loads(path.read_text(encoding="utf-8")))
    if not configured:
        raise Failure(f"{path} configures no remotes; `install` prints the table to add")
    rows = []
    for name in sorted(configured):
        table = configured[name]
        row = {
            "name": name,
            "host": table.get("host"),
            "port": table.get("port", 22),
            "identity": table.get("identity"),
            "runner": table.get("runner", "gongga runner"),
        }
        if args.check:
            row.update(reach(table))
            if row.get("connected"):
                verdict = server_verdict(name)
                if server_started_before(path):
                    verdict["stale_config"] = True
                    verdict["note"] = (
                        "the server reads the remotes file at startup; restart it to see this one"
                    )
                row["server"] = verdict
        rows.append(row)
    emit("remotes", config=str(path), remotes=rows)


def remove(args: argparse.Namespace) -> None:
    peer = Peer(args)
    distribution = args.distribution
    if not distribution.startswith(("/", "C:/", "c:/")) or "/.gongga/bin/gongga-" not in distribution:
        raise Failure("refusing to remove a path outside ~/.gongga/bin/gongga-<hash>")
    if any(ch in distribution for ch in "\n\r;`$"):
        raise Failure("unsafe distribution path")
    checked(peer.ssh(f"{BASH} -c {shlex.quote(f'rm -f {shlex.quote(distribution)}')}"), "removing distribution")
    emit("removed", peer=peer.destination, distribution=distribution)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--remotes",
        help="the machine's remotes file (default: $GONGGA_AGENTS_HOME or ~/.agents, "
        "then remotes.toml)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def peer_options(command, runner=False):
        command.add_argument("--host", required=True)
        command.add_argument("--user")
        command.add_argument("--port", type=int, default=22)
        command.add_argument("--identity")
        if runner:
            command.add_argument("--runner")

    install_parser = sub.add_parser("install", help="build and install a distribution")
    peer_options(install_parser)
    install_parser.add_argument("--remote", help="print the table for this remote")
    install_parser.add_argument("--build-from", default=".")
    install_parser.add_argument("--artifact")
    install_parser.add_argument("--release", action="store_true")
    install_parser.add_argument("--write", action="store_true")
    install_parser.set_defaults(action=install)

    config_parser = sub.add_parser("configure", help="print or update [remotes.<name>]")
    config_parser.add_argument("--remote", required=True)
    config_parser.add_argument("--host", default="")
    config_parser.add_argument("--user")
    config_parser.add_argument("--port", type=int, default=22)
    config_parser.add_argument("--identity")
    config_parser.add_argument("--runner")
    config_parser.add_argument("--write", action="store_true")
    config_parser.add_argument("--remove", action="store_true")
    config_parser.set_defaults(action=configure)

    for name, action in (("check", check), ("verify", verify)):
        command = sub.add_parser(name, help=f"{name} a runner over real SSH")
        peer_options(command, runner=True)
        command.set_defaults(action=action)

    list_parser = sub.add_parser("list", help="list configured remotes")
    list_parser.add_argument("--check", action="store_true", help="greet each remote's runner over SSH")
    list_parser.set_defaults(action=list_remotes)

    remove_parser = sub.add_parser("remove", help="remove one content-addressed distribution")
    peer_options(remove_parser)
    remove_parser.add_argument("--distribution", required=True)
    remove_parser.set_defaults(action=remove)
    return p


def main() -> int:
    args = parser().parse_args()
    try:
        args.action(args)
        return 0
    except Failure as error:
        emit("error", message=str(error))
        return 1
    except (OSError, tomllib.TOMLDecodeError, UnicodeError) as error:
        emit("error", message=str(error))
        return 1


if __name__ == "__main__":
    sys.exit(main())
