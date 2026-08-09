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

from pydantic import BaseModel, ValidationError

from roastpilot_agent.config import (
    FC_LATENCY_BUSTED_ADVISOR_ARMS,
    FC_LATENCY_SCREENED_ADVISOR_ARMS,
    OPENROUTER_BASE_URL,
    AdvisorConfig,
    AppConfig,
)
from roastpilot_agent.controller import AUTO_ADVICE_PHASES

#: Appended to a banner line whose RESOLVED value differs from the schema
#: default, so a non-default arm can never be mistaken for the proven baseline.
EXPERIMENT_TAG = "   ⚠ EXPERIMENT — non-default, watch it"

#: The phases that actually consult the advisor, single-sourced from the
#: controller's own gate so the banner cannot drift from it.  Under D35 this is
#: DEVELOPMENT alone — pre-FC is deterministic and ``_maybe_run_advisory``
#: returns before consulting there, even on a manual request.  Deriving the
#: banner from the per-phase model MAP instead would advertise (and tag) a
#: pre-FC model no advisory call can ever reach.
ADVISOR_PHASES = frozenset(AUTO_ADVICE_PHASES)


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


def _changed_fields(model: BaseModel) -> set[str]:
    """Return the names of *model*'s fields whose value differs from the default.

    A field declared with ``default_factory`` has ``FieldInfo.default`` set to
    ``PydanticUndefined``, which never equals the instance value. Comparing
    against it would mark such a field permanently non-default and tag every
    roast, so those fields are skipped rather than guessed at — no field in the
    sections this module reads uses a factory today, and the guard keeps it that
    way if one is added.

    Args:
        model: Any Pydantic model instance.

    Returns:
        The set of field names whose resolved value differs from the schema
        default, excluding ``default_factory`` fields.
    """
    return {
        name
        for name, spec in type(model).model_fields.items()
        if spec.default_factory is None and getattr(model, name) != spec.default
    }


def _arm_matches(slug: str, effort: str | None, known: frozenset[tuple[str, str | None]]) -> bool:
    """Whether the ``(slug, effort)`` ARM appears in *known*.

    Matching is exact on the lower-cased slug: every screen ran on OpenRouter,
    where slugs carry a vendor prefix, so ``someone-else/gpt-4o`` never inherits
    ``openai/gpt-4o``'s measurement. (Callers void the whole table for a
    non-OpenRouter endpoint, which is why no bare native-provider name is
    matched here — an unmeasured endpoint is unmeasured whatever it is called.)

    Surrounding whitespace is stripped: a hand-edited saved config can hold
    ``" openai/gpt-5.5 "``, and without the strip a measured-busting arm would
    silently downgrade to the weaker "no screen on record" wording.

    Args:
        slug: A resolved advisor model slug.
        effort: The configured ``reasoning_effort`` (``None`` = provider default).
        known: Lower-case ``(slug, effort)`` arms to match against.

    Returns:
        ``True`` when the arm was measured.
    """
    return (slug.strip().lower(), effort) in known


def _matches_busted(slug: str, effort: str | None) -> bool:
    """Whether this arm was MEASURED as busting the FC-latency gate.

    Also matches an OpenRouter ``:variant`` suffix (``…-4.6:thinking``) against
    its base slug, because every such variant of a busting model is slower
    still. Deliberately NOT applied to the screened table: a variant must never
    inherit a CLEARING measurement it did not earn (a thinking variant of a fast
    model can itself be slow), so the asymmetry is the safe direction in both
    cases.

    Args:
        slug: A resolved advisor model slug.
        effort: The configured ``reasoning_effort`` (``None`` = provider default).

    Returns:
        ``True`` when this arm, or its pre-``:`` base at the same effort, busts.
    """
    if _arm_matches(slug, effort, FC_LATENCY_BUSTED_ADVISOR_ARMS):
        return True
    base = slug.strip().split(":", 1)[0]
    return base != slug.strip() and _arm_matches(base, effort, FC_LATENCY_BUSTED_ADVISOR_ARMS)


