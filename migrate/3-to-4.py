#!/usr/bin/env python3
"""Take a gongga home from version 3 to version 4.

Version 3 let a remote be a `[remotes.<name>]` table in the home's `config.toml`
(and `[targets.<name>]` before the word changed). That file is the pinned content's:
every install merges it three ways, so a host name, a private key path and a runner
path -- the three things a remote is made of -- were one careless commit away from
being published. Version 4 keeps them where every other fact about this machine
lives, `~/.agents/remotes.toml`, which is not a repository.

The server runs it at startup, before it opens the database:

    python 3-to-4.py --home ~/.gongga --db ~/.gongga/gongga.db

It changes two files and nothing else -- the version file belongs to the server,
which writes it only once a migrator has exited zero. Running it twice has to be a
no-op, because a step that failed once is run again.

Where "the machine's file" is: `$GONGGA_AGENTS_HOME/remotes.toml`, else
`~/.agents/remotes.toml`. A table whose name is already there is left alone when the
two agree, and is a hard failure when they do not: two peers with one name is a
choice only a person can make, and a step that guesses would silently pick one.
Nothing is deleted from `config.toml` until the move it belongs to has succeeded.
"""

import argparse
import os
import re
import sys
from pathlib import Path

# The table a remote may arrive in. `targets` is version 3's first spelling of
# `remotes`, and a home can still carry either.
HEADER = re.compile(r"^\[(remotes|targets)\.([^\]]+)\]\s*$")


def machine_file() -> Path:
    """Where this machine keeps the facts about other machines."""
    base = os.environ.get("GONGGA_AGENTS_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".agents"
    return root / "remotes.toml"


def split_tables(lines: list[str]) -> tuple[list[str], dict[str, list[str]]]:
    """`config.toml` without its remote tables, and those tables by name.

    A table is its header and every line after it up to the next header, which is
    what a TOML table is: something beginning at a `[` line and ending at another.
    Comments between tables therefore stay exactly where the operator put them.
    """
    keep: list[str] = []
    found: dict[str, list[str]] = {}
    index = 0
    while index < len(lines):
        match = HEADER.match(lines[index].strip())
        if not match:
            keep.append(lines[index])
            index += 1
            continue
        name = match.group(2).strip()
        # The new file has one spelling. A home that carried `[targets.x]` arrives as
        # `[remotes.x]`, so what it reads is what it says.
        body = [f"[remotes.{name}]\n"]
        index += 1
        while index < len(lines) and not lines[index].lstrip().startswith("["):
            body.append(lines[index])
            index += 1
        # The blank lines that separated it from the next table belong to the file.
        while body and not body[-1].strip():
            body.pop()
        if keep and keep[-1].strip():
            keep.append("\n")
        found[name] = body
    return keep, found


def existing(path: Path) -> dict[str, list[str]]:
    """The remote tables a file already has, by name."""
    if not path.is_file():
        return {}
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    _, tables = split_tables(lines)
    return tables


def same(one: list[str], other: list[str]) -> bool:
    """Two tables saying the same thing, whatever their whitespace."""
    def words(table: list[str]) -> list[str]:
        out = []
        for line in table[1:]:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(re.sub(r"\s+", " ", line))
        return out
    return words(one) == words(other)


def append(path: Path, table: list[str]) -> None:
    """Adds one table to the machine's file, keeping what is already there."""
    body = "".join(table)
    if not body.endswith("\n"):
        body += "\n"
    if path.is_file():
        text = path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text += "\n"
        if text.strip():
            text += "\n"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = ""
    path.write_text(text + body, encoding="utf-8", newline="\n")


def migrate(home: Path, done: list[str]) -> int:
    config = home / "config.toml"
    if not config.is_file():
        return 0
    lines = config.read_text(encoding="utf-8").splitlines(keepends=True)
    keep, found = split_tables(lines)
    if not found:
        return 0

    target = machine_file()
    already = existing(target)
    for name, table in found.items():
        if name in already:
            if not same(table, already[name]):
                print(
                    f"3-to-4: remote {name!r} says one thing in {config} and another in "
                    f"{target}; choose one and run this again",
                    file=sys.stderr,
                )
                return 1
            done.append(f"{name} was already in {target}")
            continue
        append(target, table)
        already[name] = table
        done.append(f"{name} moved to {target}")

    # Only now, with every table either moved or already there, is the old copy safe
    # to take out: a step that dies before this line runs again and finds nothing left.
    config.write_text("".join(keep), encoding="utf-8", newline="\n")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", required=True, help="the gongga home directory")
    ap.add_argument("--db", required=True, help="the database inside it")
    args = ap.parse_args()

    done: list[str] = []
    status = migrate(Path(args.home), done)
    if status == 0:
        print("3-to-4: " + (", ".join(done) if done else "no remote in config.toml"))
    return status


if __name__ == "__main__":
    sys.exit(main())
