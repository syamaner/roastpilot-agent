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

import asyncio
import dataclasses
import importlib.metadata
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import bakeoff_bean_sourcing as bo  # noqa: E402

from roastpilot_agent.advisor import AdvisorDependencyError  # noqa: E402
from roastpilot_agent.config import AdvisorConfig  # noqa: E402
from roastpilot_agent.models import BeanFieldSource, BeanProfileDraft  # noqa: E402

_ADVISOR_CONFIG = AdvisorConfig()


# --- FunctionModel doubles ---------------------------------------------------


def _model_returning(args: dict[str, Any]) -> FunctionModel:
    """A double whose extraction always emits ``args`` via the output tool."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    return FunctionModel(respond)


def _model_returning_with_usage(
    args: dict[str, Any], *, input_tokens: int, output_tokens: int
) -> FunctionModel:
    """Like :func:`_model_returning`, but with an EXACT, explicit usage count
    (#601 fold round 13) -- deterministic control over the priced cost,
    unlike the default heuristic estimate."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, args)],
            usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )

    return FunctionModel(respond)


def _model_text_only() -> FunctionModel:
    """A double that only ever returns prose — never the output tool, so the
    structured extraction exhausts retries and the page fails to draft.

    A malformed-shape failure -- a SCHEMA failure (#601 F1), never wholly-failed."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("no structured output")])

    return FunctionModel(respond)


def _model_provider_error() -> FunctionModel:
    """A double that always raises a genuine provider error -- an INFRA-class
    failure (#601 F1), unlike :func:`_model_text_only`'s schema failure."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise ModelAPIError("test-model", "simulated provider outage")

    return FunctionModel(respond)


def _model_fails_nth_call(n: int) -> FunctionModel:
    """A double that raises a genuine (INFRA-class) provider error on exactly
    the ``n``th call (1-indexed page-processing order), succeeding on every
    other call -- one transient failure inside an otherwise-clean run (#649)."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == n:
            raise ModelAPIError("test-model", "simulated provider outage")
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"name": "X", "country": "Ecuador"})]
        )

    return FunctionModel(respond)


def _model_fails_calls(ns: set[int]) -> FunctionModel:
    """Like :func:`_model_fails_nth_call`, but for MULTIPLE call numbers
    (1-indexed) -- more than one transient failure inside an otherwise-clean
    run (#652)."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] in ns:
            raise ModelAPIError("test-model", "simulated provider outage")
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"name": "X", "country": "Ecuador"})]
        )

    return FunctionModel(respond)


def _model_retry_then_provider_error() -> FunctionModel:
    """A double whose FIRST attempt is malformed (a real, billed response
    with explicit usage -- a validation retry pydantic-ai recovers from),
    then raises a genuine provider error on the retry itself (#601 fold
    round 10, D amendment): captured usage IS present before an INFRA
    failure, unlike :func:`_model_provider_error`'s zero-usage case."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(
                parts=[TextPart("no structured output")],
                usage=RequestUsage(input_tokens=50, output_tokens=10),
            )
        raise ModelAPIError("test-model", "simulated provider outage")

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


def test_load_corpus_wires_the_gold_product_name(corpus: list[bo.CorpusPage]) -> None:
    """The gold JSON's top-level ``name`` must be scored, not silently
    dropped (#600 finding)."""
    page = _page(corpus, "cbc-costa-rica")
    assert page.gold_fields["name"]["value"] == "Costa Rica: La Minita Estate, Tarrazu"


def test_load_corpus_preserves_crlf_fixture_bytes(corpus: list[bo.CorpusPage]) -> None:
    """The committed fixtures are byte-exact (``.gitattributes -text``); a
    universal-newline read would silently strip their CRLF line endings
    before the mock transport ever serves them (#600 finding)."""
    page = _page(corpus, "cbc-costa-rica")
    assert "\r\n" in page.html


def test_load_corpus_rejects_missing_scored_field(tmp_path: Path) -> None:
    """A malformed/incomplete custom ``--fixtures-dir`` gold record must fail
    at LOAD time (before any provider is built / paid call made), not after
    every model has already been run (#600 finding)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    # "origin" is entirely absent from "fields" -- not even {"absent": true}.
    incomplete_fields = {
        f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name not in ("origin", "name")
    }
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": incomplete_fields,
            }
        )
    )
    with pytest.raises(ValueError, match="origin"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_ambiguous_gold_shape(tmp_path: Path) -> None:
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                # "origin" ambiguously has BOTH a value and absent=True.
                "fields": {**all_absent, "origin": {"value": "Ecuador", "absent": True}},
            }
        )
    )
    with pytest.raises(ValueError, match="origin"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_malformed_accept_any_of(tmp_path: Path) -> None:
    """A malformed ``accept_any_of`` tolerance list (a non-string entry) must
    fail at LOAD time -- before any paid call -- naming the fixture, not
    crash mid-scoring inside ``_tolerates_absent_value`` after money has
    already been spent (#602 fold 3)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": {**all_absent, "origin": {"absent": True, "accept_any_of": ["ok", 5]}},
            }
        )
    )
    with pytest.raises(ValueError, match="bad.gold.json.*accept_any_of"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_a_zero_token_accept_any_of_entry(tmp_path: Path) -> None:
    """A stopword-only ``accept_any_of`` entry ("the") strips to zero tokens
    at scoring time, so it can never match -- must fail at LOAD time, not
    silently pass a paid experiment that then never fires (#602 fold round
    8)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": {**all_absent, "origin": {"absent": True, "accept_any_of": ["the"]}},
            }
        )
    )
    with pytest.raises(ValueError, match="bad.gold.json.*accept_any_of"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_accept_any_of_on_a_gold_present_field(tmp_path: Path) -> None:
    """``accept_any_of`` is an ABSENT-only construct -- a field carrying
    BOTH a ``value`` and ``accept_any_of`` must fail at LOAD time, naming
    the field and the reason (#602 fold round 4, FOLD 3)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": {
                    **all_absent,
                    "origin": {"value": "Ecuador", "accept_any_of": ["a blend"]},
                },
            }
        )
    )
    with pytest.raises(ValueError, match="origin.*accept_any_of"):
        bo.load_corpus(tmp_path)


def test_accept_any_of_eligible_fields_excludes_altitude_and_tasting() -> None:
    """``altitude``/``tasting`` kinds have their OWN bespoke absent-handling
    (``_classify_altitude``/``_classify_tasting``) that never consults
    ``accept_any_of`` -- they must be excluded from the eligible set (#602
    fold round 5, FOLD 4)."""
    eligible = bo._ACCEPT_ANY_OF_ELIGIBLE_FIELDS  # pyright: ignore[reportPrivateUsage]
    assert "altitude" not in eligible
    assert "tasting_notes" not in eligible
    assert "origin" in eligible  # a "text"-kind field IS eligible


def test_accept_any_of_eligible_fields_excludes_bool_kind() -> None:
    """``is_blend`` (kind ``bool``) reaches the GENERIC ``classify_field``
    absent branch, but its extracted value is a ``bool``/``None``, never a
    ``str`` -- ``_classify_absent_field``'s ``isinstance(model_value, str)``
    gate means ``accept_any_of`` is NEVER consulted for it. Round 5's
    exclude-altitude-and-tasting derivation let it slip in; the POSITIVE,
    string-producing-kinds derivation must exclude it (#602 fold round 6,
    FOLD 1)."""
    eligible = bo._ACCEPT_ANY_OF_ELIGIBLE_FIELDS  # pyright: ignore[reportPrivateUsage]
    assert "is_blend" not in eligible


def test_load_corpus_rejects_accept_any_of_on_a_field_its_classifier_ignores(
    tmp_path: Path,
) -> None:
    """``tasting_notes`` (kind ``tasting``) has its OWN bespoke absent-handling
    that never consults ``accept_any_of`` -- setting it there would be
    silently ignored, so it must be rejected at LOAD time instead, naming
    the field and the reason (#602 fold round 5, FOLD 4)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": {
                    **all_absent,
                    "tasting_notes": {"absent": True, "accept_any_of": ["sweet"]},
                },
            }
        )
    )
    with pytest.raises(ValueError, match="tasting_notes.*accept_any_of"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_accept_any_of_on_is_blend(tmp_path: Path) -> None:
    """``is_blend`` (kind ``bool``) extracts a ``bool``/``None``, never a ``str`` --
    ``accept_any_of`` can never be consulted for it, so it must be rejected at
    LOAD time too, naming the field and the reason (#602 fold round 6, FOLD 1)."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_absent = {f.name: {"absent": True} for f in bo.FIELD_SPECS if f.name != "name"}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": {
                    **all_absent,
                    "is_blend": {"absent": True, "accept_any_of": ["yes"]},
                },
            }
        )
    )
    with pytest.raises(ValueError, match="is_blend.*accept_any_of"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_a_gold_json_with_no_top_level_name(tmp_path: Path) -> None:
    """A custom gold record missing the top-level ``name`` key entirely (the
    ``if name_field is not None`` branch's False path) must fail the same
    missing-field validation, not silently score an absent name."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_present: dict[str, dict[str, Any]] = {
        f.name: {"value": "x"} for f in bo.FIELD_SPECS if f.name not in ("name", "is_blend")
    }
    all_present["is_blend"] = {"value": False}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                # no top-level "name" key at all.
                "fields": all_present,
            }
        )
    )
    with pytest.raises(ValueError, match="name"):
        bo.load_corpus(tmp_path)


# --- Gold value TYPE validation (#600 round-2 finding) -------------------------
#
# _validate_gold_shape only checked that a "value" KEY existed; a custom
# fixtures-dir gold record like {"value": null} or an altitude range missing
# "min_m" passed that check and then crashed mid-paid-run in canon/numeric
# conversion/range indexing. _validate_gold_value_type extends the load-time
# check to the value's actual TYPE, so this fails before any provider spend.

_text_spec = next(s for s in bo.FIELD_SPECS if s.kind == "text")
_variety_spec = next(s for s in bo.FIELD_SPECS if s.kind == "variety")
_bool_spec = next(s for s in bo.FIELD_SPECS if s.kind == "bool")
_altitude_spec = next(s for s in bo.FIELD_SPECS if s.kind == "altitude")


def test_validate_gold_value_type_rejects_null_text() -> None:
    with pytest.raises(ValueError, match="non-empty string"):
        bo._validate_gold_value_type("slug", _text_spec, None)  # pyright: ignore[reportPrivateUsage]


def test_validate_gold_value_type_accepts_valid_text() -> None:
    bo._validate_gold_value_type("slug", _text_spec, "Ecuador")  # pyright: ignore[reportPrivateUsage]


def test_validate_gold_value_type_variety_accepts_scalar_and_list() -> None:
    bo._validate_gold_value_type("slug", _variety_spec, "Caturra")  # pyright: ignore[reportPrivateUsage]
    bo._validate_gold_value_type(  # pyright: ignore[reportPrivateUsage]
        "slug", _variety_spec, ["Caturra", "Typica"]
    )


def test_validate_gold_value_type_variety_rejects_empty_list() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        bo._validate_gold_value_type("slug", _variety_spec, [])  # pyright: ignore[reportPrivateUsage]


def test_validate_gold_value_type_variety_rejects_non_string_elements() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        bo._validate_gold_value_type(  # pyright: ignore[reportPrivateUsage]
            "slug", _variety_spec, ["Caturra", 3]
        )


def test_validate_gold_value_type_bool_rejects_non_bool() -> None:
    with pytest.raises(ValueError, match="must be a bool"):
        bo._validate_gold_value_type("slug", _bool_spec, "true")  # pyright: ignore[reportPrivateUsage]


def test_validate_gold_value_type_altitude_range_missing_key() -> None:
    with pytest.raises(ValueError, match="missing"):
        bo._validate_gold_value_type(  # pyright: ignore[reportPrivateUsage]
            "slug", _altitude_spec, {"min_m": 1000}
        )


def test_validate_gold_value_type_altitude_range_non_numeric_bound() -> None:
    with pytest.raises(ValueError, match="numeric"):
        bo._validate_gold_value_type(  # pyright: ignore[reportPrivateUsage]
            "slug", _altitude_spec, {"min_m": "low", "max_m": 2000}
        )


def test_validate_gold_value_type_altitude_scalar_rejects_string() -> None:
    with pytest.raises(ValueError, match="number"):
        bo._validate_gold_value_type("slug", _altitude_spec, "1400")  # pyright: ignore[reportPrivateUsage]


def test_validate_gold_value_type_altitude_accepts_scalar_and_range() -> None:
    bo._validate_gold_value_type("slug", _altitude_spec, 1400)  # pyright: ignore[reportPrivateUsage]
    bo._validate_gold_value_type(  # pyright: ignore[reportPrivateUsage]
        "slug", _altitude_spec, {"min_m": 1000, "max_m": 2000}
    )


# --- accept_any_of validation (#602 fold 3) -----------------------------------


def test_validate_accept_any_of_accepts_a_nonempty_string_list() -> None:
    bo._validate_accept_any_of(  # pyright: ignore[reportPrivateUsage]
        "slug", _text_spec, ["blend of multiple origins", "multiple origins"]
    )


def test_validate_accept_any_of_rejects_a_non_list() -> None:
    with pytest.raises(ValueError, match="accept_any_of"):
        bo._validate_accept_any_of("slug", _text_spec, "blend of multiple origins")  # pyright: ignore[reportPrivateUsage]


def test_validate_accept_any_of_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="accept_any_of"):
        bo._validate_accept_any_of("slug", _text_spec, [])  # pyright: ignore[reportPrivateUsage]


def test_validate_accept_any_of_rejects_a_non_string_element() -> None:
    with pytest.raises(ValueError, match="accept_any_of"):
        bo._validate_accept_any_of("slug", _text_spec, ["ok", 5])  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize("zero_token_entry", ["the", "---"])
def test_validate_accept_any_of_rejects_a_zero_token_entry(zero_token_entry: str) -> None:
    """A stopword-only or punctuation-only entry strips to zero tokens under
    ``words()`` normalisation, so it has zero recall against every
    response and can never match at scoring time -- an advertised
    tolerance that silently never fires, only discoverable after a paid
    run (#602 fold round 8)."""
    with pytest.raises(ValueError, match="accept_any_of"):
        bo._validate_accept_any_of("slug", _text_spec, [zero_token_entry])  # pyright: ignore[reportPrivateUsage]


def test_validate_accept_any_of_accepts_an_entry_with_one_substantive_token() -> None:
    """An entry need not be multi-word -- one substantive (non-stopword)
    token is enough to be matchable (#602 fold round 8)."""
    bo._validate_accept_any_of("slug", _text_spec, ["blend"])  # pyright: ignore[reportPrivateUsage]


def test_load_corpus_rejects_null_value_before_paid_calls(tmp_path: Path) -> None:
    """The exact ``name: {"value": null}`` example from the finding."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_present: dict[str, dict[str, Any]] = {
        f.name: {"value": "x"} for f in bo.FIELD_SPECS if f.name not in ("name", "is_blend")
    }
    all_present["is_blend"] = {"value": False}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": None},
                "fields": all_present,
            }
        )
    )
    with pytest.raises(ValueError, match="non-empty string"):
        bo.load_corpus(tmp_path)


def test_load_corpus_rejects_altitude_range_missing_min_m(tmp_path: Path) -> None:
    """The exact 'altitude range missing min_m' example from the finding."""
    (tmp_path / "bad.html").write_text("<html>hi</html>")
    all_present: dict[str, dict[str, Any]] = {
        f.name: {"value": "x"}
        for f in bo.FIELD_SPECS
        if f.name not in ("altitude", "is_blend", "name")
    }
    all_present["is_blend"] = {"value": False}
    all_present["altitude"] = {"value": {"max_m": 2000}}
    (tmp_path / "bad.gold.json").write_text(
        json.dumps(
            {
                "provenance": {"url": "https://example.com/bad", "vendor": "x"},
                "name": {"value": "X"},
                "fields": all_present,
            }
        )
    )
    with pytest.raises(ValueError, match="min_m"):
        bo.load_corpus(tmp_path)


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
    matched = (
        "name",
        "origin",
        "region",
        "farm",
        "variety",
        "process",
        "tasting_notes",
        "is_blend",
    )
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


# --- Origin-absent-blend gold nuance (#602) -----------------------------------


def test_classify_absent_field_tolerates_an_accepted_phrase() -> None:
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    assert (
        bo._classify_absent_field(gold, "a blend of multiple origins")  # pyright: ignore[reportPrivateUsage]
        is bo.Outcome.ABS_COR
    )


def test_classify_absent_field_still_penalises_a_real_invented_value() -> None:
    """``accept_any_of`` must not become a blanket amnesty -- a model naming
    an actual (wrong) single country on the same field still scores SPU."""
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    assert bo._classify_absent_field(gold, "Ethiopia") is bo.Outcome.SPU  # pyright: ignore[reportPrivateUsage]


def test_classify_absent_field_rejects_a_tolerated_phrase_padded_with_a_country() -> None:
    """Recall alone would let 'a blend of multiple origins, primarily
    Ethiopia' match the tolerated phrase on recall (every phrase word is
    present) while smuggling in an invented country -- the precision gate
    must catch the padding and still score SPU (#602 fold 2)."""
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    padded = "a blend of multiple origins, primarily Ethiopia"
    assert bo._classify_absent_field(gold, padded) is bo.Outcome.SPU  # pyright: ignore[reportPrivateUsage]


def test_classify_absent_field_rejects_exact_boundary_precision_padding() -> None:
    """A single padded content token lands EXACTLY on the old 0.75 fuzzy
    threshold (3 of 4 content tokens = precision 0.75) -- the fuzzy bar fit
    for free-text comparison wrongly admitted this and scored an invented
    country ABS_COR; a whitelist tolerance must reject ANY unsupported
    token, so only an EXACT (1.0) precision may pass (#602 fold 2, round
    2)."""
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    boundary = "blend of multiple origins Ethiopia"
    assert bo._classify_absent_field(gold, boundary) is bo.Outcome.SPU  # pyright: ignore[reportPrivateUsage]


def test_classify_absent_field_clean_tolerated_phrase_still_scores_abs_cor() -> None:
    """A clean (unpadded) tolerated phrase must still pass the joint
    recall+precision gate -- the precision requirement must not become so
    strict it defeats the tolerance itself (#602 fold 2)."""
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    assert (
        bo._classify_absent_field(gold, "a blend of multiple origins")  # pyright: ignore[reportPrivateUsage]
        is bo.Outcome.ABS_COR
    )


def test_classify_absent_field_without_accept_any_of_is_unchanged() -> None:
    """No ``accept_any_of`` key is a no-op -- identical pre-#602 behaviour."""
    gold = {"absent": True}
    assert bo._classify_absent_field(gold, None) is bo.Outcome.ABS_COR  # pyright: ignore[reportPrivateUsage]
    assert bo._classify_absent_field(gold, "anything") is bo.Outcome.SPU  # pyright: ignore[reportPrivateUsage]


def test_classify_absent_field_non_string_value_scores_spu() -> None:
    """A non-empty, non-string value (e.g. a stray ``bool`` reaching the
    generic absent-field path for the ``is_blend`` kind) skips the
    ``accept_any_of`` word-bag check entirely -- SPU, same as before."""
    gold = {"absent": True, "accept_any_of": ["blend of multiple origins"]}
    assert bo._classify_absent_field(gold, True) is bo.Outcome.SPU  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_origin_absent_blend_tolerates_the_gold_accept_any_of_phrase(
    corpus: list[bo.CorpusPage],
) -> None:
    """The real ``klatch-blue-thunder-blend`` fixture: a model that faithfully
    answers 'a blend of multiple origins' must not be penalised as SPU, but a
    model that invents an actual single country still is (#602)."""
    page = _page(corpus, "klatch-blue-thunder-blend")
    tolerant_model = _model_returning(
        {"name": "Blue Thunder Blend", "country": "a blend of multiple origins"}
    )
    outcomes = await _score(page, tolerant_model)
    assert outcomes["origin"] is bo.Outcome.ABS_COR

    inventive_model = _model_returning({"name": "Blue Thunder Blend", "country": "Ethiopia"})
    outcomes = await _score(page, inventive_model)
    assert outcomes["origin"] is bo.Outcome.SPU


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
_compare_variety = bo._compare_variety  # pyright: ignore[reportPrivateUsage]


def test_text_multi_origin_partial() -> None:
    assert _compare_text("Colombia, Ethiopia", "Colombia") is bo.Outcome.PAR


def test_text_substring_region_partial() -> None:
    assert _compare_text("Apaneca-Ilamatepec", "Apaneca") is bo.Outcome.PAR


def test_enum_synonym_canonicalises() -> None:
    assert _compare_enum("white honey", "honey") is bo.Outcome.COR
    assert _compare_enum("washed", "natural") is bo.Outcome.INC


def test_contradiction_guard_demotes_to_inc() -> None:
    assert bo.has_contradiction("washed process", "unwashed process") is True


# --- Hallucinated-addition penalty (#600 finding) -----------------------------


def test_text_hallucinated_addition_is_not_cor() -> None:
    """Gold tokens fully present PLUS an unsupported extra origin must not
    score full credit -- recall alone (1.0 here) used to award COR."""
    assert _compare_text("Costa Rica", "Costa Rica, Ethiopia") is bo.Outcome.PAR


def test_text_exact_match_still_cor() -> None:
    """The precision gate must not demote a genuinely exact answer."""
    assert _compare_text("Costa Rica", "Costa Rica") is bo.Outcome.COR


def test_variety_hallucinated_addition_is_not_cor() -> None:
    assert _compare_variety(["Caturra"], "Caturra, Geisha") is bo.Outcome.PAR


def test_variety_exact_match_still_cor() -> None:
    assert _compare_variety(["Caturra", "Typica"], "Caturra and Typica") is bo.Outcome.COR


def test_word_bag_precision_empty_model_text_is_full_credit() -> None:
    """Nothing to penalise when the model returned no content words at all."""
    assert bo.word_bag_precision(["Costa Rica"], "") == 1.0


# --- Tasting-notes vs. process/lot prose (#600 finding) ------------------------


def _draft_with_description(description: str | None) -> BeanProfileDraft:
    return BeanProfileDraft(
        name="X",
        bean_origin="Y",
        initial_heat_percent=100,
        initial_fan_percent=30,
        target_drop_temp_c=194.0,
        target_development_percent=14.0,
        default_bean_weight_grams=250.0,
        scouting_note="scouting",
        description=description,
    )


def _tasting_spec() -> bo.FieldSpec:
    return next(s for s in bo.FIELD_SPECS if s.name == "tasting_notes")


def test_process_only_description_on_absent_tasting_is_abs_cor() -> None:
    """A faithful process/lot-only description on a page with NO cupping
    prose must score as a correct abstention on TASTE, not a hallucinated
    tasting-notes claim -- the production ``description`` field legitimately
    covers process/lot detail as well as flavour (#600 finding)."""
    gold: dict[str, Any] = {"absent": True}
    draft = _draft_with_description("Honey-processed lot from Ramirez farm at 1400 masl.")
    assert bo.classify_field(_tasting_spec(), gold, draft) is bo.Outcome.ABS_COR


def test_flavour_description_on_absent_tasting_is_spu() -> None:
    """A description that DOES assert flavour content on a gold-absent page
    is still a genuine confabulation."""
    gold: dict[str, Any] = {"absent": True}
    draft = _draft_with_description("Notes of dark chocolate and bright citrus.")
    assert bo.classify_field(_tasting_spec(), gold, draft) is bo.Outcome.SPU


def test_process_only_description_on_present_tasting_is_mis() -> None:
    """A process-only description does not attempt the tasting-notes field
    at all when gold expects one -- a recall miss, not a wrong-value match."""
    gold = {"value": ["orange citrus", "chocolate"]}
    draft = _draft_with_description("Washed process, harvested at 1400 masl.")
    assert bo.classify_field(_tasting_spec(), gold, draft) is bo.Outcome.MIS


def test_matching_flavour_description_on_present_tasting_is_cor() -> None:
    gold = {"value": ["orange citrus", "chocolate"]}
    draft = _draft_with_description("orange citrus, chocolate, medium body")
    assert bo.classify_field(_tasting_spec(), gold, draft) is bo.Outcome.COR


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


def test_macro_f1_counts_never_attempted_field_as_zero() -> None:
    """A field the model NEVER attempts (always abstains, even when gold is
    present) must count as F1 0.0 in the macro average, not be excluded --
    excluding it lets a model improve its headline by dodging hard fields
    (#600 finding)."""
    always_mis_field = "origin"
    field_names = [s.name for s in bo.FIELD_SPECS]
    outcomes = {name: bo.Outcome.COR for name in field_names}
    outcomes[always_mis_field] = bo.Outcome.MIS
    run = _run("m", {"p1": dict(outcomes), "p2": dict(outcomes)})
    n = len(field_names)
    # every field except "origin" is perfect COR (F1 1.0); "origin" is always
    # MIS -- recall is defined (0.0) but precision is undefined (no COR/PAR/
    # INC/SPU ever), so F1 is None and must count as 0.0, not be dropped.
    expected = (n - 1) / n
    assert bo.macro_f1(run) == pytest.approx(expected)


def test_macro_f1_excludes_a_field_never_gold_present() -> None:
    """A field the CORPUS never had gold-PRESENT for (always a correct
    abstention, ABS-COR) is genuinely not applicable and stays EXCLUDED --
    distinct from the always-MIS case above, which IS scored 0.0."""
    always_abs_cor_field = "species"
    field_names = [s.name for s in bo.FIELD_SPECS]
    outcomes = {name: bo.Outcome.COR for name in field_names}
    outcomes[always_abs_cor_field] = bo.Outcome.ABS_COR
    run = _run("m", {"p1": dict(outcomes), "p2": dict(outcomes)})
    # every OTHER field is perfect COR (F1 1.0); "species" has no gold-present
    # cell anywhere (recall undefined) so it is excluded, not scored 0 -- the
    # macro average over the remaining fields is still a perfect 1.0.
    assert bo.macro_f1(run) == pytest.approx(1.0)


# --- Schema failures vs other errors (#601 fold round 1, P2) -----------------
#
# model_metrics() used to count EVERY page.error identically in page_errors,
# so a provider outage or a reasoning-induced timeout could masquerade as bad
# schema adherence. schema_failures narrows to the one BeanExtractionUnavailableError
# cause that IS a genuine malformed-structured-output failure; everything else
# (timeout, provider/transport error, model-construction failure, fetch failure)
# is other_errors instead.


def test_is_schema_failure_matches_only_the_malformed_shape_message() -> None:
    assert bo._is_schema_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction returned a malformed "
        "shape: some pydantic-ai detail"
    )
    assert not bo._is_schema_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction exceeded the 45s deadline"
    )
    assert not bo._is_schema_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction provider error: boom"
    )
    assert not bo._is_schema_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanFetchError: vendor page fetch failed: boom"
    )
    assert not bo._is_schema_failure(None)  # pyright: ignore[reportPrivateUsage]


