"""Print a picture so the harness attaches it to this turn.

The harness scans the answer a skill gives for `@image(<path>)` and uploads each one
to the provider's Files API, so the model receives an `input_image` part instead of
having to decode base64 in its head. This package's whole job is to answer exactly
that reference, once, for a path that really is a picture:

    show(paths, check=False)        # a list of paths, one reference line each

The answer is the text, which the harness pastes into the call's result. Every path
that could not be shown says why on stderr, where the harness prints it beside the
answer rather than inside it -- a sentence among references is a sentence the
harness would try to upload.
"""

import os as _os
import typing
import posixpath as _posixpath
import re as _re
import sys as _sys

import gongga_space

#: The extensions the harness accepts, mirrored from `crates/server/src/image.rs`.
#: Anything else is refused here rather than uploaded and refused there.
_KNOWN = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

_MAGIC = [
    (b"\x89PNG", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF8", "image/gif"),
    (b"BM", "image/bmp"),
]


def _die(message):
    """A refusal, in the shape a caller can catch and a package runner can print."""
    raise RuntimeError(message)


def _note(message):
    """A path this call cannot show, said where the harness prints it beside the answer."""
    print(f"image: {message}", file=_sys.stderr)


def _media_type(path, head):
    """The picture type, from the magic bytes, agreeing with the extension.

    The extension alone is not enough: a truncated or empty file named .png used to
    pass, and the reference then failed at upload instead of here. The header is the
    authority; the extension only has to be one the harness accepts.
    """
    ext = _os.path.splitext(path)[1].lstrip(".").lower()
    by_magic = None
    for magic, media in _MAGIC:
        if head.startswith(magic):
            by_magic = media
            break
    if by_magic is None and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        by_magic = "image/webp"
    if by_magic is None:
        return None
    if ext in _KNOWN:
        return by_magic
    return None


def _reference(path, session_space):
    """A picture this machine holds, as a message in this session must spell it.

    # Invariant

    Bare here, where a bare path is this machine's; `remote://local/` in a session
    standing on a peer, where a bare path would be read on that peer. See
    `docs/notes/server-meta.md`.
    """
    return f"remote://local/{path}" if session_space.name else path


def show(paths: list, check: bool = False, cwd: typing.Optional[str] = None):
    r"""One `@image(<path>)` line per readable picture in `paths`.

    # Invariant

    One path rule with `read`/`write`/`edit` (`gongga_space.space_for`): a name that
    carries `remote://` names the machine the bytes are on, else a `cwd` that carries one
    does, else the session's own. `cwd` is also the directory a relative name is joined
    against, so a `cwd` on a peer reads a relative name there. A picture a peer holds
    travels as
    `@image(remote://<name>/<path>)`, which the harness fetches there; one this machine
    holds, in a session standing here, as `@image(<path>)`. A session standing on a peer
    states the machine even for a picture here (`remote://local/`), because a bare path is
    read where that session stands. `check` says what each picture is instead of printing
    a reference. A path is printed whole inside the parentheses, and the parser counts
    them, so a path that opens more than it closes is refused rather than printed.
    """
    if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
        _die('paths is a list of path strings, e.g. ["C:/shots/a.png"]')
    if not paths:
        _die("name at least one picture")
    session_space, _ = gongga_space.space_for(cwd=cwd)
    lines = []
    for given in paths:
        try:
            space, directory = gongga_space.space_for(cwd=cwd, path=given)
        except gongga_space.SpaceError as refused:
            _die(str(refused))
        stated, plain = gongga_space.machine_of(given)
        # A path this machine holds is this machine's, whatever session asked: a caller's
        # own file is here, and a reference naming the peer for it is one nothing can
        # read there. Only a path the call did not place, and that is here, is re-homed.
        if stated is None and space.name and _os.path.exists(plain) and _os.path.isabs(plain):
            space = gongga_space.Space.named(None)
        path = space.resolve(plain, directory)
        if not space.is_local:
            if check:
                lines.append(f"{given}: on remote {space.name}, as {path}")
                continue
            if path.count("(") != path.count(")"):
                _note(
                    f"{given}: the path has an unbalanced parenthesis, and an @image(...) "
                    f"reference is read by counting them -- copy it under another name first"
                )
                continue
            lines.append(f"@image(remote://{space.name}/{path})")
            continue
        try:
            with open(path, "rb") as handle:
                head = handle.read(16)
            size = _os.path.getsize(path)
        except OSError as error:
            _note(f"cannot read {given}: {error}")
            continue
        if size == 0:
            _note(f"cannot read {given}: the file is empty")
            continue
        media = _media_type(path, head)
        if media is None:
            known = ", ".join(_KNOWN)
            _note(
                f"{given} is not a picture this harness takes ({known}); its header is "
                f"not one of them (first bytes: {head[:8]!r}). Convert it first"
            )
            continue
        if check:
            lines.append(f"{given}: {media}, {size} bytes")
            continue
        if path.count("(") != path.count(")"):
            _note(
                f"{given}: the path has an unbalanced parenthesis, and an @image(...) "
                f"reference is read by counting them -- copy it under another name first"
            )
            continue
        lines.append(f"@image({_reference(path, session_space)})")

    if not lines:
        _die("no picture was shown: every path was refused, and stderr says why")
    return "\n".join(lines)
