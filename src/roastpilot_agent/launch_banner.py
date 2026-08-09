"""Resolved banner lines for ``scripts/roast-live.sh`` (display only, issue #746).

The launcher prints a pre-charge banner whose ``Advisor cfg:`` line is the
operator's documented check for which model + control-teaching prompt a
supervised live roast is about to run under.  That line used to be derived from
a bare ``AppConfig()`` in an inline ``python -c`` heredoc, which reads
``ROASTPILOT_*`` environment variables only — :class:`AppConfig` is a
``BaseSettings`` with no YAML source, so it never sees the operator's saved
config file.  The serving agent resolves its config through
:func:`~roastpilot_agent.config_store.load_app_config` instead
(``effective = env ?? saved file ?? schema default``), so a value saved through
the ``/config`` UI showed the *schema default* on the banner while the agent
genuinely ran the saved value.

This module is the launcher's read-only view of the SAME resolution the serving
agent uses.  It has **no runtime authority**: it never constructs a controller,
never touches MCP, and never mutates config — it formats two strings.

Fail-loud contract: a malformed / unreadable saved-config file makes the banner
*unresolved*, never a plausible-looking schema default.  :func:`main` reports the
reason on stderr and exits non-zero so the launcher prints ``unresolved`` rather
than a wrong prompt version.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

from pydantic import ValidationError

from roastpilot_agent.config import DEFAULT_ADVISOR_MODEL_BY_PHASE, AppConfig

#: Appended to a banner line whose RESOLVED value differs from the schema
#: default, so a non-default arm can never be mistaken for the proven baseline.
EXPERIMENT_TAG = "   ⚠ EXPERIMENT — non-default, watch it"

#: The phases the advisor is consulted in, in roast order. Single-sourced from
#: the per-phase model map's own default so the banner cannot drift from the
#: set of phases ``AdvisorConfig.model_for`` is called with.
ADVISOR_PHASES = tuple(DEFAULT_ADVISOR_MODEL_BY_PHASE)


@dataclass(frozen=True)
class LaunchBannerLines:
    """The resolved text for the launcher's two config-derived banner lines.

    Attributes:
        advisor_cfg: The ``Advisor cfg:`` line — model slug, control-teaching
            prompt version, and the experiment tag when either is non-default.
        trim: The ``Pre-FC trim:`` line — the resolved late-Maillard trim depth
            and whether adaptive depth (#386) is active.
    """

    advisor_cfg: str
    trim: str


def _advisor_line(config: AppConfig) -> str:
    """Format the ``Advisor cfg:`` line from a fully-resolved config.

    The experiment tag compares the resolved model/prompt pair against the
    *schema* defaults, so a value that reached the agent from the saved-config
    file is tagged exactly like one exported into the environment.

    The model reported is the PHASE-RESOLVED one — what
    :meth:`AdvisorConfig.model_for` returns for each advisor-consulted phase,
    which is what ``PydanticAIAdvisor`` actually calls. It is NOT
    ``advisor.model_slug``: ``model_slug_by_phase`` ships populated for every
    advisor phase and is not editable from ``/config``, so a ``model_slug``
    saved there (or exported) is silently shadowed and never used. Printing the
    base slug would announce an arm the roast is not running — the same lie in
    the other direction, so the line names the shadowed value explicitly.

    Args:
        config: The resolved application config (env over saved file over
            schema defaults).

    Returns:
        ``"<model>  ·  prompt <version>"``, plus :data:`EXPERIMENT_TAG` when
        either value is non-default.
    """
    advisor = config.advisor
    fields = type(advisor).model_fields
    default_model = fields["model_slug"].default
    by_phase = {phase: advisor.model_for(phase) for phase in ADVISOR_PHASES}
    resolved = set(by_phase.values())

    if len(resolved) == 1:
        model_text = next(iter(resolved))
    else:
        model_text = "per phase " + ", ".join(
            f"{phase.value}={by_phase[phase]}" for phase in ADVISOR_PHASES
        )
    # Warn only when the operator SET a base slug that then has no effect. A
    # base slug left at the schema default is shadowed too, but silently and
    # harmlessly — saying so on every per-phase roast would be noise.
    if advisor.model_slug != default_model and advisor.model_slug not in resolved:
        model_text += f" (config model_slug {advisor.model_slug} is shadowed per-phase, unused)"

    is_default = resolved == {default_model} and (
        advisor.prompt_version == fields["prompt_version"].default
    )
    tag = "" if is_default else EXPERIMENT_TAG
    return f"{model_text}  ·  prompt {advisor.prompt_version}{tag}"


def _trim_line(config: AppConfig) -> str:
    """Format the ``Pre-FC trim:`` line from a fully-resolved config.

    Reports the RESOLVED depth rather than the historical fixed 65 % literal:
    the operator routinely runs a shallower or deeper cut (e.g. the trim-60
    validation recipe), and the banner claiming 65 % there is the same class of
    lie as the advisor line printing the schema-default prompt.

    ``enabled`` is checked FIRST and reported on its own. With the trim
    disabled, ``RoastControlPolicy._trim_engaged`` always returns ``False`` and
    the controller holds the flat #222 pre-FC floor, so a depth or an ADAPTIVE
    band left in the saved config is dead configuration — announcing it would
    name the treatment arm while the roast runs the baseline.

    Args:
        config: The resolved application config.

    Returns:
        The disabled line when the trim is off, the adaptive-depth line when
        ``adaptive_depth_enabled``, otherwise the fixed-depth line, tagged when
        the resolved state is non-default.
    """
    trim = config.controller.pre_first_crack_levers.late_maillard_trim
    fields = type(trim).model_fields
    heat_target = config.controller.pre_first_crack_levers.heat_target_percent
    if not trim.enabled:
        return (
            f"DISABLED — no trim window; flat {heat_target}% pre-FC floor "
            f"holds to first crack{EXPERIMENT_TAG}"
        )
    if trim.adaptive_depth_enabled:
        return (
            f"ADAPTIVE — #386 RoR-keyed depth, base {trim.base_trim}% "
            f"within {trim.min_trim}–{trim.max_trim}% (experiment, watch the cut)"
        )
    if trim.trim_heat_percent == fields["trim_heat_percent"].default:
        return f"fixed {trim.trim_heat_percent}% (proven roast-6 default)"
    return (
        f"fixed {trim.trim_heat_percent}% "
        f"(schema default {fields['trim_heat_percent'].default}%){EXPERIMENT_TAG}"
    )


def resolve_banner_lines(config: AppConfig) -> LaunchBannerLines:
    """Render both banner lines from an already-resolved config.

    Pure formatting — the caller owns config resolution, which keeps the
    tagging rules unit-testable without touching the filesystem.

    Args:
        config: The resolved application config.

    Returns:
        The rendered :class:`LaunchBannerLines`.
    """
    return LaunchBannerLines(advisor_cfg=_advisor_line(config), trim=_trim_line(config))


def load_banner_lines() -> LaunchBannerLines:
    """Resolve config the way the serving agent does, then render both lines.

    Returns:
        The rendered :class:`LaunchBannerLines`.

    Raises:
        ConfigFileError: If the saved-config file exists but is malformed.
        ValidationError: If the saved-config file holds invalid values.
        OSError: If the saved-config file exists but cannot be read.
    """
    # Imported here (not at module scope) so this module stays importable in a
    # bare interpreter without pulling config_store's filelock/yaml stack in.
    from roastpilot_agent.config_store import load_app_config

    config, _ = load_app_config()
    return resolve_banner_lines(config)


def _one_line(text: str) -> str:
    """Fold any newline in *text* to a space so one value stays one output line.

    Args:
        text: A rendered banner line.

    Returns:
        The same text with every line break replaced by a single space.  Other
        whitespace is left alone — the banner's alignment relies on the double
        spaces around ``·`` and the tag's leading run.
    """
    return " ".join(text.splitlines())


def main() -> int:
    """Print the two banner lines on stdout for ``scripts/roast-live.sh``.

    Line 1 is the ``Advisor cfg:`` text, line 2 the ``Pre-FC trim:`` text; the
    launcher reads them positionally.  A config failure prints the reason on
    stderr and returns non-zero so the launcher falls back to ``unresolved``
    instead of showing a plausible but wrong prompt version.

    Any newline inside a resolved value (a hand-edited saved config can hold a
    multi-line ``model_slug``) is folded to a space, so a value can never split
    the positional two-line contract and shift the trim text onto the advisor
    line.

    Returns:
        ``0`` on success, ``1`` when the saved config could not be resolved.
    """
    from roastpilot_agent.config_store import ConfigFileError

    try:
        lines = load_banner_lines()
    except ConfigFileError as exc:
        print(f"error: saved-config file is malformed — {exc}", file=sys.stderr)
        return 1
    except ValidationError as exc:
        print(f"error: saved-config file has invalid values — {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: saved-config file is unreadable — {exc}", file=sys.stderr)
        return 1
    print(_one_line(lines.advisor_cfg))
    print(_one_line(lines.trim))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entrypoint, driven by the launcher
    raise SystemExit(main())
