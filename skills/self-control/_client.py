"""The harness's own client, imported from the home that serves this shell.

    server = connect()                  # a connected `gongga.Client`
    server.call("get_messages", session="x", range="-1:-1")

`<home>/lib/gongga.py` is the client the server installs beside its home (see
`docs/notes/server-pylib.md`): one method per command it publishes, a `Reply`
for each, an exception when the server refuses. A skill's script imports that
module instead of spawning `gongga-cli`: a command line is a shape for a person
to read, and reading its printed lines back is a second copy of a contract that
already has a Python form. SKILL.md §0b is the rule, this file is the import.

# Invariant

The module beside *this* home, never an installed copy elsewhere: the client and
the server it dials are one build, and the server is what wrote it. Two places
name that directory, and a skill gets one of them from whatever started it --
`GONGGA_SKILL_LIB` from a skill runner, `GONGGA_HOME` from a shell the server
started.
"""

import os
import sys


class Unusable(Exception):
    """There is no client to use: no module to import, or nothing answered the dial.

    One exception for both, so a caller says "this cannot run here" once instead
    of knowing which layer said it.
    """


def lib():
    """The directory holding `gongga.py`, or None when nothing names one."""
    for directory in (
        os.environ.get("GONGGA_SKILL_LIB"),
        os.path.join(os.environ.get("GONGGA_HOME") or "", "lib"),
    ):
        if directory and os.path.isfile(os.path.join(directory, "gongga.py")):
            return directory
    return None


def module():
    """The client module, for its exceptions as well as its `connect`."""
    directory = lib()
    if directory is None:
        raise Unusable(
            "no gongga.py to import: this needs GONGGA_HOME (a shell the server "
            "started has it) or GONGGA_SKILL_LIB"
        )
    if directory not in sys.path:
        sys.path.insert(0, directory)
    import gongga

    return gongga


def connect(timeout=30.0):
    """A connected client for this home's server, or `Unusable` with the reason."""
    gongga = module()
    try:
        return gongga.connect(timeout=timeout)
    except gongga.Unreachable as unreachable:
        raise Unusable(str(unreachable)) from None
    except OSError as missing:
        raise Unusable(f"no key to dial with: {missing}") from None
