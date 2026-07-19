"""Deterministic, no-spend self-test for the #588 bean-sourcing bake-off harness.

Drives ``scripts/bakeoff_bean_sourcing.py``'s REAL pipeline over the committed
corpus with a PydanticAI ``FunctionModel`` recorded-response double (mirroring
``tests/test_bean_sourcing.py``) — no key, no network, no paid model call — and
asserts the section-5.1 scoring is correct: gold values score ``COR``, a wrong
value ``INC``, an abstention on a gold-absent field ``ABS-COR``, an invented
value on a gold-absent field ``SPU``, plus the altitude RANGE contract, ``PAR``,
whole-page-error handling, the metrics/axes, and the section-5.2 statistics
(Wilson / exact McNemar / page-clustered paired bootstrap). This locks the
scoring + stats so the (gated, paid) roster run only introduces the model, never
new scoring logic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bakeoff_bean_sourcing as bo  # noqa: E402

from roastpilot_agent.config import AdvisorConfig  # noqa: E402
from roastpilot_agent.models import BeanFieldSource, BeanProfileDraft  # noqa: E402

_ADVISOR_CONFIG = AdvisorConfig()


# --- FunctionModel doubles ---------------------------------------------------


def _model_returning(args: dict[str, Any]) -> FunctionModel:
    """A double whose extraction always emits ``args`` via the output tool."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