def _latency_screen_note(
    advisor: AdvisorConfig, resolved: set[str], advisory_timeout_seconds: float
) -> str:
    """The FC-latency soft warning for the models that will give advice (D151).

    Advisory only — nothing here rejects or substitutes a model.  Making
    ``model_slug`` effective (#747) removed the accidental protection that had
    made a tick-busting model unreachable, and the operator chose a warning over
    a hard guard: an allow-list would be stale the day a model ships and would
    reject far from the roast, whereas this fires at the pre-charge moment the
    operator can still act on it.

    Three classes, because "measured, and it busts" is a much stronger thing to
    say than "nothing on record" and the two must not be collapsed:

    * screened as clearing the gate — silent, so an ordinary roast stays quiet
      and the warning keeps its meaning;
    * screened and BUSTED — named, with what that costs;
    * neither — named as unverified, which is a prompt to screen it, not a
      verdict against it.

    A screen measures an ``(endpoint, slug, reasoning effort)`` triple, not a
    slug (local Codex P2 + safety-reviewer, both folded pre-open). Slug and
    effort are matched together as an ARM; the ENDPOINT is checked here, once,
    because every screen ran on OpenRouter via ``openai_compatible`` — any other
    provider or base URL voids the whole table rather than matching a row.

    Keying on the slug alone is not merely imprecise, it INVERTS the evidence in
    a real case: ``gpt-5.5`` busts at the provider default but was measured
    passing at 2.9 s with ``reasoning_effort="off"``, an arm the 8 Jun report
    documents as a deliberate speed/cost option. Printing "BUSTED" over the
    operator's own measured-passing configuration is worse than silence — it
    teaches them the warning is noise.

    Args:
        advisor: The resolved advisor config, for the endpoint/effort qualifier.
        resolved: The distinct model slugs :data:`ADVISOR_PHASES` resolves to.
        advisory_timeout_seconds: ``ControllerConfig.advisory_timeout_seconds``
            — the bound the CONTROLLER puts on the advisory await, which is what
            caps how long a slow call holds the loop. Not
            ``AdvisorConfig.timeout_seconds`` (the provider request timeout);
            the controller's is the one that actually fires.

    Returns:
        The note to append to the advisor line, or ``""`` when every resolved
        model has an applicable screen on record that cleared.
    """
    # The endpoint qualifier. ``provider`` alone can move the endpoint while
    # ``provider_base_url`` sits unchanged at its default (a native-provider
    # config leaves the OpenRouter URL inert but unused), so BOTH are checked —
    # keying on the URL alone let a native provider inherit an OpenRouter
    # measurement.
    effort = advisor.reasoning_effort
    voided = (
        advisor.provider != "openai_compatible" or advisor.provider_base_url != OPENROUTER_BASE_URL
    )
    busted = sorted(m for m in resolved if not voided and _matches_busted(m, effort))
    unscreened = sorted(
        m
        for m in resolved
        if voided
        or (
            not _arm_matches(m, effort, FC_LATENCY_SCREENED_ADVISOR_ARMS)
            and not _matches_busted(m, effort)
        )
    )
    notes: list[str] = []
    if busted:
        notes.append(
            f"⚠ {', '.join(busted)} BUSTED the ~5 s post-FC latency screen "
            "(D40/D41) — advice lands late at the drop, and each late call "
            "holds the control loop (next safety check + queued e-stop) for as "
            f"long as it runs, bounded at {advisory_timeout_seconds:g} s"
        )
    if unscreened:
        # Name the dimension that made it unmeasured, so the operator can tell
        # "we never screened this model" from "we screened it, but not HERE" —
        # different problems with different fixes.
        if voided:
            why = " at this endpoint (screens ran on OpenRouter)"
        elif effort is not None:
            why = f" at reasoning_effort={effort}"
        else:
            why = ""
        notes.append(
            f"⚠ no FC-latency screen on record for {', '.join(unscreened)}{why} — "
            "the ~5 s post-FC advice slot (D40/D41) is unverified for it"
        )
    return "".join(f"   {note}" for note in notes)


