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
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tomllib

# Never let an SSH login profile inject frames into a runner or probe.
BASH = "bash --norc --noprofile"
PROTOCOL = 1
DEFAULT_HOME = Path("~/.gongga")


class Failure(RuntimeError):
    pass


def emit(kind: str, **fields) -> None:
    print(json.dumps({"result": kind, **fields}, ensure_ascii=False))


def run(argv: list[str], *, stdin=None, timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            argv,
            input=stdin,
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
    text = str(path).replace("\\", "/")
    if os.name == "nt" and len(text) > 2 and text[1] == ":":
        return "/" + text[0].lower() + text[2:]
    return text


def runner_command(runner: str) -> str:
    words = runner.split()
    if not words or any(any(ch.isspace() for ch in word) for word in words):
        raise Failure("runner must contain non-empty command words without whitespace")
    return " ".join(shlex.quote(word) for word in words)


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
    if greeting.get("event") != "hello" or greeting.get("protocol") != PROTOCOL:
        raise Failure(f"unexpected runner greeting: {greeting}")
    return greeting


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
    exists = peer.ssh(f"test -x {shlex.quote(final)}")
    if exists.returncode != 0:
        checked(sftp_put_result(peer, artifact, sftp_temporary), "uploading distribution")
        size = source_size(peer, temporary)
        if size != artifact.stat().st_size:
            raise Failure(f"uploaded size {size} != local size {artifact.stat().st_size}")
        checked(peer.ssh(f"{BASH} -c {shlex.quote(f'mv -f {shlex.quote(temporary)} {shlex.quote(final)}')}"), "activating distribution")
    shell_final = f"{sftp_home}/.gongga/bin/gongga-{digest}{suffix}"
    runner = f"{shell_final} runner"
    greeting = probe(peer, runner, timeout=45)
    emit(
        "installed",
        peer=peer.destination,
        distribution=shell_final,
        sha256=digest,
        runner=runner,
        shell_distribution=final,
        protocol=greeting["protocol"],
        cwd=peer_home(peer)[1],
        config=target_toml(args.target or "build", args, runner),
    )


def sftp_put_result(peer: Peer, source: Path, destination: str):
    return peer.sftp(
        f"put {sftp_quote(local_sftp_path(source))} {sftp_quote(destination)}\n"
        f"chmod 755 {sftp_quote(destination)}\n"
    )


def toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def target_toml(name: str, args: argparse.Namespace, runner: str) -> str:
    host = f"{args.user}@{args.host}" if args.user else args.host
    lines = [f"[targets.{name}]", f"host = {toml_string(host)}"]
    if args.port != 22:
        lines.append(f"port = {args.port}")
    if args.identity:
        lines.append(f"identity = {toml_string(os.path.expanduser(args.identity))}")
    lines.append(f"runner = {toml_string(runner)}")
    return "\n".join(lines) + "\n"


def home_path(value: str | None) -> Path:
    return Path(value or os.environ.get("GONGGA_HOME", DEFAULT_HOME)).expanduser().resolve()


def configure(args: argparse.Namespace) -> None:
    if not args.target or args.target == "local" or not args.target.replace("_", "a").replace("-", "a").isalnum():
        raise Failure("target must be a non-empty TOML bare key other than local")
    path = home_path(args.home) / "config.toml"
    if args.remove:
        if not args.write:
            raise Failure("--remove requires --write")
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if args.target not in data.get("targets", {}):
            raise Failure(f"target {args.target!r} is not configured in {path}")
        args.write = True
    else:
        rendered = target_toml(args.target, args, args.runner or "gongga runner")
        if not args.write:
            print(rendered, end="")
            return
        data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old = data.get("targets", {}).get(args.target)
        desired = tomllib.loads(rendered)[ "targets"][args.target]
        if old is not None and old != desired:
            raise Failure(f"target {args.target!r} already differs in {path}; edit it deliberately")
        if old == desired:
            emit("unchanged", config=str(path), target=args.target)
            return
    # Preserve every other setting and comment. Only this target's exact table
    # header and body are replaced; no generic TOML reformatting is attempted.
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    header = f"[targets.{args.target}]"
    output = []
    index = 0
    found = False
    while index < len(lines):
        if lines[index].strip() == header:
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
    emit("removed" if args.remove else "configured", config=str(path), target=args.target, restart_required=True)


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
        if first.get("event") != "hello" or first.get("protocol") != PROTOCOL:
            raise Failure(f"unexpected greeting {first}")
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
    p.add_argument("--home", help="local Gongga home for configure")
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
    install_parser.add_argument("--target", help="print config for this target")
    install_parser.add_argument("--build-from", default=".")
    install_parser.add_argument("--artifact")
    install_parser.add_argument("--release", action="store_true")
    install_parser.set_defaults(action=install)

    config_parser = sub.add_parser("configure", help="print or update [targets.<name>]")
    config_parser.add_argument("--target", required=True)
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
