"""Regression tests for the planning-architect read-only Bash guard.

The guard (``.claude/hooks/readonly-bash-guard.py``) is defence-in-depth: it
must block every filesystem-write vector reachable through the ``Bash`` tool and
must fail closed when it cannot inspect a command, while still allowing the
read-only inspection (git history, ripgrep, cat) the planner depends on. These
cases pin the bypasses and over-blocks raised in the #728 review.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[1] / ".claude" / "hooks" / "readonly-bash-guard.py"

# Commands the read-only planner must be allowed to run.
_ALLOWED = [
    "git log --oneline -20",
    "git diff main...HEAD",
    "git blame safety.py",
    "rg advisor src/",
    "cat pyproject.toml",
    "ls -la docs/",
    "pytest -q 2>&1",  # fd-duplication, not a file redirect
    "rg install-quickstart docs/agent-topology.md",  # mutator word as an arg
    "rg '->' src",  # quoted redirection-like arg
    "git log --format='x > y'",  # quoted '>'
    "grep -n 'pip install' AGENTS.md",  # quoted package command
    "rg writeFileSync web/src",  # write-API name as a search term
    "python3 -c \"print(open('x','r').read())\"",  # read mode
    "python3 -c \"print(open('x').read())\"",  # default read mode
]

# Write vectors the guard must block (each was a real or potential bypass).
_BLOCKED = [
    "echo x > file.txt",
    "echo x >> file.txt",
    "git commit -m x",
    "git push origin main",
    "git checkout -- .",
    "rm -rf build",
    "mv a b",
    "sed -i s/a/b/ f.py",
    "touch new.py",
    "tee out.txt",
    "pip install requests",
    "echo hi 1>out.txt",  # numbered fd redirect
    "printf x 2>>tracked-file",  # numbered fd append
    "echo z 9>out.txt",
    "echo a &> both.txt",  # both streams to file
    "curl -s http://h/x -o notes.txt",
    "wget -O f http://h/x",
    "python3 -c \"open('x','w').write('1')\"",
    "python3 -c \"open('x','r+')\"",  # r+ is write-capable
    "node -e \"require('fs').writeFileSync('a','b')\"",
    "cat x > y && rm z",  # write in a later pipeline segment
    "ls; touch new",
]


def _run(command: str) -> int:
    """Invoke the guard with a hook payload and return its exit code."""
    payload = json.dumps({"tool_input": {"command": command}})
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=payload,
        capture_output=True,
        text=True,
    )
    return proc.returncode


@pytest.mark.parametrize("command", _ALLOWED)
def test_read_only_commands_are_allowed(command: str) -> None:
    assert _run(command) == 0, f"guard should allow: {command}"


@pytest.mark.parametrize("command", _BLOCKED)
def test_write_vectors_are_blocked(command: str) -> None:
    assert _run(command) == 2, f"guard should block: {command}"


def test_malformed_payload_fails_closed() -> None:
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input="not json at all",
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2


def test_missing_command_is_a_noop() -> None:
    proc = subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps({"tool_input": {}}),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0