def _model_text_only() -> FunctionModel:
    """A double that only ever returns prose — never the output tool, so the
    structured extraction exhausts retries and the page fails to draft."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("no structured output")])

    return FunctionModel(respond)


def _page(pages: list[bo.CorpusPage], prefix: str) -> bo.CorpusPage:
    return next(p for p in pages if p.slug.startswith(prefix))


@pytest.fixture
def corpus() -> list[bo.CorpusPage]:
    return bo.load_corpus(bo.DEFAULT_FIXTURES_DIR)


# --- Corpus ------------------------------------------------------------------


def test_load_corpus_pairs_and_labels_every_field(corpus: list[bo.CorpusPage]) -> None:
    assert len(corpus) >= 8
    for page in corpus:
        assert page.url.startswith("https://")
        assert page.html
        for spec in bo.FIELD_SPECS:
            gold = page.gold_fields[spec.name]
            assert ("value" in gold) ^ (gold.get("absent") is True), (page.slug, spec.name)


# --- The four headline scoring cases, through the REAL pipeline ---------------


async def _score(page: bo.CorpusPage, model: FunctionModel) -> dict[str, bo.Outcome]:
    draft, error = await bo.draft_for_page(page, advisor_config=_ADVISOR_CONFIG, model=model)
    return bo.score_page(page, draft, error)


@pytest.mark.asyncio
async def test_gold_values_score_cor(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "cbc-costa-rica")
    model = _model_returning(
        {
            "name": "Costa Rica: La Minita Estate, Tarrazu",
            "country": "Costa Rica",
            "bean_origin": "Tarrazu",
            "farm": "La Minita Estate",
            "bean_varietal": "Caturra",
            "processing": "washed",
            "bean_species": None,
            "altitude_m": None,
            "description": "orange citrus, caramelized sugar, chocolate, acidity, medium body",
            "is_blend": False,
        }
    )
    outcomes = await _score(page, model)
    matched = ("origin", "region", "farm", "variety", "process", "tasting_notes", "is_blend")
    for field_name in matched:
        assert outcomes[field_name] is bo.Outcome.COR, field_name


@pytest.mark.asyncio
async def test_wrong_value_scores_inc(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "cbc-costa-rica")
    model = _model_returning(
        {
            "name": "Costa Rica: La Minita Estate, Tarrazu",
            "country": "Costa Rica",
            "bean_origin": "Tarrazu",
            "processing": "natural",  # gold is washed -> a contradiction
            "is_blend": True,  # gold is false
        }
    )
    outcomes = await _score(page, model)
    assert outcomes["process"] is bo.Outcome.INC
    assert outcomes["is_blend"] is bo.Outcome.INC


@pytest.mark.asyncio
async def test_abstain_on_absent_scores_abs_cor(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "cbc-costa-rica")  # species is gold-absent here
    model = _model_returning(
        {"name": "Costa Rica La Minita", "country": "Costa Rica", "bean_species": None}
    )
    outcomes = await _score(page, model)
    assert outcomes["species"] is bo.Outcome.ABS_COR


@pytest.mark.asyncio
async def test_invent_on_absent_scores_spu(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "cbc-costa-rica")  # species is gold-absent here
    model = _model_returning(
        {"name": "Costa Rica La Minita", "country": "Costa Rica", "bean_species": "arabica"}
    )
    outcomes = await _score(page, model)
    assert outcomes["species"] is bo.Outcome.SPU


@pytest.mark.asyncio
async def test_variety_partial_scores_par(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "onyx-ecuador")  # gold variety = ["Typica Mejorado"]
    model = _model_returning(
        {"name": "Ecuador La Papaya Typica", "country": "Ecuador", "bean_varietal": "Typica"}
    )
    outcomes = await _score(page, model)
    assert outcomes["variety"] is bo.Outcome.PAR


@pytest.mark.asyncio
async def test_page_extraction_failure_scores_mis_and_err(corpus: list[bo.CorpusPage]) -> None:
    page = _page(corpus, "cbc-costa-rica")
    outcomes = await _score(page, _model_text_only())
    # gold-present fields on a crashed page are recall misses...
    assert outcomes["origin"] is bo.Outcome.MIS
    assert outcomes["process"] is bo.Outcome.MIS
    # ...and gold-absent fields earn NO abstention credit (ERR, not ABS-COR).
    assert outcomes["species"] is bo.Outcome.ERR


# --- Altitude RANGE / scalar contract (section 5.1), unit-level ---------------


def _draft(altitude_m: int | None, altitude_source: BeanFieldSource | None) -> BeanProfileDraft:
    sources: dict[str, BeanFieldSource] = {}
    if altitude_source is not None:
        sources["altitude_m"] = altitude_source
    return BeanProfileDraft(
        name="X",
        bean_origin="Y",
        initial_heat_percent=100,
        initial_fan_percent=30,
        target_drop_temp_c=194.0,
        target_development_percent=14.0,
        default_bean_weight_grams=250.0,
        scouting_note="scouting",
        altitude_m=altitude_m,
        field_sources=sources,
    )


def _alt_spec() -> bo.FieldSpec:
    return next(s for s in bo.FIELD_SPECS if s.name == "altitude")


def test_altitude_range_in_range_flagged_is_cor() -> None:
    gold = {"value": {"min_m": 1600, "max_m": 1900}}
    draft = _draft(1750, "origin_estimated")
    assert bo.classify_field(_alt_spec(), gold, draft) is bo.Outcome.COR


def test_altitude_range_in_range_on_page_is_inc() -> None:
    gold = {"value": {"min_m": 1600, "max_m": 1900}}
    draft = _draft(1750, "on_page")  # in range but NOT flagged estimated -> INC
    assert bo.classify_field(_alt_spec(), gold, draft) is bo.Outcome.INC


def test_altitude_range_abstain_is_mis() -> None:
    gold = {"value": {"min_m": 1600, "max_m": 1900}}
    assert bo.classify_field(_alt_spec(), gold, _draft(None, None)) is bo.Outcome.MIS


def test_altitude_scalar_exact_is_cor() -> None:
    gold = {"value": 1400}
    assert bo.classify_field(_alt_spec(), gold, _draft(1400, "on_page")) is bo.Outcome.COR


def test_altitude_scalar_near_is_par() -> None:
    gold = {"value": 1400}
    # 1550 is >2% but <15% off -> PAR
    assert bo.classify_field(_alt_spec(), gold, _draft(1550, "on_page")) is bo.Outcome.PAR


def test_altitude_scalar_far_is_inc() -> None:
    gold = {"value": 1400}
    assert bo.classify_field(_alt_spec(), gold, _draft(900, "on_page")) is bo.Outcome.INC


def test_altitude_absent_abstain_is_abs_cor() -> None:
    gold: dict[str, Any] = {"absent": True}
    assert bo.classify_field(_alt_spec(), gold, _draft(None, None)) is bo.Outcome.ABS_COR


def test_altitude_absent_value_is_spu() -> None:
    gold: dict[str, Any] = {"absent": True}
    assert bo.classify_field(_alt_spec(), gold, _draft(1800, "on_page")) is bo.Outcome.SPU


# --- Match-function edges ----------------------------------------------------


_compare_text = bo._compare_text  # pyright: ignore[reportPrivateUsage]
_compare_enum = bo._compare_enum  # pyright: ignore[reportPrivateUsage]


def test_text_multi_origin_partial() -> None:
    assert _compare_text("Colombia, Ethiopia", "Colombia") is bo.Outcome.PAR


def test_text_substring_region_partial() -> None:
    assert _compare_text("Apaneca-Ilamatepec", "Apaneca") is bo.Outcome.PAR


def test_enum_synonym_canonicalises() -> None:
    assert _compare_enum("white honey", "honey") is bo.Outcome.COR
    assert _compare_enum("washed", "natural") is bo.Outcome.INC


def test_contradiction_guard_demotes_to_inc() -> None:
    assert bo.has_contradiction("washed process", "unwashed process") is True


# --- Metrics -----------------------------------------------------------------


def _run(model_slug: str, page_outcomes: dict[str, dict[str, bo.Outcome]]) -> bo.ModelRun:
    pages = [
        bo.PageResult(slug=slug, outcomes=outcomes, error=None, on_page_fields=0)
        for slug, outcomes in page_outcomes.items()
    ]
    return bo.ModelRun(model_slug=model_slug, pages=pages)


def test_axes_and_combined_score() -> None:
    outcomes = [
        bo.Outcome.COR,
        bo.Outcome.INC,
        bo.Outcome.MIS,
        bo.Outcome.ABS_COR,
        bo.Outcome.SPU,
        bo.Outcome.PAR,
    ]
    counts = bo.tally(outcomes)
    # recall = (1 + 0.5) / (COR+INC+PAR+MIS = 4) = 0.375
    assert bo.recall(counts) == pytest.approx(0.375)
    # precision = 1.5 / (COR+INC+PAR+SPU = 4) = 0.375
    assert bo.precision(counts) == pytest.approx(0.375)
    # abstention = 1 / (ABS_COR+SPU = 2) = 0.5
    assert bo.abstention_correctness(counts) == pytest.approx(0.5)
    # combined = (1 -0.5 +0 +0.5 -1 +0.5) / 6
    assert bo.combined_score(outcomes) == pytest.approx((1 - 0.5 + 0 + 0.5 - 1 + 0.5) / 6)


def test_err_excluded_from_combined_and_counts() -> None:
    assert bo.combined_score([bo.Outcome.COR, bo.Outcome.ERR]) == pytest.approx(1.0)
    assert bo.combined_score([bo.Outcome.ERR]) is None


# --- Statistics (section 5.2) ------------------------------------------------


def test_wilson_interval_known() -> None:
    interval = bo.wilson_interval(8, 10)
    assert interval.proportion == pytest.approx(0.8)
    assert interval.low == pytest.approx(0.4902, abs=1e-3)
    assert interval.high == pytest.approx(0.9432, abs=1e-3)
    assert bo.wilson_interval(0, 0).high == 1.0


def test_mcnemar_exact_known() -> None:
    fields_a = {f"f{i}": bo.Outcome.COR for i in range(5)}
    fields_b = {f"f{i}": bo.Outcome.INC for i in range(5)}
    run_a = _run("a", {"p": fields_a})
    run_b = _run("b", {"p": fields_b})
    result = bo.mcnemar_exact(run_a, run_b)
    assert (result.a_only, result.b_only, result.discordant) == (5, 0, 5)
    # exact two-sided p = 2 * C(5,0) * 0.5**5 = 0.0625
    assert result.exact_p_two_sided == pytest.approx(0.0625)


def test_paired_bootstrap_combined_is_deterministic_and_centred() -> None:
    # Model A strictly beats B on every page (all COR vs all INC).
    run_a = _run("a", {f"p{i}": {"x": bo.Outcome.COR} for i in range(6)})
    run_b = _run("b", {f"p{i}": {"x": bo.Outcome.INC} for i in range(6)})
    first = bo.paired_bootstrap_combined(run_a, run_b, resamples=500, seed=7)
    second = bo.paired_bootstrap_combined(run_a, run_b, resamples=500, seed=7)
    assert (first.estimate, first.low, first.high) == (second.estimate, second.low, second.high)
    # every page gap is (+1 - -0.5) = +1.5, so estimate and the whole CI sit there.
    assert first.estimate == pytest.approx(1.5)
    assert first.low == pytest.approx(1.5)
    assert first.high == pytest.approx(1.5)


def test_paired_bootstrap_metric_recall_gap() -> None:
    run_a = _run("a", {f"p{i}": {"x": bo.Outcome.COR} for i in range(4)})
    run_b = _run("b", {f"p{i}": {"x": bo.Outcome.MIS} for i in range(4)})
    ci = bo.paired_bootstrap_metric(run_a, run_b, bo.recall, resamples=300, seed=1)
    assert ci.estimate == pytest.approx(1.0)  # recall 1.0 vs 0.0


# --- Cost estimate + .env loader + CLI ---------------------------------------


def test_estimate_cost_positive_and_price_ordered(corpus: list[bo.CorpusPage]) -> None:
    estimates = bo.estimate_cost(corpus, bo.MODEL_ROSTER)
    assert len(estimates) == len(bo.MODEL_ROSTER)
    assert all(e.usd > 0 for e in estimates)
    by_slug = {e.slug: e.usd for e in estimates}
    # gpt-5-nano (0.05/0.40) must be cheaper than gpt-4o (2.50/10.00) on the same corpus.
    assert by_slug["openai/gpt-5-nano"] < by_slug["openai/gpt-4o"]


def test_load_dotenv_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="sk-or-secret"\nOTHER=1\n')
    assert bo.load_dotenv_key(tmp_path) == "sk-or-secret"
    assert bo.load_dotenv_key(tmp_path / "nope") is None


@pytest.mark.asyncio
async def test_main_refuses_without_max_spend() -> None:
    assert await bo.main(["--out", "/tmp/rp588-should-not-write.json"]) == 2


@pytest.mark.asyncio
async def test_main_estimate_only_is_zero_spend(capsys: pytest.CaptureFixture[str]) -> None:
    assert await bo.main(["--estimate-only"]) == 0
    out = capsys.readouterr().out
    assert "roster total" in out


# --- Report + serialization + checkpoint/budget (all network-free) ----------


def _full_run(slug: str, outcome: bo.Outcome) -> bo.ModelRun:
    fields = {spec.name: outcome for spec in bo.FIELD_SPECS}
    return _run(slug, {"page-a": dict(fields), "page-b": dict(fields)})


def test_render_report_has_headline_pairwise_cost_and_caveat(
    corpus: list[bo.CorpusPage],
) -> None:
    runs = [_full_run("model-a", bo.Outcome.COR), _full_run("model-b", bo.Outcome.INC)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:2]))
    assert "bean-sourcing extraction bake-off" in report
    assert "macro F1" in report
    assert "Pairwise significance" in report
    assert "Estimated paid-run cost" in report
    assert "SCREENING harness, not certification" in report


def test_run_json_roundtrips_outcomes() -> None:
    run = _run("m", {"p": {"origin": bo.Outcome.COR, "process": bo.Outcome.SPU}})
    rebuilt = bo._run_from_checkpoint(bo.run_to_json(run))  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.model_slug == "m"
    assert rebuilt.pages[0].outcomes == {
        "origin": bo.Outcome.COR,
        "process": bo.Outcome.SPU,
    }


def test_checkpoint_appends_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "cells.jsonl"
    first = bo.Checkpoint(path, resume=False)
    first.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    reopened = bo.Checkpoint(path, resume=True)
    assert reopened.has("m1")
    assert reopened.get("m1")["model_slug"] == "m1"
    assert not bo.Checkpoint(path, resume=False).has("m1")  # truncated


@pytest.mark.asyncio
async def test_run_bakeoff_budget_stop_makes_no_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    estimates = bo.estimate_cost(corpus, roster)
    runs = await bo.run_bakeoff(
        corpus,
        ["m1"],
        out=tmp_path / "o.json",
        resume=False,
        max_spend=0.0,  # first model's estimate > 0 -> stop before any (paid) call
        cost_estimates=estimates,
    )
    assert runs == []


@pytest.mark.asyncio
async def test_run_bakeoff_resumes_without_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    out = tmp_path / "o.json"
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    runs = await bo.run_bakeoff(
        corpus,
        ["m1"],
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
    )
    assert [r.model_slug for r in runs] == ["m1"]
