"""FC-latency screen classification for the configured advisor model (D151).

The single source for "has the model about to give roast advice been measured
against the ~5 s first-crack advice slot, and what did the measurement say".

**Advisory only — nothing here rejects, clamps, or substitutes a model.** #747
made ``AdvisorConfig.model_slug`` effective, which removed an accidental
protection: while the per-phase map shipped populated, an operator could not put
a tick-busting model on a live roast even by asking for one. The operator chose
a WARNING over a hard guard (D151), because an allow-list needs a source of
truth that goes stale the day a model ships and rejects far from the roast.

Why the warning is worth carrying at all: the advisory call is awaited inline at
the end of ``controller.tick``, and the serve loop is drain-operator-queue ->
tick, so a slow model delays the next telemetry read, the next
``_evaluate_safety``, and the next drain of the operator queue — which is where
the in-UI EMERGENCY STOP is consumed. It is bounded by
``ControllerConfig.advisory_timeout_seconds`` and every failure is fail-closed,
so it cannot hang the loop or actuate anything; but the delay is real, and
D40/D41 measured four models busting the slot by 2-4x.

This module exists so the pre-charge banner and ``GET /api/config`` say the SAME
thing (#754). The banner is a launch-time snapshot, while the documented way to
switch model arms between roasts is ``/config`` (applies next-roast under D78) —
so a classification living only in the banner missed the very path the A/B
workflow uses.
"""

from __future__ import annotations

import enum
import os
from collections.abc import Iterable

from roastpilot_agent.config import (
    FC_LATENCY_BUSTED_ADVISOR_ARMS,
    FC_LATENCY_SCREENED_ADVISOR_ARMS,
    FC_LATENCY_TIGHT_HEADROOM_FRACTION,
    OPENROUTER_BASE_URL,
    AdvisorConfig,
)
from roastpilot_agent.models import RoastPhase


class AdvisorScreenVerdict(enum.Enum):
    """What the FC-latency record says about one configured advisor arm.

    Plain ``Enum`` (D15 idiom, never ``StrEnum``): a string comparison against
    these is a pyright-strict error, so the verdict cannot be tested by
    stringly-typed accident.

    Attributes:
        CLEARED: Measured against the ~5 s slot and comfortably inside it.
        CLEARED_TIGHT: Cleared the roster screen, but its recorded WORST call
            leaves little or no room under the configured hard timeout. A
            distinct state because the screen's ~5 s was a soft threshold on a
            median while ``advisory_timeout_seconds`` is a hard per-call cutoff
            — equal numbers, different meanings.
        BUSTED: Measured and over it.
        NO_SCREEN: Not measured — in this configuration. Not a verdict against
            the model; a statement that the record does not cover it.
    """

    CLEARED = "cleared"
    CLEARED_TIGHT = "cleared_tight"
    BUSTED = "busted"
    NO_SCREEN = "no_screen"


def advice_models(advisor: AdvisorConfig) -> set[str]:
    """The distinct model slugs that can actually answer a roast.

    Resolved through :meth:`AdvisorConfig.model_for` for the phases the
    CONTROLLER itself consults, so this cannot drift from the real gate. Under
    D35 that is DEVELOPMENT alone — pre-FC is deterministic and
    ``_maybe_run_advisory`` returns before consulting there, even on a manual
    request — so a model pinned to a pre-FC slot is correctly excluded: it can
    never answer, and warning about it would be noise.

    Args:
        advisor: The resolved advisor config.

    Returns:
        The distinct slugs an advisory call can dispatch to.
    """
    # Imported lazily: this module is pulled in by ``config_store`` (a light
    # config-layer module) and by the launcher banner, and ``controller`` drags
    # the whole control stack behind it. The gate is still single-sourced.
    from roastpilot_agent.controller import AUTO_ADVICE_PHASES  # noqa: PLC0415

    phases: Iterable[RoastPhase] = AUTO_ADVICE_PHASES
    return {advisor.model_for(phase) for phase in phases}


