"""FC-latency screen classification (#747 / D151, shared surface for #754).

The banner and ``GET /api/config`` both read this module, so these tests pin
the classification once and the two consumers' own tests only check that they
render what it returns.
"""

from __future__ import annotations

import pytest

from roastpilot_agent.advisor_screen import (
    AdvisorScreenVerdict,
    advice_models,
    classify,
    screen_warning,
)
from roastpilot_agent.config import AdvisorConfig, ControllerConfig
from roastpilot_agent.controller import AUTO_ADVICE_PHASES
from roastpilot_agent.models import RoastPhase

TIMEOUT = ControllerConfig().advisory_timeout_seconds


@pytest.fixture(autouse=True)
def advisor_key_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Wire an API key, because ``screen_warning`` is silent without one.

    No key means ``build_advisor`` returns ``None`` and nothing calls a model,
    so a latency warning would be describing a call that cannot happen. These
    tests are about the classification, so they assert the key-present world
    EXPLICITLY rather than inheriting whatever the developer's shell exports —
    the ambient value masked this until a key-less run exposed it, which is the
    CI environment.
    """
    monkeypatch.setenv(AdvisorConfig().api_key_env, "test-key")


def test_advice_models_is_the_controllers_own_gate() -> None:
    """Only phases the controller actually consults can produce a warning.

    Under D35 that is DEVELOPMENT alone, so a model pinned to a pre-FC slot is
    excluded: it can never answer an advisory call, and warning about it would
    be noise on a roast that will never call it.
    """
    assert set(AUTO_ADVICE_PHASES) == {RoastPhase.DEVELOPMENT}
    config = AdvisorConfig(
        model_slug="openai/gpt-4o",
        model_slug_by_phase={RoastPhase.PREHEATING: "openai/gpt-5.5"},
    )

    assert advice_models(config) == {"openai/gpt-4o"}


@pytest.mark.parametrize(
    ("slug", "effort", "expected"),
    [
        # D40/D41, provider-default reasoning.
        ("openai/gpt-4o", None, AdvisorScreenVerdict.CLEARED),
        # Recorded max 5.05 s against the 5 s bound — cleared the roster
        # screen, but has no room under the hard per-call cutoff.
        ("openai/gpt-4.1-mini", None, AdvisorScreenVerdict.CLEARED_TIGHT),
        ("anthropic/claude-haiku-4.5", None, AdvisorScreenVerdict.CLEARED_TIGHT),
        ("google/gemini-3.1-flash-lite", None, AdvisorScreenVerdict.CLEARED),
        ("openai/gpt-5.5", None, AdvisorScreenVerdict.BUSTED),
        ("anthropic/claude-sonnet-4.6", None, AdvisorScreenVerdict.BUSTED),
        # The arm that makes slug-keying wrong: 8 Jun measured this one at
        # 2.9 s, PASSING, while the same slug busts at the default.
        ("openai/gpt-5.5", "off", AdvisorScreenVerdict.CLEARED),
        # A cleared model at an effort nobody ran is a different arm.
        ("openai/gpt-4o", "high", AdvisorScreenVerdict.NO_SCREEN),
        # Never measured at all.
        ("openai/gpt-6-hypothetical", None, AdvisorScreenVerdict.NO_SCREEN),
        # Exact matching: a variant, padding, or a lookalike vendor prefix
        # inherits nothing, because the provider is sent the slug verbatim.
        ("openai/gpt-4o:extended", None, AdvisorScreenVerdict.NO_SCREEN),
        ("anthropic/claude-sonnet-4.6:nitro", None, AdvisorScreenVerdict.NO_SCREEN),
        (" openai/gpt-4o ", None, AdvisorScreenVerdict.NO_SCREEN),
        ("not-openai/gpt-4o", None, AdvisorScreenVerdict.NO_SCREEN),
    ],
)
def test_classify_keys_on_the_arm(
    slug: str, effort: str | None, expected: AdvisorScreenVerdict
) -> None:
    """The unit of measurement is ``(endpoint, slug, reasoning_effort)``."""
    config = AdvisorConfig(model_slug=slug, reasoning_effort=effort)  # pyright: ignore[reportArgumentType]

    assert classify(config, slug, TIMEOUT) is expected


def test_a_non_openrouter_endpoint_voids_the_table() -> None:
    """Both halves of the endpoint matter.

    ``provider_base_url`` is operator-editable, and ``provider`` can move the
    endpoint while the base URL sits unchanged at its inert default — a
    native-provider config. A URL-only check let that second case inherit an
    OpenRouter measurement.
    """
    proxied = AdvisorConfig(model_slug="openai/gpt-4o", provider_base_url="http://proxy.local/v1")
    native = AdvisorConfig(provider="openai", model_slug="gpt-4o")

    assert classify(proxied, "openai/gpt-4o", TIMEOUT) is AdvisorScreenVerdict.NO_SCREEN
    assert classify(native, "gpt-4o", TIMEOUT) is AdvisorScreenVerdict.NO_SCREEN


def test_the_pinned_baseline_produces_no_warning() -> None:
    """Silence on the proven arm is the feature, not an omission.

    A warning that fires on every ordinary roast is one the operator learns to
    ignore, which would cost exactly the case it exists for — and it is why a
    hard guard was rejected in favour of this.
    """
    assert screen_warning(AdvisorConfig(), TIMEOUT) is None


def test_the_busted_warning_names_the_loop_delay_and_its_bound() -> None:
    """The warning states the real cost, not a vague "it is slow".

    The advisory call is awaited inline in ``tick()``, so a slow model delays
    the next safety evaluation and the next drain of the operator queue — where
    the in-UI emergency stop is consumed — bounded by the controller's timeout.
    """
    warning = screen_warning(AdvisorConfig(model_slug="openai/gpt-5.5"), TIMEOUT)

    assert warning is not None
    assert "BUSTED" in warning
    assert "queued e-stop" in warning
    assert f"bounded at {TIMEOUT:g} s" in warning


def test_the_unscreened_warning_names_the_dimension_that_made_it_unmeasured() -> None:
    """ "Never screened this model" and "screened, but not HERE" differ.

    They are different problems with different fixes, so the text distinguishes
    them rather than collapsing both into "unknown".
    """
    never = screen_warning(AdvisorConfig(model_slug="openai/gpt-6-hypothetical"), TIMEOUT)
    effort = screen_warning(
        AdvisorConfig(model_slug="openai/gpt-4o", reasoning_effort="high"), TIMEOUT
    )
    endpoint = screen_warning(AdvisorConfig(provider="openai", model_slug="gpt-4o"), TIMEOUT)

    assert never is not None and "no FC-latency screen on record" in never
    assert effort is not None and "at reasoning_effort=high" in effort
    assert endpoint is not None and "at this endpoint" in endpoint


def test_tightness_is_relative_to_the_configured_bound() -> None:
    """Tightness is a relation to the bound, not a property of the model.

    A static tight/cleared partition was silently wrong the moment an operator
    moved ``ROASTPILOT_CONTROLLER__ADVISORY_TIMEOUT_SECONDS`` (local Codex P2,
    folded pre-open): it stayed quiet about gpt-4o under a 1 s bound, and still
    cried timeout for gpt-4.1-mini under a 10 s one, which its 5.05 s worst
    call comfortably fits.
    """
    gpt4o = AdvisorConfig(model_slug="openai/gpt-4o")
    mini = AdvisorConfig(model_slug="openai/gpt-4.1-mini")

    # gpt-4o (3.73 s worst) is comfortable at 5 s and hopeless at 1 s.
    assert classify(gpt4o, "openai/gpt-4o", 5.0) is AdvisorScreenVerdict.CLEARED
    assert classify(gpt4o, "openai/gpt-4o", 1.0) is AdvisorScreenVerdict.CLEARED_TIGHT
    # gpt-4.1-mini (5.05 s worst) is tight at 5 s and comfortable at 10 s.
    assert classify(mini, "openai/gpt-4.1-mini", 5.0) is AdvisorScreenVerdict.CLEARED_TIGHT
    assert classify(mini, "openai/gpt-4.1-mini", 10.0) is AdvisorScreenVerdict.CLEARED
    # And the warning follows the verdict, not a hardcoded list.
    assert screen_warning(mini, 10.0) is None
    assert screen_warning(mini, 5.0) is not None


def test_no_warning_at_all_when_the_agent_runs_advisory_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A key-less agent wires no advisor, so no model can delay anything.

    The launcher documents running key-less on purpose ("omit the key to run
    advisory-paused"), and the controller treats a missing advisor as no advice.
    Warning that a model will hold the control loop when nothing will call it is
    the cry-wolf failure this design exists to avoid (local Codex P2, folded
    pre-open).
    """
    busted = AdvisorConfig(model_slug="openai/gpt-5.5")
    monkeypatch.delenv(busted.api_key_env, raising=False)

    assert screen_warning(busted, TIMEOUT) is None


def test_a_tolerantly_equal_openrouter_url_still_counts_as_screened() -> None:
    """A trailing slash or host case must not unscreen the baseline arm.

    The endpoint check is shared with ``bean_sourcing`` rather than re-derived
    (Claude review, folded pre-open). A raw ``!=`` called
    ``https://openrouter.ai/api/v1/`` "not OpenRouter", so the PROVEN pinned arm
    would have carried a spurious "no screen on record" warning — the
    cry-wolf failure this design exists to avoid, on the one path that must stay
    silent.
    """
    for url in (
        "https://openrouter.ai/api/v1/",
        "https://OpenRouter.ai/api/v1",
        "https://openrouter.ai:443/api/v1",
    ):
        config = AdvisorConfig(provider_base_url=url)
        assert classify(config, config.model_slug, TIMEOUT) is AdvisorScreenVerdict.CLEARED

    # A genuinely different endpoint is still unmeasured, including a
    # non-default port (a LAN proxy), which must NOT be normalised away.
    other = AdvisorConfig(provider_base_url="https://openrouter.ai:8443/api/v1")
    assert classify(other, other.model_slug, TIMEOUT) is AdvisorScreenVerdict.NO_SCREEN
