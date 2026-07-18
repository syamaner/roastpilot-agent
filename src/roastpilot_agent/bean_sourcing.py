"""Bean-sourcing assistant: add-bean-from-URL (#573 phase 1).

Productises the ``.claude/skills/add-bean-profile`` skill: paste a
green-coffee vendor product URL, fetch the page, extract the bean identity
with a structured LLM call, and draft a
:class:`~roastpilot_agent.models.BeanProfileDraft` with conservative
first-roast targets plus honest per-field imputation. The operator reviews,
edits, and saves the draft via the EXISTING, unchanged create-bean-profile
action (``POST /api/bean-profiles``) — this module never persists anything
and never auto-saves (human-in-the-loop by construction, per the #573
safeguards).

**Cleanly separate from the roast advisor and the roaster/control path**
(Architecture Invariants — the safety-boxed roast advisor never gets write
tools and only returns typed ``RoastDecision``). This module:

- imports nothing from ``controller``, ``safety``, or ``mcp_client`` — a
  dedicated test (``tests/test_bean_sourcing.py``) proves this holds for the
  whole transitive import graph, in a fresh subprocess, not just this file's
  own ``import`` statements;
- builds its OWN ``pydantic_ai.Agent`` scoped to bean-identity extraction —
  never :class:`roastpilot_agent.advisor.PydanticAIAdvisor` — with no MCP
  tools of any kind wired in, so there is no path from a fetched URL back
  into the roast-control loop;
- reuses :func:`roastpilot_agent.advisor.build_model` only as the shared,
  pure PydanticAI provider-construction factory (BYOK: the same
  provider/key/model config the operator already set for the roast advisor
  drives this SEPARATE call too) — that function builds a ``Model``, not an
  advisor, and carries no roaster/controller/safety coupling itself.

Fetching is respectful and fail-soft (issue #573 safeguards): a bounded
timeout, an identifying User-Agent, and a hard response-size cap enforced
while streaming. Every failure mode — a malformed URL, a transport error, a
non-2xx response, an oversized body, or an LLM/provider failure — is raised
as one of this module's typed errors; the pipeline never raises an unhandled
exception or crashes the caller.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent, ModelAPIError, ModelSettings, UnexpectedModelBehavior
from pydantic_ai.models import Model

from roastpilot_agent.advisor import build_model
from roastpilot_agent.config import AdvisorConfig, BeanSourcingConfig
from roastpilot_agent.models import (
    BeanFieldSource,
    BeanProfileDraft,
    BeanSpecies,
    ProcessingMethod,
)

_log = logging.getLogger(__name__)


class BeanSourcingError(Exception):
    """Base class for add-bean-from-URL failures (#573 phase 1)."""


class BeanFetchError(BeanSourcingError):
    """Fetching the vendor page failed: a malformed URL, a transport/timeout
    failure, a non-2xx response, or a body over the configured size cap."""


class BeanExtractionError(BeanSourcingError):
    """The page could not be mapped to a usable bean identity: the LLM
    provider/transport failed, its output was malformed, or the page stated
    neither a usable name nor a usable origin to draft from."""


_STRIP_TAG_BLOCK_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_INLINE_WHITESPACE_RE = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

#: Extracted page text is truncated to this many characters before it is
#: handed to the LLM — a token/cost bound independent of the raw HTTP fetch
#: cap (``BeanSourcingConfig.max_response_bytes``), so even a legitimately
#: large page yields a bounded prompt.
_MAX_EXTRACTED_CHARS = 20_000


def _extract_page_text(html: str) -> str:
    """Strip a vendor page down to plain, LLM-readable text.

    A dependency-free HTML-to-text pass: ``<script>``/``<style>`` blocks are
    dropped whole (their content is never useful bean-identity text and
    could be large), remaining tags are stripped, entities are unescaped, and
    whitespace is collapsed. Good enough for a structured extraction prompt
    without adding an HTML-parsing dependency to the lean runtime install.

    Args:
        html: The raw response body, already decoded to text.

    Returns:
        Collapsed plain text, truncated to :data:`_MAX_EXTRACTED_CHARS`.
    """
    without_blocks = _STRIP_TAG_BLOCK_RE.sub(" ", html)
    without_tags = _ANY_TAG_RE.sub(" ", without_blocks)
    text = unescape(without_tags)
    text = _INLINE_WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = _BLANK_LINES_RE.sub("\n\n", text)
    text = text.strip()
    return text[:_MAX_EXTRACTED_CHARS]


async def _fetch_page_text(
    url: str,
    *,
    config: BeanSourcingConfig,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Respectfully fetch ``url`` and return its extracted plain text.

    An ``httpx`` GET, bounded by ``config.fetch_timeout_seconds``, sent with
    an identifying ``User-Agent``, with a hard cap on the response body
    (``config.max_response_bytes``) enforced by streaming the body and
    aborting the moment the cap is crossed — an oversized or slow-drip
    response is never read fully into memory. Fails soft: every failure mode
    is raised as a typed :class:`BeanFetchError`, never an unhandled
    exception.

    Args:
        url: The vendor product page URL.
        config: Fetch timeout / size-cap / User-Agent settings.
        http_client: An injectable client (the fetch test seam — e.g. one
            built with ``httpx.MockTransport``). A real client is
            constructed, used, and closed when omitted.

    Returns:
        The extracted plain text of the page.

    Raises:
        BeanFetchError: On a malformed URL, any transport/timeout failure, a
            non-2xx response, or a body over the configured size cap.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r}")

    headers = {"User-Agent": config.user_agent}
    timeout = httpx.Timeout(config.fetch_timeout_seconds)
    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient()
    try:
        async with client.stream("GET", url, headers=headers, timeout=timeout) as response:
            if response.status_code >= 400:
                raise BeanFetchError(
                    f"vendor page fetch failed: HTTP {response.status_code} for {url!r}"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > config.max_response_bytes:
                    raise BeanFetchError(
                        f"vendor page exceeded the {config.max_response_bytes}-byte "
                        f"fetch cap: {url!r}"
                    )
            html = bytes(body).decode("utf-8", errors="replace")
    except BeanFetchError:
        raise
    except httpx.HTTPError as exc:
        raise BeanFetchError(f"vendor page fetch failed for {url!r}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()
    return _extract_page_text(html)


class _ExtractedBeanIdentity(BaseModel):
    """Structured LLM output: identity fields found ON THE PAGE (#573 phase 1).

    Every field is optional and MUST be left ``None`` unless the page text
    actually states it — :data:`_EXTRACTION_INSTRUCTIONS` tells the model
    explicitly never to guess a varietal, altitude, processing method, or
    species that is not written on the page (the honest-imputation
    safeguard). This is the raw provider output; :func:`_draft_from_identity`
    layers the conservative roast-target imputation on top, deterministically
    in plain Python — the model is never asked to invent a development
    percent or a drop temperature.
    """

    name: str | None = None
    country: str | None = None
    bean_origin: str | None = None
    farm: str | None = None
    bean_varietal: str | None = None
    processing: ProcessingMethod | None = None
    bean_species: BeanSpecies | None = None
    altitude_m: int | None = Field(default=None, ge=0, le=4000)
    description: str | None = None
    is_blend: bool = False


_EXTRACTION_INSTRUCTIONS = """
You extract green-coffee bean identity from a vendor product page's text.

Read the page text in the user message and return ONLY what it actually
states. Leave a field null when the page does not state it. Do NOT guess or
infer a varietal, altitude, processing method, or species that is not
written on the page — this is a scraped-facts extraction, not a coffee
expert's estimate. Fabricating a plausible-sounding value here is worse than
leaving it null: the caller marks every non-null field you return as "found
on the vendor page" and the operator will trust it as such.

Fields:
- name: the product title / bean name as written on the page.
- country / bean_origin: the producing country. bean_origin may be more
  specific than country (e.g. a region); if the page only gives one value,
  use it for both.
- farm: the specific farm / co-op / washing station / region, only if named.
- bean_varietal: the cultivar(s), e.g. "Caturra, Typica, Bourbon" — only if
  named.
- processing: one of washed / natural / honey / anaerobic / wet_hulled /
  other — only if the page states (or unambiguously names, e.g. "washed
  process") the method; otherwise null.
- bean_species: arabica / robusta / liberica / excelsa — only if stated;
  arabica is the common case but do not assume it when the page is silent.
- altitude_m: a single representative whole-metre value if the page gives an
  altitude or an altitude range (use the midpoint of a stated range); null if
  the page gives no altitude at all.
- description: a short (1-3 sentence) summary of the tasting notes, process,
  or lot detail actually written on the page, in your own words.
- is_blend: true only if the page explicitly says this is a blend of
  multiple origins; false for a single-origin bean, even one with a mixed
  varietal.
""".strip()


def _bean_sourcing_agent(
    advisor_config: AdvisorConfig, *, model: Model | None = None
) -> Agent[None, _ExtractedBeanIdentity]:
    """Build the bean-identity extraction agent.

    A dedicated ``pydantic_ai.Agent`` scoped to this module's own
    :class:`_ExtractedBeanIdentity` structured output — never
    :class:`roastpilot_agent.advisor.PydanticAIAdvisor` — with no MCP tools
    of any kind wired in. Reuses :func:`roastpilot_agent.advisor.build_model`
    (the shared, pure provider-construction factory, D18) so the operator's
    already-configured provider/key/model (BYOK) drives this SEPARATE call
    too, without duplicating provider-construction logic.

    Args:
        advisor_config: The operator's advisor provider/key/model config.
        model: An injected ``Model`` (the extraction test seam); built via
            :func:`build_model` when omitted.

    Returns:
        The extraction agent, temperature 0 for deterministic, literal
        (non-inventive) extraction.
    """
    resolved_model = model if model is not None else build_model(advisor_config)
    return Agent(
        resolved_model,
        output_type=_ExtractedBeanIdentity,
        instructions=_EXTRACTION_INSTRUCTIONS,
        model_settings=ModelSettings(temperature=0.0),
    )


async def _extract_bean_identity(
    page_text: str,
    *,
    advisor_config: AdvisorConfig,
    model: Model | None = None,
) -> _ExtractedBeanIdentity:
    """Run the structured bean-identity extraction call over ``page_text``.

    Args:
        page_text: The vendor page's extracted plain text.
        advisor_config: The operator's advisor provider/key/model config.
        model: An injected PydanticAI ``Model`` (the extraction test seam).

    Returns:
        The provider's honest, page-only bean identity.

    Raises:
        BeanExtractionError: On any provider/transport failure, or a
            malformed structured-output shape.
    """
    agent = _bean_sourcing_agent(advisor_config, model=model)
    try:
        result = await agent.run(page_text)
    except UnexpectedModelBehavior as exc:
        raise BeanExtractionError(
            f"bean identity extraction returned a malformed shape: {exc}"
        ) from exc
    except ModelAPIError as exc:
        raise BeanExtractionError(f"bean identity extraction provider error: {exc}") from exc
    return result.output


#: Conservative first-roast ("scouting run") targets by processing method
#: (#573 phase 1) — mirrors the ``add-bean-profile`` skill's per-origin DTR
#: priors (memory ``per-origin-dtr-washed-highgrown``): a natural runs ~13 %
#: development, a washed high-grown's *eventual* medium is ~18 % but the
#: FIRST roast on an unfamiliar bean de-risks toward the bottom of its range.
#: Drop stays at or below the operator's proven 195 °C known-good line
#: (bitter > 196 °C). ``None``/anything not covered by a named process gets
#: the same conservative, mid-of-the-pack posture as ``"other"`` — no
#: evidence to lean lighter or darker, so a wrong guess in either direction
#: is equally avoided. A wrong target on an unfamiliar bean must not burn a
#: batch (the #573 safeguard).
_SCOUTING_TARGETS_BY_PROCESSING: dict[ProcessingMethod | None, tuple[float, float]] = {
    "natural": (193.0, 13.0),
    "washed": (195.0, 15.0),
    "honey": (194.0, 14.0),
    "anaerobic": (194.0, 14.0),
    "wet_hulled": (195.0, 15.0),
    "other": (194.0, 14.0),
    None: (194.0, 14.0),
}

#: The bean-identity fields tracked in ``field_sources`` — every field
#: :class:`_ExtractedBeanIdentity` can populate from the page. Roast-TARGET
#: fields are handled separately (:data:`_TARGET_FIELDS`) — always
#: ``"origin_estimated"``, never ``"on_page"``.
_IDENTITY_FIELDS: tuple[str, ...] = (
    "name",
    "country",
    "bean_origin",
    "farm",
    "bean_varietal",
    "processing",
    "bean_species",
    "altitude_m",
    "description",
)

#: Roast-target fields a vendor page never states — always
#: ``"origin_estimated"`` in the drafted :attr:`BeanProfileDraft.field_sources`.
_TARGET_FIELDS: tuple[str, ...] = (
    "charge_guidance_min_c",
    "charge_guidance_max_c",
    "initial_heat_percent",
    "initial_fan_percent",
    "target_drop_temp_c",
    "target_development_percent",
    "default_bean_weight_grams",
)

#: The charge guidance band + initial heat/fan every draft carries (the
#: skill's §2 defaults): 170-200 °C is the proven Hottop charge range, and
#: 100 %/30 % initial heat/fan are the deterministic pre-FC seed values —
#: identical to every :mod:`roastpilot_agent.seed` built-in profile.
_DEFAULT_CHARGE_GUIDANCE_MIN_C = 170.0
_DEFAULT_CHARGE_GUIDANCE_MAX_C = 200.0
_DEFAULT_INITIAL_HEAT_PERCENT = 100
_DEFAULT_INITIAL_FAN_PERCENT = 30
_DEFAULT_BEAN_WEIGHT_GRAMS = 250.0


def _draft_from_identity(identity: _ExtractedBeanIdentity, *, url: str) -> BeanProfileDraft:
    """Assemble the :class:`BeanProfileDraft` from an extracted identity.

    Applies the conservative scouting targets
    (:data:`_SCOUTING_TARGETS_BY_PROCESSING`) and builds the honest per-field
    ``field_sources`` map: every identity field the page stated is
    ``"on_page"``; every roast-target field is always ``"origin_estimated"``.

    Args:
        identity: The provider's page-only extraction.
        url: The source URL (carried onto ``source_url``).

    Returns:
        The drafted profile, ready for the operator to review, edit, and
        (optionally) save.

    Raises:
        BeanExtractionError: When the page yielded neither a usable ``name``
            nor a usable ``bean_origin``/``country`` — too little identity to
            draft a profile from.
    """
    name = (identity.name or "").strip()
    bean_origin = (identity.bean_origin or identity.country or "").strip()
    if not name or not bean_origin:
        raise BeanExtractionError(
            f"could not determine a bean name and origin from the page ({url!r}) "
            "— add the profile manually instead"
        )

    drop_temp_c, dev_percent = _SCOUTING_TARGETS_BY_PROCESSING.get(
        identity.processing, _SCOUTING_TARGETS_BY_PROCESSING[None]
    )

    field_sources: dict[str, BeanFieldSource] = {}
    for field_name in _IDENTITY_FIELDS:
        raw_value = getattr(identity, field_name)
        if raw_value not in (None, ""):
            field_sources[field_name] = "on_page"
    if "bean_origin" not in field_sources and identity.country:
        # bean_origin fell back to country — still page-sourced, just via the
        # country field rather than an explicit bean_origin statement.
        field_sources["bean_origin"] = "on_page"
    for field_name in _TARGET_FIELDS:
        field_sources[field_name] = "origin_estimated"

    scouting_note = (
        "Scouting run — this is the FIRST roast on this bean. Targets are a "
        f"conservative, de-risked starting point ({dev_percent:g} % development, "
        f"drop {drop_temp_c:g} °C) based on the "
        f"{identity.processing or 'unstated'} processing method, so a wrong guess "
        "cannot burn the batch. Taste and step the development target up on the "
        "next bag if it reads underdeveloped. Every field marked "
        '"origin_estimated" in field_sources was NOT found on the vendor page — '
        "review it before roasting."
    )

    return BeanProfileDraft(
        name=name,
        bean_origin=bean_origin,
        bean_varietal=identity.bean_varietal,
        country=identity.country,
        farm=identity.farm,
        description=identity.description,
        bean_species=identity.bean_species,
        is_blend=identity.is_blend,
        processing=identity.processing,
        altitude_m=identity.altitude_m,
        source_url=url,
        charge_guidance_min_c=_DEFAULT_CHARGE_GUIDANCE_MIN_C,
        charge_guidance_max_c=_DEFAULT_CHARGE_GUIDANCE_MAX_C,
        initial_heat_percent=_DEFAULT_INITIAL_HEAT_PERCENT,
        initial_fan_percent=_DEFAULT_INITIAL_FAN_PERCENT,
        target_drop_temp_c=drop_temp_c,
        target_development_percent=dev_percent,
        default_bean_weight_grams=_DEFAULT_BEAN_WEIGHT_GRAMS,
        field_sources=field_sources,
        scouting_note=scouting_note,
    )


async def draft_bean_profile_from_url(
    url: str,
    *,
    advisor_config: AdvisorConfig,
    sourcing_config: BeanSourcingConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
    model: Model | None = None,
) -> BeanProfileDraft:
    """Draft a bean profile from a vendor product URL (#573 phase 1).

    The full add-bean-from-URL pipeline: fetch the page (respectful,
    fail-soft, timeout/size bounded), extract its plain text, run a
    structured LLM call scoped to bean identity only (a dedicated agent,
    never the roast advisor, no MCP tools), then deterministically apply
    conservative first-roast targets and honest per-field imputation.
    Returns a DRAFT only — nothing is persisted here; saving is the caller's
    existing ``POST /api/bean-profiles`` action.

    Args:
        url: The vendor product page URL.
        advisor_config: The operator's advisor provider/key/model config
            (BYOK) — reused for this separate extraction call.
        sourcing_config: Fetch timeout/size-cap/User-Agent settings.
            Defaults are constructed when omitted.
        http_client: An injectable ``httpx.AsyncClient`` (the fetch test
            seam).
        model: An injectable PydanticAI ``Model`` (the extraction test seam).

    Returns:
        The drafted :class:`~roastpilot_agent.models.BeanProfileDraft`.

    Raises:
        BeanFetchError: The vendor page could not be fetched.
        BeanExtractionError: The LLM call failed, or the page yielded too
            little identity to draft a profile from.
    """
    config = sourcing_config if sourcing_config is not None else BeanSourcingConfig()
    _log.info("draft_bean_profile_from_url: fetching %r", url)
    page_text = await _fetch_page_text(url, config=config, http_client=http_client)
    identity = await _extract_bean_identity(page_text, advisor_config=advisor_config, model=model)
    draft = _draft_from_identity(identity, url=url)
    _log.info(
        "draft_bean_profile_from_url: drafted %r (%d fields sourced)",
        draft.name,
        len(draft.field_sources),
    )
    return draft
