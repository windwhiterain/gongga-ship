"""Run a skill's entry: the function its package publishes.

A skill may be a Python package -- a directory with `__init__.py` -- instead of a
script with a declared entry. Its public API is its *functions*: whatever the
module defines in global scope under a name that does not start with `_`. `__all__`
is not consulted -- `SKILL.md` is what says what the functions are for, and a name
this file hid would be a second answer to that.

    import gongga_skill
    gongga_skill.run("/path/to/skill", "fetch", {"url": "https://example.com"})

The process form is that call, and its request travels as one JSON object on stdin
-- no argv at all, so nothing a caller wrote is ever read back as words:

    {"skill": "<skill directory>", "function": "fetch", "args": {"url": "..."}}

`args` becomes the function's keyword arguments, as the values arrived: an object
stays an object, a list a list, `true` a `bool`, `null` `None`. Nothing is split
into words and no annotation converts anything, so a parameter annotated `dict` is
handed the dict itself.

Two mistakes are refused here, and each answer names what the function does take: a
`function` the package does not publish, and an `args` key that is no parameter of
it. A required argument left out is Python's own `TypeError`, raised by binding the
call before making it. A stdin that is not that object is refused as well. Each is
one line rather than a traceback: they are the caller's mistake, not this file's.

What the function answers is what a caller gets:

    None            ran, nothing to print
    a string        ran, and this is what to print
    an int          the exit code
    anything else   printed as JSON, so a skill may answer a structure

This file is the contract, not a copy of one. A Python caller imports it for the
in-process form; the harness installs the same bytes as `<home>/lib/gongga_skill.py`
and runs that with one JSON request on stdin. Neither looks for skills: a caller
that has a *name* resolves it first, because "which file does this name open" is
one answer and it is not Python's.
"""

import importlib.util
import inspect
import json
import os
import sys
import traceback

#: The file that makes a skill directory a package.
PACKAGE = "__init__.py"

#: The keys a request carries, and no others: a misspelling of `args` would otherwise
#: arrive as "no arguments", which reads as a function that takes none.
REQUEST = ("skill", "function", "args")

#: Where the harness keeps this file and `gongga.py`, when it says so. The skill
#: being run is a package like any other, so its own `import gongga` has to resolve:
#: the caller that starts a package sets this, and the two lines make the pair
#: importable from the package as well as from this driver.
_skill_lib = os.environ.get("GONGGA_SKILL_LIB")
if _skill_lib and _skill_lib not in sys.path:
    sys.path.insert(0, _skill_lib)


class Refused(TypeError):
    """The request does not fit the skill: a function or a parameter it does not have.

    # Invariant

    It is a `TypeError`, so a Python caller's `except TypeError` still catches it, and
    it has a name of its own so this process can print it as one line: what it says is
    Python's own sentence when Python is what found the mistake.
    """


