"""Read and change one file here: one path in, one answer out.

A call is JSON, so nothing here parses words: `content`, `old` and `new` arrive as
the bytes they are, which is the whole reason these are functions and not programs.

## The three functions

`read` says how much was **not** shown, because a reader that cannot tell a whole
file from its first page will answer about code it never saw.

`write` replaces a whole file, atomically: a temp file beside it, then `os.replace`,
so a reader never sees half a file and a refused call leaves the disk alone.

`edit` changes a span and *proves* it found the bytes it was told to change. Its
`old` is a list of segments that must occur in order; the span from the first
segment's start to the last one's end is replaced by `new`, so the middle may be
elided -- that is the token saving -- while the two ends still anchor it. One
segment is the ordinary exact match, so there is one shape here and no special
case. Every hunk is applied to an in-memory copy and the file is written once: a
call that fails in its second hunk changes nothing.

What a hunk removed is reported, not assumed: `removed_lines`, and the first and
last of them, are how a caller sees that it replaced the three lines it meant and
not three hundred.
"""

import difflib
import os

import gongga_space


class Refused(ValueError):
    """A call this file will not make, said in one line to the caller."""


def _space(remote, cwd):
    """The machine this call's path lives on, and the directory a relative one resolves in.

    # Invariant

    The default is the session's own machine (`GONGGA_REMOTE`), like `exec`; `local`
    or an empty name is this machine. A session works on one machine, so a name that
    is not its own is refused rather than run here -- never a quiet local fallback.
    See `docs/notes/server-remote.md`.
    """
    session_remote = (os.environ.get("GONGGA_REMOTE") or "").strip() or None
    if remote is None:
        name = session_remote
    else:
        stated = str(remote).strip()
        if stated.lower() in ("", "local", "none"):
            name = None
        elif stated == session_remote:
            name = stated
        elif session_remote is None:
            raise Refused(
                f"this call ran nothing: this session names no remote, so it has no peer "
                f"to work on, and {stated!r} is not one it can reach from here. "
                f"`set_session --remote {stated}` moves it there."
            )
        else:
            raise Refused(
                f"this call ran nothing: this session's directory is on remote "
                f"{session_remote!r}, and its files are there -- {stated!r} is another "
                f"machine, which this session has no directory on. A session works on one "
                f"machine: `set_session --remote {stated}` moves it there, and ends these "
                f"shells first."
            )
    space = gongga_space.Space.named(name)
    directory = cwd if cwd is not None else (gongga_space.session_cwd() or os.getcwd())
    return space, directory


def _load(space, path):
    """The file as text, and whether it opens with a BOM."""
    raw = space.read_bytes(path)
    bom = raw.startswith(b"\xef\xbb\xbf")
    if bom:
        raw = raw[3:]
    try:
        return raw.decode("utf-8"), bom
    except UnicodeDecodeError as bad:
        raise Refused(
            f"{path} is not UTF-8: byte {bad.start} is {raw[bad.start:bad.start + 1]!r}. "
            "Nothing was changed."
        ) from None


def _store(space, path, text, bom):
    data = text.encode("utf-8")
    space.write_bytes(path, (b"\xef\xbb\xbf" + data) if bom else data)


def _lines(text):
    """The text as lines that keep their own endings, so CRLF survives."""
    return text.splitlines(keepends=True)


def _offsets(lines):
    at, total = [], 0
    for line in lines:
        at.append(total)
        total += len(line)
    return at, total


def _span(text, segments):
    """Where `segments` sit, as (start, end, tier), or why they do not.

    Each segment must occur exactly once after the previous one ended, so a call
    that names a line the file repeats is refused and told how many times it
    occurs. `exact` and `indent` are the two tiers: the second ignores leading
    whitespace, which is the drift a model makes and a file never means.
    """
    found = _span_exact(text, segments)
    if found is not None:
        return found[0], found[1], "exact"
    found = _span_indent(text, segments)
    if found is not None:
        return found[0], found[1], "indent"
    raise Refused(_hint(text, segments))


def _span_exact(text, segments):
    start, at = None, 0
    for index, segment in enumerate(segments):
        where = text.find(segment, at)
        if where < 0:
            return None
        if text.find(segment, where + 1) >= 0:
            count = text.count(segment)
            raise Refused(
                f"segment {index + 1} occurs {count} times in the file; "
                f"add surrounding lines to the segment or pass its span as more segments: "
                f"{_short(segment)}"
            )
        if start is None:
            start = where
        at = where + len(segment)
    return start, at


def _span_indent(text, segments):
    lines, at, _ = _lines(text), None, 0
    at, _ = _offsets(lines)
    starts = []
    cursor = 0
    for index, segment in enumerate(segments):
        needle = [line.strip() for line in segment.splitlines()]
        got = None
        for line in range(cursor, len(lines)):
            window = [line.strip() for line in lines[line:line + len(needle)]]
            if window == needle:
                got = line
                break
        if got is None:
            return None
        starts.append(got)
        cursor = got + len(needle)
    first, last = starts[0], starts[-1]
    end_line = last + len(segments[-1].splitlines())
    start = at[first]
    end = at[end_line] if end_line < len(at) else len(text)
    return start, end