def _advisor_line(config: AppConfig) -> str:
    """Format the ``Advisor cfg:`` line from a fully-resolved config.

    The experiment tag compares the resolved model/prompt pair against the
    *schema* defaults, so a value that reached the agent from the saved-config
    file is tagged exactly like one exported into the environment.

    The model reported is the PHASE-RESOLVED one — what
    :meth:`AdvisorConfig.model_for` returns for each phase in
    :data:`ADVISOR_PHASES`, which is what ``PydanticAIAdvisor`` actually calls.
    It is NOT ``advisor.model_slug``. Since D151 (#747) the override map ships
    empty, so the two normally agree; they part company the moment a phase slot
    is pinned (a hand-edited saved config, or a
    ``ROASTPILOT_ADVISOR__MODEL_SLUG_BY_PHASE`` JSON blob), which is exactly the
    shape that shadowed ``model_slug`` for six weeks and put a gpt-4o roast on
    record as a gpt-4.1-mini arm. Printing the base slug would announce an arm
    the roast is not running, so the line names an operator-set shadowed slug
    and says only what the operator needs at pre-charge: it is not the advice
    model. It deliberately does NOT enumerate what the base slug IS still used
    for — ``healthcheck`` probes reachability with it, and bean-sourcing
    extraction falls back to it off OpenRouter — because any such list is an
    "only" claim that goes stale the next time a consumer is added.

    The FC-latency note (:func:`_latency_screen_note`) rides on the same
    resolved set, so the warning can never describe a model other than the one
    about to give advice.

    Args:
        config: The resolved application config (env over saved file over
            schema defaults).

    Returns:
        ``"<model>  ·  prompt <version>"``, plus :data:`EXPERIMENT_TAG` when
        either value is non-default, plus the FC-latency note when a resolved
        model busts the gate or has no screen on record.
    """
    advisor = config.advisor
    fields = type(advisor).model_fields
    default_model = fields["model_slug"].default
    # Distinct slugs rather than a per-phase listing: today ADVISOR_PHASES is a
    # single phase, so a phase-by-phase branch would be dead code. Joining the
    # distinct slugs stays correct if a second advice phase is ever added.
    resolved = {advisor.model_for(phase) for phase in ADVISOR_PHASES}
    model_text = " / ".join(sorted(resolved))
    # Warn only when the operator SET a base slug that then gives no advice. A
    # base slug left at the schema default is shadowed too, but silently and
    # harmlessly — saying so on an ordinary roast would be noise.
    if advisor.model_slug != default_model and advisor.model_slug not in resolved:
        model_text += f" (config model_slug {advisor.model_slug} is NOT the roast-advice model)"

    is_default = resolved == {default_model} and (
        advisor.prompt_version == fields["prompt_version"].default
    )
    tag = "" if is_default else EXPERIMENT_TAG
    latency = _latency_screen_note(advisor, resolved, config.controller.advisory_timeout_seconds)
    return f"{model_text}  ·  prompt {advisor.prompt_version}{tag}{latency}"


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
    heat_target = config.controller.pre_first_crack_levers.heat_target_percent
    if not trim.enabled:
        return (
            f"DISABLED — no trim window; flat pre-FC floor {heat_target}% "
            f"(config; a bean's pre_fc_heat overrides){EXPERIMENT_TAG}"
        )
    if trim.adaptive_depth_enabled:
        return (
            f"ADAPTIVE — #386 RoR-keyed depth, base {trim.base_trim}% "
            f"within {trim.min_trim}–{trim.max_trim}% (experiment, watch the cut)"
        )
    # "proven roast-6 default" is a claim about the whole ACTIVE fixed-mode
    # trim, not just its depth: moving the window or the bean-temp threshold
    # changes when the cut engages and so changes the roast. Compare the section
    # field-by-field rather than naming the fields that matter (an enumerated
    # list drifts the next time a field is added), minus the adaptive-only group
    # the model itself declares — those are inert with adaptive mode off, and a
    # tag that fires on the proven baseline arm teaches the operator to ignore it.
    changed = sorted(_changed_fields(trim) - type(trim).ADAPTIVE_ONLY_FIELDS)
    if not changed:
        return f"fixed {trim.trim_heat_percent}% (proven roast-6 default)"
    return f"fixed {trim.trim_heat_percent}% (non-default: {', '.join(changed)}){EXPERIMENT_TAG}"


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