def package(directory):
    """The module a skill directory is, or None when it is not a package."""
    init = os.path.join(directory, PACKAGE)
    if not os.path.isfile(init):
        return None
    name = os.path.basename(os.path.normpath(directory)) or "skill"
    spec = importlib.util.spec_from_file_location(
        name, init, submodule_search_locations=[directory]
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def functions(module):
    """The public API of a package: name to callable, in declaration order.

    Everything global that is callable and not `_`-prefixed -- which means an
    imported function is published too. A package that wants a helper out of its
    API imports the module (`import json`, then `json.dumps`) or names the helper
    with a leading `_`; `__all__` is not a third spelling this file reads.
    """
    return {
        name: value
        for name, value in vars(module).items()
        if not name.startswith("_")
        and callable(value)
        and not inspect.ismodule(value)
        and not inspect.isclass(value)
    }


def target(directory, function):
    """The function `function` names in the package at `directory`.

    # Invariant

    A caller that named nothing is asking which names exist, so that answer is the list
    of what is published rather than an empty call.
    """
    module = package(directory)
    if module is None:
        raise Refused(f"{directory} is not a Python package: no {PACKAGE}")
    public = functions(module)
    skill = os.path.basename(os.path.normpath(directory))
    if not function:
        raise Refused(f"{skill} publishes: " + (", ".join(public) or "no functions"))
    entry = public.get(function)
    if entry is None:
        raise Refused(
            f"{skill} has no function {function!r}; it publishes: "
            + (", ".join(public) or "nothing")
        )
    return entry


def bound(entry, args):
    """The call `args` makes of `entry`, or the reason it is not one."""
    signature = inspect.signature(entry)
    parameters = list(signature.parameters.values())
    named = {
        parameter.name
        for parameter in parameters
        if parameter.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    }
    # A `**named` parameter takes any name, so nothing is unknown to such a function.
    if not any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        unknown = sorted(name for name in args if name not in named)
        if unknown:
            raise Refused(
                f"{entry.__name__}{signature} has no "
                + ", ".join(repr(name) for name in unknown)
                + "; it takes "
                + (", ".join(sorted(named)) or "nothing")
            )
    try:
        return signature.bind(**args)
    except TypeError as mistake:
        # A missing, doubled or positional-only argument: Python's sentence names the
        # parameter, and a second wording here would be a second answer to the same
        # mistake.
        raise Refused(str(mistake)) from None


def run(directory, function, args):
    """Run `function` of the skill at `directory` with `args`, and answer."""
    entry = target(directory, function)
    call = bound(entry, args)
    return entry(*call.args, **call.kwargs)


def render(answer):
    """What a caller sees of an answer: text, an exit code, or a structure."""
    if answer is None:
        return "", None
    if isinstance(answer, str):
        return answer, None
    if isinstance(answer, int) and not isinstance(answer, bool):
        return "", answer
    return json.dumps(answer, ensure_ascii=False, sort_keys=True), None


def parse(line):
    """The request one line of stdin holds, checked: what to run, and with what.

    # Invariant

    This is the whole of the wire: three keys and their values, so a caller never
    spells a call as a command line and this file never reads one.
    """
    if not line.strip():
        raise ValueError(
            "nothing on stdin: one JSON object, "
            '{"skill": "<directory>", "function": "<name>", "args": {}}'
        )
    try:
        request = json.loads(line)
    except ValueError as bad:
        raise ValueError(f"stdin is not one JSON object: {bad}") from None
    if not isinstance(request, dict):
        raise ValueError(
            "the request is one JSON object: "
            '{"skill": "<directory>", "function": "<name>", "args": {}}'
        )
    unknown = sorted(set(request) - set(REQUEST))
    if unknown:
        raise ValueError(
            "the request has no "
            + ", ".join(repr(name) for name in unknown)
            + "; it is "
            + ", ".join(REQUEST)
        )
    directory = request.get("skill")
    if not isinstance(directory, str) or not directory:
        raise ValueError('"skill" is the directory of the skill to run')
    function = request.get("function")
    if function is not None and not isinstance(function, str):
        raise ValueError('"function" is the name of the function to run')
    args = request.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError('"args" is one object of named arguments: {"<parameter>": <value>}')
    return directory, function, args


def main(argv, stream=None):
    """`python gongga_skill.py`, one JSON request on stdin: `run` as a process."""
    if argv:
        print(
            "gongga_skill: reads one JSON request on stdin and takes no arguments",
            file=sys.stderr,
        )
        return 2
    try:
        directory, function, args = parse((stream or sys.stdin).readline())
    except ValueError as refused:
        print(f"gongga_skill: {refused}", file=sys.stderr)
        return 2
    try:
        text, code = render(run(directory, function, args))
    except Refused as refused:
        # What the caller wrote does not fit the API: said in one line, not as a
        # traceback, which reads as a bug in this file.
        print(f"gongga_skill: {refused}", file=sys.stderr)
        return 2
    except Exception:
        # The skill itself failed. Its traceback is the answer, and it is not this
        # file's refusal.
        traceback.print_exc()
        return 1
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 0 if code is None else code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