def test_is_provider_error_failure_matches_only_the_provider_error_message() -> None:
    """#601 fold round 11, D fold 4: the reserve-by-class predicate must match
    the runtime's provider-error branch text and nothing else -- distinct from
    a timeout, a schema failure, a model-construction failure, or a fetch
    failure."""
    assert bo._is_provider_error_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction provider error: boom"
    )
    assert not bo._is_provider_error_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction exceeded the 45s deadline"
    )
    assert not bo._is_provider_error_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction returned a malformed "
        "shape: some pydantic-ai detail"
    )
    assert not bo._is_provider_error_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanExtractionUnavailableError: bean identity extraction could not build its model: boom"
    )
    assert not bo._is_provider_error_failure(  # pyright: ignore[reportPrivateUsage]
        "BeanFetchError: vendor page fetch failed: boom"
    )
    assert not bo._is_provider_error_failure(None)  # pyright: ignore[reportPrivateUsage]


def test_model_metrics_separates_schema_failures_from_other_errors() -> None:
    fields = {spec.name: bo.Outcome.ERR for spec in bo.FIELD_SPECS}
    pages = [
        bo.PageResult(
            slug="p1",
            outcomes=dict(fields),
            error="BeanExtractionUnavailableError: bean identity extraction returned a "
            "malformed shape: x",
            on_page_fields=0,
        ),
        bo.PageResult(
            slug="p2",
            outcomes=dict(fields),
            error="BeanExtractionUnavailableError: bean identity extraction exceeded the "
            "45s deadline",
            on_page_fields=0,
        ),
        bo.PageResult(
            slug="p3",
            outcomes=dict(fields),
            error="BeanExtractionUnavailableError: bean identity extraction provider error: boom",
            on_page_fields=0,
        ),
        bo.PageResult(slug="p4", outcomes=dict(fields), error=None, on_page_fields=len(fields)),
    ]
    run = bo.ModelRun(model_slug="m", pages=pages)
    m = bo.model_metrics(run)
    assert m.schema_failures == 1  # only the malformed-shape page
    assert m.other_errors == 2  # timeout + provider error
    assert m.page_errors == 3  # schema_failures + other_errors, preserved for compat


# --- Latency capture (#600 round-2 finding) -------------------------------
#
# The evaluation plan tie-breaks a statistical tie on cost PLUS latency, but
# the harness didn't measure it: the 45s timeout can only identify a
# censored failure, not distinguish a fast model from a slow one.


def test_page_latencies_and_median_p95() -> None:
    pages = [
        bo.PageResult(slug="a", outcomes={}, error=None, on_page_fields=0, elapsed_s=1.0),
        bo.PageResult(slug="b", outcomes={}, error=None, on_page_fields=0, elapsed_s=3.0),
        bo.PageResult(slug="c", outcomes={}, error=None, on_page_fields=0, elapsed_s=None),
    ]
    run = bo.ModelRun(model_slug="m", pages=pages)
    assert bo.page_latencies(run) == [1.0, 3.0]
    latency = bo.latency_median_p95(run)
    assert latency is not None
    median, p95 = latency
    assert median == pytest.approx(2.0)
    assert p95 == pytest.approx(2.9)


def test_latency_median_p95_none_when_unmeasured() -> None:
    run = bo.ModelRun(
        model_slug="m",
        pages=[bo.PageResult(slug="a", outcomes={}, error=None, on_page_fields=0)],
    )
    assert bo.latency_median_p95(run) is None


@pytest.mark.asyncio
async def test_run_model_over_corpus_captures_elapsed_time(corpus: list[bo.CorpusPage]) -> None:
    model = _model_returning({"name": "X", "country": "Ecuador"})
    run = await bo.run_model_over_corpus(
        [corpus[0]], model_slug="m", advisor_config=_ADVISOR_CONFIG, model=model
    )
    assert run.pages[0].elapsed_s is not None
    assert run.pages[0].elapsed_s >= 0.0