def classify(
    advisor: AdvisorConfig, slug: str, advisory_timeout_seconds: float
) -> AdvisorScreenVerdict:
    """Classify one resolved slug under *advisor*'s endpoint and reasoning effort.

    The unit of measurement is an ARM — ``(endpoint, slug, reasoning_effort)`` —
    not a slug. Keying on the slug alone inverts the record in a real case:
    ``gpt-5.5`` busts at the provider default but was measured at 2.9 s,
    passing, with ``reasoning_effort="off"``.

    Matching is EXACT on the slug as configured, with no stripping or case
    folding, because ``build_model`` dispatches it verbatim: a normalisation the
    dispatch path does not share would clear one identifier while the provider
    is sent another. So a ``:variant`` suffix, a padded slug, or a different
    endpoint all land in :attr:`AdvisorScreenVerdict.NO_SCREEN` — still a
    warning, just a true one, and erring toward the warning is the safe
    direction.

    Args:
        advisor: The resolved advisor config (supplies endpoint + effort).
        slug: One model slug from :func:`advice_models`.
        advisory_timeout_seconds: The CONFIGURED controller bound. Tightness is
            a relation between a recorded measurement and this number, not a
            property of the model, so it is derived here rather than baked into
            the table — a static partition is silently wrong the moment an
            operator moves the bound.

    Returns:
        The verdict for that arm.
    """
    # Every screen ran on OpenRouter via ``openai_compatible``. ``provider``
    # alone can move the endpoint while ``provider_base_url`` sits unchanged at
    # its inert default (a native-provider config), so BOTH are checked — a
    # URL-only check let a native provider inherit an OpenRouter measurement.
    if advisor.provider != "openai_compatible" or advisor.provider_base_url != OPENROUTER_BASE_URL:
        return AdvisorScreenVerdict.NO_SCREEN
    arm = (slug, advisor.reasoning_effort)
    recorded_max = FC_LATENCY_SCREENED_ADVISOR_ARMS.get(arm)
    if recorded_max is not None:
        headroom = advisory_timeout_seconds - recorded_max
        if headroom < advisory_timeout_seconds * FC_LATENCY_TIGHT_HEADROOM_FRACTION:
            return AdvisorScreenVerdict.CLEARED_TIGHT
        return AdvisorScreenVerdict.CLEARED
    if arm in FC_LATENCY_BUSTED_ADVISOR_ARMS:
        return AdvisorScreenVerdict.BUSTED
    return AdvisorScreenVerdict.NO_SCREEN


def screen_warning(advisor: AdvisorConfig, advisory_timeout_seconds: float) -> str | None:
    """The operator-facing warning for the models that will give advice, or None.

    ``None`` when every resolved arm has a screen that cleared — so an ordinary
    roast on the pinned model stays silent and the warning keeps its meaning. A
    warning that fires on the proven baseline is one the operator learns to
    scroll past, which would cost exactly the case it exists for.

    "Measured, and it busts" and "nothing on record" are kept distinct, because
    the first is a much stronger claim than the second and collapsing them
    either overstates an unknown model or understates a known-slow one.

    Args:
        advisor: The resolved advisor config.
        advisory_timeout_seconds: ``ControllerConfig.advisory_timeout_seconds``
            — the CONTROLLER's bound on the advisory await, which is what caps
            how long a slow call holds the loop. Not
            ``AdvisorConfig.timeout_seconds`` (the provider request timeout);
            the controller's is the one that actually fires.

    Returns:
        The warning text, or ``None`` when there is nothing to warn about —
        every resolved arm cleared with headroom, or no advisor is wired at all.
    """
    # Advisory-paused: no key, so ``build_advisor`` returns ``None`` and NO
    # advisory call happens at all (the controller treats a missing advisor as
    # no advice, never as an unsafe write). Warning that a model will delay the
    # loop when nothing will call it is the cry-wolf failure this whole design
    # avoids — and the launcher documents running key-less on purpose (local
    # Codex P2, folded pre-open). The configured model is still reported; only
    # the latency claim, which presumes a call, is withheld.
    if not os.environ.get(advisor.api_key_env):
        return None
    resolved = advice_models(advisor)
    verdicts = {m: classify(advisor, m, advisory_timeout_seconds) for m in resolved}
    busted = sorted(m for m, v in verdicts.items() if v is AdvisorScreenVerdict.BUSTED)
    unscreened = sorted(m for m, v in verdicts.items() if v is AdvisorScreenVerdict.NO_SCREEN)
    tight = sorted(m for m, v in verdicts.items() if v is AdvisorScreenVerdict.CLEARED_TIGHT)
    notes: list[str] = []
    for slug in tight:
        recorded_max = FC_LATENCY_SCREENED_ADVISOR_ARMS[(slug, advisor.reasoning_effort)]
        # Quote the recorded max AGAINST the configured bound rather than
        # asserting "it will time out": the two numbers are what the operator
        # needs to judge it, and the bound is theirs to change.
        over = "above" if recorded_max >= advisory_timeout_seconds else "close to"
        notes.append(
            f"{slug} cleared the ~5 s screen but its recorded worst call "
            f"({recorded_max:g} s) is {over} the {advisory_timeout_seconds:g} s advisory "
            "timeout — expect occasional timeouts, each one REJECT + hold (fail-closed)"
        )
    if busted:
        notes.append(
            f"{', '.join(busted)} BUSTED the ~5 s post-FC latency screen (D40/D41) — "
            "advice lands late at the drop, and each late call holds the control loop "
            "(next safety check + queued e-stop) for as long as it runs, bounded at "
            f"{advisory_timeout_seconds:g} s"
        )
    if unscreened:
        # Name the dimension that made it unmeasured, so "we never screened this
        # model" reads differently from "we screened it, but not HERE".
        if advisor.provider != "openai_compatible" or (
            advisor.provider_base_url != OPENROUTER_BASE_URL
        ):
            why = " at this endpoint (screens ran on OpenRouter)"
        elif advisor.reasoning_effort is not None:
            why = f" at reasoning_effort={advisor.reasoning_effort}"
        else:
            why = ""
        notes.append(
            f"no FC-latency screen on record for {', '.join(unscreened)}{why} — "
            "the ~5 s post-FC advice slot (D40/D41) is unverified for it"
        )
    return "; ".join(notes) if notes else None
