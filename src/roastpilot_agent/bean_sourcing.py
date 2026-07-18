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
:func:`_assert_public_destination` rejects any destination whose resolved
address is not ``is_global`` (this is the single primitive that correctly
covers loopback / private (RFC1918) / link-local (including the
``169.254.169.254`` cloud metadata address) / unspecified / IANA-reserved
*and* Carrier-Grade NAT (``100.64.0.0/10`` — Tailscale/CGNAT ranges a naive
private-only predicate misses), with multicast rejected alongside it
explicitly (``is_global`` alone does not exclude multicast — see the
function's own docstring) — checked on the origin URL *and* on every
redirect hop the internally-constructed client follows, since a public URL
can 302 into a private address just as easily as an operator can paste one
directly. The naive "validate the hostname, then let ``httpx`` connect to
the hostname" version of that check has a DNS-rebinding TOCTOU gap: a
short-TTL domain can answer a public address on the validation lookup and a
private/metadata address on ``httpx``'s own connect-time re-resolution (the
exact shape behind CVE-2026-27826 / GHSA-489g-7rxv-6c8q in other
fetch-URL-for-LLM tools). This module closes that gap by connect-time IP
pinning: :func:`_assert_public_destination` returns every validated address
a hostname resolved to, and :func:`_fetch_with_ssrf_guard` issues the
request against a literal IP from that list (``httpx.URL.copy_with(host=...)``,
trying the next candidate address on a connect failure — a dual-stack host
with a dead IPv6 route, or a transient CDN node, should not fail the whole
fetch) so there is no second resolution left to poison, while an explicit
``Host`` header and the ``sni_hostname`` request extension keep
routing/TLS identity (virtual host, SNI, certificate hostname check) pinned
to the ORIGINAL hostname. The internally-constructed client also disables
keepalive pooling (``httpx.Limits(max_keepalive_connections=0)``): with IP
pinning, two DIFFERENT hostnames that happen to resolve to the SAME address
would otherwise share one pooled connection/origin, and ``sni_hostname``
only applies when a connection is *opened* — a pooled reuse across a
host-changing redirect would silently skip re-validating the new host's TLS
identity. The only remaining residual is parser-differential risk — a
hostname the resolver and this module's own URL parsing could disagree on —
mitigated by validating and pinning from the exact same parsed host on every
hop. An operator-supplied URL with embedded credentials (``user:pass@host``)
or a fragment (``#...`` — can carry a sensitive token, e.g. an OAuth
redirect's ``#access_token=...``) is rejected up front, before any logging
or outbound request — and even that rejection path aside, the source URL
is only ever logged in a credential-and-fragment-redacted form
(:func:`_redact_url_credentials`). The internally-constructed client sets
``trust_env=False``: an operator/system ``HTTPS_PROXY`` would otherwise
route the fetch through a CONNECT-tunnelling proxy that TLS-verifies
against the pinned IP literal (the ``sni_hostname`` extension is not
honored by the tunnel), silently defeating connect-time pinning. Response
bodies are decompressed by THIS module, not ``httpx``: the fetch is
streamed via ``response.aiter_raw()`` (the still-compressed wire bytes,
capped to ``max_response_bytes`` before any decompression happens at all —
never ``aiter_bytes()``, whose *internal* per-chunk decompression has no
output-size bound of its own and so is itself a decompression-bomb vector)
and decoded via :func:`_decompress_within_cap`, which bounds the DECODED
output too (``zlib.decompressobj.decompress(..., max_length=...)``, the
stdlib-documented technique). Only ``gzip``/``deflate`` are requested
(``Accept-Encoding``) and decoded; this module declares no
``brotli``/``zstandard`` dependency, so it never asks a server to use them
and fails closed if one sends them anyway. The whole fetch (all hops + body
streaming) is additionally bounded by an end-to-end deadline independent of
``httpx``'s own per-request timeout, and the LLM extraction call is bounded
by its own deadline — both mapped to this module's typed errors, never left
to hang indefinitely. Drafting is also mutually exclusive with starting a
roast (:meth:`~roastpilot_agent.api.RoastService.draft_bean_from_url` holds
the same lock :meth:`~roastpilot_agent.api.RoastService.start_roast` does,
across its own active-run check AND the whole fetch+extraction) — a
bean-extraction LLM call sharing a resource-constrained provider (e.g.
local Ollama) with an active roast's advisor calls can starve them into the
controller's sustained-outage safety fallback.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
import zlib
from html import unescape
from urllib.parse import urljoin, urlsplit, urlunsplit

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


def _redact_url_credentials(url: str) -> str:
    """Return ``url`` with any embedded userinfo (``user:pass@``) AND any
    fragment stripped, for safe logging (#587 P1/P2: neither credentials
    nor a fragment — which can carry a sensitive token, e.g. an OAuth
    redirect's ``#access_token=...`` — may ever reach a log line, even
    though :func:`draft_bean_profile_from_url` also rejects a credentialed
    or fragment-bearing URL outright before any logging happens; this is
    the defense-in-depth backstop for every OTHER place the source URL is
    logged, now or in the future).

    Deliberately netloc-string-based rather than going through
    ``SplitResult.username``/``.password``/``.port`` (which can themselves
    raise on a malformed port, #587 P2): this helper is purely for a log
    line and must NEVER itself raise, even on a malformed URL — the
    ``urlsplit()`` call itself is guarded too (a malformed URL, e.g. an
    unclosed IPv6 bracket, makes it raise ``ValueError`` eagerly), falling
    back to returning ``url`` unchanged rather than raising out of a
    logging helper. When it does parse, everything up to and including the
    last ``@`` in the netloc is stripped (always the userinfo delimiter
    when one is present — the host/port portion of a netloc cannot itself
    contain an unescaped ``@``), and the fragment is dropped entirely.

    Args:
        url: The URL to redact.

    Returns:
        ``url`` with any userinfo and fragment removed; the original
        ``url`` unchanged if it carries neither, or if it fails to parse at
        all (this helper fails open to "log the original" rather than
        raising, since bailing out of a logging call would be worse).
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return url
    redacted_netloc = parsed.netloc.rsplit("@", 1)[-1] if "@" in parsed.netloc else parsed.netloc
    if redacted_netloc == parsed.netloc and not parsed.fragment:
        return url
    return urlunsplit(parsed._replace(netloc=redacted_netloc, fragment=""))


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
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Reject a fetch destination that is not publicly routable, and return
    EVERY validated address to try CONNECTING TO (#587 fix 1, SSRF guard +
    DNS-rebinding fix; #587 P2, multi-address resilience).

    Parses ``url``'s host. If it is an IP literal, validates that address
    directly; otherwise resolves it via the non-blocking asyncio resolver
    (``loop.getaddrinfo`` — never the blocking stdlib resolver) and validates
    EVERY address the host resolves to, since a hostname can round-robin or
    dual-stack across a mix of public and non-public addresses.

    An address is rejected unless ``address.is_global`` — the single
    ``ipaddress`` primitive that correctly covers loopback, private
    (RFC1918 and friends), link-local (this is what blocks the
    ``169.254.169.254`` cloud metadata endpoint), unspecified, and
    IANA-reserved, AND the Carrier-Grade NAT range ``100.64.0.0/10``
    (Tailscale and similar overlay networks) — a naive
    loopback/private/link-local/reserved-only predicate misses CGNAT
    entirely, since Python classifies it as neither private nor reserved.
    Multicast is rejected alongside it EXPLICITLY: ``is_global`` is defined
    as (approximately) "not private, with a CGNAT carve-out" and does NOT by
    itself exclude multicast (a multicast address is not in any "private"
    range, so ``is_global`` is ``True`` for one) — verified against the
    stdlib implementation, not assumed.

    Called on the origin URL and on every redirect ``Location`` the
    internally-constructed client is about to follow — a *public* URL can
    302 into a private address just as easily as an operator can paste one
    in directly (#587).

    The caller MUST connect to one of the returned addresses rather than
    letting ``httpx`` re-resolve the hostname itself (see
    :func:`_fetch_with_ssrf_guard`) — a hostname resolved and validated
    here, then handed to the HTTP client as a hostname, gives a short-TTL
    DNS record a second chance to answer differently at connect time (a
    "rebinding" race: public on this check, private/metadata on the real
    connect). Returning the validated addresses and pinning the actual
    connection to one of them closes that gap: there is no second
    resolution left to poison. Returning ALL of them (not just the first)
    lets the caller fall back to another address if the first one it tries
    is unreachable (a dual-stack host with a dead IPv6 route, or a
    transient CDN node) — a resolution race must not fail the whole fetch
    when a perfectly good alternate address was also returned.

    Args:
        url: The absolute URL about to be connected to (the origin URL, or a
            redirect hop's resolved ``Location``).

    Returns:
        Every validated address (the literal IP itself, in a single-element
        list, or every ``getaddrinfo`` result for a hostname).

    Raises:
        BeanFetchError: The scheme is not ``http``/``https``, the URL has no
            host or a malformed port, the host could not be resolved, or any
            address it resolves to (or the literal IP itself) is not
            globally routable.
    """
    try:
        # A malformed URL (e.g. an unclosed IPv6 bracket, "http://[::1")
        # makes urlsplit() raise ValueError EAGERLY — called on every hop
        # (including redirect targets this module itself resolves), so this
        # needs its own guard independent of _fetch_page_text's initial
        # check (#587 P2).
        parsed = urlsplit(url)
    except ValueError as exc:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r} ({exc})") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r}")

    host = parsed.hostname
    try:
        # ``urlsplit`` parses a bad port lazily: accessing ``.port`` raises
        # ``ValueError`` on a non-numeric or out-of-range port (#587 P2) —
        # left unguarded this becomes an unhandled 500 instead of the typed
        # fail-soft error every other malformed-URL case gets here.
        explicit_port = parsed.port
    except ValueError as exc:
        raise BeanFetchError(f"malformed port in {url!r}: {exc}") from exc
    port = explicit_port or (443 if parsed.scheme == "https" else 80)

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
        if not address.is_global or address.is_multicast:
            raise BeanFetchError(
                f"fetch destination {url!r} resolves to a non-public address "
                f"({address}) — blocked by the SSRF guard (#587)"
            )

    return addresses