def _short(segment):
    first = segment.splitlines()[0] if segment.splitlines() else segment
    return repr(first[:80])


def _hint(text, segments):
    """The sentence a hunk that matched nothing gets: where the closest line is."""
    wanted = (segments[0].splitlines() or [""])[0].strip()
    lines = _lines(text)
    best, at = 0.0, None
    for index, line in enumerate(lines):
        ratio = difflib.SequenceMatcher(None, wanted, line.strip()).ratio()
        if ratio > best:
            best, at = ratio, index
    where = f"the closest line is {at + 1}: {lines[at].rstrip()!r}" if at is not None else ""
    return (
        f"no span starts with {_short(segments[0])} (the file has {len(lines)} lines); {where}. "
        "Read the file again and pass what is there now; nothing was changed."
    )


def read(path, offset=0, limit=0, remote=None, cwd=None):
    """Lines `offset` to `offset + limit` of a file, and how many there are in all.

    `remote` is the machine the path lives on (the session's own by default); `cwd`
    is the directory a relative path resolves in.
    """
    space, directory = _space(remote, cwd)
    try:
        path = space.resolve(path, directory)
        text, _ = _load(space, path)
        lines = _lines(text)
        first = max(0, min(int(offset), len(lines)))
        count = len(lines) - first if int(limit) <= 0 else min(int(limit), len(lines) - first)
        return {
            "path": path,
            "total_lines": len(lines),
            "first": first,
            "returned": count,
            "whole": first == 0 and count == len(lines),
            "bytes": len(text.encode("utf-8")),
            "text": "".join(lines[first:first + count]),
        }
    finally:
        space.close()


def write(path, content, remote=None, cwd=None):
    """Replace a whole file with `content`, creating its directory if it is new.

    `remote` is the machine the path lives on (the session's own by default); `cwd`
    is the directory a relative path resolves in.
    """
    if not isinstance(content, str):
        raise Refused(f"write takes `content` as a string, and this one is {type(content).__name__}")
    space, directory = _space(remote, cwd)
    try:
        path = space.resolve(path, directory)
        existed = space.stat(path)["exists"]
        bom = b""
        if existed:
            head = space.read_bytes(path, 3)
            bom = b"\xef\xbb\xbf" if head == b"\xef\xbb\xbf" else b""
        data = bom + content.encode("utf-8")
        space.write_bytes(path, data)
        return {
            "path": path,
            "bytes": len(data),
            "replaced": existed,
            "lines": len(_lines(content)),
        }
    finally:
        space.close()


def edit(path, edits, all=False, remote=None, cwd=None):
    """Replace the span each hunk anchors, in one write, or change nothing.

    `remote` is the machine the path lives on (the session's own by default); `cwd`
    is the directory a relative path resolves in.
    """
    space, directory = _space(remote, cwd)
    try:
        path = space.resolve(path, directory)
        text, bom = _load(space, path)
        if isinstance(edits, dict):
            edits = [edits]
        if not isinstance(edits, list) or not edits:
            raise Refused("edit takes `edits`: one hunk, or a list of them, each with `old` and `new`")
        done = []
        for index, hunk in enumerate(edits, 1):
            if not isinstance(hunk, dict):
                raise Refused(f"hunk {index} is not an object with `old` and `new`")
            old = hunk.get("old")
            new = hunk.get("new")
            if not isinstance(new, str):
                raise Refused(f"hunk {index} needs `new` as a string")
            segments = [old] if isinstance(old, str) else old
            if not isinstance(segments, list) or not segments or any(not (isinstance(s, str) and s) for s in segments):
                raise Refused(f"hunk {index} needs `old`: a string, or a list of non-empty strings")
            if all and len(segments) > 1:
                raise Refused(
                    "`all` replaces every occurrence of one segment; name one segment, "
                    "or drop `all` -- an elided span has no repeat to speak of"
                )
            if all:
                count = text.count(segments[0])
                if count == 0:
                    raise Refused(_hint(text, segments))
                start = text.find(segments[0])
                removed = count * len(segments[0])
                text = text.replace(segments[0], new)
                done.append(_measured(index, "exact", count, start, removed))
                continue
            start, end, tier = _span(text, segments)
            removed_text = text[start:end]
            text = text[:start] + new + text[end:]
            done.append(_measured(index, tier, 1, start, len(removed_text), removed_text))
        _store(space, path, text, bom)
        return {
            "path": path,
            "bytes": len(text.encode("utf-8")),
            "lines": len(_lines(text)),
            "edits": done,
        }
    finally:
        space.close()


def _measured(index, tier, count, start, removed_len, removed_text=None):
    row = {"index": index, "tier": tier, "occurrences": count, "bytes_removed": removed_len}
    if removed_text is None:
        return row
    gone = _lines(removed_text)
    row["removed_lines"] = len(gone)
    row["first_removed"] = gone[0].rstrip() if gone else ""
    row["last_removed"] = gone[-1].rstrip() if gone else ""
    return row
