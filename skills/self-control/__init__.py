"""Control how "the self" runs: ask another session a question, and be told when the answer is there.

The package publishes exactly one function; every other top-level name here carries a
leading `_`, because `gongga_skill`'s discovery publishes every callable that does not
and `__all__` is not consulted:

    ask(session, question)          -- deliver a question, and arm the ping that says its answer exists

The heavy lifting lives in the internal modules beside this file: `_client` (import the
harness's own client from this home, SKILL.md "The Python client inside a script") and
`ask_session`. The package runner loads a skill by executing its `__init__.py` (see
`python/gongga_skill.py::package`), which keeps no `sys.modules` entry for the package:
a relative import has no parent to resolve, so the siblings are reached by name while
the directory stands on `sys.path`.

Everything else this skill offers is a description of the server's command schemas, not
a function: SKILL.md is the reference, and the commands themselves are called through
`gongga-cli` or the home's Python client.
"""

import os as _os
import sys as _sys

_HERE = _os.path.dirname(_os.path.abspath(__file__))

_sys.path.insert(0, _HERE)
try:
    import ask_session as _ask_session
finally:
    _sys.path.remove(_HERE)


def ask(session: str, question: str):
    """Ask another session a question; answer with where its answer will be.

    Nothing is waited for: the question goes to that session with `send_message`, and a
    subscription on its `turn_end` tells this session when the turn is over, naming the
    transcript row the answer starts at. A refusal -- no such session, no way to reach
    the server, nobody to ping -- raises RuntimeError with the reason, which the package
    runner prints.
    """
    try:
        return _ask_session.ask(session, question)
    except _ask_session._Refused as refused:
        raise RuntimeError(str(refused)) from None
