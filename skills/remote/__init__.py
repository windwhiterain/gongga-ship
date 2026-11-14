"""Deploy and verify Gongga's SSH execution runner, without a second server.

Only Python's standard library and the local OpenSSH client are needed. No roster,
SSH config override, remote home convention or host-check bypass is assumed.

The published functions are the operations; each takes its options as named
arguments, and `remotes` is the path of this machine's remotes file:

    install(host, user=None, port=22, identity=None, runner=None, remote=None,
            build_from=".", artifact=None, release=False, write=False, remotes=None)
    configure(remote, host="", user=None, port=22, identity=None, runner=None,
              write=False, remove=False, remotes=None)
    check(host, user=None, port=22, identity=None, runner=None, remotes=None)
    verify(host, user=None, port=22, identity=None, runner=None, remotes=None)
    list_remotes(check=False, remotes=None)
    remove(host, distribution, user=None, port=22, identity=None, remotes=None)

Each one answers with one JSON object on stdout, and narration goes to stderr. A
refusal is that same object with `result` `error`, and the call's exit code is 1.

The work itself lives in `_remote.py` beside this file; this module is the API, so
that the names a caller sees are the six operations and nothing else.
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))

_sys.path.insert(0, _HERE)
try:
    import _remote as _implementation
finally:
    _sys.path.remove(_HERE)


def _call(action, **options):
    """One operation with the options it was given; the answer is its exit code."""
    return _implementation.answer(action, _implementation._Options(**options))


def install(
    host,
    user=None,
    port=22,
    identity=None,
    runner=None,
    remote=None,
    build_from=".",
    artifact=None,
    release=False,
    write=False,
    remotes=None,
):
    """Build (or take) a distribution, upload it, greet its runner, and say what it is.

    `host` is the peer; `user`, `port` and `identity` are how to reach it. `artifacts`
    is a built bundle to upload instead of building one from `build_from` (a Gongga
    checkout), and `release` builds the release profile. `runner` is only used to
    compose the printed table. `write` records the runner in `remote`'s table of the
    remotes file, and the server has to be restarted to read it.
    """
    return _call(
        _implementation.install,
        host=host,
        user=user,
        port=port,
        identity=identity,
        runner=runner,
        remote=remote,
        build_from=build_from,
        artifact=artifact,
        release=release,
        write=write,
        remotes=remotes,
    )


def configure(
    remote,
    host="",
    user=None,
    port=22,
    identity=None,
    runner=None,
    write=False,
    remove=False,
    remotes=None,
):
    """Print or update one `[remotes.<name>]` table of the remotes file.

    Without `write` the table is printed and nothing is touched. `remove` takes the
    table out, and needs `write`.
    """
    return _call(
        _implementation.configure,
        remote=remote,
        host=host,
        user=user,
        port=port,
        identity=identity,
        runner=runner,
        write=write,
        remove=remove,
        remotes=remotes,
    )


def check(host, user=None, port=22, identity=None, runner=None, remotes=None):
    """Greet one peer's runner over real SSH and report the protocol it speaks."""
    return _call(
        _implementation.check,
        host=host,
        user=user,
        port=port,
        identity=identity,
        runner=runner,
        remotes=remotes,
    )


def verify(host, user=None, port=22, identity=None, runner=None, remotes=None):
    """Greet a runner and put it through the shell protocol: state, status, close."""
    return _call(
        _implementation.verify,
        host=host,
        user=user,
        port=port,
        identity=identity,
        runner=runner,
        remotes=remotes,
    )


def list_remotes(check=False, remotes=None):
    """Every configured remote as a row; `check` greets each runner, and the server."""
    return _call(_implementation.list_remotes, check=check, remotes=remotes)


def remove(host, distribution, user=None, port=22, identity=None, remotes=None):
    """Delete one content-addressed distribution from a peer.

    `distribution` must be a path under the peer's `~/.gongga/bin/gongga-<hash>`;
    anything else is refused rather than removed.
    """
    return _call(
        _implementation.remove,
        host=host,
        distribution=distribution,
        user=user,
        port=port,
        identity=identity,
        remotes=remotes,
    )