@pytest.mark.asyncio
async def test_run_model_over_corpus_counts_recovered_violations_on_a_failed_page(
    corpus: list[bo.CorpusPage],
) -> None:
    """A retry-RECOVERED extraction whose identity is later REJECTED downstream (no
    usable name/origin) must still report the REAL retry count -- round 3's "0 on a
    failed page" was the WRONG contract, not the count: extraction-schema adherence
    is independent of a later, separate draft-policy rejection (#601 fold round 7,
    FOLD 2)."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] == 1:
            return ModelResponse(parts=[TextPart("not yet structured")])
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {})])

    run = await bo.run_model_over_corpus(
        [corpus[0]], model_slug="m", advisor_config=_ADVISOR_CONFIG, model=FunctionModel(respond)
    )
    page = run.pages[0]
    assert page.error is not None  # no usable name/origin -> the page DID fail
    assert page.recovered_violations == 1  # the retry still counts


@pytest.mark.asyncio
async def test_run_model_over_corpus_ledgers_every_page(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """When given ``roster_price`` + a :class:`bo.ChargeLedger``, a PENDING then a
    FINAL entry are written per page from the page's OWN captured diagnostics
    (#601 fold round 1/4, slice A + FOLD 1) -- :class:`bo.PageResult` carries
    none of this, the ledger is the token/spend store of record. No breaker
    exists this slice, so the FULL corpus always completes."""
    model = _model_returning({"name": "X", "country": "Ecuador"})
    price = bo.RosterModel("m1", 2.0, 4.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        corpus,
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=model,
        roster_price=price,
        ledger=ledger,
    )
    assert len(run.pages) == len(corpus)
    assert len(ledger.entries) == 2 * len(corpus)  # pending + final per page
    assert {e.slug for e in ledger.entries} == {p.slug for p in corpus}
    assert all(e.arm == "m1" for e in ledger.entries)
    assert ledger.total_usd() > 0.0


@pytest.mark.asyncio
async def test_run_model_over_corpus_assigns_a_fresh_call_id_each_invocation(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 6, FOLD A (P1): re-running the SAME page (simulating a
    resumed arm re-attempting from scratch after a later page's wholesale
    failure) writes entries under a NEW ``call_id`` each time -- both real
    calls' charges count, never collapsed by a bare ``(arm, slug)`` key."""
    model = _model_returning({"name": "X", "country": "Ecuador"})
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    for _ in range(2):
        await bo.run_model_over_corpus(
            [corpus[0]],
            model_slug="m1",
            advisor_config=_ADVISOR_CONFIG,
            model=model,
            roster_price=price,
            ledger=ledger,
        )
    assert len(ledger.entries) == 4  # 2 pending + 2 final, across two DISTINCT calls
    assert len({e.call_id for e in ledger.entries}) == 2  # two genuinely separate attempt-cycles
    finals = [e for e in ledger.entries if not e.is_pending]
    assert len(finals) == 2
    assert ledger.total_usd() == pytest.approx(sum(e.priced_usd for e in finals))


@pytest.mark.asyncio
async def test_draft_for_page_threads_the_enforced_cap_to_the_paid_call(
    corpus: list[bo.CorpusPage], monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601 fold round 4, FOLD 4: ``max_output_tokens`` reaches the real,
    paid ``draft_bean_profile_from_url`` call -- the reserve's worst-case
    promise only holds if the SAME value the reserve assumes is what the
    provider call itself enforces."""
    captured: list[object] = []

    async def _fake_draft(
        url: str,
        *,
        advisor_config: AdvisorConfig,
        sourcing_config: bo.BeanSourcingConfig | None = None,
        http_client: object = None,
        model: object = None,
        reasoning_effort: object = None,
        diagnostics: object = None,
        max_output_tokens: object = None,
        disable_transport_retries: object = None,
    ) -> BeanProfileDraft:
        captured.append(max_output_tokens)
        raise bo.BeanSourcingError("stub -- no real extraction needed for this guard")

    monkeypatch.setattr(bo, "draft_bean_profile_from_url", _fake_draft)
    await bo.draft_for_page(corpus[0], advisor_config=_ADVISOR_CONFIG)
    assert captured == [bo.BAKEOFF_MAX_OUTPUT_TOKENS]  # the default, uniform across arms

    captured.clear()
    await bo.draft_for_page(corpus[0], advisor_config=_ADVISOR_CONFIG, max_output_tokens=99)
    assert captured == [99]  # an explicit override still threads through


def test_reserve_prompt_text_is_the_longer_candidate_never_trafilatura(
    corpus: list[bo.CorpusPage],
) -> None:
    """#601 fold round 4, FOLD 4: the reserve ALWAYS uses the linear-strip pass
    (never the bounded, off-loop trafilatura call removed by this fold) -- it
    must be at least as long as EITHER prompt candidate a real caller could
    see: the harness's own trafilatura-first estimate text
    (:func:`bo._extract_prompt_text`) and the bare linear-strip text alone,
    since trafilatura's boilerplate-removed markdown can be SHORTER and would
    understate the reserve."""
    page = corpus[0]
    reserve_text = bo._reserve_prompt_text(page)  # pyright: ignore[reportPrivateUsage]
    estimate_text = bo._extract_prompt_text(page)  # pyright: ignore[reportPrivateUsage]
    linear_only = bo._bean_sourcing_module._extract_page_text(page.html)  # pyright: ignore[reportPrivateUsage]
    assert len(reserve_text.encode("utf-8")) >= len(estimate_text.encode("utf-8"))
    assert len(reserve_text.encode("utf-8")) >= len(linear_only.encode("utf-8"))


def test_reserve_input_tokens_per_attempt_applies_structural_inflation(
    corpus: list[bo.CorpusPage],
) -> None:
    """#601 fold round 6, FOLD C: the reserve's per-attempt input-token bound
    is inflated by :data:`bo._RESERVE_STRUCTURAL_INFLATION` over EITHER
    prompt candidate's PLAIN byte length -- markdown structural punctuation
    (table pipes/dashes, list markers, frontmatter delimiters) the
    linear-strip pass never emits could otherwise exceed the reserve's
    "longer candidate" claim even though it adds no content."""
    page = corpus[0]
    per_attempt_tokens = bo._reserve_input_tokens_per_attempt(page)  # pyright: ignore[reportPrivateUsage]
    reserve_text = bo._reserve_prompt_text(page)  # pyright: ignore[reportPrivateUsage]
    estimate_text = bo._extract_prompt_text(page)  # pyright: ignore[reportPrivateUsage]
    inflation = bo._RESERVE_STRUCTURAL_INFLATION  # pyright: ignore[reportPrivateUsage]
    assert per_attempt_tokens >= inflation * len(reserve_text.encode("utf-8"))
    assert per_attempt_tokens >= inflation * len(estimate_text.encode("utf-8"))


def _function_model_hanging() -> FunctionModel:
    """A double that never returns -- used to force a REAL outer-timeout
    cancellation (mirrors ``tests/test_bean_sourcing.py``'s helper of the same
    name) rather than a synthetic ``timed_out=True`` construction."""

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        return ModelResponse(parts=[TextPart("too late")])  # pragma: no cover

    return FunctionModel(respond)


@pytest.mark.asyncio
async def test_run_model_over_corpus_ledgers_a_real_timed_out_page_with_reserve_floor(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """End-to-end wiring proof (#601 fold round 1/4, FOLD 4): a REAL timed-out
    extraction call (a hanging model + a short extraction timeout, not a
    synthetic ``PageResult``/``LedgerEntry`` construction) must flow through to a
    FINAL ``LedgerEntry`` with ``timed_out=True``, ``reserve_applied=True``, and
    the reserve-priced USD -- catching any wrong-field wiring the synthetic-
    construction unit tests above cannot. A PENDING entry precedes it
    (#601 fold round 4, FOLD 1) -- both land on disk."""
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_function_model_hanging(),
        sourcing_config=bo.BeanSourcingConfig(extraction_timeout_seconds=0.05),
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].error is not None  # the extraction call DID fail
    assert len(ledger.entries) == 2  # pending, then final (#601 fold round 4, FOLD 1)
    pending, entry = ledger.entries
    assert pending.is_pending is True
    assert entry.is_pending is False
    assert entry.timed_out is True
    assert entry.reserve_applied is True
    assert entry.priced_usd > 0.0  # the reserve floor, never the (unreported) $0
    assert entry.call_id == pending.call_id  # one attempt-cycle (#601 fold round 6, FOLD A)
    # A timed-out FINAL entry sums a SINGLE-attempt reserve onto any captured
    # usage (#601 fold round 6, FOLD D) -- smaller than the pending entry's
    # own FULL, multi-attempt reserve valuation for the same page (at most
    # ONE in-flight request can ever be unreported at final-timeout time).
    assert entry.priced_usd <= pending.priced_usd


@pytest.mark.asyncio
async def test_run_model_over_corpus_ledgers_a_timeout_with_zero_post_call_parsing(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601 fold round 3/4, FOLD 2 + FOLD 1: the reserve is computed/cached
    BEFORE the billable call, and the PENDING ledger entry is written before
    it too, so a REAL timeout's failure handler appends the FINAL entry with
    ZERO post-call parsing -- a kill between the call and the final append
    must never lose a billed charge (the pending entry already covers it).
    Proven by failing the parse if it EVER runs after the provider call
    starts."""
    call_order: list[str] = []
    real_reserve_prompt_text = bo._reserve_prompt_text  # pyright: ignore[reportPrivateUsage]

    def _tracking_reserve_prompt_text(page: bo.CorpusPage) -> str:
        if "provider_call_started" in call_order:
            raise AssertionError("reserve parse ran AFTER the provider call started")
        call_order.append("parse")
        return real_reserve_prompt_text(page)

    monkeypatch.setattr(bo, "_reserve_prompt_text", _tracking_reserve_prompt_text)

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call_order.append("provider_call_started")
        await asyncio.sleep(10)
        return ModelResponse(parts=[TextPart("too late")])  # pragma: no cover

    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=FunctionModel(respond),
        sourcing_config=bo.BeanSourcingConfig(extraction_timeout_seconds=0.05),
        roster_price=price,
        ledger=ledger,
    )
    # TWO parses before the provider call (#601 fold round 6, FOLD D): the
    # full multi-attempt reserve (pending) and the single-attempt reserve
    # (for the final entry's own, smaller timeout addition) are computed
    # separately, both still strictly BEFORE the call starts.
    assert call_order == ["parse", "parse", "provider_call_started"]
    assert len(ledger.entries) == 2  # pending, then final
    pending, entry = ledger.entries
    assert pending.is_pending is True
    assert entry.is_pending is False
    assert entry.timed_out is True


@pytest.mark.asyncio
async def test_run_model_over_corpus_elapsed_s_excludes_reserve_work(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601 fold round 5, D FOLD 1 (P1): ``elapsed_s`` feeds
    ``latency_median_p95()``'s cost+latency tie-break -- the reserve
    computation + pending-entry write must never land inside the timed
    region. A deliberately slow reserve stub must not move ``elapsed_s``."""

    def _slow_reserve(page: bo.CorpusPage, price: bo.RosterModel, **_: object) -> float:
        time.sleep(0.5)
        return 0.01

    monkeypatch.setattr(bo, "_page_cost_reserve", _slow_reserve)
    model = _model_returning({"name": "X", "country": "Ecuador"})
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=model,
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].elapsed_s is not None
    assert run.pages[0].elapsed_s < 0.3  # well under the reserve stub's 0.5s sleep


@pytest.mark.asyncio
async def test_run_model_over_corpus_final_entry_survives_a_scoring_raise(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601 fold round 5, D FOLD 2 (P2): the FINAL ledger entry is appended
    IMMEDIATELY once the paid call returns, before scoring/``PageResult``
    construction -- so a raise there (a real, if rare, failure mode) still
    leaves the ACTUAL charge on the books, not just the pending reserve."""

    def _raising_score_page(
        page: bo.CorpusPage, draft: BeanProfileDraft | None, error: str | None
    ) -> dict[str, bo.Outcome]:
        raise RuntimeError("scoring blew up")

    monkeypatch.setattr(bo, "score_page", _raising_score_page)
    model = _model_returning({"name": "X", "country": "Ecuador"})
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    with pytest.raises(RuntimeError, match="scoring blew up"):
        await bo.run_model_over_corpus(
            [corpus[0]],
            model_slug="m1",
            advisor_config=_ADVISOR_CONFIG,
            model=model,
            roster_price=price,
            ledger=ledger,
        )
    assert len(ledger.entries) == 2  # pending, then the FINAL entry -- despite the raise
    pending, entry = ledger.entries
    assert pending.is_pending is True
    assert entry.is_pending is False
    assert entry.request_tokens > 0  # the ACTUAL charge, not just the pending reserve


@pytest.mark.asyncio
async def test_run_model_over_corpus_zero_usage_provider_error_charges_single_attempt_reserve(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 10, D amendment: with transport retries disabled, an
    accepted-but-lost request now surfaces as a genuine PROVIDER error (never
    a timeout) with ZERO captured usage -- the same unreported-attempt risk a
    timeout already covers. The final entry must be charged at the
    single-attempt reserve, not left at $0."""
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_model_provider_error(),
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].error is not None
    pending, entry = ledger.entries
    assert entry.request_tokens == 0
    assert entry.response_tokens == 0
    assert entry.reserve_applied is True
    assert entry.timed_out is False  # a genuine provider error, never a wall-clock timeout
    single_reserve = bo._single_attempt_reserve(corpus[0], price)  # pyright: ignore[reportPrivateUsage]
    assert entry.priced_usd == pytest.approx(round(single_reserve, 5))
    # The final entry's SINGLE-attempt reserve is smaller than the pending
    # entry's FULL multi-attempt one -- different valuations by design (#601
    # fold round 6, FOLD D), never expected to match.
    assert entry.priced_usd < pending.priced_usd
    # #601 fold round 14: zero captured usage -- the WHOLE priced_usd IS the
    # reserve component.
    assert entry.reserved_usd == pytest.approx(entry.priced_usd)


@pytest.mark.asyncio
async def test_run_model_over_corpus_provider_error_with_captured_usage_still_gets_the_reserve(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 11, D fold 4: a provider-error page that DID capture
    some usage (an earlier retry reported real usage before the final
    attempt raised) is NOT trusted as complete -- the retry that raised is
    itself an in-flight, possibly-billed request whose response may be
    lost. The reserve is charged ON TOP of the captured usage, same as the
    zero-usage provider-error case, because the failure CLASS (provider
    error), not the captured usage, decides the reserve."""
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_model_retry_then_provider_error(),
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].error is not None
    _, entry = ledger.entries
    assert entry.request_tokens > 0
    assert entry.response_tokens > 0
    assert entry.reserve_applied is True  # in-flight failure class -- reserve on top of captured
    single_reserve = bo._single_attempt_reserve(corpus[0], price)  # pyright: ignore[reportPrivateUsage]
    assert entry.priced_usd == pytest.approx(
        round(
            bo._raw_priced_cost(  # pyright: ignore[reportPrivateUsage]
                entry.request_tokens, entry.response_tokens, price
            )
            + single_reserve,
            5,
        )
    )
    # #601 fold round 14: reserved_usd discloses ONLY the added reserve
    # component -- not the whole (usage-inclusive) priced_usd.
    assert entry.reserved_usd == pytest.approx(round(single_reserve, 5))
    assert entry.reserved_usd < entry.priced_usd


@pytest.mark.asyncio
async def test_run_model_over_corpus_schema_failure_never_gets_the_reserve(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 10, D amendment: a SCHEMA (malformed-shape) failure is
    never treated as an unreported-usage risk -- it is a real, complete
    exchange the model simply answered wrong, not a lost request. No reserve
    on top, even with zero captured usage."""
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_model_text_only(),
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].error is not None
    _, entry = ledger.entries
    assert entry.reserve_applied is False
    assert entry.priced_usd == pytest.approx(
        round(
            bo._raw_priced_cost(  # pyright: ignore[reportPrivateUsage]
                entry.request_tokens, entry.response_tokens, price
            ),
            5,
        )
    )
    assert entry.reserved_usd == 0.0  # #601 fold round 14: no reserve, nothing to disclose


@pytest.mark.asyncio
async def test_run_model_over_corpus_schema_failure_page_is_never_retryable(
    corpus: list[bo.CorpusPage],
) -> None:
    """#649: a SCHEMA (malformed-shape) failure is a real outcome, never
    retryable -- distinct from a timeout/provider-error page."""
    run = await bo.run_model_over_corpus(
        [corpus[0]], model_slug="m1", advisor_config=_ADVISOR_CONFIG, model=_model_text_only()
    )
    assert run.pages[0].error is not None
    assert run.pages[0].retryable is False


@pytest.mark.asyncio
async def test_run_model_over_corpus_provider_error_page_is_retryable(
    corpus: list[bo.CorpusPage],
) -> None:
    """#649: a genuine (INFRA-class) provider error IS retryable -- an
    accepted, possibly-lost request worth re-attempting."""
    run = await bo.run_model_over_corpus(
        [corpus[0]], model_slug="m1", advisor_config=_ADVISOR_CONFIG, model=_model_provider_error()
    )
    assert run.pages[0].error is not None
    assert run.pages[0].retryable is True


@pytest.mark.asyncio
async def test_run_model_over_corpus_success_page_is_never_retryable(
    corpus: list[bo.CorpusPage],
) -> None:
    """A successful page is never retryable (nothing to retry)."""
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert run.pages[0].error is None
    assert run.pages[0].retryable is False


@pytest.mark.asyncio
async def test_run_model_over_corpus_model_construction_failure_never_gets_the_reserve(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#601 fold round 11, D fold 4: a model-CONSTRUCTION failure (a
    misconfigured or under-installed provider) never sends a request, so
    there is nothing billable to lose -- no reserve, even though nothing was
    captured. Distinct from a provider error (an ACCEPTED, possibly-lost
    request), which always gets the reserve regardless of captured usage."""

    def fake_build_model(
        config: AdvisorConfig,
        *,
        model_slug: str | None = None,
        disable_transport_retries: bool = False,
    ) -> Any:
        raise AdvisorDependencyError(
            "advisor provider 'anthropic' needs an optional dependency: "
            "pip install 'roastpilot-agent[anthropic]'"
        )

    monkeypatch.setattr(bo._bean_sourcing_module, "build_model", fake_build_model)  # pyright: ignore[reportPrivateUsage]
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    run = await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        roster_price=price,
        ledger=ledger,
    )
    assert run.pages[0].error is not None
    assert "could not build its model" in run.pages[0].error
    _, entry = ledger.entries
    assert entry.request_tokens == 0
    assert entry.response_tokens == 0
    assert entry.reserve_applied is False
    assert entry.priced_usd == 0.0


@pytest.mark.asyncio
async def test_run_model_over_corpus_halts_between_pages_when_meter_trips(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """The runtime :class:`bo.SpendMeter` halts the page loop BETWEEN pages once
    cumulative usage-priced spend reaches ``max_spend`` -- never mid-page -- returning
    a run SHORTER than the corpus (#601 fold round 1, slice B). An absurd per-token
    price guarantees a trip after the first page regardless of the exact
    (heuristic-estimated) token count. The already-written ledger entries persist
    even though the run itself is incomplete -- a PENDING then a FINAL entry per
    attempted page (#601 fold round 4, FOLD 1)."""
    model = _model_returning({"name": "X", "country": "Ecuador"})
    price = bo.RosterModel("m1", 1_000_000, 1_000_000, "x")  # $1/token -- any nonzero trips it
    ledger = bo.ChargeLedger(bo.ledger_path(tmp_path / "o.json"))
    meter = bo.SpendMeter(max_spend=0.01)
    run = await bo.run_model_over_corpus(
        corpus,
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=model,
        roster_price=price,
        ledger=ledger,
        meter=meter,
    )
    assert 0 < len(run.pages) < len(corpus)  # halted early, not empty, not the whole corpus
    assert meter.tripped
    assert len(ledger.entries) == 2 * len(run.pages)  # pending + final per attempted page


def test_run_json_roundtrips_elapsed_s() -> None:
    page = bo.PageResult(
        slug="p", outcomes={"origin": bo.Outcome.COR}, error=None, on_page_fields=1, elapsed_s=4.2
    )
    run = bo.ModelRun(model_slug="m", pages=[page])
    rebuilt = bo._run_from_checkpoint(bo.run_to_json(run))  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.pages[0].elapsed_s == pytest.approx(4.2)


def test_run_json_roundtrips_missing_elapsed_s_as_none() -> None:
    """A pre-round-2 checkpoint record with no ``elapsed_s`` key must still load."""
    record = {
        "model_slug": "m",
        "pages": [
            {
                "slug": "p",
                "error": None,
                "on_page_fields": 0,
                "outcomes": {"origin": "COR"},
                # no "elapsed_s" key at all -- an old checkpoint record.
            }
        ],
    }
    rebuilt = bo._run_from_checkpoint(record)  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.pages[0].elapsed_s is None


def test_run_json_roundtrips_retryable() -> None:
    page = bo.PageResult(slug="p", outcomes={}, error="boom", on_page_fields=0, retryable=True)
    run = bo.ModelRun(model_slug="m", pages=[page])
    rebuilt = bo._run_from_checkpoint(bo.run_to_json(run))  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.pages[0].retryable is True


def test_run_from_checkpoint_legacy_record_defaults_no_retryable_pages() -> None:
    """#649: a pre-#649 checkpoint record has no ``retryable`` key at all --
    every page defaults to ``retryable=False`` (never retried on resume),
    preserving EXACT prior behavior (whole-arm skip) for old records."""
    record: dict[str, Any] = {
        "model_slug": "m",
        "pages": [
            {
                "slug": "p",
                "error": "boom",
                "on_page_fields": 0,
                "outcomes": {},
                # no "retryable" key at all -- a genuinely pre-#649 record.
            }
        ],
    }
    rebuilt = bo._run_from_checkpoint(record)  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.pages[0].retryable is False


# --- Statistics (section 5.2) ------------------------------------------------


def test_wilson_interval_known() -> None:
    interval = bo.wilson_interval(8, 10)
    assert interval.proportion == pytest.approx(0.8)
    assert interval.low == pytest.approx(0.4902, abs=1e-3)
    assert interval.high == pytest.approx(0.9432, abs=1e-3)


def test_wilson_interval_zero_trials_is_undefined_proportion() -> None:
    """Zero trials must leave ``proportion`` undefined (``None``, rendered
    ``n/a``), never a fabricated ``0.0`` -- consistent with every other
    undefined metric in this harness; the degenerate ``[0, 1]`` bounds are
    kept (#602 fold round 4, FOLD 5)."""
    degenerate = bo.wilson_interval(0, 0)
    assert degenerate.proportion is None
    assert degenerate.low == 0.0
    assert degenerate.high == 1.0


def test_binary_cor_counts_excludes_par() -> None:
    """PAR is EXCLUDED from both COR and trials (#602 fold round 5, FOLD 3):
    the research note's COR-vs-not decomposition here is binary -- COR vs
    {INC, MIS} -- so a partial match is neither a success nor a countable
    trial. A prior round's ``+ par`` in the denominator contradicted the
    documented decomposition and diluted the estimate."""
    run = _run(
        "m",
        {
            "p1": {"a": bo.Outcome.COR},
            "p2": {"a": bo.Outcome.PAR},
            "p3": {"a": bo.Outcome.INC},
            "p4": {"a": bo.Outcome.MIS},
            "p5": {"a": bo.Outcome.ABS_COR},
            "p6": {"a": bo.Outcome.SPU},
        },
    )
    cor, trials = bo.binary_cor_counts(run)
    assert cor == 1  # only the COR cell
    assert trials == 3  # COR + INC + MIS -- PAR, ABS_COR, SPU all excluded


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


def test_paired_bootstrap_combined_flattens_cell_weighted_not_page_averaged() -> None:
    """The bootstrap gap must match the leaderboard's flattened, cell-
    weighted CombinedScore -- NOT a mean of equal-weighted per-page averages,
    which diverges when pages have different scorable-cell counts (#600
    finding)."""
    # page "p1" has 1 cell, page "p2" has 3 cells -- unequal counts.
    run_a = _run(
        "a",
        {
            "p1": {"x": bo.Outcome.COR},
            "p2": {"x": bo.Outcome.COR, "y": bo.Outcome.COR, "z": bo.Outcome.COR},
        },
    )
    run_b = _run(
        "b",
        {
            "p1": {"x": bo.Outcome.INC},
            "p2": {"x": bo.Outcome.INC, "y": bo.Outcome.INC, "z": bo.Outcome.INC},
        },
    )
    ci = bo.paired_bootstrap_combined(run_a, run_b, resamples=10, seed=1)
    # every cell is COR (+1.0) vs INC (-0.5) regardless of page, so the
    # flattened gap is exactly +1.5 (same as the equal-weighted case here,
    # since every cell shares the same outcome -- the key point is this must
    # equal combined_score(all A cells) - combined_score(all B cells)).
    all_a = [o for page in run_a.pages for o in page.outcomes.values()]
    all_b = [o for page in run_b.pages for o in page.outcomes.values()]
    expected = bo.combined_score(all_a) - bo.combined_score(all_b)  # type: ignore[operator]
    assert ci.estimate == pytest.approx(expected)


def test_paired_bootstrap_combined_handles_all_err_pages() -> None:
    """When every shared page's cells are all ``ERR``, the flattened
    CombinedScore is undefined for every resample -- must degrade gracefully
    and PRESERVE the undefined state as ``None`` (rendered ``n/a``), never
    fabricate a ``0.0`` gap that reads as "no difference" (#602 finding)."""
    run_a = _run("a", {"p": {"x": bo.Outcome.ERR}})
    run_b = _run("b", {"p": {"x": bo.Outcome.ERR}})
    ci = bo.paired_bootstrap_combined(run_a, run_b, resamples=50, seed=1)
    assert ci.estimate is None
    assert ci.low is None
    assert ci.high is None
    assert ci.resamples == 0


def test_paired_bootstrap_metric_undefined_denominator_is_none_not_zero() -> None:
    """A model with no gold-present cell for a metric (e.g. every field
    abstained on) has an undefined precision -- the gap must stay ``None``,
    not be fabricated as an exact tie (#602 finding)."""
    run_a = _run("a", {"p": {"x": bo.Outcome.ABS_COR}})
    run_b = _run("b", {"p": {"x": bo.Outcome.ABS_COR}})
    ci = bo.paired_bootstrap_metric(run_a, run_b, bo.precision, resamples=50, seed=1)
    assert ci.estimate is None
    assert ci.low is None
    assert ci.high is None


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


# --- Reasoning-effort arms (#601) ---------------------------------------------


def test_expand_arms_default_off_and_light_are_one_arm_per_model() -> None:
    slugs = ["m1", "m2"]
    default_arms = bo.expand_arms(slugs, "default")
    assert [(a.model_slug, a.reasoning, a.label) for a in default_arms] == [
        ("m1", "default", "m1"),
        ("m2", "default", "m2"),
    ]
    off_arms = bo.expand_arms(slugs, "off")
    assert [(a.model_slug, a.reasoning, a.label) for a in off_arms] == [
        ("m1", "off", "m1+reasoning-off"),
        ("m2", "off", "m2+reasoning-off"),
    ]
    light_arms = bo.expand_arms(slugs, "light")
    assert [(a.model_slug, a.reasoning, a.label) for a in light_arms] == [
        ("m1", "light", "m1+reasoning-light"),
        ("m2", "light", "m2+reasoning-light"),
    ]


def test_expand_arms_both_pairs_off_and_light_never_default() -> None:
    """``both`` must expand to the "off" AND "light" arms -- the research's actual
    no-reasoning-vs-light-reasoning question -- NEVER the "default" (provider-default,
    possibly-high-reasoning) arm (#601 fold round 1)."""
    both = bo.expand_arms(["m1", "m2"], "both")
    assert len(both) == 4
    # grouped per model (off then light), not all-off-then-all-light -- a per-model
    # comparison reads naturally in run/report order.
    assert [(a.model_slug, a.reasoning) for a in both] == [
        ("m1", "off"),
        ("m1", "light"),
        ("m2", "off"),
        ("m2", "light"),
    ]
    assert "default" not in {a.reasoning for a in both}
    # every arm's label is distinct (#601 record-key distinctness).
    assert len({a.label for a in both}) == 4


def test_model_roster_haiku_is_optional_not_none() -> None:
    """claude-haiku-4.5 is "optional" (#601 fold round 5, FOLD 1): off-as-no-op is
    still a genuine no-reasoning arm, and Haiku 4.5 supports extended thinking, so
    it is NOT reasoning-incapable ("none")."""
    haiku = next(m for m in bo.MODEL_ROSTER if m.slug == "anthropic/claude-haiku-4.5")
    assert haiku.reasoning == "optional"


def test_model_roster_grok_is_unknown_not_mandatory() -> None:
    """grok-4.3 has no VERIFIED reasoning evidence either way -- "unknown", not
    "mandatory" (#601 fold round 7, FOLD 3): "mandatory" is reserved for a CONFIRMED
    off-rejecting endpoint, never an unverified guess that could burn a paid arm."""
    grok = next(m for m in bo.MODEL_ROSTER if m.slug == "x-ai/grok-4.3")
    assert grok.reasoning == "unknown"


def test_model_roster_nano_and_flash_lite_are_unknown_not_mandatory() -> None:
    """gpt-5-nano's only citation is a default-effort TIMEOUT, not a disable
    attempt, and gemini-3.1-flash-lite's confirmed HTTP-400 evidence belongs to a
    DIFFERENT endpoint (gemini-3.5-flash) -- neither is CONFIRMED off-rejecting, so
    both are "unknown" (#601 fold round 8), leaving gpt-5-mini the roster's only
    "mandatory" entry."""
    by_slug = {m.slug: m for m in bo.MODEL_ROSTER}
    assert by_slug["openai/gpt-5-nano"].reasoning == "unknown"
    assert by_slug["google/gemini-3.1-flash-lite"].reasoning == "unknown"
    assert by_slug["openai/gpt-5-mini"].reasoning == "mandatory"


def test_expand_arms_both_gates_arms_by_four_way_capability(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ "both" on a mixed roster must gate BOTH off and light per capability (#601 FA,
    extended F3): "none"/"unknown" get neither (both skipped -- "unknown" is an
    UNVERIFIED endpoint, so it must not risk a paid arm on a guess); "mandatory" gets
    light only (off skipped -- disabling would 400); "optional" gets both, unchanged."""
    both = bo.expand_arms(
        ["nope", "must", "opt", "unverified"],
        "both",
        capability={
            "nope": "none",
            "must": "mandatory",
            "opt": "optional",
            "unverified": "unknown",
        },
    )
    assert [(a.model_slug, a.reasoning) for a in both] == [
        ("must", "light"),
        ("opt", "off"),
        ("opt", "light"),
    ]
    out = capsys.readouterr().out
    assert "skipping off arm for 'nope': reasoning is 'none'" in out
    assert "skipping light arm for 'nope'" in out
    assert "skipping off arm for 'must': reasoning is 'mandatory'" in out
    assert "skipping off arm for 'unverified': unverified" in out
    assert "skipping light arm for 'unverified': unverified" in out


def test_expand_arms_unknown_capability_emits_only_default_arm() -> None:
    """An "unknown" (unverified) model requesting "default" still gets its one arm --
    only off/light are gated, "default" is always emitted (#601 fold round 7, FOLD 3)."""
    arms = bo.expand_arms(["unverified"], "default", capability={"unverified": "unknown"})
    assert [(a.model_slug, a.reasoning, a.label) for a in arms] == [
        ("unverified", "default", "unverified")
    ]


def test_reasoning_effort_by_arm_maps_default_off_and_light_correctly() -> None:
    """ "default" omits the setting (provider default, NOT no-reasoning), "off" is the
    TRUE explicit no-reasoning request, "light" is the provider's low-effort tier
    (#601 fold round 1)."""
    assert bo._REASONING_EFFORT_BY_ARM == {  # pyright: ignore[reportPrivateUsage]
        "default": None,
        "off": "off",
        "light": "low",
    }


def test_estimate_cost_for_arms_default_arm_matches_estimate_cost_exactly(
    corpus: list[bo.CorpusPage],
) -> None:
    """The ``--reasoning default`` CLI default must reproduce :func:`bo.estimate_cost`'s
    per-model figures byte-for-byte (behavioural no-op, #601 scope item 6)."""
    roster = list(bo.MODEL_ROSTER[:2])
    arms = bo.expand_arms([m.slug for m in roster], "default")
    plain = bo.estimate_cost(corpus, roster)
    for_arms = bo.estimate_cost_for_arms(corpus, arms, roster)
    assert [dataclasses.asdict(e) for e in for_arms] == [dataclasses.asdict(e) for e in plain]


def test_estimate_cost_for_arms_off_arm_costs_the_same_as_default(
    corpus: list[bo.CorpusPage],
) -> None:
    """An "off" arm is priced identically to "default" (#601 fold round 1): explicit
    no-reasoning costs no more than the omitted setting, only its label differs."""
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    default_est = bo.estimate_cost_for_arms(corpus, bo.expand_arms(["m1"], "default"), roster)[0]
    off_est = bo.estimate_cost_for_arms(corpus, bo.expand_arms(["m1"], "off"), roster)[0]
    assert off_est.slug == "m1+reasoning-off"
    assert off_est.input_tokens == default_est.input_tokens
    assert off_est.output_tokens == default_est.output_tokens
    assert off_est.usd == default_est.usd


def test_estimate_cost_for_arms_light_multiplies_output_tokens(
    corpus: list[bo.CorpusPage],
) -> None:
    """A "light" arm's cost must reflect :data:`bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER`
    on output tokens, and be MORE expensive than the same model's "off" arm."""
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    arms = bo.expand_arms(["m1"], "both")
    off_est, light_est = bo.estimate_cost_for_arms(corpus, arms, roster)
    assert light_est.slug == "m1+reasoning-light"
    assert light_est.output_tokens == round(
        off_est.output_tokens * bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER
    )
    assert light_est.input_tokens == off_est.input_tokens
    assert light_est.usd > off_est.usd


def test_raw_priced_cost_prices_captured_tokens() -> None:
    """Priced cost multiplies captured tokens by roster list price (#601 fold
    round 1, slice A) -- synthetic diagnostics, no live provider call needed."""
    price = bo.RosterModel("m1", 2.0, 4.0, "x")  # $2/mtok in, $4/mtok out
    # $2*1 + $4*0.5 = $4.
    assert bo._raw_priced_cost(1_000_000, 500_000, price) == pytest.approx(4.0)  # pyright: ignore[reportPrivateUsage]


def test_actual_page_cost_applies_timeout_reserve_floor() -> None:
    """A page whose reserve applies can have unreported usage, so it is charged
    at ``max(priced, per_page_reserve)`` -- never the (possibly zero) priced
    amount alone; a page the reserve does NOT apply to is NEVER floored, even
    below the reserve."""
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    floored = bo._actual_page_cost(  # pyright: ignore[reportPrivateUsage]
        0, 0, price, per_page_reserve=0.01, apply_reserve=True
    )
    assert floored == pytest.approx(0.01)

    not_floored = bo._actual_page_cost(  # pyright: ignore[reportPrivateUsage]
        1, 1, price, per_page_reserve=0.01, apply_reserve=False
    )
    assert not_floored < 0.01  # NOT floored -- the reserve only applies when apply_reserve is set


def test_page_cost_reserve_scales_with_page_length() -> None:
    """The timeout-reserve floor is sized to THIS page's own content (#601
    fold round 1, slice A) -- updated for #601 fold round 7, FOLD 2's
    markdown-cap floor: an ordinary (ASCII-length, under the truncation
    ceiling) page sits AT that floor regardless of its own length (see the
    dedicated floor test below), so exceeding it now takes DENSE multi-byte
    content whose 2x-inflated bytes clear the floor -- not just more
    characters, not a corpus-wide average that under-charges a real timeout.
    """
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    short_page = bo.CorpusPage(
        slug="short", url="https://example.com/short", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    dense_page = bo.CorpusPage(
        slug="dense",
        url="https://example.com/dense",
        html="<p>" + ("咖啡豆的品質與烘焙程度密切相關。" * 2000) + "</p>",
        gold_fields={},
        vendor="x",
    )
    short_reserve = bo._page_cost_reserve(short_page, price)  # pyright: ignore[reportPrivateUsage]
    dense_reserve = bo._page_cost_reserve(dense_page, price)  # pyright: ignore[reportPrivateUsage]
    assert dense_reserve > short_reserve


def test_reserve_input_tokens_per_attempt_floors_at_the_markdown_cap() -> None:
    """#601 fold round 7, FOLD 2: a synthetic SHORT-text page's per-attempt
    input-token bound is still >= the runtime's markdown-cap-derived floor --
    a table-heavy page's real markdown COULD sit near that cap even when the
    linear-strip text (this page's entire prompt) is tiny, so the floor must
    hold regardless of actual page length."""
    page = bo.CorpusPage(
        slug="p", url="https://example.com/p", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    per_attempt_tokens = bo._reserve_input_tokens_per_attempt(page)  # pyright: ignore[reportPrivateUsage]
    markdown_cap_chars = bo._bean_sourcing_module._MAX_EXTRACTED_CHARS  # pyright: ignore[reportPrivateUsage]
    max_bytes_per_char = bo._RESERVE_MAX_BYTES_PER_CHAR  # pyright: ignore[reportPrivateUsage]
    assert per_attempt_tokens >= markdown_cap_chars * max_bytes_per_char


def test_page_cost_reserve_output_component_is_the_enforced_cap_times_retries() -> None:
    """The reserve's OUTPUT-token component equals the ENFORCED provider cap
    times ``1 + EXTRACTION_MAX_RETRIES`` (#601 fold round 4/8, FOLD 4; the
    transport-retry factor is GONE as of round 8 -- the bake-off's paid
    calls disable SDK transport retries entirely, Refs slice E), priced in
    full. Uniform across every arm/model (isolated here via an output-only
    price)."""
    page = bo.CorpusPage(
        slug="p", url="https://example.com/p", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    output_only_price = bo.RosterModel("m1", 0.0, 1.0, "x")  # isolates the output component
    cap = 4096
    reserve = bo._page_cost_reserve(  # pyright: ignore[reportPrivateUsage]
        page, output_only_price, max_output_tokens=cap
    )
    max_retries = bo._bean_sourcing_module.EXTRACTION_MAX_RETRIES  # pyright: ignore[reportPrivateUsage]
    expected_output_tokens = cap * (1 + max_retries)
    assert reserve == pytest.approx(expected_output_tokens / 1_000_000 * 1.0)


def test_page_cost_reserve_input_component_accounts_for_retries() -> None:
    """#601 fold round 6/8, FOLD B (revised round 8): the reserve's INPUT
    component accounts for EVERY validation attempt re-sending the prompt,
    plus each RETRY additionally re-sending the PRIOR RESPONSE (up to the
    output cap) AND its own serialized validation-error copy of it
    (``RetryPromptPart`` quotes the offending output back -- a SECOND
    cap-sized term) plus a small fixed wrapper -- not a flat single-prompt
    bound (isolated here via an input-only price)."""
    page = bo.CorpusPage(
        slug="p", url="https://example.com/p", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    input_only_price = bo.RosterModel("m1", 1.0, 0.0, "x")  # isolates the input component
    cap = 4096
    reserve = bo._page_cost_reserve(  # pyright: ignore[reportPrivateUsage]
        page, input_only_price, max_output_tokens=cap
    )
    per_attempt = bo._reserve_input_tokens_per_attempt(page)  # pyright: ignore[reportPrivateUsage]
    max_retries = bo._bean_sourcing_module.EXTRACTION_MAX_RETRIES  # pyright: ignore[reportPrivateUsage]
    wrapper_overhead = bo._RESERVE_RETRY_WRAPPER_TOKENS  # pyright: ignore[reportPrivateUsage]
    expected_input_tokens = (1 + max_retries) * per_attempt + max_retries * (
        2 * cap + wrapper_overhead
    )
    assert reserve == pytest.approx(expected_input_tokens / 1_000_000 * 1.0)


def test_page_cost_reserve_and_single_attempt_reserve_pinned_literal_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#601 qa pass, round 8: an INDEPENDENT, hand-computed literal check --
    the other formula tests recombine the SAME constants production uses, so
    a shared authoring misunderstanding could pass unnoticed. Pins every
    input to a small fixed value and asserts an exact USD figure computed by
    hand.

    Fixed inputs: per-attempt input tokens = 50 (monkeypatched, bypassing the
    byte/markdown-cap sub-formula -- already covered by its own dedicated
    tests), output cap = 100, EXTRACTION_MAX_RETRIES = 1,
    _RESERVE_RETRY_WRAPPER_TOKENS = 10, price = $1/mtok in, $2/mtok out.

    _page_cost_reserve (2 total requests -- 1 initial + 1 retry):
        input  = (1+1)*50 + 1*(2*100 + 10) = 100 + 210 = 310
        output = 100 * (1+1) = 200
        usd    = 310/1e6*1 + 200/1e6*2 = 0.00031 + 0.0004 = 0.00071

    _single_attempt_reserve (the worst SINGLE attempt, a retry):
        input  = 50 + 2*100 + 10 = 260
        output = 100
        usd    = 260/1e6*1 + 100/1e6*2 = 0.00026 + 0.0002 = 0.00046
    """

    def _fixed_per_attempt_input(page: bo.CorpusPage) -> int:
        return 50

    monkeypatch.setattr(bo, "_reserve_input_tokens_per_attempt", _fixed_per_attempt_input)
    monkeypatch.setattr(
        bo._bean_sourcing_module,  # pyright: ignore[reportPrivateUsage]
        "EXTRACTION_MAX_RETRIES",
        1,
    )
    monkeypatch.setattr(bo, "_RESERVE_RETRY_WRAPPER_TOKENS", 10)  # pyright: ignore[reportPrivateUsage]
    page = bo.CorpusPage(
        slug="p", url="https://example.com/p", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    price = bo.RosterModel("m1", 1.0, 2.0, "x")

    full_reserve = bo._page_cost_reserve(  # pyright: ignore[reportPrivateUsage]
        page, price, max_output_tokens=100
    )
    assert full_reserve == pytest.approx(0.00071)

    single_reserve = bo._single_attempt_reserve(  # pyright: ignore[reportPrivateUsage]
        page, price, max_output_tokens=100
    )
    assert single_reserve == pytest.approx(0.00046)


def test_actual_page_cost_final_timeout_uses_single_attempt_reserve_not_multi() -> None:
    """#601 fold round 6, FOLD D (P2): a FINAL timed-out entry's reserve
    addition is ONE attempt's worst case (:func:`bo._single_attempt_reserve`),
    never the full multi-attempt worst case (:func:`bo._page_cost_reserve`)
    the PENDING entry uses -- every COMPLETED retry's usage is already in
    ``request_tokens``/``response_tokens``, so at most ONE in-flight request
    can ever be unreported. Holds for a PLAIN timeout (zero captured usage)
    and a retry-completes-then-timeout page (real usage already captured)."""
    page = bo.CorpusPage(
        slug="p", url="https://example.com/p", html="<p>Hi.</p>", gold_fields={}, vendor="x"
    )
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    full_reserve = bo._page_cost_reserve(page, price)  # pyright: ignore[reportPrivateUsage]
    single_reserve = bo._single_attempt_reserve(page, price)  # pyright: ignore[reportPrivateUsage]
    assert single_reserve < full_reserve  # strictly smaller (EXTRACTION_MAX_RETRIES >= 1)

    plain = bo._actual_page_cost(  # pyright: ignore[reportPrivateUsage]
        0, 0, price, single_reserve, apply_reserve=True
    )
    assert plain == pytest.approx(single_reserve)

    retry_then_timeout = bo._actual_page_cost(  # pyright: ignore[reportPrivateUsage]
        500, 100, price, single_reserve, apply_reserve=True
    )
    assert retry_then_timeout == pytest.approx(
        bo._raw_priced_cost(500, 100, price) + single_reserve  # pyright: ignore[reportPrivateUsage]
    )
    assert retry_then_timeout > plain  # the completed retry's captured usage adds on top


def test_reserve_instruction_overhead_is_derived_not_guessed() -> None:
    """#601 fold round 3, FOLD 3: the reserve's overhead is DERIVED from the
    ACTUAL runtime extraction instructions (+ schema + margin), so it must be AT
    LEAST the instructions' own byte length -- never a smaller, hand-picked
    guess like the (separate) planning heuristic's 1600 chars."""
    overhead = bo._RESERVE_INSTRUCTION_OVERHEAD_BYTES  # pyright: ignore[reportPrivateUsage]
    instructions_bytes = len(bo._EXTRACTION_INSTRUCTIONS.encode("utf-8"))  # pyright: ignore[reportPrivateUsage]
    assert overhead >= instructions_bytes


def test_page_cost_reserve_bounds_by_bytes_not_code_points(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#601 fold round 3, FOLD 3: an emoji-dense prompt's reserve must reflect
    its UTF-8 BYTE length, not its (smaller) code-point count -- byte-level BPE
    can emit up to one token per byte, so code points alone would under-price a
    token-dense page. Bypasses real linear-strip extraction (monkeypatches
    :func:`bo._reserve_prompt_text`) for a deterministic, same-code-point-count
    comparison."""
    emoji_text = "\U0001f600" * 200  # each code point is 4 UTF-8 bytes
    plain_text = "a" * 200  # SAME code-point count, 1 byte each
    assert len(emoji_text) == len(plain_text)
    assert len(emoji_text.encode("utf-8")) > len(plain_text.encode("utf-8"))

    def _fake_reserve_prompt_text(page: bo.CorpusPage) -> str:
        return emoji_text if page.slug == "emoji" else plain_text

    monkeypatch.setattr(bo, "_reserve_prompt_text", _fake_reserve_prompt_text)
    price = bo.RosterModel("m1", 1.0, 0.0, "x")  # isolate the INPUT component
    emoji_page = bo.CorpusPage(
        slug="emoji", url="https://x/e", html="x", gold_fields={}, vendor="x"
    )
    plain_page = bo.CorpusPage(
        slug="plain", url="https://x/p", html="x", gold_fields={}, vendor="x"
    )
    emoji_reserve = bo._page_cost_reserve(emoji_page, price)  # pyright: ignore[reportPrivateUsage]
    plain_reserve = bo._page_cost_reserve(plain_page, price)  # pyright: ignore[reportPrivateUsage]
    assert emoji_reserve > plain_reserve  # same code-point count, more BYTES


def test_charge_ledger_total_usd_sums_priced_entries_and_reloads(tmp_path: Path) -> None:
    """The persistent :class:`bo.ChargeLedger` (#601 fold round 1, slice A) rolls up
    every entry's already-priced ``priced_usd`` -- synthetic entries, no live
    provider call needed -- and a fresh instance over the SAME path reloads every
    entry from disk (this is exactly how a resumed invocation will reconstruct
    spend, once a follow-on slice adds a spend guard that reads it)."""
    path = bo.ledger_path(tmp_path / "o.json")
    ledger = bo.ChargeLedger(path)
    # Distinct call_ids (#601 fold round 6, FOLD A) -- two genuinely separate
    # calls (different arms) must never collide under one supersession key.
    ledger.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=1_000_000,
            response_tokens=500_000,
            priced_usd=4.0,
            timed_out=False,
            reserve_applied=False,
            call_id="call-m1",
        )
    )
    ledger.append(
        bo.LedgerEntry(
            arm="m2",
            slug="a",
            request_tokens=1,
            response_tokens=1,
            priced_usd=0.0001,
            timed_out=False,
            reserve_applied=False,
            call_id="call-m2",
        )
    )
    assert ledger.total_usd() == pytest.approx(4.0001)
    reloaded = bo.ChargeLedger(path)
    assert reloaded.total_usd() == pytest.approx(4.0001)


def test_charge_ledger_skips_a_blank_line_on_load(tmp_path: Path) -> None:
    """A stray blank line in the ledger JSONL (a manual edit, or a partial write) is
    skipped on load, mirroring :class:`bo.Checkpoint`'s tolerance."""
    path = tmp_path / "o.json.ledger.jsonl"
    entry = {
        "arm": "m1",
        "slug": "a",
        "request_tokens": 1,
        "response_tokens": 1,
        "priced_usd": 0.01,
        "timed_out": False,
        "reserve_applied": False,
        "fingerprint": "fp-a",
    }
    path.write_text(json.dumps(entry) + "\n\n")  # a trailing blank line
    ledger = bo.ChargeLedger(path, fingerprint="fp-a")
    assert len(ledger.entries) == 1
    assert ledger.total_usd() == pytest.approx(0.01)


def _seeded_ledger_entry() -> bo.LedgerEntry:
    return bo.LedgerEntry(
        arm="m1",
        slug="a",
        request_tokens=1,
        response_tokens=1,
        priced_usd=0.05,
        timed_out=False,
        reserve_applied=False,
    )


def test_charge_ledger_no_resume_wipes_a_prior_experiment(tmp_path: Path) -> None:
    """``resume=False`` unlinks a pre-existing ledger, mirroring
    :class:`bo.Checkpoint` (#601 fold round 1, FOLD 1) -- a fresh experiment means
    a fresh budget; a completed PRIOR experiment's charges must never silently eat
    a later, unrelated one's --max-spend."""
    path = tmp_path / "o.json.ledger.jsonl"
    seeded = bo.ChargeLedger(path)
    seeded.append(_seeded_ledger_entry())
    assert seeded.total_usd() == pytest.approx(0.05)

    fresh = bo.ChargeLedger(path, resume=False)
    assert fresh.total_usd() == pytest.approx(0.0)
    assert fresh.entries == []
    assert not path.exists()  # unlinked, not just ignored in memory


def test_charge_ledger_resume_preserves_prior_charges(tmp_path: Path) -> None:
    path = tmp_path / "o.json.ledger.jsonl"
    seeded = bo.ChargeLedger(path)
    seeded.append(_seeded_ledger_entry())

    resumed = bo.ChargeLedger(path, resume=True)
    assert resumed.total_usd() == pytest.approx(0.05)
    assert len(resumed.entries) == 1


def test_charge_ledger_scopes_total_usd_to_the_current_fingerprint(tmp_path: Path) -> None:
    """#601 fold round 3, FOLD 4: a fingerprint change (corpus/pipeline/
    environment) starts a fresh budget -- ``total_usd()`` counts ONLY the
    CURRENT lineage's entries, but the file (``entries``) retains every
    lineage EVER written -- an append-only money-history audit trail, never
    wiped on a fingerprint change (only ``resume=False`` wipes it)."""
    path = tmp_path / "o.json.ledger.jsonl"
    old_lineage = bo.ChargeLedger(path, fingerprint="fp-old")
    old_lineage.append(_seeded_ledger_entry())  # $0.05 under "fp-old"

    new_lineage = bo.ChargeLedger(path, fingerprint="fp-new")
    assert new_lineage.total_usd() == pytest.approx(0.0)  # a fresh budget
    new_lineage.append(_seeded_ledger_entry())  # $0.05 under "fp-new"
    assert new_lineage.total_usd() == pytest.approx(0.05)  # only ITS lineage

    reloaded = bo.ChargeLedger(path, fingerprint="fp-new")
    assert len(reloaded.entries) == 2  # BOTH lineages still on disk
    assert reloaded.total_usd() == pytest.approx(0.05)  # still scoped to fp-new


def test_charge_ledger_excludes_a_legacy_entry_with_no_fingerprint(tmp_path: Path) -> None:
    """#601 fold round 3, FOLD 4: a pre-fold ledger entry with NO persisted
    ``fingerprint`` key is treated as non-matching -- excluded from the meter
    regardless of the CURRENT fingerprint (fail-closed, never silently
    trusted) -- but stays visible in ``entries`` (the audit trail)."""
    path = tmp_path / "o.json.ledger.jsonl"
    legacy_record = dataclasses.asdict(_seeded_ledger_entry())
    del legacy_record["fingerprint"]  # a genuinely pre-fold-4 on-disk record
    path.write_text(json.dumps(legacy_record) + "\n")

    ledger = bo.ChargeLedger(path, fingerprint="")  # even the disabled-guard default
    assert len(ledger.entries) == 1
    assert ledger.total_usd() == pytest.approx(0.0)  # never silently trusted


def _pending_entry(*, priced_usd: float = 0.05, call_id: str = "") -> bo.LedgerEntry:
    return bo.LedgerEntry(
        arm="m1",
        slug="a",
        request_tokens=0,
        response_tokens=0,
        priced_usd=priced_usd,
        timed_out=False,
        reserve_applied=True,
        is_pending=True,
        call_id=call_id,
    )


def test_charge_ledger_kill_between_pending_and_final_counts_the_reserve(
    tmp_path: Path,
) -> None:
    """#601 fold round 4, FOLD 1: a PENDING-only entry (a kill landed before the
    FINAL write) still counts, at its reserve valuation -- the two-phase
    design's whole point: a mid-call kill must never silently drop a page's
    worst-case charge from the books."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(_pending_entry(priced_usd=0.07))
    assert ledger.total_usd() == pytest.approx(0.07)

    reloaded = bo.ChargeLedger(path)  # a fresh process, reloading from disk
    assert reloaded.total_usd() == pytest.approx(0.07)


def test_charge_ledger_final_supersedes_pending_not_summed(tmp_path: Path) -> None:
    """#601 fold round 4, FOLD 1: the FINAL entry for a ``(arm, slug)`` key
    REPLACES its PENDING one in the meter -- the call completed, so the
    reserve's worst-case guess retires. Summing them would double-charge
    every normal (non-killed) page."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(_pending_entry(priced_usd=0.07))  # the reserve, pre-call
    ledger.append(  # the real, smaller, post-call charge
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
            is_pending=False,
        )
    )
    assert ledger.total_usd() == pytest.approx(0.02)  # NOT 0.09
    assert len(ledger.entries) == 2  # both stay on disk -- the audit trail


def test_charge_ledger_a_pending_entry_never_eclipses_an_existing_final(
    tmp_path: Path,
) -> None:
    """#601 fold round 4, FOLD 1: a FINAL entry, once recorded, is never
    superseded by a LATER pending write for the same ``(arm, slug)`` key --
    the defensive direction of :meth:`bo.ChargeLedger._effective_entries`'s
    supersede rule (final always wins, regardless of write order)."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
            is_pending=False,
        )
    )
    ledger.append(_pending_entry(priced_usd=0.07))  # arrives AFTER the final
    assert ledger.total_usd() == pytest.approx(0.02)  # the final still wins


def test_charge_ledger_resume_after_pending_counts_only_the_new_final(
    tmp_path: Path,
) -> None:
    """#601 fold round 4, FOLD 1 + fold round 6, FOLD A: resuming after a kill
    re-attempts the page, writing a FRESH pending + final pair -- a NEW
    call_id, since it is a genuinely separate call -- alongside the orphaned
    pending from the killed attempt (same ``(arm, slug)``, DIFFERENT
    ``call_id``, same lineage). The meter must count only the winning final
    for the resumed call_id, never double-count the orphaned one's own
    pending (which, being a DIFFERENT call, still counts on its own -- see
    :func:`test_charge_ledger_two_genuinely_separate_calls_for_one_page_both_count`
    for the case where the orphan's call itself later completes)."""
    path = tmp_path / "o.json.ledger.jsonl"
    killed_run = bo.ChargeLedger(path, fingerprint="fp-1")
    killed_run.append(
        _pending_entry(priced_usd=0.07, call_id="call-killed")
    )  # orphaned: no final follows

    resumed = bo.ChargeLedger(path, fingerprint="fp-1")
    assert resumed.total_usd() == pytest.approx(0.07)  # still just the orphan, pre-resume
    resumed.append(
        _pending_entry(priced_usd=0.06, call_id="call-resumed")
    )  # the resumed attempt's OWN call
    resumed.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=80,
            response_tokens=40,
            priced_usd=0.015,
            timed_out=False,
            reserve_applied=False,
            is_pending=False,
            call_id="call-resumed",
        )
    )
    # The orphan's own pending (0.07, never finalized) still counts on its
    # own call_id; the resumed call's final (0.015) supersedes ITS pending.
    assert resumed.total_usd() == pytest.approx(0.085)
    assert len(resumed.entries) == 3  # orphaned pending + resumed pending + final


def test_charge_ledger_two_genuinely_separate_calls_for_one_page_both_count(
    tmp_path: Path,
) -> None:
    """#601 fold round 6, FOLD A (P1): a page CALLED TWICE across a resume --
    e.g. it succeeded before the arm's run later failed wholesale and re-ran
    from page 0 -- is TWO real, separate charges. Keying supersession by bare
    ``(arm, slug)`` would collapse them into one, under-counting actual
    cumulative spend; keying by ``call_id`` keeps both."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    first_call = bo.LedgerEntry(
        arm="m1",
        slug="a",
        request_tokens=100,
        response_tokens=50,
        priced_usd=0.02,
        timed_out=False,
        reserve_applied=False,
        is_pending=False,
        call_id="call-1",
    )
    second_call = dataclasses.replace(first_call, priced_usd=0.03, call_id="call-2")
    ledger.append(first_call)
    ledger.append(second_call)
    assert ledger.total_usd() == pytest.approx(0.05)  # BOTH real calls counted


def test_charge_ledger_total_usd_for_arm_supersedes_pending_not_summed(
    tmp_path: Path,
) -> None:
    """#601 fold round 12 (final review): ``total_usd_for_arm()`` must share
    ``total_usd()``'s :meth:`bo.ChargeLedger._effective_entries` supersession
    rule -- a raw sum over ``self._entries`` double-charged every normally
    completed page's pending reserve on top of its final charge in the
    per-arm figure (the report column and the ``actual_costs`` JSON
    artifact both read this)."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(_pending_entry(priced_usd=0.07, call_id="call-1"))  # the reserve
    ledger.append(  # the real, smaller, post-call charge, same call_id
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
            is_pending=False,
            call_id="call-1",
        )
    )
    assert ledger.total_usd_for_arm("m1") == pytest.approx(0.02)  # NOT 0.09


def test_charge_ledger_total_usd_for_arm_counts_kill_window_pending_at_the_reserve(
    tmp_path: Path,
) -> None:
    """A page killed mid-call (pending only, no final ever follows) still
    counts at its reserve in the per-arm figure -- the same fail-safe
    :meth:`bo.ChargeLedger._effective_entries` already guarantees for
    ``total_usd()``."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(_pending_entry(priced_usd=0.07, call_id="call-killed"))
    assert ledger.total_usd_for_arm("m1") == pytest.approx(0.07)


def test_ledger_actual_costs_matches_total_usd_for_arm_semantics(tmp_path: Path) -> None:
    """The report/JSON-artifact ``actual_costs`` figure
    (:func:`bo._ledger_actual_costs`) must share ``total_usd_for_arm()``'s
    supersession semantics on the SAME ledger -- a stale, un-superseded sum
    here would silently overstate a normally-completed arm's reported cost
    even though the meter itself (:class:`bo.SpendMeter`, seeded from
    ``total_usd()``) stayed correct: an easy-to-miss reporting-only
    regression, not a spend-guard one."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(_pending_entry(priced_usd=0.07, call_id="call-1"))
    ledger.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
            is_pending=False,
            call_id="call-1",
        )
    )
    actual = bo._ledger_actual_costs(ledger)  # pyright: ignore[reportPrivateUsage]
    assert actual["m1"] == pytest.approx(ledger.total_usd_for_arm("m1"))
    assert actual["m1"] == pytest.approx(0.02)  # NOT 0.09


def test_charge_ledger_reserved_usd_for_arm_sums_the_persisted_field(
    tmp_path: Path,
) -> None:
    """#601 fold round 14: ``reserved_usd_for_arm()`` sums the PERSISTED
    ``reserved_usd`` field, not a derived ``reserve_applied``-filtered whole
    ``priced_usd`` -- a pure-captured page (``reserved_usd=0.0``) never
    contributes; a zero-usage reserved (timeout) page's WHOLE charge is its
    reserve, so it contributes its full amount."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(  # pure captured, no reserve
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
            call_id="call-1",
        )
    )
    ledger.append(  # a genuine reserved (timeout) page, zero captured usage
        bo.LedgerEntry(
            arm="m1",
            slug="b",
            request_tokens=0,
            response_tokens=0,
            priced_usd=0.05,
            timed_out=True,
            reserve_applied=True,
            reserved_usd=0.05,
            call_id="call-2",
        )
    )
    assert ledger.total_usd_for_arm("m1") == pytest.approx(0.07)
    assert ledger.reserved_usd_for_arm("m1") == pytest.approx(0.05)  # NOT 0.07
    reserved = bo._ledger_reserved_costs(ledger)  # pyright: ignore[reportPrivateUsage]
    assert reserved["m1"] == pytest.approx(0.05)


def test_charge_ledger_reserved_usd_for_arm_excludes_captured_usage(
    tmp_path: Path,
) -> None:
    """#601 fold round 14: a reserved entry that ALSO captured real usage (a
    retry succeeded before the final attempt hit a provider error)
    discloses only the ADDED reserve component -- never the whole,
    usage-inclusive ``priced_usd``."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=50,
            response_tokens=10,
            priced_usd=0.05,  # captured (0.02) + reserve (0.03) -- mixed case
            timed_out=False,
            reserve_applied=True,
            reserved_usd=0.03,
        )
    )
    assert ledger.total_usd_for_arm("m1") == pytest.approx(0.05)
    assert ledger.reserved_usd_for_arm("m1") == pytest.approx(0.03)  # NOT 0.05


def test_charge_ledger_reserved_usd_for_arm_zero_when_pure_captured(
    tmp_path: Path,
) -> None:
    """An arm with no reserved entries reports ``0.0``, never absent."""
    path = tmp_path / "o.json.ledger.jsonl"
    ledger = bo.ChargeLedger(path)
    ledger.append(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=100,
            response_tokens=50,
            priced_usd=0.02,
            timed_out=False,
            reserve_applied=False,
        )
    )
    assert ledger.reserved_usd_for_arm("m1") == pytest.approx(0.0)


def test_charge_ledger_legacy_reserve_applied_entry_discloses_full_amount(
    tmp_path: Path,
) -> None:
    """#650 round-1: a pre-#601-fold-round-14 legacy record (NO
    ``reserved_usd`` key at all, ``reserve_applied=True``) has an
    UNRECOVERABLE captured-vs-reserve split -- the loader falls back to the
    WHOLE ``priced_usd``, conservative in the DISCLOSURE direction (overstate
    reserved rather than silently claim observed captured usage)."""
    path = tmp_path / "o.json.ledger.jsonl"
    legacy_reserved = dataclasses.asdict(
        bo.LedgerEntry(
            arm="m1",
            slug="a",
            request_tokens=0,
            response_tokens=0,
            priced_usd=0.05,
            timed_out=True,
            reserve_applied=True,
        )
    )
    del legacy_reserved["reserved_usd"]  # genuinely pre-#650 on-disk shape
    path.write_text(json.dumps(legacy_reserved) + "\n")

    ledger = bo.ChargeLedger(path)
    assert len(ledger.entries) == 1
    assert ledger.entries[0].reserved_usd == pytest.approx(0.05)
    assert ledger.reserved_usd_for_arm("m1") == pytest.approx(0.05)


def test_charge_ledger_legacy_non_reserved_entry_discloses_zero(tmp_path: Path) -> None:
    """A legacy record (no ``reserved_usd`` key) with ``reserve_applied=False``
    still defaults to ``0.0`` -- the fallback only widens for a RESERVED
    legacy entry, never invents a reserve where none was applied."""
    path = tmp_path / "o.json.ledger.jsonl"
    legacy_plain = dataclasses.asdict(_seeded_ledger_entry())
    del legacy_plain["reserved_usd"]
    path.write_text(json.dumps(legacy_plain) + "\n")

    ledger = bo.ChargeLedger(path)
    assert ledger.entries[0].reserved_usd == pytest.approx(0.0)


def test_load_dotenv_key(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="sk-or-secret"\nOTHER=1\n')
    assert bo.load_dotenv_key(tmp_path) == "sk-or-secret"
    assert bo.load_dotenv_key(tmp_path / "nope") is None


def test_resolve_roster_for_slugs_roster_members() -> None:
    slugs = [bo.MODEL_ROSTER[0].slug, bo.MODEL_ROSTER[1].slug]
    resolved = bo.resolve_roster_for_slugs(slugs)
    assert [m.slug for m in resolved] == slugs


def test_resolve_roster_for_slugs_rejects_unpriced_custom_slug() -> None:
    """A custom ``--models`` slug with no roster price used to fall back to
    an implicit $0 estimate, letting it bypass ``--max-spend`` entirely
    (#600 finding); it must be refused instead."""
    with pytest.raises(ValueError, match="some/unknown-slug"):
        bo.resolve_roster_for_slugs(["some/unknown-slug"])


@pytest.mark.asyncio
async def test_main_refuses_without_max_spend() -> None:
    assert await bo.main(["--out", "/tmp/rp588-should-not-write.json"]) == 2


@pytest.mark.asyncio
async def test_main_refuses_unpriced_custom_model(capsys: pytest.CaptureFixture[str]) -> None:
    code = await bo.main(["--models", "some/unknown-slug", "--max-spend", "5"])
    assert code == 2
    err = capsys.readouterr().err
    assert "some/unknown-slug" in err


@pytest.mark.asyncio
async def test_main_refuses_reasoning_light_with_no_capable_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--reasoning light`` against a roster with ZERO reasoning-capable models must
    refuse before any spend (#601 fold round 2, F3; the categorical empty-expansion
    guard, fold round 4) -- gpt-4o is non-reasoning."""
    code = await bo.main(["--models", "openai/gpt-4o", "--reasoning", "light", "--max-spend", "5"])
    assert code == 2
    assert "nothing to run" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_refuses_reasoning_off_with_no_off_capable_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--reasoning off`` must refuse pre-spend, not just ``light`` -- gpt-4o is
    "none" (off skipped) and gpt-5-mini is "mandatory" (off skipped), so EVERY
    requested model is skipped and the arm list is empty (#601 fold round 4)."""
    code = await bo.main(
        [
            "--models",
            "openai/gpt-4o",
            "openai/gpt-5-mini",
            "--reasoning",
            "off",
            "--max-spend",
            "5",
        ]
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "REFUSED: --reasoning off" in err
    assert "nothing to run" in err


@pytest.mark.asyncio
async def test_main_refuses_reasoning_off_with_no_off_capable_model_estimate_only(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The categorical empty-expansion guard fires even under ``--estimate-only`` --
    it must not exit 0 with a zero-arm estimate (#601 fold round 4)."""
    code = await bo.main(
        ["--models", "openai/gpt-4o", "openai/gpt-5-mini", "--reasoning", "off", "--estimate-only"]
    )
    assert code == 2
    assert "nothing to run" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_refuses_reasoning_both_with_no_comparable_model(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--reasoning both`` must refuse pre-spend when NO requested model has both an
    off and a light arm -- gpt-4o is "none", gpt-5-mini is "mandatory" (off-incapable),
    so nothing is comparable (#601 fold round 3, FA)."""
    code = await bo.main(
        [
            "--models",
            "openai/gpt-4o",
            "openai/gpt-5-mini",
            "--reasoning",
            "both",
            "--max-spend",
            "5",
        ]
    )
    assert code == 2
    assert "no requested model has BOTH off and light arms" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_main_estimate_only_is_zero_spend(capsys: pytest.CaptureFixture[str]) -> None:
    assert await bo.main(["--estimate-only"]) == 0
    out = capsys.readouterr().out
    assert "arm total" in out  # #601: "roster total" renamed once arms can outnumber models


# --- --max-spend argparse validation (#602 finding) --------------------------


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "-1"])
def test_parse_args_rejects_non_finite_or_negative_max_spend(raw: str) -> None:
    """``float()`` happily parses ``inf``/``nan``, and every
    ``spent + upcoming > max_spend`` guard comparison in :func:`run_bakeoff`
    is FALSE against either -- silently bypassing the explicit spend guard
    (#602 finding)."""
    with pytest.raises(SystemExit):
        bo._parse_args(["--max-spend", raw])  # pyright: ignore[reportPrivateUsage]


def test_parse_args_rejects_a_non_numeric_max_spend() -> None:
    with pytest.raises(SystemExit):
        bo._parse_args(["--max-spend", "not-a-number"])  # pyright: ignore[reportPrivateUsage]


def test_parse_args_accepts_a_finite_nonnegative_max_spend() -> None:
    args = bo._parse_args(["--max-spend", "3.5"])  # pyright: ignore[reportPrivateUsage]
    assert args.max_spend == pytest.approx(3.5)
    zero_args = bo._parse_args(["--max-spend", "0"])  # pyright: ignore[reportPrivateUsage]
    assert zero_args.max_spend == 0.0


def test_parse_args_rejects_duplicate_models() -> None:
    """A duplicate ``--models`` slug is rejected AT PARSE TIME (#601 fold round 1,
    cheapest categorical fix) -- it would otherwise run + charge the ledger twice
    under one report row."""
    with pytest.raises(SystemExit):
        bo._parse_args(["--models", "m1", "m2", "m1"])  # pyright: ignore[reportPrivateUsage]


def test_parse_args_accepts_distinct_models() -> None:
    args = bo._parse_args(["--models", "m1", "m2"])  # pyright: ignore[reportPrivateUsage]
    assert args.models == ["m1", "m2"]


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
    assert "## Cost" in report
    assert "SCREENING harness, not certification" in report
    assert "RANGE-altitude COR" in report  # the range-altitude caveat (#600)
    assert "PARTIAL RUN" not in report
    assert "EXCLUDED -- failed this invocation" not in report


def test_render_report_headline_table_shows_schema_counts_for_unpaired_arm(
    corpus: list[bo.CorpusPage],
) -> None:
    """schema_failures/recovered_violations must render in the per-model HEADLINE
    table (always present, every arm) -- not just the off-vs-light paired section --
    so a lone ``--reasoning off``/``light`` run (or an unpaired arm left by a budget
    stop) still surfaces the primary adherence signal (#601 fold round 6)."""
    fields = {spec.name: bo.Outcome.ERR for spec in bo.FIELD_SPECS}
    page = bo.PageResult(
        slug="p",
        outcomes=dict(fields),
        error="BeanExtractionUnavailableError: ... returned a malformed shape: x",
        on_page_fields=0,
    )
    run = bo.ModelRun(model_slug="model-a+reasoning-off", pages=[page])
    report = bo.render_report([run], bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "schema F/R" in report  # the new headline column
    assert "| 1/0 |" in report  # schema_failures=1, recovered_violations=0
    assert "Reasoning-arm comparison" not in report  # no light sibling -- unpaired


def test_render_report_pairwise_reports_every_promised_axis(
    corpus: list[bo.CorpusPage],
) -> None:
    """The report's own selection rule leans on P/R/A bootstrap CIs, not
    recall alone -- precision and abstention-correctness gaps must be
    reported too (#600 finding)."""
    runs = [_full_run("model-a", bo.Outcome.COR), _full_run("model-b", bo.Outcome.INC)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:2]))
    assert "faithfulness (precision) gap" in report
    assert "abstention gap" in report


def test_render_report_pairwise_shows_na_not_zero_for_undefined_metric(
    corpus: list[bo.CorpusPage],
) -> None:
    """Two models that only ever abstain (ABS_COR) have an undefined
    faithfulness/abstention denominator -- the gap must render ``n/a``, never
    a fabricated ``+0.000`` that reads as a confirmed tie (#602 finding)."""
    runs = [_full_run("model-a", bo.Outcome.ABS_COR), _full_run("model-b", bo.Outcome.ABS_COR)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:2]))
    assert "faithfulness (precision) gap n/a" in report


def test_render_report_emits_wilson_intervals(corpus: list[bo.CorpusPage]) -> None:
    """The report promises Wilson intervals (module docstring + CAVEAT_TEXT)
    -- they must actually be rendered, not just computed and dropped (#602
    finding)."""
    runs = [_full_run("model-a", bo.Outcome.COR)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "## Wilson intervals" in report
    assert "`model-a`" in report
    assert "95% Wilson CI" in report


def test_render_report_wilson_zero_trials_renders_na(corpus: list[bo.CorpusPage]) -> None:
    """A model with zero present-field decisions (every field ABS_COR) has
    an undefined Wilson proportion -- rendered 'n/a', never a fabricated
    '0.000' (#602 fold round 4, FOLD 5)."""
    runs = [_full_run("model-a", bo.Outcome.ABS_COR)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "| `model-a` | 0/0 | n/a | [0.000, 1.000] |" in report


def test_render_report_marks_budget_stopped_runs_partial(
    corpus: list[bo.CorpusPage],
) -> None:
    runs = [_full_run("model-a", bo.Outcome.COR)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]),
        stopped_early=True,
        unevaluated_slugs=["model-b", "model-c"],
    )
    assert "PARTIAL RUN" in report
    assert "model-b" in report
    assert "model-c" in report


def test_render_report_marks_failed_models_excluded(corpus: list[bo.CorpusPage]) -> None:
    """A wholly-failed model must appear ONLY in the exclusion banner, never
    in the leaderboard/per-page/pairwise sections below it (#600 round-2
    finding) -- and the banner shows its DISPLAY-ONLY heuristic label,
    never checkpointed regardless of it (#602 fold round 5)."""
    runs = [_full_run("model-a", bo.Outcome.COR)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]),
        failed_slugs=[bo.FailedRun(model_slug="model-failed", heuristic_label="MODEL-SPECIFIC")],
    )
    assert "EXCLUDED -- failed this invocation" in report
    assert "`model-failed` (MODEL-SPECIFIC, schema 0/other 0)" in report
    assert "- models scored: 1" in report  # the failed model is NOT counted


def test_render_report_labels_still_retryable_pages(corpus: list[bo.CorpusPage]) -> None:
    """#649: a run with any still-retryable page is labelled in the report, so
    a partial/degraded result is never silently presented as final."""
    fields = {spec.name: bo.Outcome.COR for spec in bo.FIELD_SPECS}
    run = bo.ModelRun(
        model_slug="model-a",
        pages=[
            bo.PageResult(
                slug="p1", outcomes=dict(fields), error="boom", on_page_fields=0, retryable=True
            ),
            bo.PageResult(slug="p2", outcomes=dict(fields), error=None, on_page_fields=1),
        ],
    )
    report = bo.render_report([run], bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "RESUME TO RETRY" in report
    assert "`model-a` (1)" in report


def test_render_report_no_retryable_banner_when_nothing_retryable(
    corpus: list[bo.CorpusPage],
) -> None:
    runs = [_full_run("model-a", bo.Outcome.COR)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "RESUME TO RETRY" not in report


def test_render_report_pairwise_covers_all_pairs_not_just_first(
    corpus: list[bo.CorpusPage],
) -> None:
    """The pairwise section must generate every comparison the selection
    relies on, not just first-model-vs-rest (#600 round-2 finding)."""
    runs = [
        _full_run("model-a", bo.Outcome.COR),
        _full_run("model-b", bo.Outcome.PAR),
        _full_run("model-c", bo.Outcome.INC),
    ]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:3]))
    # C(3,2) = 3 pairs, including model-b vs model-c (neither is "the first model").
    assert "`model-a` vs `model-b`" in report
    assert "`model-a` vs `model-c`" in report
    assert "`model-b` vs `model-c`" in report


def test_render_report_labels_actual_spend_vs_resumed(corpus: list[bo.CorpusPage]) -> None:
    """A newly-called model's cost must be labelled as spend INCURRED, not
    the generic pre-run 'NOT yet spent' estimate framing (#600 round-2
    finding) -- and never claimed as verified ACTUAL billing, since this
    harness has no live token-usage/billing readback (#602 finding: the
    round-2 'ACTUALLY SPENT' wording overclaimed this)."""
    runs = [_full_run("model-a", bo.Outcome.COR), _full_run("model-b", bo.Outcome.COR)]
    cost_estimates = [
        bo.ModelCostEstimate(slug="model-a", input_tokens=100, output_tokens=50, usd=0.01),
        bo.ModelCostEstimate(slug="model-b", input_tokens=100, output_tokens=50, usd=0.02),
    ]
    report = bo.render_report(runs, cost_estimates, executed_slugs=["model-a"])
    assert "ESTIMATED SPEND INCURRED" in report
    assert "ACTUALLY SPENT" not in report
    assert "$0.0100" in report  # only model-a's cost counted as incurred
    assert "spend incurred (est.)" in report
    assert "resumed (no new spend)" in report


def test_render_report_no_executed_slugs_is_pure_estimate(corpus: list[bo.CorpusPage]) -> None:
    runs = [_full_run("model-a", bo.Outcome.COR)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "NOT yet spent" in report
    assert "ESTIMATED SPEND INCURRED" not in report
    assert "ACTUALLY SPENT" not in report


def test_render_report_shows_actual_vs_estimated_spend(corpus: list[bo.CorpusPage]) -> None:
    """The cost table's "usage-priced USD (list price)" column (#601 fold round 1,
    slice B) coexists with the unchanged estimate column/label -- "n/a" for an arm
    with no captured usage data, the real figure otherwise."""
    slug_a, slug_b = bo.MODEL_ROSTER[0].slug, bo.MODEL_ROSTER[1].slug
    runs = [_full_run(slug_a, bo.Outcome.COR), _full_run(slug_b, bo.Outcome.INC)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:2]),
        executed_slugs=[slug_a],
        actual_costs={slug_a: 0.0042},
    )
    assert "usage-priced USD (list price)" in report
    assert "$0.0042" in report
    cost_section = report.split("## Cost", 1)[1]
    assert f"| `{slug_b}` |" in cost_section
    remainder = cost_section.split(f"| `{slug_b}` |", 1)[1]
    assert "n/a" in remainder  # slug_b has no captured actual cost


def test_render_report_discloses_reserved_component_of_actuals(
    corpus: list[bo.CorpusPage],
) -> None:
    """#601 fold round 13, F3: an arm whose actual includes a reserve
    component (a timeout/provider-error page) must disclose it -- the
    headline "usage-priced" column stops implying pure captured usage."""
    slug_a = bo.MODEL_ROSTER[0].slug
    runs = [_full_run(slug_a, bo.Outcome.COR)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]),
        executed_slugs=[slug_a],
        actual_costs={slug_a: 0.0042},
        reserved_costs={slug_a: 0.0010},
    )
    assert "of which reserved" in report
    assert "$0.0010" in report


def test_render_report_pure_captured_shows_zero_reserved(
    corpus: list[bo.CorpusPage],
) -> None:
    """An arm with no reserved pages shows an explicit ``$0.0000``, never
    "n/a" (which is reserved for an arm with no actual cost at all)."""
    slug_a = bo.MODEL_ROSTER[0].slug
    runs = [_full_run(slug_a, bo.Outcome.COR)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]),
        executed_slugs=[slug_a],
        actual_costs={slug_a: 0.0042},
        reserved_costs={slug_a: 0.0},
    )
    cost_section = report.split("## Cost", 1)[1]
    assert f"| `{slug_a}` |" in cost_section
    row = cost_section.split(f"| `{slug_a}` |", 1)[1].splitlines()[0]
    assert "$0.0000" in row


def test_render_report_renders_ledger_only_prior_lineage_arm(
    corpus: list[bo.CorpusPage],
) -> None:
    """#601 fold round 13, F4: an arm present in the current-lineage ledger
    but absent from THIS invocation's ``cost_estimates`` (a prior
    ``--models`` subset) still gets its own report row, marked
    prior-lineage, and still counts in the actual total -- no more
    tripped-breaker-with-$0-actuals."""
    slug_a = bo.MODEL_ROSTER[0].slug
    runs = [_full_run(slug_a, bo.Outcome.COR)]
    report = bo.render_report(
        runs,
        bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]),
        executed_slugs=[slug_a],
        actual_costs={slug_a: 0.0042, "prior-model": 0.0500},
        reserved_costs={slug_a: 0.0, "prior-model": 0.0},
    )
    assert "| `prior-model` |" in report
    assert "prior-lineage-arm" in report
    cost_section = report.split("## Cost", 1)[1]
    total_row = next(line for line in cost_section.splitlines() if "arm total" in line)
    assert "$0.0542" in total_row  # 0.0042 + 0.0500, the prior arm counted


def test_render_report_prorates_mid_arm_incurred_estimate(
    corpus: list[bo.CorpusPage],
) -> None:
    """#601 fold round 14, F3: a mid-arm breaker trip must prorate that arm's
    ESTIMATED SPEND INCURRED contribution by ``prorated_attempted_estimate``
    (COST-weighted), not count the full-corpus estimate for pages never
    attempted."""
    slug_a = bo.MODEL_ROSTER[0].slug
    runs = [_full_run(slug_a, bo.Outcome.COR)]
    cost_estimates = [
        bo.ModelCostEstimate(slug=slug_a, input_tokens=900, output_tokens=90, usd=0.09),
    ]
    report = bo.render_report(
        runs,
        cost_estimates,
        executed_slugs=[slug_a],
        prorated_arm=slug_a,
        prorated_attempted_pages=1,
        prorated_total_pages=9,
        prorated_attempted_estimate=0.01,
    )
    assert "PRORATED" in report
    assert "$0.0100" in report  # the cost-weighted figure, not the full $0.0900


def test_render_report_reasoning_arm_comparison_groups_by_model(
    corpus: list[bo.CorpusPage],
) -> None:
    """When both the "off" and "light" arms of a model were scored, the report groups
    them into a per-model comparison section (#601 scope item 5, fold round 1: off vs
    light, never default vs light)."""
    runs = [
        _full_run("model-a+reasoning-off", bo.Outcome.COR),
        _full_run("model-a+reasoning-light", bo.Outcome.PAR),
        _full_run("model-b", bo.Outcome.INC),  # a lone "default" arm -- must not appear below
    ]
    roster = [bo.RosterModel("model-a", 0.1, 0.1, "x"), bo.RosterModel("model-b", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["model-a"], "both") + bo.expand_arms(["model-b"], "default")
    report = bo.render_report(runs, bo.estimate_cost_for_arms(corpus, arms, roster))
    assert "Reasoning-arm comparison (off vs light" in report
    after_heading = report.split("Reasoning-arm comparison", 1)[1]
    section = after_heading.split("\n## ", 1)[0]  # up to the NEXT top-level heading only
    assert "`model-a`" in section
    assert "`model-b`" not in section  # only a paired model is compared


def test_render_report_reasoning_arm_comparison_absent_for_default_only_runs(
    corpus: list[bo.CorpusPage],
) -> None:
    """The ``--reasoning default`` CLI default (no off/light sibling ever scored) must
    not add the comparison section at all -- report shape stays exactly the pre-#601
    shape."""
    runs = [_full_run("model-a", bo.Outcome.COR), _full_run("model-b", bo.Outcome.INC)]
    report = bo.render_report(runs, bo.estimate_cost(corpus, bo.MODEL_ROSTER[:2]))
    assert "Reasoning-arm comparison" not in report


def test_render_report_reasoning_arm_comparison_separates_schema_and_other_errors(
    corpus: list[bo.CorpusPage],
) -> None:
    """The off-vs-light comparison must show 'schema failures' and 'other errors' as
    separate columns, never one merged 'page errors' count (#601 fold round 1, P2)."""

    def _run_with_errors(slug: str, errors: list[str | None]) -> bo.ModelRun:
        # Every field is populated (mirroring how score_page fills a whole-page
        # failure) so render_report's per-page outcomes table can index every
        # FIELD_SPECS cell without a KeyError.
        fields = {spec.name: bo.Outcome.ERR for spec in bo.FIELD_SPECS}
        pages = [
            bo.PageResult(slug=f"p{i}", outcomes=dict(fields), error=err, on_page_fields=0)
            for i, err in enumerate(errors)
        ]
        return bo.ModelRun(model_slug=slug, pages=pages)

    off_run = _run_with_errors(
        "model-a+reasoning-off",
        ["BeanExtractionUnavailableError: bean identity extraction returned a malformed shape: x"],
    )
    light_run = _run_with_errors(
        "model-a+reasoning-light",
        ["BeanExtractionUnavailableError: bean identity extraction exceeded the 45s deadline"],
    )
    roster = [bo.RosterModel("model-a", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["model-a"], "both")
    report = bo.render_report([off_run, light_run], bo.estimate_cost_for_arms(corpus, arms, roster))
    assert "schema F/recovered R (off -> light)" in report
    assert "other errors (off -> light)" in report
    assert "1/0 -> 0/0" in report  # off's schema failure (0 recovered) -> light has none
    assert "0 -> 1" in report  # light's timeout (other error) -> off has none


def test_run_json_roundtrips_outcomes() -> None:
    run = _run("m", {"p": {"origin": bo.Outcome.COR, "process": bo.Outcome.SPU}})
    rebuilt = bo._run_from_checkpoint(bo.run_to_json(run))  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.model_slug == "m"
    assert rebuilt.pages[0].outcomes == {
        "origin": bo.Outcome.COR,
        "process": bo.Outcome.SPU,
    }


def test_run_json_roundtrips_extracted_values() -> None:
    """The drafted values must survive the checkpoint/artifact round trip, so
    a wrong label/match-function can be audited or rescored offline without
    re-calling the (paid) model (#600 finding)."""
    page = bo.PageResult(
        slug="p",
        outcomes={"origin": bo.Outcome.COR},
        error=None,
        on_page_fields=1,
        extracted={"name": "X", "country": "Ecuador"},
    )
    run = bo.ModelRun(model_slug="m", pages=[page])
    rebuilt = bo._run_from_checkpoint(bo.run_to_json(run))  # pyright: ignore[reportPrivateUsage]
    assert rebuilt.pages[0].extracted == {"name": "X", "country": "Ecuador"}


@pytest.mark.asyncio
async def test_run_model_over_corpus_persists_extracted_draft(
    corpus: list[bo.CorpusPage],
) -> None:
    page = _page(corpus, "cbc-costa-rica")
    model = _model_returning({"name": "Costa Rica La Minita", "country": "Costa Rica"})
    run = await bo.run_model_over_corpus(
        [page], model_slug="m", advisor_config=_ADVISOR_CONFIG, model=model
    )
    extracted = run.pages[0].extracted
    assert extracted is not None
    assert extracted["name"] == "Costa Rica La Minita"
    # #612: the full draft dump rides along, so field_sources/field_evidence
    # need no separate harness-side projection -- both keys are present
    # (empty here since the double supplied no *_evidence args).
    assert "field_sources" in extracted
    assert "field_evidence" in extracted
    # a failed page persists no extracted draft.
    failed = await bo.run_model_over_corpus(
        [page], model_slug="m", advisor_config=_ADVISOR_CONFIG, model=_model_text_only()
    )
    assert failed.pages[0].extracted is None


# --- Evidence-quote capture summary (#612) -----------------------------------


def test_draft_model_dump_carries_field_sources_and_evidence_verbatim() -> None:
    """``BeanProfileDraft.model_dump`` already carries ``field_sources``/
    ``field_evidence`` (#627/#633) -- the harness's ``PageResult.extracted``
    is the whole draft dump, so #612 needs no separate projection."""
    draft = BeanProfileDraft(
        name="X",
        bean_origin="Y",
        initial_heat_percent=100,
        initial_fan_percent=30,
        target_drop_temp_c=194.0,
        target_development_percent=14.0,
        default_bean_weight_grams=250.0,
        scouting_note="scouting",
        altitude_m=1400,
        field_sources={"altitude_m": "on_page", "processing": "origin_estimated"},
        field_evidence={"altitude_m": "grown at 1,400 metres"},
    )
    dumped = draft.model_dump(mode="json")
    assert dumped["field_sources"] == {"altitude_m": "on_page", "processing": "origin_estimated"}
    assert dumped["field_evidence"] == {"altitude_m": "grown at 1,400 metres"}


def _evidence_page(
    slug: str,
    *,
    field_sources: dict[str, str] | None = None,
    field_evidence: dict[str, str] | None = None,
    has_draft: bool = True,
) -> bo.PageResult:
    extracted = (
        None
        if not has_draft
        else {"field_sources": field_sources or {}, "field_evidence": field_evidence or {}}
    )
    return bo.PageResult(
        slug=slug,
        outcomes={},
        error=None if has_draft else "boom",
        on_page_fields=0,
        extracted=extracted,
    )


def test_evidence_summary_counts_captured_and_missing_per_typed_field() -> None:
    run = bo.ModelRun(
        model_slug="m",
        pages=[
            _evidence_page(
                "a",
                field_sources={
                    "altitude_m": "on_page",
                    "name": "on_page",
                    "processing": "origin_estimated",
                },
                field_evidence={"altitude_m": "grown at 1400m"},
            ),
            _evidence_page(
                "b", field_sources={"altitude_m": "origin_estimated"}, field_evidence={}
            ),
        ],
    )
    summary = bo.evidence_summary(run)
    assert summary.model_slug == "m"
    assert summary.pages_scored == 2
    by_field = {f.field_name: f for f in summary.typed_fields}
    assert by_field["altitude_m"].captured == 1
    assert by_field["altitude_m"].no_evidence == 1
    assert by_field["processing"].captured == 0
    assert by_field["processing"].no_evidence == 2
    assert by_field["bean_species"].captured == 0
    assert by_field["bean_species"].no_evidence == 2
    assert by_field["is_blend"].captured == 0
    assert by_field["is_blend"].no_evidence == 2
    # 2 "on_page" of 4 total field_sources entries across both pages (page a:
    # 2 on_page of 3 entries; page b: 0 on_page of 1 entry) -> 2/4 = 0.5.
    assert summary.on_page_rate == pytest.approx(0.5)


def test_evidence_summary_empty_maps_yield_none_on_page_rate() -> None:
    run = bo.ModelRun(
        model_slug="m", pages=[_evidence_page("a", field_sources={}, field_evidence={})]
    )
    summary = bo.evidence_summary(run)
    assert summary.pages_scored == 1
    assert all(f.captured == 0 and f.no_evidence == 1 for f in summary.typed_fields)
    assert summary.on_page_rate is None


def test_evidence_summary_skips_pages_with_no_draft() -> None:
    run = bo.ModelRun(
        model_slug="m",
        pages=[
            _evidence_page("a", has_draft=False),
            _evidence_page(
                "b", field_sources={"altitude_m": "on_page"}, field_evidence={"altitude_m": "q"}
            ),
        ],
    )
    summary = bo.evidence_summary(run)
    assert summary.pages_scored == 1
    by_field = {f.field_name: f for f in summary.typed_fields}
    assert by_field["altitude_m"].captured == 1
    assert by_field["altitude_m"].no_evidence == 0


def test_render_report_includes_evidence_capture_section(corpus: list[bo.CorpusPage]) -> None:
    fields = {spec.name: bo.Outcome.COR for spec in bo.FIELD_SPECS}
    page = bo.PageResult(
        slug="page-a",
        outcomes=fields,
        error=None,
        on_page_fields=1,
        extracted={
            "field_sources": {"altitude_m": "on_page"},
            "field_evidence": {"altitude_m": "grown at 1400m"},
        },
    )
    run = bo.ModelRun(model_slug="model-a", pages=[page])
    report = bo.render_report([run], bo.estimate_cost(corpus, bo.MODEL_ROSTER[:1]))
    assert "Evidence-quote capture" in report
    assert "NOT certification" in report
    assert "altitude_m" in report


def test_run_to_json_includes_evidence_summary() -> None:
    run = bo.ModelRun(
        model_slug="m",
        pages=[
            _evidence_page(
                "a", field_sources={"altitude_m": "on_page"}, field_evidence={"altitude_m": "q"}
            )
        ],
    )
    payload = bo.run_to_json(run)
    assert payload["evidence_summary"]["model_slug"] == "m"
    assert payload["evidence_summary"]["pages_scored"] == 1
    typed = {f["field_name"]: f for f in payload["evidence_summary"]["typed_fields"]}
    assert typed["altitude_m"]["captured"] == 1
    assert typed["altitude_m"]["no_evidence"] == 0


def test_checkpoint_appends_and_resumes(tmp_path: Path) -> None:
    path = tmp_path / "cells.jsonl"
    first = bo.Checkpoint(path, resume=False)
    first.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    reopened = bo.Checkpoint(path, resume=True)
    assert reopened.has("m1")
    assert reopened.get("m1")["model_slug"] == "m1"
    assert not bo.Checkpoint(path, resume=False).has("m1")  # truncated


def test_checkpoint_recovers_earlier_records_after_truncated_final_line(
    tmp_path: Path,
) -> None:
    """A kill mid-``write`` can leave the LAST appended line incomplete; the
    earlier, complete, already-paid-for records must still be recoverable
    (#600 finding)."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    checkpoint.append(bo.run_to_json(_run("m2", {"p": {"origin": bo.Outcome.COR}})))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"model_slug": "m3", "pages": [truncated')  # no trailing newline
    reopened = bo.Checkpoint(path, resume=True)
    assert reopened.has("m1")
    assert reopened.has("m2")
    assert not reopened.has("m3")
    # The truncated tail must be REMOVED from disk, not merely skipped in
    # memory -- otherwise the next append concatenates a new record directly
    # onto it (no trailing newline) and a later resume raises (#602 finding).
    assert "truncated" not in path.read_text()
    reopened.append(bo.run_to_json(_run("m4", {"p": {"origin": bo.Outcome.COR}})))
    resumed_again = bo.Checkpoint(path, resume=True)
    assert resumed_again.has("m1")
    assert resumed_again.has("m2")
    assert resumed_again.has("m4")
    assert not resumed_again.has("m3")


def test_atomic_write_text_replaces_content_and_leaves_no_temp_file(tmp_path: Path) -> None:
    """``_atomic_write_text`` fully replaces the destination's content via a
    same-directory temp file + ``os.replace`` -- never a bare truncate-then-
    write in place (#602 fold 4)."""
    path = tmp_path / "cells.jsonl"
    path.write_text("old content\n")
    bo._atomic_write_text(path, "new content\n")  # pyright: ignore[reportPrivateUsage]
    assert path.read_text() == "new content\n"
    assert list(tmp_path.glob("*.tmp")) == []  # the temp file is renamed away, not left behind


def test_checkpoint_tail_repair_goes_through_atomic_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The truncated-tail repair goes through ``_atomic_write_text`` (#602
    fold 4), not a direct ``path.write_text`` -- a non-atomic truncate-then-
    write would risk destroying the just-recovered records on a SECOND crash
    mid-repair, exactly the failure class this repair exists to prevent."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"model_slug": "m2", "pages": [truncated')  # no trailing newline

    calls: list[tuple[Path, str]] = []
    original = bo._atomic_write_text  # pyright: ignore[reportPrivateUsage]

    def spy(target: Path, content: str) -> None:
        calls.append((target, content))
        original(target, content)

    monkeypatch.setattr(bo, "_atomic_write_text", spy)
    reopened = bo.Checkpoint(path, resume=True)
    assert reopened.has("m1")
    assert len(calls) == 1
    repaired_path, repaired_content = calls[0]
    assert repaired_path == path
    assert repaired_content == path.read_text()  # exactly what's on disk now


def test_checkpoint_raises_on_malformed_interior_line(tmp_path: Path) -> None:
    """A malformed line that is NOT the final one is real corruption, not an
    interrupted append, and must still raise."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    m2_record = json.dumps(bo.run_to_json(_run("m2", {"p": {"origin": bo.Outcome.COR}})))
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
        handle.write(m2_record + "\n")
    with pytest.raises(json.JSONDecodeError):
        bo.Checkpoint(path, resume=True)


def test_checkpoint_raises_on_a_newline_terminated_malformed_final_line(
    tmp_path: Path,
) -> None:
    """A malformed FINAL line that DOES end with a newline cannot be an
    interrupted in-flight append -- that write completed, so the broken
    JSON is real corruption (a manual edit / a bug), not a mid-write kill.
    It must raise for manual recovery, NOT be silently auto-repaired away
    (#602 fold round 4, FOLD 4)."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"model_slug": "m2", "pages": [corrupted}\n')  # WITH a trailing newline
    with pytest.raises(json.JSONDecodeError):
        bo.Checkpoint(path, resume=True)
    # Not auto-repaired: the corrupted bytes must still be on disk, untouched,
    # for manual recovery.
    assert "corrupted" in path.read_text()


def test_checkpoint_normalises_a_valid_newline_less_tail(tmp_path: Path) -> None:
    """A COMPLETE, valid JSON final line missing only its trailing newline parses fine, so
    the truncated-tail repair never fires -- yet the next append would concatenate directly
    onto it with no separator, corrupting the JSONL. The loader must normalise this
    newline-less-but-valid tail atomically before any further append (#602 fold round 6,
    FOLD 3)."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    checkpoint.append(bo.run_to_json(_run("m2", {"p": {"origin": bo.Outcome.COR}})))
    # Simulate a kill that wrote the complete final JSON payload but not its
    # trailing newline.
    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    path.write_bytes(raw[:-1])

    reopened = bo.Checkpoint(path, resume=True)
    assert reopened.has("m1")
    assert reopened.has("m2")
    assert path.read_text().endswith("\n")  # normalised BEFORE any further append

    reopened.append(bo.run_to_json(_run("m3", {"p": {"origin": bo.Outcome.COR}})))
    # The new record must be on its OWN line, not concatenated -- a resume
    # must read all three records as valid JSONL.
    resumed_again = bo.Checkpoint(path, resume=True)
    assert resumed_again.has("m1")
    assert resumed_again.has("m2")
    assert resumed_again.has("m3")


def test_checkpoint_ignores_stale_fingerprinted_records(tmp_path: Path) -> None:
    path = tmp_path / "cells.jsonl"
    written = bo.Checkpoint(path, resume=False, fingerprint="fp-a")
    written.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    same_fp = bo.Checkpoint(path, resume=True, fingerprint="fp-a")
    assert same_fp.has("m1")
    different_fp = bo.Checkpoint(path, resume=True, fingerprint="fp-b")
    assert not different_fp.has("m1")
    # an empty fingerprint disables the guard entirely (back-compat / tests
    # that do not care about staleness).
    no_guard = bo.Checkpoint(path, resume=True)
    assert no_guard.has("m1")


def test_checkpoint_supersedes_earlier_record_for_the_same_arm(tmp_path: Path) -> None:
    """#649: appending a SECOND record for the same ``model_slug`` supersedes
    the first (latest wins) -- the sidecar is append-only, never rewritten in
    place, mirroring :meth:`bo.ChargeLedger._effective_entries`'s supersession
    pattern. This is what makes the residual-retry merge coherent."""
    path = tmp_path / "cells.jsonl"
    checkpoint = bo.Checkpoint(path, resume=False)
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.INC}})))
    checkpoint.append(bo.run_to_json(_run("m1", {"p": {"origin": bo.Outcome.COR}})))
    assert checkpoint.get("m1")["pages"][0]["outcomes"]["origin"] == "COR"  # the LATEST record

    reopened = bo.Checkpoint(path, resume=True)
    assert (
        reopened.get("m1")["pages"][0]["outcomes"]["origin"] == "COR"
    )  # latest wins on reload too


def test_retryable_slugs_only_includes_flagged_pages() -> None:
    run = bo.ModelRun(
        model_slug="m1",
        pages=[
            bo.PageResult(slug="a", outcomes={}, error=None, on_page_fields=1),
            bo.PageResult(slug="b", outcomes={}, error="boom", on_page_fields=0, retryable=True),
        ],
    )
    assert bo._retryable_slugs(run) == {"b"}  # pyright: ignore[reportPrivateUsage]


def test_merge_retry_results_replaces_only_matching_slugs_preserving_order() -> None:
    """#649: a fresh entry replaces its matching slug; a slug the retry never
    reached (the meter tripped mid-retry) keeps its prior entry, still
    retryable, ready for a later resume -- corpus order preserved throughout."""
    existing = bo.ModelRun(
        model_slug="m1",
        pages=[
            bo.PageResult(slug="a", outcomes={}, error=None, on_page_fields=1),
            bo.PageResult(slug="b", outcomes={}, error="boom", on_page_fields=0, retryable=True),
            bo.PageResult(slug="c", outcomes={}, error="boom", on_page_fields=0, retryable=True),
        ],
    )
    fresh = bo.ModelRun(
        model_slug="m1",
        pages=[bo.PageResult(slug="b", outcomes={}, error=None, on_page_fields=1)],
    )
    merged = bo._merge_retry_results(existing, fresh)  # pyright: ignore[reportPrivateUsage]
    assert [p.slug for p in merged.pages] == ["a", "b", "c"]  # corpus order preserved
    assert merged.pages[1].error is None  # "b" resolved by the retry
    assert merged.pages[2].retryable is True  # "c" untouched -- never reached by this retry


@pytest.mark.asyncio
async def test_run_bakeoff_budget_stop_makes_no_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A tiny but NONZERO --max-spend (#601 fold round 13: the meter is
    checked FIRST, so a bare ``0.0`` would trip via the meter -- charged
    ``0.0 >= max_spend 0.0`` -- before ever reaching the estimate guard this
    test targets) exercises the PURE pre-run estimate guard: a fresh ledger's
    meter (``charged=0.0``) is not yet tripped, but the arm's own real
    estimate alone exceeds the budget."""
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    estimates = bo.estimate_cost(corpus, roster)
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=tmp_path / "o.json",
        resume=False,
        max_spend=0.001,  # first model's estimate (~$0.025) > this -> stop, meter not tripped
        cost_estimates=estimates,
        roster=roster,
    )
    assert result.runs == []
    assert result.stopped_early is True
    assert result.unevaluated_slugs == ["m1"]
    assert result.executed_slugs == []
    assert result.failed_slugs == []
    assert result.breaker_tripped is False  # the ESTIMATE guard, not the meter


@pytest.mark.asyncio
async def test_run_bakeoff_resumes_without_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    # Full page coverage -- #601 fold round 2, FOLD 4 + fold round 5, D FOLD 3:
    # a checkpoint with no ledger at all refuses (see
    # test_run_bakeoff_refuses_a_pre_ledger_checkpoint); a checkpoint whose
    # ledger is missing any of ITS OWN pages refuses too.
    seed_ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("m1", ["page-a", "page-b"]):
        seed_ledger.append(entry)
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
    )
    assert [r.model_slug for r in result.runs] == ["m1"]
    assert result.stopped_early is False
    assert result.unevaluated_slugs == []
    assert result.executed_slugs == []  # resumed, not newly called
    assert result.failed_slugs == []


# --- #649: page-level residual retry -----------------------------------------


@pytest.mark.asyncio
async def test_run_bakeoff_resume_retries_only_the_transient_page_and_merges(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#649: a transient infra failure on ONE page of an otherwise-successful
    arm is checkpointed with that page marked retryable; ``--resume`` retries
    ONLY that page and merges the fresh result into the record -- metrics
    recomputed, the arm no longer labelled retryable once resolved."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    arms = bo.expand_arms(["m1"], "default")
    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, roster)

    failing_call = 2  # the 2nd page processed fails, the rest succeed
    first = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_fails_nth_call(failing_call),
    )
    assert first.executed_slugs == ["m1"]
    first_run = first.runs[0]
    failed_page = corpus[failing_call - 1]
    first_entry = next(p for p in first_run.pages if p.slug == failed_page.slug)
    assert first_entry.error is not None
    assert first_entry.retryable is True
    first_metrics = bo.model_metrics(first_run)
    assert first_metrics.page_errors == 1

    resumed = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert resumed.executed_slugs == ["m1"]  # a real (retry) call was made
    resumed_run = resumed.runs[0]
    assert all(p.error is None for p in resumed_run.pages)
    assert all(not p.retryable for p in resumed_run.pages)
    resumed_metrics = bo.model_metrics(resumed_run)
    assert resumed_metrics.page_errors == 0  # metrics recomputed from the merged set

    # The supersede-aware Checkpoint sidecar reflects the merged record too.
    reloaded_checkpoint = bo.Checkpoint(
        bo.sidecar_path(out), resume=True, fingerprint=bo.compute_fingerprint(corpus)
    )
    reloaded_run = bo._run_from_checkpoint(  # pyright: ignore[reportPrivateUsage]
        reloaded_checkpoint.get("m1")
    )
    assert all(p.error is None for p in reloaded_run.pages)


@pytest.mark.asyncio
async def test_run_bakeoff_ledger_charges_accumulate_across_a_residual_retry(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#649: money is untouched by the retry design -- the retried page is a
    genuinely NEW call, charged again, on top of the failed attempt's own
    charge (never a free retry, never a double-count either)."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    arms = bo.expand_arms(["m1"], "default")
    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, roster)

    await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_fails_nth_call(1),
    )
    fingerprint = bo.compute_fingerprint(corpus)
    charge_after_failure = bo.ChargeLedger(
        bo.ledger_path(out), fingerprint=fingerprint
    ).total_usd_for_arm("m1")
    assert charge_after_failure > 0.0  # the failed attempt's reserve is already charged

    await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    charge_after_retry = bo.ChargeLedger(
        bo.ledger_path(out), fingerprint=fingerprint
    ).total_usd_for_arm("m1")
    assert charge_after_retry > charge_after_failure  # the retry ADDS a new charge


@pytest.mark.asyncio
async def test_run_bakeoff_resume_idempotent_once_nothing_retryable_remains(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#649: once a residual retry resolves every retryable page, a THIRD
    resume makes NO new call at all -- the arm is skipped wholesale, same as
    any other fully-resolved checkpointed arm."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    arms = bo.expand_arms(["m1"], "default")
    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, roster)

    await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_fails_nth_call(1),
    )
    await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )

    def _never_called(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("model must not be called -- nothing retryable remains")

    third = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=roster,
        model=FunctionModel(_never_called),
    )
    assert third.executed_slugs == []
    assert all(not p.retryable for p in third.runs[0].pages)


@pytest.mark.asyncio
async def test_run_bakeoff_breaker_trip_mid_retry_reports_honestly(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#652: a meter trip DURING a residual retry must report breaker_tripped
    (exit 3), never a false stopped_early=False/exit 0 from falling through to
    the next arm (or simply running out of arms) -- the SAME trip semantics a
    fresh arm's mid-arm trip already gets. The untouched (never-reached)
    retryable page stays retryable for the next resume -- the merge already
    guarantees this; pinned here too."""
    out = tmp_path / "o.json"
    cheap_roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1"], "default")
    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, cheap_roster)

    # Two transient failures (pages 1 and 2, corpus order) inside an
    # otherwise-clean run, at a CHEAP real price -- sets up a checkpoint with
    # two retryable pages without tripping anything.
    await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=10.0,
        cost_estimates=cost_estimates,
        roster=cheap_roster,
        model=_model_fails_calls({1, 2}),
    )
    fingerprint = bo.compute_fingerprint(corpus)
    charged_after_setup = bo.ChargeLedger(
        bo.ledger_path(out), fingerprint=fingerprint
    ).total_usd_for_arm("m1")

    # Resume: an ABSURD real price so retrying the FIRST retryable page alone
    # trips the meter -- the retry's own internal loop must then break BEFORE
    # ever reaching the second retryable page.
    absurd_roster = [bo.RosterModel("m1", 1_000_000, 1_000_000, "x")]
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=charged_after_setup + 0.01,  # tiny headroom -- one absurd charge blows past it
        cost_estimates=cost_estimates,
        roster=absurd_roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.breaker_tripped is True
    assert result.stopped_early is True
    assert result.executed_slugs == ["m1"]  # the retry WAS attempted this invocation
    merged_run = result.runs[0]
    still_retryable = bo._retryable_slugs(merged_run)  # pyright: ignore[reportPrivateUsage]
    assert len(still_retryable) == 1  # the second retryable page was never reached

    # The untouched page is still retryable on disk too -- a later resume
    # would pick it up (the merge/checkpoint-supersede guarantee).
    reloaded = bo._run_from_checkpoint(  # pyright: ignore[reportPrivateUsage]
        bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint).get("m1")
    )
    assert bo._retryable_slugs(reloaded) == still_retryable  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_run_bakeoff_refuses_a_pre_ledger_checkpoint(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A checkpoint with non-stale records but NO ledger sidecar (#601 fold round
    2, FOLD 4) predates the charge ledger's existence -- its spend cannot be
    accounted for, so resuming it must refuse rather than silently initialise an
    empty budget meter and skip already-paid arms forever. Names both fixes in
    the message."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    assert not bo.ledger_path(out).exists()  # no ledger seeded -- the pre-ledger scenario

    with pytest.raises(ValueError, match=r"--no-resume.*--out elsewhere"):
        await bo.run_bakeoff(
            corpus,
            bo.expand_arms(["m1"], "default"),
            out=out,
            resume=True,
            max_spend=1000.0,
            cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
            roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        )


async def _refuses_naming(
    corpus: list[bo.CorpusPage], out: Path, arm_labels: list[str], expect_named: str
) -> None:
    """Shared assertion for the coverage-based refusal's per-arm scenarios (#601
    fold round 4, FOLD 2): ``run_bakeoff`` must refuse, naming ``expect_named``."""
    roster = [bo.RosterModel(a, 0.1, 0.1, "x") for a in arm_labels]
    with pytest.raises(ValueError, match=rf"{expect_named}.*--no-resume.*--out elsewhere"):
        await bo.run_bakeoff(
            corpus,
            bo.expand_arms(arm_labels, "default"),
            out=out,
            resume=True,
            max_spend=1000.0,
            cost_estimates=bo.estimate_cost(corpus, roster),
            roster=roster,
        )


@pytest.mark.asyncio
async def test_run_bakeoff_refuses_a_legacy_only_ledger(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 4, FOLD 2: EXISTENCE isn't COVERAGE -- a ledger with an
    entry for "m1" but NO fingerprint (a genuinely pre-fold-4 record) does not
    cover the checkpointed arm; must refuse the same as no ledger at all."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    legacy_record = dataclasses.asdict(dataclasses.replace(_seeded_ledger_entry(), arm="m1"))
    del legacy_record["fingerprint"]
    bo.ledger_path(out).write_text(json.dumps(legacy_record) + "\n")

    await _refuses_naming(corpus, out, ["m1"], "m1")


@pytest.mark.asyncio
async def test_run_bakeoff_refuses_a_foreign_fingerprint_ledger(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A ledger entry for "m1" under a DIFFERENT (foreign) fingerprint does not
    cover THIS invocation's checkpointed "m1" -- must refuse."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    foreign = bo.ChargeLedger(bo.ledger_path(out), fingerprint="fp-foreign")
    foreign.append(dataclasses.replace(_seeded_ledger_entry(), arm="m1"))

    await _refuses_naming(corpus, out, ["m1"], "m1")


def _page_covering_entries(arm: str, slugs: list[str]) -> list[bo.LedgerEntry]:
    """One synthetic FINAL ledger entry per ``slugs`` member, for seeding
    page-level coverage fixtures (#601 fold round 5, D FOLD 3). Each gets its
    OWN ``call_id`` (#601 qa pass, round 8) -- the shared default ``""``
    would collide two entries under one key in
    :meth:`bo.ChargeLedger._effective_entries` if a future call site ever
    asserted ``total_usd()`` against these fixtures too."""
    return [
        dataclasses.replace(_seeded_ledger_entry(), arm=arm, slug=slug, call_id=slug)
        for slug in slugs
    ]


@pytest.mark.asyncio
async def test_run_bakeoff_refuses_naming_only_the_uncovered_arm(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """Per-arm coverage: "m1" has a ledger entry for EVERY one of its
    checkpointed pages, "m2" has none -- the refusal must name ONLY "m2", not
    the already-fully-covered "m1"."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    checkpoint.append(bo.run_to_json(_full_run("m2", bo.Outcome.COR)))
    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("m1", ["page-a", "page-b"]):  # "m2" left uncovered
        ledger.append(entry)

    await _refuses_naming(corpus, out, ["m1", "m2"], "m2")


@pytest.mark.asyncio
async def test_run_bakeoff_refuses_an_arm_with_partial_page_coverage(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 5, D FOLD 3: a checkpointed TWO-page arm with a ledger
    entry for only ONE of its pages must still refuse -- per-ARM existence
    (the round 4 check) would have wrongly passed this; page-level coverage
    catches the tail-truncated-ledger case it cannot."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("m1", ["page-a"]):  # "page-b" never landed
        ledger.append(entry)

    await _refuses_naming(corpus, out, ["m1"], "m1")


@pytest.mark.asyncio
async def test_run_bakeoff_full_page_coverage_is_unaffected(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 5, D FOLD 3: a checkpointed arm with a ledger entry for
    EVERY one of its pages resumes normally -- the guard never fires against a
    genuinely complete arm."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("m1", ["page-a", "page-b"]):
        ledger.append(entry)

    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, roster),
        roster=roster,
    )
    assert len(result.runs) == 1  # resumed from checkpoint, not refused


@pytest.mark.asyncio
async def test_run_bakeoff_fresh_run_with_neither_file_is_unaffected(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A brand-new ``--out`` (neither checkpoint nor ledger exists) is the
    ordinary case -- #601 fold round 2, FOLD 4's guard must never fire for it."""
    out = tmp_path / "o.json"
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.runs[0].model_slug == "m1"


@pytest.mark.asyncio
async def test_main_refuses_a_pre_ledger_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``main()`` surfaces the pre-ledger-checkpoint refusal (#601 fold round 2,
    FOLD 4) as exit code 2, matching every other pre-spend REFUSED guard."""

    async def _refusing_run_bakeoff(*args: object, **kwargs: object) -> bo.BakeoffResult:
        raise ValueError(
            "o.json.cells.jsonl predates the charge ledger (#601) -- its spend cannot "
            "be accounted for. Rerun with --no-resume (fresh books, fresh budget) or "
            "point --out elsewhere."
        )

    monkeypatch.setattr(bo, "run_bakeoff", _refusing_run_bakeoff)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    out = tmp_path / "out.json"
    code = await bo.main(
        [
            "--models",
            bo.MODEL_ROSTER[0].slug,
            "--max-spend",
            "5",
            "--out",
            str(out),
        ]
    )
    assert code == 2
    assert not out.exists()


@pytest.mark.asyncio
async def test_run_bakeoff_meter_reconstructs_from_ledger_no_double_charge(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """The :class:`bo.SpendMeter` is seeded from the persistent :class:`bo.ChargeLedger`
    (#601 fold round 1, slice B) BEFORE the main loop -- NEVER from scored/checkpointed
    runs -- so a resumed invocation never resets it or double-charges completed work.
    "m1" is checkpointed (so it resumes without a new call) and its LEDGER already
    carries a HUGE charge (synthetic entries, no heuristic guessing); "m2" then never
    starts because the reconstructed meter is already at --max-spend."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x"), bo.RosterModel("m2", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1", "m2"], "default")
    fingerprint = bo.compute_fingerprint(corpus)

    # "m1" is scored + checkpointed (a trivial run) -- its DOLLAR figure comes from
    # the LEDGER, not from PageResult (which carries no token fields, #601 slice A).
    seed_pages = [
        bo.PageResult(slug=p.slug, outcomes={}, error=None, on_page_fields=0) for p in corpus
    ]
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(bo.ModelRun(model_slug="m1", pages=seed_pages)))

    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for page in corpus:
        ledger.append(
            bo.LedgerEntry(
                arm="m1",
                slug=page.slug,
                request_tokens=1_000_000,
                response_tokens=1_000_000,
                priced_usd=0.2,  # (0.1+0.1) $/mtok * 1M tokens each -- huge, well over budget
                timed_out=False,
                reserve_applied=False,
            )
        )

    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, roster)
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=0.5,  # m1's ledgered spend alone (0.2 * len(corpus)) is huge
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert {r.model_slug for r in result.runs} == {"m1"}  # resumed, no new call
    assert result.breaker_tripped is True
    assert result.stopped_early is True
    assert result.unevaluated_slugs == ["m2"]
    assert result.executed_slugs == []  # m1 resumed, m2 never attempted -- no NEW spend
    assert result.actual_costs["m1"] == pytest.approx(0.2 * len(corpus))


@pytest.mark.asyncio
async def test_run_bakeoff_meter_checked_before_estimate_guard_on_resume(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 13: the runtime METER must be checked BEFORE the
    pre-run ESTIMATE guard in the arm loop. On a resumed invocation whose
    ledger already carries real spend past a tiny remaining --max-spend, the
    next arm's OWN naive char-based estimate also exceeds that tiny budget
    -- BOTH guards fire on the same iteration. The estimate guard, checked
    first before this fold, won the race and returned breaker_tripped=False
    (exit 0) even though real money was already over budget."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x"), bo.RosterModel("m2", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1", "m2"], "default")
    fingerprint = bo.compute_fingerprint(corpus)

    seed_pages = [
        bo.PageResult(slug=p.slug, outcomes={}, error=None, on_page_fields=0) for p in corpus
    ]
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(bo.ModelRun(model_slug="m1", pages=seed_pages)))

    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for page in corpus:
        ledger.append(
            bo.LedgerEntry(
                arm="m1",
                slug=page.slug,
                request_tokens=1_000_000,
                response_tokens=1_000_000,
                priced_usd=0.2,
                timed_out=False,
                reserve_applied=False,
            )
        )

    cost_estimates = bo.estimate_cost_for_arms(corpus, arms, roster)
    # Tiny remaining budget: m2's own real corpus-based estimate (well over a
    # cent) ALSO exceeds it, so the estimate guard would independently fire
    # too -- the meter (real spend) must still win the race.
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=0.0001,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.breaker_tripped is True
    assert result.stopped_early is True
    assert result.executed_slugs == []


@pytest.mark.asyncio
async def test_meter_rounding_parity_live_vs_resumed_at_the_boundary(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 13: the meter must charge the SAME rounded amount the
    ledger persists (``round(priced, 5)``) -- charging the raw, unrounded
    float let a LIVE run's meter disagree with a RESUMED run's meter (seeded
    from :meth:`bo.ChargeLedger.total_usd`, which sums the PERSISTED rounded
    entries) at exactly the boundary --max-spend sits on."""
    price = bo.RosterModel("m1", 35.0, 1.0, "x")
    raw = 3 / 1_000_000 * 35.0  # 0.000105
    rounded = round(raw, 5)  # 0.00011 -- rounds UP past raw
    assert rounded > raw  # sanity: the case this test needs to expose

    ledger_path = bo.ledger_path(tmp_path / "o.json")
    live_ledger = bo.ChargeLedger(ledger_path)
    live_meter = bo.SpendMeter(max_spend=rounded)
    await bo.run_model_over_corpus(
        [corpus[0]],
        model_slug="m1",
        advisor_config=_ADVISOR_CONFIG,
        model=_model_returning_with_usage(
            {"name": "X", "country": "Ecuador"}, input_tokens=3, output_tokens=0
        ),
        roster_price=price,
        ledger=live_ledger,
        meter=live_meter,
    )
    assert live_meter.charged == pytest.approx(rounded)
    assert live_meter.tripped is True  # exactly at the boundary

    resumed_ledger = bo.ChargeLedger(ledger_path)  # reloads the same persisted entries
    resumed_meter = bo.SpendMeter(max_spend=rounded, charged=resumed_ledger.total_usd())
    assert resumed_meter.charged == pytest.approx(live_meter.charged)
    assert resumed_meter.tripped == live_meter.tripped  # must AGREE, not coincidentally match


def test_spend_meter_charge_rounds_cumulative_after_every_add() -> None:
    """#601 fold round 14: ``charged`` is rounded to 5dp after EVERY charge,
    not left as a raw float sum -- three additions of $0.00007 in raw binary
    float sum to $0.00020999999999999998 (epsilon UNDER $0.00021), which
    would disagree with a ledger-reconstructed meter (whose ``total_usd()``
    rounds once, at the end, reaching exactly $0.00021) at that exact
    boundary."""
    live = bo.SpendMeter(max_spend=0.00021)
    for _ in range(3):
        live.charge(0.00007)
    assert live.charged == 0.00021  # not 0.00020999999999999998
    assert live.tripped is True

    # A meter reconstructed from a ledger that summed the SAME 3 rounded
    # entries once, at the end (mirroring ChargeLedger.total_usd()), agrees.
    resumed_charged = round(sum([0.00007, 0.00007, 0.00007]), 5)
    resumed = bo.SpendMeter(max_spend=0.00021, charged=resumed_charged)
    assert resumed.charged == live.charged
    assert resumed.tripped == live.tripped


@pytest.mark.asyncio
async def test_run_bakeoff_estimate_guard_forecasts_off_carried_meter_spend(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 14, F4: the pre-arm ESTIMATE guard's forecast base is
    the METER's real cumulative charge (includes RESUMED-ledger spend), not
    a per-invocation-zero accumulator. Resuming with $0.09 already charged, a
    $0.10 limit, and the next arm's own ~$0.09 estimate must refuse BEFORE
    any call (0.09 + 0.09 = 0.18 > 0.10) -- even though the estimate ALONE
    (0.09 <= 0.10) would have let the OLD per-invocation-zero accumulator
    through."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x"), bo.RosterModel("m2", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1", "m2"], "default")
    fingerprint = bo.compute_fingerprint(corpus)

    seed_pages = [
        bo.PageResult(slug=p.slug, outcomes={}, error=None, on_page_fields=0) for p in corpus
    ]
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(bo.ModelRun(model_slug="m1", pages=seed_pages)))

    # Every checkpointed page needs >=1 ledger entry (page-level coverage,
    # #601 fold round 5, D FOLD 3) -- charge it all on the first page so the
    # total stays exactly $0.09 while every OTHER page is still covered.
    ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for i, page in enumerate(corpus):
        ledger.append(
            bo.LedgerEntry(
                arm="m1",
                slug=page.slug,
                request_tokens=900_000 if i == 0 else 0,
                response_tokens=0,
                priced_usd=0.09 if i == 0 else 0.0,
                timed_out=False,
                reserve_applied=False,
            )
        )

    cost_estimates = [
        bo.ModelCostEstimate(slug="m1", input_tokens=0, output_tokens=0, usd=0.0),
        bo.ModelCostEstimate(slug="m2", input_tokens=900_000, output_tokens=0, usd=0.09),
    ]
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=0.10,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.stopped_early is True
    assert result.executed_slugs == []  # refused BEFORE any call
    assert result.unevaluated_slugs == ["m2"]
    assert result.breaker_tripped is False  # the ESTIMATE guard, not the real-spend breaker


@pytest.mark.asyncio
async def test_run_bakeoff_mid_arm_breaker_trip_is_not_checkpointed(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A NEW (non-checkpointed) arm whose usage-priced spend trips the meter MID-RUN
    (#601 fold round 1, slice B) is reported via ``breaker_tripped`` and NEVER
    checkpointed -- a re-run always retries the whole arm from page one.
    ``cost_estimates`` stays a tiny hand-built figure decoupled from the absurd
    ACTUAL roster price so the pre-existing chars/4 ESTIMATE guard does not fire
    first."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 1_000_000, 1_000_000, "x")]  # absurd actual price
    cost_estimates = [bo.ModelCostEstimate(slug="m1", input_tokens=1, output_tokens=1, usd=0.0001)]
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=0.01,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.breaker_tripped is True
    assert result.stopped_early is True
    assert result.runs == []  # the incomplete run is never appended
    assert result.unevaluated_slugs == ["m1"]
    assert result.executed_slugs == ["m1"]  # a real call WAS attempted
    checkpoint = bo.Checkpoint(
        bo.sidecar_path(out), resume=True, fingerprint=bo.compute_fingerprint(corpus)
    )
    assert not checkpoint.has("m1")  # never checkpointed -- a re-run retries from page one


@pytest.mark.asyncio
async def test_run_bakeoff_prorates_by_cost_not_flat_page_fraction(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#601 fold round 14, F3: proration is COST-weighted, not a flat
    attempted/total page-count fraction -- attempting the corpus's most
    EXPENSIVE (longest) page FIRST must show MORE than its 1/N page-count
    share of the arm's total estimate."""
    out = tmp_path / "o.json"
    pages = sorted(
        corpus,
        key=lambda p: len(bo._extract_prompt_text(p)),  # pyright: ignore[reportPrivateUsage]
        reverse=True,
    )
    # A modest, REAL size-based estimate (decoupled from the absurd actual
    # price below, so the pre-run ESTIMATE guard doesn't fire first).
    estimate_roster = [bo.RosterModel("m1", 0.05, 0.05, "x")]
    cost_estimates = bo.estimate_cost(pages, estimate_roster)
    roster = [bo.RosterModel("m1", 1_000_000, 1_000_000, "x")]  # absurd actual price, trips fast
    result = await bo.run_bakeoff(
        pages,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=0.01,
        cost_estimates=cost_estimates,
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert result.breaker_tripped is True
    assert result.prorated_attempted_pages == 1
    assert result.prorated_total_pages == len(pages)
    flat_fraction_share = cost_estimates[0].usd / len(pages)
    assert result.prorated_attempted_estimate is not None
    assert result.prorated_attempted_estimate > flat_fraction_share


def test_page_cost_estimate_light_reasoning_multiplies_output_component(
    corpus: list[bo.CorpusPage],
) -> None:
    """#650 round-1: ``_page_cost_estimate`` must match
    ``estimate_cost_for_arms``'s :data:`bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER`
    on the output-token component -- mid-arm proration on a light arm
    previously understated every attempted page by using the base
    (non-multiplied) output estimate."""
    page = corpus[0]
    price = bo.RosterModel("m1", 1.0, 1.0, "x")
    default_est = bo._page_cost_estimate(page, price)  # pyright: ignore[reportPrivateUsage]
    light_est = bo._page_cost_estimate(page, price, reasoning="light")  # pyright: ignore[reportPrivateUsage]
    input_component = (
        bo._page_input_tokens_estimate(page)  # pyright: ignore[reportPrivateUsage]
        / 1_000_000
        * price.price_in_per_mtok
    )
    default_output_component = default_est - input_component
    light_output_component = light_est - input_component
    assert light_output_component == pytest.approx(
        default_output_component * bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER
    )


@pytest.mark.asyncio
async def test_run_bakeoff_prorates_light_arm_output_component_at_the_multiplier(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#650 round-1: an identical mid-arm trip on a "light" arm must prorate
    at :data:`bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER` on the output
    component -- matching a "default" arm's trip except for that scaling,
    never silently reusing the base (unscaled) per-page estimate."""
    out_default = tmp_path / "default.json"
    out_light = tmp_path / "light.json"
    estimate_roster = [bo.RosterModel("m1", 0.05, 0.5, "x")]  # nonzero output price
    real_roster = [bo.RosterModel("m1", 1_000_000, 1_000_000, "x")]  # trips after page 1

    default_arms = bo.expand_arms(["m1"], "default")
    default_estimates = bo.estimate_cost_for_arms(corpus, default_arms, estimate_roster)
    default_result = await bo.run_bakeoff(
        corpus,
        default_arms,
        out=out_default,
        resume=True,
        max_spend=0.01,
        cost_estimates=default_estimates,
        roster=real_roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )

    light_arms = bo.expand_arms(["m1"], "light")
    light_estimates = bo.estimate_cost_for_arms(corpus, light_arms, estimate_roster)
    light_result = await bo.run_bakeoff(
        corpus,
        light_arms,
        out=out_light,
        resume=True,
        max_spend=0.01,
        cost_estimates=light_estimates,
        roster=real_roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )

    assert default_result.breaker_tripped is True
    assert light_result.breaker_tripped is True
    assert default_result.prorated_attempted_pages == 1
    assert light_result.prorated_attempted_pages == 1
    # The mid-arm proration prices at the REAL roster (`roster=`, what
    # price_by_slug resolves to inside run_bakeoff) -- estimate_roster is
    # ONLY the pre-run estimate guard's decoupled basis, never this.
    price = real_roster[0]
    input_component = (
        bo._page_input_tokens_estimate(corpus[0])  # pyright: ignore[reportPrivateUsage]
        / 1_000_000
        * price.price_in_per_mtok
    )
    assert default_result.prorated_attempted_estimate is not None
    assert light_result.prorated_attempted_estimate is not None
    default_output = default_result.prorated_attempted_estimate - input_component
    light_output = light_result.prorated_attempted_estimate - input_component
    assert light_output == pytest.approx(
        default_output * bo.LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER
    )


@pytest.mark.asyncio
async def test_run_bakeoff_ledger_persists_a_mid_arm_trip_across_invocations(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """The repeated-resume overspend regression (#601 fold round 1, the round-1 P1):
    a mid-arm trip's charges are NEVER checkpointed as a scored run, so reconstructing
    the meter from scored runs would find NOTHING for an incomplete arm and let a
    re-run spend the WHOLE budget again on the SAME arm. The persistent ledger closes
    that gap: a SECOND invocation against the same ``--out`` refuses "m1" immediately,
    before any new call."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 1_000_000, 1_000_000, "x")]  # absurd -- 1 page trips it
    arms = bo.expand_arms(["m1"], "default")
    cost_estimates = [bo.ModelCostEstimate(slug="m1", input_tokens=1, output_tokens=1, usd=0.0001)]
    model = _model_returning({"name": "X", "country": "Ecuador"})

    first = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=0.01,
        cost_estimates=cost_estimates,
        roster=roster,
        model=model,
    )
    assert first.breaker_tripped is True
    assert first.runs == []  # the incomplete arm was never checkpointed

    second = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=0.01,
        cost_estimates=cost_estimates,
        roster=roster,
        model=model,
    )
    assert second.breaker_tripped is True
    assert second.executed_slugs == []  # refused BEFORE any new call -- ledger carried it
    assert second.runs == []


@pytest.mark.asyncio
async def test_run_bakeoff_threads_the_correct_reasoning_effort_per_arm(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration guard against an off<->light swap (#601 qa pass, round 8
    addendum): ``Arm.reasoning`` -> ``_REASONING_EFFORT_BY_ARM`` -> the actual
    ``draft_bean_profile_from_url`` call must reach the provider with the
    CORRECT, DISTINCT ``reasoning_effort`` per arm -- "off" arm gets ``"off"``,
    "light" arm gets ``"low"``. ``_REASONING_EFFORT_BY_ARM`` is unit-tested and the
    extra_body plumbing is covered in test_bean_sourcing.py, but neither exercises
    this harness-level hand-off end-to-end."""
    captured: list[object] = []

    async def _fake_draft(
        url: str,
        *,
        advisor_config: AdvisorConfig,
        sourcing_config: bo.BeanSourcingConfig | None = None,
        http_client: object = None,
        model: object = None,
        reasoning_effort: object = None,
        diagnostics: object = None,
        max_output_tokens: object = None,
        disable_transport_retries: object = None,
    ) -> BeanProfileDraft:
        captured.append(reasoning_effort)
        raise bo.BeanSourcingError("stub -- no real extraction needed for this guard")

    monkeypatch.setattr(bo, "draft_bean_profile_from_url", _fake_draft)
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1"], "both")
    await bo.run_bakeoff(
        corpus,
        arms,
        out=tmp_path / "o.json",
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost_for_arms(corpus, arms, roster),
        roster=roster,
    )
    assert len(captured) == 2 * len(corpus)  # "off" arm's pages, then "light" arm's
    assert set(captured[: len(corpus)]) == {"off"}
    assert set(captured[len(corpus) :]) == {"low"}


@pytest.mark.asyncio
async def test_run_bakeoff_both_arms_checkpoint_under_distinct_labels(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """``--reasoning both`` must checkpoint/score the "off" and "light" arms of ONE
    model as two DISTINCT records (#601 scope item 3: the per-run record key
    includes the reasoning arm; fold round 1: "both" is off+light, never default)."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1"], "both")
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost_for_arms(corpus, arms, roster),
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert {r.model_slug for r in result.runs} == {"m1+reasoning-off", "m1+reasoning-light"}
    assert result.executed_slugs == ["m1+reasoning-off", "m1+reasoning-light"]
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert checkpoint.has("m1+reasoning-off")
    assert checkpoint.has("m1+reasoning-light")


@pytest.mark.asyncio
async def test_run_bakeoff_resume_distinguishes_arms(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A checkpointed "off" arm must NOT be mistaken for its "light" sibling on
    resume -- only the arm actually on disk is skipped, the other still runs."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    fingerprint = bo.compute_fingerprint(corpus)
    seed = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    # only the "off" arm resumed
    seed.append(bo.run_to_json(_full_run("m1+reasoning-off", bo.Outcome.COR)))
    # Full page coverage -- #601 fold round 2, FOLD 4 + fold round 5, D FOLD 3.
    seed_ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("m1+reasoning-off", ["page-a", "page-b"]):
        seed_ledger.append(entry)

    arms = bo.expand_arms(["m1"], "both")
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost_for_arms(corpus, arms, roster),
        roster=roster,
        model=_model_returning({"name": "X", "country": "Ecuador"}),
    )
    assert {r.model_slug for r in result.runs} == {"m1+reasoning-off", "m1+reasoning-light"}
    assert result.executed_slugs == ["m1+reasoning-light"]  # "off" resumed, no new call


# --- Pipeline fingerprint (#600 round-2 finding) --------------------------
#
# compute_fingerprint used to key only on corpus content: changing the
# extractor's preprocessing (e.g. #590) without touching the committed
# HTML/gold fixtures would silently resume a pre-change checkpoint.


def test_pipeline_fingerprint_is_deterministic_and_nonempty() -> None:
    first = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]
    second = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]
    assert first == second
    assert first  # this env can locate the extractor/harness source files


def test_fingerprinted_modules_cover_the_whole_extraction_call_path() -> None:
    """Round-2 hashed only ``bean_sourcing.py`` + the harness; the evaluated
    call path also calls ``advisor.build_model`` and constructs
    ``BeanProfileDraft``/config objects through ``config.py``/``models.py`` --
    a change to any of those could alter a fresh result while an old
    checkpoint is still (wrongly) accepted (#602 finding)."""
    module_names = {m.__name__ for m in bo._FINGERPRINTED_MODULES}  # pyright: ignore[reportPrivateUsage]
    assert module_names == {
        "roastpilot_agent.bean_sourcing",
        "roastpilot_agent.advisor",
        "roastpilot_agent.config",
        "roastpilot_agent.models",
    }


def test_pipeline_fingerprint_changes_when_a_widened_module_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``advisor.py`` (not just ``bean_sourcing.py`` + the harness) must be
    hashed too -- a provider-construction change must invalidate a stale
    checkpoint (#602 finding)."""
    import inspect as inspect_module

    original_getsourcefile = inspect_module.getsourcefile
    baseline = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]

    fake_advisor_source = tmp_path / "advisor_fake.py"
    fake_advisor_source.write_text("# changed content for a fingerprint-sensitivity test\n")

    def _patched(module: object) -> str | None:
        if module is bo._advisor_module:  # pyright: ignore[reportPrivateUsage]
            return str(fake_advisor_source)
        return original_getsourcefile(module)  # type: ignore[arg-type]

    monkeypatch.setattr(bo.inspect, "getsourcefile", _patched)
    changed = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]
    assert changed != baseline
    assert changed  # still resolvable, not degraded to ""


def test_environment_fingerprint_is_stable_across_calls() -> None:
    """The same installed environment must fingerprint identically across
    calls (#602 fold round 6, FOLD 2)."""
    first = bo._environment_fingerprint()  # pyright: ignore[reportPrivateUsage]
    second = bo._environment_fingerprint()  # pyright: ignore[reportPrivateUsage]
    assert first == second
    assert first  # this env has installed distributions


def test_environment_fingerprint_changes_with_interpreter_and_platform_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distribution set alone misses the RUNTIME itself: identical packages under
    Python 3.12 vs 3.11 (or a different interpreter/OS) must NOT fingerprint the same, or
    a checkpoint could resume across interpreters (#602 fold round 7)."""
    baseline = bo._environment_fingerprint()  # pyright: ignore[reportPrivateUsage]

    monkeypatch.setattr(bo.platform, "python_version", lambda: "9.9.9")
    assert bo._environment_fingerprint() != baseline  # pyright: ignore[reportPrivateUsage]
    monkeypatch.undo()

    monkeypatch.setattr(bo.platform, "platform", lambda: "Some-Other-Platform-x86")
    assert bo._environment_fingerprint() != baseline  # pyright: ignore[reportPrivateUsage]
    monkeypatch.undo()

    monkeypatch.setattr(bo.sys.implementation, "name", "fake-impl")
    assert bo._environment_fingerprint() != baseline  # pyright: ignore[reportPrivateUsage]


def test_pipeline_fingerprint_changes_when_the_environment_changes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CATEGORICAL distribution-set fingerprint means ANY installed
    package's version change invalidates resume -- including a TRANSITIVE
    dependency a hand-picked list would miss entirely (#602 fold round 6,
    FOLD 2 -- replaces the round-4/5 hand-list)."""
    baseline = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]
    real_distributions = list(importlib.metadata.distributions())

    class _FakeDist:
        def __init__(self, name: str, version: str) -> None:
            self.name = name
            self.version = version

    def _patched() -> list[_FakeDist]:
        fakes = [_FakeDist(d.name, d.version) for d in real_distributions]
        if fakes:
            fakes[0].version = "999.999.999"  # simulate ANY package moving, even transitive
        return fakes

    monkeypatch.setattr(bo.importlib.metadata, "distributions", _patched)
    changed = bo._pipeline_fingerprint()  # pyright: ignore[reportPrivateUsage]
    assert changed != baseline
    assert changed


def test_compute_fingerprint_changes_with_pipeline_fingerprint(
    corpus: list[bo.CorpusPage], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bo, "_pipeline_fingerprint", lambda: "pipeline-a")
    fp_a = bo.compute_fingerprint(corpus)
    monkeypatch.setattr(bo, "_pipeline_fingerprint", lambda: "pipeline-b")
    fp_b = bo.compute_fingerprint(corpus)
    assert fp_a != fp_b


def test_pipeline_fingerprint_disabled_when_source_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Introspection failure degrades to the corpus-only guard rather than
    crashing a real run."""

    def _no_source(_module: object) -> str | None:
        return None

    monkeypatch.setattr(bo.inspect, "getsourcefile", _no_source)
    assert bo._pipeline_fingerprint() == ""  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_run_bakeoff_pipeline_change_invalidates_resume(
    corpus: list[bo.CorpusPage], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Changing the evaluated pipeline (e.g. #590 preprocessing) without
    touching the corpus must NOT silently resume the pre-change checkpoint
    -- the exact #590 scenario the finding calls out."""
    out = tmp_path / "o.json"
    model = _model_returning({"name": "X", "country": "Ecuador"})
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]

    monkeypatch.setattr(bo, "_pipeline_fingerprint", lambda: "pipeline-v1")
    first = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, roster),
        roster=roster,
        model=model,
    )
    assert first.executed_slugs == ["m1"]

    # same corpus, DIFFERENT pipeline fingerprint (simulating a #590-style
    # preprocessing change) -- must re-execute, not resume the stale record.
    monkeypatch.setattr(bo, "_pipeline_fingerprint", lambda: "pipeline-v2")
    second = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, roster),
        roster=roster,
        model=model,
    )
    assert second.executed_slugs == ["m1"]


@pytest.mark.asyncio
async def test_run_bakeoff_stale_fingerprint_is_not_resumed(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A checkpoint written against a DIFFERENT corpus/settings must not be
    silently resumed and mixed into this run's leaderboard (#600 finding)."""
    out = tmp_path / "o.json"
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint="stale-fingerprint")
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    # max_spend=0.0 -> if the stale record were wrongly resumed the run would
    # short-circuit with no budget check; instead it must be treated as NOT
    # on disk and hit the (real) budget-stop path.
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=0.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 1.0, 1.0, "x")]),
        roster=[bo.RosterModel("m1", 1.0, 1.0, "x")],
    )
    assert result.runs == []
    assert result.stopped_early is True


@pytest.mark.asyncio
async def test_run_bakeoff_does_not_checkpoint_a_wholly_failed_run(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """Every page erroring on a genuine INFRA-class failure, with NO peer having
    succeeded this invocation, is labelled INFRA-WIDE (heuristic, display-only, #602
    fold round 5): must not be cached as 'done' -- a re-run should retry, not resume
    the outage forever (#600 finding) -- AND must not appear in ``runs`` as a scored
    0.000 row polluting the leaderboard/statistics (#600 round-2 finding): it is
    reported separately via ``failed_slugs`` instead. Uses a genuine provider error
    (NOT a schema failure -- #601 F1 gives that case different treatment, see
    :func:`test_run_bakeoff_all_schema_failure_run_is_checkpointed_and_scored`)."""
    out = tmp_path / "o.json"
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        model=_model_provider_error(),
    )
    assert result.runs == []
    assert result.failed_slugs == [
        bo.FailedRun(model_slug="m1", heuristic_label="INFRA-WIDE", other_errors=len(corpus))
    ]
    assert result.executed_slugs == ["m1"]  # a paid attempt WAS made
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert not checkpoint.has("m1")


@pytest.mark.asyncio
async def test_run_bakeoff_all_schema_failure_run_is_checkpointed_and_scored(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """An ALL-SCHEMA-failure run (every page a malformed-structured-output failure) is
    the strongest possible non-adherence signal, not an outage: it must be checkpointed,
    scored, and appear in the off-vs-light comparison, not dropped as INFRA-WIDE
    (#601 fold round 2, F1)."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, roster),
        roster=roster,
        model=_model_text_only(),
    )
    assert result.failed_slugs == []
    assert [r.model_slug for r in result.runs] == ["m1"]
    m = bo.model_metrics(result.runs[0])
    assert m.schema_failures == len(corpus)
    assert m.other_errors == 0
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert checkpoint.has("m1")


@pytest.mark.asyncio
async def test_run_bakeoff_all_schema_failure_arm_appears_in_reasoning_comparison(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """An all-schema-failure "off" arm must reach the off-vs-light report section --
    the strongest adherence signal, not dropped (#601 F1)."""
    out = tmp_path / "o.json"
    roster = [bo.RosterModel("m1", 0.1, 0.1, "x")]
    arms = bo.expand_arms(["m1"], "both")
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost_for_arms(corpus, arms, roster),
        roster=roster,
        model=_model_text_only(),  # both arms all-schema-failure
    )
    assert {r.model_slug for r in result.runs} == {"m1+reasoning-off", "m1+reasoning-light"}
    report = bo.render_report(result.runs, bo.estimate_cost_for_arms(corpus, arms, roster))
    assert "Reasoning-arm comparison (off vs light" in report
    assert f"{len(corpus)}/0 -> {len(corpus)}/0" in report  # both arms' schema failures


@pytest.mark.asyncio
async def test_run_bakeoff_mixed_failure_run_is_dropped_with_counts_preserved(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A MIXED all-error run (1 schema failure + 8 infra failures) is dropped (never
    checkpointed, always retried on resume) -- but its FailedRun annotation preserves
    the schema/other-error split, so the adherence signal stays visible even though
    the run itself isn't scored (#601 fold round 7, FOLD 1)."""
    out = tmp_path / "o.json"
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        if calls["n"] <= 2:  # first page: schema failure (1 attempt + 1 retry)
            return ModelResponse(parts=[TextPart("no structured output")])
        raise ModelAPIError("test-model", "simulated provider outage")  # every other page

    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        model=FunctionModel(respond),
    )
    assert result.runs == []
    assert result.failed_slugs == [
        bo.FailedRun(
            model_slug="m1",
            heuristic_label="INFRA-WIDE",
            schema_failures=1,
            other_errors=len(corpus) - 1,
        )
    ]
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert not checkpoint.has("m1")

    calls["n"] = 0  # a resume must ALWAYS retry a dropped run
    resumed = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        model=FunctionModel(respond),
    )
    assert resumed.executed_slugs == ["m1"]


@pytest.mark.asyncio
async def test_run_bakeoff_resumed_success_does_not_flip_the_heuristic_label(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """A RESUMED peer's PAST success does not count as ``has_fresh_success``
    for the DISPLAY-ONLY heuristic label (#602 fold round 1, preserved
    through the round-5 simplification): with only a resumed peer and every
    FRESH model failing, the fresh failure is labelled INFRA-WIDE -- never
    checkpointed either way (the label no longer affects persistence at
    all, #602 fold round 5)."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    # Seed a checkpoint with a peer that succeeded in a PRIOR invocation --
    # resuming it (no new call) means it is NOT freshly executed this run.
    seed = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    seed.append(bo.run_to_json(_full_run("good", bo.Outcome.COR)))
    # Full page coverage -- #601 fold round 2, FOLD 4 + fold round 5, D FOLD 3.
    seed_ledger = bo.ChargeLedger(bo.ledger_path(out), fingerprint=fingerprint)
    for entry in _page_covering_entries("good", ["page-a", "page-b"]):
        seed_ledger.append(entry)

    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["good", "bad"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(
            corpus, [bo.RosterModel("good", 0.1, 0.1, "x"), bo.RosterModel("bad", 0.1, 0.1, "x")]
        ),
        roster=[bo.RosterModel("good", 0.1, 0.1, "x"), bo.RosterModel("bad", 0.1, 0.1, "x")],
        model=_model_provider_error(),  # only applies to "bad" -- "good" resumes
    )
    assert {r.model_slug for r in result.runs} == {"good"}  # "bad" NOT scored
    assert result.failed_slugs == [
        bo.FailedRun(model_slug="bad", heuristic_label="INFRA-WIDE", other_errors=len(corpus))
    ]
    assert result.executed_slugs == ["bad"]
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert not checkpoint.has("bad")  # NEVER checkpointed, regardless of the heuristic label


def _model_switch_after(n: int, *, fail_first: bool) -> FunctionModel:
    """A double that flips outcome after the ``n``th call, letting one
    shared ``model`` drive TWO sequential models in one ``run_bakeoff``
    invocation to different outcomes. ``fail_first`` fails the first ``n``
    calls then succeeds; otherwise it succeeds the first ``n`` then fails.
    The failing side raises a genuine (INFRA-class, #601 F1) provider error,
    NOT a schema failure -- 1 call per page either way."""
    calls = {"n": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        calls["n"] += 1
        succeeding = calls["n"] > n if fail_first else calls["n"] <= n
        if succeeding:
            return ModelResponse(
                parts=[ToolCallPart(info.output_tools[0].name, {"name": "X", "country": "Ecuador"})]
            )
        raise ModelAPIError("test-model", "simulated provider outage")

    return FunctionModel(respond)


@pytest.mark.asyncio
async def test_run_bakeoff_never_checkpoints_a_wholly_failed_run_even_with_a_fresh_peer(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """#602 fold round 5 SIMPLIFICATION: rounds 3-4 eagerly checkpointed a
    wholly-failed run once a fresh peer's success made it "provable"
    MODEL-SPECIFIC -- but no invocation-local signal can truly tell a
    transient outage occurring inside ONE model's turn apart from a
    model-specific fault, so that eager persistence could permanently
    corrupt the leaderboard on exactly this ambiguous case. Failed attempts
    are cheap to retry; a mis-scored failure is expensive to fix -- so a
    wholly-failed run is NEVER checkpointed, even when "a" succeeds first
    and "b" then wholly fails (the heuristic label is display-only)."""
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    roster = [bo.RosterModel("a", 0.1, 0.1, "x"), bo.RosterModel("b", 0.1, 0.1, "x")]
    estimates = bo.estimate_cost(corpus, roster)
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["a", "b"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=estimates,
        roster=roster,
        model=_model_switch_after(len(corpus), fail_first=False),  # "a" succeeds, "b" fails
    )
    assert {r.model_slug for r in result.runs} == {"a"}  # the success IS scored, unaffected
    assert result.failed_slugs == [
        bo.FailedRun(model_slug="b", heuristic_label="MODEL-SPECIFIC", other_errors=len(corpus))
    ]
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert checkpoint.has("a")
    assert not checkpoint.has("b")  # NEVER checkpointed, regardless of the heuristic label

    # A resume must ALWAYS retry "b" -- a paid attempt is made again.
    resumed = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["a", "b"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=estimates,
        roster=roster,
        model=_model_text_only(),
    )
    assert resumed.executed_slugs == ["b"]  # "a" resumed (no new call), "b" retried


def test_run_wholly_failed_detects_all_errored_pages() -> None:
    run = bo.ModelRun(
        model_slug="m",
        pages=[
            bo.PageResult(slug="a", outcomes={}, error="boom", on_page_fields=0),
            bo.PageResult(slug="b", outcomes={}, error="boom", on_page_fields=0),
        ],
    )
    assert bo._run_wholly_failed(run) is True  # pyright: ignore[reportPrivateUsage]
    mixed = bo.ModelRun(
        model_slug="m",
        pages=[
            bo.PageResult(slug="a", outcomes={}, error="boom", on_page_fields=0),
            bo.PageResult(slug="b", outcomes={}, error=None, on_page_fields=1),
        ],
    )
    assert bo._run_wholly_failed(mixed) is False  # pyright: ignore[reportPrivateUsage]
    empty = bo.ModelRun(model_slug="m", pages=[])
    assert bo._run_wholly_failed(empty) is False  # pyright: ignore[reportPrivateUsage]


def test_run_wholly_failed_drops_any_mix_but_retains_all_schema() -> None:
    """Synthesis of round 5 + round 7 (#601): a MIXED all-error run (even 1 schema +
    8 infra) is dropped -- its F1 would measure the outage, not model quality -- but
    an ALL-SCHEMA run (every failure malformed structured output) is still retained
    as a real outcome, the strongest non-adherence signal. An ALL-infra run is also
    dropped."""
    schema_error = "BeanExtractionUnavailableError: ... returned a malformed shape: x"
    timeout_error = "BeanExtractionUnavailableError: ... exceeded the 45s deadline"

    def _run(n_schema: int, n_timeout: int) -> bo.ModelRun:
        pages = [
            bo.PageResult(slug=f"s{i}", outcomes={}, error=schema_error, on_page_fields=0)
            for i in range(n_schema)
        ] + [
            bo.PageResult(slug=f"t{i}", outcomes={}, error=timeout_error, on_page_fields=0)
            for i in range(n_timeout)
        ]
        return bo.ModelRun(model_slug="m", pages=pages)

    mixed = _run(1, 8)
    assert bo._run_wholly_failed(mixed) is True  # pyright: ignore[reportPrivateUsage]

    all_schema = _run(9, 0)
    assert bo._run_wholly_failed(all_schema) is False  # pyright: ignore[reportPrivateUsage]
    m = bo.model_metrics(all_schema)
    assert m.schema_failures == 9
    assert m.other_errors == 0

    all_infra = _run(0, 9)
    assert bo._run_wholly_failed(all_infra) is True  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_run_bakeoff_checkpoints_a_successful_run(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    """The ordinary (non-wholly-failed) success path DOES checkpoint."""
    model = _model_returning({"name": "X", "country": "Ecuador"})
    out = tmp_path / "o.json"
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
        roster=[bo.RosterModel("m1", 0.1, 0.1, "x")],
        model=model,
    )
    assert result.stopped_early is False
    assert result.runs[0].model_slug == "m1"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=True, fingerprint=fingerprint)
    assert checkpoint.has("m1")


@pytest.mark.asyncio
async def test_main_full_run_writes_artifact_and_partial_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives ``main()``'s full (non-``--estimate-only``) success path against
    a monkeypatched, network-free :func:`bo.run_bakeoff` -- covers the
    artifact/report-writing + :class:`BakeoffResult` propagation glue,
    including the PARTIAL-run labelling, without a real paid call."""
    canned = bo.BakeoffResult(
        runs=[_full_run(bo.MODEL_ROSTER[0].slug, bo.Outcome.COR)],
        stopped_early=True,
        unevaluated_slugs=[bo.MODEL_ROSTER[1].slug],
        failed_slugs=[bo.FailedRun(model_slug="some/failed-slug", heuristic_label="INFRA-WIDE")],
        executed_slugs=[bo.MODEL_ROSTER[0].slug, "some/failed-slug"],
    )

    async def _fake_run_bakeoff(*args: object, **kwargs: object) -> bo.BakeoffResult:
        return canned

    monkeypatch.setattr(bo, "run_bakeoff", _fake_run_bakeoff)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    out = tmp_path / "out.json"
    # a report-md path into a NONEXISTENT nested dir -- must be created, not
    # crash after the (simulated) paid run (#600 round-2 finding).
    report_md = tmp_path / "nested" / "reports" / "report.md"
    code = await bo.main(
        [
            "--models",
            bo.MODEL_ROSTER[0].slug,
            bo.MODEL_ROSTER[1].slug,
            "--max-spend",
            "5",
            "--out",
            str(out),
            "--report-md",
            str(report_md),
        ]
    )
    assert code == 0
    artifact = json.loads(out.read_text())
    assert artifact["stopped_early"] is True
    assert artifact["unevaluated_slugs"] == [bo.MODEL_ROSTER[1].slug]
    assert artifact["failed_slugs"] == [
        {
            "model_slug": "some/failed-slug",
            "heuristic_label": "INFRA-WIDE",
            "schema_failures": 0,
            "other_errors": 0,
        }
    ]
    assert artifact["executed_slugs"] == [bo.MODEL_ROSTER[0].slug, "some/failed-slug"]
    report_text = report_md.read_text()
    assert "PARTIAL RUN" in report_text
    assert "EXCLUDED -- failed this invocation" in report_text
    assert "some/failed-slug" in report_text
    assert "ESTIMATED SPEND INCURRED" in report_text


@pytest.mark.asyncio
async def test_main_returns_distinct_exit_code_when_breaker_tripped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A :class:`bo.BakeoffResult` with ``breaker_tripped=True`` (#601 fold round 1,
    slice B) must surface as exit code 3 -- distinct from the plain 0 success exit
    and the 2 used by the pre-run REFUSED guards, per the parser epilog."""
    canned = bo.BakeoffResult(
        runs=[_full_run(bo.MODEL_ROSTER[0].slug, bo.Outcome.COR)],
        stopped_early=True,
        unevaluated_slugs=[bo.MODEL_ROSTER[1].slug],
        failed_slugs=[],
        executed_slugs=[bo.MODEL_ROSTER[0].slug],
        breaker_tripped=True,
    )

    async def _fake_run_bakeoff(*args: object, **kwargs: object) -> bo.BakeoffResult:
        return canned

    monkeypatch.setattr(bo, "run_bakeoff", _fake_run_bakeoff)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    out = tmp_path / "out.json"
    code = await bo.main(
        [
            "--models",
            bo.MODEL_ROSTER[0].slug,
            bo.MODEL_ROSTER[1].slug,
            "--max-spend",
            "5",
            "--out",
            str(out),
        ]
    )
    assert code == 3
    artifact = json.loads(out.read_text())
    assert artifact["breaker_tripped"] is True


@pytest.mark.asyncio
async def test_main_refuses_a_zero_max_spend_before_any_real_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--max-spend 0`` admits no spend at all -- REFUSED at exit 2, distinct from
    exit 3 (the breaker halting a NON-zero, real run) (#601 fold round 1, slice B).
    Never calls :func:`bo.run_bakeoff` at all -- a monkeypatched trap fails the test
    if it does."""

    async def _trap_run_bakeoff(*args: object, **kwargs: object) -> bo.BakeoffResult:
        raise AssertionError("run_bakeoff must not be called for a zero --max-spend")

    monkeypatch.setattr(bo, "run_bakeoff", _trap_run_bakeoff)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-not-real")
    out = tmp_path / "out.json"
    code = await bo.main(
        [
            "--models",
            bo.MODEL_ROSTER[0].slug,
            "--max-spend",
            "0",
            "--out",
            str(out),
        ]
    )
    assert code == 2
    assert not out.exists()  # refused before any artifact is written
