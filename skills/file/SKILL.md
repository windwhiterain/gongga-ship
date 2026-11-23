---
name: file
description: Read and change a file on this machine or the session's remote with JSON arguments instead of a command line — `read` (a page, and how much was not shown), `write` (a whole file, atomically), `edit` (replace the span between two anchors, with the middle elided and the removal measured). Use it whenever you would otherwise build a heredoc, a quoted `printf`, or a Python string literal to put text into a file; also use `edit` instead of re-writing a whole file for a small change.
---

# Files, called with JSON

One path in, one answer out. Nothing here parses a word: `content`, `old` and `new`
arrive as the bytes they are, which is the whole point of these being functions.

## `read`

A page of a file, and the facts a reader needs to know what it is *not* seeing:
`total_lines`, `first`, `returned`, `whole`. The page itself is `text`, decoded as
strict UTF-8 -- a file that is not UTF-8 is refused with the byte offset rather than
shown as mojibake.

## `write`

Replaces a whole file. It is atomic: a temp file beside the target, then a replace,
so no reader ever sees half a file, and a refusal changes nothing. A file that starts
with a BOM keeps it. Parent directories are created.

## `edit`

`edits` is one hunk, or a list of them, each `{"old": ..., "new": ...}`:

- `old` is **a list of segments** that must occur in that order, each exactly once
after the previous one ended. The span from the first segment's start to the last
segment's end is replaced by `new` -- **both ends included**.
- One segment is the ordinary exact match. Two segments let you elide a long middle:
`["def big():\n", "    return a\n"]` names a six-line function without writing it
out, which is the token saving, while the two ends still anchor the change.
- The matcher has two tiers: exact, then one that ignores leading whitespace on each
line (the drift a model makes and a file never means). A hunk that matches nothing is
refused with the closest line and its number; nothing is written.
- A segment the file repeats is refused with how many times it occurs: add context, or
add a segment that pins it.
- **All hunks or none**: the whole batch is applied to a copy and the file is written
once. A failure in the third hunk leaves the first two unwritten.
- `all=true` replaces every occurrence of a **single** segment; with more than one
segment it is refused, because an elided span has no repeat to speak of.

The answer says what was destroyed: per hunk, `tier`, `occurrences`, `removed_lines`,
`first_removed`, `last_removed`, `bytes_removed`. Read those numbers -- a span that
swallowed three hundred lines looks exactly like one that swallowed three, until they
are printed.

## Which machine

The **path names its machine**. `remote://whiting_main//home/whting/x` is that peer's
file -- note the two slashes: the name ends at the first `/` and the path is the peer's
own absolute one, which starts with `/` too. `remote://local/x` and a bare `x` are this
machine's, and a bare path with no prefix belongs to the session's own machine
(`GONGGA_REMOTE`, the same default `exec` uses) -- so a session standing on a peer reads
and writes there with no extra argument. Any peer `~/.agents/remotes.toml` configures is
reachable from any session, so one call may look at another machine without the session
moving there; a name no table configures is refused, never a quiet fall back to here.

A **relative** path is resolved by the machine that holds it, against `cwd`, which
defaults to the session's directory -- and that directory is on the session's own
machine. So a relative path naming *another* machine is refused: it would be looked for
under a directory that does not exist there. Write it in that machine's spelling (an
absolute path, with `/`: `C:/Users/...` or `/home/...`), or pass `cwd` as a directory on
that machine.

The bytes travel through the peer's runner; the matching, the BOM and the
all-or-nothing batch are the same code on either machine.

## Calling it

```bash
gongga-cli '{"name":"edit","skill":"file","args":{"path":"src/main.rs","edits":[{"old":["fn big() {\n","}\n"],"new":"fn big() {\n    todo!()\n}\n"}]}}'
```

Line endings and the BOM are the file's, not the caller's: CRLF stays CRLF, and a
refused call leaves the file byte-for-byte as it was.
