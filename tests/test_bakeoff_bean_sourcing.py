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

import dataclasses
import importlib.metadata
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelAPIError
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
async def test_run_model_over_corpus_zeroes_recovered_violations_on_a_failed_page(
    corpus: list[bo.CorpusPage],
) -> None:
    """A retry-RECOVERED extraction whose identity is later REJECTED downstream (no
    usable name/origin) must still report ``recovered_violations == 0`` on that page
    -- ``PageResult``'s own "0 on a failed page" contract (#601 fold round 3, FB)."""
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
    assert page.recovered_violations == 0


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


def test_expand_arms_both_gates_arms_by_three_way_capability(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ "both" on a mixed roster must gate BOTH off and light per capability (#601
    fold round 3, FA): "none" gets neither (both skipped), "mandatory" gets light
    only (off skipped -- disabling would 400), "optional" gets both, unchanged."""
    both = bo.expand_arms(
        ["nope", "must", "opt"],
        "both",
        capability={"nope": "none", "must": "mandatory", "opt": "optional"},
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
    assert "`model-failed` (MODEL-SPECIFIC, heuristic)" in report
    assert "- models scored: 1" in report  # the failed model is NOT counted


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


@pytest.mark.asyncio
async def test_run_bakeoff_budget_stop_makes_no_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    roster = [bo.RosterModel("m1", 1.0, 1.0, "x")]
    estimates = bo.estimate_cost(corpus, roster)
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=tmp_path / "o.json",
        resume=False,
        max_spend=0.0,  # first model's estimate > 0 -> stop before any (paid) call
        cost_estimates=estimates,
    )
    assert result.runs == []
    assert result.stopped_early is True
    assert result.unevaluated_slugs == ["m1"]
    assert result.executed_slugs == []
    assert result.failed_slugs == []


@pytest.mark.asyncio
async def test_run_bakeoff_resumes_without_calls(
    corpus: list[bo.CorpusPage], tmp_path: Path
) -> None:
    out = tmp_path / "o.json"
    fingerprint = bo.compute_fingerprint(corpus)
    checkpoint = bo.Checkpoint(bo.sidecar_path(out), resume=False, fingerprint=fingerprint)
    checkpoint.append(bo.run_to_json(_full_run("m1", bo.Outcome.COR)))
    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["m1"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(corpus, [bo.RosterModel("m1", 0.1, 0.1, "x")]),
    )
    assert [r.model_slug for r in result.runs] == ["m1"]
    assert result.stopped_early is False
    assert result.unevaluated_slugs == []
    assert result.executed_slugs == []  # resumed, not newly called
    assert result.failed_slugs == []


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

    arms = bo.expand_arms(["m1"], "both")
    result = await bo.run_bakeoff(
        corpus,
        arms,
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost_for_arms(corpus, arms, roster),
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
        model=_model_provider_error(),
    )
    assert result.runs == []
    assert result.failed_slugs == [bo.FailedRun(model_slug="m1", heuristic_label="INFRA-WIDE")]
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
        model=_model_text_only(),  # both arms all-schema-failure
    )
    assert {r.model_slug for r in result.runs} == {"m1+reasoning-off", "m1+reasoning-light"}
    report = bo.render_report(result.runs, bo.estimate_cost_for_arms(corpus, arms, roster))
    assert "Reasoning-arm comparison (off vs light" in report
    assert f"{len(corpus)}/0 -> {len(corpus)}/0" in report  # both arms' schema failures


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

    result = await bo.run_bakeoff(
        corpus,
        bo.expand_arms(["good", "bad"], "default"),
        out=out,
        resume=True,
        max_spend=1000.0,
        cost_estimates=bo.estimate_cost(
            corpus, [bo.RosterModel("good", 0.1, 0.1, "x"), bo.RosterModel("bad", 0.1, 0.1, "x")]
        ),
        model=_model_provider_error(),  # only applies to "bad" -- "good" resumes
    )
    assert {r.model_slug for r in result.runs} == {"good"}  # "bad" NOT scored
    assert result.failed_slugs == [bo.FailedRun(model_slug="bad", heuristic_label="INFRA-WIDE")]
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
        model=_model_switch_after(len(corpus), fail_first=False),  # "a" succeeds, "b" fails
    )
    assert {r.model_slug for r in result.runs} == {"a"}  # the success IS scored, unaffected
    assert result.failed_slugs == [bo.FailedRun(model_slug="b", heuristic_label="MODEL-SPECIFIC")]
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
        {"model_slug": "some/failed-slug", "heuristic_label": "INFRA-WIDE"}
    ]
    assert artifact["executed_slugs"] == [bo.MODEL_ROSTER[0].slug, "some/failed-slug"]
    report_text = report_md.read_text()
    assert "PARTIAL RUN" in report_text
    assert "EXCLUDED -- failed this invocation" in report_text
    assert "some/failed-slug" in report_text
    assert "ESTIMATED SPEND INCURRED" in report_text
