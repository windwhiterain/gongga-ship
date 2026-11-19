"""Write what each package skill's functions take, as one JSON Schema document.

The artifact is generated here, by the publisher, and every reader trusts it: the
harness completes `run_skill`'s arguments from it and hands its bytes to a model
beside the skill's own prose, and a reader outside this harness reads the same file.
Asking a package again at request time would be a second opinion about one API, so
`inspect.signature` is read here and nowhere else.

    python schemas.py <skills root>            # write or refresh each package's file
    python schemas.py --check <skills root>    # say which are stale; exit 1 if any

`schema.json` sits beside `__init__.py`, so the file and the package it describes are
one thing the checkout carries, and a signature that moved without its file
regenerated is caught by `--check` before it is published.
"""

import json
import os
import sys

# The contract beside this file is the one reading of a signature.
import gongga_skill

#: The document, in the skill's own directory.
SCHEMA = "schema.json"

#: The one member every document has: a function name to its arguments' schema.
FUNCTIONS = "functions"


def document(directory):
    """What one package's functions take, keyed by function name.

    # Invariant

    The names, which arguments a call must carry, and each one's type are the
    contract's answer, read off the signature: this file restates none of them. A
    parameter whose annotation names no type accepts whatever the caller writes, so
    its schema states no `type` rather than guessing one.
    """
    answer = gongga_skill.manifest(directory)
    functions = {}
    for name, arguments in answer[FUNCTIONS].items():
        properties = {}
        required = []
        for argument in arguments:
            typed = argument.get("type")
            # A union is JSON Schema's own list of types: `dict | list[dict]` is
            # `["object", "array"]`, so the document says what the annotation says.
            # A union states no type: constrained decoding compiles none, and a document
            # that named both would be the one place a reader saw a shape no call can send.
            kinds = [kind.strip() for kind in (typed or "").split("|") if kind.strip()]
            properties[argument["name"]] = {"type": kinds[0]} if len(kinds) == 1 else {}
            if argument["required"]:
                required.append(argument["name"])
        function = {"type": "object", "properties": properties}
        if required:
            function["required"] = required
        functions[name] = function
    return {FUNCTIONS: functions}


def rendered(document):
    """The bytes written to disk: sorted and indented, so one API has one file."""
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def directories(root):
    """Every skill directory under `root`, in the order a table draws them."""
    for name in sorted(os.listdir(root)):
        directory = os.path.join(root, name)
        if os.path.isfile(os.path.join(directory, "SKILL.md")):
            yield directory


def stale(root):
    """The files a package's signature disagrees with, as `path: what moved`."""
    problems = []
    for directory in directories(root):
        path = os.path.join(directory, SCHEMA)
        if not os.path.isfile(os.path.join(directory, gongga_skill.PACKAGE)):
            if os.path.isfile(path):
                problems.append(
                    f"{path}: no __init__.py beside it, so the package it describes is gone"
                )
            continue
        want = rendered(document(directory))
        try:
            with open(path, encoding="utf-8") as kept:
                have = kept.read()
        except OSError:
            have = None
        if have != want:
            problems.append(f"{path}: not what the package's signatures say")
    return problems


def write(root):
    """Regenerates every package's file, and takes away a schema whose package left."""
    for directory in directories(root):
        path = os.path.join(directory, SCHEMA)
        if not os.path.isfile(os.path.join(directory, gongga_skill.PACKAGE)):
            if os.path.isfile(path):
                os.remove(path)
                print(f"schemas: removed {path}, which describes no package")
            continue
        want = rendered(document(directory))
        try:
            with open(path, encoding="utf-8") as kept:
                if kept.read() == want:
                    continue
        except OSError:
            pass
        with open(path, "w", encoding="utf-8", newline="\n") as out:
            out.write(want)
        print(f"schemas: wrote {path}")


def main(argv):
    checking = argv[:1] == ["--check"]
    if checking:
        argv = argv[1:]
    if len(argv) != 1:
        print("schemas: one skills root, and `--check` before it", file=sys.stderr)
        return 2
    root = argv[0]
    if checking:
        problems = stale(root)
        for problem in problems:
            print(f"schemas: {problem}", file=sys.stderr)
        return 1 if problems else 0
    write(root)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
