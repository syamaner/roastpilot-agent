#!/usr/bin/env python3
"""Read-only Bash guard (PreToolUse) for the planning-architect subagent.

Fail-closed defence-in-depth. The primary read-only controls are the agent's
tool list (no ``Edit``/``Write``) and ``permissionMode: plan``; this guard
covers the documented case where a permissive parent session
(``acceptEdits``/``auto``/``bypassPermissions``) discards plan mode, so the
planner's ``Bash`` tool must not become a write path.

It reads the Claude Code PreToolUse hook JSON on stdin, extracts
``tool_input.command``, and exits 2 (block) on any mutation vector or on any
inability to inspect the command. Anything it cannot positively clear is
blocked, never allowed. See ``docs/agent-topology.md`` (F1).
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from typing import Any, NoReturn

# Commands that mutate the filesystem when in command position.
_MUTATORS: frozenset[str] = frozenset(
    {
        "rm",
        "rmdir",
        "mv",
        "cp",
        "dd",
        "tee",
        "truncate",
        "install",
        "mkdir",
        "touch",
        "chmod",
        "chown",
        "chgrp",
        "ln",
        "shred",
        "patch",
    }
)
# git subcommands that write; read-only ones (log, show, diff, status, blame,
# rev-parse, ls-files, cat-file, describe, shortlog) are intentionally absent.
_GIT_WRITES: frozenset[str] = frozenset(
    {
        "commit",
        "add",
        "rm",
        "mv",
        "push",
        "pull",
        "fetch",
        "clone",
        "checkout",
        "switch",
        "reset",
        "restore",
        "rebase",
        "merge",
        "cherry-pick",
        "revert",
        "stash",
        "tag",
        "branch",
        "apply",
        "am",
        "clean",
        "gc",
        "init",
        "remote",
        "config",
        "update-ref",
        "worktree",
    }
)
_PKG: frozenset[str] = frozenset(
    {
        "npm",
        "pnpm",
        "yarn",
        "pip",
        "pip3",
        "uv",
        "poetry",
        "brew",
        "apt",
        "apt-get",
        "gem",
        "cargo",
        "go",
    }
)
_PKG_SUB: frozenset[str] = frozenset(
    {"install", "add", "remove", "uninstall", "update", "upgrade", "ci", "publish", "build", "run"}
)
# Split a command line into simple-command segments on shell control operators.
_SEGMENT_SPLIT = re.compile(r"\s*(?:\|\||&&|\||;|&)\s*")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# A '>' or '>>' that is not fd-duplication ('>&N'): a write to a file.
_FILE_REDIRECT = re.compile(r">>?(?!&\d)")


def _block(reason: str) -> NoReturn:
    """Emit a blocking message and exit 2 (Claude Code's block code)."""
    sys.stderr.write(f"Blocked: {reason} The planning-architect is read-only.\n")
    raise SystemExit(2)


def _strip_quoted(text: str) -> str:
    """Replace single- and double-quoted spans with spaces.

    So a quoted ``>`` or a quoted mutator word (e.g. ``rg '->' src`` or
    ``grep -n 'pip install' AGENTS.md``) is not misread as an operator or a
    write command.
    """
    text = re.sub(r"'[^']*'", " ", text)
    text = re.sub(r'"[^"]*"', " ", text)
    return text


def _check_segment(segment: str) -> None:
    """Block the segment if its command position is a known write vector."""
    try:
        tokens = shlex.split(segment, posix=True)
    except ValueError:
        _block("unparseable shell segment (fail-closed).")
    idx = 0
    while idx < len(tokens) and _ENV_ASSIGN.match(tokens[idx]):
        idx += 1  # skip leading VAR=value assignments
    if idx >= len(tokens):
        return
    cmd = tokens[idx].rsplit("/", 1)[-1]  # basename: /bin/rm -> rm
    args = tokens[idx + 1 :]

    if cmd in _MUTATORS:
        _block(f"mutating command '{cmd}'.")
    if cmd == "git":
        for arg in args:
            if arg.startswith("-"):
                continue
            if arg in _GIT_WRITES:
                _block(f"git write subcommand '{arg}'.")
            break
    if cmd in {"sed", "perl", "gawk", "awk"} and any(
        arg == "-i" or arg.startswith("-i") for arg in args
    ):
        _block(f"in-place edit via '{cmd} -i'.")
    if cmd in {"curl", "wget"} and any(
        arg in ("-o", "-O", "--output", "--output-document")
        or arg.startswith(("-o", "-O", "--output"))
        for arg in args
    ):
        _block(f"'{cmd}' writes downloaded output to a file.")
    if cmd in _PKG and any(arg in _PKG_SUB for arg in args):
        _block(f"package/build mutation via '{cmd}'.")


def _check_interpreter_writes(command: str) -> None:
    """Block an inline interpreter write (``python -c "open(...,'w')"``).

    Runs on the ORIGINAL command text, because the write call lives inside a
    quoted argument that quote-stripping removes. Keyed to an interpreter in
    command position, so a search whose pattern merely mentions a write API
    (e.g. ``rg writeFileSync src``) is not blocked.
    """
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        _block("unparseable shell command (fail-closed).")
    interpreters = {"python", "python3", "node", "nodejs", "ruby"}
    if not any(token.rsplit("/", 1)[-1] in interpreters for token in tokens):
        return
    joined = " ".join(tokens)
    # Match the mode argument of open(...) (after the comma), so a write mode
    # (w/a/x/+) blocks while a read mode (r, rb) and a filename beginning with
    # w/a/x do not.
    if (
        re.search(r"""open\s*\([^)]*,\s*['"][^'")]*[wax+]""", joined)
        or "writeFileSync" in joined
        or "writeFile" in joined
    ):
        _block("inline file write via an interpreter.")


def main() -> int:
    """Inspect the piped hook payload; return 0 to allow, exit 2 to block."""
    raw = sys.stdin.read()
    try:
        payload: Any = json.loads(raw)
    except ValueError:
        _block("read-only guard could not parse the hook payload (fail-closed).")
    try:
        command_value: Any = payload["tool_input"]["command"]
    except (KeyError, TypeError, IndexError):
        command_value = ""
    command = command_value if isinstance(command_value, str) else ""
    if not command.strip():
        return 0

    scannable = _strip_quoted(command)
    if _FILE_REDIRECT.search(scannable):
        _block("output redirection is a write.")
    for segment in _SEGMENT_SPLIT.split(scannable):
        if segment.strip():
            _check_segment(segment)
    _check_interpreter_writes(command)
    return 0


if __name__ == "__main__":
    sys.exit(main())