def _append_within_cap(body: bytearray, chunk: bytes, *, max_bytes: int, url: str) -> None:
    """Append ``chunk`` to ``body`` IF doing so would stay within
    ``max_bytes`` — otherwise raise WITHOUT appending (#587 P1,
    compression-bomb guard).

    ``response.aiter_bytes()`` yields DECOMPRESSED bytes, so a
    highly-compressed gzip/brotli body can hand the caller a SINGLE chunk
    that blows past the cap on its own. Checking the running total BEFORE
    appending — rather than extending first and checking after — means
    ``body`` never transiently holds more than ``max_bytes``; a
    check-after-append implementation would also eventually raise here, but
    only after ``body`` had already ballooned past the cap for at least
    that one (potentially huge) chunk.

    Args:
        body: The buffer accumulated so far; mutated in place ONLY when the
            appended total would stay within ``max_bytes``.
        chunk: The next chunk read from the response stream.
        max_bytes: The configured response-size cap
            (``BeanSourcingConfig.max_response_bytes``).
        url: The URL being fetched (for the error message).

    Raises:
        BeanFetchError: Appending ``chunk`` would exceed ``max_bytes``.
    """
    if len(body) + len(chunk) > max_bytes:
        raise BeanFetchError(f"vendor page exceeded the {max_bytes}-byte fetch cap: {url!r}")
    body.extend(chunk)


