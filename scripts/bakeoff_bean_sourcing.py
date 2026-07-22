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

**Reasoning-effort arms (#601).** ``--reasoning {default,off,light,both}`` (default
``default``, behaviour/spend-preserving) adds a second study dimension: light
reasoning barely helps extraction quality but sharply improves schema adherence on
the cheapest models. Three DISTINCT arms (:data:`ReasoningArm`) -- "default"
(provider default, possibly-high effort) is NOT "off" (true no-reasoning), so
``"both"`` expands to "off"+"light" only. Each arm is checkpointed/reported under
its own :attr:`Arm.label`. Arms are skipped, with a printed note, per a roster
entry's :data:`RosterReasoningCapability` (FA/F7/F8).

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
import platform
import random
import re
import sys
import time
import unicodedata
import uuid
from collections.abc import AsyncGenerator, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from math import comb, isfinite, sqrt
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, cast

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # editable-install fallback

from pydantic_ai.models import Model  # noqa: E402

import roastpilot_agent.advisor as _advisor_module  # noqa: E402
import roastpilot_agent.bean_sourcing as _bean_sourcing_module  # noqa: E402
import roastpilot_agent.config as _config_module  # noqa: E402
import roastpilot_agent.models as _models_module  # noqa: E402
from roastpilot_agent.bean_sourcing import (  # noqa: E402
    _EXTRACTION_INSTRUCTIONS,  # pyright: ignore[reportPrivateUsage]
    BeanSourcingDiagnostics,
    BeanSourcingError,
    _ExtractedBeanIdentity,  # pyright: ignore[reportPrivateUsage]
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
#: dependencies are hashed CATEGORICALLY instead of a hand-picked list --
#: see :func:`_environment_fingerprint` (#602 fold round 6, FOLD 2).
_FINGERPRINTED_MODULES: tuple[ModuleType, ...] = (
    _bean_sourcing_module,
    _advisor_module,
    _config_module,
    _models_module,
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


#: A model's reasoning-capability class (#601 FA/F7): "none" skips off+light
#: ("default" only); "optional" gets both; "mandatory" REJECTS disabling reasoning
#: (HTTP 400, docs/advisor-bakeoff-2026-06-08.md:279-291) so only "light" runs;
#: "unknown" (unverified) skips BOTH like "none" -- "mandatory" needs CONFIRMED evidence.
RosterReasoningCapability = Literal["none", "optional", "mandatory", "unknown"]


@dataclass(frozen=True)
class RosterModel:
    """One candidate model + its list price (research note section 4).

    Attributes:
        slug: The OpenRouter model slug.
        price_in_per_mtok: List input price, USD per 1M tokens.
        price_out_per_mtok: List output price, USD per 1M tokens.
        note: The shortlist rationale (report only).
        reasoning: The model's :data:`RosterReasoningCapability`, gating
            :func:`expand_arms`'s off/light arms.
    """

    slug: str
    price_in_per_mtok: float
    price_out_per_mtok: float
    note: str
    reasoning: RosterReasoningCapability = "optional"


#: The section-4 cost/quality-frontier shortlist for the (later, gated) paid
#: run. Prices are the note's list prices -- VERIFY in the OpenRouter dashboard
#: at run time (they drift). NOT run on import or under the self-test.
#: A one-shot bean-draft's extraction budget (seconds). Decoupled from the 10s
#: per-tick roast-advice default so slow/reasoning models are measured on quality,
#: not cut off; a user pasting a URL tolerates this. See make_sourcing_config
#: (the extraction-owning config, #590 slice A) and make_advisor_config.
BAKEOFF_EXTRACTION_TIMEOUT_S: float = 45.0

#: The PLANNING estimate's heuristic (:func:`estimate_cost` only -- the
#: RESERVE's overhead is a separate DERIVED figure, see
#: :data:`_RESERVE_INSTRUCTION_OVERHEAD_BYTES`).
_INSTRUCTION_OVERHEAD_CHARS = 1600
_OUTPUT_TOKENS_PER_PAGE = 220  #: A small flat ``BeanProfileDraft`` record.

#: The bake-off's own enforced provider-side output cap (#601 fold round 4,
#: FOLD 4) -- passed as ``max_output_tokens`` on every real (paid) call, so
#: the reserve's output component bounds what the provider can actually
#: BILL, not a physical decode-rate guess. Structured output is ~220 tokens
#: (:data:`_OUTPUT_TOKENS_PER_PAGE`); a DEFAULT arm on a reasoning-mandatory
#: endpoint can genuinely bill several thousand -- 16384 clears that with
#: margin, under every roster model's own ceiling, uniform across every arm.
BAKEOFF_MAX_OUTPUT_TOKENS: int = 16384

#: Reasoning classification evidence (#601 FA/F7/F8, "mandatory" ONLY for a
#: CONFIRMED off-rejecting endpoint): gpt-5-mini is HTTP-400-on-disable per
#: docs/advisor-bakeoff-2026-06-08.md:279-291 -> "mandatory". gpt-5-nano's only
#: citation is a default-effort TIMEOUT (19 Jul bake-off, not a disable attempt),
#: and gemini-3.1-flash-lite's HTTP-400 evidence is for gemini-3.5-flash, a
#: DIFFERENT endpoint -> both "unknown", like grok-4.3 (no evidence either way).
#: gpt-4o/gpt-4.1-mini are classic non-reasoning ("none"). gpt-5.6-luna/claude-
#: haiku-4.5 are "optional" (off-as-no-op is genuine, both support light/thinking).
MODEL_ROSTER: tuple[RosterModel, ...] = (
    RosterModel("openai/gpt-5-nano", 0.05, 0.40, "cheapest; beat this on price", "unknown"),
    RosterModel("x-ai/grok-4.3", 0.20, 0.50, "grok-4-fast dead (404); 4.3 live", "unknown"),
    RosterModel("google/gemini-3.1-flash-lite", 0.25, 1.00, "beats gpt-5-mini 6/8", "unknown"),
    RosterModel("openai/gpt-5-mini", 0.25, 2.00, "ParseBench small-model reference", "mandatory"),
    RosterModel("openai/gpt-4.1-mini", 0.40, 1.60, "battle-tested strict-SO workhorse", "none"),
    RosterModel("anthropic/claude-haiku-4.5", 1.00, 5.00, "best at deciding not to emit"),
    RosterModel("openai/gpt-5.6-luna", 1.00, 6.00, "strong text/table extraction (ParseBench)"),
    RosterModel("openai/gpt-4o", 2.50, 10.00, "ceiling only, no extraction edge", "none"),
)


#: A study arm's ``reasoning_effort`` request (#601 F1). "default" omits the field
#: (provider default, possibly high -- NOT no-reasoning, never compared vs "light");
#: "off" is the true explicit no-reasoning request; "light" is the LOW effort tier.
ReasoningArm = Literal["default", "off", "light"]

_REASONING_EFFORT_BY_ARM: dict[ReasoningArm, Literal["off", "low"] | None] = {
    "default": None,
    "off": "off",
    "light": "low",
}

#: Suffixes marking an "off"/"light" arm's LABEL, distinct from the bare model slug
#: a "default" arm uses -- unchanged while ``--reasoning`` stays at its default.
_OFF_ARM_LABEL_SUFFIX = "+reasoning-off"
_LIGHT_ARM_LABEL_SUFFIX = "+reasoning-light"


@dataclass(frozen=True)
class Arm:
    """One (model, reasoning-effort) bake-off study arm (#601).

    Attributes:
        model_slug: The OpenRouter model slug -- drives provider construction and cost.
        reasoning: The study arm (see :data:`ReasoningArm`).
        label: The report/checkpoint identity: bare ``model_slug`` for "default",
            else ``model_slug`` + :data:`_OFF_ARM_LABEL_SUFFIX`/:data:`_LIGHT_ARM_LABEL_SUFFIX`.
    """

    model_slug: str
    reasoning: ReasoningArm
    label: str


def expand_arms(
    model_slugs: Sequence[str],
    reasoning: Literal["default", "off", "light", "both"],
    *,
    capability: Mapping[str, RosterReasoningCapability] | None = None,
) -> list[Arm]:
    """Expand requested model slugs into study arms per ``--reasoning`` (#601).

    Args:
        model_slugs: The requested model slugs, in request order.
        reasoning: ``"default"``/``"off"``/``"light"`` yields one arm per model;
            ``"both"`` yields "off"+"light" (never "default"), grouped per model.
        capability: Optional ``{model_slug: RosterReasoningCapability}`` map --
            ``"none"``/``"unknown"`` skip off+light (printed note); ``"mandatory"``
            skips off only; ``"optional"`` (or absent) gets both. "default" always runs.

    Returns:
        The expanded arm list.
    """
    cap = capability or {}
    arms: list[Arm] = []
    for slug in model_slugs:
        model_cap = cap.get(slug, "optional")
        if reasoning == "default":
            arms.append(Arm(model_slug=slug, reasoning="default", label=slug))
        if reasoning in ("off", "both"):
            if model_cap in ("none", "mandatory", "unknown"):
                reason = "unverified" if model_cap == "unknown" else f"reasoning is {model_cap!r}"
                print(f"[reasoning] skipping off arm for {slug!r}: {reason}")
            else:
                arms.append(
                    Arm(model_slug=slug, reasoning="off", label=slug + _OFF_ARM_LABEL_SUFFIX)
                )
        if reasoning in ("light", "both"):
            if model_cap in ("none", "unknown"):
                reason = "unverified" if model_cap == "unknown" else "not reasoning-capable"
                print(f"[reasoning] skipping light arm for {slug!r}: {reason}")
            else:
                arms.append(
                    Arm(model_slug=slug, reasoning="light", label=slug + _LIGHT_ARM_LABEL_SUFFIX)
                )
    return arms


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

    Runs at corpus-LOAD time, same as :func:`_validate_gold_value_type`, so a malformed list
    (e.g. a non-string entry) fails fast BEFORE any paid call (#602 fold 3). Every entry must
    also yield >=1 token under the SAME :func:`words` normalisation scoring uses (#602 fold
    round 8): a stopword/punctuation-only entry (``"the"``, ``"---"``) strips to zero tokens,
    so it can never match -- an advertised tolerance silently dead until a paid run reveals it.

    Args:
        slug: The fixture stem (for the error message).
        spec: The field spec (for the error message).
        value: The candidate ``accept_any_of`` payload.

    Raises:
        ValueError: If ``value`` isn't a list of non-empty strings, or any entry has no
            matchable token after :func:`words` normalisation.
    """
    label = f"{slug}.gold.json: field {spec.name!r} 'accept_any_of'"
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a non-empty list of non-empty strings, got {value!r}")
    items = cast("list[Any]", value)
    if not items or not all(isinstance(v, str) and v.strip() for v in items):
        raise ValueError(f"{label} must be a non-empty list of non-empty strings, got {value!r}")
    for item in cast("list[str]", items):
        if not words(item):
            raise ValueError(f"{label} entry {item!r} has no matchable token (never matches)")


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

#: FieldSpec kinds whose ``extract`` returns ``str | None`` -- the ONLY kinds
#: ``_classify_absent_field``'s ``isinstance(str)`` gate lets reach the tolerance check.
_STRING_PRODUCING_FIELD_KINDS: frozenset[str] = frozenset({"text", "enum", "variety"})

#: Field names eligible for ``accept_any_of``; rejected elsewhere at load time.
_ACCEPT_ANY_OF_ELIGIBLE_FIELDS: frozenset[str] = frozenset(
    spec.name for spec in FIELD_SPECS if spec.kind in _STRING_PRODUCING_FIELD_KINDS
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
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high"] | None = None,
    diagnostics: BeanSourcingDiagnostics | None = None,
    max_output_tokens: int = BAKEOFF_MAX_OUTPUT_TOKENS,
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
        reasoning_effort: Threaded through to ``draft_bean_profile_from_url`` (#601);
            ``None`` omits the setting.
        diagnostics: Optional accumulator, forwarded through (#601 F2).
        max_output_tokens: The ENFORCED provider-side output cap (#601 fold
            round 4, FOLD 4), passed to ``draft_bean_profile_from_url`` --
            :data:`BAKEOFF_MAX_OUTPUT_TOKENS` by default, every arm alike.

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
            reasoning_effort=reasoning_effort,
            diagnostics=diagnostics,
            max_output_tokens=max_output_tokens,
            disable_transport_retries=True,  # #601 fold round 8: exact request accounting
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
        recovered_violations: Validation-retry events the EXTRACTION step recovered
            from (#601 F2), independent of the page's final outcome -- a later
            ``_draft_from_identity`` rejection does NOT zero this (F7: extraction
            adherence is not draft policy). ``0`` if extraction never retried.
    """

    slug: str
    outcomes: dict[str, Outcome]
    error: str | None
    on_page_fields: int
    extracted: dict[str, Any] | None = None
    elapsed_s: float | None = None
    recovered_violations: int = 0


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
    reasoning_effort: Literal["off", "minimal", "low", "medium", "high"] | None = None,
    roster_price: RosterModel | None = None,
    ledger: ChargeLedger | None = None,
    max_output_tokens: int = BAKEOFF_MAX_OUTPUT_TOKENS,
) -> ModelRun:
    """Draft + score every page for one model.

    Args:
        pages: The corpus.
        model_slug: The run's report label (an :attr:`Arm.label`, not necessarily the
            bare provider slug -- see :func:`expand_arms`); also the ledger's ``arm``
            key (#601 fold round 1, slice A).
        advisor_config: The provider/key/model config.
        model: An injected ``Model`` (self-test); ``None`` = a real paid call.
        sourcing_config: Fetch/extraction-limit config (#590 slice A).
        reasoning_effort: Threaded to :func:`draft_for_page` per page (#601); ``None``
            omits the setting.
        roster_price: This arm's model price (#601 fold round 1, slice A); ``None``
            disables ledger writes entirely (existing/test callers unaffected).
        ledger: The invocation's :class:`ChargeLedger`; when given alongside
            ``roster_price``, a PENDING entry (reserve) is written BEFORE each
            page's call, then a FINAL entry supersedes it once the call
            completes (#601 fold round 4, FOLD 1) -- the token/spend store of
            record, :class:`PageResult` carries none of it.
        max_output_tokens: The ENFORCED provider-side output cap (#601 fold
            round 4, FOLD 4), threaded to :func:`draft_for_page` AND sized
            into the reserve -- the SAME value on both sides is what makes
            the reserve's worst case true.

    Returns:
        The :class:`ModelRun`.
    """
    results: list[PageResult] = []
    for page in pages:
        diagnostics = BeanSourcingDiagnostics()  # #601 F2: per-page retry count
        # A fresh id per ATTEMPT-CYCLE (#601 fold round 6, FOLD A) -- shared by
        # this page's pending + final entry, so a resumed re-attempt's charge
        # never collapses into a prior, GENUINELY separate call for the page.
        call_id = uuid.uuid4().hex
        # Computed BEFORE the billable call (#601 fold round 3, FOLD 2): the
        # failure handler must never re-parse, or a kill mid-timeout loses a
        # billed charge. `reserve` = full MULTI-attempt worst case (pending
        # entry, attempt count unknown yet); `single_attempt_reserve` = ONE
        # attempt's worst case (#601 fold round 6, FOLD D -- the final
        # entry's own addition, at most one request ever unreported).
        reserve = (
            _page_cost_reserve(page, roster_price, max_output_tokens=max_output_tokens)
            if roster_price is not None and ledger is not None
            else 0.0
        )
        single_attempt_reserve = (
            _single_attempt_reserve(page, roster_price, max_output_tokens=max_output_tokens)
            if roster_price is not None and ledger is not None
            else 0.0
        )
        if roster_price is not None and ledger is not None:
            # PENDING, charged at the reserve (#601 fold round 4, FOLD 1) --
            # written BEFORE the call so a kill mid-call leaves this page's
            # worst-case charge on the books; FINAL below supersedes it. A
            # kill during local preprocessing (no bytes sent) leaves only this
            # pending entry -- ACCEPTED (#601 review): fails safe, cents-scale.
            ledger.append(
                LedgerEntry(
                    arm=model_slug,
                    slug=page.slug,
                    request_tokens=0,
                    response_tokens=0,
                    priced_usd=round(reserve, 5),
                    timed_out=False,
                    reserve_applied=True,
                    is_pending=True,
                    call_id=call_id,
                )
            )
        # STARTED here, AFTER the reserve compute + pending write (#601 fold
        # round 5, D FOLD 1): elapsed_s feeds latency_median_p95()'s
        # cost+latency tie-break -- reserve/ledger overhead in that window
        # would corrupt the metric with non-provider latency.
        started = time.monotonic()
        draft, error = await draft_for_page(
            page,
            advisor_config=advisor_config,
            model=model,
            sourcing_config=sourcing_config,
            reasoning_effort=reasoning_effort,
            diagnostics=diagnostics,
            max_output_tokens=max_output_tokens,
        )
        elapsed_s = time.monotonic() - started
        if roster_price is not None and ledger is not None:
            # FINAL supersedes PENDING (#601 fold round 4, FOLD 1), appended
            # IMMEDIATELY once the call returns (#601 fold round 5, D FOLD 2)
            # -- before scoring, so a later raise there keeps the ACTUAL charge.
            timed_out = diagnostics.timed_out_runs > 0
            # #601 fold round 10: transport retries off (Refs slice E) means an
            # accepted-but-lost request surfaces as an INFRA (non-schema) error
            # with ZERO usage -- the same unreported-attempt risk as a timeout.
            zero_usage_infra_failure = (
                error is not None
                and not _is_schema_failure(error)
                and not timed_out
                and diagnostics.request_tokens == 0
                and diagnostics.response_tokens == 0
            )
            apply_reserve = timed_out or zero_usage_infra_failure
            priced = _actual_page_cost(
                diagnostics.request_tokens,
                diagnostics.response_tokens,
                roster_price,
                single_attempt_reserve,
                apply_reserve=apply_reserve,
            )
            ledger.append(
                LedgerEntry(
                    arm=model_slug,
                    slug=page.slug,
                    request_tokens=diagnostics.request_tokens,
                    response_tokens=diagnostics.response_tokens,
                    priced_usd=round(priced, 5),
                    timed_out=timed_out,
                    reserve_applied=apply_reserve,
                    is_pending=False,
                    call_id=call_id,
                )
            )
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
                # UNCONDITIONAL (#601 F7 -- round 3's zero-on-failed-page contract was
                # wrong): extraction adherence, independent of a later draft rejection.
                recovered_violations=diagnostics.schema_retries,
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


#: The message fragment for a validation-retry-exhausted (malformed structured
#: output) extraction -- the ONLY ``page.error`` cause that is a genuine
#: schema-adherence failure; every other cause (timeout, provider/fetch error, model
#: construction failure) must not be counted as one (#601 F1/F2).
_SCHEMA_FAILURE_MARKER = "returned a malformed shape"


def _is_schema_failure(error: str | None) -> bool:
    """Whether a page's error string is a schema/structured-output failure."""
    return error is not None and _SCHEMA_FAILURE_MARKER in error


@dataclass(frozen=True)
class ModelMetrics:
    """A model's headline metrics over the corpus.

    Attributes:
        page_errors: ``schema_failures + other_errors`` (kept for pre-#601 callers).
        schema_failures: Pages failing on a malformed structured-output shape --
            the schema-adherence proxy (see :func:`_is_schema_failure`).
        other_errors: Every OTHER page error -- NOT a schema-adherence signal.
        recovered_violations: Extraction-level retry events, summed over pages,
            independent of final page outcome (#601 F2/F7).
    """

    model_slug: str
    counts: Counts
    recall: float | None
    precision: float | None
    abstention: float | None
    micro_f1: float | None
    macro_f1: float | None
    combined_score: float | None
    page_errors: int
    schema_failures: int
    other_errors: int
    recovered_violations: int
    median_latency_s: float | None
    p95_latency_s: float | None


def model_metrics(run: ModelRun) -> ModelMetrics:
    """Compute a model's headline metrics."""
    counts = tally(all_outcomes(run))
    prec = precision(counts)
    rec = recall(counts)
    latency = latency_median_p95(run)
    schema_failures = sum(1 for page in run.pages if _is_schema_failure(page.error))
    other_errors = sum(
        1 for page in run.pages if page.error is not None and not _is_schema_failure(page.error)
    )
    return ModelMetrics(
        model_slug=run.model_slug,
        counts=counts,
        recall=rec,
        precision=prec,
        abstention=abstention_correctness(counts),
        micro_f1=f1(prec, rec),
        macro_f1=macro_f1(run),
        combined_score=combined_score(all_outcomes(run)),
        page_errors=schema_failures + other_errors,
        schema_failures=schema_failures,
        other_errors=other_errors,
        recovered_violations=sum(page.recovered_violations for page in run.pages),
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
    """``(COR, trials)``, strictly-binary COR-vs-{INC,MIS} (PAR excluded, #602)."""
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
    """The exact text the extractor would feed the model for ``page`` (post strip
    + 20k-char cap) -- imported lazily so the cost path is self-contained.

    Mirrors ``_fetch_page_text``'s REAL prompt assembly (#601 fold round 1): the
    trafilatura-first Markdown, falling back to the linear-strip pass, with the
    JSON-LD context prepend -- a JSON-LD-heavy page's context used to be invisible
    to every cost figure (the pre-run estimate AND the timeout-reserve floor)
    before this fix. Offline-callable, no network fetch: both underlying steps
    take ``html`` directly, and this harness already has ``page.html``.
    """
    from roastpilot_agent.bean_sourcing import (  # noqa: PLC0415
        _extract_page_markdown,  # pyright: ignore[reportPrivateUsage]
        _extract_page_text,  # pyright: ignore[reportPrivateUsage]
        _format_json_ld_context,  # pyright: ignore[reportPrivateUsage]
        _match_json_ld_product_facts,  # pyright: ignore[reportPrivateUsage]
    )

    extracted_text = _extract_page_markdown(page.html) or _extract_page_text(page.html)
    facts = _match_json_ld_product_facts(page.html, page.url)
    json_ld_context = _format_json_ld_context(facts) if facts is not None else None
    return extracted_text if json_ld_context is None else f"{json_ld_context}\n\n{extracted_text}"


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
    input_tokens = sum(
        (len(_extract_prompt_text(page)) + _INSTRUCTION_OVERHEAD_CHARS) // 4 for page in pages
    )
    output_tokens = _OUTPUT_TOKENS_PER_PAGE * len(pages)
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


#: Conservative ESTIMATE-only multiplier on a "light" arm's output tokens: reasoning
#: tokens bill as completion tokens, and this harness has no live usage readback --
#: chosen high so the spend guard never under-budgets a light call.
LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER: float = 4.0


def estimate_cost_for_arms(
    pages: Sequence[CorpusPage], arms: Sequence[Arm], roster: Sequence[RosterModel]
) -> list[ModelCostEstimate]:
    """Per-ARM cost estimate (#601), built on top of :func:`estimate_cost`.

    "default" is IDENTICAL to :func:`estimate_cost`'s per-model figure (bare-slug
    label, so the CLI default reproduces its numbers exactly). "off" is priced the
    SAME as "default" (explicit no-reasoning costs no more than omitted), just
    relabelled distinct. "light" multiplies output tokens by
    :data:`LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER`. Every arm is labelled under
    :attr:`Arm.label` so a model's several arms are distinct report rows.

    Args:
        pages: The corpus.
        arms: The expanded arm list (see :func:`expand_arms`).
        roster: The priced roster the arms' model slugs resolve against.

    Returns:
        One :class:`ModelCostEstimate` per arm, keyed by :attr:`Arm.label`.
    """
    per_model = {est.slug: est for est in estimate_cost(pages, roster)}
    price_by_slug = {entry.slug: entry for entry in roster}
    estimates: list[ModelCostEstimate] = []
    for arm in arms:
        base = per_model[arm.model_slug]
        if arm.reasoning in ("default", "off"):
            estimates.append(dataclasses.replace(base, slug=arm.label))
            continue
        output_tokens = round(base.output_tokens * LIGHT_REASONING_OUTPUT_TOKEN_MULTIPLIER)
        price = price_by_slug[arm.model_slug]
        usd = (
            base.input_tokens / 1_000_000 * price.price_in_per_mtok
            + output_tokens / 1_000_000 * price.price_out_per_mtok
        )
        estimates.append(
            ModelCostEstimate(
                slug=arm.label,
                input_tokens=base.input_tokens,
                output_tokens=output_tokens,
                usd=round(usd, 5),
            )
        )
    return estimates


#: Worst-case input-token bound for the RESERVE (#601 fold round 3): 1 token per
#: BYTE, never a code point (an emoji/CJK char spans several UTF-8 bytes, and
#: byte-level BPE emits at most one token per byte). A SAFETY FLOOR, not the
#: chars/4 PLANNING heuristic :func:`estimate_cost` uses.
_RESERVE_BYTES_PER_TOKEN = 1

#: The RESERVE's instruction+schema overhead, DERIVED from the ACTUAL runtime
#: constants at import time (#601 fold round 3) -- not a guess like
#: :data:`_INSTRUCTION_OVERHEAD_CHARS`. +500 bytes margin for provider-specific
#: wire-framing this harness cannot observe.
_RESERVE_INSTRUCTION_OVERHEAD_BYTES = (
    len(_EXTRACTION_INSTRUCTIONS.encode("utf-8"))
    + len(json.dumps(_ExtractedBeanIdentity.model_json_schema()).encode("utf-8"))
    + 500
)

#: A generous structural-inflation factor on the reserve's PAGE-TEXT bytes
#: (#601 fold round 6, FOLD C) -- markdown syntax linear-strip never emits
#: could otherwise exceed its byte count with no added content, breaking
#: the "linear-strip is longer" claim FOLD 4 relied on -- without
#: reintroducing trafilatura (:func:`_reserve_prompt_text` never touches it).
_RESERVE_STRUCTURAL_INFLATION = 2

#: A small, fixed retry-prompt-WRAPPER overhead only (#601 fold round 8) --
#: pydantic-ai's ``RetryPromptPart`` wraps a validation failure in a short,
#: fixed "please fix this" instruction; the larger SERIALIZED error quoting
#: the offending output back is priced separately, a second
#: ``max_output_tokens``-sized term (see :func:`_page_cost_reserve`).
_RESERVE_RETRY_WRAPPER_TOKENS = 200

#: Worst-case UTF-8 bytes/char (#601 fold round 7, FOLD 2) -- a code point can
#: span up to 4 UTF-8 bytes; converts the runtime's CHARACTER-based
#: extraction cap (below) into a worst-case BYTE floor (matches
#: :data:`_RESERVE_BYTES_PER_TOKEN`'s own philosophy).
_RESERVE_MAX_BYTES_PER_CHAR = 4


def _reserve_prompt_text(page: CorpusPage) -> str:
    """The prompt text for the RESERVE floor (#601 fold round 4) -- ALWAYS
    the linear-strip pass + JSON-LD context, never trafilatura's markdown:
    trafilatura can produce SHORTER text, UNDERSTATING the reserve, so the
    reserve wants the LONGER candidate outright (no parse-pool budget needed).
    """
    from roastpilot_agent.bean_sourcing import (  # noqa: PLC0415
        _extract_page_text,  # pyright: ignore[reportPrivateUsage]
        _format_json_ld_context,  # pyright: ignore[reportPrivateUsage]
        _match_json_ld_product_facts,  # pyright: ignore[reportPrivateUsage]
    )

    extracted_text = _extract_page_text(page.html)
    facts = _match_json_ld_product_facts(page.html, page.url)
    json_ld_context = _format_json_ld_context(facts) if facts is not None else None
    return extracted_text if json_ld_context is None else f"{json_ld_context}\n\n{extracted_text}"


def _reserve_input_tokens_per_attempt(page: CorpusPage) -> int:
    """ONE attempt's worst-case input-token bound: ``max`` of two candidates
    (#601 fold round 6/7, FOLD C + FOLD 2), plus the DERIVED instruction+
    schema overhead (never inflated) -- 2x-inflated reserve-text bytes
    (:data:`_RESERVE_STRUCTURAL_INFLATION`, covering markdown punctuation),
    OR the runtime's ``bean_sourcing._MAX_EXTRACTED_CHARS`` cap (never
    forked) as worst-case bytes + JSON-LD bytes -- a table-heavy page's
    markdown can sit near that cap while linear-strip is barely half of it.
    """
    from roastpilot_agent.bean_sourcing import (  # noqa: PLC0415
        _MAX_EXTRACTED_CHARS,  # pyright: ignore[reportPrivateUsage]
        _extract_page_text,  # pyright: ignore[reportPrivateUsage]
    )

    prompt_text = _reserve_prompt_text(page)
    prompt_bytes = len(prompt_text.encode("utf-8"))
    linear_bytes = len(_extract_page_text(page.html).encode("utf-8"))
    json_ld_bytes = prompt_bytes - linear_bytes  # 0 when no JSON-LD context was prepended
    inflated_strip_bytes = prompt_bytes * _RESERVE_STRUCTURAL_INFLATION
    markdown_cap_bytes = _MAX_EXTRACTED_CHARS * _RESERVE_MAX_BYTES_PER_CHAR + json_ld_bytes
    input_bytes = (
        max(inflated_strip_bytes, markdown_cap_bytes) + _RESERVE_INSTRUCTION_OVERHEAD_BYTES
    )
    return input_bytes // _RESERVE_BYTES_PER_TOKEN


def _page_cost_reserve(
    page: CorpusPage, price: RosterModel, *, max_output_tokens: int = BAKEOFF_MAX_OUTPUT_TOKENS
) -> float:
    """THIS page's PHYSICALLY-BOUNDED, WORST-CASE, MULTI-ATTEMPT timeout-reserve
    floor (#601 fold rounds 1-8) -- for the PENDING entry, before anything is
    known about how many attempts will occur.

    Total worst-case REQUEST count is ``1 + EXTRACTION_MAX_RETRIES`` (#601
    fold round 8: paid calls now run with ``disable_transport_retries=True``,
    Refs slice E -- no separate transport factor needed). Output =
    ``max_output_tokens * total_requests``.

    Input (#601 fold round 6/8, FOLD B revised) = every VALIDATION attempt
    re-sending the prompt, plus each RETRY additionally re-sending the PRIOR
    RESPONSE AND its own SERIALIZED validation-error copy of it
    (``RetryPromptPart`` quotes the offending output back, #601 fold round
    8 -- a second ``max_output_tokens``-sized term) plus the small, fixed
    :data:`_RESERVE_RETRY_WRAPPER_TOKENS` (see :func:`_single_attempt_reserve`
    for the final entry's smaller bound).

    Args:
        page: The corpus page (its extracted prompt text drives the input estimate).
        price: The arm's model pricing.
        max_output_tokens: The ENFORCED per-request output cap actually passed
            to the real call (:data:`BAKEOFF_MAX_OUTPUT_TOKENS` by default).

    Returns:
        The page's physically-bounded, worst-case, multi-attempt estimated USD cost.
    """
    from roastpilot_agent.bean_sourcing import (  # noqa: PLC0415
        EXTRACTION_MAX_RETRIES,
    )

    per_attempt_input = _reserve_input_tokens_per_attempt(page)
    input_tokens = (1 + EXTRACTION_MAX_RETRIES) * per_attempt_input + EXTRACTION_MAX_RETRIES * (
        2 * max_output_tokens + _RESERVE_RETRY_WRAPPER_TOKENS
    )
    output_tokens = max_output_tokens * (1 + EXTRACTION_MAX_RETRIES)
    return (
        input_tokens / 1_000_000 * price.price_in_per_mtok
        + output_tokens / 1_000_000 * price.price_out_per_mtok
    )


def _single_attempt_reserve(
    page: CorpusPage, price: RosterModel, *, max_output_tokens: int = BAKEOFF_MAX_OUTPUT_TOKENS
) -> float:
    """ONE attempt's worst-case reserve (#601 fold round 6/8) -- for the
    FINAL entry's addition, at most ONE in-flight attempt ever unreported (a
    completed retry's usage is already captured). That attempt is the WORST
    single one -- a RETRY-shaped input, same term shape as
    :func:`_page_cost_reserve`, which keeps the full multi-attempt case for
    the PENDING entry.

    Args:
        page: The corpus page.
        price: The arm's model pricing.
        max_output_tokens: The ENFORCED per-request output cap.

    Returns:
        ONE attempt's physically-bounded, worst-case estimated USD cost.
    """
    input_tokens = (
        _reserve_input_tokens_per_attempt(page)
        + 2 * max_output_tokens
        + _RESERVE_RETRY_WRAPPER_TOKENS
    )
    return (
        input_tokens / 1_000_000 * price.price_in_per_mtok
        + max_output_tokens / 1_000_000 * price.price_out_per_mtok
    )


def _raw_priced_cost(request_tokens: int, response_tokens: int, price: RosterModel) -> float:
    """Priced cost from captured tokens, with NO timeout-reserve floor (#601 fold
    round 1, slice A) -- what :func:`_actual_page_cost`'s floor compares against,
    and what ``LedgerEntry.reserve_applied`` reports on. Takes raw token counts, not
    a :class:`PageResult` -- the ledger is the token/spend store of record."""
    return (
        request_tokens / 1_000_000 * price.price_in_per_mtok
        + response_tokens / 1_000_000 * price.price_out_per_mtok
    )


def _actual_page_cost(
    request_tokens: int,
    response_tokens: int,
    price: RosterModel,
    per_page_reserve: float,
    *,
    apply_reserve: bool,
) -> float:
    """A page's usage-priced (list-price) cost from its captured tokens (#601 fold
    round 1, slice A). ``request_tokens``/``response_tokens`` SUM every
    COMPLETED, billed retry attempt -- when ``apply_reserve`` is set, the
    reserve is a DIFFERENT, additional call (the unreported one), so it is
    ADDED, never maxed (#601 fold round 4, FOLD 3).

    ``apply_reserve`` covers TWO cases (#601 fold round 10, D amendment): a
    wall-clock TIMEOUT (always unreported, regardless of prior captured
    usage), or an INFRA-class error (never schema/malformed-shape) with ZERO
    captured usage overall -- with transport retries disabled (Refs slice
    E), an accepted-but-lost request surfaces as exactly this. An infra
    failure that DID capture some usage is trusted as complete, no reserve.

    ``per_page_reserve`` must be a SINGLE-ATTEMPT reserve (:func:`_single_attempt_reserve`),
    never the full multi-attempt :func:`_page_cost_reserve` the PENDING entry
    uses -- with attempts bounded, at most ONE can ever be unreported here.
    """
    priced = _raw_priced_cost(request_tokens, response_tokens, price)
    return priced + per_page_reserve if apply_reserve else priced


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
            f"`{f.model_slug}` ({f.heuristic_label}, schema {f.schema_failures}/"
            f"other {f.other_errors})"
            for f in failed_slugs
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
        "cost/latency tie-break; schema F/R = schema failures/recovered, #601 F6)"
    )
    lines.append("")
    lines.append(
        "| Model | COR | PAR | INC | MIS | ABS-COR | SPU | ERR | Recall | Faithful | Abstain | "
        "micro F1 | macro F1 | Combined | latency p50/p95 (s) | schema F/R |"
    )
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|")
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
            f"{_fmt(m.combined_score)} | {latency} | {m.schema_failures}/{m.recovered_violations} |"
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

    light_runs = {
        r.model_slug[: -len(_LIGHT_ARM_LABEL_SUFFIX)]: r
        for r in runs
        if r.model_slug.endswith(_LIGHT_ARM_LABEL_SUFFIX)
    }
    off_runs = {
        r.model_slug[: -len(_OFF_ARM_LABEL_SUFFIX)]: r
        for r in runs
        if r.model_slug.endswith(_OFF_ARM_LABEL_SUFFIX)
    }
    paired_models = sorted(set(light_runs) & set(off_runs))
    if paired_models:
        lines.append(
            "## Reasoning-arm comparison (off vs light, #601) -- per-model deltas where "
            "BOTH arms were scored (never vs 'default'). 'schema F/recovered R' is the "
            "adherence proxy; 'other errors' is NOT."
        )
        lines.append("")
        lines.append(
            "| Model | macro F1 (off -> light) | Combined (off -> light) | "
            "Recall (off -> light) | Faithful (off -> light) | schema F/recovered R "
            "(off -> light) | other errors (off -> light) |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for model_slug in paired_models:
            off_m = model_metrics(off_runs[model_slug])
            light_m = model_metrics(light_runs[model_slug])
            lines.append(
                f"| `{model_slug}` | {_fmt(off_m.macro_f1)} -> {_fmt(light_m.macro_f1)} | "
                f"{_fmt(off_m.combined_score)} -> {_fmt(light_m.combined_score)} | "
                f"{_fmt(off_m.recall)} -> {_fmt(light_m.recall)} | "
                f"{_fmt(off_m.precision)} -> {_fmt(light_m.precision)} | "
                f"{off_m.schema_failures}/{off_m.recovered_violations} -> "
                f"{light_m.schema_failures}/{light_m.recovered_violations} | "
                f"{off_m.other_errors} -> {light_m.other_errors} |"
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
        f"| **arm total (1 pass each, every requested model/reasoning arm)** | | | "
        f"**${total:.4f}** | |"
    )
    lines.append("")
    lines.append(
        "Token counts use a chars/4 heuristic over the extractor's ACTUAL post-strip "
        "prompt text; prompt caching on the stable schema/instructions makes the real "
        "cost lower. EVERY USD figure above -- including a model marked 'spend incurred "
        "(est.)' -- is still this harness's pre-call ESTIMATE, never a verified OpenRouter "
        "billing amount: actual output/reasoning tokens, retries, and prompt caching can "
        "all make the real charge differ. (This harness DOES now capture real per-page "
        "usage internally, #601 fold round 1 -- not yet surfaced in this table.) A "
        "self-consistency vote (sample 3-5x) or a two-pass entailment judge would "
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
            "schema_failures": m.schema_failures,
            "other_errors": m.other_errors,
            "recovered_violations": m.recovered_violations,
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
                "recovered_violations": page.recovered_violations,
            }
            for page in run.pages
        ],
    }


# --- Checkpoint (resume) + cost guard ----------------------------------------


def sidecar_path(out: Path) -> Path:
    """The append-only per-``(model)`` checkpoint sidecar next to ``--out``."""
    return out.with_name(out.name + ".cells.jsonl")


def _environment_fingerprint() -> str:
    """A stable fingerprint of the WHOLE environment: interpreter, platform, distributions.

    Categorical (#602 round 6): a TRANSITIVE dependency moving invalidates resume too, no
    enumeration arms race. Also hashes ``sys.implementation.name``, ``platform.python_version()``,
    and ``platform.platform()`` (#602 round 7 -- the distribution set alone missed the runtime).
    Deliberately conservative: ANY change invalidates resume, never silently wrong.

    Returns:
        A stable string: interpreter/platform identity + every distribution's ``(name, version)``.
    """
    pairs = sorted((dist.name, dist.version) for dist in importlib.metadata.distributions())
    identity = f"{sys.implementation.name}|{platform.python_version()}|{platform.platform()}"
    return identity + "|" + "|".join(f"{name}=={version}" for name, version in pairs)


def _pipeline_fingerprint() -> str:
    """A fingerprint of the EVALUATED PIPELINE (not the corpus).

    Hashes every module in :data:`_FINGERPRINTED_MODULES` (first-party source that can change
    a drafted result) plus this harness's OWN source, the extraction timeout, and the WHOLE
    installed environment (see :func:`_environment_fingerprint`, #602 fold round 6). Any
    change invalidates a stale checkpoint automatically (closes the #600 round-2 gap:
    #590-style preprocessing changes without touching any fixture).

    Returns:
        A short, stable hex digest of every fingerprinted module + the environment; ``""``
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
        digest.update(_environment_fingerprint().encode())
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
    elsewhere, OR a malformed FINAL line that DOES end with a newline (#602 round 4: that write
    completed, so it's genuine corruption, not an interrupted append), still raises for manual
    recovery -- only a newline-less tail is auto-repaired, ATOMICALLY (see
    :func:`_atomic_write_text`). A kill can ALSO land right after the complete JSON payload but
    before its trailing ``\\n`` -- that line parses FINE, so the repair above never fires, yet
    the next :meth:`Checkpoint.append` would concatenate onto it (#602 round 6): a still-missing
    final newline is normalised in atomically after every line parses.

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
    elif lines and not raw_text.endswith("\n"):
        # Every line parsed, but the file itself is missing its final newline: a
        # valid tail with no separator for the next append (#602 fold round 6,
        # FOLD 3). Normalise it now, atomically, before any further append.
        _atomic_write_text(path, "".join(f"{ln}\n" for ln in lines))
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


def ledger_path(out: Path) -> Path:
    """The append-only per-PAGE-CALL :class:`ChargeLedger` sidecar next to ``--out``
    (#601 fold round 1, slice A) -- independent of the per-model :class:`Checkpoint`.
    """
    return out.with_name(out.name + ".ledger.jsonl")


@dataclass(frozen=True)
class LedgerEntry:
    """One page call's real charge, independent of its later scored outcome.

    Attributes:
        arm: The study arm's report label (:attr:`Arm.label`).
        slug: The page slug.
        request_tokens: Captured input/prompt tokens.
        response_tokens: Captured output/completion tokens.
        priced_usd: The page's usage-priced (list-price) cost, past the
            timeout-reserve floor (see :func:`_actual_page_cost`).
        timed_out: Whether the outer extraction timeout cancelled this call.
        reserve_applied: Whether the reserve was ADDED to ``priced_usd`` (#601
            fold round 4/10 -- true for ``timed_out`` OR a zero-usage infra
            failure, summed not maxed; see :func:`_actual_page_cost`).
        fingerprint: The writing invocation's experiment fingerprint (#601 fold
            round 3, FOLD 4), stamped by :meth:`ChargeLedger.append`.
        is_pending: ``True`` for the PENDING entry (charged at the reserve),
            ``False`` for the FINAL entry that supersedes it once the call
            completes (#601 fold round 4, FOLD 1); pre-FOLD-1 entries default
            ``False`` (final).
        call_id: A per-ATTEMPT-CYCLE identity (fresh ``uuid4().hex`` per page
            processed, #601 fold round 6, FOLD A) -- a pending entry and its
            final share ONE ``call_id``; a resumed re-attempt gets a NEW one
            (see :meth:`ChargeLedger._effective_entries`). Defaults ``""``;
            loaded as a fresh random id per pre-FOLD-A entry (never shared).
    """

    arm: str
    slug: str
    request_tokens: int
    response_tokens: int
    priced_usd: float
    timed_out: bool
    reserve_applied: bool
    fingerprint: str = ""
    is_pending: bool = False
    call_id: str = ""


#: Sentinel for a pre-fold-4 ledger entry with no persisted fingerprint (#601
#: fold round 3, FOLD 4) -- never equals a real fingerprint (a hex digest) or the
#: fingerprint-disabled empty-string default, so a legacy entry never matches.
_LEGACY_LEDGER_FINGERPRINT = "<legacy>"


class ChargeLedger:
    """Append-only, per-INVOCATION-LINEAGE record of every priced page call
    (#601 fold round 1, slice A) -- independent of :class:`Checkpoint`. A page's
    charge is recorded the INSTANT its call completes, before any
    scoring/checkpoint decision, so a mid-arm trip, whole failure, or dropped
    mixed-failure arm all leave real spend on the books.

    "Lineage" is every RESUME of the SAME experiment, not unrelated ones:
    :meth:`total_usd` counts ONLY entries fingerprinted to THIS invocation
    (#601 fold round 3) -- every entry EVER written stays on disk regardless,
    an append-only AUDIT TRAIL across lineages (only ``resume=False`` wipes it).
    A legacy entry predating this field is always excluded (fail-closed).
    """

    def __init__(self, path: Path, *, resume: bool = True, fingerprint: str = "") -> None:
        """Load every existing entry (if any) from ``path``, reusing
        :func:`_load_checkpoint_lines`'s truncation-safe JSONL parser --
        or wipe a stale ledger when starting fresh (``resume=False``,
        mirroring :class:`Checkpoint`).

        Args:
            path: The ledger JSONL path (see :func:`ledger_path`).
            resume: Load existing entries when ``True`` (the default);
                delete the file first when ``False``.
            fingerprint: THIS invocation's experiment fingerprint (#601 fold
                round 3, FOLD 4), stamped onto every newly-appended entry and
                what :meth:`total_usd` scopes the meter to.
        """
        self.path = path
        self.fingerprint = fingerprint
        self._entries: list[LedgerEntry] = []
        if not resume and path.exists():
            path.unlink()
        if resume and path.exists():
            for record in _load_checkpoint_lines(path):
                # A pre-FOLD-A record has no call_id -- assign a FRESH random
                # one per entry (#601 fold round 6, FOLD A) so two such
                # legacy entries never wrongly collide under one key.
                call_id = str(record["call_id"]) if record.get("call_id") else uuid.uuid4().hex
                self._entries.append(
                    LedgerEntry(
                        arm=str(record["arm"]),
                        slug=str(record["slug"]),
                        request_tokens=int(record["request_tokens"]),
                        response_tokens=int(record["response_tokens"]),
                        priced_usd=float(record["priced_usd"]),
                        timed_out=bool(record["timed_out"]),
                        reserve_applied=bool(record["reserve_applied"]),
                        fingerprint=str(record.get("fingerprint", _LEGACY_LEDGER_FINGERPRINT)),
                        is_pending=bool(record.get("is_pending", False)),
                        call_id=call_id,
                    )
                )

    @property
    def entries(self) -> list[LedgerEntry]:
        """Every entry loaded or appended, EVERY lineage -- the full audit
        trail (see :meth:`total_usd` for the fingerprint-scoped view)."""
        return list(self._entries)

    def _effective_entries(self) -> list[LedgerEntry]:
        """One EFFECTIVE entry per ``call_id``, scoped to THIS fingerprint
        (#601 fold round 6, FOLD A -- keyed on ``call_id``, never bare
        ``(arm, slug)``: a page CALLED more than once across a resume is
        TWO real, separate charges; collapsing by page would under-count).
        A FINAL entry always supersedes ITS OWN pending; a ``call_id`` with
        ONLY a pending entry (killed mid-call) still counts, at its reserve.
        """
        by_call: dict[str, LedgerEntry] = {}
        for entry in self._entries:
            if entry.fingerprint != self.fingerprint:
                continue
            existing = by_call.get(entry.call_id)
            if existing is None or not entry.is_pending or existing.is_pending:
                by_call[entry.call_id] = entry
        return list(by_call.values())

    def total_usd(self) -> float:
        """Cumulative charged spend, scoped to THIS fingerprint (#601 fold
        round 4, FOLD 1: a final entry supersedes its pending counterpart --
        see :meth:`_effective_entries`) -- a legacy or other-lineage entry
        never counts."""
        return round(sum(e.priced_usd for e in self._effective_entries()), 5)

    def append(self, entry: LedgerEntry) -> None:
        """Persist one page's real charge immediately (append-only, never
        rewritten), stamped with THIS invocation's fingerprint."""
        entry = dataclasses.replace(entry, fingerprint=self.fingerprint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(dataclasses.asdict(entry)) + "\n")
            handle.flush()
        self._entries.append(entry)


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
    """Whether every page errored AND NOT ALL failures are schema failures.

    ALL-SCHEMA is a real outcome, scored, never dropped (strongest signal); any MIX
    with an infra failure is a genuine outage -- see :class:`FailedRun` for how the
    mix stays visible instead of silently discarded (#601 F7)."""
    if not run.pages or not all(page.error is not None for page in run.pages):
        return False
    return not all(_is_schema_failure(page.error) for page in run.pages)


def _has_any_success(run: ModelRun) -> bool:
    """Whether at least one page of ``run`` succeeded (extraction did not error)."""
    return any(page.error is None for page in run.pages)


@dataclass(frozen=True)
class FailedRun:
    """A wholly-failed run this invocation -- NEVER checkpointed (#602 fold round 5).

    Rounds 3-4 eagerly checkpointed a "provable" MODEL-SPECIFIC failure -- unfixable, since
    no invocation-local signal can tell a transient outage apart from a model-specific fault.
    Failed attempts are CHEAP to retry; a mis-scored failure is EXPENSIVE to fix.

    Attributes:
        model_slug: The failed model.
        heuristic_label: ``"MODEL-SPECIFIC"`` if a FRESHLY-EXECUTED peer had already succeeded
            this invocation, else ``"INFRA-WIDE"``. DISPLAY-ONLY best-effort context -- NOT
            authoritative, NEVER affects checkpointing or scoring.
        schema_failures: Malformed-structured-output pages in this DROPPED run --
            a mixed run's adherence signal stays visible here (#601 F7).
        other_errors: Every OTHER page error in this dropped run.
    """

    model_slug: str
    heuristic_label: str
    schema_failures: int = 0
    other_errors: int = 0


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
    arms: Sequence[Arm],
    *,
    out: Path,
    resume: bool,
    max_spend: float,
    cost_estimates: Sequence[ModelCostEstimate],
    roster: Sequence[RosterModel],
    model: Model | None = None,
) -> BakeoffResult:
    """Run + checkpoint every arm over the corpus, under a spend guard.

    Read-only: never touches any store/DB, never saves a profile. Stops gracefully BEFORE an
    arm whose estimated cost would breach ``max_spend`` (see :attr:`BakeoffResult.stopped_early`).
    A WHOLLY-FAILED run (see :func:`_run_wholly_failed`) is NEVER checkpointed -- reported as a
    :class:`FailedRun` (DISPLAY-ONLY heuristic label + its schema/other-error counts) and always
    retried on resume, but still counted against the spend guard. Every checkpoint/report
    identity is :attr:`Arm.label` (#601), so a model's several arms are tracked distinctly.

    A persistent :class:`ChargeLedger` (#601 fold round 1, slice A) is ALSO opened and
    threaded through, so every dollar is now durably recorded page-by-page -- but
    nothing here reads it back yet, no spend guard enforces against it (a follow-on
    slice adds one).

    Args:
        pages: The corpus.
        arms: The (model, reasoning) study arms to run (real, paid calls) -- see
            :func:`expand_arms`.
        out: The JSON artifact path (anchors the checkpoint sidecar + the ledger, see
            :func:`ledger_path`).
        resume: Skip arms already checkpointed.
        max_spend: USD budget; an arm is skipped once the running estimate
            would exceed it.
        cost_estimates: Per-arm cost estimates (the spend guard's basis), keyed by
            :attr:`Arm.label`.
        roster: Priced roster the arms' model slugs resolve against (for the ledger's
            usage pricing, #601 fold round 1, slice A).
        model: An injected ``Model`` (the self-test seam); ``None`` = a real
            paid call, threaded through to :func:`run_model_over_corpus`.

    Returns:
        The :class:`BakeoffResult`.

    Raises:
        ValueError: If a non-stale checkpointed arm (about to be skipped/resumed)
            is missing a current-fingerprint ledger entry for ONE OR MORE of its
            OWN checkpointed pages (#601 fold round 5, D FOLD 3 -- PAGE-LEVEL,
            not mere per-arm EXISTENCE) -- names the uncovered arm(s) + missing
            count. The message names both fixes (``--no-resume``, or a
            different ``--out``).
    """
    cost_by_slug = {est.slug: est.usd for est in cost_estimates}
    price_by_slug = {r.slug: r for r in roster}
    fingerprint = compute_fingerprint(pages)
    checkpoint = Checkpoint(sidecar_path(out), resume=resume, fingerprint=fingerprint)
    ledger = ChargeLedger(ledger_path(out), resume=resume, fingerprint=fingerprint)
    # #601 fold round 5, D FOLD 3: PAGE-LEVEL coverage, per missing page -- the
    # bar is >=1 entry for the page, ANY call (#601 fold round 6, FOLD A);
    # call_id only matters for total_usd()'s supersession, not this check.
    covered_pages: dict[str, set[str]] = {}
    for e in ledger.entries:
        if e.fingerprint == fingerprint:
            covered_pages.setdefault(e.arm, set()).add(e.slug)
    uncovered: list[str] = []
    for a in arms:
        if not checkpoint.has(a.label):
            continue
        expected = {
            str(p["slug"]) for p in cast("list[dict[str, Any]]", checkpoint.get(a.label)["pages"])
        }
        missing = expected - covered_pages.get(a.label, set())
        if missing:
            uncovered.append(f"{a.label} (missing {len(missing)}/{len(expected)} page(s))")
    uncovered.sort()
    if uncovered:
        raise ValueError(
            f"{sidecar_path(out)}: checkpointed arm(s) {uncovered} have incomplete "
            "ledger coverage for this fingerprint -- their spend cannot be fully "
            "accounted for. Rerun with --no-resume (fresh books, fresh budget) or "
            "point --out elsewhere."
        )
    runs: list[ModelRun] = []
    failed_runs: list[FailedRun] = []
    executed_slugs: list[str] = []
    has_fresh_success = False
    spent = 0.0
    for index, arm in enumerate(arms):
        slug = arm.label
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
                unevaluated_slugs=[a.label for a in arms[index:]],
                failed_slugs=failed_runs,
                executed_slugs=executed_slugs,
            )
        run = await run_model_over_corpus(
            pages,
            model_slug=slug,
            advisor_config=make_advisor_config(arm.model_slug),
            model=model,
            sourcing_config=make_sourcing_config(arm.model_slug),
            reasoning_effort=_REASONING_EFFORT_BY_ARM[arm.reasoning],
            roster_price=price_by_slug[arm.model_slug],
            ledger=ledger,
        )
        spent += upcoming
        executed_slugs.append(slug)  # a real call was attempted, win or lose
        if _run_wholly_failed(run):
            label = "MODEL-SPECIFIC" if has_fresh_success else "INFRA-WIDE"
            schema_n = sum(1 for p in run.pages if _is_schema_failure(p.error))
            other_n = len(run.pages) - schema_n
            print(
                f"[run] {slug}: ALL {len(run.pages)} page(s) errored -- {label} (heuristic, "
                f"display-only), schema {schema_n}/other {other_n} -- NEVER checkpointed, a "
                "re-run always retries it",
                flush=True,
            )
            failed_runs.append(
                FailedRun(
                    model_slug=slug,
                    heuristic_label=label,
                    schema_failures=schema_n,
                    other_errors=other_n,
                )
            )
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
                recovered_violations=int(cast("int", page.get("recovered_violations", 0))),
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
    parser.add_argument(
        "--reasoning",
        choices=("default", "off", "light", "both"),
        default="default",
        help="reasoning-effort study arm(s) per model (#601): 'default' (no override, "
        "the CLI default -- NOTE this is the provider's OWN default effort, NOT the "
        "same as no reasoning), 'off' (explicit no-reasoning), 'light' (provider "
        "low effort), or 'both' (the 'off' AND 'light' arms -- the research's actual "
        "no-reasoning-vs-light-reasoning question, section 4)",
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
    reasoning_arg = cast("Literal['default', 'off', 'light', 'both']", args.reasoning)
    capability: dict[str, RosterReasoningCapability] = {
        m.slug: m.reasoning for m in roster_for_cost
    }
    arms = expand_arms(model_slugs, reasoning_arg, capability=capability)
    if not arms:
        # Categorical, mode-agnostic (#601 F4): ANY --reasoning value can skip every
        # requested model -- fires before any cost estimate or spend.
        print(
            f"REFUSED: --reasoning {reasoning_arg} -- every requested model "
            f"({', '.join(model_slugs)}) was skipped as reasoning-incapable for this "
            "mode; nothing to run (#601).",
            file=sys.stderr,
        )
        return 2
    if reasoning_arg == "both" and not any(c == "optional" for c in capability.values()):
        print(
            "REFUSED: --reasoning both -- no requested model has BOTH off and light arms "
            "(#601 FA); nothing comparable to run.",
            file=sys.stderr,
        )
        return 2
    cost_estimates = estimate_cost_for_arms(pages, arms, roster_for_cost)

    if args.estimate_only:
        for est in cost_estimates:
            print(f"{est.slug}: ~${est.usd:.4f} ({est.input_tokens} in / {est.output_tokens} out)")
        print(f"arm total (1 pass each): ~${sum(e.usd for e in cost_estimates):.4f}")
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

    try:
        result = await run_bakeoff(
            pages,
            arms,
            out=cast("Path", args.out),
            resume=not bool(args.no_resume),
            max_spend=float(cast("float", args.max_spend)),
            cost_estimates=cost_estimates,
            roster=roster_for_cost,
        )
    except ValueError as exc:
        # #601 fold round 2, FOLD 4: a pre-ledger checkpoint.
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
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
