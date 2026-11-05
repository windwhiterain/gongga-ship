#!/usr/bin/env python3
"""Take a gongga home from version 4 to version 5.

Version 4 named the window's own file `cli.toml`. That name came from a time when
the command line owned it; by version 5 it is the window's alone -- the sticky
entries, the render settings and the connection a window dials are all client state,
and the command layer reads none of it. So the file is `tui.toml`.

The server runs it at startup, before it opens the database:

    python 4-to-5.py --home ~/.gongga --db ~/.gongga/gongga.db

Renaming is the whole step, and it changes one file that holds settings a person
chose -- so it moves the bytes and never rewrites them. Running it twice is a no-op:
a home that already has `tui.toml` is left exactly as it is, and a home with neither
file (a home that never ran a window) has nothing to do.
"""

import argparse
import os
import sys
from pathlib import Path

#: What version 4 called the file, and what version 5 calls it.
WAS = "cli.toml"
NOW = "tui.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--home", required=True, help="the gongga home to move forward")
    # The database is passed to every step and this one does not touch it.
    parser.add_argument("--db", help="the home's database; unused here")
    args = parser.parse_args()

    home = Path(args.home).expanduser()
    was, now = home / WAS, home / NOW
    if not was.is_file():
        print(f"{was} is not there: nothing to rename")
        return 0
    if now.exists():
        # A step that ran once, or a window that has already written the new name:
        # two files are two answers to "where is my state", and only a person can
        # choose between them.
        print(f"{now} is already there: leaving {was} and {now} as they are")
        return 0
    os.replace(was, now)
    print(f"{was} -> {now}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
