"""Control how "the self" runs: ask another session, or open a fresh one with my own configuration.

The package publishes exactly two functions; every other top-level name here
carries a leading `_`, because `gongga_skill`'s discovery publishes every
callable that does not and `__all__` is not consulted:

    ask(session, question)          -- §4b: ask another session and wait for its answer
    new_session(name, ...)          -- §4c: a fresh session that runs on my configuration

The heavy lifting lives in the three internal modules beside this file:
`_client` (import the harness's own client from this home, SKILL.md §0b),
`ask_session` and `new_session`. The package runner loads a skill by executing
its `__init__.py` (see `python/gongga_skill.py::package`), which keeps no
`sys.modules` entry for the package: a relative import has no parent to resolve,
so the siblings are reached by name while the directory stands on `sys.path`.
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))

_sys.path.insert(0, _HERE)
try:
    import ask_session as _ask_session
    import new_session as _new_session
finally:
    _sys.path.remove(_HERE)


def ask(session, question):
    """Ask another session a question and wait for its answer (§4b).

    The answer is the other session's own words from its transcript. A refusal
    -- no such session, no answer within the timeout -- raises RuntimeError
    with the reason, which the package runner prints; the script form in
    `ask_session.py` answers the same question with exit code 2.
    """
    try:
        return _ask_session.ask(session, question)
    except _ask_session._Refused as refused:
        raise RuntimeError(str(refused)) from None


def new_session(name, source=None, model=None, thinking=None, ctx_limit=None,
                cache_tier=None, keep_alive=None, mode=None, remote=None,
                cwd=None, dry_run=False):
    """Open a new session that copies my own configuration (§4c).

    `name` is required; every other argument overrides the field it names,
    `--name value` or positional. Without `--source`, the caller's own session
    ($GONGGA_SESSION) is the source. With `dry_run`, nothing is created: the
    planned `set_session` line is the answer. Failure -- a taken name, an
    unknown remote, no source -- raises RuntimeError with the server's words,
    which the package runner prints.
    """
    return _new_session.new_session(name, source=source, model=model, thinking=thinking,
                                    ctx_limit=ctx_limit, cache_tier=cache_tier,
                                    keep_alive=keep_alive, mode=mode,
                                    remote=remote, cwd=cwd, dry_run=dry_run)
