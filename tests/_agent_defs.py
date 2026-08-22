"""Fail-closed frontmatter grammar shared by agent-governance test guards."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final, NoReturn

AGENTS_DIR: Final = Path(__file__).resolve().parents[1] / ".claude" / "agents"
"""Authoritative directory containing committed Claude role definitions."""

REQUIRED_KEYS: Final = frozenset({"name", "description", "tools", "model", "effort"})
"""Closed set of frontmatter keys every committed role must provide."""

OPTIONAL_KEYS: Final = frozenset({"permissionMode"})
"""Closed set of frontmatter keys a committed role may additionally provide."""

_OPENING_MARKER: Final = re.compile(r"^---\n")
_FIELD: Final = re.compile(r"([A-Za-z][A-Za-z0-9_]*): ([^\s\r\n][^\r\n]*)\n")
_TOOL: Final = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_YAML_VALUE_INDICATORS: Final[frozenset[str]] = frozenset("-?:,[]{}#&*!|>'\"%@`")
_YAML_IMPLICIT_WORDS: Final[frozenset[str]] = frozenset(
    {"~", "null", "true", "false", "yes", "no", "on", "off", ".inf", ".nan"}
)
_YAML_NUMBER: Final = re.compile(
    r"[-+]?(?:[0-9][0-9_]*(?:\.[0-9_]*)?(?:[eE][-+]?[0-9_]+)?|"
    r"\.[0-9_]+(?:[eE][-+]?[0-9_]+)?|"
    r"0[xX][0-9a-fA-F_]+|0[oO][0-7_]+|0[bB][01_]+|\.(?:inf|nan))$"
)
_YAML_DATE_LIKE: Final = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}(?:[Tt ][^\r\n]+)?$")


class AgentFrontmatterError(ValueError):
    """Raised when an agent definition violates the closed frontmatter grammar."""


def _error(source: str | Path, detail: str) -> NoReturn:
    """Raise one source-named frontmatter failure without relying on assertions."""
    raise AgentFrontmatterError(f"{source}: {detail}")


def _is_yaml_implicit_scalar(value: str) -> bool:
    """Return whether a conservative YAML loader could coerce a plain scalar."""
    lowered = value.lower()
    return (
        lowered in _YAML_IMPLICIT_WORDS
        or _YAML_NUMBER.fullmatch(value) is not None
        or _YAML_DATE_LIKE.fullmatch(value) is not None
    )


def agent_files(directory: Path = AGENTS_DIR) -> list[Path]:
    """Return the nonempty, sorted immediate roster from one agent directory.

    Args:
        directory: Directory whose ``*.md`` files form the governed roster.

    Returns:
        Sorted immediate Markdown definition paths.

    Raises:
        AgentFrontmatterError: If the roster directory is missing, invalid, or empty.
    """
    if not directory.exists():
        _error(directory, "agent roster directory is missing")
    if not directory.is_dir():
        _error(directory, "agent roster path is not a directory")
    files = sorted(directory.glob("*.md"))
    if not files:
        _error(directory, "agent roster is empty")
    if any(path.is_symlink() or not path.is_file() for path in files):
        _error(directory, "agent roster contains a non-file definition")
    return files


def split_frontmatter(
    text: str, source: str | Path = "<agent definition>"
) -> tuple[dict[str, str], str]:
    """Parse one leading frontmatter block and return its fields with the exact body.

    The accepted grammar is byte-zero ``---\\n``, one or more unquoted
    ``key: value\\n`` fields, exact ``---\\n``, then arbitrary body text. The
    first colon alone separates each key from its value, preserving later colons.

    Args:
        text: Complete decoded role-definition content.
        source: Source name included in every failure.

    Returns:
        The validated field mapping and unmodified body text.

    Raises:
        AgentFrontmatterError: If the leading block violates the closed grammar.
    """
    marker = _OPENING_MARKER.match(text)
    if marker is None:
        _error(source, "frontmatter must begin at byte zero with ---\\n")
    fields: dict[str, str] = {}
    position = marker.end()
    while True:
        end = text.find("\n", position)
        if end < 0:
            _error(source, "frontmatter terminator is missing")
        line = text[position : end + 1]
        if line == "---\n":
            break
        field = _FIELD.fullmatch(line)
        if field is None:
            _error(source, "frontmatter field is malformed")
        key, value = field.groups()
        if value[0] in _YAML_VALUE_INDICATORS:
            _error(source, "frontmatter field value is quoted or structured")
        if _is_yaml_implicit_scalar(value):
            _error(source, "frontmatter field value is implicitly typed")
        if (
            value != value.rstrip()
            or any(not character.isprintable() for character in value)
            or any(
                character == "#" and index > 0 and value[index - 1].isspace()
                for index, character in enumerate(value)
            )
        ):
            _error(source, "frontmatter field value is malformed")
        if key in fields:
            _error(source, f"frontmatter key {key!r} is duplicated")
        fields[key] = value
        position = end + 1
    if not fields:
        _error(source, "frontmatter has no fields")
    allowed = REQUIRED_KEYS | OPTIONAL_KEYS
    unknown = set(fields) - allowed
    if unknown:
        _error(source, f"frontmatter has unknown keys: {sorted(unknown)}")
    missing = REQUIRED_KEYS - set(fields)
    if missing:
        _error(source, f"frontmatter is missing required keys: {sorted(missing)}")
    return fields, text[end + 1 :]


def _read_agent(path: Path) -> str:
    """Decode one definition without silently accepting malformed bytes."""
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        _error(path.name, "agent definition cannot be read as UTF-8")


def parse_frontmatter(path: Path) -> dict[str, str]:
    """Return the validated frontmatter fields from one definition path.

    Args:
        path: Agent definition to parse.

    Returns:
        Validated closed-grammar fields.

    Raises:
        AgentFrontmatterError: If the definition cannot be read or is malformed.
    """
    fields, _body = split_frontmatter(_read_agent(path), source=path.name)
    return fields


def agent_body(path: Path) -> str:
    """Return one definition's exact body after validated leading frontmatter.

    Args:
        path: Agent definition whose body is required.

    Returns:
        Exact unmodified body text after the first frontmatter terminator.

    Raises:
        AgentFrontmatterError: If the definition cannot be read or is malformed.
    """
    _fields, body = split_frontmatter(_read_agent(path), source=path.name)
    return body


def agent_tools(path: Path) -> set[str]:
    """Return the unique, closed-grammar comma-separated tools for one role.

    Args:
        path: Agent definition whose tools field is required.

    Returns:
        Unique tool names from the validated frontmatter.

    Raises:
        AgentFrontmatterError: If a tool name is malformed, empty, or duplicated.
    """
    tools: set[str] = set()
    for raw_tool in parse_frontmatter(path)["tools"].split(","):
        tool = raw_tool.strip()
        if not tool or _TOOL.fullmatch(tool) is None:
            _error(path.name, "frontmatter tools field is malformed")
        if tool in tools:
            _error(path.name, f"frontmatter tool {tool!r} is duplicated")
        tools.add(tool)
    return tools
