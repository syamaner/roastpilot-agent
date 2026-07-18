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

**SSRF + resource-exhaustion hardening (#587):** the fetch is a server-side
GET of an operator-supplied URL on a LAN tool with no authentication, so
:func:`_assert_public_destination` rejects any destination that resolves to
a loopback / private / link-local (including the ``169.254.169.254`` cloud
metadata address) / multicast / unspecified / reserved address — checked on
the origin URL *and* on every redirect hop the internally-constructed client
follows, since a public URL can 302 into a private address just as easily as
an operator can paste one directly. The naive "validate the hostname, then
let ``httpx`` connect to the hostname" version of that check has a
DNS-rebinding TOCTOU gap: a short-TTL domain can answer a public address on
the validation lookup and a private/metadata address on ``httpx``'s own
connect-time re-resolution (the exact shape behind CVE-2026-27826 /
GHSA-489g-7rxv-6c8q in other fetch-URL-for-LLM tools). This module closes
that gap by connect-time IP pinning: :func:`_assert_public_destination`
returns the validated address, and :func:`_fetch_with_ssrf_guard` issues
the request against that literal IP (``httpx.URL.copy_with(host=...)``) so
there is no second resolution left to poison, while an explicit ``Host``
header and the ``sni_hostname`` request extension keep routing/TLS identity
(virtual host, SNI, certificate hostname check) pinned to the ORIGINAL
hostname. The only remaining residual is parser-differential risk — a
hostname the resolver and this module's own URL parsing could disagree on —
mitigated by validating and pinning from the exact same parsed host on every
hop. The whole fetch (all hops + body streaming) is additionally bounded by
an end-to-end deadline independent of ``httpx``'s own per-request timeout,
and the LLM extraction call is bounded by its own deadline — both mapped to
this module's typed errors, never left to hang indefinitely.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from html import unescape
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent, ModelAPIError, ModelSettings, UnexpectedModelBehavior
from pydantic_ai.models import Model

from roastpilot_agent.advisor import AdvisorDependencyError, AdvisorError, build_model
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


#: Redirect hops the internally-constructed client will follow manually
#: (#587 fix 1) before giving up — matches the prior ``httpx``
#: ``max_redirects=5`` policy this replaces.
_MAX_REDIRECTS = 5


async def _assert_public_destination(
    url: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Reject a fetch destination that is not publicly routable, and return
    the validated address to CONNECT TO (#587 fix 1, SSRF guard + DNS-
    rebinding fix).

    Parses ``url``'s host. If it is an IP literal, validates that address
    directly; otherwise resolves it via the non-blocking asyncio resolver
    (``loop.getaddrinfo`` — never the blocking stdlib resolver) and validates
    EVERY address the host resolves to, since a hostname can round-robin or
    dual-stack across a mix of public and non-public addresses. An address is
    rejected when it is loopback, private (RFC1918 and friends), link-local
    (this is what blocks the ``169.254.169.254`` cloud metadata endpoint),
    multicast, unspecified, or otherwise IANA-reserved.

    Called on the origin URL and on every redirect ``Location`` the
    internally-constructed client is about to follow — a *public* URL can
    302 into a private address just as easily as an operator can paste one
    in directly (#587).

    The caller MUST connect to the returned address rather than letting
    ``httpx`` re-resolve the hostname itself (see :func:`_fetch_with_ssrf_guard`)
    — a hostname resolved and validated here, then handed to the HTTP client
    as a hostname, gives a short-TTL DNS record a second chance to answer
    differently at connect time (a "rebinding" race: public on this check,
    private/metadata on the real connect). Returning the validated address
    and pinning the actual connection to it closes that gap: there is no
    second resolution left to poison.

    Args:
        url: The absolute URL about to be connected to (the origin URL, or a
            redirect hop's resolved ``Location``).

    Returns:
        The validated address (the first address checked — the literal IP
        itself, or the first ``getaddrinfo`` result for a hostname; every
        resolved address was checked, not just this one).

    Raises:
        BeanFetchError: The scheme is not ``http``/``https``, the URL has no
            host, the host could not be resolved, or any address it
            resolves to (or the literal IP itself) is non-public.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r}")

    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)

    try:
        addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [
            ipaddress.ip_address(host)
        ]
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            resolved = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise BeanFetchError(f"could not resolve host {host!r} for {url!r}: {exc}") from exc
        addresses = [ipaddress.ip_address(info[4][0]) for info in resolved]
        if not addresses:
            raise BeanFetchError(
                f"host {host!r} resolved to no usable address for {url!r}"
            ) from None

    for address in addresses:
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
        ):
            raise BeanFetchError(
                f"fetch destination {url!r} resolves to a non-public address "
                f"({address}) — blocked by the SSRF guard (#587)"
            )

    return addresses[0]


async def _fetch_with_ssrf_guard(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    config: BeanSourcingConfig,
) -> str:
    """Fetch ``url`` on the internally-constructed client, following
    redirects MANUALLY (#587 fix 1) with every hop's connection PINNED to
    its validated address (#587 fix 1b, closes the DNS-rebinding TOCTOU
    gap).

    Per hop: :func:`_assert_public_destination` validates the CURRENT
    (hostname-based) URL and returns the address to connect to; the actual
    request is then issued against that address literally
    (``httpx.URL.copy_with(host=...)``) so ``httpx``/``httpcore`` never
    re-resolves the hostname themselves — there is no second DNS lookup a
    rebinding attack could win. Routing and TLS identity stay pinned to the
    ORIGINAL hostname: an explicit ``Host`` header (mirroring what ``httpx``
    would have sent had it connected to the hostname directly) preserves
    virtual-host routing, and the ``sni_hostname`` request extension keeps
    the TLS ClientHello's SNI — and therefore certificate hostname
    verification — targeting the real host, not the IP. The next hop's
    ``Location`` is resolved against the ORIGINAL (hostname) URL, never the
    IP-pinned one, so a relative redirect stays correct.

    Only used for the client this module constructs itself (never an
    injected ``http_client`` — see :func:`_fetch_page_text`); that client is
    built with ``follow_redirects=False`` so ``httpx`` never follows a
    redirect for us before we get a chance to validate (and pin) its
    destination.

    Args:
        client: The internally-constructed ``httpx.AsyncClient``.
        url: The origin URL.
        headers: Request headers (the identifying User-Agent).
        timeout: The per-request ``httpx.Timeout``.
        config: Response-size-cap settings.

    Returns:
        The decoded body of the final, non-redirect response.

    Raises:
        BeanFetchError: A destination rejected by the SSRF guard at any hop,
            a non-2xx/non-3xx-with-``Location`` response, a body over the
            size cap, a bare 3xx with no ``Location`` header, or more than
            :data:`_MAX_REDIRECTS` redirects.
    """
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        validated_address = await _assert_public_destination(current_url)
        original_url = httpx.URL(current_url)
        pinned_url = original_url.copy_with(host=str(validated_address))
        pinned_headers = {**headers, "Host": original_url.netloc.decode("ascii")}
        async with client.stream(
            "GET",
            pinned_url,
            headers=pinned_headers,
            timeout=timeout,
            extensions={"sni_hostname": original_url.host},
        ) as response:
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if not location:
                    raise BeanFetchError(
                        f"vendor page redirected (HTTP {response.status_code}) with no "
                        f"Location header for {current_url!r}"
                    )
                current_url = urljoin(current_url, location)
                continue
            if response.status_code >= 400:
                raise BeanFetchError(
                    f"vendor page fetch failed: HTTP {response.status_code} for {current_url!r}"
                )
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > config.max_response_bytes:
                    raise BeanFetchError(
                        f"vendor page exceeded the {config.max_response_bytes}-byte "
                        f"fetch cap: {current_url!r}"
                    )
            return bytes(body).decode("utf-8", errors="replace")
    raise BeanFetchError(f"too many redirects (> {_MAX_REDIRECTS}) fetching {url!r}")


async def _fetch_page_text(
    url: str,
    *,
    config: BeanSourcingConfig,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    """Respectfully fetch ``url`` and return its extracted plain text.

    An ``httpx`` GET, bounded by ``config.fetch_timeout_seconds`` per
    request AND by an end-to-end ``config.fetch_timeout_seconds`` deadline
    across the whole fetch — every redirect hop and the full body stream
    (#587 fix 2, a slow-drip server can otherwise keep the request alive
    indefinitely past any single-request timeout) — sent with an identifying
    ``User-Agent``, with a hard cap on the response body
    (``config.max_response_bytes``) enforced by streaming the body and
    aborting the moment the cap is crossed. Fails soft: every failure mode
    is raised as a typed :class:`BeanFetchError`, never an unhandled
    exception.

    The internally-constructed client never auto-follows a redirect —
    :func:`_fetch_with_ssrf_guard` follows redirects manually so every hop's
    destination clears :func:`_assert_public_destination` first (#587 fix
    1). An injected ``http_client`` (the test seam, or a future
    caller-supplied client) is exempt from that machinery: its redirect
    policy and destination are the caller's to set, matching this module's
    prior behavior.

    Args:
        url: The vendor product page URL.
        config: Fetch timeout / size-cap / User-Agent settings.
        http_client: An injectable client (the fetch test seam — e.g. one
            built with ``httpx.MockTransport``). A real client is
            constructed, used, and closed when omitted.

    Returns:
        The extracted plain text of the page.

    Raises:
        BeanFetchError: On a malformed URL, a destination rejected by the
            SSRF guard, any transport/timeout failure, a non-2xx response, a
            body over the configured size cap, or exceeding the end-to-end
            fetch deadline.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r}")

    headers = {"User-Agent": config.user_agent}
    timeout = httpx.Timeout(config.fetch_timeout_seconds)
    owns_client = http_client is None
    client = http_client if http_client is not None else httpx.AsyncClient(follow_redirects=False)
    try:
        async with asyncio.timeout(config.fetch_timeout_seconds):
            if owns_client:
                html = await _fetch_with_ssrf_guard(
                    client, url, headers=headers, timeout=timeout, config=config
                )
            else:
                # Injected client (the fetch test seam): its redirect policy
                # and destination are the caller's to set — no SSRF guard,
                # no manual redirect loop, matching this module's behavior
                # before #587.
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
    except TimeoutError as exc:
        raise BeanFetchError(
            f"vendor page fetch exceeded the {config.fetch_timeout_seconds:g}s end-to-end "
            f"deadline for {url!r}"
        ) from exc
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
        BeanExtractionError: On any provider/transport failure, a malformed
            structured-output shape, a failure to construct the extraction
            agent itself (a missing optional provider dependency, or an
            unsupported provider — see :func:`build_model`), or exceeding
            ``advisor_config.timeout_seconds`` (#587 fix 3 — an unbounded
            LLM call must not be able to hang the drafting request forever).
    """
    try:
        # Agent construction (which calls ``build_model`` when ``model`` is
        # omitted) lives INSIDE the try: it can raise ``AdvisorDependencyError``
        # / ``AdvisorError`` on a misconfigured or under-installed provider,
        # and that must fail soft as ``BeanExtractionError`` too, not escape
        # as an unhandled exception (#587).
        agent = _bean_sourcing_agent(advisor_config, model=model)
        async with asyncio.timeout(advisor_config.timeout_seconds):
            result = await agent.run(page_text)
    except TimeoutError as exc:
        raise BeanExtractionError(
            f"bean identity extraction exceeded the {advisor_config.timeout_seconds:g}s deadline"
        ) from exc
    except UnexpectedModelBehavior as exc:
        raise BeanExtractionError(
            f"bean identity extraction returned a malformed shape: {exc}"
        ) from exc
    except ModelAPIError as exc:
        raise BeanExtractionError(f"bean identity extraction provider error: {exc}") from exc
    except (AdvisorDependencyError, AdvisorError) as exc:
        raise BeanExtractionError(
            f"bean identity extraction could not build its model: {exc}"
        ) from exc
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
            draft a profile from — or when the assembled draft fails
            :class:`BeanProfileDraft`'s own field validation (e.g. a
            ``source_url`` embedding userinfo or a malformed port, which
            :func:`_fetch_page_text`'s scheme/host check does not itself
            reject, per #587).
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
    if identity.is_blend:
        # is_blend is excluded from _IDENTITY_FIELDS because it defaults to
        # False (never None/""), so the generic "not in (None, '')" test
        # above would mark it "on_page" even when the page never mentioned
        # blending. Record it explicitly, and only when the page actually
        # said True — the default False stays provenance-less, which is what
        # keeps "absent from field_sources" meaningful as "unset" (#587
        # fix 4).
        field_sources["is_blend"] = "on_page"
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

    try:
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
    except ValidationError as exc:
        # BeanProfileDraft.source_url runs a stricter validator (models.py —
        # rejects embedded userinfo and a malformed port) than the
        # scheme/host check _fetch_page_text already passed, so a fetched-ok
        # URL can still fail here (#587) — fail soft, not an unhandled
        # pydantic.ValidationError.
        raise BeanExtractionError(
            f"drafted bean profile failed validation for {url!r}: {exc}"
        ) from exc


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