#: Sent as this module's OWN ``Accept-Encoding`` (overriding ``httpx``'s
#: default, which also offers ``br``/``zstd`` whenever those optional
#: decoder libraries happen to be installed) — deliberately gzip/deflate
#: ONLY (#587 P1). Both are decoded here via stdlib ``zlib`` with a
#: cap-bounded ``decompress(data, max_length=...)`` call (see
#: :func:`_decompress_within_cap`); this module declares no
#: ``brotli``/``brotlicffi``/``zstandard`` dependency in ``pyproject.toml``
#: and imports none, so it has no safe way to bound a brotli/zstd
#: decompression the same way — a compliant server simply never sends
#: either, because we never asked for them.
_ACCEPT_ENCODING = "gzip, deflate"


def _decompress_within_cap(
    raw_body: bytes, content_encoding: str, *, max_bytes: int, url: str
) -> bytes:
    """Decompress ``raw_body`` per ``content_encoding``, bounded to
    ``max_bytes`` of DECODED output (#587 P1, compression-bomb guard —
    round 2).

    ``raw_body`` is the STILL-COMPRESSED response body, already capped to
    ``max_bytes`` raw bytes by the caller (streamed via
    ``response.aiter_raw()``, never ``aiter_bytes()`` — see the callers).
    That raw cap alone is not enough: a highly-compressed body can still
    decompress to something enormous. ``zlib.decompressobj.decompress``'s
    ``max_length`` parameter bounds the OUTPUT of a single call — the
    stdlib-documented technique for safely decompressing untrusted data —
    so this never allocates more than ``max_bytes + 1`` decoded bytes
    REGARDLESS of the compression ratio, in one bounded call (no
    hand-rolled incremental multi-call draining loop needed, since the raw
    input is already fully buffered and capped by the time this runs).

    Only ``gzip``/``deflate``/absent/``identity`` are decoded — anything
    else (``br``, ``zstd``, an unknown/typo'd value) is REJECTED rather
    than silently treated as identity (which would hand the LLM extraction
    step raw compressed garbage) or decompressed by a library this module
    does not import (see :data:`_ACCEPT_ENCODING`): this module only ever
    REQUESTS gzip/deflate, so a compliant server never sends anything else;
    a non-compliant one that does anyway fails closed here rather than
    silently corrupting the extracted text.

    Args:
        raw_body: The still-compressed (or identity) response body.
        content_encoding: The response's ``Content-Encoding`` header value
            (``""`` when absent).
        max_bytes: The configured response-size cap
            (``BeanSourcingConfig.max_response_bytes``).
        url: The URL being fetched (for the error message).

    Returns:
        The decoded (decompressed) bytes.

    Raises:
        BeanFetchError: ``content_encoding`` is not one of the encodings
            this module requests and knows how to decode safely, the
            decoded output would exceed ``max_bytes``, or the body does not
            decompress cleanly under its declared encoding.
    """
    normalized = content_encoding.strip().lower()
    if normalized in ("", "identity"):
        return raw_body
    if normalized not in ("gzip", "x-gzip", "deflate"):
        raise BeanFetchError(
            f"vendor page used an unsupported Content-Encoding "
            f"{content_encoding!r} for {url!r} (only gzip/deflate are "
            "requested and decoded)"
        )
    try:
        if normalized == "deflate":
            try:
                decompressor = zlib.decompressobj()
                decoded = decompressor.decompress(raw_body, max_bytes + 1)
            except zlib.error:
                # Some servers send raw DEFLATE (no zlib header) despite
                # the "deflate" name — retry with a raw window, mirroring
                # httpx's own DeflateDecoder compatibility shim.
                decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
                decoded = decompressor.decompress(raw_body, max_bytes + 1)
        else:
            decompressor = zlib.decompressobj(zlib.MAX_WBITS | 16)
            decoded = decompressor.decompress(raw_body, max_bytes + 1)
        if decompressor.unconsumed_tail:
            # decompress() stopped at max_length with more input left to
            # process — the decoded output would have exceeded the cap.
            raise BeanFetchError(
                f"vendor page exceeded the {max_bytes}-byte fetch cap "
                f"(after decompression) for {url!r}"
            )
        decoded += decompressor.flush()
    except zlib.error as exc:
        raise BeanFetchError(
            f"vendor page failed to decompress ({content_encoding!r}) for {url!r}: {exc}"
        ) from exc
    if len(decoded) > max_bytes:
        raise BeanFetchError(
            f"vendor page exceeded the {max_bytes}-byte fetch cap (after decompression) for {url!r}"
        )
    return decoded


