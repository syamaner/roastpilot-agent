"""#588 bean-sourcing extraction bake-off harness (research note section 5).

Screens candidate extraction models for the add-bean-from-URL feature (#573)
against a hand-labelled corpus of real vendor product pages
(``tests/fixtures/bean-sourcing/*.html`` + ``*.gold.json``). For each
``(page, field)`` it runs the FULL, unchanged
:func:`roastpilot_agent.bean_sourcing.draft_bean_profile_from_url` pipeline over
the CAPTURED page bytes and scores the resulting
:class:`~roastpilot_agent.models.BeanProfileDraft` against the gold state, using
the outcome table and match functions in the research note (section 5.1) and the
small-N statistics in section 5.2.

**Deterministic, replay-only fetch.** The page is never fetched live: the
captured fixture HTML is served through the extractor's own injected
``http_client`` seam — an ``httpx.AsyncClient`` over an
``httpx.MockTransport`` that returns the fixture bytes as a streamed body for
the page URL. That seam is deliberately exempt from the SSRF/pinning path (it is
the module's documented test seam), so the bake-off exercises the real
preprocessing + LLM extraction + provenance logic with zero network I/O to the
vendor. (The Onyx fixtures are nav-heavy Shopify pages whose product specs sit
PAST the extractor's 20,000-char text cap; a real model therefore sees mostly
navigation text for them — a genuine preprocessing finding, research note
section 2, that the corpus deliberately includes.)

**Scoring (section 5.1).** Each ``(page, field)`` is classified
``COR / PAR / INC / MIS / ABS-COR / SPU`` (see :class:`Outcome`). Match
functions: canonicalise (trim/lower/strip-accents) -> altitude to metres with a
tolerance and explicit RANGE handling -> a closed process/species synonym table
-> normalised-Levenshtein + word-bag recall for names/regions/farms (with a
small antonym/contradiction guard) -> order-independent word-bag alignment for
multi-cultivar varieties -> a coarse flavour-token overlap for free-text tasting
notes. It reports the three axes (recall, faithfulness, abstention-correctness),
micro + macro F1, and a ``CombinedScore`` that makes an honest abstainer beat a
confabulator (+1 COR / +0.5 PAR / +0.5 ABS-COR / 0 MIS / -0.5 INC / -1 SPU).

**Statistics (section 5.2).** A page-clustered paired bootstrap on the
``CombinedScore`` gap and on P/R/A (PRIMARY — resamples PAGES, so within-page
correlation is respected), an exact-binomial McNemar as a SECONDARY, indicative
per-field check (it treats field-pairs as independent, which OVERSTATES
significance), and a Wilson interval only on a strictly-binary COR-vs-not
decomposition (indicative, given the clustering). N is roughly 9 pages -- this
is a SCREENING harness, not certification (see the committed caveat text in the
report).

**Disclosed limitation -- RANGE-altitude ``COR`` is currently unreachable.**
:func:`_classify_altitude`'s RANGE branch only scores ``COR`` when the model
returns a scalar inside the range AND :attr:`BeanProfileDraft.field_sources`
tags it ``"origin_estimated"`` (research note section 5.1). The unchanged
production pipeline (``bean_sourcing.py``) never does that today: the model is
explicitly instructed not to compute a midpoint for a stated range, and
``altitude_m`` is an identity field, so any non-null altitude it returns is
always tagged ``"on_page"``, never ``"origin_estimated"`` (the midpoint/
``origin_estimated`` contract is deferred future work, ``bean_sourcing.py``
lines ~1219-1226). So for a page whose gold altitude is a RANGE, a compliant
model can only ever land ``MIS`` (a correct abstention) or ``INC`` (a leaked
scalar, always tagged ``on_page``) -- never ``COR`` -- uniformly deflating
recall/macro-F1 on the two RANGE-altitude corpus pages
(``cbc-costa-rica-laminita-tarrazu``, ``counterculture-concepcion-huista``)
regardless of model quality. **This does NOT guarantee an unchanged relative ranking** (#602
correction -- prior wording overclaimed this): ``MIS`` (weight 0) and ``INC`` (weight -0.5) are
different penalties, so one model abstaining while another leaks a scalar here CAN shift
``CombinedScore``/macro-F1 ordering. The committed 19 Jul run saw uniform abstention on both
cells -- a property of THAT run, not a guarantee; see :data:`CAVEAT_TEXT` and the results doc.
Aligning the RANGE contract with a real midpoint/``origin_estimated`` feature is deferred to #590.

**Evidence-quote capture (#612).** Every ``pages[*].extracted`` record already
carries the drafted :attr:`~roastpilot_agent.models.BeanProfileDraft.field_sources`
and :attr:`~roastpilot_agent.models.BeanProfileDraft.field_evidence` maps
verbatim (the harness serialises the whole draft via ``model_dump(mode="json")``
-- see :func:`run_model_over_corpus`), so nothing new needs projecting through.
:func:`evidence_summary` computes a compact per-run rollup of that data --
per-typed-field {evidence captured, no evidence} counts plus an overall
``on_page`` rate -- surfaced in :func:`render_report` and persisted in
:func:`run_to_json`'s ``evidence_summary`` key. **This is a quote
capture/authenticity-rate summary, NOT a certification signal**: the typed-field
citation VALUE gates stay permanently parked (#590), so it describes what the
extractor captured/tagged, never whether the value itself is correct.

**Model roster (section 4).** :data:`MODEL_ROSTER` pins the shortlist for the
eventual paid run. **This module is read-only and never runs a paid model on
import or under the self-test**; a real bake-off spends OpenRouter credits and
is gated on explicit operator approval (see the run command below and #588).

**Ops gotcha -- a stale ``OPENROUTER_API_KEY`` shadows ``.env`` -> 401.** The
advisor reads ``OPENROUTER_API_KEY`` from ``os.environ`` (via
:func:`roastpilot_agent.advisor.build_model`). A key exported in the shell (even
an old/empty one) SHADOWS the repo ``.env`` value, and the paid run then fails
every call with a 401 that looks like a model/provider fault (the same trap the
#567 reference-curve bake-off and the research note section 7 flag). The paid
run must therefore pass the ``.env`` value EXPLICITLY: :func:`main` reads the
repo ``.env`` and OVERRIDES ``os.environ[OPENROUTER_API_KEY]`` with it before
building any advisor (see :func:`load_dotenv_key`), printing a one-line note so
the source of the key is never ambiguous.

Paid run (gated -- needs the operator's go-ahead)::

    # the .env OPENROUTER_API_KEY is loaded + used explicitly; a stale shell
    # export is overridden, not silently honoured.
    python scripts/bakeoff_bean_sourcing.py \\
        --max-spend 2 \\
        --out /tmp/bakeoff-bean-sourcing.json \\
        --report-md /tmp/bakeoff-bean-sourcing.md

Zero-spend proof of the scoring/wiring is the deterministic self-test
(``tests/test_bakeoff_bean_sourcing.py``), which drives this module's real
pipeline with a PydanticAI ``FunctionModel`` -- no key, no network.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import importlib.metadata
import inspect
import itertools
import json
import os
import random
import re
import sys
import time
import unicodedata
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from math import comb, isfinite, sqrt
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # editable-install fallback

from pydantic_ai.models import Model  # noqa: E402

import roastpilot_agent.advisor as _advisor_module  # noqa: E402
import roastpilot_agent.bean_sourcing as _bean_sourcing_module  # noqa: E402
import roastpilot_agent.config as _config_module  # noqa: E402
import roastpilot_agent.models as _models_module  # noqa: E402
from roastpilot_agent.bean_sourcing import (  # noqa: E402
    BeanSourcingError,
    draft_bean_profile_from_url,
)
from roastpilot_agent.config import (  # noqa: E402
    OPENROUTER_BASE_URL,
    AdvisorConfig,
    BeanSourcingConfig,
)
from roastpilot_agent.models import BeanProfileDraft  # noqa: E402

#: The first-party ``roastpilot_agent`` modules transitively imported by the
#: evaluated extraction call path (``draft_bean_profile_from_url``) that can
#: change a drafted RESULT: preprocessing/extraction (``bean_sourcing``),
#: provider/model construction (``advisor.build_model``), config defaults
#: (``AdvisorConfig``/``BeanSourcingConfig``), and schema/validation
#: (``BeanProfileDraft``). INCLUSION RULE (#602): hash every first-party
#: module on that call path whose source can change a result. Third-party
#: dependencies are hashed SEPARATELY, by installed VERSION (see
#: :data:`_FINGERPRINTED_DEPENDENCIES`), not source -- their code is not
#: committed to this repo.
_FINGERPRINTED_MODULES: tuple[ModuleType, ...] = (
    _bean_sourcing_module,
    _advisor_module,
    _config_module,
    _models_module,
)

#: Third-party distribution names whose INSTALLED VERSION can change
#: extraction behaviour with no first-party source change (#602 fold round
#: 4): no lockfile, and pyproject's ranges (e.g. ``httpx>=0.28,<1``) admit a
#: compatible upgrade between invocations. Same INCLUSION RULE as
#: :data:`_FINGERPRINTED_MODULES`, applied to ``bean_sourcing.py``'s own
#: third-party imports instead of first-party source -- ``openai`` (fold
#: round 5) is the SDK behind ``pydantic-ai-slim[openai]``'s
#: ``OpenAIProvider``, one more hop down the same call path.
_FINGERPRINTED_DEPENDENCIES: tuple[str, ...] = (
    "httpx",
    "pydantic",
    "pydantic-ai-slim",
    "openai",
    "extruct",
    "trafilatura",
    "lxml",
)

# --- Constants ---------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The labelled corpus location (committed under tests/fixtures, the AGENTS.md
#: fixture exception).
DEFAULT_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "bean-sourcing"

# OPENROUTER_BASE_URL is imported from roastpilot_agent.config (the single
# canonical constant, #590 P2 fix) rather than duplicated here — this
# harness's own real (paid) run is deliberately, always OpenRouter, and
# bean_sourcing._resolve_extraction_model_slug now compares
# AdvisorConfig.provider_base_url against that SAME constant to decide
# whether an advisor is actually on OpenRouter.
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"


@dataclass(frozen=True)
class RosterModel:
    """One candidate model + its list price (research note section 4).

    Attributes:
        slug: The OpenRouter model slug.
        price_in_per_mtok: List input price, USD per 1M tokens.
        price_out_per_mtok: List output price, USD per 1M tokens.
        note: The shortlist rationale (report only).
    """

    slug: str
    price_in_per_mtok: float
    price_out_per_mtok: float
    note: str


#: The section-4 cost/quality-frontier shortlist for the (later, gated) paid
#: run. Prices are the note's list prices -- VERIFY in the OpenRouter dashboard
#: at run time (they drift). NOT run on import or under the self-test.
#: A one-shot bean-draft's extraction budget (seconds). Decoupled from the 10s
#: per-tick roast-advice default so slow/reasoning models are measured on quality,
#: not cut off; a user pasting a URL tolerates this. See make_sourcing_config
#: (the extraction-owning config, #590 slice A) and make_advisor_config.
BAKEOFF_EXTRACTION_TIMEOUT_S: float = 45.0

MODEL_ROSTER: tuple[RosterModel, ...] = (
    RosterModel("openai/gpt-5-nano", 0.05, 0.40, "cheapest frontier; the one to beat on price"),
    RosterModel("x-ai/grok-4.3", 0.20, 0.50, "grok-4-fast deprecated (404); 4.3 is the live slug"),
    RosterModel("google/gemini-3.1-flash-lite", 0.25, 1.00, "beats gpt-5-mini on 6/8 benches"),
    RosterModel("openai/gpt-5-mini", 0.25, 2.00, "ParseBench small-model reference; safe default"),
    RosterModel("openai/gpt-4.1-mini", 0.40, 1.60, "battle-tested strict-SO workhorse"),
    RosterModel("anthropic/claude-haiku-4.5", 1.00, 5.00, "best at deciding not to emit"),
    RosterModel("openai/gpt-5.6-luna", 1.00, 6.00, "strong text/table extraction (ParseBench)"),
    RosterModel("openai/gpt-4o", 2.50, 10.00, "ceiling only -- no extraction edge at 50x price"),
)


class Outcome(Enum):
    """A single ``(page, field)`` scoring outcome (research note section 5.1).

    A plain ``Enum`` (repo convention -- never ``StrEnum``), compared by
    identity, never string-compared. ``ERR`` is this harness's own addition for
    a WHOLE-PAGE extraction failure's gold-absent fields: it earns no
    abstention credit (a crash is not a correct abstention) and is excluded from
    every metric denominator, while the page's gold-PRESENT fields still score
    ``MIS`` (a real recall miss), so a crashing model is penalised on recall and
    never rewarded on faithfulness (the survivorship guard, research note
    section 5.3).
    """

    COR = "COR"
    PAR = "PAR"
    INC = "INC"
    MIS = "MIS"
    ABS_COR = "ABS_COR"
    SPU = "SPU"
    ERR = "ERR"


#: CombinedScore weights (research note section 5.1). ``ERR`` is excluded from
#: the mean entirely (see :func:`combined_score`).
_OUTCOME_WEIGHT: dict[Outcome, float] = {
    Outcome.COR: 1.0,
    Outcome.PAR: 0.5,
    Outcome.ABS_COR: 0.5,
    Outcome.MIS: 0.0,
    Outcome.INC: -0.5,
    Outcome.SPU: -1.0,
}

#: Free-text tasting-notes flavour-token recall at/above which the description
#: scores COR (a coarse overlap heuristic -- NOT an LLM-judge; the research note
#: reserves an equivalence judge for a richer pass, section 5.1). Below it but
#: above zero scores PAR.
_TASTING_COR_RECALL = 0.5

#: Word-bag recall at/above which a name/region/farm/variety scores COR.
_WORDBAG_COR_RECALL = 0.9

#: Word-bag PRECISION (fraction of the MODEL's own words found in gold) a
#: text/variety match must ALSO clear to earn COR -- gates hallucinated
#: additions (e.g. gold "Costa Rica" vs model "Costa Rica, Ethiopia" has
#: recall 1.0 but precision 0.67, and must not score full credit: a model
#: padding on extra unsupported tokens is a confabulation, not a correct
#: answer). Below this it is demoted to the ordinary PAR/INC path, never
#: silently rewarded as if faithful.
_WORDBAG_COR_PRECISION = 0.75

#: The tasting-notes analogue of :data:`_WORDBAG_COR_PRECISION`, looser
#: because free-text tasting prose legitimately carries more filler/connective
#: words around the flavour tokens than a name/region/variety match does.
_TASTING_COR_PRECISION = 0.3

#: Normalised-Levenshtein similarity at/above which two names score COR; the
#: PAR floor is :data:`_NAME_PAR_SIM`.
_NAME_COR_SIM = 0.9
_NAME_PAR_SIM = 0.6

#: Absolute (metres) and relative altitude tolerances for a SCALAR gold value.
_ALT_ABS_TOL_M = 5.0
_ALT_REL_TOL_COR = 0.02
_ALT_REL_TOL_PAR = 0.15

#: Process/species stop the model already constrains to a closed ``Literal``,
#: so the model value is canonical; the table canonicalises the GOLD label (and
#: any near-synonym) into the same closed set, and documents the mapping.
_PROCESS_SYNONYMS: dict[str, str] = {
    "washed": "washed",
    "fully washed": "washed",
    "wet": "washed",
    "wet process": "washed",
    "dry washed": "washed",
    "european prep": "washed",
    "natural": "natural",
    "dry": "natural",
    "dry process": "natural",
    "sun dried": "natural",
    "unwashed": "natural",
    "honey": "honey",
    "pulped natural": "honey",
    "white honey": "honey",
    "yellow honey": "honey",
    "red honey": "honey",
    "black honey": "honey",
    "miel": "honey",
    "anaerobic": "anaerobic",
    "anaerobic natural": "anaerobic",
    "anaerobic washed": "anaerobic",
    "carbonic maceration": "anaerobic",
    "co ferment": "anaerobic",
    "inoculated": "anaerobic",
    "innoculated": "anaerobic",
    "wet hulled": "wet_hulled",
    "giling basah": "wet_hulled",
    "semi washed": "wet_hulled",
}

#: Small coffee antonym set for the section-5.1 contradiction guard: a high
#: string similarity must NOT score COR/PAR when the two values are semantic
#: opposites (``washed`` vs ``unwashed``). Applied to free-text/name matches;
#: the closed enum fields already fail an antonym as a plain inequality.
_ANTONYM_PAIRS: tuple[frozenset[str], ...] = (
    frozenset({"washed", "unwashed"}),
    frozenset({"natural", "unnatural"}),
    frozenset({"decaf", "caffeinated"}),
    frozenset({"blend", "single"}),
)

_STOPWORDS: frozenset[str] = frozenset(
    {"and", "the", "of", "a", "an", "notes", "note", "variety", "varieties", "coffee", "with", "in"}
)

#: A curated coffee-flavour/cupping-descriptor vocabulary (superset of every
#: gold ``tasting_notes`` token in the committed corpus, plus common cupping
#: adjectives). Used to tell a real tasting-notes claim apart from a
#: ``description`` that only summarises process/lot details (#600 finding):
#: the production extraction prompt lets ``description`` cover EITHER, so a
#: faithful model that describes process/lot on a page with NO cupping prose
#: must not be scored as if it invented tasting notes. NOT exhaustive -- a
#: heuristic lexical gate, not an LLM judge (the research note reserves an
#: equivalence judge for a richer pass, section 5.1).
_TASTING_LEXICON: frozenset[str] = frozenset(
    {
        "acidity",
        "acidic",
        "almond",
        "apple",
        "apricot",
        "balanced",
        "berry",
        "berries",
        "bitter",
        "blackberry",
        "blueberry",
        "body",
        "bright",
        "brightness",
        "brown",
        "butterscotch",
        "caramel",
        "caramelized",
        "cherry",
        "chestnut",
        "chestnuts",
        "chocolate",
        "citrus",
        "clarity",
        "clean",
        "cocoa",
        "complex",
        "complexity",
        "creamy",
        "dark",
        "earthy",
        "floral",
        "fruit",
        "fruity",
        "grapefruit",
        "hazelnut",
        "herbal",
        # NOTE: deliberately NOT "honey" -- it is also the "honey" PROCESSING
        # method (_PROCESS_SYNONYMS), so a faithful process-only description
        # ("honey-processed lot...") would otherwise false-positive as a
        # tasting claim (#600 finding regression risk).
        "jasmine",
        "juicy",
        "lemon",
        "licorice",
        "lime",
        "malt",
        "mango",
        "maple",
        "molasses",
        "mouthfeel",
        "nutty",
        "orange",
        "peach",
        "pineapple",
        "plum",
        "raisin",
        "raspberry",
        "roasted",
        "rose",
        "silky",
        "smoky",
        "spice",
        "spicy",
        "strawberry",
        "sugar",
        "sweet",
        "sweetness",
        "syrup",
        "tart",
        "tea",
        "tealike",
        "toffee",
        "tropical",
        "vanilla",
        "velvety",
        "wine",
        "winey",
        "zest",
    }
)


# --- Corpus loading ----------------------------------------------------------


@dataclass(frozen=True)
class CorpusPage:
    """One labelled corpus page.

    Attributes:
        slug: The fixture stem (e.g. ``onyx-monarch-blend``).
        url: The vendor product URL the extractor is asked to draft from (also
            the key the mock transport serves the fixture bytes under).
        html: The captured page bytes, decoded to text.
        gold_fields: The per-field gold map: ``{field: {"value": ...}}`` or
            ``{field: {"absent": true}}`` (extra gold keys like
            ``roast_guidance`` are preserved but only the :data:`FIELD_SPECS`
            fields are scored).
        vendor: The vendor name (report display).
    """

    slug: str
    url: str
    html: str
    gold_fields: dict[str, dict[str, Any]]
    vendor: str


def _validate_gold_value_type(slug: str, spec: FieldSpec, value: Any) -> None:
    """Validate one field's ``value`` payload matches the type the scorer needs.

    Extends :func:`_validate_gold_shape` from mere KEY presence (``"value"``
    exists) to actual TYPE checking: ``{"value": null}`` used to pass the
    shape check and then crash in ``canon``/numeric conversion/range
    indexing partway through a paid run (#600 round-2 finding) -- every
    branch below rejects ``None`` (and every other wrong shape) the same
    way, so a null value is caught here regardless of field kind.

    Args:
        slug: The fixture stem (for the error message).
        spec: The field spec (its ``kind`` selects the expected shape).
        value: The candidate ``value`` payload.

    Raises:
        ValueError: If ``value`` does not match the shape ``spec.kind`` needs.
    """
    label = f"{slug}.gold.json: field {spec.name!r} (kind={spec.kind!r}) 'value'"
    if spec.kind in ("text", "enum"):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} must be a non-empty string, got {value!r}")
    elif spec.kind in ("variety", "tasting"):
        ok_scalar = isinstance(value, str) and bool(value.strip())
        ok_list = False
        if isinstance(value, list):
            items = cast("list[Any]", value)
            ok_list = bool(items) and all(isinstance(v, str) and v.strip() for v in items)
        if not (ok_scalar or ok_list):
            raise ValueError(
                f"{label} must be a non-empty string or a non-empty list of non-empty "
                f"strings, got {value!r}"
            )
    elif spec.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be a bool, got {value!r}")
    elif spec.kind == "altitude":
        if isinstance(value, dict):
            range_value = cast("dict[str, Any]", value)
            missing = [k for k in ("min_m", "max_m") if k not in range_value]
            if missing:
                raise ValueError(f"{label} RANGE is missing {missing}: {value!r}")
            for key in ("min_m", "max_m"):
                bound = range_value[key]
                if not isinstance(bound, (int, float)) or isinstance(bound, bool):
                    raise ValueError(f"{label} RANGE {key!r} must be numeric, got {bound!r}")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            raise ValueError(
                f"{label} must be a number or a {{'min_m', 'max_m'}} range dict, got {value!r}"
            )
    else:  # pragma: no cover - every FIELD_SPECS kind is handled above
        raise AssertionError(f"unhandled FieldSpec.kind {spec.kind!r} for {spec.name!r}")


def _validate_accept_any_of(slug: str, spec: FieldSpec, value: Any) -> None:
    """Validate an optional gold-ABSENT ``accept_any_of`` tolerance list.

    Runs at corpus-LOAD time, same as :func:`_validate_gold_value_type`, so
    a malformed list (e.g. a non-string entry) fails fast BEFORE any paid
    call, not partway through :func:`_tolerates_absent_value` (#602 fold 3).

    Args:
        slug: The fixture stem (for the error message).
        spec: The field spec (for the error message).
        value: The candidate ``accept_any_of`` payload.

    Raises:
        ValueError: If ``value`` is not a list of non-empty strings.
    """
    label = f"{slug}.gold.json: field {spec.name!r} 'accept_any_of'"
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a non-empty list of non-empty strings, got {value!r}")
    items = cast("list[Any]", value)
    if not items or not all(isinstance(v, str) and v.strip() for v in items):
        raise ValueError(f"{label} must be a non-empty list of non-empty strings, got {value!r}")


def _validate_gold_shape(slug: str, gold_fields: dict[str, dict[str, Any]]) -> None:
    """Validate every scored field has exactly one of ``{"value": ...}`` / ``{"absent": true}``.

    Runs at corpus-LOAD time (before any provider is built / any paid call is
    made), so a malformed custom ``--fixtures-dir`` gold record fails fast
    with a clear message instead of the run completing every paid model call
    and only then crashing in :func:`render_report`'s unconditional per-field
    indexing (#600 finding) -- extended in round 2 to also validate the
    ``value`` payload's TYPE (see :func:`_validate_gold_value_type`), not
    just that the ``"value"`` key exists, and in #602 (fold 3) to also
    validate an optional ``accept_any_of`` tolerance list on an ABSENT field
    (see :func:`_validate_accept_any_of`).

    Args:
        slug: The fixture stem (for the error message).
        gold_fields: The candidate ``field -> gold-state`` map.

    Raises:
        ValueError: If a required field is missing, has neither/both of
            ``"value"``/``"absent": true``, a present ``"value"`` has the wrong
            type/shape, ``"value"`` and ``accept_any_of`` are BOTH present (the
            latter is absent-only), ``accept_any_of`` is on a field whose
            classifier never consults it (see
            :data:`_ACCEPT_ANY_OF_ELIGIBLE_FIELDS`), or it is not a non-empty
            string list.
    """
    for spec in FIELD_SPECS:
        field = gold_fields.get(spec.name)
        if field is None:
            raise ValueError(
                f"{slug}.gold.json: missing required scored field {spec.name!r} "
                "(expected under 'fields', or top-level 'name' for the name field)"
            )
        has_value = "value" in field
        is_absent = field.get("absent") is True
        if has_value == is_absent:  # both True (ambiguous) or both False (neither given)
            raise ValueError(
                f"{slug}.gold.json: field {spec.name!r} must have exactly one of "
                "{'value': ...} or {'absent': true}, got " + repr(field)
            )
        if has_value:
            if "accept_any_of" in field:
                raise ValueError(
                    f"{slug}.gold.json: field {spec.name!r} has BOTH 'value' and "
                    "'accept_any_of' -- 'accept_any_of' is an ABSENT-only tolerance list, "
                    "not valid alongside a 'value' (#602 fold round 4)"
                )
            _validate_gold_value_type(slug, spec, field["value"])
        elif "accept_any_of" in field:
            if spec.name not in _ACCEPT_ANY_OF_ELIGIBLE_FIELDS:
                raise ValueError(
                    f"{slug}.gold.json: field {spec.name!r} (kind={spec.kind!r}) carries "
                    "'accept_any_of', but its classifier never consults it on an absent "
                    f"field -- only {sorted(_ACCEPT_ANY_OF_ELIGIBLE_FIELDS)} do"
                )
            _validate_accept_any_of(slug, spec, field["accept_any_of"])


def load_corpus(fixtures_dir: Path) -> list[CorpusPage]:
    """Load every ``<slug>.html`` + ``<slug>.gold.json`` pair under ``fixtures_dir``.

    The HTML is read as raw bytes and decoded WITHOUT universal-newline
    translation, so a captured fixture's exact bytes (including any CRLF line
    endings the vendor page actually used) reach the mock transport unchanged
    -- ``tests/fixtures/bean-sourcing/.gitattributes`` marks these files
    ``-text`` precisely so the committed bytes stay byte-exact, and a
    newline-normalizing read would silently violate that (#600 finding).

    Args:
        fixtures_dir: The corpus directory (``*.html`` + matching
            ``*.gold.json``).

    Returns:
        Every page, sorted by slug for a reproducible run/report order.

    Raises:
        FileNotFoundError: If a ``.gold.json`` has no matching ``.html``, or the
            directory has no fixtures at all.
        ValueError: If a gold record is missing a required scored field or has
            a malformed ``{"value"}``/``{"absent"}`` shape (see
            :func:`_validate_gold_shape`) -- raised before any provider is
            built, so a bad custom corpus never burns a paid call.
    """
    pages: list[CorpusPage] = []
    gold_paths = sorted(fixtures_dir.glob("*.gold.json"))
    if not gold_paths:
        raise FileNotFoundError(f"no *.gold.json corpus files under {fixtures_dir}")
    for gold_path in gold_paths:
        slug = gold_path.name[: -len(".gold.json")]
        html_path = fixtures_dir / f"{slug}.html"
        if not html_path.exists():
            raise FileNotFoundError(f"gold {gold_path.name} has no matching {slug}.html")
        gold = cast("dict[str, Any]", json.loads(gold_path.read_text()))
        provenance = cast("dict[str, Any]", gold.get("provenance", {}))
        gold_fields = dict(cast("dict[str, dict[str, Any]]", gold["fields"]))
        name_field = gold.get("name")
        if name_field is not None:
            gold_fields["name"] = cast("dict[str, Any]", name_field)
        _validate_gold_shape(slug, gold_fields)
        pages.append(
            CorpusPage(
                slug=slug,
                url=str(provenance["url"]),
                html=html_path.read_bytes().decode("utf-8", errors="replace"),
                gold_fields=gold_fields,
                vendor=str(provenance.get("vendor", "")),
            )
        )
    return pages


# --- Replay-only fetch seam --------------------------------------------------


async def _stream_bytes(data: bytes) -> AsyncGenerator[bytes, None]:
    """Yield ``data`` as a single-chunk async stream.

    The extractor's injected-client path reads the body via
    ``response.aiter_raw()`` (never ``aiter_bytes()``), which raises
    ``StreamConsumed`` on a pre-buffered ``content=<bytes>`` response -- so the
    mock body must be a genuine async stream, exactly as a real over-the-wire
    response is (mirrors ``tests/test_bean_sourcing.py``'s ``_bytes_stream``).

    Args:
        data: The response body bytes.

    Yields:
        The single body chunk.
    """
    yield data


def build_mock_client(url: str, html: str) -> httpx.AsyncClient:
    """An ``httpx.AsyncClient`` that serves ``html`` (streamed) for ``url``.

    Injected into :func:`draft_bean_profile_from_url` as its ``http_client``
    seam, so the real fetch+extract pipeline runs over the captured bytes with
    no network I/O. A request to any OTHER URL returns 404 (defensive -- the
    extractor only ever requests the one page URL).

    Args:
        url: The page URL the fixture is served under.
        html: The captured page HTML.

    Returns:
        The mock-transport-backed async client (the caller closes it).
    """
    body = html.encode("utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            return httpx.Response(200, content=_stream_bytes(body))
        return httpx.Response(404, content=_stream_bytes(b"not found"))  # pragma: no cover

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# --- Text / match helpers ----------------------------------------------------


def canon(value: str) -> str:
    """Canonicalise a string: strip accents, lowercase, collapse to ``a-z0-9`` words.

    Args:
        value: The raw string.

    Returns:
        The canonical form (accent-free, lowercase, single-spaced), or ``""``.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    lowered = re.sub(r"[^a-z0-9]+", " ", stripped.lower())
    return re.sub(r"\s+", " ", lowered).strip()


def words(value: str) -> set[str]:
    """The canonical, non-stopword word set of ``value``."""
    return {w for w in canon(value).split() if w and w not in _STOPWORDS}


def levenshtein(a: str, b: str) -> int:
    """Levenshtein edit distance between ``a`` and ``b`` (iterative DP)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (0 if ca == cb else 1))
            )
        previous = current
    return previous[-1]


def similarity(a: str, b: str) -> float:
    """Normalised Levenshtein similarity of two canonical strings (0..1)."""
    ca, cb = canon(a), canon(b)
    if not ca and not cb:
        return 1.0
    longest = max(len(ca), len(cb))
    if longest == 0:
        return 1.0
    return 1.0 - levenshtein(ca, cb) / longest


def has_contradiction(a: str, b: str) -> bool:
    """Whether ``a`` and ``b`` assert a known coffee antonym (section 5.1 guard)."""
    wa, wb = words(a), words(b)
    return any(
        (pair & wa) and (pair & wb) and (pair & wa) != (pair & wb) for pair in _ANTONYM_PAIRS
    )


def word_bag_recall(gold_tokens: Iterable[str], model_text: str) -> float:
    """Fraction of gold word-tokens present in ``model_text`` (0..1).

    Args:
        gold_tokens: The gold value token strings (each may be multi-word).
        model_text: The model's value text.

    Returns:
        ``|gold_words & model_words| / |gold_words|``, or ``0.0`` when the gold
        word set is empty.
    """
    gold_words: set[str] = set()
    for token in gold_tokens:
        gold_words |= words(token)
    if not gold_words:
        return 0.0
    return len(gold_words & words(model_text)) / len(gold_words)


def word_bag_precision(gold_tokens: Iterable[str], model_text: str) -> float:
    """Fraction of the MODEL's own words that appear in the gold token set (0..1).

    The bidirectional counterpart to :func:`word_bag_recall`: recall alone
    only checks that every gold word appears SOMEWHERE in the model text, so a
    model that pads the correct answer with extra, unsupported content (e.g.
    gold "Costa Rica" -> model "Costa Rica, Ethiopia") scores perfect recall
    while hallucinating an addition. This penalises that padding, gating COR
    eligibility in :func:`_compare_text`/:func:`_compare_variety`/
    :func:`_compare_tasting` (#600 finding).

    Args:
        gold_tokens: The gold value token strings (each may be multi-word).
        model_text: The model's value text.

    Returns:
        ``|gold_words & model_words| / |model_words|``, or ``1.0`` when the
        model text has no content words (nothing to penalise).
    """
    gold_words: set[str] = set()
    for token in gold_tokens:
        gold_words |= words(token)
    model_words = words(model_text)
    if not model_words:
        return 1.0
    return len(model_words & gold_words) / len(model_words)


# --- Field registry ----------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One scored field: how to pull the model's value + how it is matched.

    Attributes:
        name: The gold-field key (also the report/label key).
        kind: The match family (``text`` / ``enum`` / ``variety`` / ``tasting``
            / ``bool`` / ``altitude``).
        extract: Pulls this field's model value from a drafted profile (``None``
            == the model abstained on this field). Altitude classification is
            bespoke (needs provenance), so its ``extract`` is only used for the
            report, not for scoring.
    """

    name: str
    kind: str
    extract: Callable[[BeanProfileDraft], object | None]


def _extract_name(draft: BeanProfileDraft) -> str | None:
    return (draft.name or "").strip() or None


def _extract_origin(draft: BeanProfileDraft) -> str | None:
    return (draft.country or draft.bean_origin or "").strip() or None


def _extract_region(draft: BeanProfileDraft) -> str | None:
    """The model's sub-country region -- ``bean_origin`` ONLY when it is a
    distinct value from ``country`` (the extractor falls ``bean_origin`` back to
    ``country`` when the page gives one origin string, and that fallback is not
    a region the model actually read)."""
    bean_origin = (draft.bean_origin or "").strip()
    country = (draft.country or "").strip()
    if bean_origin and (not country or canon(bean_origin) != canon(country)):
        # When country is empty, _extract_origin already used bean_origin as the
        # origin, so there is no *separate* region to credit -> None.
        return bean_origin if country else None
    return None


def _extract_text(attr: str) -> Callable[[BeanProfileDraft], str | None]:
    def extract(draft: BeanProfileDraft) -> str | None:
        value = cast("str | None", getattr(draft, attr))
        return value.strip() or None if value else None

    return extract


FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("name", "text", _extract_name),
    FieldSpec("origin", "text", _extract_origin),
    FieldSpec("region", "text", _extract_region),
    FieldSpec("farm", "text", _extract_text("farm")),
    FieldSpec("variety", "variety", _extract_text("bean_varietal")),
    FieldSpec("process", "enum", lambda d: d.processing),
    FieldSpec("species", "enum", lambda d: d.bean_species),
    FieldSpec("altitude", "altitude", lambda d: d.altitude_m),
    FieldSpec("tasting_notes", "tasting", _extract_text("description")),
    FieldSpec("is_blend", "bool", lambda d: d.is_blend),
)

#: Field names whose :func:`classify_field` routing reaches
#: :func:`_classify_absent_field` on a gold-ABSENT state (#602 fold round 5, FOLD 4) --
#: ``altitude``/``tasting`` have their OWN bespoke absent-handling that never looks at
#: ``accept_any_of``; :func:`_validate_gold_shape` rejects it there at load time.
_ACCEPT_ANY_OF_ELIGIBLE_FIELDS: frozenset[str] = frozenset(
    spec.name for spec in FIELD_SPECS if spec.kind not in ("altitude", "tasting")
)


def _is_empty(value: object | None) -> bool:
    """Whether a model value counts as an abstention (``None`` or blank text)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _compare_text(gold: str, model: str) -> Outcome:
    if has_contradiction(gold, model):
        return Outcome.INC
    gc, mc = canon(gold), canon(model)
    if gc == mc:
        return Outcome.COR
    recall = word_bag_recall([gold], model)
    precision_ = word_bag_precision([gold], model)
    sim = similarity(gold, model)
    # A high similarity/recall alone is not enough for COR: it must ALSO
    # clear the precision gate, or a model padding the correct answer with
    # an unsupported extra token (e.g. "Costa Rica" -> "Costa Rica, Ethiopia")
    # would score full credit for a confabulated addition (#600 finding).
    cor_eligible = sim >= _NAME_COR_SIM or recall >= _WORDBAG_COR_RECALL
    if cor_eligible and precision_ >= _WORDBAG_COR_PRECISION:
        return Outcome.COR
    if sim >= _NAME_PAR_SIM or recall > 0.0 or (gc and mc and (gc in mc or mc in gc)):
        return Outcome.PAR
    return Outcome.INC


def _compare_enum(gold: str, model: str) -> Outcome:
    gold_canon = _PROCESS_SYNONYMS.get(canon(gold), canon(gold))
    model_canon = _PROCESS_SYNONYMS.get(canon(model), canon(model))
    return Outcome.COR if gold_canon == model_canon else Outcome.INC


def _gold_tokens(gold: Any) -> list[str]:
    """Normalise a gold value (a list of tokens, or a scalar) to ``list[str]``."""
    if isinstance(gold, list):
        return [str(t) for t in cast("list[Any]", gold)]
    return [str(gold)]


def _compare_variety(gold: Any, model: str) -> Outcome:
    tokens = _gold_tokens(gold)
    recall = word_bag_recall(tokens, model)
    precision_ = word_bag_precision(tokens, model)
    # Same bidirectional gate as _compare_text (#600 finding): a model that
    # names the correct cultivar(s) PLUS an unsupported extra one must not
    # earn full credit for the hallucinated addition.
    if recall >= _WORDBAG_COR_RECALL and precision_ >= _WORDBAG_COR_PRECISION:
        return Outcome.COR
    if recall > 0.0:
        return Outcome.PAR
    return Outcome.INC


def _compare_tasting(gold: Any, model: str) -> Outcome:
    tokens = _gold_tokens(gold)
    recall = word_bag_recall(tokens, model)
    precision_ = word_bag_precision(tokens, model)
    # Looser precision gate than text/variety (see _TASTING_COR_PRECISION):
    # free-text tasting prose legitimately carries connective/filler words a
    # single-value text match would not, but a description padded mostly with
    # unrelated content alongside the right flavour words still should not
    # earn full credit.
    if recall >= _TASTING_COR_RECALL and precision_ >= _TASTING_COR_PRECISION:
        return Outcome.COR
    if recall > 0.0:
        return Outcome.PAR
    return Outcome.INC


def _compare_bool(gold: object, model: object) -> Outcome:
    return Outcome.COR if bool(gold) == bool(model) else Outcome.INC


def _classify_altitude(gold_field: dict[str, Any], draft: BeanProfileDraft) -> Outcome:
    """Score altitude, honouring the section-5.1 RANGE contract.

    Gold RANGE (``{"min_m", "max_m"}``): COR only when the model returns a
    scalar inside the range AND flags it ``origin_estimated``; a scalar in range
    but tagged ``on_page`` (or one out of range) is INC (a present-value error,
    NOT SPU -- SPU stays reserved for gold-absent fields); an abstention is MIS.
    Gold SCALAR: COR within +/-5 m or +/-2%, PAR within +/-15%, else INC.

    Args:
        gold_field: This field's gold state (``{"value": ...}`` or
            ``{"absent": true}``).
        draft: The drafted profile.

    Returns:
        The altitude :class:`Outcome`.
    """
    model_value = draft.altitude_m
    if gold_field.get("absent") is True:
        return Outcome.ABS_COR if model_value is None else Outcome.SPU
    if model_value is None:
        return Outcome.MIS
    gold_value = gold_field["value"]
    flagged = draft.field_sources.get("altitude") == "origin_estimated" or (
        draft.field_sources.get("altitude_m") == "origin_estimated"
    )
    if isinstance(gold_value, dict):
        gold_range = cast("dict[str, Any]", gold_value)
        low = float(gold_range["min_m"])
        high = float(gold_range["max_m"])
        in_range = low - _ALT_ABS_TOL_M <= model_value <= high + _ALT_ABS_TOL_M
        return Outcome.COR if (in_range and flagged) else Outcome.INC
    target = float(cast("float | int", gold_value))
    delta = abs(model_value - target)
    if delta <= max(_ALT_ABS_TOL_M, _ALT_REL_TOL_COR * target):
        return Outcome.COR
    if delta <= _ALT_REL_TOL_PAR * target:
        return Outcome.PAR
    return Outcome.INC


def _is_tasting_claim(text: str) -> bool:
    """Whether ``text`` asserts actual flavour/cupping content, not just
    process/lot prose (see :data:`_TASTING_LEXICON`)."""
    return bool(words(text) & _TASTING_LEXICON)


def _classify_tasting(gold_field: dict[str, Any], draft: BeanProfileDraft) -> Outcome:
    """Score ``tasting_notes``, distinguishing a real tasting claim from prose.

    The production ``description`` field covers process/lot detail as well as
    tasting notes (``bean_sourcing.py``'s extraction instructions), so
    unconditionally treating any nonempty description as a tasting-notes
    answer would score a faithful process-only description as a spurious
    tasting-notes hallucination (SPU) on a page whose gold tasting notes are
    absent (#600 finding). A description is only treated as an attempted
    tasting-notes answer when it contains at least one recognised flavour
    token (:func:`_is_tasting_claim`); otherwise it is scored as an
    abstention on THIS field, exactly like an empty description.

    Args:
        gold_field: This field's gold state (``{"value": ...}`` or
            ``{"absent": true}``).
        draft: The drafted profile.

    Returns:
        The tasting-notes :class:`Outcome`.
    """
    raw = draft.description
    model_value = raw.strip() if raw and raw.strip() else None
    is_claim = model_value is not None and _is_tasting_claim(model_value)
    if gold_field.get("absent") is True:
        return Outcome.SPU if is_claim else Outcome.ABS_COR
    if not is_claim:
        return Outcome.MIS
    assert model_value is not None  # narrows for the comparator (is_claim implies non-None)
    return _compare_tasting(gold_field["value"], model_value)


_COMPARATORS: dict[str, Callable[[Any, Any], Outcome]] = {
    "text": _compare_text,
    "enum": _compare_enum,
    "variety": _compare_variety,
    "bool": _compare_bool,
}


def _tolerates_absent_value(tolerated: Sequence[str], model_value: str) -> bool:
    """Whether ``model_value`` matches ANY tolerated phrase on recall AND
    EXACT precision -- unlike :func:`_compare_text`'s fuzzy 0.75 gate (fit
    for free-text comparison), ``accept_any_of`` is a WHITELIST, so ANY
    unsupported token must reject the match: a fuzzy threshold admitted
    "blend of multiple origins Ethiopia" (precision exactly 0.75) as if it
    were a correct non-answer (#602 fold round 2). Precision must be
    ``1.0`` -- zero padding tolerated.

    Args:
        tolerated: The gold-JSON ``accept_any_of`` phrase list.
        model_value: The model's extracted (non-empty) text value.

    Returns:
        Whether any phrase matches on both axes.
    """
    for phrase in tolerated:
        recall = word_bag_recall([phrase], model_value)
        precision_ = word_bag_precision([phrase], model_value)
        if recall >= _WORDBAG_COR_RECALL and precision_ >= 1.0:
            return True
    return False


def _classify_absent_field(gold_field: dict[str, Any], model_value: object | None) -> Outcome:
    """Score a gold-ABSENT field, honouring an optional ``accept_any_of`` tolerance.

    A field is gold-ABSENT either because the page says nothing, or because it is
    absent-because-UNKNOWABLE -- e.g. a blend's ``origin`` has no single country, so a model
    answering "a blend of multiple origins" has not hallucinated a wrong one (#602 gold-label
    nuance). ``accept_any_of`` (an optional gold-JSON phrase list) marks those cases: a value
    matching ANY tolerated phrase on BOTH recall AND precision (:func:`_tolerates_absent_value`)
    scores ``ABS_COR`` -- same as a plain abstention. Anything else, including a padded
    tolerated phrase, still scores ``SPU``. Absent/empty ``accept_any_of`` is a no-op.

    Args:
        gold_field: The field's gold state (``{"absent": true, ...}``).
        model_value: The model's extracted value (``None``/blank counts as
            an abstention).

    Returns:
        ``ABS_COR`` (a correct abstention or a tolerated non-answer) or
        ``SPU`` (a genuinely spurious value).
    """
    if _is_empty(model_value):
        return Outcome.ABS_COR
    if isinstance(model_value, str):
        tolerated = cast("list[str]", gold_field.get("accept_any_of", []))
        if _tolerates_absent_value(tolerated, model_value):
            return Outcome.ABS_COR
    return Outcome.SPU


def classify_field(spec: FieldSpec, gold_field: dict[str, Any], draft: BeanProfileDraft) -> Outcome:
    """Classify one ``(field)`` of a successfully-drafted page (section 5.1).

    Args:
        spec: The field spec.
        gold_field: The field's gold state (``{"value": ...}`` /
            ``{"absent": true}``, the latter optionally carrying
            ``accept_any_of`` -- see :func:`_classify_absent_field`).
        draft: The drafted profile.

    Returns:
        The field :class:`Outcome`.
    """
    if spec.kind == "altitude":
        return _classify_altitude(gold_field, draft)
    if spec.kind == "tasting":
        return _classify_tasting(gold_field, draft)
    model_value = spec.extract(draft)
    if gold_field.get("absent") is True:
        return _classify_absent_field(gold_field, model_value)
    if _is_empty(model_value):
        return Outcome.MIS
    return _COMPARATORS[spec.kind](gold_field["value"], model_value)


def score_page(
    page: CorpusPage, draft: BeanProfileDraft | None, error: str | None
) -> dict[str, Outcome]:
    """Score every :data:`FIELD_SPECS` field of one page.

    On a whole-page extraction failure (``draft is None``), gold-PRESENT fields
    score ``MIS`` (a real recall miss) and gold-ABSENT fields score ``ERR`` (no
    abstention credit for a crash -- see :class:`Outcome`).

    Args:
        page: The corpus page.
        draft: The drafted profile, or ``None`` on extraction failure.
        error: The extraction error string, or ``None`` on success.

    Returns:
        A ``field -> Outcome`` map over :data:`FIELD_SPECS`.
    """
    outcomes: dict[str, Outcome] = {}
    for spec in FIELD_SPECS:
        gold_field = page.gold_fields.get(spec.name)
        if gold_field is None:
            continue  # pragma: no cover - every corpus page labels every spec field
        if error is not None or draft is None:
            outcomes[spec.name] = Outcome.MIS if "value" in gold_field else Outcome.ERR
        else:
            outcomes[spec.name] = classify_field(spec, gold_field, draft)
    return outcomes


# --- Running the pipeline over the corpus ------------------------------------


async def draft_for_page(
    page: CorpusPage,
    *,
    advisor_config: AdvisorConfig,
    model: Model | None = None,
    sourcing_config: BeanSourcingConfig | None = None,
) -> tuple[BeanProfileDraft | None, str | None]:
    """Run the real extractor over one captured page (replay-only, fail-soft).

    Args:
        page: The corpus page.
        advisor_config: The provider/key/model config (BYOK).
        model: An injected PydanticAI ``Model`` (the self-test seam); ``None``
            builds the real provider model from ``advisor_config`` (a paid
            call).
        sourcing_config: Fetch/extraction-limit config (#590 slice A: also
            selects the extraction model/timeout, not just the fetch); a
            default is built when omitted.

    Returns:
        ``(draft, None)`` on success, or ``(None, error_str)`` on any typed
        :class:`~roastpilot_agent.bean_sourcing.BeanSourcingError`.
    """
    client = build_mock_client(page.url, page.html)
    try:
        draft = await draft_bean_profile_from_url(
            page.url,
            advisor_config=advisor_config,
            sourcing_config=sourcing_config,
            http_client=client,
            model=model,
        )
        return draft, None
    except BeanSourcingError as exc:
        return None, f"{type(exc).__name__}: {exc}"
    finally:
        await client.aclose()


@dataclass(frozen=True)
class PageResult:
    """One page's scored outcome for one model.

    Attributes:
        slug: The page slug.
        outcomes: The per-field :class:`Outcome` map.
        error: The extraction error string, or ``None`` on success.
        on_page_fields: How many drafted fields the model tagged ``on_page``
            (provenance observability; ``0`` on a failed page).
        extracted: The drafted :class:`BeanProfileDraft`, JSON-serialised
            (``model_dump(mode="json")``), or ``None`` on a failed page.
            Persisted so a paid run can be AUDITED or RESCORED (a label or a
            match-function fix re-run offline) without re-calling the model
            (#600 finding) -- contains only bean-identity/roast-guidance
            values, nothing secret.
        elapsed_s: Wall-clock seconds for this page's draft attempt
            (success OR failure -- a censored timeout is itself latency
            signal). ``None`` only for a checkpoint record written before
            this field existed. Feeds the cost+latency tie-break the
            selection plan specifies for a statistical tie (#600 round-2
            finding): the 45s timeout alone can only identify a censored
            failure, not distinguish a 2s model from a 40s one.
    """

    slug: str
    outcomes: dict[str, Outcome]
    error: str | None
    on_page_fields: int
    extracted: dict[str, Any] | None = None
    elapsed_s: float | None = None


@dataclass(frozen=True)
class ModelRun:
    """A model's full scored run over the corpus.

    Attributes:
        model_slug: The model under test (or a fake's label).
        pages: Per-page results, in corpus order.
    """

    model_slug: str
    pages: list[PageResult]


async def run_model_over_corpus(
    pages: Sequence[CorpusPage],
    *,
    model_slug: str,
    advisor_config: AdvisorConfig,
    model: Model | None = None,
    sourcing_config: BeanSourcingConfig | None = None,
) -> ModelRun:
    """Draft + score every page for one model.

    Args:
        pages: The corpus.
        model_slug: The model's report label.
        advisor_config: The provider/key/model config.
        model: An injected ``Model`` (self-test); ``None`` = a real paid call.
        sourcing_config: Fetch/extraction-limit config (#590 slice A).

    Returns:
        The :class:`ModelRun`.
    """
    results: list[PageResult] = []
    for page in pages:
        started = time.monotonic()
        draft, error = await draft_for_page(
            page, advisor_config=advisor_config, model=model, sourcing_config=sourcing_config
        )
        elapsed_s = time.monotonic() - started
        on_page = (
            0 if draft is None else sum(1 for v in draft.field_sources.values() if v == "on_page")
        )
        results.append(
            PageResult(
                slug=page.slug,
                outcomes=score_page(page, draft, error),
                error=error,
                on_page_fields=on_page,
                extracted=None if draft is None else draft.model_dump(mode="json"),
                elapsed_s=elapsed_s,
            )
        )
    return ModelRun(model_slug=model_slug, pages=results)


# --- Metrics (section 5.1) ---------------------------------------------------


@dataclass(frozen=True)
class Counts:
    """Outcome tallies over some set of ``(page, field)`` cells."""

    cor: int = 0
    par: int = 0
    inc: int = 0
    mis: int = 0
    abs_cor: int = 0
    spu: int = 0
    err: int = 0


def tally(outcomes: Iterable[Outcome]) -> Counts:
    """Tally a stream of outcomes into :class:`Counts`."""
    buckets: dict[Outcome, int] = dict.fromkeys(Outcome, 0)
    for outcome in outcomes:
        buckets[outcome] += 1
    return Counts(
        cor=buckets[Outcome.COR],
        par=buckets[Outcome.PAR],
        inc=buckets[Outcome.INC],
        mis=buckets[Outcome.MIS],
        abs_cor=buckets[Outcome.ABS_COR],
        spu=buckets[Outcome.SPU],
        err=buckets[Outcome.ERR],
    )


def recall(counts: Counts) -> float | None:
    """Field recall ``(COR + 0.5 PAR) / (COR+INC+PAR+MIS)`` (``None`` if empty)."""
    denom = counts.cor + counts.inc + counts.par + counts.mis
    return (counts.cor + 0.5 * counts.par) / denom if denom else None


def precision(counts: Counts) -> float | None:
    """Faithfulness ``(COR + 0.5 PAR) / (COR+INC+PAR+SPU)`` (``None`` if empty)."""
    denom = counts.cor + counts.inc + counts.par + counts.spu
    return (counts.cor + 0.5 * counts.par) / denom if denom else None


def abstention_correctness(counts: Counts) -> float | None:
    """Abstention correctness ``ABS-COR / (ABS-COR + SPU)`` (``None`` if empty)."""
    denom = counts.abs_cor + counts.spu
    return counts.abs_cor / denom if denom else None


def f1(prec: float | None, rec: float | None) -> float | None:
    """Harmonic mean of precision + recall (``None`` if either is undefined)."""
    if prec is None or rec is None or (prec + rec) == 0.0:
        return None
    return 2.0 * prec * rec / (prec + rec)


def combined_score(outcomes: Iterable[Outcome]) -> float | None:
    """Mean CombinedScore over non-``ERR`` cells (``None`` if none scored)."""
    weights = [_OUTCOME_WEIGHT[o] for o in outcomes if o is not Outcome.ERR]
    return sum(weights) / len(weights) if weights else None


def all_outcomes(run: ModelRun) -> list[Outcome]:
    """Every ``(page, field)`` outcome in a run, flattened."""
    return [outcome for page in run.pages for outcome in page.outcomes.values()]


def field_outcomes(run: ModelRun, field_name: str) -> list[Outcome]:
    """Every page's outcome for one field."""
    return [page.outcomes[field_name] for page in run.pages if field_name in page.outcomes]


def macro_f1(run: ModelRun) -> float | None:
    """Mean per-field F1 (every field weighted equally -- the model-choice headline).

    A field is EXCLUDED from the average only when :func:`recall` is
    undefined for it (the corpus never had a gold-PRESENT cell for that
    field in this run -- not applicable, a data property, not a model
    behaviour). Whenever recall IS defined but :func:`f1` is ``None`` --
    which happens exactly when the model never returned a non-abstaining
    value for that field at all (``precision`` undefined) -- the field
    counts as F1 ``0.0``, not excluded: a model that improves its headline
    by always abstaining on a hard field must not be rewarded for dodging
    it (#600 finding).
    """
    per_field: list[float] = []
    for spec in FIELD_SPECS:
        counts = tally(field_outcomes(run, spec.name))
        rec = recall(counts)
        if rec is None:
            continue
        value = f1(precision(counts), rec)
        per_field.append(0.0 if value is None else value)
    return sum(per_field) / len(per_field) if per_field else None


def page_latencies(run: ModelRun) -> list[float]:
    """Every page's captured wall-clock elapsed seconds.

    Omits any page with no captured latency (``None`` -- only a checkpoint
    record written before latency capture existed).
    """
    return [p.elapsed_s for p in run.pages if p.elapsed_s is not None]


def latency_median_p95(run: ModelRun) -> tuple[float, float] | None:
    """``(median, p95)`` per-page latency in seconds, or ``None`` if unmeasured.

    The evaluation plan tie-breaks a statistical dead heat (see the
    page-clustered bootstrap CIs -- most pairs among the top models cross
    zero) on cost PLUS latency; the 45s extraction timeout alone can only
    flag a censored failure, not distinguish a fast model from a slow one
    (#600 round-2 finding). Reuses :func:`_percentile` (module-order-
    independent: called only after the whole module has loaded).
    """
    values = sorted(page_latencies(run))
    if not values:
        return None
    return _percentile(values, 0.5), _percentile(values, 0.95)


@dataclass(frozen=True)
class ModelMetrics:
    """A model's headline metrics over the corpus."""

    model_slug: str
    counts: Counts
    recall: float | None
    precision: float | None
    abstention: float | None
    micro_f1: float | None
    macro_f1: float | None
    combined_score: float | None
    page_errors: int
    median_latency_s: float | None
    p95_latency_s: float | None


def model_metrics(run: ModelRun) -> ModelMetrics:
    """Compute a model's headline metrics."""
    counts = tally(all_outcomes(run))
    prec = precision(counts)
    rec = recall(counts)
    latency = latency_median_p95(run)
    return ModelMetrics(
        model_slug=run.model_slug,
        counts=counts,
        recall=rec,
        precision=prec,
        abstention=abstention_correctness(counts),
        micro_f1=f1(prec, rec),
        macro_f1=macro_f1(run),
        combined_score=combined_score(all_outcomes(run)),
        page_errors=sum(1 for page in run.pages if page.error is not None),
        median_latency_s=latency[0] if latency else None,
        p95_latency_s=latency[1] if latency else None,
    )


# --- Evidence-quote capture summary (#612) -----------------------------------
#
# `BeanProfileDraft.field_evidence`/`field_sources` already ride along in
# every `PageResult.extracted` dump (`draft.model_dump(mode="json")` in
# `run_model_over_corpus`, and the checkpoint/artifact round trip via
# `run_to_json`/`_run_from_checkpoint` -- both persist the WHOLE draft, so no
# projection is needed). This section only adds a compact, honest SUMMARY of
# what was captured, for the extraction-quality/#612 review; it computes no
# new Outcome and changes no scoring.

#: The four TYPED fields `BeanProfileDraft.field_evidence` tracks quotes for
#: (#627/#633) -- these are the `field_evidence`/`field_sources` dict KEYS
#: (the draft's own attribute names), which for `processing`/`bean_species`/
#: `altitude_m` differ from the report-facing `FIELD_SPECS` names
#: (`process`/`species`/`altitude`); `is_blend` matches both.
TYPED_EVIDENCE_FIELDS: tuple[str, ...] = ("processing", "bean_species", "altitude_m", "is_blend")


@dataclass(frozen=True)
class TypedFieldEvidenceCounts:
    """Evidence-quote capture counts for one typed field, over a run's scored pages.

    Attributes:
        field_name: The ``field_evidence``/``field_sources`` key (see
            :data:`TYPED_EVIDENCE_FIELDS`).
        captured: Pages (with a draft) where an authenticated evidence quote
            was present in ``field_evidence`` for this field.
        no_evidence: Pages (with a draft) where no quote is present -- either
            the model gave none, or a quote was given but failed the
            authenticity check (#633) and was dropped; this count cannot
            distinguish the two.
    """

    field_name: str
    captured: int
    no_evidence: int


@dataclass(frozen=True)
class EvidenceSummary:
    """A run's quote-capture / authenticity-rate summary.

    **Quote capture/authenticity rates, NOT certification.** These counts
    describe how often the extractor captured or tagged something, not
    whether the underlying VALUE is correct -- the scored :class:`Outcome`
    tallies elsewhere are the correctness signal. Every automated citation
    VALUE gate for the four typed fields is permanently parked (#590), so
    ``field_evidence``/``field_sources`` exist for OPERATOR judgement, not
    automated certification; do not read this summary as a quality score.

    Attributes:
        model_slug: The model this summary is for.
        pages_scored: Pages with a persisted draft this summary is computed
            over (a whole-page extraction error has no draft to inspect and
            contributes nothing).
        typed_fields: Per-:data:`TYPED_EVIDENCE_FIELDS` evidence-capture
            counts.
        on_page_rate: Fraction of every ``field_sources`` entry (across every
            drafted bean-identity/target field, not just the typed four)
            tagged ``"on_page"`` over ``pages_scored`` -- ``None`` if no
            scored page had any ``field_sources`` entry at all.
    """

    model_slug: str
    pages_scored: int
    typed_fields: tuple[TypedFieldEvidenceCounts, ...]
    on_page_rate: float | None


def evidence_summary(run: ModelRun) -> EvidenceSummary:
    """Compute a run's #612 quote-capture / provenance summary.

    Args:
        run: The scored model run -- reads each page's persisted
            :attr:`PageResult.extracted` draft dump; a page with no draft
            (a whole-page extraction error) is skipped.

    Returns:
        The :class:`EvidenceSummary`.
    """
    scored_pages = [p.extracted for p in run.pages if p.extracted is not None]
    captured: dict[str, int] = dict.fromkeys(TYPED_EVIDENCE_FIELDS, 0)
    on_page = 0
    total_sources = 0
    for extracted in scored_pages:
        field_evidence = cast("dict[str, str]", extracted.get("field_evidence") or {})
        field_sources = cast("dict[str, str]", extracted.get("field_sources") or {})
        for field_name in TYPED_EVIDENCE_FIELDS:
            if field_name in field_evidence:
                captured[field_name] += 1
        total_sources += len(field_sources)
        on_page += sum(1 for v in field_sources.values() if v == "on_page")
    typed_fields = tuple(
        TypedFieldEvidenceCounts(
            field_name=field_name,
            captured=captured[field_name],
            no_evidence=len(scored_pages) - captured[field_name],
        )
        for field_name in TYPED_EVIDENCE_FIELDS
    )
    return EvidenceSummary(
        model_slug=run.model_slug,
        pages_scored=len(scored_pages),
        typed_fields=typed_fields,
        on_page_rate=(on_page / total_sources) if total_sources else None,
    )


# --- Statistics (section 5.2) ------------------------------------------------


def page_outcome_lists(run: ModelRun) -> dict[str, list[Outcome]]:
    """Every page's raw :class:`Outcome` list (cells kept per page, not averaged).

    Feeds :func:`paired_bootstrap_combined`, which needs to recompute the
    FLATTENED (cell-weighted) :func:`combined_score` within each page-cluster
    resample -- the same statistic the leaderboard shows -- rather than
    averaging equal-weighted per-page means (see that function's docstring
    for why those two statistics diverge, #600 finding).
    """
    return {page.slug: list(page.outcomes.values()) for page in run.pages}


def page_counts(run: ModelRun) -> dict[str, Counts]:
    """Per-page :class:`Counts` (for the page-clustered bootstrap of P/R/A)."""
    return {page.slug: tally(page.outcomes.values()) for page in run.pages}


@dataclass(frozen=True)
class BootstrapCI:
    """A paired-bootstrap point estimate + percentile interval.

    Attributes:
        estimate: The observed A-minus-B gap, or ``None`` when the metric is
            undefined for A or B (an empty denominator) -- rendered
            ``n/a``, never coerced to a fabricated ``0.0`` (#602 finding).
        low: The lower percentile bound (2.5%), or ``None`` under the same
            condition as ``estimate``.
        high: The upper percentile bound (97.5%); ``None`` likewise.
        resamples: How many bootstrap resamples had both sides defined.
    """

    estimate: float | None
    low: float | None
    high: float | None
    resamples: int


def _percentile(sorted_values: list[float], fraction: float) -> float:
    """Linear-interpolated percentile of a pre-sorted list."""
    if not sorted_values:  # pragma: no cover - guarded by callers
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = fraction * (len(sorted_values) - 1)
    lower = int(position)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = position - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def _sum_counts(counts: Iterable[Counts]) -> Counts:
    total = Counts()
    for c in counts:
        total = Counts(
            cor=total.cor + c.cor,
            par=total.par + c.par,
            inc=total.inc + c.inc,
            mis=total.mis + c.mis,
            abs_cor=total.abs_cor + c.abs_cor,
            spu=total.spu + c.spu,
            err=total.err + c.err,
        )
    return total


def paired_bootstrap_combined(
    run_a: ModelRun,
    run_b: ModelRun,
    *,
    resamples: int = 10_000,
    seed: int = 12345,
    ci: tuple[float, float] = (0.025, 0.975),
) -> BootstrapCI:
    """Page-clustered paired bootstrap on the CombinedScore gap (A - B).

    Resamples PAGES (not field-decisions) with replacement, so within-page
    correlation is respected (research note section 5.2 -- the PRIMARY test).
    Only pages both models scored are used. Within each resample, the gap is
    the FLATTENED (cell-weighted) :func:`combined_score` over every non-``ERR``
    cell across the resampled pages -- the SAME statistic the leaderboard's
    CombinedScore column reports -- not the mean of equal-weighted per-page
    averages, which diverges from it whenever pages have different scorable-
    cell counts (a whole-page failure turns gold-absent cells into excluded
    ``ERR`` outcomes, so cell counts DO vary across pages here; #600 finding).

    Args:
        run_a: Model A's run.
        run_b: Model B's run.
        resamples: Bootstrap resample count.
        seed: RNG seed (deterministic + reproducible).
        ci: The (low, high) percentile fractions.

    Returns:
        The :class:`BootstrapCI` on the flattened CombinedScore gap.

    Raises:
        ValueError: If the two runs share no scorable page.
    """
    outcomes_a = page_outcome_lists(run_a)
    outcomes_b = page_outcome_lists(run_b)
    shared = sorted(outcomes_a.keys() & outcomes_b.keys())
    if not shared:
        raise ValueError("runs share no scorable page for a paired bootstrap")

    def _flattened(picks: Sequence[str], outcomes: dict[str, list[Outcome]]) -> float | None:
        cells: list[Outcome] = []
        for slug in picks:
            cells.extend(outcomes[slug])
        return combined_score(cells)

    full_a = _flattened(shared, outcomes_a)
    full_b = _flattened(shared, outcomes_b)
    estimate = None if full_a is None or full_b is None else full_a - full_b
    rng = random.Random(seed)
    n = len(shared)
    gaps: list[float] = []
    for _ in range(resamples):
        picks = [shared[rng.randrange(n)] for _ in range(n)]
        sa = _flattened(picks, outcomes_a)
        sb = _flattened(picks, outcomes_b)
        if sa is not None and sb is not None:
            gaps.append(sa - sb)
    gaps.sort()
    return BootstrapCI(
        estimate=estimate,
        low=_percentile(gaps, ci[0]) if gaps else None,
        high=_percentile(gaps, ci[1]) if gaps else None,
        resamples=len(gaps),
    )


def paired_bootstrap_metric(
    run_a: ModelRun,
    run_b: ModelRun,
    metric_fn: Callable[[Counts], float | None],
    *,
    resamples: int = 10_000,
    seed: int = 12345,
    ci: tuple[float, float] = (0.025, 0.975),
) -> BootstrapCI:
    """Page-clustered paired bootstrap on a P/R/A metric gap (A - B).

    Resamples PAGES, re-aggregating each model's per-page :class:`Counts` within
    the resample before recomputing ``metric_fn`` -- so the fractional-PAR and
    within-page clustering the note warns Wilson ignores are respected here.

    Args:
        run_a: Model A's run.
        run_b: Model B's run.
        metric_fn: :func:`recall` / :func:`precision` /
            :func:`abstention_correctness`.
        resamples: Bootstrap resample count.
        seed: RNG seed.
        ci: The (low, high) percentile fractions.

    Returns:
        The :class:`BootstrapCI` on the metric gap. Resamples where either
        model's metric is undefined (empty denominator) are skipped.

    Raises:
        ValueError: If the two runs share no page.
    """
    counts_a = page_counts(run_a)
    counts_b = page_counts(run_b)
    shared = sorted(counts_a.keys() & counts_b.keys())
    if not shared:
        raise ValueError("runs share no page for a paired bootstrap")
    full_a = metric_fn(_sum_counts(counts_a[s] for s in shared))
    full_b = metric_fn(_sum_counts(counts_b[s] for s in shared))
    estimate = None if full_a is None or full_b is None else full_a - full_b
    rng = random.Random(seed)
    n = len(shared)
    gaps: list[float] = []
    for _ in range(resamples):
        picks = [shared[rng.randrange(n)] for _ in range(n)]
        ma = metric_fn(_sum_counts(counts_a[s] for s in picks))
        mb = metric_fn(_sum_counts(counts_b[s] for s in picks))
        if ma is not None and mb is not None:
            gaps.append(ma - mb)
    gaps.sort()
    return BootstrapCI(
        estimate=estimate,
        low=_percentile(gaps, ci[0]) if gaps else None,
        high=_percentile(gaps, ci[1]) if gaps else None,
        resamples=len(gaps),
    )


@dataclass(frozen=True)
class McNemarResult:
    """Exact-binomial McNemar on paired per-field correctness (SECONDARY).

    Attributes:
        a_only: Fields A got right and B got wrong.
        b_only: Fields B got right and A got wrong.
        discordant: ``a_only + b_only``.
        exact_p_two_sided: The exact two-sided p-value.
    """

    a_only: int
    b_only: int
    discordant: int
    exact_p_two_sided: float


def _is_correct(outcome: Outcome) -> bool:
    """A binary correct/incorrect view: a matched present value or a correct
    abstention is 'correct'; PAR/INC/MIS/SPU/ERR are 'incorrect'."""
    return outcome in (Outcome.COR, Outcome.ABS_COR)


def mcnemar_exact(run_a: ModelRun, run_b: ModelRun) -> McNemarResult:
    """Exact-binomial McNemar over paired ``(page, field)`` correctness.

    Uses the same exact form as ``scripts/advisor_significance.mcnemar_exact``
    (``2 * sum_{k=0..min(b,c)} C(n,k) * 0.5**n``). This is the SECONDARY,
    INDICATIVE check only: it treats each field-pair as independent, which the
    note's own clustering caveat says is false, so it OVERSTATES significance --
    never the deciding test where it disagrees with the page-clustered
    bootstrap (section 5.2).

    Args:
        run_a: Model A's run.
        run_b: Model B's run.

    Returns:
        The :class:`McNemarResult` over the fields both runs scored.
    """
    outcomes_a = {(p.slug, f): o for p in run_a.pages for f, o in p.outcomes.items()}
    outcomes_b = {(p.slug, f): o for p in run_b.pages for f, o in p.outcomes.items()}
    a_only = b_only = 0
    for key in outcomes_a.keys() & outcomes_b.keys():
        ca, cb = _is_correct(outcomes_a[key]), _is_correct(outcomes_b[key])
        if ca and not cb:
            a_only += 1
        elif cb and not ca:
            b_only += 1
    n = a_only + b_only
    if n == 0:
        exact_p = 1.0
    else:
        k = min(a_only, b_only)
        tail = sum(comb(n, i) for i in range(k + 1))
        exact_p = min(1.0, 2.0 * tail * (0.5**n))
    return McNemarResult(a_only=a_only, b_only=b_only, discordant=n, exact_p_two_sided=exact_p)


@dataclass(frozen=True)
class WilsonInterval:
    """A Wilson score interval on a strictly-binary proportion.

    Attributes:
        successes: COR count.
        trials: STRICTLY binary COR-vs-not (COR+INC+MIS; PAR excluded from both --
            #602 fold round 5, matching the research note's documented decomposition).
        proportion: ``successes / trials``, or ``None`` when 0 (rendered ``n/a``,
            never a fabricated ``0.000``, #602 fold round 4).
        low: Lower Wilson bound (degenerate ``0.0`` when ``trials`` is 0).
        high: Upper Wilson bound (degenerate ``1.0`` when ``trials`` is 0).
    """

    successes: int
    trials: int
    proportion: float | None
    low: float
    high: float


def wilson_interval(successes: int, trials: int, *, z: float = 1.959963984540054) -> WilsonInterval:
    """Wilson score interval for ``successes`` of ``trials`` (default 95%).

    The note reserves Wilson for a strictly-binary decomposition only
    (COR-vs-not, PAR excluded), reported as INDICATIVE given the within-page
    clustering it ignores (section 5.2).

    Args:
        successes: The success count.
        trials: The trial count.
        z: The standard-normal quantile (default 95%).

    Returns:
        The :class:`WilsonInterval` (``proportion=None`` and a degenerate
        ``[0, 1]`` bound when ``trials == 0``).
    """
    if trials == 0:
        return WilsonInterval(0, 0, None, 0.0, 1.0)
    phat = successes / trials
    denom = 1.0 + z * z / trials
    centre = (phat + z * z / (2 * trials)) / denom
    margin = (z * sqrt(phat * (1 - phat) / trials + z * z / (4 * trials * trials))) / denom
    return WilsonInterval(
        successes=successes,
        trials=trials,
        proportion=phat,
        low=max(0.0, centre - margin),
        high=min(1.0, centre + margin),
    )


def binary_cor_counts(run: ModelRun) -> tuple[int, int]:
    """``(COR, trials)`` for the strictly-binary COR-vs-not Wilson view.

    PAR is EXCLUDED from ``trials`` (#602 fold round 5): a partial match is neither a
    success nor a countable trial in this binary COR-vs-{INC,MIS} decomposition.
    """
    counts = tally(all_outcomes(run))
    trials = counts.cor + counts.inc + counts.mis
    return counts.cor, trials


# --- Cost estimate -----------------------------------------------------------


@dataclass(frozen=True)
class ModelCostEstimate:
    """A rough per-model paid-run cost estimate (for operator approval)."""

    slug: str
    input_tokens: int
    output_tokens: int
    usd: float


def _extract_prompt_text(page: CorpusPage) -> str:
    """The exact text the extractor would feed the model for ``page`` (post
    strip + 20k-char cap) -- imported lazily so the cost path is self-contained."""
    from roastpilot_agent.bean_sourcing import (  # noqa: PLC0415
        _extract_page_text,  # pyright: ignore[reportPrivateUsage]
    )

    return _extract_page_text(page.html)


def estimate_cost(
    pages: Sequence[CorpusPage], roster: Sequence[RosterModel]
) -> list[ModelCostEstimate]:
    """Estimate the paid bake-off's per-model cost (chars/4 token heuristic).

    Input tokens ~= (extracted page text + a fixed instruction/schema overhead)
    / 4 chars-per-token; output ~= a small fixed structured record. Prompt
    caching (the stable schema/instructions) makes the REAL cost lower -- this
    is a deliberately conservative upper estimate for approval, not a bill.

    Args:
        pages: The corpus.
        roster: The candidate models with list prices.

    Returns:
        One :class:`ModelCostEstimate` per roster model.
    """
    instruction_overhead_chars = 1600  # the extraction instructions + schema, roughly
    output_tokens_per_page = 220  # a small flat BeanProfileDraft record
    input_tokens = sum(
        (len(_extract_prompt_text(page)) + instruction_overhead_chars) // 4 for page in pages
    )
    output_tokens = output_tokens_per_page * len(pages)
    estimates: list[ModelCostEstimate] = []
    for entry in roster:
        usd = (
            input_tokens / 1_000_000 * entry.price_in_per_mtok
            + output_tokens / 1_000_000 * entry.price_out_per_mtok
        )
        estimates.append(
            ModelCostEstimate(
                slug=entry.slug,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=round(usd, 5),
            )
        )
    return estimates


def resolve_roster_for_slugs(model_slugs: Sequence[str]) -> list[RosterModel]:
    """Every requested slug's :class:`RosterModel`, in request order.

    ``--models`` accepts arbitrary OpenRouter slugs, but the spend guard
    (:func:`run_bakeoff`) is only as good as its cost estimate: a slug absent
    from :data:`MODEL_ROSTER` used to be silently dropped from the cost table,
    so :func:`run_bakeoff` resolved its estimated cost to ``$0`` and even
    ``--max-spend 0`` let it through (#600 finding). Refuses rather than
    guessing a price for an unrecognised slug -- add it to
    :data:`MODEL_ROSTER` with a real list price first.

    Args:
        model_slugs: The requested model slugs.

    Returns:
        One :class:`RosterModel` per slug, in ``model_slugs`` order.

    Raises:
        ValueError: If any slug has no :data:`MODEL_ROSTER` entry.
    """
    by_slug = {m.slug: m for m in MODEL_ROSTER}
    missing = [s for s in model_slugs if s not in by_slug]
    if missing:
        raise ValueError(
            f"no cost estimate for {missing} -- add to MODEL_ROSTER with a real list price "
            "before running (the spend guard refuses to run an unpriced model)"
        )
    return [by_slug[s] for s in model_slugs]


# --- The committed caveat text -----------------------------------------------

CAVEAT_TEXT = (
    "N is roughly 9 pages: this is a SCREENING harness, not certification. "
    "A perfect small-set score is a WARNING (an over-easy fixture or a "
    "mislabel), not a verdict. Prefer model A over B only where the "
    "page-clustered bootstrap CI on the CombinedScore (and on P/R/A) excludes "
    "zero AND the paired test agrees; otherwise choose on cost/latency. Field "
    "decisions are CLUSTERED within pages, so the effective N is well below the "
    "raw decision count -- the McNemar and Wilson figures treat field-pairs as "
    "independent and therefore OVERSTATE certainty; they are indicative only, "
    "and the page-clustered bootstrap is the primary test. RANGE-altitude COR "
    "is currently UNREACHABLE against the real, unmodified extractor (it never "
    "computes a range midpoint or tags altitude 'origin_estimated'), so the two "
    "RANGE-altitude pages (cbc-costa-rica-laminita-tarrazu, "
    "counterculture-concepcion-huista) cap altitude at MIS (a compliant abstention, "
    "weight 0) or INC (a leaked scalar, weight -0.5) regardless of model quality -- "
    "an asymmetric penalty whose effect on CombinedScore/macro-F1 ordering is NOT "
    "guaranteed uniform across the roster (a run where one model abstains and another "
    "leaks a scalar on these cells CAN shift the ranking; see the module docstring)."
)


# --- Report ------------------------------------------------------------------


def _fmt(value: float | None, digits: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _fmt_gap(ci: BootstrapCI) -> str:
    """Render a paired-bootstrap gap, preserving ``n/a`` for an undefined
    comparison (#602 finding) instead of the pre-fix fabricated ``0.000``."""
    if ci.estimate is None:
        return "n/a (undefined -- no comparable cells)"
    return f"{ci.estimate:+.3f} [{_fmt(ci.low)}, {_fmt(ci.high)}]"


def render_report(
    runs: Sequence[ModelRun],
    cost_estimates: Sequence[ModelCostEstimate],
    *,
    stopped_early: bool = False,
    unevaluated_slugs: Sequence[str] = (),
    failed_slugs: Sequence[FailedRun] = (),
    executed_slugs: Sequence[str] = (),
) -> str:
    """Render the markdown scorecard for one or more model runs.

    Args:
        runs: The scored model runs.
        cost_estimates: The per-model paid-run cost estimate (for the operator).
        stopped_early: Whether the spend guard stopped before evaluating every
            requested model (see :attr:`BakeoffResult.stopped_early`). A
            PARTIAL banner is rendered prominently at the top so this result
            cannot be mistaken for a completed roster comparison (#600
            finding).
        unevaluated_slugs: The requested models never run, when
            ``stopped_early`` is ``True``.
        failed_slugs: Every wholly-failed run this invocation, with its DISPLAY-ONLY
            heuristic label (:class:`FailedRun`) -- rendered as a separate banner; NEVER
            checkpointed, never counted in the leaderboard/stats below (#600 round-2;
            never-checkpoint simplification #602 fold round 5).
        executed_slugs: Models a REAL call was made for THIS invocation --
            distinguishes spend already INCURRED (still this harness's cost
            ESTIMATE, never verified OpenRouter billing -- #602) from a
            pre-run planning estimate (#600 round-2). Empty (the default)
            renders the cost section as a pure pre-run estimate.

    Returns:
        The markdown report text.
    """
    lines: list[str] = ["# #588 bean-sourcing extraction bake-off", ""]
    if stopped_early:
        lines.append(
            "> **PARTIAL RUN -- budget-stopped.** The `--max-spend` guard stopped before "
            f"evaluating {len(unevaluated_slugs)} requested model(s): "
            f"{', '.join(f'`{s}`' for s in unevaluated_slugs)}. The leaderboard below "
            "covers ONLY the models that finished; it is NOT a completed roster "
            "comparison -- re-run with a higher `--max-spend` (or `--no-resume` off, to "
            "resume) before treating any ranking here as final."
        )
        lines.append("")
    if failed_slugs:
        failed_desc = ", ".join(
            f"`{f.model_slug}` ({f.heuristic_label}, heuristic)" for f in failed_slugs
        )
        lines.append(
            f"> **EXCLUDED -- failed this invocation.** Every page errored for "
            f"{len(failed_slugs)} model(s): {failed_desc}. The heuristic label is DISPLAY-ONLY "
            "best-effort context (no invocation-local signal can truly tell a transient "
            "outage apart from a model-specific fault) -- NEVER checkpointed regardless of "
            "it, so a re-run ALWAYS retries them; excluded from every statistic below."
        )
        lines.append("")
    lines.append(f"- models scored: {len(runs)}")
    lines.append(f"- corpus pages: {len(runs[0].pages) if runs else 0}")
    lines.append("")
    lines.append(
        "## Per-model headline (macro F1 is the model-choice headline; latency is the "
        "cost/latency tie-break signal for a statistical tie)"
    )
    lines.append("")
    lines.append(
        "| Model | COR | PAR | INC | MIS | ABS-COR | SPU | ERR | Recall | Faithful | Abstain | "
        "micro F1 | macro F1 | Combined | latency p50/p95 (s) |"
    )
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
    for run in runs:
        m = model_metrics(run)
        c = m.counts
        latency = (
            f"{m.median_latency_s:.1f} / {m.p95_latency_s:.1f}"
            if m.median_latency_s is not None and m.p95_latency_s is not None
            else "n/a"
        )
        lines.append(
            f"| `{m.model_slug}` | {c.cor} | {c.par} | {c.inc} | {c.mis} | {c.abs_cor} | "
            f"{c.spu} | {c.err} | {_fmt(m.recall)} | {_fmt(m.precision)} | "
            f"{_fmt(m.abstention)} | {_fmt(m.micro_f1)} | {_fmt(m.macro_f1)} | "
            f"{_fmt(m.combined_score)} | {latency} |"
        )
    lines.append("")

    lines.append(
        "## Wilson intervals (indicative only, section 5.2 -- ignores within-page "
        "clustering, so it OVERSTATES certainty like McNemar; the bootstrap above is primary)"
    )
    lines.append("")
    lines.append("| Model | COR / trials | proportion | 95% Wilson CI |")
    lines.append("|---|--:|--:|--:|")
    for run in runs:
        cor, trials = binary_cor_counts(run)
        wi = wilson_interval(cor, trials)
        lines.append(
            f"| `{run.model_slug}` | {cor}/{trials} | {_fmt(wi.proportion)} | "
            f"[{_fmt(wi.low)}, {_fmt(wi.high)}] |"
        )
    lines.append("")

    for run in runs:
        lines.append(f"### `{run.model_slug}` -- per-page outcomes")
        lines.append("")
        header = "| Page | " + " | ".join(s.name for s in FIELD_SPECS) + " | err |"
        lines.append(header)
        lines.append("|---" * (len(FIELD_SPECS) + 2) + "|")
        for page in run.pages:
            cells = " | ".join(page.outcomes[s.name].value for s in FIELD_SPECS)
            lines.append(f"| {page.slug} | {cells} | {'yes' if page.error else ''} |")
        lines.append("")

    lines.append(
        "## Evidence-quote capture (#612) -- quote capture/authenticity rates, "
        "NOT certification: every typed-field citation VALUE gate stays permanently "
        "parked (#590); these counts describe what the extractor captured/tagged, not "
        "whether the value is correct"
    )
    lines.append("")
    lines.append("| Model | pages scored | field | captured | no evidence | on_page rate |")
    lines.append("|---|--:|---|--:|--:|--:|")
    for run in runs:
        summary = evidence_summary(run)
        on_page_rate = _fmt(summary.on_page_rate, digits=3)
        for i, field_counts in enumerate(summary.typed_fields):
            model_cell = f"`{summary.model_slug}`" if i == 0 else ""
            pages_cell = str(summary.pages_scored) if i == 0 else ""
            rate_cell = on_page_rate if i == 0 else ""
            lines.append(
                f"| {model_cell} | {pages_cell} | {field_counts.field_name} | "
                f"{field_counts.captured} | {field_counts.no_evidence} | {rate_cell} |"
            )
    lines.append("")

    if len(runs) >= 2:
        lines.append(
            "## Pairwise significance (ALL pairs, section 5.2) -- every comparison the "
            "selection relies on is generated here, not just one model versus the rest"
        )
        lines.append("")
        for base, other in itertools.combinations(runs, 2):
            boot = paired_bootstrap_combined(base, other)
            rec = paired_bootstrap_metric(base, other, recall)
            prec = paired_bootstrap_metric(base, other, precision)
            absn = paired_bootstrap_metric(base, other, abstention_correctness)
            mc = mcnemar_exact(base, other)
            lines.append(
                f"- `{base.model_slug}` vs `{other.model_slug}`: CombinedScore gap "
                f"{_fmt_gap(boot)} (page-clustered bootstrap -- PRIMARY); recall gap "
                f"{_fmt_gap(rec)}; faithfulness (precision) gap {_fmt_gap(prec)}; "
                f"abstention gap {_fmt_gap(absn)}; McNemar exact "
                f"p={mc.exact_p_two_sided:.4f} (secondary, indicative)."
            )
        lines.append("")

    cost_by_slug = {est.slug: est.usd for est in cost_estimates}
    executed_set = set(executed_slugs)
    scored_slugs = {r.model_slug for r in runs}
    resumed_set = scored_slugs - executed_set
    if executed_slugs:
        incurred_estimate = sum(cost_by_slug.get(s, 0.0) for s in executed_slugs)
        lines.append("## Cost (estimated spend incurred this invocation)")
        lines.append("")
        lines.append(
            f"**~${incurred_estimate:.4f} ESTIMATED SPEND INCURRED** this invocation, on "
            f"{len(executed_slugs)} newly-called model(s): "
            f"{', '.join(f'`{s}`' for s in executed_slugs)}. A real (paid) call WAS made for "
            "each -- but see the note below: this is still this harness's cost ESTIMATE, "
            "never a verified OpenRouter billing amount."
        )
    else:
        lines.append("## Cost (pre-run estimate -- NOT yet spent)")
        lines.append("")
        lines.append(
            "$0.0000 spent this invocation -- every scored model below was resumed from an "
            "existing checkpoint (no new paid calls) or this is a pre-run estimate."
        )
    lines.append("")
    lines.append("| Model | in tok | out tok | est. USD (full corpus, 1 pass) | status |")
    lines.append("|---|--:|--:|--:|---|")
    total = 0.0
    for est in cost_estimates:
        total += est.usd
        if est.slug in executed_set:
            status = "spend incurred (est.)"
        elif est.slug in resumed_set:
            status = "resumed (no new spend)"
        else:
            status = "not run"
        lines.append(
            f"| `{est.slug}` | {est.input_tokens} | {est.output_tokens} | ${est.usd:.4f} | "
            f"{status} |"
        )
    lines.append(
        f"| **roster total (1 pass each, every requested model)** | | | **${total:.4f}** | |"
    )
    lines.append("")
    lines.append(
        "Token counts use a chars/4 heuristic over the extractor's ACTUAL post-strip "
        "prompt text; prompt caching on the stable schema/instructions makes the real "
        "cost lower. This harness has NO live token-usage/billing readback, so EVERY USD "
        "figure above -- including a model marked 'spend incurred (est.)' -- is still this "
        "harness's pre-call ESTIMATE, never a verified OpenRouter billing amount: actual "
        "output/reasoning tokens, retries, and prompt caching can all make the real charge "
        "differ. A self-consistency vote (sample 3-5x) or a two-pass entailment judge would "
        "multiply these figures accordingly."
    )
    lines.append("")
    lines.append("## Caveat")
    lines.append("")
    lines.append(CAVEAT_TEXT)
    return "\n".join(lines)


def run_to_json(run: ModelRun) -> dict[str, Any]:
    """Serialise a model run + its metrics for the ``--out`` artifact.

    Includes ``evidence_summary`` (#612) -- the quote-capture/authenticity
    counts, NOT a certification signal (see :class:`EvidenceSummary`) --
    alongside the existing faithfulness/recall/abstention metrics. Every
    ``pages[*].extracted`` entry already carries the drafted
    ``field_sources``/``field_evidence`` maps verbatim (the full
    ``BeanProfileDraft`` dump), so no separate per-page projection is added.
    """
    m = model_metrics(run)
    summary = evidence_summary(run)
    return {
        "model_slug": run.model_slug,
        "metrics": {
            "counts": dataclasses.asdict(m.counts),
            "recall": m.recall,
            "precision": m.precision,
            "abstention": m.abstention,
            "micro_f1": m.micro_f1,
            "macro_f1": m.macro_f1,
            "combined_score": m.combined_score,
            "page_errors": m.page_errors,
            "median_latency_s": m.median_latency_s,
            "p95_latency_s": m.p95_latency_s,
        },
        "evidence_summary": dataclasses.asdict(summary),
        "pages": [
            {
                "slug": page.slug,
                "error": page.error,
                "on_page_fields": page.on_page_fields,
                "outcomes": {
                    field_name: outcome.value for field_name, outcome in page.outcomes.items()
                },
                "extracted": page.extracted,
                "elapsed_s": page.elapsed_s,
            }
            for page in run.pages
        ],
    }


# --- Checkpoint (resume) + cost guard ----------------------------------------


def sidecar_path(out: Path) -> Path:
    """The append-only per-``(model)`` checkpoint sidecar next to ``--out``."""
    return out.with_name(out.name + ".cells.jsonl")


def _pipeline_fingerprint() -> str:
    """A fingerprint of the EVALUATED PIPELINE (not the corpus).

    Hashes every module in :data:`_FINGERPRINTED_MODULES` (first-party source that can change
    a drafted result) plus this harness's OWN source, the extraction timeout, and every
    :data:`_FINGERPRINTED_DEPENDENCIES` entry's INSTALLED VERSION (#602 fold round 4 -- no
    lockfile, so a compatible third-party upgrade between invocations would otherwise change
    behaviour under an unchanged fingerprint and mix old/new records). Any change invalidates a
    stale checkpoint automatically (closes the #600 round-2 gap: #590-style preprocessing
    changes without touching any fixture).

    Returns:
        A short, stable hex digest of every fingerprinted module/dependency; ``""``
        (fingerprinting disabled) if ANY source file cannot be located -- degrading to the
        pre-existing corpus-only guard rather than crashing.
    """
    try:
        harness_source = inspect.getsourcefile(sys.modules[__name__])
        module_sources = [inspect.getsourcefile(m) for m in _FINGERPRINTED_MODULES]
        if harness_source is None or any(src is None for src in module_sources):
            return ""
        digest = hashlib.sha256()
        for src in module_sources:
            digest.update(Path(cast("str", src)).read_bytes())
        digest.update(Path(harness_source).read_bytes())
        digest.update(str(BAKEOFF_EXTRACTION_TIMEOUT_S).encode())
        for name in _FINGERPRINTED_DEPENDENCIES:
            try:
                installed_version = importlib.metadata.version(name)
            except importlib.metadata.PackageNotFoundError:
                installed_version = ""
            digest.update(f"{name}=={installed_version}".encode())
        return digest.hexdigest()[:16]
    except OSError:  # pragma: no cover - only when source is unavailable
        return ""


def compute_fingerprint(pages: Sequence[CorpusPage]) -> str:
    """A stable fingerprint of the corpus content AND the evaluated pipeline.

    Stored in every checkpoint record and compared on load: reusing an
    ``--out`` path after changing ``--fixtures-dir``, editing a page's HTML,
    relabelling gold values, OR changing the extraction/scoring pipeline
    itself (see :func:`_pipeline_fingerprint`) would otherwise silently
    resume old records and combine them with freshly-run models into one
    leaderboard whose entries were evaluated against DIFFERENT experiments
    (#600 finding, hardened in round 2 to also cover pipeline drift).

    Args:
        pages: The corpus the run will be scored against.

    Returns:
        A short, stable hex digest of every page's slug/url/html/gold_fields
        plus the current pipeline fingerprint.
    """
    payload = [
        {"slug": p.slug, "url": p.url, "html": p.html, "gold_fields": p.gold_fields} for p in pages
    ]
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    digest = hashlib.sha256(encoded)
    digest.update(_pipeline_fingerprint().encode())
    return digest.hexdigest()[:16]


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace ``path``'s content atomically via a same-directory temp file.

    A plain ``path.write_text(content)`` truncates THEN writes -- a crash in between leaves a
    partial/zero-byte file, destroying already-complete records a repair means to preserve
    (#602 fold 4). Write a sibling temp file, ``fsync`` it, then ``os.replace`` over ``path``:
    atomic, so the destination is always either the full OLD or full NEW content.

    Args:
        path: The destination file.
        content: The full text to write.
    """
    tmp_path = path.with_name(f"{path.name}.repair-{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)


def _load_checkpoint_lines(path: Path) -> list[dict[str, Any]]:
    """Parse the checkpoint sidecar, recovering from a truncated final line.

    A kill mid-``write`` can leave the LAST line an incomplete, newline-LESS JSON object (one
    ``write()`` call cut off before the trailing ``\\n``) -- unrepaired, the next invocation
    raises and loses every earlier, already-paid-for record too (#600). A malformed line
    elsewhere, OR a malformed FINAL line that DOES end with a newline (#602 fold round 4: that
    write completed, so it's genuine corruption, not an interrupted append), still raises for
    manual recovery -- only a newline-less tail is auto-repaired, ATOMICALLY (see
    :func:`_atomic_write_text`): a non-atomic rewrite would itself risk destroying recovered
    records on a second crash mid-repair.

    Args:
        path: The sidecar JSONL path.

    Returns:
        Every successfully-parsed record, in file order.

    Raises:
        json.JSONDecodeError: If a non-final line is malformed, or the final line is malformed
            but newline-terminated.
    """
    raw_text = path.read_text()
    lines = [ln for ln in raw_text.splitlines() if ln.strip()]
    records: list[dict[str, Any]] = []
    truncated_at: int | None = None
    for i, line in enumerate(lines):
        try:
            records.append(cast("dict[str, Any]", json.loads(line)))
        except json.JSONDecodeError:
            if i == len(lines) - 1 and not raw_text.endswith("\n"):
                truncated_at = i
                print(
                    f"[resume] ignoring a truncated final line in {path.name} "
                    f"(interrupted write) -- recovered {len(records)} earlier record(s)",
                    flush=True,
                )
                break
            raise  # non-final OR newline-terminated final line: real corruption, not repaired
    if truncated_at is not None:
        clean_lines = lines[:truncated_at]
        _atomic_write_text(path, "".join(f"{ln}\n" for ln in clean_lines))
    return records


class Checkpoint:
    """Append-only sidecar of completed per-model runs (resume support).

    Mirrors ``scripts/bakeoff_reference_567.Checkpoint``: each completed model's
    scored run is appended immediately, so a kill / budget stop / crash leaves
    every finished model recoverable, and a re-run with the same ``--out``
    skips models already on disk. Every appended record is fingerprinted
    against the CURRENT corpus (:func:`compute_fingerprint`); a record whose
    fingerprint does not match is treated as stale and NOT resumed (the
    model re-runs) -- see :data:`fingerprint`.
    """

    def __init__(self, path: Path, *, resume: bool = True, fingerprint: str = "") -> None:
        """Open (and optionally load) the sidecar.

        Args:
            path: The sidecar JSONL path.
            resume: Load + skip existing model runs when ``True``; truncate
                when ``False``.
            fingerprint: The current run's :func:`compute_fingerprint` value,
                stamped onto every newly-appended record and used to reject a
                stale resumed record from a different corpus/settings. An
                empty string (the default) disables the guard -- every test
                that does not care about staleness need not thread it
                through.
        """
        self.path = path
        self.fingerprint = fingerprint
        self._records: dict[str, dict[str, Any]] = {}
        if resume and path.exists():
            stale = 0
            for record in _load_checkpoint_lines(path):
                if fingerprint and record.get("fingerprint") not in (None, fingerprint):
                    stale += 1
                    continue
                self._records[str(record["model_slug"])] = record
            if stale:
                print(
                    f"[resume] ignored {stale} stale checkpoint record(s): corpus/fixtures "
                    "changed since they were written -- re-running those models",
                    flush=True,
                )
        elif not resume and path.exists():
            path.unlink()

    def has(self, model_slug: str) -> bool:
        """Whether ``model_slug`` is already complete on disk."""
        return model_slug in self._records

    def get(self, model_slug: str) -> dict[str, Any]:
        """The stored record for an already-complete model."""
        return self._records[model_slug]

    def append(self, record: dict[str, Any]) -> None:
        """Persist one completed model run to disk immediately, fingerprinted."""
        record = {**record, "fingerprint": self.fingerprint}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
            handle.flush()
        self._records[str(record["model_slug"])] = record


# --- .env key loading (the 401-shadowing ops gotcha) -------------------------


def load_dotenv_key(repo_root: Path, key: str = OPENROUTER_KEY_ENV) -> str | None:
    """Read ``key`` from the repo ``.env``, or ``None`` if absent.

    The caller OVERRIDES ``os.environ[key]`` with this value before building any
    advisor, so a stale shell export cannot shadow the ``.env`` key into a 401
    (see the module docstring / research note section 7).

    Args:
        repo_root: The repo root holding ``.env``.
        key: The variable to read.

    Returns:
        The value with surrounding quotes stripped, or ``None`` if ``.env`` is
        missing or has no such key.
    """
    env_path = repo_root / ".env"
    if not env_path.exists():
        return None
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == key:
            return value.strip().strip('"').strip("'")
    return None


# --- Orchestration + CLI -----------------------------------------------------


def make_advisor_config(model_slug: str) -> AdvisorConfig:
    """An OpenRouter-backed config pinning every phase to ``model_slug``.

    ``model_slug``/``timeout_seconds`` here drive the (unused, in this
    harness) roast-ADVICE path only — since #590 slice A,
    ``draft_bean_profile_from_url`` builds the EXTRACTION model/timeout
    from :func:`make_sourcing_config` instead (see its own docstring). Kept
    set here too (harmlessly) so this config stays representative of a real
    operator config pinning one model everywhere.
    """
    from roastpilot_agent.models import RoastPhase  # noqa: PLC0415

    return AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=OPENROUTER_BASE_URL,
        api_key_env=OPENROUTER_KEY_ENV,
        model_slug=model_slug,
        model_slug_by_phase={phase: model_slug for phase in RoastPhase},
        timeout_seconds=BAKEOFF_EXTRACTION_TIMEOUT_S,
    )


def make_sourcing_config(model_slug: str) -> BeanSourcingConfig:
    """The extraction model/timeout config for one bake-off roster slug.

    #590 slice A moved bean-identity EXTRACTION off
    ``AdvisorConfig.model_slug``/``timeout_seconds`` onto
    ``BeanSourcingConfig.model_slug``/``extraction_timeout_seconds`` (a
    one-shot bean draft must not silently ride whatever model/timeout the
    roast advisor happens to be configured with). This harness compares
    MANY models in one run by varying the slug under test per roster entry
    (see :func:`run_bakeoff`'s real, non-injected-``model`` path) — it must
    set the SAME slug explicitly on ``BeanSourcingConfig`` now, or every
    real (paid) run would silently resolve the extraction model
    provider-aware instead (``bean_sourcing._resolve_extraction_model_slug``,
    #590 P1 fix) — here that would still land on the bake-off's OpenRouter
    default (``"openai/gpt-5-mini"``, since :func:`make_advisor_config`
    always sets ``provider="openai_compatible"``), regardless of which
    roster model this run claims to be scoring.

    Args:
        model_slug: The roster model under test.

    Returns:
        A :class:`BeanSourcingConfig` pinning extraction to ``model_slug``,
        with the bake-off's own 45s extraction budget (matches the config
        default, kept explicit here for the same "representative operator
        config" reason as :func:`make_advisor_config`).
    """
    return BeanSourcingConfig(
        model_slug=model_slug,
        extraction_timeout_seconds=BAKEOFF_EXTRACTION_TIMEOUT_S,
    )


def _run_wholly_failed(run: ModelRun) -> bool:
    """Whether EVERY page of ``run`` errored (a total-outage, not a model finding)."""
    return bool(run.pages) and all(page.error is not None for page in run.pages)


def _has_any_success(run: ModelRun) -> bool:
    """Whether at least one page of ``run`` succeeded (extraction did not error)."""
    return any(page.error is None for page in run.pages)


@dataclass(frozen=True)
class FailedRun:
    """A wholly-failed run this invocation -- NEVER checkpointed (#602 fold round 5).

    Rounds 3-4 eagerly checkpointed a "provable" MODEL-SPECIFIC failure -- backwards AND
    unfixable, since no invocation-local signal can tell a transient outage apart from a
    model-specific fault. Failed attempts are CHEAP to retry; a mis-scored failure is
    EXPENSIVE to fix -- so every wholly-failed run is excluded and NEVER checkpointed.

    Attributes:
        model_slug: The failed model.
        heuristic_label: ``"MODEL-SPECIFIC"`` if a FRESHLY-EXECUTED peer had already succeeded
            this invocation, else ``"INFRA-WIDE"``. DISPLAY-ONLY best-effort context -- NOT
            authoritative, NEVER affects checkpointing or scoring.
    """

    model_slug: str
    heuristic_label: str


@dataclass(frozen=True)
class BakeoffResult:
    """The outcome of :func:`run_bakeoff`.

    Attributes:
        runs: The SCORED :class:`ModelRun` list (resumed + freshly run,
            EXCLUDING any wholly-failed run -- see :attr:`failed_slugs`).
        stopped_early: Whether the spend guard stopped BEFORE evaluating every
            requested model -- the result is PARTIAL, not a completed roster
            comparison (#600 finding).
        unevaluated_slugs: The requested model slugs never run at all because
            of the budget stop (empty when ``stopped_early`` is ``False``).
        failed_slugs: Every wholly-failed run this invocation, as a
            :class:`FailedRun` (model slug + DISPLAY-ONLY heuristic label). NEVER
            checkpointed (#602 fold round 5 -- see :class:`FailedRun`'s docstring
            for the trade), excluded from ``runs`` and every metric/leaderboard/
            pairwise statistic -- never a scored 0.000 row (#600 round-2) -- and a
            re-run always retries them.
        executed_slugs: Model slugs a REAL (paid) call was made for THIS
            invocation -- includes every :attr:`failed_slugs` entry (a paid
            attempt was still made) but excludes anything resumed from an
            existing checkpoint. Distinguishes spend already INCURRED (still
            this harness's cost ESTIMATE, never verified billing -- #602) from
            a pre-run planning estimate (#600 round-2).
    """

    runs: list[ModelRun]
    stopped_early: bool
    unevaluated_slugs: list[str]
    failed_slugs: list[FailedRun]
    executed_slugs: list[str]


async def run_bakeoff(
    pages: Sequence[CorpusPage],
    model_slugs: Sequence[str],
    *,
    out: Path,
    resume: bool,
    max_spend: float,
    cost_estimates: Sequence[ModelCostEstimate],
    model: Model | None = None,
) -> BakeoffResult:
    """Run + checkpoint every model over the corpus, under a spend guard.

    Read-only: never touches any store/DB, never saves a profile. Stops gracefully BEFORE a
    model whose estimated cost would breach ``max_spend`` (see :attr:`BakeoffResult.stopped_early`).
    A run where EVERY page errored is NEVER checkpointed (#602 fold round 5 -- see
    :class:`FailedRun`'s docstring for the trade this simplification resolves): it is reported
    as a :class:`FailedRun` with a DISPLAY-ONLY ``heuristic_label`` (``"MODEL-SPECIFIC"`` if a
    FRESHLY-EXECUTED peer had already succeeded this invocation, else ``"INFRA-WIDE"``) and a
    re-run ALWAYS retries it. Either way a wholly-failed run IS still counted against the spend
    guard (a paid attempt was made).

    Args:
        pages: The corpus.
        model_slugs: The models to run (real, paid calls).
        out: The JSON artifact path (anchors the checkpoint sidecar).
        resume: Skip models already checkpointed.
        max_spend: USD budget; a model is skipped once the running estimate
            would exceed it.
        cost_estimates: Per-model cost estimates (the spend guard's basis).
        model: An injected ``Model`` (the self-test seam); ``None`` = a real
            paid call, threaded through to :func:`run_model_over_corpus`.

    Returns:
        The :class:`BakeoffResult`.
    """
    cost_by_slug = {est.slug: est.usd for est in cost_estimates}
    fingerprint = compute_fingerprint(pages)
    checkpoint = Checkpoint(sidecar_path(out), resume=resume, fingerprint=fingerprint)
    runs: list[ModelRun] = []
    failed_runs: list[FailedRun] = []
    executed_slugs: list[str] = []
    has_fresh_success = False
    spent = 0.0
    for index, slug in enumerate(model_slugs):
        if checkpoint.has(slug):
            runs.append(_run_from_checkpoint(checkpoint.get(slug)))
            print(f"[resume] {slug}: on disk", flush=True)
            continue
        upcoming = cost_by_slug.get(slug, 0.0)
        if spent + upcoming > max_spend:
            print(
                f"[budget] stopping before {slug}: est. ${upcoming:.4f} would exceed "
                f"--max-spend ${max_spend:.2f} (spent est. ${spent:.4f})",
                flush=True,
            )
            return BakeoffResult(
                runs=runs,
                stopped_early=True,
                unevaluated_slugs=list(model_slugs[index:]),
                failed_slugs=failed_runs,
                executed_slugs=executed_slugs,
            )
        run = await run_model_over_corpus(
            pages,
            model_slug=slug,
            advisor_config=make_advisor_config(slug),
            model=model,
            sourcing_config=make_sourcing_config(slug),
        )
        spent += upcoming
        executed_slugs.append(slug)  # a real call was attempted, win or lose
        if _run_wholly_failed(run):
            label = "MODEL-SPECIFIC" if has_fresh_success else "INFRA-WIDE"
            print(
                f"[run] {slug}: ALL {len(run.pages)} page(s) errored -- {label} (heuristic, "
                "display-only) -- NEVER checkpointed, a re-run always retries it",
                flush=True,
            )
            failed_runs.append(FailedRun(model_slug=slug, heuristic_label=label))
            continue
        has_fresh_success = has_fresh_success or _has_any_success(run)
        checkpoint.append(run_to_json(run))
        m = model_metrics(run)
        print(
            f"[run] {slug}: combined={_fmt(m.combined_score)} macroF1={_fmt(m.macro_f1)} "
            f"errors={m.page_errors} (est. ${upcoming:.4f}, ${spent:.4f} total)",
            flush=True,
        )
        runs.append(run)
    return BakeoffResult(
        runs=runs,
        stopped_early=False,
        unevaluated_slugs=[],
        failed_slugs=failed_runs,
        executed_slugs=executed_slugs,
    )


def _run_from_checkpoint(record: dict[str, Any]) -> ModelRun:
    """Rebuild a :class:`ModelRun` from a checkpoint record."""
    pages: list[PageResult] = []
    for page in cast("list[dict[str, Any]]", record["pages"]):
        outcomes = {
            field_name: Outcome(value)
            for field_name, value in cast("dict[str, str]", page["outcomes"]).items()
        }
        elapsed_raw = page.get("elapsed_s")
        pages.append(
            PageResult(
                slug=str(page["slug"]),
                outcomes=outcomes,
                error=cast("str | None", page["error"]),
                on_page_fields=int(page["on_page_fields"]),
                extracted=cast("dict[str, Any] | None", page.get("extracted")),
                elapsed_s=None if elapsed_raw is None else float(cast("float", elapsed_raw)),
            )
        )
    return ModelRun(model_slug=str(record["model_slug"]), pages=pages)


def _finite_nonnegative_spend(raw: str) -> float:
    """``argparse`` ``type=`` for ``--max-spend``: a finite, non-negative USD figure.

    Plain ``type=float`` parses ``"inf"``/``"nan"``, and every
    ``spent + upcoming > max_spend`` guard in :func:`run_bakeoff` is FALSE
    against either -- silently bypassing the spend guard (#602). A negative
    limit is equally meaningless.

    Args:
        raw: The raw CLI argument string.

    Returns:
        The parsed, validated float.

    Raises:
        argparse.ArgumentTypeError: If ``raw`` does not parse as a finite,
            non-negative number.
    """
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--max-spend must be a number, got {raw!r}") from exc
    if not isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(
            f"--max-spend must be a finite, non-negative USD amount, got {raw!r}"
        )
    return value


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--fixtures-dir", type=Path, default=DEFAULT_FIXTURES_DIR, help="corpus directory"
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="model slug(s) to run (default: the full section-4 roster)",
    )
    parser.add_argument(
        "--max-spend",
        type=_finite_nonnegative_spend,
        default=None,
        help="REQUIRED USD budget for a real run; the run stops before a model whose "
        "estimated cost would breach it. No default -- an unbounded paid run is refused. "
        "Must be finite and non-negative (inf/nan/negative are rejected).",
    )
    parser.add_argument("--out", type=Path, default=Path("/tmp/bakeoff-bean-sourcing.json"))
    parser.add_argument("--report-md", type=Path, default=None)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--estimate-only",
        action="store_true",
        help="print the cost estimate for the roster + corpus and exit (zero spend, no key)",
    )
    return parser.parse_args(argv)


async def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for the (gated) paid bake-off.

    Args:
        argv: Optional argument vector.

    Returns:
        Process exit code (``0`` on success or a graceful budget stop).
    """
    args = _parse_args(argv)
    pages = load_corpus(cast("Path", args.fixtures_dir))
    roster_slugs = [m.slug for m in MODEL_ROSTER]
    model_slugs = cast("list[str] | None", args.models) or roster_slugs
    try:
        roster_for_cost = resolve_roster_for_slugs(model_slugs)
    except ValueError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    cost_estimates = estimate_cost(pages, roster_for_cost)

    if args.estimate_only:
        for est in cost_estimates:
            print(f"{est.slug}: ~${est.usd:.4f} ({est.input_tokens} in / {est.output_tokens} out)")
        print(f"roster total (1 pass each): ~${sum(e.usd for e in cost_estimates):.4f}")
        return 0

    if args.max_spend is None:
        print(
            "REFUSED: --max-spend is required for a real (paid) run. Use --estimate-only "
            "for a zero-spend cost estimate, or run tests/test_bakeoff_bean_sourcing.py for "
            "the deterministic no-spend wiring proof.",
            file=sys.stderr,
        )
        return 2

    dotenv_key = load_dotenv_key(_REPO_ROOT)
    if dotenv_key:
        os.environ[OPENROUTER_KEY_ENV] = dotenv_key
        print(f"[env] {OPENROUTER_KEY_ENV} loaded from .env and set explicitly (shadow-proof)")
    elif not os.environ.get(OPENROUTER_KEY_ENV):
        print(f"REFUSED: no {OPENROUTER_KEY_ENV} in .env or environment.", file=sys.stderr)
        return 2

    result = await run_bakeoff(
        pages,
        model_slugs,
        out=cast("Path", args.out),
        resume=not bool(args.no_resume),
        max_spend=float(cast("float", args.max_spend)),
        cost_estimates=cost_estimates,
    )
    report = render_report(
        result.runs,
        cost_estimates,
        stopped_early=result.stopped_early,
        unevaluated_slugs=result.unevaluated_slugs,
        failed_slugs=result.failed_slugs,
        executed_slugs=result.executed_slugs,
    )
    print("\n" + report, flush=True)

    out_path = cast("Path", args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(
            {
                "runs": [run_to_json(r) for r in result.runs],
                "stopped_early": result.stopped_early,
                "unevaluated_slugs": result.unevaluated_slugs,
                "failed_slugs": [dataclasses.asdict(f) for f in result.failed_slugs],
                "executed_slugs": result.executed_slugs,
            },
            indent=2,
        )
    )
    print(f"\nwrote artifact -> {out_path}", flush=True)
    if args.report_md is not None:
        report_path = cast("Path", args.report_md)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report)
        print(f"wrote markdown report -> {report_path}", flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint guard
    raise SystemExit(asyncio.run(main()))