def _decode_response_body(body: bytes, response: httpx.Response) -> str:
    """Decode a raw fetched body using the response's declared charset
    (#587 P2) instead of assuming UTF-8.

    ``response.encoding`` reads the ``charset`` parameter off the
    ``Content-Type`` header when present, falling back to UTF-8 otherwise —
    it is safe to read here even though the body was collected manually via
    ``aiter_bytes()`` (to enforce the streaming size cap) rather than
    ``response.read()``/``response.text``, because the default
    ``default_encoding="utf-8"`` never needs the body itself to resolve. A
    vendor page served as e.g. ``text/html; charset=iso-8859-1`` must decode
    under ITS declared charset, not get silently corrupted into replacement
    characters by an unconditional UTF-8 decode. ``errors="replace"`` is
    kept as the final fallback so a body that does not even decode cleanly
    under its own declared charset still yields text rather than raising.

    Args:
        body: The raw fetched bytes (already capped to the configured size
            limit).
        response: The ``httpx.Response`` whose headers determine the
            encoding.

    Returns:
        The decoded text.
    """
    return body.decode(response.encoding or "utf-8", errors="replace")


async def _fetch_one_hop(
    client: httpx.AsyncClient,
    current_url: str,
    candidate_addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address],
    *,
    headers: dict[str, str],
    timeout: httpx.Timeout,
    config: BeanSourcingConfig,
) -> tuple[str, bool]:
    """Fetch a single hop of ``current_url``, trying each of its validated
    candidate addresses in turn until one connects (#587 P2: a dual-stack
    host with a dead route on one family, or a transient CDN node, must not
    fail the whole fetch when an alternate resolved address would work).
    Catches both ``httpx.ConnectError`` (e.g. connection refused) AND
    ``httpx.ConnectTimeout`` (a "black-holed" address — a route exists but
    nothing ever answers) — the two are SIBLING exceptions under
    ``httpx.TransportError``, not a subclass relationship, so a fallback
    loop that only caught ``ConnectError`` would silently give up on a
    timed-out address instead of trying the next one.

    Every attempt is pinned to its candidate IP literal
    (``httpx.URL.copy_with(host=...)``) with the original hostname preserved
    via an explicit ``Host`` header and the ``sni_hostname`` extension (SNI +
    certificate-hostname identity) — see :func:`_fetch_with_ssrf_guard`.

    When there is more than one candidate, each attempt's CONNECT phase is
    bounded to ``timeout.connect`` divided by the candidate count (#587 P2)
    — read/write/pool stay at the full configured value. Without this, one
    black-holed FIRST address could consume the entire per-request connect
    budget, leaving no time for the fallback loop to even attempt a second
    address before the caller's outer end-to-end deadline
    (``asyncio.timeout`` in :func:`_fetch_page_text`) also expires — the
    per-address bound is what actually gives the fallback a chance to run.

    Args:
        client: The internally-constructed ``httpx.AsyncClient``.
        current_url: This hop's (hostname-based) URL.
        candidate_addresses: Every address :func:`_assert_public_destination`
            validated for ``current_url``'s host, tried in order.
        headers: Request headers (the identifying User-Agent).
        timeout: The per-request ``httpx.Timeout`` (its ``connect`` value is
            subdivided across candidates; ``read``/``write``/``pool`` are
            reused as-is).
        config: Response-size-cap settings.

    Returns:
        ``(text_or_next_url, is_redirect)``: when ``is_redirect`` is
        ``True``, the first element is the resolved absolute redirect
        target URL for the caller to validate and fetch next; otherwise it
        is the final decoded page text.

    Raises:
        BeanFetchError: Every candidate address failed to connect (or
            timed out connecting), a non-2xx/non-3xx-with-``Location``
            response, a body over the size cap, a bare 3xx with no
            ``Location`` header, or a malformed redirect ``Location``.
    """
    try:
        # ``httpx.URL()`` uses a DIFFERENT (stricter, in some ways) parser
        # than the stdlib ``urlsplit()`` this hop's url was already checked
        # with — e.g. a NUL byte in the path passes ``urlsplit`` but
        # ``httpx.URL()`` raises ``httpx.InvalidURL`` for it, which is NOT
        # an ``httpx.HTTPError`` subclass and so bypasses the generic
        # mapping in :func:`_fetch_page_text`, escaping as an unhandled 500
        # (#587 P2).
        original_url = httpx.URL(current_url)
    except httpx.InvalidURL as exc:
        raise BeanFetchError(f"not a well-formed http(s) URL: {current_url!r} ({exc})") from exc
    pinned_headers = {**headers, "Host": original_url.netloc.decode("ascii")}
    per_address_timeout = timeout
    if len(candidate_addresses) > 1 and timeout.connect is not None:
        per_address_timeout = httpx.Timeout(
            connect=timeout.connect / len(candidate_addresses),
            read=timeout.read,
            write=timeout.write,
            pool=timeout.pool,
        )
    last_connect_error: httpx.ConnectError | httpx.ConnectTimeout | None = None
    for candidate_address in candidate_addresses:
        pinned_url = original_url.copy_with(host=str(candidate_address))
        try:
            async with client.stream(
                "GET",
                pinned_url,
                headers=pinned_headers,
                timeout=per_address_timeout,
                extensions={"sni_hostname": original_url.host},
            ) as response:
                if 300 <= response.status_code < 400:
                    location = response.headers.get("location")
                    if not location:
                        raise BeanFetchError(
                            f"vendor page redirected (HTTP {response.status_code}) with no "
                            f"Location header for {current_url!r}"
                        )
                    try:
                        # A malformed Location (e.g. an unclosed IPv6
                        # bracket, "http://[::1") makes urljoin() raise
                        # ValueError, which would otherwise escape as an
                        # unhandled 500 (#587 P2).
                        next_url = urljoin(current_url, location)
                    except ValueError as exc:
                        raise BeanFetchError(
                            f"vendor page redirected to a malformed Location "
                            f"{location!r} for {current_url!r}: {exc}"
                        ) from exc
                    return next_url, True
                if response.status_code >= 400:
                    raise BeanFetchError(
                        f"vendor page fetch failed: HTTP {response.status_code} for {current_url!r}"
                    )
                # aiter_raw(), never aiter_bytes() (#587 P1 round 2):
                # aiter_bytes() runs httpx's OWN internal decompression per
                # network chunk with no output-size bound at all, so a
                # single (still small, still within our raw cap) chunk
                # could already have been decompressed into something huge
                # INSIDE httpx before _append_within_cap ever saw it.
                # aiter_raw() yields the STILL-COMPRESSED bytes as received
                # off the wire; we cap those, then decompress ourselves
                # with an explicit output bound (_decompress_within_cap).
                raw_body = bytearray()
                async for chunk in response.aiter_raw():
                    _append_within_cap(
                        raw_body, chunk, max_bytes=config.max_response_bytes, url=current_url
                    )
                decoded = _decompress_within_cap(
                    bytes(raw_body),
                    response.headers.get("content-encoding", ""),
                    max_bytes=config.max_response_bytes,
                    url=current_url,
                )
                return _decode_response_body(decoded, response), False
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_connect_error = exc
            continue
    raise BeanFetchError(
        f"could not connect to any resolved address for {current_url!r}: {last_connect_error}"
    ) from last_connect_error


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
    one of its validated addresses (#587 fix 1b, closes the DNS-rebinding
    TOCTOU gap; #587 P2, tries every validated address before giving up).

    Per hop: :func:`_assert_public_destination` validates the CURRENT
    (hostname-based) URL and returns every address to try;
    :func:`_fetch_one_hop` issues the request against those addresses in
    turn, literally (``httpx.URL.copy_with(host=...)``), so
    ``httpx``/``httpcore`` never re-resolves the hostname themselves —
    there is no second DNS lookup a rebinding attack could win. Routing and
    TLS identity stay pinned to the ORIGINAL hostname: an explicit ``Host``
    header (mirroring what ``httpx`` would have sent had it connected to
    the hostname directly) preserves virtual-host routing, and the
    ``sni_hostname`` request extension keeps the TLS ClientHello's SNI —
    and therefore certificate hostname verification — targeting the real
    host, not the IP. The next hop's ``Location`` is resolved against the
    ORIGINAL (hostname) URL, never the IP-pinned one, so a relative
    redirect stays correct.

    Only used for the client this module constructs itself (never an
    injected ``http_client`` — see :func:`_fetch_page_text`); that client is
    built with ``follow_redirects=False`` (so ``httpx`` never follows a
    redirect for us before we get a chance to validate/pin its destination)
    and ``limits=httpx.Limits(max_keepalive_connections=0)`` (so a
    host-changing redirect that happens to pin to the SAME address as a
    prior hop can never reuse that hop's pooled connection — and therefore
    its already-negotiated TLS identity — for the NEW host; ``sni_hostname``
    only takes effect when a connection is opened, not when one is reused).

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
            every validated address failing to connect, a
            non-2xx/non-3xx-with-``Location`` response, a body over the
            size cap, a bare 3xx with no ``Location`` header, or more than
            :data:`_MAX_REDIRECTS` redirects.
    """
    current_url = url
    for _ in range(_MAX_REDIRECTS + 1):
        candidate_addresses = await _assert_public_destination(current_url)
        result, is_redirect = await _fetch_one_hop(
            client,
            current_url,
            candidate_addresses,
            headers=headers,
            timeout=timeout,
            config=config,
        )
        if is_redirect:
            current_url = result
            continue
        return result
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
    try:
        # A malformed URL (e.g. an unclosed IPv6 bracket, "http://[::1")
        # makes urlsplit() raise ValueError EAGERLY (unlike a bad port,
        # which it only raises on lazily via .port) — left unguarded this
        # escapes as an unhandled 500 instead of the typed fail-soft error
        # every other malformed-URL case here gets (#587 P2).
        parsed = urlsplit(url)
    except ValueError as exc:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r} ({exc})") from exc
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r}")

    headers = {"User-Agent": config.user_agent, "Accept-Encoding": _ACCEPT_ENCODING}
    timeout = httpx.Timeout(config.fetch_timeout_seconds)
    owns_client = http_client is None
    client = (
        http_client
        if http_client is not None
        else httpx.AsyncClient(
            follow_redirects=False,
            # No keepalive pooling (#587 P2): with connect-time IP pinning,
            # two DIFFERENT hostnames that happen to resolve to the SAME
            # address would otherwise share one pooled connection/origin —
            # and ``sni_hostname`` only applies when a connection is
            # OPENED, not when a pooled one is reused, so a host-changing
            # redirect could silently skip re-validating the new host's TLS
            # identity. Forcing a fresh connection per request closes that.
            limits=httpx.Limits(max_keepalive_connections=0),
            # No env proxies (#587 P2, round 5): httpx defaults to
            # trust_env=True, so an operator/system HTTPS_PROXY would route
            # this fetch through a CONNECT-tunnelling proxy that TLS-
            # verifies against the PINNED IP LITERAL, not the original
            # hostname the sni_hostname extension names — silently
            # bypassing the whole connect-time-pinning defense above. The
            # injected client (test seam / future caller) is untouched;
            # its proxy behavior is the caller's to set.
            trust_env=False,
        )
    )
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
                    # aiter_raw() + our own bounded decompress — see the
                    # owns-client path's identical comment in
                    # _fetch_one_hop (#587 P1 round 2).
                    raw_body = bytearray()
                    async for chunk in response.aiter_raw():
                        _append_within_cap(
                            raw_body, chunk, max_bytes=config.max_response_bytes, url=url
                        )
                    decoded = _decompress_within_cap(
                        bytes(raw_body),
                        response.headers.get("content-encoding", ""),
                        max_bytes=config.max_response_bytes,
                        url=url,
                    )
                    html = _decode_response_body(decoded, response)
    except TimeoutError as exc:
        raise BeanFetchError(
            f"vendor page fetch exceeded the {config.fetch_timeout_seconds:g}s end-to-end "
            f"deadline for {url!r}"
        ) from exc
    except BeanFetchError:
        raise
    except httpx.InvalidURL as exc:
        # httpx.URL()'s parser is stricter than urlsplit() in some ways
        # (e.g. a NUL byte in the path passes urlsplit() but httpx.URL()
        # rejects it) — httpx.InvalidURL is NOT an httpx.HTTPError
        # subclass, so it would bypass the generic mapping below and
        # escape as an unhandled 500 (#587 P2). Reachable here via the
        # injected-client path's client.stream(url) internally parsing
        # ``url``; the owns-client path's own httpx.URL() call (in
        # _fetch_one_hop) is already guarded at the source.
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r} ({exc})") from exc
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
    is_blend: bool | None = None
    """Tri-state, not a plain ``bool`` with a False default (#587 P2): the
    page can state EITHER "this is a blend" (``True``) OR "this is a single
    origin" (``False``) OR say nothing about it at all (``None``) — a bare
    ``bool`` cannot distinguish the second case from the third, which would
    make an unstated page silently look like an on-page "not a blend"
    claim. See :data:`_EXTRACTION_INSTRUCTIONS` and
    :func:`_draft_from_identity`'s provenance handling."""


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
- is_blend: true if the page explicitly says this is a blend of multiple
  origins; false if the page explicitly states or clearly identifies a
  SINGLE origin (even one with a mixed varietal) — a named single farm,
  region, or country IS a single-origin statement; leave null ONLY if the
  page does not address single-origin-vs-blend at all.
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


def _normalize_optional_text(value: str | None) -> str | None:
    """Strip an optional identity string; blank-after-strip normalizes to
    ``None`` (#587 P2).

    Matches :class:`~roastpilot_agent.models.BeanProfileDraft`'s OWN
    optional-text validators (``_BeanProfileFieldsBase._strip_optional_identity``
    for ``country``/``farm``/``description``), applied here FIRST so the
    same normalization is visible to both the provenance-tagging loop and
    the draft construction below — without this, a whitespace-only page
    value would get tagged ``"on_page"`` for a field that then silently
    becomes ``None`` on construction (a provenance lie), and for
    ``bean_varietal`` specifically (whose OWN base-model validator,
    ``_strip_and_require_content``, RAISES on a whitespace-only value rather
    than normalizing it) an un-normalized whitespace-only extraction would
    reject the WHOLE draft instead of just being treated as unstated.

    Args:
        value: The raw extracted value, or ``None``.

    Returns:
        The stripped value, or ``None`` if it was ``None`` or blank.
    """
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _draft_from_identity(identity: _ExtractedBeanIdentity, *, url: str) -> BeanProfileDraft:
    """Assemble the :class:`BeanProfileDraft` from an extracted identity.

    Applies the conservative scouting targets
    (:data:`_SCOUTING_TARGETS_BY_PROCESSING`) and builds the honest per-field
    ``field_sources`` map: every identity field the page stated is
    ``"on_page"``; every roast-target field is always ``"origin_estimated"``.
    The optional free-text fields (``country``, ``farm``, ``bean_varietal``,
    ``description``) are normalized via :func:`_normalize_optional_text`
    BEFORE both the provenance loop and the draft construction (#587 P2) —
    see that function's docstring for why the ordering matters.

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
    country = _normalize_optional_text(identity.country)
    # Normalized BEFORE the fallback chain (#587 P2, round 5): a raw
    # whitespace-only identity.bean_origin is truthy, so an un-normalized
    # ``identity.bean_origin or country`` would let it WIN the fallback
    # over a perfectly good page-sourced country, then strip to empty and
    # wrongly reject the whole draft as having no usable origin.
    raw_bean_origin = _normalize_optional_text(identity.bean_origin)
    bean_origin = raw_bean_origin or country or ""
    farm = _normalize_optional_text(identity.farm)
    bean_varietal = _normalize_optional_text(identity.bean_varietal)
    description = _normalize_optional_text(identity.description)
    if not name or not bean_origin:
        raise BeanExtractionError(
            f"could not determine a bean name and origin from the page ({url!r}) "
            "— add the profile manually instead"
        )

    drop_temp_c, dev_percent = _SCOUTING_TARGETS_BY_PROCESSING.get(
        identity.processing, _SCOUTING_TARGETS_BY_PROCESSING[None]
    )

    # The values used for provenance tagging — every one already
    # normalized where normalization matters (#587 P2: country/farm/
    # bean_varietal/description/bean_origin). ``bean_origin`` here is the
    # NORMALIZED-BUT-PRE-FALLBACK ``raw_bean_origin``, NOT the local
    # ``bean_origin`` var (which already has the fallback-to-country
    # applied): the generic loop below must tag "on_page" only when the
    # page stated bean_origin DIRECTLY, so the separate fallback-to-country
    # special case immediately below stays reachable and meaningful (and
    # the two stay honestly distinguishable — not that it changes the
    # resulting provenance value, both are "on_page", but it keeps the
    # branch structure legible). ``name`` is similarly its own local
    # stripped var — the page's usable-name check already guarantees it is
    # non-blank by this point.
    identity_values: dict[str, object] = {
        "name": name,
        "country": country,
        "bean_origin": raw_bean_origin,
        "farm": farm,
        "bean_varietal": bean_varietal,
        "processing": identity.processing,
        "bean_species": identity.bean_species,
        "altitude_m": identity.altitude_m,
        "description": description,
    }

    field_sources: dict[str, BeanFieldSource] = {}
    for field_name in _IDENTITY_FIELDS:
        raw_value = identity_values[field_name]
        if raw_value not in (None, ""):
            field_sources[field_name] = "on_page"
    if "bean_origin" not in field_sources and country:
        # bean_origin fell back to country — still page-sourced, just via the
        # country field rather than an explicit bean_origin statement.
        field_sources["bean_origin"] = "on_page"
    if identity.is_blend is not None:
        # is_blend is excluded from _IDENTITY_FIELDS because its "the page
        # said nothing" value is None, not ""/False — the generic
        # "not in (None, '')" test above would work for None but a bare
        # ``False`` used to be indistinguishable from "unstated" before
        # #587 P2 made this field tri-state. Now: on_page for an explicit
        # True OR an explicit False (the page addressed single-origin vs
        # blend either way), and no field_sources entry at all when the
        # page said nothing (identity.is_blend is None) — "absent from
        # field_sources" stays meaningful as "unset".
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
            bean_varietal=bean_varietal,
            country=country,
            farm=farm,
            description=description,
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
        BeanFetchError: The URL embeds credentials (``user:pass@host``) or
            a fragment (``#...``, #587 P1/P2 — both checked FIRST, before
            any logging or outbound request), or the vendor page could not
            be fetched.
        BeanExtractionError: The LLM call failed, or the page yielded too
            little identity to draft a profile from.
    """
    # Credential-leak guard (#587 P1): checked before ANYTHING else — no
    # logging, no fetch, no billable LLM call — so a URL with embedded
    # basic-auth credentials is never sent over the wire (to the vendor OR
    # to the LLM provider) and never appears in a log line even transiently.
    # A malformed URL (e.g. an unclosed IPv6 bracket) makes urlsplit() raise
    # ValueError eagerly (#587 P2) — this is the very first thing ANY url
    # goes through, so it needs its own guard rather than relying on a
    # later call site to catch it.
    try:
        parsed_url = urlsplit(url)
    except ValueError as exc:
        raise BeanFetchError(f"not a well-formed http(s) URL: {url!r} ({exc})") from exc
    if parsed_url.username is not None or parsed_url.password is not None:
        _log.warning(
            "draft_bean_profile_from_url: rejected a URL with embedded credentials: %r",
            _redact_url_credentials(url),
        )
        raise BeanFetchError(
            "vendor URLs with embedded credentials (user:pass@host) are not "
            "supported — remove the credentials from the URL and, if the "
            "page needs authentication, save the profile manually instead"
        )
    if parsed_url.fragment:
        # A fragment (#587 P2, round 5) is never sent to the vendor over
        # HTTP (fragments are client-side-only, per the URL spec) — the
        # risk is entirely in what THIS module does with the raw ``url``
        # value itself: it is logged (mirroring the credential leak above)
        # and, worse, carried verbatim into the returned draft's
        # ``source_url`` — so a URL an operator pasted straight out of an
        # OAuth redirect (``#access_token=...``) or a hash-router page
        # would leak that token into logs and into a saved bean profile.
        # Mirrors the credential check exactly: rejected up front, logged
        # only in fragment-and-credential-redacted form.
        _log.warning(
            "draft_bean_profile_from_url: rejected a URL with a fragment: %r",
            _redact_url_credentials(url),
        )
        raise BeanFetchError(
            "vendor URLs with a fragment (#...) are not supported — a "
            "fragment can carry a sensitive token (e.g. an OAuth redirect's "
            "#access_token=...) that must never be fetched, logged, or "
            "stored; remove the fragment from the URL"
        )

    config = sourcing_config if sourcing_config is not None else BeanSourcingConfig()
    _log.info("draft_bean_profile_from_url: fetching %r", _redact_url_credentials(url))
    page_text = await _fetch_page_text(url, config=config, http_client=http_client)
    identity = await _extract_bean_identity(page_text, advisor_config=advisor_config, model=model)
    draft = _draft_from_identity(identity, url=url)
    _log.info(
        "draft_bean_profile_from_url: drafted %r (%d fields sourced)",
        draft.name,
        len(draft.field_sources),
    )
    return draft
