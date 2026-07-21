"""#573 phase 1: add-bean-from-URL backend tests.

Fakes BOTH collaborators — the vendor-page fetch (``httpx.MockTransport``,
never real network) and the structured LLM call (``pydantic_ai``'s
``FunctionModel`` recorded-response double, mirroring ``test_advisor.py``'s
convention, never a real provider) — so the whole pipeline is deterministic
and hardware/network/LLM-free.

Covers: the draft mapping from a sample page, the honest per-field
provenance (on_page vs origin_estimated), the conservative scouting targets
by processing method, fail-soft on a fetch error and on an LLM/extraction
error (typed, never an unhandled exception), the endpoint happy path + error
codes (in ``test_api.py``), and — critically — that this module never
touches the roaster/control path (imports checked directly here; the whole
transitive import graph checked in a fresh subprocess).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import gzip
import ipaddress
import logging
import socket
import subprocess
import sys
import threading
import time
import unittest.mock
import zlib
from collections.abc import AsyncGenerator, Callable
from typing import Literal

import extruct  # type: ignore[import-untyped]
import httpx
import pytest
from pydantic import ValidationError
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from roastpilot_agent import bean_sourcing
from roastpilot_agent.advisor import AdvisorDependencyError
from roastpilot_agent.bean_sourcing import (
    BeanExtractionError,
    BeanExtractionUnavailableError,
    BeanFetchError,
    draft_bean_profile_from_url,
)
from roastpilot_agent.config import OPENROUTER_BASE_URL, AdvisorConfig, BeanSourcingConfig
from roastpilot_agent.models import BeanProfileDraft

# --- test doubles ---


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


async def _bytes_stream(data: bytes) -> AsyncGenerator[bytes, None]:
    """A single-chunk async byte stream (#587 P1 round 2/5): plain
    ``httpx.Response(status, content=<bytes>)`` PRE-BUFFERS the body and
    marks the response's stream as ALREADY CONSUMED — harmless for
    ``aiter_bytes()`` (which special-cases pre-buffered content and just
    chunks it) but fatal for ``aiter_raw()`` (which this module's fetch
    path now uses instead — no such special case, so it raises
    ``httpx.StreamConsumed``). Every test double that constructs a response
    BODY must go through this helper (or its own async generator, like the
    slow-body timeout test does) instead of a bare ``content=bytes``, to
    behave like an actual streamed-over-the-wire HTTP response rather than
    a fully-buffered-upfront one — no production response is ever
    pre-buffered like that, so this is a MORE realistic fixture too, not
    just a workaround."""
    yield data


def _html_response(status_code: int, html: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=_bytes_stream(html.encode()))

    return httpx.MockTransport(handler)


#: A GENUINELY global address (example.com's real A record) the no-op
#: destination stub below "resolves" every host to — not TEST-NET
#: (``203.0.113.0/24`` and friends), which the ``is_global`` SSRF predicate
#: (#587 CGNAT fix) now correctly rejects as non-public, since TEST-NET is a
#: documentation/reserved range. Needs to be a real, syntactically valid
#: public IP literal so the pinning code path (which needs a real
#: ``ipaddress`` object to pin to) has something to pin to.
_STUB_PUBLIC_IP = ipaddress.ip_address("93.184.216.34")


async def _noop_assert_public_destination(
    url: str,
) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """Test double: skips the real DNS/IP validation (#587's SSRF guard),
    "resolving" every host to :data:`_STUB_PUBLIC_IP` so client-lifecycle/
    redirect-following tests can drive the (now pinning) fetch path without
    depending on a real resolver. Tests that need to assert the ORIGINAL
    hostname is preserved (Host header, SNI) still can — this only stubs the
    validated CONNECTION target; destination validation itself, and the
    real pinning mechanism, are covered separately."""
    return [_STUB_PUBLIC_IP]


_SAMPLE_HTML = """
<html>
<head><title>ignored</title><style>.x { color: red; }</style></head>
<body>
<script>var trackme = 1;</script>
<h1>Kenya Kiambu AA (Washed)</h1>
<p>Origin: Kenya. Region: Kiambu.</p>
<p>Farm: Gakuyuini Factory.</p>
<p>Variety: SL28, SL34.</p>
<p>Process: washed.</p>
<p>Altitude: 1,700-1,850m.</p>
<p>Tasting notes: blackcurrant, tomato, bright acidity.</p>
</body>
</html>
"""


#: A page corpus containing every default ``_identity_args()`` value
#: verbatim (or, for ``altitude_m``, its digit run) — the default ``corpus``
#: for ``_draft_from_identity`` tests that expect the default identity's
#: fields to verify as ``"on_page"`` (#590 D1).
_IDENTITY_PAGE_TEXT = (
    "Kenya Kiambu AA (Washed) is a washed coffee from Kenya, grown on the "
    "Gakuyuini Factory farm. Variety: SL28, SL34. Altitude: 1775m. "
    "Tasting notes: blackcurrant, tomato, bright acidity."
)


def _identity_args(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": "Kenya Kiambu AA (Washed)",
        "country": "Kenya",
        "bean_origin": "Kenya",
        "farm": "Gakuyuini Factory",
        "bean_varietal": "SL28, SL34",
        "processing": "washed",
        "bean_species": None,
        "altitude_m": 1775,
        "description": "Blackcurrant, tomato, bright acidity.",
        "is_blend": None,
    }
    base.update(overrides)
    return base


def _function_model_returning(args: dict[str, object]) -> FunctionModel:
    """A recorded-response double: the model always calls its output tool
    with ``args`` (mirrors ``test_advisor.py``'s helper of the same name)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, args)])

    return FunctionModel(respond)


def _function_model_text(text: str) -> FunctionModel:
    """A double that only ever returns prose — never the output tool, so
    structured-output extraction exhausts retries (a malformed shape)."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(respond)


def _function_model_raising(exc: BaseException) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise exc

    return FunctionModel(respond)


def _function_model_hanging() -> FunctionModel:
    """A double that never returns — used to exercise the extraction
    end-to-end timeout (#587 fix 3) without a real slow provider."""

    async def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.sleep(10)
        return ModelResponse(parts=[TextPart("too late")])  # pragma: no cover

    return FunctionModel(respond)


_ADVISOR_CONFIG = AdvisorConfig()


# --- _extract_page_text ---


def test_extract_page_text_strips_scripts_styles_and_tags() -> None:
    text = bean_sourcing._extract_page_text(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]
    assert "trackme" not in text
    assert "color: red" not in text
    assert "<h1>" not in text
    assert "Kenya Kiambu AA (Washed)" in text
    assert "Gakuyuini Factory" in text


def test_extract_page_text_unescapes_entities_and_collapses_whitespace() -> None:
    html = "<p>Sweet &amp; bright,&nbsp;&nbsp;citrus</p>\n\n\n\n<p>more</p>"
    text = bean_sourcing._extract_page_text(html)  # pyright: ignore[reportPrivateUsage]
    assert "&amp;" not in text
    assert "Sweet & bright" in text
    assert "\n\n\n" not in text


def test_extract_page_text_truncates_long_pages() -> None:
    huge = "<p>" + ("a" * 50_000) + "</p>"
    text = bean_sourcing._extract_page_text(huge)  # pyright: ignore[reportPrivateUsage]
    assert len(text) == bean_sourcing._MAX_EXTRACTED_CHARS  # pyright: ignore[reportPrivateUsage]


# --- #587 P1 rounds 7-8: linear-time script/style + tag strip (ReDoS fix) ---


def test_strip_script_and_style_blocks_removes_well_formed_elements() -> None:
    strip = bean_sourcing._strip_script_and_style_blocks  # pyright: ignore[reportPrivateUsage]
    html = "<p>Before</p><script>var x = 1 < 2;</script><p>After</p>"
    result = strip(html)
    assert "var x" not in result
    assert "Before" in result
    assert "After" in result


def test_strip_script_and_style_blocks_handles_element_ending_at_string_end() -> None:
    """Covers the "no more open tags AND nothing left after the last
    closed element" branch — the html ends immediately after </script>,
    with no trailing text at all."""
    strip = bean_sourcing._strip_script_and_style_blocks  # pyright: ignore[reportPrivateUsage]
    html = "<script>var x = 1;</script>"
    result = strip(html)
    assert "var x" not in result


def test_strip_script_and_style_blocks_handles_unterminated_script_tag() -> None:
    """#587 P1: an unterminated ``<script>`` (no matching ``</script>``
    anywhere) must not raise or hang — the rest of the document, having no
    matching close, is treated as part of the (unterminated) element and
    dropped; content BEFORE the open tag is preserved."""
    strip = bean_sourcing._strip_script_and_style_blocks  # pyright: ignore[reportPrivateUsage]
    html = "<p>Before</p><script>var x = 1; // never closed"
    result = strip(html)
    assert "Before" in result
    assert "var x" not in result


def test_extract_page_text_handles_unterminated_script_tag() -> None:
    text = bean_sourcing._extract_page_text(  # pyright: ignore[reportPrivateUsage]
        "<p>Kenya Kiambu AA</p><script>var x = 1;"
    )
    assert "Kenya Kiambu AA" in text
    assert "var x" not in text


def test_extract_page_text_pathological_unterminated_tags_completes_quickly() -> None:
    """#587 P1, the actual ReDoS: the PRIOR backtracking regex
    (``<(script|style)\\b[^>]*>.*?</\\1>``, ``DOTALL``) re-scanned the
    ENTIRE remainder of the document from EVERY failed/unterminated open
    tag — O(n) per tag, O(n^2) total for n such tags. A ~90 KB payload
    with ~10k unterminated ``<script>`` opens (this test's exact shape)
    would take unacceptably long (multi-second-to-minutes, scaling with
    the payload) under that implementation. The linear-time replacement
    must process this in a small fraction of a second — asserted with a
    generous-for-CI-noise but still discriminating bound; the quadratic
    version would blow past it by orders of magnitude, not narrowly."""
    payload = "<script>" * 10_000 + "a" * 10_000
    assert len(payload) > 80_000

    started = time.monotonic()
    result = bean_sourcing._extract_page_text(payload)  # pyright: ignore[reportPrivateUsage]
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"script/style strip took {elapsed:.3f}s — looks quadratic again"
    # The (single, unterminated) element swallows everything from the
    # first <script> onward — nothing survives into the extracted text.
    assert result == ""


def test_tag_name_starts_at_handles_tag_name_at_string_end() -> None:
    """Covers the "tag name reaches exactly end-of-string, no character
    left to check for a word boundary" branch."""
    starts_at = bean_sourcing._tag_name_starts_at  # pyright: ignore[reportPrivateUsage]
    assert starts_at("<script", 0, "script") is True
    assert starts_at("<style", 0, "style") is True
    # "<scripty" is NOT a "script" tag — the char right after "script" is a
    # word character, so this is a longer, different tag name.
    assert starts_at("<scripty", 0, "script") is False


def test_strip_script_and_style_blocks_handles_missing_open_tag_close_bracket() -> None:
    """#587 P1, round 8 — THE bug round 7 missed: an opening tag with no
    ">" anywhere in the rest of the document (round 7's ``[^>]*``-based
    open-tag matcher was still quadratic on exactly this shape). Content
    BEFORE the broken opener is preserved; nothing after it survives."""
    strip = bean_sourcing._strip_script_and_style_blocks  # pyright: ignore[reportPrivateUsage]
    result = strip("<p>Before</p><script never closed at all")
    assert "Before" in result
    assert "never closed" not in result


def test_strip_script_and_style_blocks_handles_missing_close_tag_close_bracket() -> None:
    """The CLOSING tag itself is found (``</script``) but has no ">" after
    it anywhere in the remaining document."""
    strip = bean_sourcing._strip_script_and_style_blocks  # pyright: ignore[reportPrivateUsage]
    result = strip("<p>Before</p><script>var x = 1;</script no closing bracket")
    assert "Before" in result
    assert "var x" not in result


def test_strip_remaining_tags_handles_missing_close_bracket() -> None:
    strip = bean_sourcing._strip_remaining_tags  # pyright: ignore[reportPrivateUsage]
    result = strip("Before<div never closed at all")
    assert result == "Before "


@pytest.mark.parametrize(
    "payload_fn",
    [
        lambda: "<script " * 50_000,
        lambda: "<div " * 50_000,
    ],
    ids=["script-opener-no-close-bracket", "div-opener-no-close-bracket"],
)
def test_extract_page_text_pathological_opener_with_no_close_bracket_completes_quickly(
    payload_fn: Callable[[], str],
) -> None:
    """#587 P1, round 8 — the EXACT shape round 7 missed: an opener
    (``<script ``/``<div ``) repeated many times with a trailing SPACE but
    NO ">" anywhere. Round 7's ``[^>]*``/``[^>]+`` regexes each re-scanned
    the entire remaining document from every occurrence looking for a ">"
    that never comes — O(n) per occurrence, O(n^2) total (the SAME
    quadratic shape as the original finding, just moved to the attribute
    search). Genuinely linear now: must complete in a small fraction of a
    second even at 50,000 repetitions (~350-400 KB)."""
    payload = payload_fn()
    assert len(payload) > 200_000

    started = time.monotonic()
    result = bean_sourcing._extract_page_text(payload)  # pyright: ignore[reportPrivateUsage]
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"tag strip took {elapsed:.3f}s — still looks quadratic"
    # No ">" anywhere in the whole payload -> nothing survives past the
    # first opener; the result is empty either way.
    assert result == ""


# --- _fetch_page_text ---


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_malformed_url() -> None:
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "not-a-url", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_scheme_without_host() -> None:
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_url_with_unclosed_ipv6_bracket() -> None:
    """#587 P2: ``urlsplit()`` raises ``ValueError`` EAGERLY (unlike a bad
    port, which it only raises lazily via ``.port``) for a malformed IPv6
    bracket like ``http://[::1`` — left unguarded this escapes as an
    unhandled 500 instead of the typed fail-soft error every other
    malformed-URL case here gets."""
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://[::1", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_url_with_unclosed_ipv6_bracket() -> None:
    """Same malformed-bracket case, but reached via ``_assert_public_destination``
    directly — this function is called per-hop (including redirect targets
    ``_fetch_page_text``'s own initial check never sees), so it needs its
    own guard independent of that one (#587 P2)."""
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "http://[::1"
        )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_rejects_url_with_unclosed_ipv6_bracket() -> None:
    """Same malformed-bracket case, but reached via the PUBLIC entry point's
    own credential-check ``urlsplit()`` call — the very first thing ANY url
    goes through (#587 P2)."""
    with pytest.raises(BeanFetchError, match="well-formed"):
        await draft_bean_profile_from_url(
            "http://[::1",
            advisor_config=_ADVISOR_CONFIG,
            model=_function_model_returning(_identity_args()),
        )


@pytest.mark.asyncio
async def test_fetch_page_text_success_extracts_text() -> None:
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/kenya",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_fetch_page_text_raises_on_non_2xx() -> None:
    async with _mock_client(_html_response(404, "not found")) as client:
        with pytest.raises(BeanFetchError, match="404"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/gone",
                config=BeanSourcingConfig(),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_page_text_raises_on_oversized_body() -> None:
    huge_html = "<p>" + ("x" * 5000) + "</p>"
    async with _mock_client(_html_response(200, huge_html)) as client:
        with pytest.raises(BeanFetchError, match="fetch cap"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/huge",
                config=BeanSourcingConfig(max_response_bytes=100),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_page_text_fails_soft_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with _mock_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BeanFetchError, match="fetch failed"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/down",
                config=BeanSourcingConfig(),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_page_text_constructs_and_closes_its_own_client_when_none_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Covers the owns_client=True branch: no client is injected, so the
    function must build (and close) a real ``httpx.AsyncClient`` — patched
    here onto a mock transport so no real network is touched. The SSRF
    destination check is stubbed out too (#587): this test is about client
    lifecycle, not resolution — resolution itself is covered separately."""
    transport = _html_response(200, _SAMPLE_HTML)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya", config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_fetch_page_text_follows_redirects_on_internally_constructed_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 fix 1: the internally-constructed client is built with
    ``follow_redirects=False`` — ``_fetch_with_ssrf_guard`` follows redirects
    MANUALLY instead, so every hop's destination clears
    ``_assert_public_destination`` first. A public->public 302 (common for
    bare->www, http->https, trailing-slash) must still resolve to the final
    page's text (preserves the pre-#587 redirect-follow behavior)."""
    original_url = "https://vendor.example/products/kenya"
    redirected_url = "https://www.vendor.example/products/kenya"

    def handler(request: httpx.Request) -> httpx.Response:
        # The connection is now PINNED to the (stubbed) validated IP, so the
        # request's URL host is no longer the original hostname — route by
        # the preserved ``Host`` header instead (#587 fix 1b).
        host_header = request.headers.get("host")
        assert request.url.host == str(_STUB_PUBLIC_IP)
        if host_header == "vendor.example":
            return httpx.Response(302, headers={"Location": redirected_url})
        assert host_header == "www.vendor.example"
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient
    captured_kwargs: dict[str, object] = {}

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured_kwargs.update(kwargs)
        return real_async_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing,
        "_assert_public_destination",
        _noop_assert_public_destination,
    )
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            original_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    assert captured_kwargs.get("follow_redirects") is False


# --- #587 fix 1: SSRF destination guard ---


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_loopback_literal() -> None:
    """The SSRF guard runs BEFORE any request is sent — an IP-literal
    destination never touches the resolver or the network at all, so no
    transport mocking is needed here (unlike the hostname-resolution case
    below)."""
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://127.0.0.1/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_private_rfc1918_literal() -> None:
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://10.0.0.5/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_link_local_metadata_literal() -> None:
    """Blocks the cloud-metadata address (``169.254.169.254``) directly."""
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://169.254.169.254/latest/meta-data/", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_cgnat_literal() -> None:
    """#587 CGNAT fix: ``100.64.0.0/10`` (Carrier-Grade NAT — also the range
    Tailscale and similar overlay networks use) is neither ``is_private``
    nor ``is_reserved`` in Python's ``ipaddress`` — a naive
    loopback/private/link-local/reserved-only predicate silently lets it
    through. ``is_global`` has an explicit carve-out for this exact range
    (verified against the stdlib docstring, not assumed) and correctly
    rejects it."""
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://100.64.0.1/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_multicast_literal() -> None:
    """``is_global`` alone does NOT exclude multicast (a multicast address
    is not in any ``is_private`` range, so ``is_global`` reports ``True``
    for one — verified directly against the stdlib, not assumed) — the
    guard rejects ``is_multicast`` explicitly, alongside ``is_global``."""
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://224.0.0.1/x", config=BeanSourcingConfig()
        )


# --- #587 P2 round 6: reserved / embedded-IPv4 IPv6 forms ---


def test_extract_embedded_ipv4_ipv4_mapped() -> None:
    extract = bean_sourcing._extract_embedded_ipv4  # pyright: ignore[reportPrivateUsage]
    assert extract(ipaddress.ip_address("::ffff:10.0.0.1")) == ipaddress.ip_address("10.0.0.1")
    assert extract(ipaddress.ip_address("::ffff:8.8.8.8")) == ipaddress.ip_address("8.8.8.8")


def test_extract_embedded_ipv4_nat64() -> None:
    extract = bean_sourcing._extract_embedded_ipv4  # pyright: ignore[reportPrivateUsage]
    # 64:ff9b::a00:1 embeds 10.0.0.1 (0a00:0001 as the low 32 bits).
    assert extract(ipaddress.ip_address("64:ff9b::a00:1")) == ipaddress.ip_address("10.0.0.1")


def test_extract_embedded_ipv4_ipv4_compatible() -> None:
    extract = bean_sourcing._extract_embedded_ipv4  # pyright: ignore[reportPrivateUsage]
    # ::a00:1 (deprecated IPv4-compatible form) embeds 10.0.0.1 too.
    assert extract(ipaddress.ip_address("::a00:1")) == ipaddress.ip_address("10.0.0.1")


def test_extract_embedded_ipv4_returns_none_for_plain_addresses() -> None:
    extract = bean_sourcing._extract_embedded_ipv4  # pyright: ignore[reportPrivateUsage]
    assert extract(ipaddress.ip_address("10.0.0.1")) is None
    assert extract(ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")) is None


def test_is_non_public_address_rejects_reserved_even_when_global() -> None:
    """NAT64 (``64:ff9b::/96``) and IPv4-compatible (``::/96``) addresses
    are ``is_global`` ``True`` in the stdlib despite ALSO being
    ``is_reserved`` — verified directly, not assumed. An ``is_global``-only
    check would let them through."""
    is_non_public = bean_sourcing._is_non_public_address  # pyright: ignore[reportPrivateUsage]
    nat64 = ipaddress.ip_address("64:ff9b::a00:1")
    compatible = ipaddress.ip_address("::a00:1")
    assert nat64.is_global is True  # the actual gap this fix closes
    assert compatible.is_global is True
    assert is_non_public(nat64) is True
    assert is_non_public(compatible) is True


def test_is_non_public_address_allows_genuine_global_addresses() -> None:
    is_non_public = bean_sourcing._is_non_public_address  # pyright: ignore[reportPrivateUsage]
    assert is_non_public(ipaddress.ip_address("93.184.216.34")) is False
    assert is_non_public(ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946")) is False


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_ipv4_mapped_loopback() -> None:
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://[::ffff:127.0.0.1]/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_ipv4_mapped_private() -> None:
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://[::ffff:10.0.0.1]/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_nat64_embedding_private_ipv4() -> None:
    """``64:ff9b::a00:1`` embeds ``10.0.0.1`` — rejected here via the
    ``is_reserved`` check on the OUTER address (the whole NAT64 prefix is
    unconditionally reserved in the stdlib), which is the same outcome the
    embedded-address re-check would independently reach too."""
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "http://[64:ff9b::a00:1]/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_allows_a_genuine_global_ipv6_literal() -> None:
    """Confirms the reserved/embedded-IPv4 hardening does not false-positive
    on an ordinary global IPv6 literal."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "http://[2606:2800:220:1:248:1893:25c8:1946]/x",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_embedded_ipv4_when_outer_address_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense-in-depth wiring proof: even if a form embedding an IPv4
    address were ever to pass the OUTER ``is_global``/``is_reserved``/
    ``is_multicast`` checks (not reachable via any real address in the
    currently-installed stdlib for the three forms this module handles —
    see the ``_extract_embedded_ipv4``/``_is_non_public_address`` unit
    tests above, which is why this test patches
    ``_extract_embedded_ipv4`` directly rather than hunting for a real
    address), the embedded address is independently re-validated and
    rejected."""

    def fake_extract_embedded_ipv4(
        address: ipaddress.IPv4Address | ipaddress.IPv6Address,
    ) -> ipaddress.IPv4Address | None:
        return ipaddress.IPv4Address("10.0.0.1")

    monkeypatch.setattr(bean_sourcing, "_extract_embedded_ipv4", fake_extract_embedded_ipv4)
    with pytest.raises(BeanFetchError, match="embeds a non-public IPv4"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "http://[2606:2800:220:1:248:1893:25c8:1946]/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_hostname_resolving_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname (not a literal IP) that resolves to a private address must
    be rejected too — the guard resolves via the real (patched) resolver."""

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        assert host == "internal.vendor.example"
        return [(None, None, None, "", ("10.1.2.3", port))]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "http://internal.vendor.example/x"
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_redirect_into_private_address(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PUBLIC origin URL that 302s to a private/loopback address must be
    rejected — the private hop is never fetched (#587 fix 1). The origin
    hostname's resolution is stubbed to a real public address (no DNS in
    this sandbox); the redirect target is an IP literal (``127.0.0.1``), so
    it needs no resolver stub — it is rejected directly."""
    public_url = "https://vendor.example/products/kenya"
    private_target = "http://127.0.0.1/"
    request_hosts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_hosts.append(request.headers.get("host"))
        return httpx.Response(302, headers={"Location": private_target})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        assert host == "vendor.example"
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BeanFetchError, match="non-public address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            public_url, config=BeanSourcingConfig()
        )
    # Only the (public, safe) origin hop was ever connected to — the
    # rejected private redirect target was never fetched at all.
    assert request_hosts == ["vendor.example"]


@pytest.mark.asyncio
async def test_fetch_page_text_redirect_public_to_public_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public->public redirect keeps working end-to-end (not just the
    ``follow_redirects=False`` kwarg check above) — the SSRF guard runs on
    AND pins EACH hop independently, via a stubbed resolver that reports a
    (different) public address per host."""
    original_host = "vendor.example"
    redirected_host = "www.vendor.example"
    original_url = f"https://{original_host}/products/kenya"
    redirected_url = f"https://{redirected_host}/products/kenya"
    host_ips = {original_host: "93.184.216.34", redirected_host: "93.184.216.35"}
    resolver_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        host_header = request.headers.get("host")
        if host_header == original_host:
            assert request.url.host == host_ips[original_host]
            return httpx.Response(302, headers={"Location": redirected_url})
        assert host_header == redirected_host
        assert request.url.host == host_ips[redirected_host]
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        resolver_calls.append(host)
        return [(None, None, None, "", (host_ips[host], port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            original_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    # Each hop was independently resolved (and thus independently validated
    # + pinned) — exactly once per hop, no re-resolution.
    assert resolver_calls == [original_host, redirected_host]


# --- #587 fix 1b: DNS-rebinding TOCTOU close (connect-time IP pinning) ---


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_pins_connection_to_validated_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The actual HTTP request must target the VALIDATED IP literal — not
    the hostname — with the original hostname preserved via the ``Host``
    header and the ``sni_hostname`` extension (TLS SNI / certificate
    hostname identity). This is what closes the DNS-rebinding TOCTOU gap: a
    rebinding domain gets exactly one resolution (the validation one), and
    the connection never gives it a second chance to answer differently."""
    origin_url = "https://example.test/products/kenya"
    public_ip = "93.184.216.34"
    resolver_calls: list[str] = []
    captured_requests: list[httpx.Request] = []

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        resolver_calls.append(host)
        assert host == "example.test"
        return [(None, None, None, "", (public_ip, port))]

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            origin_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text

    # Resolved exactly once — no second resolution left for a rebinding
    # domain to poison (the #587 rebind-defeated proof).
    assert resolver_calls == ["example.test"]
    assert len(captured_requests) == 1
    sent = captured_requests[0]
    # The actual connection target is the validated IP literal...
    assert sent.url.host == public_ip
    # ...while routing/TLS identity stay pinned to the ORIGINAL hostname.
    assert sent.headers.get("host") == "example.test"
    assert sent.extensions.get("sni_hostname") == "example.test"


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_pins_ipv6_address_with_brackets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IPv6 pinning smoke test: ``httpx.URL.copy_with(host=...)`` must
    bracket a raw (unbracketed) ``ipaddress.IPv6Address`` string
    automatically for the pinned request URL. A real IPv6 socket connect
    isn't exercised — ``MockTransport`` substitutes the whole transport, so
    there is no real network stack anywhere in this test suite — this
    validates the pinned URL/host reaching the mock handler is well-formed
    and correctly bracketed, which is the httpx-integration risk unique to
    IPv6 pinning."""
    origin_url = "https://example.test/products/kenya"
    public_ipv6 = "2606:2800:220:1:248:1893:25c8:1946"
    captured_requests: list[httpx.Request] = []

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int, int, int]]]:
        return [(None, None, None, "", (public_ipv6, port, 0, 0))]

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            origin_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    assert len(captured_requests) == 1
    sent = captured_requests[0]
    assert sent.url.host == public_ipv6
    assert str(sent.url).startswith(f"https://[{public_ipv6}]")
    assert sent.headers.get("host") == "example.test"


# --- #587 P2: no keepalive pooling (TLS identity on a host-changing redirect) ---


@pytest.mark.asyncio
async def test_fetch_page_text_constructs_client_with_no_keepalive_pooling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: with connect-time IP pinning, two DIFFERENT hostnames that
    happen to resolve to the SAME address would otherwise share one pooled
    connection/origin — and ``sni_hostname`` only applies when a connection
    is OPENED, not when a pooled one is reused, so a host-changing redirect
    could silently skip re-validating the new host's TLS identity.
    ``httpx.MockTransport`` never performs real connection pooling (it
    substitutes the whole transport), so the strongest assertion available
    at this layer is that the internally-constructed client is CONFIGURED
    with no keepalive connections — mirrors the existing
    ``follow_redirects`` kwarg-capture test above."""
    transport = _html_response(200, _SAMPLE_HTML)
    real_async_client = httpx.AsyncClient
    captured_kwargs: dict[str, object] = {}

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured_kwargs.update(kwargs)
        return real_async_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya", config=BeanSourcingConfig()
    )
    limits = captured_kwargs.get("limits")
    assert isinstance(limits, httpx.Limits)
    assert limits.max_keepalive_connections == 0


@pytest.mark.asyncio
async def test_fetch_page_text_redirect_public_to_public_tracks_sni_per_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Complements the no-keepalive-pooling config test above: even at the
    ``MockTransport`` layer (no real connection reuse to observe), each
    hop's request must carry the SNI (``sni_hostname`` extension) for THAT
    hop's own host, never a stale one from a prior hop — the closest
    behavioral proxy for "TLS identity tracks the new host each hop" this
    test layer can assert."""
    original_host = "vendor.example"
    redirected_host = "www.vendor.example"
    original_url = f"https://{original_host}/products/kenya"
    redirected_url = f"https://{redirected_host}/products/kenya"
    host_ips = {original_host: "93.184.216.34", redirected_host: "93.184.216.35"}
    sni_per_request: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sni_per_request.append(request.extensions.get("sni_hostname"))
        host_header = request.headers.get("host")
        if host_header == original_host:
            return httpx.Response(302, headers={"Location": redirected_url})
        assert host_header == redirected_host
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [(None, None, None, "", (host_ips[host], port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            original_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    assert sni_per_request == [original_host, redirected_host]


# --- #587 P2: try every validated address before giving up ---


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_tries_next_address_after_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dual-stack host whose first resolved address is unreachable (a dead
    route, or a transient CDN node) must still succeed via its SECOND
    validated address, rather than failing the whole fetch."""
    bad_ip = "1.1.1.1"
    good_ip = "93.184.216.34"
    attempted_hosts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_hosts.append(request.url.host)
        if request.url.host == bad_ip:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [
            (None, None, None, "", (bad_ip, port)),
            (None, None, None, "", (good_ip, port)),
        ]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya", config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    assert attempted_hosts == [bad_ip, good_ip]


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_raises_when_every_address_fails_to_connect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When EVERY validated address fails to connect, the fetch must still
    fail soft as a typed ``BeanFetchError``, not leak the raw
    ``httpx.ConnectError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BeanFetchError, match="could not connect to any resolved address"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_tries_next_address_after_connect_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: ``httpx.ConnectTimeout`` (a "black-holed" address — a route
    exists but nothing ever answers) is a SIBLING of ``httpx.ConnectError``
    under ``httpx.TransportError``, not a subclass — a fallback loop that
    only caught ``ConnectError`` would silently give up on a timed-out
    first address instead of trying the next one."""
    bad_ip = "1.1.1.1"
    good_ip = "93.184.216.34"
    attempted_hosts: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempted_hosts.append(request.url.host)
        if request.url.host == bad_ip:
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [
            (None, None, None, "", (bad_ip, port)),
            (None, None, None, "", (good_ip, port)),
        ]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya", config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Kenya Kiambu AA" in text
    assert attempted_hosts == [bad_ip, good_ip]


@pytest.mark.asyncio
async def test_fetch_one_hop_divides_connect_timeout_across_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: with more than one candidate address, each attempt's
    CONNECT phase must be bounded to a FRACTION of the configured connect
    timeout — otherwise one black-holed FIRST address could consume the
    entire per-request connect budget, leaving the fallback loop no time to
    even attempt a second address before the caller's outer end-to-end
    deadline also expires."""
    captured_timeouts: list[httpx.Timeout] = []
    original_stream = httpx.AsyncClient.stream

    def recording_stream(
        self: httpx.AsyncClient, method: str, url: object, **kwargs: object
    ) -> object:
        captured_timeouts.append(kwargs.get("timeout"))  # type: ignore[arg-type]
        return original_stream(self, method, url, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "stream", recording_stream)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    candidate_addresses = [
        ipaddress.ip_address("93.184.216.34"),
        ipaddress.ip_address("1.1.1.1"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result, is_redirect = await bean_sourcing._fetch_one_hop(  # pyright: ignore[reportPrivateUsage]
            client,
            "https://vendor.example/products/kenya",
            candidate_addresses,
            headers={"User-Agent": "x"},
            timeout=httpx.Timeout(10.0),
            config=BeanSourcingConfig(),
        )
    assert is_redirect is False
    assert "Kenya Kiambu AA" in result
    assert len(captured_timeouts) == 1
    assert captured_timeouts[0].connect == 5.0
    assert captured_timeouts[0].read == 10.0


# --- #587 P2: decode using the response's declared charset ---


@pytest.mark.asyncio
async def test_fetch_page_text_decodes_declared_non_utf8_charset_injected_client() -> None:
    """#587 P2: a page served with a declared non-UTF-8 charset must decode
    under THAT charset, not get corrupted by an unconditional UTF-8 decode.
    Injected-client path (mirrors the owns-client version below)."""
    html_latin1 = "<p>Café Kenya</p>".encode("iso-8859-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_bytes_stream(html_latin1),
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )

    async with _mock_client(httpx.MockTransport(handler)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/cafe",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert "Café Kenya" in text


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_decodes_declared_non_utf8_charset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: owns-client path version of the charset-decode fix — must
    behave identically to the injected-client path above."""
    html_latin1 = "<p>Café Kenya</p>".encode("iso-8859-1")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_bytes_stream(html_latin1),
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/cafe", config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Café Kenya" in text


def test_decode_response_body_falls_back_to_utf8_on_unknown_charset() -> None:
    """#587 P2, round 8: ``response.encoding`` is not a hard guarantee of a
    decodable codec name — ``bytes.decode()`` raises ``LookupError`` for an
    unrecognized one, which ``errors="replace"`` does NOT protect against
    (that only governs decode errors WITHIN an already-resolved codec, not
    an unknown codec NAME). ``httpx``'s own ``Response.encoding`` getter
    validates the common case (a ``Content-Type: charset=...`` header) via
    the codec registry, but the ``.encoding`` SETTER — used elsewhere in
    ``httpx``'s own internals — does not re-validate at all; forced here
    via that setter to exercise the fallback directly and deterministically
    (independent of httpx-version-specific getter validation behavior)."""
    response = httpx.Response(200, content=b"hello")
    response.encoding = "not-a-real-codec"
    result = bean_sourcing._decode_response_body(  # pyright: ignore[reportPrivateUsage]
        b"hello", response
    )
    assert result == "hello"


@pytest.mark.asyncio
async def test_fetch_page_text_exhausting_max_redirects_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Always redirects to a new URL — never terminates.
        next_url = str(request.url) + "x"
        return httpx.Response(302, headers={"Location": next_url})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="too many redirects"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_bare_redirect_with_no_location_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)  # no Location header

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="no Location header"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_redirect_to_non_http_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A redirect ``Location`` that switches to a non-http(s) scheme (e.g.
    ``ftp://``) must be rejected by ``_assert_public_destination``'s own
    scheme/host check, reached this time via the redirect hop rather than
    the origin URL."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "ftp://internal.example/x"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [(None, None, None, "", ("93.184.216.34", port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_redirect_to_malformed_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: a malformed redirect ``Location`` (an unclosed IPv6 bracket,
    ``http://[::1``) makes ``urljoin()`` raise ``ValueError`` — left
    unguarded this escapes as an unhandled 500 instead of failing soft.

    Uses HTTP 300 (Multiple Choices), not 302: ``httpx`` itself EAGERLY
    parses the ``Location`` header for the standard redirect codes
    (301/302/303/307/308) regardless of ``follow_redirects`` (confirmed by
    reading ``httpx._client``'s ``_send_handling_redirects`` — it always
    calls ``_build_redirect_request``/``_redirect_url``, just conditionally
    on whether to actually FOLLOW it), so a malformed Location on one of
    those codes is already caught by httpx's own ``RemoteProtocolError``
    (mapped here via the pre-existing generic ``except httpx.HTTPError``
    handler — also fail-soft, just a different message). ``300`` is
    OUTSIDE that standard list (``Response.has_redirect_location`` is
    ``False`` for it) but still inside this module's own broader
    ``300 <= status_code < 400`` redirect-hop range, so httpx hands us the
    raw header untouched and OUR OWN ``urljoin()`` call — the one this test
    targets — is what has to catch the malformed value."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(300, headers={"Location": "http://[::1"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="malformed Location"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_standard_redirect_code_with_malformed_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Companion to the test above: on a STANDARD redirect code (302),
    ``httpx`` itself intercepts the malformed ``Location`` first
    (``RemoteProtocolError``), which the pre-existing generic
    ``except httpx.HTTPError`` handler already maps to ``BeanFetchError`` —
    proving this path was ALREADY fail-soft even before this module's own
    ``urljoin()`` guard, and stays fail-soft with it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "http://[::1"})

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="fetch failed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_assert_public_destination_maps_resolver_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver failure (e.g. ``socket.gaierror``, a subclass of
    ``OSError``) must be mapped to ``BeanFetchError``, not left to escape as
    the raw ``OSError``."""

    async def failing_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        raise OSError("name resolution failed")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", failing_getaddrinfo)
    with pytest.raises(BeanFetchError, match="could not resolve host"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "https://unresolvable.example/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_maps_resolver_unicode_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2 round 6: ``loop.getaddrinfo()`` raises ``UnicodeError``/
    ``UnicodeEncodeError`` (NOT ``OSError``) for a hostname it cannot even
    IDNA-encode — e.g. a lone UTF-16 surrogate reaching the resolver. Left
    unguarded this escapes as an unhandled 500 instead of the typed
    fail-soft error every other malformed-host case here gets. Simulated
    via a patched resolver here (a lone-surrogate hostname is actually
    rejected even earlier, by ``urlsplit()`` itself, in the real flow —
    see the genuinely-reachable over-long-label test below)."""

    async def failing_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        raise UnicodeError("encoding with 'idna' codec failed")

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", failing_getaddrinfo)
    with pytest.raises(BeanFetchError, match="could not resolve host"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "https://unresolvable.example/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_real_over_long_dns_label() -> None:
    """A genuinely-reachable trigger, no resolver mocking: a DNS label over
    63 characters cannot be IDNA-encoded, and the REAL
    ``loop.getaddrinfo()`` raises ``UnicodeError`` for it — this passes
    ``urlsplit()`` fine (it does not validate label lengths), so it is what
    actually reaches the resolver's own ``UnicodeError`` path."""
    long_label = "x" * 64
    with pytest.raises(BeanFetchError, match="could not resolve host"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            f"https://{long_label}.example.test/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_host_resolving_to_no_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver that returns no addresses at all (an edge case some
    resolvers can hit for a name with only unsupported record types) must
    fail closed, not silently proceed with an empty address list."""

    async def empty_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return []

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", empty_getaddrinfo)
    with pytest.raises(BeanFetchError, match="resolved to no usable address"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "https://no-addresses.example/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_non_numeric_port() -> None:
    """#587 P2: ``urlsplit(...).port`` raises ``ValueError`` on a non-numeric
    port — left unguarded this becomes an unhandled 500 instead of the typed
    fail-soft error every other malformed-URL case here gets."""
    with pytest.raises(BeanFetchError, match="malformed port"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example:bad/x"
        )


@pytest.mark.asyncio
async def test_assert_public_destination_rejects_out_of_range_port() -> None:
    """#587 P2: same failure mode as the non-numeric case, for a numeric but
    out-of-TCP-range port (> 65535)."""
    with pytest.raises(BeanFetchError, match="malformed port"):
        await bean_sourcing._assert_public_destination(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example:99999/x"
        )


@pytest.mark.asyncio
async def test_fetch_page_text_owns_client_rejects_malformed_port() -> None:
    """Full-pipeline proof: a malformed port reaches ``_fetch_page_text``
    (via the owns-client path — no client injected) as ``BeanFetchError``,
    never an unhandled exception."""
    with pytest.raises(BeanFetchError, match="malformed port"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example:bad/x", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_owns_client_raises_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-2xx status check inside ``_fetch_with_ssrf_guard`` (the
    owns-client path) — the equivalent injected-client check is covered by
    ``test_fetch_page_text_raises_on_non_2xx`` above."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=_bytes_stream(b"not found"))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="404"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/gone", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_owns_client_raises_on_oversized_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The size-cap check inside ``_fetch_with_ssrf_guard`` (the owns-client
    path) — the equivalent injected-client check is covered by
    ``test_fetch_page_text_raises_on_oversized_body`` above."""
    huge_html = "<p>" + ("x" * 5000) + "</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bytes_stream(huge_html.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="fetch cap"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/huge",
            config=BeanSourcingConfig(max_response_bytes=100),
        )


# --- #587 P1: compression-bomb guard (check the cap BEFORE appending) ---


def test_append_within_cap_raises_without_mutating_body_over_cap() -> None:
    """#587 P1: proves the buffer NEVER exceeds the cap directly — a
    check-AFTER-append implementation would also eventually raise here
    (the coverage of "it raises" alone does not discriminate the two), but
    would have mutated ``body`` to be over-cap first. This asserts ``body``
    is untouched by the over-cap chunk."""
    body = bytearray(b"short")
    with pytest.raises(BeanFetchError, match="fetch cap"):
        bean_sourcing._append_within_cap(  # pyright: ignore[reportPrivateUsage]
            body, b"x" * 1000, max_bytes=10, url="https://vendor.example/x"
        )
    assert bytes(body) == b"short"
    assert len(body) <= 10


def test_append_within_cap_allows_a_chunk_that_stays_within_the_cap() -> None:
    body = bytearray()
    bean_sourcing._append_within_cap(  # pyright: ignore[reportPrivateUsage]
        body, b"hello", max_bytes=10, url="https://vendor.example/x"
    )
    assert bytes(body) == b"hello"


@pytest.mark.asyncio
async def test_fetch_page_text_injected_client_rejects_a_single_oversized_decompressed_chunk() -> (
    None
):
    """#587 P1: simulates a compression bomb — ``aiter_bytes()`` handing the
    caller a SINGLE (decompressed) chunk that alone blows past the cap,
    rather than many small chunks accumulating past it. Injected-client
    path."""
    huge_body = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bytes_stream(huge_body))

    async with _mock_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BeanFetchError, match="fetch cap"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/bomb",
                config=BeanSourcingConfig(max_response_bytes=100),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_rejects_a_single_oversized_decompressed_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owns-client-path version of the compression-bomb test above."""
    huge_body = b"x" * 5000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_bytes_stream(huge_body))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="fetch cap"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/bomb",
            config=BeanSourcingConfig(max_response_bytes=100),
        )


# --- #587 P1 round 2: streaming decompression, bounded raw AND decoded ---


def test_decompress_within_cap_passes_through_identity() -> None:
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    assert decode(b"hello", "", max_bytes=100, url="https://x/y") == b"hello"
    assert decode(b"hello", "identity", max_bytes=100, url="https://x/y") == b"hello"


def test_decompress_within_cap_decodes_gzip() -> None:
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    payload = b"<html>Kenya Kiambu AA</html>"
    compressed = gzip.compress(payload)
    assert decode(compressed, "gzip", max_bytes=1000, url="https://x/y") == payload
    # Case/whitespace-insensitive, and the "x-gzip" alias.
    assert decode(compressed, " GZIP ", max_bytes=1000, url="https://x/y") == payload
    assert decode(compressed, "x-gzip", max_bytes=1000, url="https://x/y") == payload


def test_decompress_within_cap_decodes_deflate_with_zlib_header() -> None:
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    payload = b"<html>Kenya Kiambu AA</html>"
    compressed = zlib.compress(payload)  # zlib-wrapped (has the 2-byte header)
    assert decode(compressed, "deflate", max_bytes=1000, url="https://x/y") == payload


def test_decompress_within_cap_decodes_raw_deflate_without_zlib_header() -> None:
    """Some servers send raw DEFLATE (no zlib header) despite the
    "deflate" Content-Encoding name — the first attempt must fail over to
    a raw window, mirroring httpx's own DeflateDecoder compatibility."""
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    payload = b"<html>Kenya Kiambu AA</html>"
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    compressed = compressor.compress(payload) + compressor.flush()
    assert decode(compressed, "deflate", max_bytes=1000, url="https://x/y") == payload


def test_decompress_within_cap_rejects_unsupported_encoding() -> None:
    """#587 P2: only gzip/deflate are requested/decoded — brotli, zstd, or
    an unknown value fails closed rather than being silently mistreated as
    identity (LLM-extraction garbage) or decompressed by an unimported lib."""
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(BeanFetchError, match="unsupported Content-Encoding"):
        decode(b"whatever-bytes", "br", max_bytes=1000, url="https://x/y")


def test_decompress_within_cap_rejects_decompression_bomb() -> None:
    """A small compressed payload that decompresses beyond the cap must
    raise — and must never actually allocate the full decompressed size."""
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    huge_payload = b"a" * 1_000_000  # highly compressible -> tiny gzip body
    compressed = gzip.compress(huge_payload)
    assert len(compressed) < 2000  # confirms this really is a "bomb" shape
    with pytest.raises(BeanFetchError, match="fetch cap"):
        decode(compressed, "gzip", max_bytes=1000, url="https://x/y")


def test_decompress_within_cap_rejects_exact_one_byte_over_cap_with_no_unconsumed_tail() -> None:
    """Boundary case: a payload whose TRUE decoded size is exactly
    ``max_bytes + 1`` can fully consume the compressed input AND leave
    ``unconsumed_tail`` EMPTY (the decompressor has genuinely nothing left
    to give) — so the ``unconsumed_tail`` check alone does not catch it;
    the trailing length check after ``flush()`` is what does. Verified
    directly against zlib's actual behavior at this exact boundary before
    writing this test, not assumed."""
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    payload = b"x" * 101
    compressed = gzip.compress(payload)
    with pytest.raises(BeanFetchError, match="fetch cap"):
        decode(compressed, "gzip", max_bytes=100, url="https://x/y")


def test_decompress_within_cap_rejects_corrupt_gzip_data() -> None:
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(BeanFetchError, match="failed to decompress"):
        decode(b"not actually gzip data", "gzip", max_bytes=1000, url="https://x/y")


def test_decompress_within_cap_rejects_truncated_gzip_body() -> None:
    """#587 P2 round 6: a truncated gzip stream (a connection cut mid-body,
    or a misbehaving server) can decompress+flush to PARTIAL output with NO
    exception raised and ``decompressor.eof`` staying ``False`` — verified
    directly against zlib's actual behavior before writing this test, not
    assumed. Left unguarded this would silently hand the LLM extraction
    step a truncated page instead of failing the fetch."""
    decode = bean_sourcing._decompress_within_cap  # pyright: ignore[reportPrivateUsage]
    payload = b"Kenya Kiambu AA washed process altitude 1850m tasting notes" * 3
    compressed = gzip.compress(payload)
    truncated = compressed[: len(compressed) - 15]
    with pytest.raises(BeanFetchError, match="truncated/incomplete"):
        decode(truncated, "gzip", max_bytes=len(payload) + 1, url="https://x/y")


@pytest.mark.asyncio
async def test_fetch_page_text_rejects_a_truncated_gzip_response() -> None:
    """Full-pipeline proof: a truncated gzip response body must fail soft
    as ``BeanFetchError``, not silently draft from partial page text."""
    payload = _SAMPLE_HTML.encode()
    compressed = gzip.compress(payload)
    truncated = compressed[: len(compressed) - 15]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_bytes_stream(truncated), headers={"Content-Encoding": "gzip"}
        )

    async with _mock_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BeanFetchError, match="truncated/incomplete"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/kenya",
                config=BeanSourcingConfig(),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_page_text_injected_client_rejects_a_gzip_decompression_bomb() -> None:
    """#587 P1 round 2: a SMALL gzip body that decompresses beyond the cap
    must raise BeanFetchError — proving the cap is enforced on the DECODED
    size, not just the (small, wire-size) raw/compressed body. Injected-
    client path."""
    huge_payload = b"a" * 1_000_000
    compressed = gzip.compress(huge_payload)
    assert len(compressed) < 2000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_bytes_stream(compressed), headers={"Content-Encoding": "gzip"}
        )

    async with _mock_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BeanFetchError, match="fetch cap"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/bomb.gz",
                config=BeanSourcingConfig(max_response_bytes=1000),
                http_client=client,
            )


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_rejects_a_gzip_decompression_bomb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owns-client-path version of the gzip decompression-bomb test above."""
    huge_payload = b"a" * 1_000_000
    compressed = gzip.compress(huge_payload)
    assert len(compressed) < 2000

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_bytes_stream(compressed), headers={"Content-Encoding": "gzip"}
        )

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="fetch cap"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/bomb.gz",
            config=BeanSourcingConfig(max_response_bytes=1000),
        )


@pytest.mark.asyncio
async def test_fetch_page_text_decodes_a_small_legitimate_gzip_body() -> None:
    """A well-behaved small gzip response decodes correctly end-to-end
    (not just the decompression-bomb rejection path)."""
    compressed = gzip.compress(_SAMPLE_HTML.encode())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=_bytes_stream(compressed), headers={"Content-Encoding": "gzip"}
        )

    async with _mock_client(httpx.MockTransport(handler)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/kenya",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_fetch_page_text_sends_gzip_deflate_only_accept_encoding() -> None:
    """#587 P1 round 2: this module requests ONLY gzip/deflate — never
    br/zstd, which it has no safe (cap-bounded) way to decode."""
    captured_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(request.headers)
        return httpx.Response(200, content=_bytes_stream(_SAMPLE_HTML.encode()))

    async with _mock_client(httpx.MockTransport(handler)) as client:
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya",
            config=BeanSourcingConfig(),
            http_client=client,
        )
    assert captured_headers.get("accept-encoding") == "gzip, deflate"


# --- #587 P2 round 5: httpx.InvalidURL (a NUL byte passes urlsplit but not httpx.URL) ---


@pytest.mark.asyncio
async def test_fetch_with_ssrf_guard_rejects_nul_byte_in_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NUL byte in the path passes ``urlsplit()`` (so
    ``_assert_public_destination`` validates the host fine) but
    ``httpx.URL()`` raises ``httpx.InvalidURL`` for it — not an
    ``httpx.HTTPError`` subclass, so it must be mapped explicitly
    (#587 P2) rather than escaping as an unhandled 500. Owns-client path:
    caught inside ``_fetch_one_hop`` itself."""
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="well-formed"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/prod\x00uct", config=BeanSourcingConfig()
        )


@pytest.mark.asyncio
async def test_fetch_page_text_injected_client_rejects_nul_byte_in_path() -> None:
    """Injected-client-path version: caught by ``_fetch_page_text``'s own
    outer ``httpx.InvalidURL`` handler, since the injected client's
    ``client.stream(url)`` call is what parses the NUL-bearing url this
    time (no ``_fetch_one_hop`` in this path to catch it earlier)."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("must never connect for an invalid URL")

    async with _mock_client(httpx.MockTransport(handler)) as client:
        with pytest.raises(BeanFetchError, match="well-formed"):
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/prod\x00uct",
                config=BeanSourcingConfig(),
                http_client=client,
            )


# --- #587 P2 round 5: no env proxies on the internally-constructed client ---


@pytest.mark.asyncio
async def test_fetch_page_text_constructs_client_with_trust_env_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 P2: with an env HTTPS_PROXY set, httpx's default
    ``trust_env=True`` would route the fetch through a CONNECT-tunnelling
    proxy that TLS-verifies against the pinned IP LITERAL (the
    ``sni_hostname`` extension is not honored by the tunnel) — silently
    defeating connect-time pinning. The internally-constructed client must
    disable this; an injected client is untouched (the caller's to set)."""
    transport = _html_response(200, _SAMPLE_HTML)
    real_async_client = httpx.AsyncClient
    captured_kwargs: dict[str, object] = {}

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        captured_kwargs.update(kwargs)
        return real_async_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya", config=BeanSourcingConfig()
    )
    assert captured_kwargs.get("trust_env") is False


# --- #587 fix 2: end-to-end fetch deadline ---


@pytest.mark.asyncio
async def test_fetch_page_text_end_to_end_timeout_raises_bean_fetch_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-drip body (one that keeps yielding chunks forever, each within
    a per-chunk delay under the per-request timeout) must still be bounded
    by the END-TO-END deadline — never hang the request indefinitely."""

    async def slow_handler(request: httpx.Request) -> httpx.Response:
        async def slow_body() -> AsyncGenerator[bytes, None]:
            while True:
                await asyncio.sleep(0.05)
                yield b"x"

        return httpx.Response(200, content=slow_body())

    transport = httpx.MockTransport(slow_handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    monkeypatch.setattr(
        bean_sourcing, "_assert_public_destination", _noop_assert_public_destination
    )
    with pytest.raises(BeanFetchError, match="deadline"):
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/x",
            config=BeanSourcingConfig(fetch_timeout_seconds=0.15),
        )


# --- _bean_sourcing_agent / _extract_bean_identity ---


def test_bean_sourcing_agent_builds_model_from_sourcing_config_default_when_none_injected() -> None:
    """Covers the model=None branch without any network call — build_model
    only constructs the Model object."""
    agent = bean_sourcing._bean_sourcing_agent(  # pyright: ignore[reportPrivateUsage]
        AdvisorConfig(
            provider="openai_compatible",
            api_key_env="ROASTPILOT_TEST_UNSET_KEY",
            model_slug="openai/gpt-4o",
        )
    )
    assert agent is not None


# --- _resolve_extraction_model_slug (#590 P1 + P2 fix: provider-aware default) ---
#
# Codex caught a P1 on the PR that introduced BeanSourcingConfig.model_slug:
# its OpenRouter-slug default ("openai/gpt-5-mini") was handed to
# build_model() unconditionally, regardless of advisor_config.provider — so
# an operator on a NATIVE provider (openai/anthropic/google/ollama) got an
# invalid (or silently wrong-vendor) model slug and every extraction failed,
# a regression from their currently-working, provider-compatible advisor
# model. A follow-up P2 caught that ``provider == "openai_compatible"``
# alone is NOT synonymous with OpenRouter — that provider setting also
# covers any OTHER OpenAI-compatible endpoint (a local server, LiteLLM,
# etc. via a custom ``provider_base_url``) — so the gate additionally keys
# on ``provider_base_url`` actually matching OpenRouter's. The cases below
# pin both fixes.


def test_resolve_extraction_model_slug_openai_compatible_uses_bakeoff_default() -> None:
    """(a) provider "openai_compatible" WITH the OpenRouter base URL
    (BYOK-OpenRouter, the bake-off's own setup) + no explicit override ->
    the bake-off's OpenRouter screening pick, "openai/gpt-5-mini"."""
    advisor_config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=OPENROUTER_BASE_URL,
        model_slug="openai/gpt-4o",
    )
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "openai/gpt-5-mini"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google", "ollama"])
def test_resolve_extraction_model_slug_native_provider_uses_advisor_model_slug(
    provider: Literal["openai", "anthropic", "google", "ollama"],
) -> None:
    """(b) a NATIVE provider + no explicit override -> the operator's own
    advisor_config.model_slug, NEVER the OpenRouter-prefixed
    "openai/gpt-5-mini" default (#590 P1: that slug is invalid/wrong-vendor
    against a native provider's own API)."""
    advisor_config = AdvisorConfig(provider=provider, model_slug="a-native-provider-model")
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "a-native-provider-model"
    assert resolved != "openai/gpt-5-mini"


@pytest.mark.parametrize("provider", ["openai_compatible", "anthropic"])
def test_resolve_extraction_model_slug_explicit_override_wins_regardless_of_provider(
    provider: Literal["openai_compatible", "anthropic"],
) -> None:
    """An explicit ``sourcing_config.model_slug`` always wins, whatever the
    provider — the operator (or the bake-off harness, pinning a slug per
    roster model under test) is trusted to have named a compatible one."""
    advisor_config = AdvisorConfig(provider=provider, model_slug="advisor-default")
    sourcing_config = BeanSourcingConfig(model_slug="explicit-override")
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, sourcing_config
    )
    assert resolved == "explicit-override"


def test_resolve_extraction_model_slug_openai_compatible_non_openrouter() -> None:
    """(d) #590 P2 fix: provider "openai_compatible" pointed at a
    NON-OpenRouter endpoint (a local server / LiteLLM / etc. via a custom
    ``provider_base_url``) + no explicit override -> the operator's own
    ``advisor_config.model_slug``, NOT the OpenRouter-only default —
    ``provider == "openai_compatible"`` alone is not synonymous with
    OpenRouter."""
    advisor_config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url="https://my-local-litellm.example/v1",
        model_slug="locally-served-model",
    )
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "locally-served-model"
    assert resolved != "openai/gpt-5-mini"


@pytest.mark.parametrize(
    "provider_base_url",
    [
        OPENROUTER_BASE_URL,
        OPENROUTER_BASE_URL + "/",
        "HTTPS://OpenRouter.AI/api/v1",
        "https://openrouter.ai:443/api/v1",
    ],
    ids=["exact", "trailing-slash", "case-variant", "explicit-default-port"],
)
def test_resolve_extraction_model_slug_openai_compatible_openrouter_variants(
    provider_base_url: str,
) -> None:
    """(e) provider "openai_compatible" WITH the OpenRouter base URL —
    including a trailing-slash, a host-case, and an explicit-default-port
    (``:443``) variant — resolves to the bake-off default, tolerant per
    ``_normalize_base_url``."""
    advisor_config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url=provider_base_url,
        model_slug="openai/gpt-4o",
    )
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "openai/gpt-5-mini"


def test_resolve_extraction_model_slug_openai_compatible_non_default_port_not_openrouter() -> None:
    """A NON-default explicit port (e.g. a LAN reverse-proxy in front of an
    OpenAI-compatible endpoint on ``:8443``) must NOT be treated as
    OpenRouter — dropping it would be the exact false-positive
    ``_normalize_base_url``'s default-port tolerance must not introduce."""
    advisor_config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url="https://openrouter.ai:8443/api/v1",
        model_slug="a-proxied-model",
    )
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "a-proxied-model"
    assert resolved != "openai/gpt-5-mini"


def test_resolve_extraction_model_slug_openai_compatible_malformed_port_not_openrouter() -> None:
    """A malformed (non-numeric) port in ``provider_base_url`` must not
    crash resolution — ``urlsplit(...).port`` raises ``ValueError``,
    ``_normalize_base_url`` catches it, the comparison simply fails to
    match, and resolution falls through to ``advisor_config.model_slug``
    (this function must never raise)."""
    advisor_config = AdvisorConfig(
        provider="openai_compatible",
        provider_base_url="https://openrouter.ai:notaport/api/v1",
        model_slug="a-fallback-model",
    )
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, None
    )
    assert resolved == "a-fallback-model"
    assert resolved != "openai/gpt-5-mini"


def test_resolve_extraction_model_slug_sourcing_config_present_but_model_slug_unset() -> None:
    """A ``sourcing_config`` that customizes only ``extraction_timeout_seconds``
    (leaving ``model_slug`` at its own ``None`` default) must still fall
    through to the provider-aware resolution — not be mistaken for an
    explicit override of ``None`` itself."""
    advisor_config = AdvisorConfig(provider="openai", model_slug="the-advisor-model")
    sourcing_config = BeanSourcingConfig(extraction_timeout_seconds=99.0)
    resolved = bean_sourcing._resolve_extraction_model_slug(  # pyright: ignore[reportPrivateUsage]
        advisor_config, sourcing_config
    )
    assert resolved == "the-advisor-model"


def test_bean_sourcing_agent_uses_resolved_model_slug_openai_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration: with provider "openai_compatible" pointed at OpenRouter
    and no explicit override, ``_bean_sourcing_agent`` passes the bake-off
    default to ``build_model`` — not ``advisor_config.model_slug``, which
    is deliberately set to something else here to prove it is not
    consulted."""
    captured: dict[str, object] = {}

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        captured["model_slug"] = model_slug
        return _function_model_returning(_identity_args())

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    bean_sourcing._bean_sourcing_agent(  # pyright: ignore[reportPrivateUsage]
        AdvisorConfig(
            provider="openai_compatible",
            provider_base_url=OPENROUTER_BASE_URL,
            model_slug="anthropic/claude-opus-4.8",
        )
    )
    assert captured["model_slug"] == "openai/gpt-5-mini"


def test_bean_sourcing_agent_native_provider_uses_advisor_model_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) integration, the P1 regression test: with a NATIVE provider and
    no explicit ``sourcing_config`` override, ``_bean_sourcing_agent`` must
    pass ``advisor_config.model_slug`` to ``build_model`` — never the
    OpenRouter-only default."""
    captured: dict[str, object] = {}

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        captured["model_slug"] = model_slug
        return _function_model_returning(_identity_args())

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    bean_sourcing._bean_sourcing_agent(  # pyright: ignore[reportPrivateUsage]
        AdvisorConfig(provider="openai", model_slug="gpt-5-mini")
    )
    assert captured["model_slug"] == "gpt-5-mini"


def test_bean_sourcing_agent_injected_model_overrides_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) an injected ``model=`` wins over BOTH the provider-aware default
    AND an explicit ``sourcing_config.model_slug`` — ``build_model`` must
    not even be called."""

    def fail_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        raise AssertionError("build_model must not be called when model= is injected")

    monkeypatch.setattr(bean_sourcing, "build_model", fail_build_model)
    injected = _function_model_returning(_identity_args())
    agent = bean_sourcing._bean_sourcing_agent(  # pyright: ignore[reportPrivateUsage]
        AdvisorConfig(provider="openai", model_slug="gpt-5-mini"),
        sourcing_config=BeanSourcingConfig(model_slug="explicit-override"),
        model=injected,
    )
    assert agent is not None


def test_default_extraction_timeout_matches_config_default() -> None:
    """Drift guard: the module-level fallback constant must always equal
    ``BeanSourcingConfig.extraction_timeout_seconds``'s own default."""
    default_timeout = bean_sourcing._DEFAULT_EXTRACTION_TIMEOUT_SECONDS  # pyright: ignore[reportPrivateUsage]
    assert BeanSourcingConfig().extraction_timeout_seconds == default_timeout


@pytest.mark.asyncio
async def test_extract_bean_identity_returns_provider_output() -> None:
    model = _function_model_returning(_identity_args())
    identity = await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
        "page text", advisor_config=_ADVISOR_CONFIG, model=model
    )
    assert identity.name == "Kenya Kiambu AA (Washed)"
    assert identity.processing == "washed"
    assert identity.altitude_m == 1775


@pytest.mark.asyncio
async def test_extract_bean_identity_maps_malformed_output() -> None:
    """#613: validation-retry exhaustion is DEPENDENCY-origin (model-quality
    failure, not a bad caller URL) — raised as the ``BeanExtractionError``
    SUBCLASS, ``BeanExtractionUnavailableError``, not the base class."""
    model = _function_model_text("here is some prose, not the tool call")
    with pytest.raises(BeanExtractionUnavailableError, match="malformed"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG, model=model
        )


@pytest.mark.asyncio
async def test_extract_bean_identity_maps_provider_error() -> None:
    """#613: a provider/transport error is DEPENDENCY-origin."""
    model = _function_model_raising(
        ModelHTTPError(status_code=503, model_name="x", body="upstream down")
    )
    with pytest.raises(BeanExtractionUnavailableError, match="provider error"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG, model=model
        )


@pytest.mark.asyncio
async def test_extract_bean_identity_timeout_uses_sourcing_config_not_advisor_config() -> None:
    """#590 slice A: the extraction deadline is
    ``sourcing_config.extraction_timeout_seconds`` — NOT
    ``advisor_config.timeout_seconds``. ``advisor_config`` here is given a
    LONG timeout that would never fire on its own, and ``sourcing_config`` a
    short one, so this only passes if the short, sourcing-owned deadline is
    the one actually enforced (the old coupling would let this hang for the
    full 10s test-suite-unfriendly duration, or simply never time out at
    all with a 100s advisor budget). Also #613: a provider timeout is
    DEPENDENCY-origin."""
    model = _function_model_hanging()
    advisor_config = AdvisorConfig(timeout_seconds=100.0)
    sourcing_config = BeanSourcingConfig(extraction_timeout_seconds=0.05)
    with pytest.raises(BeanExtractionUnavailableError, match="deadline"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text",
            advisor_config=advisor_config,
            sourcing_config=sourcing_config,
            model=model,
        )


@pytest.mark.asyncio
async def test_extract_bean_identity_default_timeout_ignores_short_advisor_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``sourcing_config`` entirely falls back to
    :data:`bean_sourcing._DEFAULT_EXTRACTION_TIMEOUT_SECONDS` (45s) — never
    to ``advisor_config.timeout_seconds`` (the old, too-tight 10s advice
    budget that timed out reasoning models in the bean-sourcing bake-off).
    Spies on ``asyncio.timeout`` to assert the actual deadline passed to it,
    with a deliberately tiny ``advisor_config.timeout_seconds`` that would
    prove the (undesired) coupling if it were still in effect."""
    recorded: list[float] = []
    real_timeout = asyncio.timeout

    def spy_timeout(delay: float) -> asyncio.Timeout:
        recorded.append(delay)
        return real_timeout(delay)

    # Patches the shared ``asyncio`` module object (not ``bean_sourcing``'s
    # own import binding) -- bean_sourcing.py's own ``import asyncio`` refers
    # to this same module instance, so its ``asyncio.timeout(...)`` call
    # picks up the spy too.
    monkeypatch.setattr(asyncio, "timeout", spy_timeout)
    model = _function_model_returning(_identity_args())
    advisor_config = AdvisorConfig(timeout_seconds=0.001)
    await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
        "page text", advisor_config=advisor_config, model=model
    )
    assert recorded == [bean_sourcing._DEFAULT_EXTRACTION_TIMEOUT_SECONDS]  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_bean_identity_maps_build_model_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 fix 2: ``_bean_sourcing_agent`` (which calls ``build_model`` when
    ``model`` is omitted) used to run BEFORE the try block, so an
    ``AdvisorDependencyError`` from a missing optional provider dependency
    escaped uncaught instead of failing soft as ``BeanExtractionError``.
    #613: this is DEPENDENCY-origin, so it is the ``BeanExtractionUnavailableError``
    subclass, not the base class."""

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        raise AdvisorDependencyError(
            "advisor provider 'anthropic' needs an optional dependency: "
            "pip install 'roastpilot-agent[anthropic]'"
        )

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    with pytest.raises(BeanExtractionUnavailableError, match="could not build its model"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG
        )


# --- #609: max_length parity for the model-returned free-text fields ---
#
# ``_ExtractedBeanIdentity``'s five short free-text fields (``name``,
# ``country``, ``bean_origin``, ``farm``, ``bean_varietal``) previously had
# NO length bound at all, unlike the deterministic JSON-LD path
# (``_MAX_JSON_LD_FIELD_CHARS`` = 500) and the four ``*_evidence`` fields
# (#590 D2a, also capped at 500). ``description`` gets its own, wider cap
# (``_MAX_DESCRIPTION_FIELD_CHARS`` = 2000) since it is intentionally
# multi-sentence prose, not a short name/label.

_MAX_LENGTH_FIELD_CAPS = {
    "name": bean_sourcing._MAX_JSON_LD_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
    "country": bean_sourcing._MAX_JSON_LD_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
    "bean_origin": bean_sourcing._MAX_JSON_LD_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
    "farm": bean_sourcing._MAX_JSON_LD_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
    "bean_varietal": bean_sourcing._MAX_JSON_LD_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
    "description": bean_sourcing._MAX_DESCRIPTION_FIELD_CHARS,  # pyright: ignore[reportPrivateUsage]
}


@pytest.mark.parametrize("field_name,cap", sorted(_MAX_LENGTH_FIELD_CAPS.items()))
def test_extracted_bean_identity_rejects_over_limit_free_text_field(
    field_name: str, cap: int
) -> None:
    """#609: a value one character over the cap fails ``model_validate`` —
    schema-level proof of the new ``Field(max_length=...)`` bound, direct
    construction (never a hand-rolled length check elsewhere)."""
    with pytest.raises(ValidationError, match=field_name):
        bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
            _identity_args(**{field_name: "a" * (cap + 1)})
        )


@pytest.mark.parametrize("field_name,cap", sorted(_MAX_LENGTH_FIELD_CAPS.items()))
def test_extracted_bean_identity_accepts_at_limit_free_text_field(
    field_name: str, cap: int
) -> None:
    """The boundary value itself (exactly ``cap`` chars) is still valid —
    only strictly OVER the cap is rejected."""
    value = "a" * cap
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(**{field_name: value})
    )
    assert getattr(identity, field_name) == value


@pytest.mark.asyncio
async def test_extract_bean_identity_maps_over_long_name_to_unavailable() -> None:
    """#609 end-to-end mechanism, confirmed rather than assumed: an
    over-long ``name`` fails the new ``max_length`` bound on every retry
    attempt (the ``FunctionModel`` double always returns the same args), so
    pydantic-ai exhausts its output-validation retries and raises
    ``UnexpectedModelBehavior`` — which ``_extract_bean_identity`` maps to
    ``BeanExtractionUnavailableError`` (#613: DEPENDENCY-origin, so HTTP 503
    at the endpoint via ``test_api.py``'s existing generic parametrized
    503 test — never a 422 as if the vendor page itself were bad). See
    ``test_draft_bean_profile_from_url_propagates_over_long_field_error``
    below for the same proof through the FULL fetch+extract pipeline."""
    model = _function_model_returning(_identity_args(name="x" * 501))
    with pytest.raises(BeanExtractionUnavailableError, match="malformed shape"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG, model=model
        )


# --- #590 D2a: evidence-quote schema + prompt (capture only, no provenance change) ---


@pytest.mark.asyncio
async def test_extract_bean_identity_round_trips_evidence_quotes() -> None:
    """The four ``*_evidence`` fields arrive on the extraction result
    unchanged — the schema addition itself, exercised through a real
    ``agent.run`` call (never hand-constructed via ``model_validate``)."""
    args = _identity_args(
        altitude_m_evidence="Altitude: 1,700-1,850m.",
        processing_evidence="Process: washed.",
        bean_species_evidence=None,
        is_blend_evidence=None,
    )
    model = _function_model_returning(args)
    identity = await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
        "page text", advisor_config=_ADVISOR_CONFIG, model=model
    )
    assert identity.altitude_m_evidence == "Altitude: 1,700-1,850m."
    assert identity.processing_evidence == "Process: washed."
    assert identity.bean_species_evidence is None
    assert identity.is_blend_evidence is None


@pytest.mark.asyncio
async def test_extract_bean_identity_prompt_still_yields_valid_identity() -> None:
    """The rewritten :data:`bean_sourcing._EXTRACTION_INSTRUCTIONS` (abstention
    bias + CoT nudge + evidence-quote instructions, #590 D2a) does not break
    the extraction agent's construction or its structured-output contract —
    a plain identity round trip still succeeds end to end."""
    model = _function_model_returning(_identity_args())
    identity = await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
        "page text", advisor_config=_ADVISOR_CONFIG, model=model
    )
    assert identity.name == "Kenya Kiambu AA (Washed)"
    assert identity.altitude_m == 1775


def test_extraction_instructions_never_offer_a_placeholder_value() -> None:
    """The abstention-bias prompt text (#590 D2a, folding in slice F) must
    say null-on-absence, not offer the model a placeholder string to use
    instead — a regression here would silently reintroduce a spurious
    "unknown"/"none" value the D1 provenance loop cannot distinguish from a
    real one."""
    assert "null" in bean_sourcing._EXTRACTION_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]
    assert '"none"' in bean_sourcing._EXTRACTION_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]
    assert "verbatim" in bean_sourcing._EXTRACTION_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]


def test_draft_from_identity_evidence_quotes_do_not_change_free_text_provenance() -> None:
    """Regression proof: attaching ``*_evidence`` quotes to an identity does
    not change how the FIVE FREE-TEXT fields verify — D1's containment gate
    for them is untouched by #590 D2b/D2c. The TYPED fields (``altitude_m``,
    ``processing``) stay byte-identical regardless of evidence quality too:
    ``processing`` is deferred to slice E unconditionally, and
    ``altitude_m``'s citation gate (:func:`bean_sourcing._quote_supports_altitude`)
    ships DORMANT (:data:`bean_sourcing._ALTITUDE_CITATION_GATE_ENABLED`) —
    round-6 (Codex) review found the guard-stack design fails open on novel
    text shapes, so enablement moves to a fail-CLOSED whitelist redesign
    (D2d, #615). See the dedicated ``--- #590 D2b/D2c`` tests below, which
    call the gate function directly, for its (currently inert) verification
    logic."""
    identity_with_evidence = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            altitude_m_evidence="Altitude: 1775m.",
            processing_evidence="washed coffee",
            bean_species_evidence=None,
            is_blend_evidence=None,
        )
    )
    identity_without_evidence = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft_with_evidence = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity_with_evidence,
        url="https://vendor.example/products/kenya",
        corpus=_IDENTITY_PAGE_TEXT,
    )
    draft_without_evidence = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity_without_evidence,
        url="https://vendor.example/products/kenya",
        corpus=_IDENTITY_PAGE_TEXT,
    )
    for field in ("name", "country", "bean_origin", "farm", "bean_varietal", "description"):
        assert draft_with_evidence.field_sources[field] == "on_page", field
        assert draft_without_evidence.field_sources[field] == "on_page", field
    # Even a genuinely quote-supported altitude ("Altitude: 1775m." — digit
    # run + "altitude" cue) stays origin_estimated while the gate is
    # dormant — no divergence from the no-evidence draft.
    assert draft_with_evidence.field_sources["altitude_m"] == "origin_estimated"
    assert draft_with_evidence.field_sources["processing"] == "origin_estimated"
    assert draft_without_evidence.field_sources["altitude_m"] == "origin_estimated"
    assert draft_without_evidence.field_sources["processing"] == "origin_estimated"
    assert "bean_species" not in draft_with_evidence.field_sources
    assert "is_blend" not in draft_with_evidence.field_sources
    # #627: field_evidence is INDEPENDENT of field_sources/the gate verdicts
    # above — a captured quote surfaces for operator judgement regardless
    # of whether its field demoted to "origin_estimated".
    assert draft_with_evidence.field_evidence["altitude_m"] == "Altitude: 1775m."
    assert draft_with_evidence.field_evidence["processing"] == "washed coffee"
    assert "bean_species" not in draft_with_evidence.field_evidence
    assert "is_blend" not in draft_with_evidence.field_evidence
    assert draft_without_evidence.field_evidence == {}


#: A page corpus carrying a genuine, corpus-backed quote for each of the
#: four typed fields (#633) — each on its OWN sentence/segment, so every
#: quote below is a whole-phrase match within a single
#: :func:`bean_sourcing._split_corpus_segments` segment.
_FOUR_FIELD_EVIDENCE_PAGE_TEXT = (
    "Kenya Kiambu AA is a washed coffee, dried on raised beds. "
    "This lot is 100% Arabica. "
    "Altitude: 1775m. "
    "This is a blend of two lots."
)


def test_draft_from_identity_field_evidence_captures_all_four_typed_fields() -> None:
    """#627/#633: every one of the four typed fields' captured quote is
    threaded onto the draft's ``field_evidence``, keyed the same way as
    ``field_sources`` — provided each quote AUTHENTICATES against the page
    (#633): appears verbatim, whole-phrase, within one corpus segment."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            processing_evidence="washed coffee, dried on raised beds",
            bean_species_evidence="100% Arabica",
            altitude_m_evidence="Altitude: 1775m.",
            is_blend=True,
            is_blend_evidence="This is a blend of two lots.",
        )
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=_FOUR_FIELD_EVIDENCE_PAGE_TEXT,
    )
    assert draft.field_evidence == {
        "processing": "washed coffee, dried on raised beds",
        "bean_species": "100% Arabica",
        "altitude_m": "Altitude: 1775m.",
        "is_blend": "This is a blend of two lots.",
    }


def test_draft_from_identity_field_evidence_drops_a_fabricated_quote() -> None:
    """#633 (Codex P2): a quote the model returns but which never actually
    appears on the page must be DROPPED from ``field_evidence`` — the
    operator must never see a possibly-fabricated string presented as
    verbatim vendor-page text. The other three genuine, corpus-backed
    quotes stay included — only the fabricated one is excluded."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            processing_evidence="washed coffee, dried on raised beds",
            # Fabricated: this sentence never appears anywhere on the page.
            bean_species_evidence="Certified 100% organic since 1995.",
            altitude_m_evidence="Altitude: 1775m.",
            is_blend=True,
            is_blend_evidence="This is a blend of two lots.",
        )
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=_FOUR_FIELD_EVIDENCE_PAGE_TEXT,
    )
    assert "bean_species" not in draft.field_evidence
    assert draft.field_evidence == {
        "processing": "washed coffee, dried on raised beds",
        "altitude_m": "Altitude: 1775m.",
        "is_blend": "This is a blend of two lots.",
    }


def test_draft_from_identity_field_evidence_drops_a_cross_segment_splice() -> None:
    """#633: whole-corpus normalized containment alone would let a
    sentence-ending period turn "...at 1800." + "Masl is..." into the
    contiguous phrase "1800 masl" once normalized — a splice across TWO
    separate sentences, never actually written together (same repro
    shape as :func:`test_quote_supports_altitude_cross_sentence_splice_demotes`).
    :func:`bean_sourcing._find_authentic_segment` requires a whole-phrase
    match WITHIN one segment, so this must be dropped from
    ``field_evidence`` too — not just kept out of ``field_sources``."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="1800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This farm sits at 1800. Masl is the unit used here.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"
    assert "altitude_m" not in draft.field_evidence


@pytest.mark.parametrize("blank_quote", ["", "   ", None])
def test_draft_from_identity_field_evidence_omits_blank_or_absent_quotes(
    blank_quote: str | None,
) -> None:
    """#627: a blank/whitespace-only/``None`` evidence quote leaves the
    field simply ABSENT from ``field_evidence`` — never an empty-string
    entry — the same "absent means unset" convention as ``field_sources``."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            processing_evidence=blank_quote,
            bean_species_evidence=blank_quote,
            altitude_m_evidence=blank_quote,
            is_blend_evidence=blank_quote,
        )
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=_IDENTITY_PAGE_TEXT,
    )
    assert draft.field_evidence == {}


def test_draft_from_identity_field_evidence_strips_surrounding_whitespace() -> None:
    """#627: a quote is stripped like every other optional identity text
    field (:func:`bean_sourcing._normalize_optional_text`)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing_evidence="  washed coffee  ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=_IDENTITY_PAGE_TEXT,
    )
    assert draft.field_evidence["processing"] == "washed coffee"


def test_draft_from_identity_at_limit_name_still_verifies_on_page() -> None:
    """#609 regression: D1's free-text containment gate (#590 D1) is
    unaffected by the new ``max_length`` bound — a ``name`` value exactly AT
    the 500-char cap still verifies ``"on_page"`` when it is genuinely
    present in the corpus, the same as any shorter value would."""
    at_limit_name = "a" * bean_sourcing._MAX_JSON_LD_FIELD_CHARS  # pyright: ignore[reportPrivateUsage]
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(name=at_limit_name)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"Product: {at_limit_name}. {_IDENTITY_PAGE_TEXT}",
    )
    assert draft.field_sources["name"] == "on_page"


def test_draft_from_identity_abstained_processing_has_no_spurious_provenance() -> None:
    """A model that abstains (``processing=None``, page silent) — per the
    D2a prompt's null-on-absence rule — must yield no value and no
    ``field_sources`` entry for it, evidence quote or not (#590 D2a: the
    null-on-absence prompt behaviour holding at the schema/draft level,
    independent of any live LLM call)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing=None, processing_evidence=None)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.processing is None
    assert "processing" not in draft.field_sources


# --- #590 D2b/D2c: the altitude citation gate (narrowed to altitude_m
# only — Codex round-1 triage: sound processing/bean_species verification
# needs locality + conflicting-method logic deferred to slice E) ---
#
# RETIRED (#617 D2d-a): the guard-stack matcher FAILED OPEN across two
# enable attempts (#615/#616 found 6 more shape bypasses on top of 5 prior
# hardening folds) — its machinery is deleted;
# :func:`bean_sourcing._quote_supports_altitude` is now a stub always
# returning ``False``. The gate stays DORMANT
# (``_ALTITUDE_CITATION_GATE_ENABLED = False``), so tests below asserting a
# "_demotes" outcome via ``_draft_from_identity`` remain valid proof of
# today's SAFE runtime behaviour — they just no longer exercise the
# specific (now-deleted) mechanism their docstring names. The sibling
# direct-call tests that proved each mechanism are deleted; #617 D2d-b's
# whitelist grammar re-proves the same adversarial shapes structurally.


def test_quote_supports_altitude_price_cited_as_evidence_is_rejected() -> None:
    """HEADLINE adversarial test: a model citing a PRICE span as altitude
    evidence must be rejected. The quote genuinely appears on the page
    (authentic single-segment span); ``"$18.00"``'s cents are only 2
    digits, so :func:`_elides_as_thousands_separator` (#590 D2b fix 1)
    correctly refuses to collapse it into a hallucinated ``"1800"`` — no
    digit match at all, and "priced at $18.00" also has no elevation cue
    anywhere, so the gate must still demote either way."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="priced at $18.00")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} This lot is priced at $18.00 per pound.",
    )
    assert draft.altitude_m == 1800
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_decimal_comma_price_demotes() -> None:
    """#590 D2b fix 1 (Codex round, SXVDN): the old unconditional
    inter-digit elision let a decimal-comma price like "€18,00" collapse
    to a hallucinated "1800" digit token, which then paired with an
    UNRELATED "High-altitude" cue within the proximity window. Only a
    VALID 3-digit thousands grouping elides now
    (:func:`bean_sourcing._elides_as_thousands_separator`), so "18,00"
    (2 digits after the comma) never produces "1800" at all."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="High-altitude coffee costs €18,00")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="High-altitude coffee costs €18,00 per bag.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_prefix_glued_m_does_not_verify() -> None:
    """#590 D2c fold 2 (SX60f): the glued-"m" shortcut is SUFFIX-only — a
    PREFIX form like "M1800" (a model number) has the same
    :func:`bean_sourcing._alpha_runs` shape (``["m"]``) as a genuine glued
    unit but must NOT verify. No other cue is present, so the quote
    demotes entirely."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="Model M1800 humidifier")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This shop also sells the Model M1800 humidifier separately.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_hyphen_glued_identifier_prefix_does_not_verify() -> None:
    """#590 D2c fold 2 (security-reviewer BLOCKER): "SKU-1800m" splits to
    ``["SKU", "1800m"]`` via :func:`bean_sourcing._proximity_tokens` (the
    hyphen is an ordinary boundary), so the suffix-glued-m shortcut alone
    saw a clean "1800m" token. :func:`bean_sourcing._is_isolated_value_token`
    checks the RAW text directly: the character before the value's first
    digit is a hyphen, not whitespace/start/opening-punctuation, so this
    must demote."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="product code SKU-1800m in stock")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This page lists product code SKU-1800m in stock today.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_space_separated_bare_m_still_demotes() -> None:
    """The safe-direction mirror of fix 2: "1850 m" (space-separated, not
    glued) must NOT verify off the bare "m" alone — only a unit glued
    directly onto the matching digits is unambiguous enough to count."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1850, altitude_m_evidence="1850 m")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This lot sits at 1850 m according to the survey.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_glued_m_on_different_number_has_no_effect() -> None:
    """A bare "m" glued to a DIFFERENT (non-matching) number must not
    leak into verifying an unrelated claimed altitude — the digit run
    never matches the claimed value at all."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="priced at 18m tall shelving")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This shop uses priced at 18m tall shelving for storage.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_review_count_laundering_demotes() -> None:
    """A quote can be genuine page text and contain BOTH a number equal to
    the claimed altitude and an elevation-cue word — while the number is
    actually a review count and the cue belongs to a different sentence
    about the shop's physical elevation/directions. This quote also spans
    a sentence boundary internally ("...this year. View..."), so it fails
    the fix-3 authenticity check (:func:`bean_sourcing._find_authentic_segment`)
    outright — it was never a genuine single-segment span."""
    corpus = (
        "Free shipping on all orders. Customer reviews: 1,800 5-star ratings this year. "
        "View our shop's elevation and directions on the map page for visiting hours."
    )
    quote = "reviews: 1,800 5-star ratings this year. View our shop's elevation and directions"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_phone_number_laundering_demotes() -> None:
    """The phone-number variant of the same laundering class also spans a
    sentence boundary ("...order. Our elevation-view...") and fails
    authenticity."""
    quote = "Call 1800 555 0100 to place an order. Our elevation-view tasting room is open daily."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} {quote}",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_punctuation_glued_launder_demotes() -> None:
    """#590 D2b fix 2 (SH8b9) HEADLINE repro: a colon/semicolon/hyphen-glued
    chain used to whitespace-tokenize into ONE token, putting the digits
    and "elevation" at proximity distance 0 (always inside the window,
    regardless of how far apart they really are). Retokenizing on
    punctuation AND whitespace (:func:`bean_sourcing._proximity_tokens`)
    fixes the distance computation; this exact quote also contains ';'
    (a fix-3 sentence boundary), so it fails authenticity too — both
    fixes converge on the same demote."""
    quote = "Reviews:1,800;shipping;shop-elevation-map"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} {quote}",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_stock_level_launder_demotes() -> None:
    """#590 D2b fix 1 (SH8bw) HEADLINE repro: standalone "level" used to be
    an elevation cue, so "Stock level: 1,800 bags" (a genuine, single-
    sentence, authentic page span) verified a confabulated altitude off
    the bare word "level" alone. Dropping standalone above/sea/level (now
    only the complete "above sea level" phrase counts) closes this."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="Stock level: 1,800 bags")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Stock level: 1,800 bags remain in inventory.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


@pytest.mark.parametrize(
    ("evidence_quote", "corpus_sentence"),
    [
        (
            "1,800 parameter guide",
            "Featured in our 1,800 parameter guide for enthusiasts.",
        ),
        ("1800 kilometer drive", "This is a 1800 kilometer drive from the coast."),
        ("1800 diameter", "The pipe has a 1800 diameter design."),
    ],
)
def test_quote_supports_altitude_whole_word_cue_closes_substring_launder(
    evidence_quote: str, corpus_sentence: str
) -> None:
    """#590 D2b (claude-review): ``"meter"`` used to match as a SUBSTRING
    inside ``"parameter"``/``"diameter"``/``"kilometer"`` — a false cue
    sitting directly adjacent to an unrelated, genuinely on-page number
    still passed the proximity window. Whole-word cue matching
    (:func:`bean_sourcing._token_carries_altitude_cue`) closes this."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=evidence_quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus_sentence
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_cross_sentence_splice_demotes() -> None:
    """#590 D2b fix 3 (SH8b8) HEADLINE repro: whole-corpus normalized
    containment turns a sentence-ending period into a space, so
    "...at 1800." + "Masl is..." (two DIFFERENT sentences) reads as the
    contiguous phrase "1800 masl" once normalized — a fabricated citation
    that was never actually written together. The authentic-span check
    (:func:`bean_sourcing._find_authentic_segment`) requires the quote to
    be a whole-phrase match WITHIN a single corpus segment, closing this."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="1800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This farm sits at 1800. Masl is the unit used here.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #590 D2c fold 1: cue-value BINDING (STRICT ADJACENCY, security review) ---


def test_quote_supports_altitude_unbound_cue_across_a_real_noun_phrase_demotes() -> None:
    """#590 D2c fold 1 (SX60U) HEADLINE repro: the old plain ``abs(distance)
    <= window`` scan let "High-altitude coffee with 1,800 reviews" bind the
    "altitude" cue to an unrelated review count 3 words away. Binding now
    requires STRICT ADJACENCY (:func:`bean_sourcing._cue_binds_to_value`) —
    "coffee"/"with" push the cue well past distance 1, so this must
    demote."""
    evidence = "High-altitude coffee with 1,800 reviews"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=evidence)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{evidence} this season.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #590 D2c fold 3: non-metre unit rejection ---


def test_quote_supports_altitude_feet_unit_demotes_despite_adjacent_cue() -> None:
    """#590 D2c fold 3 (SX60o) HEADLINE repro: "Elevation: 1,800 ft" must
    never certify a metres claim of 1800, even though "Elevation" sits
    immediately adjacent — a non-metre unit rejects the reading outright
    (:func:`bean_sourcing._digit_token_has_non_metre_unit`)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="Elevation: 1,800 ft")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Elevation: 1,800 ft above the valley floor.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #590 D2c fold 5: range-endpoint rejection ---


@pytest.mark.parametrize(
    "corpus",
    [
        "This farm sits at 1,600-1,800 masl on the slope.",
        "This farm sits at 1600 to 1800 masl on the slope.",
        "This farm sits at between 1600 and 1800 masl on the slope.",
        "This farm sits at 1,800-1,600 masl on the slope.",
    ],
)
def test_quote_supports_altitude_range_endpoint_demotes(corpus: str) -> None:
    """#590 D2c fold 5 (SXVDY) HEADLINE repro: a page-stated RANGE
    ("1,600-1,800 masl" / "1600 to 1800 masl" / "between 1600 and 1800
    masl", ascending or descending) cropped down to a quote naming only
    one bound must not certify a scalar reading of 1800 — checked on the
    AUTHENTIC SEGMENT, not the cropped quote
    (:func:`bean_sourcing._value_is_range_endpoint`)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="1,800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- Additional negative-probe regressions (security-reviewer "already
# held" categories) confirmed to still demote under the D2c hardening ---


@pytest.mark.parametrize(
    ("evidence_quote", "corpus"),
    [
        # A square-metres suffix ("m2") is NOT the glued bare-"m" unit —
        # no adjacent cue means no other path certifies it either.
        ("1800m2 apartment for sale", "This building has a 1800m2 apartment for sale nearby."),
        # "kms" (informal plural of km) is a non-metre unit.
        ("Elevation: 1,800 kms", "Elevation: 1,800 kms away from the coast."),
        # A trailing-period abbreviation tokenizes to the plain unit.
        ("Elevation 1800 ft.", "Elevation 1800 ft. above the valley floor."),
        # A hyphenated adjective form ("50-foot") still rejects via the
        # immediately-following-unit check.
        ("a 1800-foot antenna", "The site has a 1800-foot antenna nearby."),
        # IP-like digit noise never carries a whole-word elevation cue.
        (
            "Server at 192.168.1.1800 configuration notes",
            "Server at 192.168.1.1800 configuration notes follow below.",
        ),
        # A bare "above" (no complete "above sea level" phrase) is not a cue.
        ("1800 above market value", "This lot sold for 1800 above market value last year."),
    ],
)
def test_quote_supports_altitude_additional_negative_probes_still_demote(
    evidence_quote: str, corpus: str
) -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence=evidence_quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_forward_range_with_glued_units_demotes() -> None:
    """#590 D2c fold 4 (security-reviewer BLOCKER): the backward-only
    original range check never looked FORWARD, so "grown at 1,800m to
    2,000m" bound via the glued unit and certified the lower endpoint as a
    scalar. :func:`bean_sourcing._looks_like_a_value_token` recognizes a
    unit-glued endpoint ("2000m") as a value token too, so the forward
    "to"-then-value pattern now catches this."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="grown at 1,800m")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This farm is grown at 1,800m to 2,000m depending on the plot.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #590 D2c fold 5 MEDIUM: bound qualifiers ---


def test_quote_supports_altitude_up_to_bound_demotes() -> None:
    """#590 D2c fold 5 MEDIUM (security-reviewer): "up to 1,800 masl"
    states a ceiling, not a scalar reading — "to" preceded by "up"
    demotes even though there is no digit on either side."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="up to 1,800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This region grows coffee up to 1,800 masl in the highlands.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_max_qualifier_bound_demotes() -> None:
    """ "1,800 masl max" states an upper bound, not that the farm sits at
    exactly 1800m — the qualifier is allowed 2 tokens forward so the unit
    token ("masl") may sit between the value and "max"."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="1,800 masl max")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This farm sits at 1,800 masl max on the ridge.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_draft_from_identity_processing_and_species_demote_without_an_anchor() -> None:
    """#590 slice E2 (superseding the D2b-era unconditional demote this
    test originally proved): even a genuine, well-cued, well-formed
    evidence quote for BOTH enum fields still demotes when the page
    carries NEITHER anchor (no frontmatter ``title:`` block, no matched
    JSON-LD name) — with no anchor, :func:`bean_sourcing._main_product_region`
    collapses to ``""`` (fail-CLOSED, no whole-corpus fallback), so the
    quotes have nowhere authentic to land."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            processing="washed",
            processing_evidence="fully washed process",
            bean_species="arabica",
            bean_species_evidence="100% arabica",
        )
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=(
            f"{_IDENTITY_PAGE_TEXT} This lot went through a fully washed process before "
            "drying. This is 100% arabica."
        ),
    )
    assert draft.field_sources["processing"] == "origin_estimated"
    assert draft.field_sources["bean_species"] == "origin_estimated"


def test_draft_from_identity_is_blend_demotes_with_no_anchor_even_with_a_genuine_quote() -> None:
    """#590 slice E1b (executable spec): a genuinely on-page, genuinely
    authentic evidence quote still demotes when the page carries NEITHER
    anchor (no frontmatter ``title:`` — this ``corpus`` has no leading
    ``---`` block — and no JSON-LD product name). With no anchor,
    :func:`bean_sourcing._main_product_region` collapses to ``""`` —
    there is no whole-corpus fallback (that would be fail-OPEN) — so the
    quote has nowhere to authenticate against and the claim demotes
    regardless of how genuine it is."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence="a blend of three origins")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/blend",
        corpus="This is a blend of three origins, roasted together.",
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"


def test_draft_from_identity_altitude_ships_dormant() -> None:
    """#617 terminal probe: the citation gate ships DORMANT PERMANENTLY
    (:data:`bean_sourcing._ALTITUDE_CITATION_GATE_ENABLED` is ``False``)
    — even a PERFECT citation (genuine, region-authenticated, no
    conjunction/comma-compound heading, no clause-bridging quote gap, a
    real context-cued unit) must still demote at runtime, because
    :func:`_draft_from_identity` gates the flip on the flag. The
    underlying gate function itself (the whitelist grammar stays built
    and unit-tested, just permanently unconsumed) would return ``True``
    for this exact quote — the two-round enable arc closed five, then
    three, real leaks in this exact mechanism before the terminal probe
    found two more, so it never got to run this correctly in
    production."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="grown at 1,800 masl")
    )
    corpus = _framed(
        "Kenya Kiambu AA (Washed)",
        "Kenya Kiambu AA (Washed) is a washed coffee from Kenya, grown at 1,800 masl on the "
        "Gakuyuini Factory farm. Variety: SL28, SL34. Tasting notes: blackcurrant, tomato, "
        "bright acidity.\n",
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"
    main_region = bean_sourcing._main_product_region(corpus, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "grown at 1,800 masl", main_region
        )
        is True
    )


# --- #617 terminal probe: the two plausible certify-leaks that PARKED
# altitude certification permanently (see
# :data:`bean_sourcing._ALTITUDE_CITATION_GATE_ENABLED`'s docstring). The
# whitelist grammar stays built and unit-tested (all the other fold tests
# in this file still pass), but these two repros are the MOTIVATING
# EVIDENCE for the park: each asserts the CURRENT actual (leaking)
# outcome, not the desired one — do NOT "fix" these assertions without
# also revisiting the park decision itself. ---


@pytest.mark.parametrize("separator", ["; ", "/ ", "| "])
def test_heading_matches_anchor_separator_sweep_incompleteness_is_a_leak(
    separator: str,
) -> None:
    """Terminal-probe repro (leak 1): the comma/conjunction sweep (#617
    fold 4, fold 4-FIX-1) covered only the ONE separator character
    actually repro'd at each round, not the general class — ";"/"/"/"|"
    still anchor a compound heading exactly like the comma and "&"/"and"
    did before their own fixes. This is the CURRENT LEAKING behavior
    (``True``), asserted as the park's motivating evidence — it is NOT
    the desired outcome."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor(f"Kenya AA{separator}Sumatra", ["kenya aa"]) is True


def test_quote_supports_altitude_slash_heading_cross_bean_misattribution_is_a_leak() -> None:
    """Terminal-probe repro (leak 1, end-to-end): a page anchored on
    "Kenya AA" has a "## Kenya AA / Sumatra Mandailing" section stating a
    SIBLING lot's altitude ("1,800 metres") — the slash-joined heading
    still anchors (leak 1), so the sibling's reading sits INSIDE the main
    region and cross-bean-certifies. Asserted as the park's motivating
    evidence — it is NOT the desired outcome."""
    body = _framed(
        "Kenya AA",
        "## Kenya AA / Sumatra Mandailing\n"
        "Our Sumatra Mandailing lot is grown at 1,800 metres in the highlands.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "is grown at 1,800 metres", region
        )
        is True
    )


def test_quote_supports_altitude_paren_bridged_quote_gap_is_a_leak() -> None:
    """Terminal-probe repro (leak 2): the clause-break character set
    (#617 fold 4-FIX-2) was a hand-picked subset of the module's own
    boundary-punctuation set — it never included the paren/bracket/pipe
    family, so a 2-char gap through ")" still bridges an unrelated
    clause: "This farm has (with a lovely tasting room) 1800masl of
    shelving space nearby" certifies off a quote naming only the
    parenthetical, which never mentions the number at all. Asserted as
    the park's motivating evidence — it is NOT the desired outcome."""
    corpus = "This farm has (with a lovely tasting room) 1800masl of shelving space nearby."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "with a lovely tasting room", corpus
        )
        is True
    )


def test_quote_supports_altitude_pipe_bridged_quote_gap_is_a_leak() -> None:
    """Terminal-probe repro (leak 2, pipe variant): "Farm Notes| 1800masl
    of shelving space in the warehouse" certifies off a quote naming only
    "Farm Notes", which never mentions the number either — the same
    clause-break gap, through "|" instead of ")". Asserted as the park's
    motivating evidence — it is NOT the desired outcome."""
    corpus = "Farm Notes| 1800masl of shelving space in the warehouse."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Farm Notes", corpus
        )
        is True
    )


def test_quote_supports_altitude_context_word_referent_mismatch_is_a_leak() -> None:
    """Terminal-probe repro (leak 3, the context-word medium): "This
    locally grown coffee sits beside 1,800 metres of shelving" certifies
    — "grown" is genuinely within the context-word window, but it
    describes the COFFEE, not the shelving measurement; a proximity
    check has no way to verify the word's actual REFERENT, only its
    distance. Asserted as the park's motivating evidence — it is NOT the
    desired outcome."""
    corpus = "This locally grown coffee sits beside 1,800 metres of shelving"
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, corpus, corpus
        )
        is True
    )


def test_draft_from_identity_altitude_no_anchor_page_demotes_despite_a_perfect_quote() -> None:
    """The fail-CLOSED law at the :func:`_draft_from_identity` level: a
    page with NEITHER a frontmatter title nor a matched JSON-LD name has
    an EMPTY main region (:func:`bean_sourcing._main_product_region`'s
    documented fail-closed collapse) — even a perfect, genuine,
    grammar-matching citation demotes, because there is nowhere authentic
    for it to land. The underlying gate function (tested directly
    elsewhere) returns ``True`` for this exact quote against a region
    that actually contained it."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="grown at 1,800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This lot is grown at 1,800 masl, no frontmatter or heading anchor anywhere.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800,
            "grown at 1,800 masl",
            "This lot is grown at 1,800 masl, no frontmatter or heading anchor anywhere.",
        )
        is True
    )


def test_draft_from_identity_altitude_wrong_entity_sibling_product_demotes() -> None:
    """The wrong-entity class the E-gated fields close, now closed for
    altitude too (#617 D2d-b): a single-origin page's own anchored region
    never mentions an altitude at all; a SIBLING product's "1,900 masl"
    sits under an unmatched "## You May Also Like" heading, outside the
    main region entirely. Citing that line demotes even though it is a
    genuine, grammar-matching page span — it simply belongs to the wrong
    product."""
    corpus = _framed(
        "Ethiopia Yirgacheffe Single Origin",
        "This lot is a single origin coffee from Yirgacheffe.\n"
        "\n"
        "## You May Also Like\n"
        "Our Kenya AA grows at 1,900 masl on the Gakuyuini Factory farm.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1900, altitude_m_evidence="Our Kenya AA grows at 1,900 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #617 D2d-b: the positive matrix (direct-call, region text == the
# raw text under test, bypassing _main_product_region) ---


@pytest.mark.parametrize(
    "evidence_quote",
    [
        "grown at 1,800 masl",
        "1,800 masl",
        "1800masl",
        "elevation of 1800 metres",
        "1800 metres elevation",
        "at 1800 above sea level",
        "1800 above sea level",
    ],
)
def test_quote_supports_altitude_genuinely_verified_flips_on_page(evidence_quote: str) -> None:
    """A quote that both (a) is an authentic single-segment page span and
    (b) genuinely matches the #617 D2d-b whitelist grammar (a ``NUMBER
    UNIT`` shape — comma-grouped digits, a glued unit, a unit before or
    after the value, or the complete "above sea level" phrase) — the
    ENABLED gate returns ``True``."""
    corpus = f"{_IDENTITY_PAGE_TEXT} This lot is {evidence_quote}."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, evidence_quote, corpus
        )
        is True
    )


def test_quote_supports_altitude_cue_first_unitless_form_now_demotes() -> None:
    """#617 D2d-b's accepted OVER-DEMOTE: "Elevation: 1,800" has no
    recognized unit adjacent to the digits (a bare colon is not a unit),
    so the whitelist grammar does not certify it — the whole cue-adjacency
    surface the retired guard-stack matcher used is gone entirely. #617's
    stopping rule explicitly accepts this direction of change (over-
    demotes never count as a certify-bypass)."""
    evidence_quote = "Elevation: 1,800"
    corpus = f"{_IDENTITY_PAGE_TEXT} This lot is {evidence_quote}."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, evidence_quote, corpus
        )
        is False
    )


def test_quote_supports_altitude_thousands_period_glued_metre_verifies() -> None:
    """A European-style thousands-period number glued to a bare "m" unit
    ("1.850m") must verify — the segmenter
    (:func:`bean_sourcing._split_corpus_segments`) must not treat that
    period as a sentence boundary, or the authentic-span check could
    never see the quote as a single segment."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1850, "grown at 1.850m", "This farm is grown at 1.850m above the valley floor."
        )
        is True
    )


def test_quote_supports_altitude_shape_stands_alone_irrelevant_surrounding_copy() -> None:
    """The grammar needs no elevation cue at all — the shape itself is
    sufficient evidence, regardless of what the rest of the page says.
    (NOT phrased as "Beans FROM 1,750 masl" — #617 fold 3 makes "from" a
    PRE-bound qualifier, see
    ``test_quote_supports_altitude_from_qualifier_bound_demotes``.)"""
    corpus = "Tasting notes: blackcurrant and stone fruit. Farm data: 1,750 masl. Roasted fresh."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1750, "Farm data: 1,750 masl", corpus
        )
        is True
    )


def test_quote_supports_altitude_real_single_sentence_quote_verifies() -> None:
    """A genuine sentence among many others still verifies — a quote that
    genuinely sits within ONE sentence, surrounded by other sentences on
    both sides (a page with many segments, not just the target one)."""
    corpus = "Random intro text. This farm sits at 1800 masl elevation. More text follows after."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1800 masl elevation", corpus
        )
        is True
    )


# --- #617 D2d-b: additional grammar-branch coverage (direct-call, region
# text == the raw text under test, bypassing _main_product_region so the
# shape/context-guard machinery is what's actually exercised rather than
# short-circuiting on an empty region) ---


def test_quote_supports_altitude_plain_multidigit_glued_unit_verifies() -> None:
    """A plain multi-digit NUMBER with no thousands separator at all
    (exercising :func:`bean_sourcing._altitude_number_at`'s digit-run loop
    beyond the first character) glued to a unit still certifies."""
    text = "This farm is grown at 1850m above the valley floor."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1850, text, text
        )
        is True
    )


def test_altitude_whitelist_match_glued_unit_boundary_break_falls_through_to_space_check() -> None:
    """A glued unit match that fails ITS OWN trailing-boundary check must
    fall through to the space-separated form, not return early — "1800m2"
    (a digit glued right after "m") never certifies either way, since
    there is no space-separated unit here either."""
    text = "This building has a 1800m2 apartment for sale nearby."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_glued_unit_hyphen_suffix_falls_through() -> None:
    """The hyphen-glued-suffix mirror of the "1800m2" case: "1800masl-x" —
    the glued "masl" match fails its trailing-boundary check (a hyphen
    right after), and no space-separated unit follows either."""
    text = "This lot is coded 1800masl-x in our internal system."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_space_unit_boundary_break_falls_through_to_phrase_check() -> None:
    """A space-separated unit match that fails its trailing-boundary check
    must fall through to the "above sea level" phrase check, not return
    early — "1800 masl2" (a digit glued right after "masl") never
    certifies, and the phrase check correctly also fails ("masl2" is not
    "above")."""
    text = "This sign reads 1800 masl2 at the trailhead."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_above_sea_level_missing_space_demotes() -> None:
    """The "above sea level" phrase match requires an ACTUAL single space
    between each word — "above-sea level" (a hyphen instead of a space
    after "above") does not match the phrase, and there is no other unit
    adjacent either."""
    text = "This farm sits at 1800 above-sea level on the ridge."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_above_sea_levels_word_mismatch_demotes() -> None:
    """The phrase match's word-equality check: "above sea levels" reads a
    FULL alphabetic run ("levels", not "level") for the third word, which
    does not equal "level" at all — a plain mismatch, not a boundary
    break."""
    text = "This farm sits at 1800 above sea levels on the ridge."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_above_sea_level_glued_digit_suffix_demotes() -> None:
    """The phrase match's OWN trailing-boundary check
    (:func:`bean_sourcing._match_above_sea_level`): "above sea level2" —
    the alpha run for the third word reads exactly "level" (stopping at
    the digit), satisfying the word-equality check, but the digit
    immediately glued after it breaks the trailing word boundary, so the
    phrase still does not match."""
    text = "This farm sits at 1800 above sea level2 on the ridge."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, text, text
        )
        is False
    )


def test_altitude_whitelist_match_non_metre_unit_adjacent_to_a_clean_shape_demotes() -> None:
    """#617 D2d-b context guard: a non-metre unit sitting in the word
    IMMEDIATELY before an otherwise clean, structurally-complete
    ``NUMBER UNIT`` shape must still reject it — a mixed-unit page stating
    a feet reading directly beside the metres one must not let the metres
    reading certify uncontested."""
    text = "This farm reads 5,905 ft 1,800 masl at the gate."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1,800 masl", text
        )
        is False
    )


def test_altitude_whitelist_match_range_joiner_word_with_no_far_digit_is_unaffected() -> None:
    """The safe-direction mirror of the unit-mediated-range guard: a range
    joiner word ("to") sitting near the shape but NOT followed by another
    digit run within the window must not false-fire — "grown at 1,800
    masl to great acclaim" is an ordinary sentence, not a range."""
    text = "This farm is grown at 1,800 masl to great acclaim."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1,800 masl", text
        )
        is True
    )


def test_altitude_whitelist_match_skips_a_rejected_occurrence_for_a_later_clean_one() -> None:
    """#617 D2d-b step 3's stated behavior: an earlier occurrence failing
    the context guard does not stop
    :func:`bean_sourcing._altitude_whitelist_match` from certifying a
    LATER, genuinely clean occurrence of the same value in the same
    segment — "up to 1,800 masl" (bound, rejected) followed by "sits at
    1,800 masl exactly" (clean) within one sentence."""
    text = "This farm ranges up to 1,800 masl and a neighbouring plot sits at 1,800 masl exactly"
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "sits at 1,800 masl exactly", text
        )
        is True
    )


def test_altitude_whitelist_match_all_occurrences_bounded_demotes() -> None:
    """The negative control for the multi-occurrence loop: when EVERY
    occurrence in the segment is context-guard-rejected, the whole match
    demotes — it does not just stop at the first rejection and skip
    forward past a clean one that doesn't exist."""
    text = "This region grows coffee up to 1,800 masl and never above 1,800 masl either."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "up to 1,800 masl", text
        )
        is False
    )


# --- #617 fold 1 (BLOCKER, adversarial review post-D2d-b): the shape scan
# must be anchored to the QUOTE's own raw span, not just anywhere in the
# authentic segment — else a model can cite ANY authentic clause of a
# sentence while the number it names sits elsewhere in that same segment. ---


def test_quote_supports_altitude_unrelated_clause_in_same_segment_demotes() -> None:
    """Reviewer repro: the quote cites a genuinely authentic clause of the
    sentence ("which also produces excellent honey-processed lots") that
    never mentions 1,900 at all — the claimed altitude sits in a
    DIFFERENT clause of the SAME segment. Without a quote-anchored shape
    scan this used to certify (any authentic segment text, wherever the
    shape happened to be); #617 fold 1 requires the matched shape to
    overlap the quote's own span, so this now demotes."""
    corpus = (
        "This coffee, which also produces excellent honey-processed lots, "
        "grows at 1,900 masl in a nearby valley."
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(
            altitude_m=1900,
            altitude_m_evidence="which also produces excellent honey-processed lots",
        )
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=_framed("Kenya Example", corpus),
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1900, "which also produces excellent honey-processed lots", corpus
        )
        is False
    )


def test_quote_supports_altitude_quote_actually_containing_the_number_verifies() -> None:
    """The positive control for fold 1: the SAME sentence, but the model's
    quote genuinely contains "1,900 masl" this time — the matched shape
    sits inside the quote's own span, so it certifies."""
    corpus = (
        "This coffee, which also produces excellent honey-processed lots, "
        "grows at 1,900 masl in a nearby valley."
    )
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1900, "grows at 1,900 masl in a nearby valley", corpus
        )
        is True
    )


def test_quote_supports_altitude_shape_just_outside_the_quote_span_margin_demotes() -> None:
    """The margin (:data:`bean_sourcing._ALTITUDE_QUOTE_SPAN_MARGIN_CHARS`)
    is a SMALL punctuation-trim allowance, not a licence to reach a
    distant clause — a shape well beyond the margin outside the quote's
    own span still demotes even though it is in the SAME authentic
    segment."""
    corpus = (
        "This coffee, grown with meticulous care across several small family "
        "plots nestled in the highlands, reaches 1,950 masl at its peak."
    )
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1950,
            "grown with meticulous care across several small family plots",
            corpus,
        )
        is False
    )


def test_locate_quote_span_returns_none_for_a_blank_after_normalization_quote() -> None:
    """Direct unit coverage: a quote that normalizes to zero words (pure
    punctuation) has no span to locate."""
    locate = bean_sourcing._locate_quote_span  # pyright: ignore[reportPrivateUsage]
    assert locate("...", "This farm sits at 1,800 masl.") is None


def test_locate_quote_span_returns_none_when_word_sequence_is_absent() -> None:
    """Direct unit coverage: a quote whose word sequence never appears in
    the segment (even though both are non-blank) cannot be located."""
    locate = bean_sourcing._locate_quote_span  # pyright: ignore[reportPrivateUsage]
    assert locate("completely unrelated words", "This farm sits at 1,800 masl.") is None


# --- #617 fold 2 (BLOCKER, adversarial review post-D2d-b): a GENERIC
# metre unit ("m"/"metre(s)"/"meter(s)") is an ordinary length unit with
# countless non-altitude readings, so it additionally needs an
# altitude-context word nearby; self-sufficient units (masl/asl/msnm, or
# "above sea level") still need none. ---


@pytest.mark.parametrize(
    "corpus",
    [
        "This building is 1,800 metres of shelving in total.",
        "The gauge shows a 1800 meter reading today.",
        "The warehouse stocks 1800 meters of copper for the project.",
        "This lot fetched £1,800m revenue at auction last year.",
    ],
)
def test_quote_supports_altitude_generic_unit_without_context_word_demotes(corpus: str) -> None:
    """Reviewer repros: a generic metre unit with NO altitude-context word
    within the window must demote, even though the shape itself is
    structurally complete."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, corpus, corpus
        )
        is False
    )


@pytest.mark.parametrize(
    ("value", "corpus"),
    [
        (1850, "This farm is grown at 1,850 metres in the highlands."),
        (1850, "The elevation of 1.850m is typical for this region."),
    ],
)
def test_quote_supports_altitude_generic_unit_with_context_word_verifies(
    value: int, corpus: str
) -> None:
    """Reviewer repros: a generic metre unit WITH an altitude-context word
    ("grown"/"elevation") within the window certifies."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            value, corpus, corpus
        )
        is True
    )


def test_quote_supports_altitude_bare_generic_unit_is_a_documented_over_demote() -> None:
    """#617 fold 2's accepted OVER-DEMOTE: a bare generic-unit reading with
    NO context word anywhere nearby ("1850m" entirely standing alone, no
    "grown"/"elevation"/etc.) now demotes — the safe direction; a
    self-sufficient unit (masl/asl/msnm) needs no such cue, see
    ``test_quote_supports_altitude_shape_stands_alone_irrelevant_surrounding_copy``."""
    corpus = "Product code: 1850m. Ships in 3-5 business days."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1850, "Product code: 1850m", corpus
        )
        is False
    )


# --- #617 fold 3 (MEDIUM, adversarial review post-D2d-b): context-guard
# set extensions — comma-glued digit runs, the "or" joiner, the "from"
# pre-qualifier, and the "higher"/"lower" post-qualifiers. ---


def test_quote_supports_altitude_comma_glued_digit_run_range_demotes() -> None:
    """Reviewer repro: "1,600, 1,800 masl" — the comma glued directly onto
    the first number is BOTH the far digit run and its own joiner, a
    shape the word/char joiner scan alone would miss (neither "1,600,"
    nor a bare comma is a recognized joiner WORD)."""
    corpus = "This farm sits at 1,600, 1,800 masl on the slope."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1,800 masl", corpus
        )
        is False
    )


def test_quote_supports_altitude_or_joiner_range_demotes() -> None:
    """Reviewer repro: "1,600 or 1,800 masl" is the same disjunctive-range
    shape as "to"/"and", now closed for "or" too."""
    corpus = "This farm sits at 1,600 or 1,800 masl on the slope."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1,800 masl", corpus
        )
        is False
    )


def test_quote_supports_altitude_from_qualifier_bound_demotes() -> None:
    """Reviewer repro: "Grown from 1,200 masl" states a floor, not a
    scalar reading — "from" now sits in the PRE-bound qualifier set."""
    corpus = "Grown from 1,200 masl in select highland plots."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1200, corpus, corpus
        )
        is False
    )


@pytest.mark.parametrize("qualifier", ["higher", "lower"])
def test_quote_supports_altitude_higher_lower_post_qualifier_bound_demotes(
    qualifier: str,
) -> None:
    """Reviewer repro: "1,800 masl and higher/lower" states an open-ended
    bound — "and" ALONE does not fire here (nothing after it starts with
    a digit), so "higher"/"lower" must independently be recognized as a
    post-qualifier."""
    corpus = f"This region grows coffee at 1,800 masl and {qualifier} in some plots."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, corpus, corpus
        )
        is False
    )


# --- #617 fold 4 (MEDIUM, E1-inherited, now live): a conjunction heading
# ("## Kenya AA & Friends") must NOT anchor for a bare product name —
# doing so admits a sibling product's data into the wrong region. ---


def test_heading_matches_anchor_conjunction_symbol_no_longer_anchors() -> None:
    """Direct unit coverage: "## Kenya AA & Friends" no longer anchors for
    "Kenya AA" — the remainder "& Friends" names a compound section."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Kenya AA & Friends", ["kenya aa"]) is False


@pytest.mark.parametrize("conjunction", ["and", "with", "plus"])
def test_heading_matches_anchor_conjunction_word_no_longer_anchors(conjunction: str) -> None:
    """Direct unit coverage: the standalone conjunction WORDS "and"/
    "with"/"plus" in the remainder are treated the same as "&"/"+"."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor(f"Kenya AA {conjunction} Friends", ["kenya aa"]) is False


def test_heading_matches_anchor_still_anchors_with_no_conjunction_in_remainder() -> None:
    """The E1b regression this fold must NOT break: "## Kenya Kiambu —
    Single Origin" still anchors for "Kenya Kiambu" — its remainder
    ("single origin", the em dash already punctuation-translated to a
    space before tokenizing) carries no conjunction at all."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Kenya Kiambu — Single Origin", ["kenya kiambu"]) is True


def test_heading_matches_anchor_skips_a_blank_anchor_in_the_list() -> None:
    """Direct unit coverage: an empty-string anchor in ``anchors_normalized``
    (never produced by :func:`bean_sourcing._main_product_region`'s own
    filtered construction, but defended against here) is skipped rather
    than raising or matching everything."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Kenya AA & Friends", ["", "kenya aa"]) is False


def test_draft_from_identity_altitude_conjunction_heading_sibling_demotes() -> None:
    """End-to-end repro (#617 fold 4): a page anchored on "Kenya AA" has a
    "## Kenya AA & Friends" section describing a SIBLING lot's altitude
    ("1,900 masl") — the conjunction heading no longer anchors at all, so
    that section falls OUTSIDE the main region and the citation demotes."""
    corpus = _framed(
        "Kenya AA",
        "## Kenya AA & Friends\n"
        "Our sister lot, the Ethiopia Yirgacheffe, grows at 1,900 masl "
        "and is also available.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1900, altitude_m_evidence="grows at 1,900 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya-aa", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #617 fold 4-FIX-1 (BLOCKER, second review round): a COMMA-compound
# heading ("## Kenya AA, Sumatra Mandailing") must not anchor either —
# the comma is erased by normalization before the word/symbol conjunction
# check ever runs, so it is checked separately on the RAW heading text. ---


def test_heading_matches_anchor_comma_compound_no_longer_anchors() -> None:
    """Direct unit coverage: "## Kenya AA, Sumatra Mandailing" no longer
    anchors for "Kenya AA" — the comma in the raw remainder marks a
    compound heading, same disposition as "&"/"and"/"with"/"plus"."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Kenya AA, Sumatra Mandailing", ["kenya aa"]) is False


def test_heading_matches_anchor_still_anchors_with_no_comma_in_remainder() -> None:
    """The E1b regression this fix must NOT break: "## Kenya Kiambu —
    Single Origin" still anchors — its remainder has no comma (the em
    dash is a different character, already punctuation-translated away
    before the word-level conjunction check, and never reaches the raw
    comma check at all since "," is not "—")."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Kenya Kiambu — Single Origin", ["kenya kiambu"]) is True


def test_heading_matches_anchor_is_linear_not_quadratic_on_a_crafted_heading() -> None:
    """Perf regression (Codex PR #626): the OLD implementation
    materialized a fresh remainder (list slice + string slice) and
    re-scanned it for EVERY anchor occurrence — O(n) work per
    occurrence, O(n²) total on a heading engineered to produce O(n)
    occurrences, a synchronous event-loop stall held behind
    ``draft_bean_from_url``'s start-lock. A ~19,900-char heading of
    repeated "a," (each "a" its own occurrence of the 1-character
    anchor "a") must complete in comfortably sub-second time — the
    bound is loose (well under the O(n²) case's expected multi-second
    blowup) to absorb CI jitter, not to pin an exact budget."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    heading = "a," * 9_950  # ~19,900 chars
    start = time.monotonic()
    result = matches_anchor(heading, ["a"])
    elapsed = time.monotonic() - start
    assert result is False  # every "a" is comma-adjacent — a compound heading throughout
    assert elapsed < 1.0


def test_heading_matches_anchor_kmp_worst_case_anchor_is_linear_not_quadratic() -> None:
    """Perf regression (Codex PR #626 round 3): the OLD
    :func:`bean_sourcing._phrase_token_spans` materialized and compared a
    fresh anchor-width tuple slice at EVERY heading token position — an
    anchor sharing almost all of its tokens with the heading, but never
    actually matching in full, is the classic quadratic-naive-matching
    worst case: a 501-token anchor ("a " * 500 + "b") against a
    9,000-token heading of nothing but "a" would make a naive scan do
    ~500 comparisons at ALMOST EVERY position (each failing only on the
    501st token, since the heading never contains "b" at all) — up to
    ~4.5M token comparisons. Token-sequence KMP resolves the whole scan
    in O(anchor_width + heading_length) — comfortably sub-second."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    anchor = "a " * 500 + "b"
    heading = "a " * 9_000
    start = time.monotonic()
    result = matches_anchor(heading, [anchor])
    elapsed = time.monotonic() - start
    assert result is False  # the heading never contains the anchor's trailing "b"
    assert elapsed < 1.0


# --- #617 D2d, Codex PR #626 round 2 (BLOCKER): casefold() can EXPAND a
# character into MORE codepoints than it started with (German "ß" -> "ss",
# Turkish "İ" -> "i" + a combining dot), so a raw span computed from a
# pre-casefolded copy silently desyncs from the real text's own length —
# an ordinary non-English heading could raise IndexError out of the
# fixed-size prefix array, aborting the whole draft. Fixed by computing
# every span from the punctuation-translated (never casefolded) raw
# text; casefold is applied per-token, strictly after its span is fixed. ---


def test_heading_matches_anchor_german_sharp_s_expansion_no_exception() -> None:
    """ "Große Kaffee" ("ß" casefolds to "ss", expanding the string) must
    not raise — and, with the anchor normalized the SAME way the real
    caller would (:func:`bean_sourcing._normalize_for_containment`), it
    correctly anchors."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    anchor = bean_sourcing._normalize_for_containment(  # pyright: ignore[reportPrivateUsage]
        "Große Kaffee"
    )
    assert matches_anchor("Große Kaffee", [anchor]) is True


def test_heading_matches_anchor_german_sharp_s_expansion_non_match_no_exception() -> None:
    """The same expanding heading against an UNRELATED anchor must not
    raise either, and correctly does not anchor."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("Große Kaffee", ["kenya aa"]) is False


def test_heading_matches_anchor_turkish_dotted_capital_expansion_no_exception() -> None:
    """ "İ Kenya" (Turkish dotted capital I, U+0130, casefolds to "i" plus a
    COMBINING DOT ABOVE — two codepoints from one) must not raise."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    assert matches_anchor("İ Kenya", ["kenya"]) is True


def test_heading_matches_anchor_expanding_fold_inside_a_long_comma_heading_no_indexerror() -> None:
    """A length-expanding fold ("ß") sitting INSIDE a long, comma-heavy
    heading — exercising the marker prefix array right at its own
    boundary, where a desynced span would most easily read or write past
    ``len(heading_text)`` — must not raise ``IndexError``."""
    matches_anchor = bean_sourcing._heading_matches_anchor  # pyright: ignore[reportPrivateUsage]
    heading = "Kenya AA, " * 200 + "Große Kaffee, " * 5 + "Sumatra Mandailing"
    assert matches_anchor(heading, ["kenya aa"]) is False  # comma-compound throughout


def test_draft_from_identity_heading_with_german_sharp_s_no_error() -> None:
    """End-to-end: a page whose product section heading contains "ß" must
    draft cleanly — :func:`_main_product_region` (and the
    :func:`_heading_matches_anchor` it calls) must not raise while
    computing the region, whatever the anchor outcome."""
    corpus = _framed(
        "Große Kaffee",
        "## Große Kaffee\nA rich, full-bodied lot from the highlands.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/grosse-kaffee", corpus=corpus
    )
    assert draft.name


def test_draft_from_identity_altitude_comma_compound_heading_sibling_demotes() -> None:
    """End-to-end repro (#617 fold 4-FIX-1): a page anchored on "Kenya AA"
    has a "## Kenya AA, Sumatra Mandailing" section naming a SIBLING lot's
    altitude ("1,200 masl") — the comma-compound heading no longer
    anchors, so that section falls OUTSIDE the main region and the
    citation demotes."""
    corpus = _framed(
        "Kenya AA",
        "## Kenya AA, Sumatra Mandailing\n"
        "Our Sumatra Mandailing lot grows at 1,200 masl and is also available.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1200, altitude_m_evidence="grows at 1,200 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya-aa", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #617 fold 4-FIX-2 (BLOCKER, second review round): the quote-span
# margin is shrunk to 2 chars AND the gap it allows must be clause-clean
# (no comma/semicolon/colon/dash) — the margin absorbs trimmed
# punctuation, never bridges to an unrelated clause. ---


def test_quote_supports_altitude_short_comma_gap_to_an_unrelated_clause_demotes() -> None:
    """Reviewer repro: "This farm has a lovely tasting room, 1800masl of
    area" — the quote names only "a lovely tasting room", a mere 2
    characters (", ") from the shape. The OLD 10-char margin let this
    certify; the NEW margin is itself only 2 chars, but the comma inside
    that gap now rejects it regardless of length."""
    corpus = "This farm has a lovely tasting room, 1800masl of area."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "a lovely tasting room", corpus
        )
        is False
    )


def test_quote_supports_altitude_shape_flush_at_end_of_quote_verifies() -> None:
    """A quote ending flush at the shape ("Grown at 1,800 masl" cited in
    full, the shape sitting entirely within the quote's own span) still
    verifies — a genuine overlap, no gap involved at all."""
    corpus = "Grown at 1,800 masl in the highlands."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Grown at 1,800 masl", corpus
        )
        is True
    )


def test_quote_supports_altitude_quote_with_trailing_period_still_verifies() -> None:
    """A quote ending at "...masl." with a trailing period still verifies
    — the period tokenizes away, so the quote's own located span ends
    flush with the shape (a genuine overlap), the trailing punctuation
    itself never entering the gap calculation at all."""
    corpus = "Grown at 1,800 masl. Roasted fresh weekly."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Grown at 1,800 masl.", corpus
        )
        is True
    )


# --- #617 fold 4-FIX-3 (MEDIUM, second review round): drop "sea" and
# "growing" from the generic-unit context-word set — both admitted
# non-altitude business copy. ---


def test_quote_supports_altitude_growing_business_copy_demotes() -> None:
    """Reviewer repro: "growing business with 1,800 metres of shelving" —
    the present-participle "growing" used to satisfy the context-word
    requirement for ordinary business copy with no altitude meaning at
    all."""
    corpus = "Our growing business with 1,800 metres of shelving needs more space."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, corpus, corpus
        )
        is False
    )


def test_quote_supports_altitude_distance_from_the_sea_demotes() -> None:
    """Reviewer repro: "1,800 metres from the sea" is a distance-from-the-
    coast reading, not an altitude — "sea" alone used to satisfy the
    context-word requirement; the accepted "above sea level" phrase is a
    SEPARATE, already self-sufficient code path and never needs this
    set."""
    corpus = "This warehouse sits 1,800 metres from the sea on the coast road."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, corpus, corpus
        )
        is False
    )


def test_quote_supports_altitude_grown_at_metres_still_verifies_after_context_tightening() -> None:
    """The positive control for fix 3: "grown at 1,850 metres" still
    verifies — "grown" (the past participle used in genuine provenance
    statements) stays in the context-word set."""
    corpus = "This lot is grown at 1,850 metres in the highlands."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1850, corpus, corpus
        )
        is True
    )


# --- #616 Codex round-1: known guard-stack bypasses. The guard-stack is
# RETIRED (#617 D2d-a) and REPLACED (#617 D2d-b) — the fail-closed
# whitelist grammar closes every one of these STRUCTURALLY (positive
# recognition of a shape, never enumeration of a bad one). ---


def test_quote_supports_altitude_delimiter_blind_adjacency_is_a_known_bypass() -> None:
    """#616 Codex repro: "Altitude | 1,800 reviews" — the retired
    guard-stack's cue-adjacency check was blind to a field-delimiter ("|")
    sitting between a cue and an unrelated number and WRONGLY certified
    it. Closed STRUCTURALLY by #617 D2d-b: "reviews" is not an accepted
    unit, so no ``NUMBER UNIT`` shape ever matches "1800" here."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Altitude | 1,800 reviews", "This lot is Altitude | 1,800 reviews."
        )
        is False
    )


@pytest.mark.parametrize(
    "evidence_quote",
    ["grown above 1,800 masl", "at least 1,800 masl"],
)
def test_quote_supports_altitude_above_below_at_least_bounds_are_a_known_bypass(
    evidence_quote: str,
) -> None:
    """#616 Codex repro: bound phrasing ("above"/"at least") the retired
    guard-stack's fold-5 qualifier set didn't cover, so it WRONGLY
    certified a bound as a scalar. Closed STRUCTURALLY by #617 D2d-b's
    PRE-qualifier context guard
    (:data:`bean_sourcing._ALTITUDE_PRE_BOUND_QUALIFIER_WORDS`), which
    covers both "above" and "least" (for "at least")."""
    corpus = f"This lot is {evidence_quote}."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, evidence_quote, corpus
        )
        is False
    )


def test_quote_supports_altitude_un_isolated_plain_digit_is_a_known_bypass() -> None:
    """#616 Codex repro: "product SKU-1800 masl" — the retired
    guard-stack's isolation guard only ran on the glued-m shortcut path, so
    a plain digit token hyphen-glued to an identifier, immediately
    adjacent to a genuine "masl" cue, was never isolation-checked and
    WRONGLY certified. Closed STRUCTURALLY by #617 D2d-b: the leading
    word-boundary check applies to EVERY NUMBER match unconditionally
    (:func:`bean_sourcing._altitude_number_at`) — the digits are preceded
    by a hyphen, so no NUMBER match can even start there."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "product SKU-1800 masl", "This lot is product SKU-1800 masl."
        )
        is False
    )


def test_quote_supports_altitude_quote_scoped_unit_check_is_a_known_bypass() -> None:
    """#616 Codex repro: segment "Elevation: 1,800 ft" cropped to quote
    "Elevation: 1,800" — the retired guard-stack's non-metre-unit check ran
    on the QUOTE, not the authenticated segment, so cropping the unit out
    of the quote defeated it even though the full segment states feet, and
    it WRONGLY certified. Closed STRUCTURALLY by #617 D2d-b: the grammar
    scans the RAW AUTHENTIC SEGMENT (never the cropped quote) for a
    shape, and "ft" is not an accepted unit, so no shape matches "1800"
    here at all."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Elevation: 1,800", "Elevation: 1,800 ft"
        )
        is False
    )


def test_quote_supports_altitude_unit_mediated_range_is_a_known_bypass() -> None:
    """#616 Codex repro: "1,600 masl to 1,800 masl" — each endpoint has its
    OWN unit token between it and "to", so the retired guard-stack's
    adjacent-to-"to" range check never fired and it WRONGLY certified the
    upper bound as a scalar. Closed STRUCTURALLY by #617 D2d-b: the
    context guard scans OUTWARD past the unit word for a range joiner
    followed by a far digit run
    (:func:`bean_sourcing._altitude_context_guard_rejects`), which is
    exactly this unit-mediated shape."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1,600 masl to 1,800 masl", "This lot is 1,600 masl to 1,800 masl."
        )
        is False
    )


def test_quote_supports_altitude_decimal_split_segment_is_a_known_bypass() -> None:
    """#616 Codex repro: segment "Elevation: 1,800.5 ft" with quote
    "Elevation: 1,800" — the retired guard-stack's segmenter treated the
    non-thousands decimal point as a sentence boundary, splitting the unit
    into a DIFFERENT segment than the one the quote authenticated against,
    and it WRONGLY certified. Closed STRUCTURALLY by #617 D2d-b: the
    grammar requires a UNIT immediately after the parsed NUMBER (glued or
    one space away); here the NUMBER "1800" is followed by "." with no
    unit at all, so no shape matches — the decimal-split segmentation
    quirk no longer matters."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "Elevation: 1,800", "Elevation: 1,800.5 ft"
        )
        is False
    )


def test_draft_from_identity_absent_altitude_with_stray_evidence_quote_has_no_entry() -> None:
    """An absent value (``altitude_m=None``) must never spuriously verify
    even if a (malformed-shape) evidence quote is somehow present — the
    ``raw_value in (None, "")`` skip in the field loop runs BEFORE the
    citation gate is ever consulted."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=None, altitude_m_evidence="grown at 1800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} grown at 1800 masl.",
    )
    assert draft.altitude_m is None
    assert "altitude_m" not in draft.field_sources
    # #627/#633: field_evidence is built from the raw ``*_evidence`` values
    # alone (skip None/blank; strip; then authenticity-check against the
    # page, #633) — independent of whether the corresponding typed VALUE
    # is present. A deliberate divergence from field_sources' "raw_value in
    # (None, '')" skip: a stray-but-GENUINE quote the model captured still
    # surfaces for operator review even against an otherwise-absent field,
    # since the point is showing the operator what the model actually cited
    # from the page, not re-deriving provenance. It authenticates here
    # because the corpus above deliberately carries "grown at 1800 masl"
    # verbatim, on its own segment.
    assert draft.field_evidence["altitude_m"] == "grown at 1800 masl"


@pytest.mark.parametrize("evidence_quote", ["", "   ", None])
def test_quote_supports_altitude_fails_soft_on_malformed_quote(
    evidence_quote: str | None,
) -> None:
    """A blank/whitespace-only/``None`` evidence quote must demote, never
    raise (fail-soft is mandatory over untrusted model output)."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, evidence_quote, "grown at 1800 masl"
        )
        is False
    )


def test_quote_supports_altitude_returns_false_for_none_value() -> None:
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            None, "grown at 1800 masl", "grown at 1800 masl"
        )
        is False
    )


def test_quote_supports_altitude_fabricated_quote_demotes() -> None:
    """A quote that genuinely WOULD support the value if it were real page
    text, but is never actually on the page (a fabricated citation), must
    still demote — the authentic-single-segment-span check is the first
    gate, exercised here with a region that never mentions 1800/masl at
    all."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "grown at 1,800 masl", _IDENTITY_PAGE_TEXT
        )
        is False
    )


def test_elides_as_thousands_separator_requires_exactly_three_digits() -> None:
    elides = bean_sourcing._elides_as_thousands_separator  # pyright: ignore[reportPrivateUsage]
    assert elides("1,800", 1) is True
    assert elides("18,00", 2) is False  # only 2 digits follow
    assert elides("1,8a0", 1) is False  # non-digit within the next 3
    assert elides("1,8000", 1) is False  # 4 digits follow, not exactly 3
    assert elides("1,80", 1) is False  # fewer than 3 chars remain


def test_elides_as_thousands_separator_requires_a_clean_digit_run_start() -> None:
    """#590 D2c fold 3 (security-reviewer BLOCKER): a version-style string
    like "v1.800" must NOT elide — the digit run before the separator has
    to start at a clean word boundary, or a glued letter prefix lets a
    version number masquerade as a thousands-grouped one."""
    elides = bean_sourcing._elides_as_thousands_separator  # pyright: ignore[reportPrivateUsage]
    assert elides("v1.800", 2) is False  # glued letter prefix
    assert elides(" 1.800", 2) is True  # whitespace before the run
    assert elides("1.800", 1) is True  # start of text before the run
    assert elides("v121.800", 4) is False  # a multi-digit run, still glued


def test_split_corpus_segments_drops_boundaries_and_skips_consecutive_ones() -> None:
    """Consecutive boundary characters (e.g. ``". "`` then another
    boundary) must not produce a spurious empty segment — exercises the
    "boundary hit with an empty accumulator" no-op branch."""
    split_segments = bean_sourcing._split_corpus_segments  # pyright: ignore[reportPrivateUsage]
    assert split_segments("One. Two.. Three") == ["One", " Two", " Three"]
    assert split_segments("No boundary at all") == ["No boundary at all"]
    assert split_segments("Ends with a boundary.") == ["Ends with a boundary"]


def test_split_corpus_segments_keeps_a_thousands_period_within_one_segment() -> None:
    """#590 D2c fold 4 (SX60a): a period that satisfies
    :func:`bean_sourcing._elides_as_thousands_separator` (digit before,
    exactly 3 digits after) must NOT split the segment — else
    "grown at 1.850m" could never form a single-segment authentic span,
    since :func:`bean_sourcing._proximity_tokens` elides that same period."""
    split_segments = bean_sourcing._split_corpus_segments  # pyright: ignore[reportPrivateUsage]
    assert split_segments("This farm is grown at 1.850m above the valley.") == [
        "This farm is grown at 1.850m above the valley"
    ]


def test_split_corpus_segments_still_splits_a_genuine_decimal_or_sentence_end() -> None:
    """The safe-direction mirror of fold 4: a genuine 2-digit decimal
    (only 2 digits follow, not exactly 3) and an ordinary sentence-ending
    period (a letter follows, not 3 digits) must both still split
    normally — the exception is narrowly scoped to the thousands-grouping
    shape only."""
    split_segments = bean_sourcing._split_corpus_segments  # pyright: ignore[reportPrivateUsage]
    assert split_segments("This costs $1.00. It grows at 1850m.") == [
        "This costs $1",
        "00",
        " It grows at 1850m",
    ]
    assert split_segments("The lot is 1800. Elevation is high.") == [
        "The lot is 1800",
        " Elevation is high",
    ]


def test_find_authentic_segment_blank_quote_returns_none() -> None:
    """A quote that normalizes to blank (all punctuation, no words) must
    fail soft to ``None`` rather than vacuously matching."""
    find_segment = bean_sourcing._find_authentic_segment  # pyright: ignore[reportPrivateUsage]
    assert find_segment("...", "Some real page text.") is None


# --- _draft_from_identity: honest imputation + conservative targets ---


def test_draft_from_identity_marks_page_fields_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert isinstance(draft, BeanProfileDraft)
    for field in (
        "name",
        "country",
        "bean_origin",
        "farm",
        "bean_varietal",
        "description",
    ):
        assert draft.field_sources[field] == "on_page", field
    # processing/altitude_m are TYPED fields verified via the D2b citation
    # gate — with no evidence quote supplied here (this fixture's default),
    # they demote even though the page genuinely states "washed"/1775m and
    # the model returned them correctly; see the dedicated
    # _with_no_evidence_quote_demotes tests for the individual coverage,
    # and the "--- #590 D2b" section below for the flipped-when-verified
    # cases.
    assert draft.field_sources["processing"] == "origin_estimated"
    assert draft.field_sources["altitude_m"] == "origin_estimated"
    # bean_species was NOT stated on the page (None in the fixture) — no
    # fabricated value, and no field_sources entry claiming it is on_page.
    assert draft.bean_species is None
    assert "bean_species" not in draft.field_sources
    # is_blend defaults to None (unstated) in the fixture — the page never
    # SAID anything about blending, so no provenance should be recorded for
    # it either (#587 P2: absent-from-field_sources must mean "unset", and
    # None is now the ONLY value that means that — see the dedicated
    # is_blend tri-state tests below for the explicit True/False cases).
    assert draft.is_blend is None
    assert "is_blend" not in draft.field_sources


# --- #590 D1: code-verified on_page via value containment ---


def test_draft_from_identity_confabulated_farm_is_demoted_to_origin_estimated() -> None:
    """A field value the model returned but the corpus never actually
    states (a confabulation) must be demoted to ``"origin_estimated"``,
    not blanket-trusted just because the model claimed it (#590 D1 — the
    core gap this slice closes)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(farm="Finca El Injerto")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.farm == "Finca El Injerto"
    assert draft.field_sources["farm"] == "origin_estimated"


def test_draft_from_identity_confabulated_bean_varietal_is_demoted() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_varietal="Geisha")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.bean_varietal == "Geisha"
    assert draft.field_sources["bean_varietal"] == "origin_estimated"


def test_draft_from_identity_bean_varietal_matches_slash_separated_page_form() -> None:
    """#590 D1 fold 3 (round-3 Codex P2): the punctuation-normalisation
    set only mapped ``,.'"-()`` to spaces, so a page rendering
    "SL28/SL34" (slash, no comma) failed to match a genuine
    ``bean_varietal="SL28, SL34"`` value and over-demoted it. The
    broadened set (``/\\:;!?[]{}<>|`` + en/em dash + curly quotes) must
    verify it."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_varietal="SL28 SL34")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Variety: SL28/SL34, grown at high altitude.",
    )
    assert draft.bean_varietal == "SL28 SL34"
    assert draft.field_sources["bean_varietal"] == "on_page"


def test_draft_from_identity_description_stays_exempt_even_when_paraphrased() -> None:
    """``description`` is EXEMPT from the containment gate (#590 D1) — the
    model may legitimately summarize/paraphrase the page's prose rather
    than quote it verbatim, so it stays ``"on_page"`` on presence alone
    even though this paraphrase is not a literal substring of the corpus."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(description="A fruity, bright Kenyan coffee with berry notes.")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.description == "A fruity, bright Kenyan coffee with berry notes."
    assert draft.field_sources["description"] == "on_page"


def test_draft_from_identity_processing_with_no_evidence_quote_demotes() -> None:
    """``processing`` is a closed-vocabulary enum whose values are common
    English words that collide with unrelated page prose, so D1 demoted
    it UNCONDITIONALLY. #590 slice E2 now verifies it via
    :func:`bean_sourcing._quote_supports_processing` — but with NO
    ``processing_evidence`` quote supplied, the gate still demotes
    (condition 1), even when the page GENUINELY states "washed" and the
    model correctly returned the value."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing="washed")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This lot was fully washed and sun-dried on raised beds.",
    )
    assert draft.processing == "washed"
    assert draft.field_sources["processing"] == "origin_estimated"


def test_draft_from_identity_processing_honey_collision_is_demoted() -> None:
    """The exact collision repro: ``processing="honey"`` (the process)
    trivially word-matches an unrelated TASTING-NOTE mention of "honey"
    (the flavor) — crude single-word containment can't tell those apart,
    so D1 never even tried it. #590 slice E2's gate demotes it too, but
    for a SOUND reason this time: no ``processing_evidence`` quote is
    supplied, so the gate never reaches the process-word cue check that
    would in any case reject "honey" in a bare tasting-note sentence (see
    the dedicated E2 cue tests below)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing="honey")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Tasting notes: honey, stone fruit, and a clean, sweet finish.",
    )
    assert draft.processing == "honey"
    assert draft.field_sources["processing"] == "origin_estimated"


def test_draft_from_identity_bean_species_is_always_demoted_in_d1() -> None:
    """Same D1 scoping fold as processing, for the other enum field: with
    no ``bean_species_evidence`` quote supplied, #590 slice E2's gate
    still demotes even when the value is genuinely stated on the page."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_species="arabica")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="100% arabica beans, hand-picked at peak ripeness.",
    )
    assert draft.bean_species == "arabica"
    assert draft.field_sources["bean_species"] == "origin_estimated"


def _containment_corpus(raw: str) -> str:
    """Build the normalized corpus form :func:`bean_sourcing._value_is_contained`
    takes, exactly as :func:`bean_sourcing._draft_from_identity` computes it
    once per draft (#590 D1) — a shared test helper."""
    return bean_sourcing._normalize_for_containment(raw)  # pyright: ignore[reportPrivateUsage]


def test_value_is_contained_none_and_empty_are_never_contained() -> None:
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus(_IDENTITY_PAGE_TEXT)
    assert is_contained(None, normalized) is False
    assert is_contained("", normalized) is False
    assert is_contained("   ", normalized) is False


def test_value_is_contained_matches_case_and_punctuation_insensitively() -> None:
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("Farm: Gakuyuini Factory.")
    assert is_contained("gakuyuini factory", normalized) is True
    assert is_contained("GAKUYUINI FACTORY", normalized) is True
    assert is_contained("Nairobi Estate", normalized) is False


def test_contains_whole_phrase_rejects_an_empty_phrase() -> None:
    """A value that normalizes to an EMPTY phrase (e.g. a punctuation-only
    string, since :func:`_normalize_for_containment` maps every
    ``,.'"-()`` to a space) must never be treated as contained."""
    contains_whole_phrase = bean_sourcing._contains_whole_phrase  # pyright: ignore[reportPrivateUsage]
    corpus_normalized = _containment_corpus("A washed lot from Kenya.")
    assert contains_whole_phrase("", corpus_normalized) is False


def test_value_is_contained_rejects_a_punctuation_only_value() -> None:
    """End-to-end: a confabulated field value that is punctuation-only
    (normalizes to an empty phrase) must demote, not raise or false-match."""
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("A washed lot from Kenya.")
    assert is_contained("---", normalized) is False


def test_value_is_contained_rejects_java_matching_inside_javascript() -> None:
    """Plain substring containment let "Java" match inside "JavaScript"
    boilerplate — a confabulated origin verified from unrelated site
    chrome. Whole-word matching must reject it."""
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("Please enable JavaScript in your browser to use this site.")
    assert is_contained("Java", normalized) is False


def test_value_is_contained_rejects_india_matching_inside_indianapolis() -> None:
    """Plain substring containment let "India" match inside "Indianapolis"
    — a confabulated origin verified from an unrelated shipping-address
    mention. Whole-word matching must reject it."""
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("Our roastery has a second location in Indianapolis, IN.")
    assert is_contained("India", normalized) is False


def test_value_is_contained_accepts_a_real_adjacent_multi_word_origin() -> None:
    """A genuine two-word origin stated adjacently on the page verifies."""
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("A washed lot from Yirgacheffe, Ethiopia.")
    assert is_contained("Yirgacheffe Ethiopia", normalized) is True


def test_value_is_contained_accepts_a_real_single_word_origin() -> None:
    """A genuine whole-word origin verifies."""
    is_contained = bean_sourcing._value_is_contained  # pyright: ignore[reportPrivateUsage]
    normalized = _containment_corpus("A bright, fruit-forward lot from Kenya.")
    assert is_contained("Kenya", normalized) is True


def test_draft_from_identity_bean_origin_yirgacheffe_whole_word_is_on_page() -> None:
    """End-to-end free-text verification: a page stating "Yirgacheffe" as
    a whole word with a matching ``bean_origin`` value earns ``on_page``."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_origin="Yirgacheffe", country="Ethiopia")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/yirgacheffe",
        corpus="A washed lot from Yirgacheffe, Ethiopia.",
    )
    assert draft.bean_origin == "Yirgacheffe"
    assert draft.field_sources["bean_origin"] == "on_page"


def test_draft_from_identity_country_java_from_javascript_boilerplate_is_demoted() -> None:
    """Bug 2 repro, end-to-end: a confabulated ``country="Java"`` must
    demote when the page only mentions "JavaScript" boilerplate, never
    the country."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(country="Java", bean_origin="Java")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/java",
        corpus="Please enable JavaScript in your browser to use this site.",
    )
    assert draft.country == "Java"
    assert draft.field_sources["country"] == "origin_estimated"


def test_draft_from_identity_country_india_from_indianapolis_is_demoted() -> None:
    """Bug 2 repro, end-to-end: a confabulated ``country="India"`` must
    demote when the page only mentions "Indianapolis"."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(country="India", bean_origin="India")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/india",
        corpus="Our roastery has a second location in Indianapolis, IN.",
    )
    assert draft.country == "India"
    assert draft.field_sources["country"] == "origin_estimated"


# --- #587 P2 round 6: altitude range must not be tagged on_page ---


def test_extraction_instructions_tell_the_model_not_to_compute_a_midpoint() -> None:
    """#587 P2 round 6: the prompt used to instruct the model to compute a
    RANGE's midpoint for ``altitude_m``, which then got tagged
    ``"on_page"`` for a value the page never actually stated as a single
    number. The model must now be told to leave it null for a range."""
    instructions = bean_sourcing._EXTRACTION_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]
    assert "midpoint" in instructions.lower()
    assert "do not compute" in instructions.lower() or "not compute" in instructions.lower()


def test_draft_from_identity_altitude_range_page_leaves_altitude_null_and_unset() -> None:
    """#587 P2 round 6: given the corrected prompt, a page that stated an
    altitude RANGE extracts as ``altitude_m=None`` (the model's job, not
    exercised here — this test proves the DOWNSTREAM provenance handling is
    honest given that null) — no ``on_page`` tag for a value that was never
    actually a stated scalar."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=None)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.altitude_m is None
    assert "altitude_m" not in draft.field_sources


def test_draft_from_identity_altitude_with_no_evidence_quote_demotes() -> None:
    """``altitude_m`` is a TYPED field D1 demoted UNCONDITIONALLY — pure
    string containment can't safely verify a number against arbitrary
    page-numeral noise (a price, a SKU, a year). #590 D2b now verifies it
    via the citation gate (:func:`bean_sourcing._quote_supports_value`) —
    but with NO ``altitude_m_evidence`` quote supplied, the gate still
    demotes, even when the page GENUINELY states "Altitude: 1850 m" and
    the model correctly returned it (see
    :data:`bean_sourcing._TYPED_CITATION_FIELDS`)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1850)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} Altitude: 1850 m.",
    )
    assert draft.altitude_m == 1850
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #587 P2: normalize optional identity values before tagging provenance ---


def test_draft_from_identity_whitespace_only_country_not_tagged_on_page() -> None:
    """#587 P2: a whitespace-only ``country`` is accepted by
    ``_ExtractedBeanIdentity`` but normalizes to ``None`` on
    ``BeanProfileDraft`` (the base model's ``_strip_optional_identity``
    validator) — the raw-value provenance check must not tag it
    ``"on_page"`` for a value that becomes ``None`` (a provenance lie)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(country="   ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.country is None
    assert "country" not in draft.field_sources


def test_draft_from_identity_whitespace_only_farm_not_tagged_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(farm="   ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.farm is None
    assert "farm" not in draft.field_sources


def test_draft_from_identity_whitespace_only_description_not_tagged_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(description="   ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.description is None
    assert "description" not in draft.field_sources


def test_draft_from_identity_whitespace_only_bean_varietal_does_not_reject_draft() -> None:
    """#587 P2 (the acute case): ``BeanProfileDraft.bean_varietal`` runs the
    STRICTER base-model validator (``_strip_and_require_content``), which
    RAISES on a whitespace-only value rather than normalizing it — passed
    through un-normalized, a whitespace-only page extraction would reject
    the WHOLE draft (``BeanExtractionError``) instead of just being treated
    as unstated. Pre-normalizing to ``None`` before construction avoids
    that entirely."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_varietal="   ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.bean_varietal is None
    assert "bean_varietal" not in draft.field_sources


def test_draft_from_identity_whitespace_only_bean_origin_falls_back_to_country() -> None:
    """#587 P2 round 5: a whitespace-only ``bean_origin`` is TRUTHY, so an
    un-normalized ``identity.bean_origin or country`` fallback chain would
    let it WIN over a perfectly good page-sourced ``country``, then strip
    to empty and wrongly reject the whole draft as having no usable
    origin. Normalizing ``bean_origin`` BEFORE the fallback fixes this."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_origin="   ", country="Ethiopia")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/eth",
        corpus="Ethiopia Yirgacheffe, a natural process lot.",
    )
    assert draft.bean_origin == "Ethiopia"
    assert draft.field_sources["bean_origin"] == "on_page"


def test_draft_from_identity_whitespace_padded_values_are_stripped_and_still_on_page() -> None:
    """Whitespace-PADDED (not whitespace-ONLY) optional text must still be
    stripped and tagged on_page — normalization must not turn a real value
    into "unstated"."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(country="  Kenya  ", farm="  Gakuyuini Factory  ")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.country == "Kenya"
    assert draft.field_sources["country"] == "on_page"
    assert draft.farm == "Gakuyuini Factory"
    assert draft.field_sources["farm"] == "on_page"


def test_normalize_optional_text_strips_and_blanks_to_none() -> None:
    normalize = bean_sourcing._normalize_optional_text  # pyright: ignore[reportPrivateUsage]
    assert normalize(None) is None
    assert normalize("") is None
    assert normalize("   ") is None
    assert normalize("  Kenya  ") == "Kenya"


def test_extraction_instructions_require_explicit_blend_evidence() -> None:
    """#587 P2 round 7: the prompt used to say a named single farm/region/
    country IS itself a single-origin statement — meaning a page that just
    named ONE origin (never actually addressing blend-vs-single-origin)
    got an INVENTED explicit ``false`` instead of the honest ``null``. The
    model must now be told to leave it null unless the page EXPLICITLY
    addresses blending either way."""
    instructions = bean_sourcing._EXTRACTION_INSTRUCTIONS  # pyright: ignore[reportPrivateUsage]
    lowered = instructions.lower()
    assert "explicitly" in lowered
    assert "not itself" in lowered or "not an explicit" in lowered or "is not itself" in lowered


def test_draft_from_identity_is_blend_silent_page_leaves_unset() -> None:
    """#587 P2: the page saying NOTHING about blend-vs-single-origin must
    leave ``is_blend`` unset (``None``) with no ``field_sources`` entry —
    distinct from an explicit ``False``."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=None)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/silent", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.is_blend is None
    assert "is_blend" not in draft.field_sources


def test_draft_from_identity_is_blend_true_with_no_evidence_quote_demotes() -> None:
    """#590 slice E1b: ``is_blend=True`` with NO evidence quote
    (``is_blend_evidence`` left unset) always demotes —
    :func:`bean_sourcing._quote_supports_is_blend` requires a quote to
    even begin authenticating, so token presence elsewhere on the page is
    never enough on its own (a single-origin product page can still
    contain the word "blend" via an unrelated "shop our house blend"
    cross-sell link; the quote+locality gate is what makes verification
    sound, not bare presence)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/blend",
        corpus="Our house blend combines beans from three origins.",
    )
    assert draft.is_blend is True
    assert draft.field_sources["is_blend"] == "origin_estimated"


def test_draft_from_identity_is_blend_false_with_no_evidence_quote_demotes() -> None:
    """The mirror case: ``is_blend=False`` with no evidence quote demotes
    too, even when the page genuinely says "single origin"."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=False)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/single-origin",
        corpus="This is a single origin lot from one estate.",
    )
    assert draft.is_blend is False
    assert draft.field_sources["is_blend"] == "origin_estimated"


# --- #590 slice E1: fail-closed main-product-region locality (machinery
# only, capture-only posture — no field's provenance consumes this yet;
# a future slice wires is_blend through it) ---


def _framed(title: str, body: str) -> str:
    """Build a body_text carrying the exact trafilatura-frontmatter shape
    _main_product_region expects (a leading title-only frontmatter block,
    mirroring _sanitize_trafilatura_frontmatter's emitted shape)."""
    return f"---\ntitle: {title}\n---\n{body}"


def test_main_product_region_a1_only_lead_region_no_json_ld() -> None:
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        _framed("Kenya Kiambu AA", "Great coffee here.\n"), "", ""
    )
    assert region == "Kenya Kiambu AA\nGreat coffee here."


def test_main_product_region_a2_only_no_title_no_heading() -> None:
    """No frontmatter at all (the linear-strip shape) and no heading to
    anchor against — the region collapses to the JSON-LD value alone,
    never the lead text (no A1, no lead region)."""
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        "Some plain linear-strip text with no frontmatter at all.",
        "Kenya Kiambu AA",
        "Kenya Kiambu AA",
    )
    assert region == "Kenya Kiambu AA"


def test_main_product_region_neither_anchor_collapses_to_empty() -> None:
    """The fail-closed law, directly on the pure function: with NEITHER
    anchor, the region is "" — never the whole corpus."""
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        "Some plain linear-strip text with nothing else on the page.", "", ""
    )
    assert region == ""


def test_main_product_region_both_anchors_headings_multiple_levels() -> None:
    body = _framed(
        "Kenya Kiambu AA",
        "Intro line before any heading.\n"
        "# Kenya Kiambu AA\n"
        "Top-level anchored content.\n"
        "## Kenya Kiambu AA Details\n"
        "Nested anchored content too.\n"
        "### Unrelated Subsection\n"
        "Excluded content under an unmatched sub-heading.\n",
    )
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        body, "Kenya Kiambu AA", "Kenya Kiambu AA"
    )
    assert "Intro line before any heading." in region
    assert "Top-level anchored content." in region
    assert "Nested anchored content too." in region
    assert "Excluded content under an unmatched sub-heading." not in region
    assert region.endswith("Kenya Kiambu AA")  # the JSON-LD value appended last


def test_main_product_region_sentinel_paragraph_truncates_lead_region() -> None:
    """A sentinel need not be a heading — a plain cross-sell PARAGRAPH
    truncates the lead region just the same."""
    body = _framed(
        "Kenya Kiambu AA",
        "This is a single origin lot from Kiambu.\n"
        "\n"
        "You may also like our house blend for an everyday cup.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert "single origin" in region
    assert "house blend" not in region.lower()


def test_main_product_region_anchored_heading_closes_at_next_heading() -> None:
    body = _framed(
        "Kenya Kiambu AA",
        "## Kenya Kiambu AA\n"
        "This is our house blend of three lots.\n"
        "## Shipping Information\n"
        "This shop also offers a house blend gift box.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert "house blend of three lots" in region
    assert "gift box" not in region


def test_main_product_region_two_consecutive_anchored_headings_each_get_own_region() -> None:
    """Two anchor-matching headings back-to-back, with no content line
    between them: each still opens its OWN region, seeded with its own
    heading text (#590 Codex round-2 fold, Sa4cg — the heading IS
    positive recognition, so the first heading's region is no longer
    empty; it is the heading text alone). The second heading's region
    correctly picks up the content that follows it."""
    body = _framed(
        "Kenya Kiambu AA",
        "## Kenya Kiambu AA\n## Kenya Kiambu AA\nReal content under the second heading.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert "Real content under the second heading." in region
    assert region.count("Kenya Kiambu AA") >= 2  # title prepend + at least one heading echo


def test_main_product_region_json_ld_name_absent_brand_heading_opens_nothing() -> None:
    """Codex round-1 fold (SaV9L): when the identity-matched JSON-LD
    Product block omits ``name`` but states ``brand``/``sku``,
    ``json_ld_values``'s first line is that brand/SKU, not a product
    name. A2 must come from ``json_ld_name`` alone — with it blank, a
    heading matching the brand text must NOT open a region."""
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        "## Acme\nWelcome to our shop, browse our full range.\n",
        "Acme Roasters",
        "",
    )
    assert region == "Acme Roasters"
    assert "Welcome to our shop" not in region


def test_main_product_region_json_ld_name_present_heading_opens_as_before() -> None:
    """The mirror case: with ``json_ld_name`` genuinely set to the
    product's own name, a matching heading still opens its region exactly
    as before this fold."""
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        "## Kenya Kiambu AA\nReal product content here.\n",
        "Kenya Kiambu AA",
        "Kenya Kiambu AA",
    )
    assert "Real product content here." in region


def test_main_product_region_sentinel_heading_never_opens_even_when_anchor_matches() -> None:
    """Codex round-1 fold (SaV9O): a heading that is BOTH a sentinel and
    an anchor match ("## More from <anchor>") never opens a region — the
    sentinel check runs first, regardless of the anchor match."""
    body = _framed(
        "Kenya Kiambu AA",
        "Great single origin coffee.\n"
        "## More from Kenya Kiambu AA\n"
        "Our house blend for everyday drinking.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert "Our house blend for everyday drinking." not in region


def test_main_product_region_includes_title_text_for_authentication() -> None:
    """Codex round-1 fold (SaV9T): A1's title text is itself part of the
    main region, so a quote of the title alone (the only blend/polarity
    statement on a page whose body is just tasting notes) can still
    authenticate."""
    body = _framed("Morning House Blend", "Notes of cocoa and dried fruit linger sweetly.\n")
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._find_authentic_segment(  # pyright: ignore[reportPrivateUsage]
            "Morning House Blend", region
        )
        is not None
    )


# --- #590 slice E1a, Codex round 2: one-directional anchor match (Sa4cf)
# + matched heading text belongs to its region (Sa4cg) ---


def test_heading_matches_anchor_reverse_direction_no_longer_matches() -> None:
    """Sa4cf: the reverse direction (a longer anchor merely CONTAINING
    the heading) is DROPPED — a generic "Coffee" heading must not match
    just because it's a substring of the longer anchor "Kenya Coffee"
    (that direction would let an unrelated "## Coffee" related-products
    heading open a region on any page whose own name contains "coffee")."""
    matches = bean_sourcing._heading_matches_anchor(  # pyright: ignore[reportPrivateUsage]
        "Coffee", ["kenya coffee"]
    )
    assert matches is False


def test_heading_matches_anchor_forward_direction_still_matches() -> None:
    """The forward direction (the heading CONTAINS the anchor) still
    matches — a heading that extends the anchor with extra qualifying
    text."""
    matches = bean_sourcing._heading_matches_anchor(  # pyright: ignore[reportPrivateUsage]
        "Kenya Kiambu — Single Origin", ["kenya kiambu"]
    )
    assert matches is True


def test_heading_matches_anchor_abbreviation_is_a_documented_over_demote() -> None:
    """Documented over-demote (AC E-2, same pattern as the altitude
    whitelist): a heading that ABBREVIATES a suffix-laden anchor ("Kenya
    Kiambu AA" vs the fuller "Kenya Kiambu AA 250g Whole Bean") no longer
    matches now that the reverse direction is gone — the safe direction
    only; widening is evidence-gated, never assumed."""
    matches = bean_sourcing._heading_matches_anchor(  # pyright: ignore[reportPrivateUsage]
        "Kenya Kiambu AA", ["kenya kiambu aa 250g whole bean"]
    )
    assert matches is False


def test_main_product_region_reverse_match_heading_opens_nothing() -> None:
    """Integration proof for Sa4cf: a generic "## Coffee" heading, with
    JSON-LD name "Kenya Coffee", must not open a region — related-products
    "Coffee" chrome must not become main region just because the page's
    own name happens to contain that word."""
    region = bean_sourcing._main_product_region(  # pyright: ignore[reportPrivateUsage]
        "## Coffee\nShop our full range of house blends.\n", "Kenya Coffee", "Kenya Coffee"
    )
    assert region == "Kenya Coffee"
    assert "Shop our full range" not in region


def test_main_product_region_matched_heading_text_authenticates() -> None:
    """Sa4cg: the matched heading's OWN text belongs to its region — a
    polarity statement written only in the heading itself (never repeated
    in the body) can still authenticate."""
    body = _framed(
        "Kenya Kiambu",
        "## Kenya Kiambu — Single Origin\nNotes of stone fruit and honey.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._find_authentic_segment(  # pyright: ignore[reportPrivateUsage]
            "Kenya Kiambu — Single Origin", region
        )
        is not None
    )


def test_frontmatter_title_and_body_untitled_block_returns_none() -> None:
    """A hand-built, untitled frontmatter block — production code never
    emits this shape (_sanitize_trafilatura_frontmatter drops an untitled
    block entirely rather than leaving it in place), but the pure
    function is exercised against it directly."""
    title, rest = bean_sourcing._frontmatter_title_and_body(  # pyright: ignore[reportPrivateUsage]
        "---\nauthor: Someone\n---\nBody text here."
    )
    assert title is None
    assert rest == "Body text here."


def test_heading_matches_anchor_empty_heading_text_never_matches() -> None:
    """A bare ``"#"`` heading (no text after the hashes) normalizes to
    ``""`` and can never match any anchor."""
    matches = bean_sourcing._heading_matches_anchor(  # pyright: ignore[reportPrivateUsage]
        "", ["kenya kiambu aa"]
    )
    assert matches is False


def test_fetched_page_verification_corpus_matches_pre_split_formula() -> None:
    """#590 slice E1: ``verification_corpus`` stays a derived,
    byte-identical accessor after the ``extracted_text``/``json_ld_values``
    split — both the blank and the non-blank ``json_ld_values`` cases."""
    blank = bean_sourcing._FetchedPage(  # pyright: ignore[reportPrivateUsage]
        prompt_text="prompt", extracted_text="body only", json_ld_values=""
    )
    assert blank.verification_corpus == "body only"
    with_facts = bean_sourcing._FetchedPage(  # pyright: ignore[reportPrivateUsage]
        prompt_text="prompt", extracted_text="body text", json_ld_values="Kenya Kiambu AA"
    )
    assert with_facts.verification_corpus == "body text\nKenya Kiambu AA"


# --- #590 slice E1b: is_blend citation + locality gate ---


def test_quote_supports_is_blend_false_claim_without_false_polarity_phrase_demotes() -> None:
    """An authentic main-region quote that claims ``False`` but never
    actually states a single-origin-family phrase demotes (the quote's
    OWN polarity check, distinct from the region-wide opposite-polarity
    veto)."""
    region = "This coffee has notes of stone fruit and honey."
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False,
            "This coffee has notes of stone fruit and honey.",
            region,
            anchor_names_blend=False,
        )
        is False
    )


def test_quote_supports_is_blend_none_or_blank_quote_demotes() -> None:
    """A missing or whitespace-only evidence quote demotes immediately —
    the gate never even reaches the authenticity check."""
    region = "This is a single origin coffee from Kenya."
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False, None, region, anchor_names_blend=False
        )
        is False
    )
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False, "   ", region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_marquee_decoy_demotes_true_claim() -> None:
    """The marquee decoy: a single-origin page whose cross-sell block says
    "Shop our House Blend" under an unmatched "You May Also Like" heading.
    Citing that line as ``is_blend=True`` evidence demotes — the quote
    fails to authenticate against the MAIN region, since the cross-sell
    block sits behind a heading that never matched the page's own
    anchor."""
    body = _framed(
        "Ethiopia Yirgacheffe Single Origin",
        "This lot is a single origin coffee from Yirgacheffe.\n"
        "\n"
        "## You May Also Like\n"
        "Shop our House Blend for an everyday cup.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence="Shop our House Blend")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"


def test_draft_from_identity_is_blend_marquee_decoy_symmetric_false_claim_stays_dormant() -> None:
    """Symmetric proof, DORMANT-gate form (#590 slice E1b ships dormant —
    :data:`bean_sourcing._IS_BLEND_LOCALITY_GATE_ENABLED`): the SAME page,
    claiming ``False`` and citing the authentic main-region "single
    origin" sentence, demotes at RUNTIME regardless — the helper itself
    still recognizes the citation (see the direct-call assertion below),
    proving the polarity veto is main-region-scoped and not a whole-page
    string search the cross-sell chrome could poison, but that recognition
    is not (yet) wired to ``field_sources``."""
    body = _framed(
        "Ethiopia Yirgacheffe Single Origin",
        "This lot is a single origin coffee from Yirgacheffe.\n"
        "\n"
        "## You May Also Like\n"
        "Shop our House Blend for an everyday cup.\n",
    )
    quote = "This lot is a single origin coffee from Yirgacheffe."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=False, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False, quote, region, anchor_names_blend=False
        )
        is True
    )


def test_draft_from_identity_is_blend_both_polarities_in_region_demotes_true_claim() -> None:
    """Both polarities present in the SAME main region — an ambiguous page
    — demotes a ``True`` claim (both at runtime, gate dormant, AND
    genuinely per the helper: the opposite-polarity veto rejects it even
    though the quote itself carries a valid Tier-2 compound phrase)."""
    body = _framed(
        "Kenya Kiambu",
        "This lot is a single origin coffee, though our house blend uses similar beans.\n",
    )
    quote = "our house blend uses similar beans"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_both_polarities_in_region_demotes_false_claim() -> None:
    """The mirror case: a valid single-origin quote still demotes (both at
    runtime and genuinely per the helper) when the SAME region also
    carries a Tier-2 blend compound."""
    body = _framed(
        "Kenya Kiambu",
        "This lot is a single origin coffee, though our house blend uses similar beans.\n",
    )
    quote = "a single origin coffee"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=False, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_tasting_metaphor_bare_word_demotes() -> None:
    """A tasting-note metaphor ("a blend of chocolate and cherry notes")
    never verifies ``True`` — bare "blend", no compound phrase, and a
    non-blend-named anchor — demotes both at runtime and genuinely per
    the helper."""
    body = _framed(
        "Kenya Kiambu AA",
        "In the cup: a blend of chocolate and cherry notes lingers into the finish.\n",
    )
    quote = "a blend of chocolate and cherry notes lingers into the finish"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_anchor_named_blend_tier1_stays_dormant() -> None:
    """Tier 1 (dormant-gate form): the product's own title names it a
    blend AND the quote carries a bare "blend" — the helper recognizes it
    (:func:`bean_sourcing._quote_supports_is_blend` returns ``True``
    called directly) but the draft still demotes at runtime, since the
    gate is dormant (:data:`bean_sourcing._IS_BLEND_LOCALITY_GATE_ENABLED`)."""
    body = _framed(
        "Autumn Blend",
        "Our Autumn Blend brings together three origins. This is a blend crafted for balance.\n",
    )
    quote = "This is a blend crafted for balance."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/autumn", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=True
        )
        is True
    )


def test_draft_from_identity_is_blend_compound_phrase_tier2_stays_dormant() -> None:
    """Tier 2 (dormant-gate form): a fixed compound phrase is recognized
    by the helper directly, but the draft still demotes at runtime."""
    body = _framed(
        "Kenya Kiambu AA",
        "This is our house blend crafted from three regions for balance.\n",
    )
    quote = "This is our house blend crafted from three regions for balance."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is True
    )


def test_draft_from_identity_is_blend_documented_over_demote_genuine_blend_prose() -> None:
    """Executable spec for a DOCUMENTED over-demote: genuine "blend" prose
    ("This is a blend of two Colombian lots") on a non-blend-named
    product demotes both at runtime and genuinely per the helper, because
    it satisfies neither tier — a future evidence-gated widening of the
    compound-phrase set may flip this specific shape; over-demotes never
    block convergence in the meantime."""
    body = _framed(
        "Kenya Kiambu AA",
        "This is a blend of two Colombian lots sourced for this harvest.\n",
    )
    quote = "This is a blend of two Colombian lots sourced for this harvest."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_no_anchor_single_origin_demotes() -> None:
    """Executable spec: even a perfect on-page "single origin" quote
    demotes when the page carries no anchor at all (no frontmatter title,
    no JSON-LD product name) — the main region collapses to "" and there
    is no whole-corpus fallback, so the helper itself demotes too (an
    empty region yields no authentic segment)."""
    corpus = "This is a single origin coffee, hand-picked at peak ripeness."
    quote = "This is a single origin coffee."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=False, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/no-anchor", corpus=corpus
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(corpus, "", "")  # pyright: ignore[reportPrivateUsage]
    assert region == ""
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            False, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_sentinel_paragraph_blend_never_verifies() -> None:
    body = _framed(
        "Kenya Kiambu AA",
        "This is a single origin lot from Kiambu.\n"
        "\n"
        "You may also like our house blend for an everyday cup.\n",
    )
    quote = "our house blend for an everyday cup"
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is False
    )


def test_draft_from_identity_is_blend_under_anchored_heading_stays_dormant() -> None:
    """Dormant-gate form: the helper recognizes evidence cited from an
    anchored-heading region, but the draft still demotes at runtime."""
    body = _framed(
        "Kenya Kiambu AA",
        "## Kenya Kiambu AA\n"
        "This is our house blend of three lots.\n"
        "## Shipping Information\n"
        "This shop also offers a house blend gift box.\n",
    )
    quote = "This is our house blend of three lots."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is True
    )


def test_draft_from_identity_is_blend_demotes_after_the_next_heading() -> None:
    """The mirror of the previous test: the SAME page's evidence, cited
    from the section AFTER the anchored heading closes, demotes both at
    runtime and genuinely per the helper (the quote is not authentic
    within the region — that section never made it in)."""
    body = _framed(
        "Kenya Kiambu AA",
        "## Kenya Kiambu AA\n"
        "This is our house blend of three lots.\n"
        "## Shipping Information\n"
        "This shop also offers a house blend gift box.\n",
    )
    quote = "This shop also offers a house blend gift box."
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence=quote)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kiambu", corpus=body
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
            True, quote, region, anchor_names_blend=False
        )
        is False
    )


# --- #590 slice E1b: independent adversarial security-reviewer BLOCK —
# SEMANTIC certify-bypass class (negation/composition/collection-
# membership chrome), motivating the permanent dormancy above. These
# three tests are EXECUTABLE SPECS for the reviewer's exact findings:
# they assert the helper's ACTUAL (documented-bypass) behaviour, not the
# desired one — proof the park is warranted, not a claim the gate is
# safe. Empirically verified against the current implementation.


def test_quote_supports_is_blend_negation_composition_bypass_certifies_wrongly() -> None:
    """Reviewer probe 1: ordinary vendor copy openly describing a BLEND
    ("This blend combines two single origin lots from Kenya and Ethiopia
    for a bright, balanced cup.") — citing the "two single origin lots"
    fragment as ``is_blend=False`` evidence CERTIFIES (returns ``True``),
    because the quote's own polarity phrase ("single origin") is
    genuinely present and the region's "blend combines" prose matches
    neither Tier-1 (no blend-named anchor) nor any fixed Tier-2 compound
    — the lexical whitelist has no notion of COMPOSITION statements
    ("a blend of X and Y") as a signal at all, let alone one that should
    veto a same-region opposite-polarity quote."""
    body = _framed(
        "Sunrise No. 4",
        "This blend combines two single origin lots from Kenya and Ethiopia "
        "for a bright, balanced cup.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    result = bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
        False, "two single origin lots", region, anchor_names_blend=False
    )
    assert result is True  # the documented bypass — NOT the desired outcome


def test_quote_supports_is_blend_negation_tier1_bypass_certifies_wrongly() -> None:
    """Reviewer probe 2: a NEGATION statement ("this coffee is never a
    blend") on a brand-named-"Blend" product CERTIFIES ``is_blend=True``
    — Tier 1 only checks that the anchor names a blend AND a bare "blend"
    word appears in the quote; it has no notion of negation, so "never a
    blend" satisfies the same lexical test as "a genuine blend"."""
    body = _framed(
        "Morning Blend Roastery — Kenya Kiambu",
        "This exceptional coffee is never a blend, sourced entirely from "
        "one estate in the Kiambu region.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    quote = (
        "This exceptional coffee is never a blend, sourced entirely from "
        "one estate in the Kiambu region."
    )
    result = bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
        True, quote, region, anchor_names_blend=True
    )
    assert result is True  # the documented bypass — NOT the desired outcome


def test_quote_supports_is_blend_collection_chrome_bypass_certifies_wrongly() -> None:
    """Reviewer probe 3: collection-membership CHROME ("part of our
    single origin collection") sitting in the lead region, while the
    REAL composition statement lives under an unrelated "## Blend
    Composition" heading the page's own anchor never matches (so it sits
    OUTSIDE the region) — citing the collection-chrome line as
    ``is_blend=False`` evidence CERTIFIES, because the region-exclusion
    that correctly keeps CROSS-SELL chrome out has no way to know this
    particular in-region sentence is itself misleading marketing copy,
    not a genuine composition statement."""
    body = _framed(
        "Roaster's Reserve No. 7",
        "This lot is part of our single origin collection, hand-picked for quality.\n"
        "## Blend Composition\n"
        "70% Colombia, 30% Brazil, roasted together for balance.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    result = bean_sourcing._quote_supports_is_blend(  # pyright: ignore[reportPrivateUsage]
        False, "part of our single origin collection", region, anchor_names_blend=False
    )
    assert result is True  # the documented bypass — NOT the desired outcome


# --- #590 slice E2: fail-closed enum (processing/bean_species) citation
# gate — the final field family in the story. Reuses E1/E1b's
# _main_product_region + _find_authentic_segment unchanged.


def test_quote_supports_processing_spec_row_verifies() -> None:
    """ "Process: Natural" — the colon normalizes to a space, so the
    display token "natural" sits immediately adjacent to "process"."""
    region = "Process: Natural"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "Process: Natural", region
        )
        is True
    )


def test_quote_supports_processing_markdown_table_row_verifies() -> None:
    """A markdown table row — pipes are in the punctuation-to-space
    translation table, so "| Process | Natural |" collapses to the same
    adjacent shape as "Process: Natural"."""
    region = "| Process | Natural |"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "| Process | Natural |", region
        )
        is True
    )


def test_quote_supports_processing_conflict_sentence_demotes_both_claims() -> None:
    """ "The washed process preserves natural sweetness" certifies
    NEITHER claim — ``washed`` fails the segment-scoped conflicting-method
    exclusion (``natural`` is also present), and ``natural`` fails its own
    process-word cue (it sits next to "sweetness", not a process word).
    Either way, symmetric: both demote."""
    sentence = "The washed process preserves natural sweetness"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "washed", sentence, sentence
        )
        is False
    )
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", sentence, sentence
        )
        is False
    )


def test_quote_supports_processing_tasting_note_without_process_cue_demotes() -> None:
    """ "notes of honey and stone fruit" — "honey" the display spelling is
    present, but neither adjacent token ("of"/"and") is a process word, so
    this never counts as a processing claim."""
    region = "This coffee has notes of honey and stone fruit."
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "honey", "notes of honey and stone fruit", region
        )
        is False
    )


def test_quote_supports_processing_segment_scoping_lets_natural_verify() -> None:
    """ "natural process lot" in one segment and "Washed lots also
    available." in ANOTHER segment (period-separated): the conflicting-
    method exclusion is SEGMENT-scoped, so the ``washed`` mention in a
    different sentence does not veto ``natural`` here."""
    region = "This is a natural process lot. Washed lots also available."
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "natural process lot", region
        )
        is True
    )


@pytest.mark.parametrize(
    "quote",
    ["wet hulled process", "wet-hulled method"],
)
def test_quote_supports_processing_wet_hulled_display_spelling_verifies(quote: str) -> None:
    """The one enum value whose underscore Literal ("wet_hulled") differs
    from its display spelling ("wet hulled") — both a space- and a
    hyphen-joined vendor spelling verify (the hyphen normalizes to a
    space, same as every other punctuation-to-space translation)."""
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "wet_hulled", quote, quote
        )
        is True
    )


def test_quote_supports_processing_bare_wet_hulled_tag_line_is_a_documented_over_demote() -> None:
    """A bare tag-line — "Wet-Hulled | Sumatra" — with no attached process
    word demotes. This is a DELIBERATE, documented over-demote (uniform
    cue requirement across every method, no per-value carve-out); the
    operator still sees the captured evidence quote for review."""
    region = "Wet-Hulled | Sumatra"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "wet_hulled", "Wet-Hulled | Sumatra", region
        )
        is False
    )


def test_quote_supports_processing_other_never_verifies() -> None:
    """AC E-6, permanent: ``"other"`` never verifies, for any quote —
    it has no vendor display spelling to cite."""
    region = "Process: Other"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "other", "Process: Other", region
        )
        is False
    )


def test_quote_supports_processing_no_anchor_page_demotes() -> None:
    """No anchor -> :func:`bean_sourcing._main_product_region` collapses
    to ``""`` -> nothing E-gated ever verifies, regardless of a genuine
    quote."""
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "natural process", ""
        )
        is False
    )


def test_quote_supports_processing_fabricated_quote_demotes() -> None:
    """A quote that never appears in the main region at all fails
    authentication before any of the value-derivation checks run."""
    region = "This coffee has notes of stone fruit and honey."
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "washed", "washed process", region
        )
        is False
    )


def test_quote_supports_processing_genuine_quote_without_display_spelling_demotes() -> None:
    """A genuinely authentic, whole-phrase main-region quote that simply
    never mentions the claimed method at all (value-derivation, condition
    2) demotes — authenticity alone is not enough."""
    region = "This coffee is grown at high altitude. Great coffee from Kenya."
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "washed", "Great coffee from Kenya", region
        )
        is False
    )


def test_quote_supports_processing_none_or_blank_quote_demotes() -> None:
    """A missing or whitespace-only evidence quote demotes immediately —
    the gate never even reaches authentication."""
    region = "Process: Natural"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", None, region
        )
        is False
    )
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "   ", region
        )
        is False
    )


def test_quote_supports_processing_none_value_demotes() -> None:
    """A ``None`` claimed value never verifies, quote notwithstanding."""
    region = "Process: Natural"
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            None, "Process: Natural", region
        )
        is False
    )


# --- #590 slice E2: independent adversarial security-reviewer BLOCK on
# ``processing`` — two SEMANTIC certify-bypass classes on ORDINARY vendor
# copy, motivating the permanent dormancy above (same disposition as
# E1b's ``is_blend``). These three tests are EXECUTABLE SPECS for the
# reviewer's exact findings: they assert the helper's ACTUAL
# (documented-bypass) behaviour, not the desired one — proof the park is
# warranted, not a claim the gate is correct.


def test_quote_supports_processing_negation_with_adjacent_cue_bypass_certifies_wrongly() -> None:
    """Reviewer repro 1: negation, with the process-word cue immediately
    adjacent and the TRUE method never named anywhere in the sentence —
    "never a washed process — we let the fruit dry on the bean" certifies
    ``washed`` even though the sentence explicitly DENIES it. The
    conflict exclusion cannot fire here: no OTHER method's display
    spelling is present to contradict the claim, so the whitelist has no
    signal at all that "washed" is being negated rather than asserted."""
    sentence = "never a washed process — we let the fruit dry on the bean"
    result = bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
        "washed", sentence, sentence
    )
    assert result is True  # documented negation bypass — NOT the desired outcome


def test_quote_supports_processing_non_coffee_cue_collision_bypass_certifies_wrongly() -> None:
    """Reviewer repro 2: a non-coffee use of "process"/"method" collides
    with the context cue — "Our roasting process: natural gas burners
    power the drum." is describing EQUIPMENT FUEL, not a coffee
    processing method, yet ``natural`` certifies because the cue words
    are not coffee-processing-specific and "natural" sits right after the
    colon (which normalizes away)."""
    sentence = "Our roasting process: natural gas burners power the drum."
    result = bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
        "natural", sentence, sentence
    )
    assert result is True  # documented cue-collision bypass — NOT the desired outcome


def test_quote_supports_processing_sibling_product_locality_bypass_now_closed() -> None:
    """MEDIUM finding, CLOSED by #617 fold 4: an anchored sibling-product
    heading ("## Kenya AA & Friends" matching the page's own "Kenya AA"
    anchor) used to admit a DIFFERENT product's processing sentence ("our
    sister lot, the Ethiopia Yirgacheffe, which uses a honey process")
    into the main region — the heading-match whitelist had no way to know
    the sentence described another SKU entirely, so the wrong-entity
    citation certified ``honey`` for the Kenya AA page. Fixed at the
    SHARED root (:func:`bean_sourcing._heading_matches_anchor`'s
    conjunction-remainder check, #617 fold 4, driven by the altitude
    whitelist's own sibling-product finding) — a heading whose remainder
    names a conjunction no longer anchors at all, for ANY citation gate
    built on :func:`bean_sourcing._main_product_region`, not just
    altitude's."""
    body = _framed(
        "Kenya AA",
        "## Kenya AA & Friends\n"
        "Our sister lot, the Ethiopia Yirgacheffe, which uses a honey "
        "process, is also available.\n",
    )
    region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    result = bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
        "honey", "which uses a honey process", region
    )
    assert result is False


def test_quote_supports_bean_species_percent_prefix_verifies_without_a_cue() -> None:
    """ "100% arabica" verifies with NO process-word-style cue required —
    species tokens are self-disambiguating."""
    region = "100% arabica"
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "100% arabica", region
        )
        is True
    )


def test_quote_supports_bean_species_mixed_species_conflict_demotes() -> None:
    """ "80% Arabica, 20% Robusta" certifies NEITHER species — a
    single-valued field cannot honestly certify a mix (AC E-7)."""
    region = "80% Arabica, 20% Robusta"
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "80% Arabica, 20% Robusta", region
        )
        is False
    )
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "robusta", "80% Arabica, 20% Robusta", region
        )
        is False
    )


def test_quote_supports_bean_species_tail_region_fails_authentication() -> None:
    """ "robusta" appearing only in a cross-sell/tail block that never made
    it into the main region fails authentication — this test represents
    that scenario directly, by constructing a ``main_region_text`` that
    simply does not contain the word (as E1's locality machinery would
    produce for a related-products block)."""
    main_region_text = "Our finest single-estate arabica lot."
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "robusta", "robusta", main_region_text
        )
        is False
    )


def test_quote_supports_bean_species_negation_probe_documents_actual_behavior() -> None:
    """Adversarial negation probe: "this is not a robusta" — the gate has
    NO negation handling (species has no context-cue step to catch it
    the way a process-word-adjacency check might), so the bare token
    "robusta" is found, whole-word, with nothing else in the segment to
    conflict with it. **This is the ACTUAL, CURRENT behavior — it
    CERTIFIES the wrong claim** (a known gap in the lexical whitelist,
    structurally the same class that got ``is_blend`` parked in E1b).

    THIS is the demonstrated evidence that got
    :data:`bean_sourcing._BEAN_SPECIES_CITATION_GATE_ENABLED` parked
    PRE-REVIEW (lead triage, #590 slice E2) rather than shipped enabled
    pending a review round: ordinary vendor copy ("arabica" marketing
    routinely disses "robusta"), not an adversarial construction, and no
    context cue could have saved it — "varietal" sits immediately
    adjacent to "robusta" in this very sentence, so importing
    ``processing``'s cue mechanism would not close it either. The helper
    itself stays exercised directly here (never through
    :func:`bean_sourcing._draft_from_identity` while the gate is
    dormant); not asserted as correct, only as current."""
    sentence = "This coffee is not a robusta varietal."
    result = bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
        "robusta", sentence, sentence
    )
    assert result is True  # documented negation gap — NOT the desired outcome


def test_quote_supports_bean_species_no_anchor_page_demotes() -> None:
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "100% arabica", ""
        )
        is False
    )


def test_quote_supports_bean_species_fabricated_quote_demotes() -> None:
    region = "Our finest coffee, hand-picked and sun-dried."
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "100% arabica", region
        )
        is False
    )


def test_quote_supports_bean_species_none_or_blank_quote_demotes() -> None:
    region = "100% arabica"
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", None, region
        )
        is False
    )
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "   ", region
        )
        is False
    )


def test_quote_supports_bean_species_none_value_demotes() -> None:
    region = "100% arabica"
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            None, "100% arabica", region
        )
        is False
    )


def test_phrase_token_spans_empty_phrase_returns_no_spans() -> None:
    """Direct unit coverage: the width==0 guard."""
    spans = bean_sourcing._phrase_token_spans(  # pyright: ignore[reportPrivateUsage]
        (), ["natural", "process"]
    )
    assert spans == []


def test_phrase_token_spans_overlapping_matches_all_reported() -> None:
    """#617 perf fold (token-sequence KMP, Codex PR #626 round 3): every
    OVERLAPPING occurrence is still reported, identical to the naive
    per-position scan this replaced — "a a a" against pattern "a a"
    matches at both (0, 1) and (1, 2)."""
    spans = bean_sourcing._phrase_token_spans(  # pyright: ignore[reportPrivateUsage]
        ("a", "a"), ["a", "a", "a"]
    )
    assert spans == [(0, 1), (1, 2)]


def test_phrase_token_spans_exercises_the_kmp_failure_fallback() -> None:
    """Direct unit coverage of the KMP mismatch-after-partial-match
    branch: pattern ("a", "a", "b") against text ("a", "a", "a", "b") —
    at the third token the partial match "a a" fails to extend with "a"
    (pattern wants "b" next), so the failure table falls back to a
    shorter partial match ("a") rather than restarting from scratch, and
    still finds the match ending at the final "b"."""
    spans = bean_sourcing._phrase_token_spans(  # pyright: ignore[reportPrivateUsage]
        ("a", "a", "b"), ["a", "a", "a", "b"]
    )
    assert spans == [(1, 3)]


def test_kmp_failure_function_matches_the_textbook_example() -> None:
    """Direct unit coverage of :func:`bean_sourcing._kmp_failure_function`
    against the standard textbook pattern "a b a b a a" — a mix of
    partial-prefix reuse (index 4) and a fallback-then-extend case
    (index 5)."""
    failure = bean_sourcing._kmp_failure_function(  # pyright: ignore[reportPrivateUsage]
        ("a", "b", "a", "b", "a", "a")
    )
    assert failure == [0, 0, 1, 2, 3, 1]


def test_segment_has_conflicting_enum_value_no_conflict_returns_false() -> None:
    result = bean_sourcing._segment_has_conflicting_enum_value(  # pyright: ignore[reportPrivateUsage]
        "washed",
        ["washed", "process", "only"],
        bean_sourcing._PROCESSING_DISPLAY_SPELLINGS,  # pyright: ignore[reportPrivateUsage]
    )
    assert result is False


# --- #590 slice E2: full-pipeline (_draft_from_identity) integration ---


def test_draft_from_identity_processing_under_matched_heading_stays_dormant() -> None:
    """DORMANT-gate form (#590 slice E2 — ``processing`` shipped ENABLED
    at birth, then PARKED PERMANENTLY by an independent adversarial
    review BLOCK before this slice opened, see
    :data:`bean_sourcing._PROCESSING_CITATION_GATE_ENABLED`), mirroring
    E1b's ``is_blend`` dormancy proof: an anchored heading region
    genuinely stating "Process: Natural", with a matching evidence quote,
    demotes at RUNTIME regardless — the helper itself still recognizes
    the citation (see the direct-call assertion below), proving the
    machinery works and only its consumption is gated off."""
    body = _framed(
        "Ethiopia Yirgacheffe",
        "## Ethiopia Yirgacheffe\nProcess: Natural\nGrown at high altitude.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing="natural", processing_evidence="Process: Natural")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=body
    )
    assert draft.field_sources["processing"] == "origin_estimated"
    main_region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_processing(  # pyright: ignore[reportPrivateUsage]
            "natural", "Process: Natural", main_region
        )
        is True
    )


def test_draft_from_identity_bean_species_under_matched_heading_stays_dormant() -> None:
    """DORMANT-gate form (#590 slice E2 — ``bean_species`` split off and
    parked before this slice opened, see
    :data:`bean_sourcing._BEAN_SPECIES_CITATION_GATE_ENABLED`), mirroring
    E1b's ``is_blend`` dormancy proof: the SAME page, with a genuine,
    authentic, well-formed citation, demotes at RUNTIME regardless — the
    helper itself still recognizes the citation (see the direct-call
    assertion below), proving the machinery works and only its
    consumption is gated off."""
    body = _framed(
        "Ethiopia Yirgacheffe",
        "## Ethiopia Yirgacheffe\n100% arabica, hand-picked at peak ripeness.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_species="arabica", bean_species_evidence="100% arabica")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=body
    )
    assert draft.field_sources["bean_species"] == "origin_estimated"
    main_region = bean_sourcing._main_product_region(body, "", "")  # pyright: ignore[reportPrivateUsage]
    assert (
        bean_sourcing._quote_supports_bean_species(  # pyright: ignore[reportPrivateUsage]
            "arabica", "100% arabica", main_region
        )
        is True
    )


def test_draft_from_identity_processing_cross_sell_decoy_demotes() -> None:
    """The marquee decoy, mirrored from E1b's ``is_blend`` test: a
    genuinely-cited "washed process" line sitting under an unmatched
    "## You May Also Like" heading never enters the main region, so it
    demotes regardless of how genuine the citation is."""
    body = _framed(
        "Ethiopia Yirgacheffe Natural",
        "This lot is a natural process coffee from Yirgacheffe.\n"
        "\n"
        "## You May Also Like\n"
        "Our washed process Kenya lot.\n",
    )
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing="washed", processing_evidence="washed process")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/yirgacheffe", corpus=body
    )
    assert draft.field_sources["processing"] == "origin_estimated"


def test_draft_from_identity_processing_gate_ships_dormant() -> None:
    """#590 slice E2: ``processing`` shipped ENABLED at birth, then was
    PARKED PERMANENTLY by an independent adversarial review BLOCK before
    this slice opened — two demonstrated semantic bypasses on ordinary
    vendor copy (see the negation-probe and cue-collision tests below),
    the same disposition as ``bean_species``/``is_blend``."""
    assert (
        bean_sourcing._PROCESSING_CITATION_GATE_ENABLED  # pyright: ignore[reportPrivateUsage]
        is False
    )


def test_draft_from_identity_bean_species_gate_ships_dormant() -> None:
    """#590 slice E2: ``bean_species`` was split off and parked BEFORE
    this slice opened — a demonstrated negation bypass (see the negation
    probe test above), the same semantic class that parked ``is_blend``
    in E1b, with no context cue available to close it."""
    assert (
        bean_sourcing._BEAN_SPECIES_CITATION_GATE_ENABLED  # pyright: ignore[reportPrivateUsage]
        is False
    )


def test_draft_from_identity_marks_every_roast_target_origin_estimated() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=_IDENTITY_PAGE_TEXT
    )
    for field in (
        "charge_guidance_min_c",
        "charge_guidance_max_c",
        "initial_heat_percent",
        "initial_fan_percent",
        "target_drop_temp_c",
        "target_development_percent",
        "default_bean_weight_grams",
    ):
        assert draft.field_sources[field] == "origin_estimated", field
    assert "scouting run" in draft.scouting_note.lower()


def test_draft_from_identity_scouting_note_does_not_claim_deferred_fields_absent() -> None:
    """#590 D1 fold 2 (round-3 Codex P2), carried into D2b: the note used to
    say every ``origin_estimated`` field "was NOT found on the vendor
    page" — false for a TYPED field (e.g. ``processing``) that genuinely
    IS on the page but whose citation check failed (no evidence quote
    here). The wording must be honest across absent / failed-citation /
    deferred(``is_blend``), and must NOT claim outright absence."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing="washed")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This lot was fully washed and sun-dried on raised beds.",
    )
    assert draft.field_sources["processing"] == "origin_estimated"
    note = draft.scouting_note.lower()
    assert "not verified" in note
    assert "citation check failed" in note
    assert "was not found on the vendor page" not in note


def test_draft_from_identity_bean_origin_falls_back_to_country_and_is_still_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_origin=None, country="Ethiopia")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/eth",
        corpus="Ethiopia Yirgacheffe, a natural process lot.",
    )
    assert draft.bean_origin == "Ethiopia"
    assert draft.field_sources["bean_origin"] == "on_page"


def test_draft_from_identity_bean_origin_fallback_inherits_a_demoted_country() -> None:
    """#590 D1: when ``bean_origin`` falls back to ``country``, the
    fallback must inherit COUNTRY's own verified provenance rather than an
    automatic ``"on_page"`` — a confabulated country (absent from the
    corpus) must leave the bean_origin fallback demoted too."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_origin=None, country="Ethiopia")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/eth", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.bean_origin == "Ethiopia"
    assert draft.field_sources["bean_origin"] == "origin_estimated"


@pytest.mark.parametrize(
    ("missing_name", "missing_origin"),
    [
        (True, False),
        (False, True),
        (True, True),
    ],
)
def test_draft_from_identity_raises_when_name_or_origin_is_missing(
    missing_name: bool, missing_origin: bool
) -> None:
    overrides: dict[str, object] = {}
    if missing_name:
        overrides["name"] = None
    if missing_origin:
        overrides["bean_origin"] = None
        overrides["country"] = None
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(**overrides)
    )
    with pytest.raises(BeanExtractionError, match="could not determine"):
        bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
            identity, url="https://vendor.example/products/nope", corpus=_IDENTITY_PAGE_TEXT
        )


@pytest.mark.parametrize(
    ("processing", "expected_drop", "expected_dev"),
    [
        ("natural", 193.0, 13.0),
        ("washed", 195.0, 15.0),
        ("honey", 194.0, 14.0),
        ("anaerobic", 194.0, 14.0),
        ("wet_hulled", 195.0, 15.0),
        ("other", 194.0, 14.0),
        (None, 194.0, 14.0),
    ],
)
def test_draft_from_identity_applies_conservative_scouting_targets_by_processing(
    processing: str | None, expected_drop: float, expected_dev: float
) -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(processing=processing)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/x", corpus=_IDENTITY_PAGE_TEXT
    )
    assert draft.target_drop_temp_c == expected_drop
    assert draft.target_development_percent == expected_dev
    # Every scouting drop/dev stays inside the operator's proven de-risked
    # band (issue #573): drop <=195, dev in [13, 15].
    assert draft.target_drop_temp_c <= 195.0
    assert 13.0 <= draft.target_development_percent <= 15.0


# --- #590 slice B: deterministic JSON-LD product extraction (unit) ---

_MATCHING_JSON_LD_URL = "https://vendor.example/products/kenya-kiambu"

_MATCHING_JSON_LD_SCRIPT = """
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Kenya Kiambu AA (Washed)",
  "url": "https://vendor.example/products/kenya-kiambu",
  "sku": "KE-KIAMBU-AA",
  "brand": {"@type": "Brand", "name": "Vendor Roastery"},
  "description": "A washed Kenyan lot from Kiambu.",
  "offers": {"@type": "Offer", "url": "https://vendor.example/products/kenya-kiambu"}
}
</script>
"""

#: A DIFFERENT product's block, embedded on the SAME page ("customers also
#: bought" style) — the identity-match gate must reject it (#590 slice B).
_STALE_JSON_LD_SCRIPT = """
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Ethiopia Yirgacheffe (Natural)",
  "url": "https://vendor.example/products/ethiopia-yirgacheffe",
  "description": "A DIFFERENT, unrelated product on the same page."
}
</script>
"""


def _html_with_json_ld(*scripts: str) -> str:
    """``_SAMPLE_HTML`` with one or more JSON-LD ``<script>`` blocks spliced
    into ``<head>`` (#590 slice B test fixture)."""
    return _SAMPLE_HTML.replace("<head>", "<head>" + "".join(scripts), 1)


def test_clean_json_ld_text_rejects_non_string_and_blank() -> None:
    assert bean_sourcing._clean_json_ld_text("  Kenya  ") == "Kenya"  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._clean_json_ld_text("   ") is None  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._clean_json_ld_text(42) is None  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._clean_json_ld_text(None) is None  # pyright: ignore[reportPrivateUsage]


def test_clean_json_ld_text_truncates_an_oversized_field() -> None:
    # A malicious page could put an enormous string in a single JSON-LD
    # field (e.g. "description"); this must never reach the LLM prompt
    # unbounded, independent of the separate page-text truncation (#590
    # slice B resource-exhaustion guard).
    huge_value = "x" * 10_000
    cleaned = bean_sourcing._clean_json_ld_text(huge_value)  # pyright: ignore[reportPrivateUsage]
    assert cleaned is not None
    assert len(cleaned) == bean_sourcing._MAX_JSON_LD_FIELD_CHARS  # pyright: ignore[reportPrivateUsage]


def test_canonical_product_locator_resolves_relative_and_normalizes() -> None:
    locator = bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
        "/products/kenya-kiambu/", base_url="https://Vendor.example/other-page"
    )
    assert locator == bean_sourcing._CanonicalLocator(  # pyright: ignore[reportPrivateUsage]
        host_path="vendor.example/products/kenya-kiambu", query=""
    )


def test_canonical_product_locator_ignores_scheme_but_preserves_query() -> None:
    """Scheme is still ignored for identity (http/https don't discriminate),
    but the query is now PRESERVED on the locator itself (#590 P2 fix) —
    whether it discriminates a MATCH is `_locators_identity_match`'s job,
    not this function's."""
    with_query = bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
        "http://vendor.example/products/kenya-kiambu?ref=email",
        base_url="https://vendor.example/products/kenya-kiambu",
    )
    without_query = bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya-kiambu",
        base_url="https://vendor.example/products/kenya-kiambu",
    )
    assert with_query is not None
    assert without_query is not None
    assert with_query.host_path == without_query.host_path == "vendor.example/products/kenya-kiambu"
    assert with_query.query == "ref=email"
    assert without_query.query == ""


def test_canonical_product_locator_normalizes_query_param_order() -> None:
    a = bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/p?a=1&b=2", base_url="https://vendor.example/p"
    )
    b = bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/p?b=2&a=1", base_url="https://vendor.example/p"
    )
    assert a == b


def test_locators_identity_match_requires_same_host_path() -> None:
    a = bean_sourcing._CanonicalLocator(host_path="vendor.example/p", query="")  # pyright: ignore[reportPrivateUsage]
    b = bean_sourcing._CanonicalLocator(host_path="vendor.example/other", query="")  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._locators_identity_match(a, b) is False  # pyright: ignore[reportPrivateUsage]


def test_locators_identity_match_rejects_a_discriminating_query_mismatch() -> None:
    """`?id=kenya` vs `?id=ethiopia` on the SAME path must NOT cross-match
    (#590 P2 fix) — the query is a real identity discriminator here (a
    query-param-selected product, or a Shopify `?variant=...`)."""
    kenya = bean_sourcing._CanonicalLocator(host_path="vendor.example/p", query="id=kenya")  # pyright: ignore[reportPrivateUsage]
    ethiopia = bean_sourcing._CanonicalLocator(  # pyright: ignore[reportPrivateUsage]
        host_path="vendor.example/p", query="id=ethiopia"
    )
    assert bean_sourcing._locators_identity_match(kenya, ethiopia) is False  # pyright: ignore[reportPrivateUsage]


def test_locators_identity_match_allows_a_query_less_side_to_match_either_way() -> None:
    """A JSON-LD block's own url commonly omits a page's variant/tracking
    query entirely — a query-less side must still match a query-bearing
    one on the SAME path (#590 P2 fix), or the block is lost even though it
    IS the right product."""
    with_query = bean_sourcing._CanonicalLocator(host_path="vendor.example/p", query="id=kenya")  # pyright: ignore[reportPrivateUsage]
    without_query = bean_sourcing._CanonicalLocator(host_path="vendor.example/p", query="")  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._locators_identity_match(with_query, without_query) is True  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._locators_identity_match(without_query, with_query) is True  # pyright: ignore[reportPrivateUsage]


def test_canonical_product_locator_rejects_malformed_value() -> None:
    assert (
        bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
            "http://[::1", base_url="https://vendor.example/products/kenya-kiambu"
        )
        is None
    )


def test_canonical_product_locator_rejects_non_http_scheme() -> None:
    assert (
        bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
            "mailto:sales@vendor.example", base_url="https://vendor.example/x"
        )
        is None
    )


def test_canonical_product_locator_rejects_missing_host() -> None:
    # A DIFFERENT-scheme base_url avoids urljoin's same-scheme netloc-
    # inheriting compatibility quirk, so the empty-host "https:///..." form
    # is resolved as-is rather than folded onto the base's own host.
    assert (
        bean_sourcing._canonical_product_locator(  # pyright: ignore[reportPrivateUsage]
            "https:///no-host", base_url="ftp://vendor.example/x"
        )
        is None
    )


def test_is_product_type_matches_bare_string() -> None:
    assert bean_sourcing._is_product_type("Product") is True  # pyright: ignore[reportPrivateUsage]


def test_is_product_type_matches_within_a_list_and_skips_non_strings() -> None:
    assert bean_sourcing._is_product_type(["Thing", 42, "Product"]) is True  # pyright: ignore[reportPrivateUsage]


def test_is_product_type_matches_full_schema_org_uri() -> None:
    assert bean_sourcing._is_product_type("https://schema.org/Product") is True  # pyright: ignore[reportPrivateUsage]


def test_is_product_type_rejects_non_matching_and_wrong_case() -> None:
    assert bean_sourcing._is_product_type("BreadcrumbList") is False  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._is_product_type("product") is False  # pyright: ignore[reportPrivateUsage]
    assert bean_sourcing._is_product_type(None) is False  # pyright: ignore[reportPrivateUsage]


def test_product_blocks_from_items_finds_top_level_and_graph_nested() -> None:
    items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Top-level"},
        {
            "@type": "WebPage",
            "@graph": [
                {"@type": "BreadcrumbList"},
                {"@type": "Product", "name": "Nested"},
            ],
        },
    ]
    blocks = bean_sourcing._product_blocks_from_items(items)  # pyright: ignore[reportPrivateUsage]
    assert [b["name"] for b in blocks] == ["Top-level", "Nested"]


def test_product_blocks_from_items_skips_non_list_graph_and_non_dict_nested() -> None:
    items: list[dict[str, object]] = [
        {"@type": "WebPage", "@graph": "not-a-list"},
        {"@type": "WebPage", "@graph": [1, 2, {"@type": "Product", "name": "Nested"}]},
    ]
    blocks = bean_sourcing._product_blocks_from_items(items)  # pyright: ignore[reportPrivateUsage]
    assert [b["name"] for b in blocks] == ["Nested"]


def test_product_blocks_from_items_bounds_top_level_inspection() -> None:
    items: list[dict[str, object]] = [{"@type": "Product", "name": f"item-{i}"} for i in range(30)]
    blocks = bean_sourcing._product_blocks_from_items(items)  # pyright: ignore[reportPrivateUsage]
    assert len(blocks) == bean_sourcing._MAX_JSON_LD_ITEMS  # pyright: ignore[reportPrivateUsage]


def test_product_blocks_from_items_bounds_nested_graph_inspection() -> None:
    items: list[dict[str, object]] = [
        {
            "@type": "WebPage",
            "@graph": [{"@type": "Product", "name": f"nested-{i}"} for i in range(30)],
        }
    ]
    blocks = bean_sourcing._product_blocks_from_items(items)  # pyright: ignore[reportPrivateUsage]
    # The top-level WebPage item itself counts against the shared bound too.
    assert len(blocks) == bean_sourcing._MAX_JSON_LD_ITEMS - 1  # pyright: ignore[reportPrivateUsage]


def test_product_identity_candidates_collects_id_url_and_offers_list() -> None:
    block: dict[str, object] = {
        "@id": "https://vendor.example/products/a",
        "url": "https://vendor.example/products/b",
        "offers": [
            {"url": "https://vendor.example/products/c"},
            {"no_url": "ignored"},
            "not-a-dict",
        ],
    }
    candidates = bean_sourcing._product_identity_candidates(block)  # pyright: ignore[reportPrivateUsage]
    assert candidates == [
        "https://vendor.example/products/a",
        "https://vendor.example/products/b",
        "https://vendor.example/products/c",
    ]


def test_product_identity_candidates_handles_single_offer_object_and_missing_offers() -> None:
    with_single_offer = bean_sourcing._product_identity_candidates(  # pyright: ignore[reportPrivateUsage]
        {"offers": {"url": "https://vendor.example/products/d"}}
    )
    assert with_single_offer == ["https://vendor.example/products/d"]
    assert bean_sourcing._product_identity_candidates({}) == []  # pyright: ignore[reportPrivateUsage]


def test_select_identity_matched_product_finds_matching_block_among_others() -> None:
    raw_items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Stale", "url": "https://vendor.example/products/other"},
        {"@type": "Product", "name": "Real", "url": _MATCHING_JSON_LD_URL},
    ]
    matched = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url=_MATCHING_JSON_LD_URL
    )
    assert matched is not None
    assert matched["name"] == "Real"


def test_select_identity_matched_product_returns_none_when_nothing_matches() -> None:
    raw_items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Stale", "url": "https://vendor.example/products/other"}
    ]
    assert (
        bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
            raw_items, url=_MATCHING_JSON_LD_URL
        )
        is None
    )


def test_select_identity_matched_product_returns_none_for_malformed_target_url() -> None:
    raw_items: list[dict[str, object]] = [{"@type": "Product", "url": "not-http"}]
    assert (
        bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
            raw_items, url="mailto:x@vendor.example"
        )
        is None
    )


def test_select_identity_matched_product_skips_a_malformed_candidate_url() -> None:
    """A VALID target but a candidate whose own url is malformed (an
    unclosed IPv6 bracket) must be skipped, not crash — in EACH pass."""
    raw_items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Malformed", "url": "http://[::1"},
        {"@type": "Product", "name": "Real", "url": _MATCHING_JSON_LD_URL},
    ]
    matched = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url=_MATCHING_JSON_LD_URL
    )
    assert matched is not None
    assert matched["name"] == "Real"


def test_select_identity_matched_product_query_discriminates_between_variants() -> None:
    """#590 P2 fix: two blocks sharing a path but DIFFERENT identity-bearing
    queries (a query-param-selected origin, or a Shopify ``?variant=...``)
    must not cross-match — the requested URL's query picks the right one."""
    raw_items: list[dict[str, object]] = [
        {
            "@type": "Product",
            "name": "Kenya",
            "url": "https://vendor.example/product?id=kenya",
        },
        {
            "@type": "Product",
            "name": "Ethiopia",
            "url": "https://vendor.example/product?id=ethiopia",
        },
    ]
    kenya_match = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url="https://vendor.example/product?id=kenya"
    )
    ethiopia_match = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url="https://vendor.example/product?id=ethiopia"
    )
    assert kenya_match is not None and kenya_match["name"] == "Kenya"
    assert ethiopia_match is not None and ethiopia_match["name"] == "Ethiopia"


def test_select_identity_matched_product_matches_query_less_json_ld_against_query_bearing_url() -> (
    None
):
    """#590 P2 fix: a JSON-LD block whose OWN url omits the page's variant/
    tracking query must still match a requested URL that carries one — a
    query-less side is not itself discriminating, so the block that IS the
    right product is not lost."""
    raw_items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Kenya", "url": "https://vendor.example/product"}
    ]
    matched = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url="https://vendor.example/product?variant=12345"
    )
    assert matched is not None
    assert matched["name"] == "Kenya"


def test_select_identity_matched_product_prefers_exact_match_over_query_less_generic_block() -> (
    None
):
    """#590 P2 fix, round 2 (Codex): a page can carry BOTH a generic
    (query-less) Product block and a variant-specific one — the generic
    block must NOT shadow the exact one just because it appears first in
    document order; an exact locator match (host+path AND query) always
    wins over the query-less wildcard fallback."""
    raw_items: list[dict[str, object]] = [
        {"@type": "Product", "name": "Generic", "url": "https://vendor.example/product"},
        {
            "@type": "Product",
            "name": "Variant B",
            "url": "https://vendor.example/product?variant=B",
        },
    ]
    matched = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url="https://vendor.example/product?variant=B"
    )
    assert matched is not None
    assert matched["name"] == "Variant B"


def test_select_identity_matched_product_prefers_exact_match_regardless_of_block_order() -> None:
    """Same as above with the blocks in the OPPOSITE document order — the
    two-pass selection must be order-independent within each pass."""
    raw_items: list[dict[str, object]] = [
        {
            "@type": "Product",
            "name": "Variant B",
            "url": "https://vendor.example/product?variant=B",
        },
        {"@type": "Product", "name": "Generic", "url": "https://vendor.example/product"},
    ]
    matched = bean_sourcing._select_identity_matched_product(  # pyright: ignore[reportPrivateUsage]
        raw_items, url="https://vendor.example/product?variant=B"
    )
    assert matched is not None
    assert matched["name"] == "Variant B"


def test_facts_from_product_block_unwraps_nested_brand_object() -> None:
    facts = bean_sourcing._facts_from_product_block(  # pyright: ignore[reportPrivateUsage]
        {
            "name": " Kenya Kiambu AA ",
            "brand": {"name": "Vendor Roastery"},
            "sku": "KE-1",
            "description": "A lot.",
        }
    )
    assert facts.name == "Kenya Kiambu AA"
    assert facts.brand == "Vendor Roastery"
    assert facts.sku == "KE-1"
    assert facts.description == "A lot."


def test_facts_from_product_block_accepts_plain_string_brand_and_missing_fields() -> None:
    facts = bean_sourcing._facts_from_product_block({"brand": "Vendor Roastery"})  # pyright: ignore[reportPrivateUsage]
    assert facts.brand == "Vendor Roastery"
    assert facts.name is None
    facts_no_brand = bean_sourcing._facts_from_product_block({})  # pyright: ignore[reportPrivateUsage]
    assert facts_no_brand.brand is None


def test_format_json_ld_context_renders_present_fields_only() -> None:
    facts = bean_sourcing._JsonLdProductFacts(name="Kenya Kiambu AA", sku="KE-1")  # pyright: ignore[reportPrivateUsage]
    context = bean_sourcing._format_json_ld_context(facts)  # pyright: ignore[reportPrivateUsage]
    assert context is not None
    assert "- name: Kenya Kiambu AA" in context
    assert "- sku: KE-1" in context
    assert "- brand:" not in context


def test_format_json_ld_context_returns_none_when_every_field_absent() -> None:
    assert (
        bean_sourcing._format_json_ld_context(bean_sourcing._JsonLdProductFacts())  # pyright: ignore[reportPrivateUsage]
        is None
    )


# --- #590 slice B: _parse_html_for_json_ld (the XXE-safety boundary) ---


def test_parse_html_for_json_ld_extracts_matching_script() -> None:
    items = bean_sourcing._parse_html_for_json_ld(  # pyright: ignore[reportPrivateUsage]
        _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT)
    )
    assert len(items) == 1
    assert items[0]["name"] == "Kenya Kiambu AA (Washed)"


def test_parse_html_for_json_ld_returns_empty_list_when_no_json_ld_present() -> None:
    assert bean_sourcing._parse_html_for_json_ld(_SAMPLE_HTML) == []  # pyright: ignore[reportPrivateUsage]


def test_parse_html_for_json_ld_fails_soft_on_malformed_json() -> None:
    malformed = _html_with_json_ld(
        '<script type="application/ld+json">{not valid json at all!}</script>'
    )
    assert bean_sourcing._parse_html_for_json_ld(malformed) == []  # pyright: ignore[reportPrivateUsage]


def test_parse_html_for_json_ld_fails_soft_on_unparseable_html() -> None:
    # An empty document raises lxml.etree.ParserError ("Document is empty"),
    # verified directly against lxml's real behavior — the LxmlError branch.
    assert bean_sourcing._parse_html_for_json_ld("") == []  # pyright: ignore[reportPrivateUsage]


def test_parse_html_for_json_ld_fails_soft_when_extruct_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: object, **kwargs: object) -> object:
        raise RuntimeError("simulated extruct failure")

    monkeypatch.setattr(extruct, "extract", _raise)
    items = bean_sourcing._parse_html_for_json_ld(  # pyright: ignore[reportPrivateUsage]
        _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT)
    )
    assert items == []


def test_parse_html_for_json_ld_filters_non_dict_top_level_items() -> None:
    array_of_scalars = _html_with_json_ld(
        '<script type="application/ld+json">'
        '[1, "two", {"@type": "Product", "name": "Three"}]'
        "</script>"
    )
    items = bean_sourcing._parse_html_for_json_ld(array_of_scalars)  # pyright: ignore[reportPrivateUsage]
    assert items == [{"@type": "Product", "name": "Three"}]


def test_parse_html_for_json_ld_xxe_payload_does_not_expand_entity_or_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An external-entity payload referencing a local/metadata address must
    never be fetched or expanded (#590 slice B, checklist class 4)."""
    connect_attempts: list[object] = []

    def _spy_connect(self: socket.socket, address: object) -> None:
        # Records the attempt and fails closed — the test's whole point is
        # that this must NEVER be called at all, so it deliberately never
        # forwards to a real connect (safe even if that assumption were
        # ever wrong: no real network I/O happens from a test either way).
        connect_attempts.append(address)
        raise OSError("network access blocked in test")

    monkeypatch.setattr(socket.socket, "connect", _spy_connect)
    xxe_payload = (
        "<!DOCTYPE html [<!ENTITY xxe SYSTEM "
        '"http://169.254.169.254/latest/meta-data/">]>\n'
        "<html><body>"
        '<script type="application/ld+json">'
        '{"@type": "Product", "name": "Bean", "url": "' + _MATCHING_JSON_LD_URL + '", '
        '"description": "&xxe;"}'
        "</script>"
        "</body></html>"
    )
    items = bean_sourcing._parse_html_for_json_ld(xxe_payload)  # pyright: ignore[reportPrivateUsage]
    assert connect_attempts == []
    assert len(items) == 1
    # The entity is NEVER expanded in HTML parsing mode — the literal,
    # unexpanded marker string comes through unchanged, not fetched content.
    assert items[0]["description"] == "&xxe;"


def test_parse_html_for_json_ld_billion_laughs_style_payload_completes_quickly() -> None:
    """A deeply-nested internal-entity declaration (the "billion laughs"
    shape) must not hang or crash — HTML-mode parsing never expands it."""
    payload = (
        "<!DOCTYPE html [\n"
        '<!ENTITY lol "lol">\n'
        '<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
        '<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
        "]>\n"
        "<html><body>"
        '<script type="application/ld+json">{"@type": "Product", "name": "&lol3;"}</script>'
        "</body></html>"
    )
    start = time.monotonic()
    items = bean_sourcing._parse_html_for_json_ld(payload)  # pyright: ignore[reportPrivateUsage]
    elapsed = time.monotonic() - start
    assert elapsed < 2.0
    assert len(items) == 1
    assert items[0]["name"] == "&lol3;"


def test_parse_html_for_json_ld_fails_soft_on_deeply_nested_json_recursion_bomb() -> None:
    """A JSON-native DoS shape (unlike the inert HTML-entity one above): a
    deeply-nested JSON array exhausts Python's ``json.loads`` recursion
    limit (verified directly: ``RecursionError``) rather than the XML-DTD
    entity mechanism — must still fail soft, not crash the fetch."""
    deeply_nested_array = "[" * 5000 + "]" * 5000
    payload = _html_with_json_ld(
        f'<script type="application/ld+json">{deeply_nested_array}</script>'
    )
    assert bean_sourcing._parse_html_for_json_ld(payload) == []  # pyright: ignore[reportPrivateUsage]


# --- #590 slice B: _match_json_ld_product_facts (#590 D1 fold 1: renamed
# from _build_json_ld_context, which now composes this + _format_json_ld_
# context — both independently tested) ---


def test_match_json_ld_product_facts_returns_facts_for_a_matching_page() -> None:
    facts = bean_sourcing._match_json_ld_product_facts(  # pyright: ignore[reportPrivateUsage]
        _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT), _MATCHING_JSON_LD_URL
    )
    assert facts is not None
    assert facts.name == "Kenya Kiambu AA (Washed)"
    assert facts.sku == "KE-KIAMBU-AA"


def test_match_json_ld_product_facts_returns_none_without_json_ld() -> None:
    assert (
        bean_sourcing._match_json_ld_product_facts(  # pyright: ignore[reportPrivateUsage]
            _SAMPLE_HTML, _MATCHING_JSON_LD_URL
        )
        is None
    )


def test_match_json_ld_product_facts_returns_none_for_a_stale_unmatched_block() -> None:
    assert (
        bean_sourcing._match_json_ld_product_facts(  # pyright: ignore[reportPrivateUsage]
            _html_with_json_ld(_STALE_JSON_LD_SCRIPT), _MATCHING_JSON_LD_URL
        )
        is None
    )


def test_match_json_ld_product_facts_fails_soft_on_internal_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(html: str) -> list[dict[str, object]]:
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(bean_sourcing, "_parse_html_for_json_ld", _raise)
    assert (
        bean_sourcing._match_json_ld_product_facts(  # pyright: ignore[reportPrivateUsage]
            _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT), _MATCHING_JSON_LD_URL
        )
        is None
    )


# --- #590 slice B: wired into _fetch_page_text ---


@pytest.mark.asyncio
async def test_fetch_page_text_prepends_matching_json_ld_context() -> None:
    async with _mock_client(
        _html_response(200, _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT))
    ) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                _MATCHING_JSON_LD_URL, config=BeanSourcingConfig(), http_client=client
            )
        ).prompt_text
    assert text.startswith("Structured data found in this page's JSON-LD")
    assert "KE-KIAMBU-AA" in text
    assert "Kenya Kiambu AA" in text  # the plain-text extraction still follows


@pytest.mark.asyncio
async def test_fetch_page_text_ignores_a_stale_unmatched_json_ld_block() -> None:
    async with _mock_client(
        _html_response(200, _html_with_json_ld(_STALE_JSON_LD_SCRIPT))
    ) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                _MATCHING_JSON_LD_URL, config=BeanSourcingConfig(), http_client=client
            )
        ).prompt_text
    assert "Structured data found in this page" not in text
    assert "Yirgacheffe" not in text  # the stale block's own text never leaks in


@pytest.mark.asyncio
async def test_fetch_page_text_without_json_ld_falls_through_byte_for_byte_unchanged() -> None:
    """No JSON-LD on the page: the page-body text is exactly whatever
    :func:`_extract_page_markdown` produces for it (#590 slice C — the
    linear-strip :func:`_extract_page_text` is only reached when trafilatura
    finds nothing usable; see the ``_returns_none`` variant below for that
    path)."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                _MATCHING_JSON_LD_URL, config=BeanSourcingConfig(), http_client=client
            )
        ).prompt_text
    assert text == bean_sourcing._extract_page_markdown(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_fetch_page_text_fails_soft_on_a_page_with_malformed_json_ld() -> None:
    malformed_page = _html_with_json_ld(
        '<script type="application/ld+json">{not valid json at all!}</script>'
    )
    async with _mock_client(_html_response(200, malformed_page)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                _MATCHING_JSON_LD_URL, config=BeanSourcingConfig(), http_client=client
            )
        ).prompt_text
    assert "Structured data found in this page" not in text
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_fetch_page_text_identity_matches_json_ld_against_final_redirected_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#590 slice B P1 fix: a redirect (bare -> www here, but equally
    http->https/trailing-slash/slug canonicalisation) means the fetched
    HTML's own JSON-LD reflects the FINAL URL, not the operator-supplied
    one — identity-match must run against the URL _fetch_with_ssrf_guard
    actually landed on. Before the fix this JSON-LD (whose ``url`` is the
    REDIRECTED, not the original, URL) would never match."""
    original_host = "vendor.example"
    redirected_host = "www.vendor.example"
    original_url = f"https://{original_host}/products/kenya-kiambu"
    redirected_url = f"https://{redirected_host}/products/kenya-kiambu"
    host_ips = {original_host: "93.184.216.34", redirected_host: "93.184.216.35"}
    page_with_json_ld = _html_with_json_ld(
        '<script type="application/ld+json">'
        f'{{"@type": "Product", "name": "Kenya Kiambu AA", "sku": "KE-REDIRECTED", '
        f'"url": "{redirected_url}"}}'
        "</script>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host_header = request.headers.get("host")
        if host_header == original_host:
            return httpx.Response(302, headers={"Location": redirected_url})
        assert host_header == redirected_host
        return httpx.Response(200, content=_bytes_stream(page_with_json_ld.encode()))

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    async def fake_getaddrinfo(
        host: str, port: int, *, type: int
    ) -> list[tuple[object, object, object, object, tuple[str, int]]]:
        return [(None, None, None, "", (host_ips[host], port))]

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    text = (
        await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            original_url, config=BeanSourcingConfig()
        )
    ).prompt_text
    assert "Structured data found in this page's JSON-LD" in text
    assert "KE-REDIRECTED" in text


# --- #590 slice B: reaches the extraction prompt (draft_bean_profile_from_url) ---


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_feeds_json_ld_context_to_extraction_prompt() -> None:
    seen_prompts: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in messages:
            for part in message.parts:
                text = getattr(part, "content", None)
                if isinstance(text, str):
                    seen_prompts.append(text)
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, _identity_args())])

    async with _mock_client(
        _html_response(200, _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT))
    ) as http_client:
        draft = await draft_bean_profile_from_url(
            _MATCHING_JSON_LD_URL,
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=FunctionModel(respond),
        )
    assert draft.name == "Kenya Kiambu AA (Washed)"
    assert any("Structured data found in this page" in prompt for prompt in seen_prompts)
    assert any("KE-KIAMBU-AA" in prompt for prompt in seen_prompts)


#: A page whose VISIBLE body never states the product name/description —
#: only the JSON-LD block does. Proves the #590 D1 containment corpus
#: includes the prepended JSON-LD DATA section, not just the extracted
#: body text.
_JSON_LD_ONLY_URL = "https://vendor.example/products/colombia-huila"

_JSON_LD_ONLY_HTML = """
<html>
<head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "Product",
  "name": "Colombia Huila Pink Bourbon",
  "url": "https://vendor.example/products/colombia-huila",
  "description": "A vibrant lot from Huila."
}
</script>
</head>
<body>
<p>Great coffee. Free shipping on orders over $50. Subscribe and save.</p>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_json_ld_only_value_verifies_on_page() -> None:
    """#590 D1: the containment corpus is the SAME page text the model
    saw, which includes the prepended JSON-LD DATA section — a field value
    present ONLY via JSON-LD (never in the visible body text) must still
    verify ``"on_page"``, not get wrongly demoted for being off the
    rendered body."""

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name,
                    _identity_args(
                        name="Colombia Huila Pink Bourbon",
                        country="Colombia",
                        bean_origin="Colombia",
                        farm=None,
                        bean_varietal=None,
                        processing=None,
                        bean_species=None,
                        altitude_m=None,
                        description="A vibrant lot from Huila.",
                        is_blend=None,
                    ),
                )
            ]
        )

    async with _mock_client(_html_response(200, _JSON_LD_ONLY_HTML)) as http_client:
        draft = await draft_bean_profile_from_url(
            _JSON_LD_ONLY_URL,
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=FunctionModel(respond),
        )
    assert draft.name == "Colombia Huila Pink Bourbon"
    assert draft.field_sources["name"] == "on_page"


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_confabulated_value_from_our_own_header_is_demoted() -> (
    None
):
    """#590 D1 fold 1 (Codex P2): the LLM-prompt text (``page_text``) is
    prefixed with OUR OWN generated JSON-LD context header/labels
    ("Structured data found in this page's JSON-LD (schema.org Product
    block, identity-matched to the fetched URL)."). Reusing that text as
    the containment-verification corpus let a model-returned value match
    OUR scaffolding instead of real vendor content — a prompt-injected
    page could aim a confabulated value straight at it. The verification
    corpus must be vendor-data-only, so a value drawn from the header
    ("schema org Product block", present only in generated text — the
    page's actual name is "Kenya Kiambu AA (Washed)") must demote."""
    identity = _identity_args(name="schema org Product block")

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tool_name = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool_name, identity)])

    async with _mock_client(
        _html_response(200, _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT))
    ) as http_client:
        draft = await draft_bean_profile_from_url(
            _MATCHING_JSON_LD_URL,
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=FunctionModel(respond),
        )
    assert draft.name == "schema org Product block"
    assert draft.field_sources["name"] == "origin_estimated"


# --- #590 slice C: _extract_page_markdown (trafilatura) ---

#: A nav-heavy page whose real product specs sit well past where a naive
#: byte-capped linear strip would have to cut (the Onyx-page failure #600's
#: bake-off surfaced) — trafilatura's boilerplate stripping must keep the
#: specs even under a cap the raw nav text alone blows past.
_NAV_HEAVY_HTML = (
    "<html><head><title>Vendor Shop</title></head><body>\n"
    "<nav><ul>"
    + "".join(
        f'<li><a href="/collections/cat-{i}">Category {i} filler nav text</a></li>'
        for i in range(20)
    )
    + "</ul></nav>\n"
    "<main><article>\n"
    "<h1>Ethiopia Guji Natural</h1>\n"
    "<p>Origin: Ethiopia. Region: Guji.</p>\n"
    "<p>Process: Natural.</p>\n"
    "<p>Roast Recommendation: Light to Medium roast.</p>\n"
    "</article></main>\n"
    "</body></html>"
)


def test_extract_page_markdown_keeps_specs_a_naive_cap_would_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Onyx-page case: nav boilerplate alone exceeds a small character
    cap, so the linear-strip fallback never reaches the product specs — but
    trafilatura strips the nav ENTIRELY, so the (much shorter) real content
    fits the SAME cap untruncated (#590 slice C, README §2)."""
    monkeypatch.setattr(bean_sourcing, "_MAX_EXTRACTED_CHARS", 300)

    linear = bean_sourcing._extract_page_text(_NAV_HEAVY_HTML)  # pyright: ignore[reportPrivateUsage]
    markdown = bean_sourcing._extract_page_markdown(_NAV_HEAVY_HTML)  # pyright: ignore[reportPrivateUsage]

    assert len(linear) == 300
    assert "Roast Recommendation" not in linear  # cut off by the naive cap

    assert markdown is not None
    assert "Ethiopia Guji Natural" in markdown  # the h1/title, via with_metadata
    assert "Roast Recommendation" in markdown  # survives — nav never ate the budget
    assert "Category 0" not in markdown  # nav boilerplate is gone, not just deferred


def test_extract_page_markdown_truncates_to_the_same_cap_as_linear_strip() -> None:
    huge_article = (
        "<html><body><article><h1>Huge Bean</h1>"
        + "".join(f"<p>Paragraph {i} of real tasting-note prose content.</p>" for i in range(2000))
        + "</article></body></html>"
    )
    markdown = bean_sourcing._extract_page_markdown(huge_article)  # pyright: ignore[reportPrivateUsage]
    assert markdown is not None
    assert len(markdown) == bean_sourcing._MAX_EXTRACTED_CHARS  # pyright: ignore[reportPrivateUsage]


def test_extract_page_markdown_returns_none_when_trafilatura_finds_nothing() -> None:
    """A JS-only/too-sparse page trafilatura cannot make sense of — the
    ``None`` branch :func:`_fetch_page_text` falls back on."""
    assert bean_sourcing._extract_page_markdown("<html><body></body></html>") is None  # pyright: ignore[reportPrivateUsage]


def test_extract_page_markdown_returns_none_when_trafilatura_returns_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _blank(*args: object, **kwargs: object) -> str:
        return "   \n  "

    # String-target setattr (matches the ``httpx.AsyncClient`` monkeypatch
    # convention above): ``trafilatura`` is re-exported implicitly, so a
    # direct ``bean_sourcing.trafilatura`` attribute reference trips
    # pyright strict's ``reportPrivateImportUsage``.
    monkeypatch.setattr("roastpilot_agent.bean_sourcing.trafilatura.extract", _blank)
    assert bean_sourcing._extract_page_markdown(_SAMPLE_HTML) is None  # pyright: ignore[reportPrivateUsage]


def test_extract_page_markdown_fails_soft_on_internal_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: object, **kwargs: object) -> str:
        raise RuntimeError("simulated trafilatura failure")

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.trafilatura.extract", _raise)
    assert bean_sourcing._extract_page_markdown(_SAMPLE_HTML) is None  # pyright: ignore[reportPrivateUsage]


def test_extract_page_markdown_xxe_payload_does_not_expand_entity_or_touch_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrors the ``_parse_html_for_json_ld`` XXE test (#590 slice B) for
    trafilatura's OWN, separate parser instance (#590 slice C, checklist
    class 4): an external-entity payload referencing a local/metadata
    address must never be fetched or expanded."""
    connect_attempts: list[object] = []

    def _spy_connect(self: socket.socket, address: object) -> None:
        connect_attempts.append(address)
        raise OSError("network access blocked in test")

    monkeypatch.setattr(socket.socket, "connect", _spy_connect)
    xxe_payload = (
        "<!DOCTYPE html [<!ENTITY xxe SYSTEM "
        '"http://169.254.169.254/latest/meta-data/">]>\n'
        "<html><body><article>"
        "<h1>Ethiopia Guji Natural</h1>"
        "<p>Origin: Ethiopia. Notes: &xxe; plus enough further prose padding "
        "for trafilatura to treat this as real article content worth "
        "keeping rather than discarding as too sparse.</p>"
        "</article></body></html>"
    )
    markdown = bean_sourcing._extract_page_markdown(xxe_payload)  # pyright: ignore[reportPrivateUsage]
    assert connect_attempts == []
    assert markdown is not None
    # The entity is NEVER expanded in HTML parsing mode — the literal,
    # unexpanded marker string comes through unchanged, not fetched content.
    assert "&xxe;" in markdown


# --- #590 slice C P1 fix: metadata frontmatter leaks the page's own
# og:url/canonical url/hostname/sitename regardless of whether a url=
# argument is passed to trafilatura.extract; sanitised down to title only ---


def test_extract_page_markdown_strips_url_hostname_sitename_leak_from_frontmatter() -> None:
    """A page's OWN ``og:url``/``<link rel="canonical">`` tag — attacker-
    influenceable content, not this module's own fetch destination — must
    never reach the LLM prompt as a code-populated-looking ``url:``/
    ``hostname:``/``sitename:`` frontmatter key, even though NO ``url=``
    argument is ever passed to ``trafilatura.extract`` (#590 slice C P1
    fix: the prior docstring's "no leak because no url= is passed" claim
    was verified WRONG)."""
    html = (
        "<html><head>"
        '<link rel="canonical" href="http://169.254.169.254/latest/meta-data/">'
        '<meta property="og:url" content="http://internal-admin.example.local/secret-path">'
        "</head><body><article>"
        "<h1>Kenya Kiambu AA (Washed)</h1>"
        "<p>Origin: Kenya. Region: Kiambu. Farm: Gakuyuini Factory.</p>"
        "<p>Process: washed. Altitude: 1,700-1,850m.</p>"
        "</article></body></html>"
    )
    markdown = bean_sourcing._extract_page_markdown(html)  # pyright: ignore[reportPrivateUsage]
    assert markdown is not None
    assert "169.254.169.254" not in markdown
    assert "internal-admin.example.local" not in markdown
    assert "url:" not in markdown
    assert "hostname:" not in markdown
    assert "sitename:" not in markdown
    # The one field with_metadata=True was enabled to recover still survives.
    assert "title: Kenya Kiambu AA (Washed)" in markdown


def test_sanitize_trafilatura_frontmatter_keeps_only_title() -> None:
    sanitize = bean_sourcing._sanitize_trafilatura_frontmatter  # pyright: ignore[reportPrivateUsage]
    markdown = (
        "---\n"
        "title: Kenya Kiambu AA\n"
        "author: Vendor Roastery\n"
        "url: http://internal.example/leak\n"
        "hostname: internal.example\n"
        "sitename: internal.example\n"
        "---\n"
        "Body text here."
    )
    assert sanitize(markdown) == "---\ntitle: Kenya Kiambu AA\n---\nBody text here."


def test_sanitize_trafilatura_frontmatter_drops_block_entirely_when_no_title() -> None:
    sanitize = bean_sourcing._sanitize_trafilatura_frontmatter  # pyright: ignore[reportPrivateUsage]
    markdown = "---\nurl: http://internal.example/leak\nhostname: internal.example\n---\nBody text."
    assert sanitize(markdown) == "Body text."


def test_sanitize_trafilatura_frontmatter_passes_through_text_without_frontmatter() -> None:
    sanitize = bean_sourcing._sanitize_trafilatura_frontmatter  # pyright: ignore[reportPrivateUsage]
    assert sanitize("Just plain body text, no frontmatter block at all.") == (
        "Just plain body text, no frontmatter block at all."
    )


def test_sanitize_trafilatura_frontmatter_passes_through_unclosed_block_unchanged() -> None:
    """Defensive branch: a string that starts like a frontmatter block but
    never closes is left untouched rather than guessed at."""
    sanitize = bean_sourcing._sanitize_trafilatura_frontmatter  # pyright: ignore[reportPrivateUsage]
    malformed = "---\ntitle: Kenya Kiambu AA\nno closing delimiter here"
    assert sanitize(malformed) == malformed


# --- #590 slice C: wired into _fetch_page_text (primary, with linear-strip fallback) ---


@pytest.mark.asyncio
async def test_fetch_page_text_uses_trafilatura_markdown_as_page_body() -> None:
    async with _mock_client(_html_response(200, _NAV_HEAVY_HTML)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/ethiopia-guji",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert "Roast Recommendation" in text
    assert "Category 0" not in text  # nav boilerplate never reaches the LLM


@pytest.mark.asyncio
async def test_fetch_page_text_falls_back_to_linear_strip_when_trafilatura_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Trafilatura finding nothing usable must not regress the page below
    the pre-slice-C linear-strip behavior (#590 slice C: fail-soft
    fallback, never worse than before)."""

    def _no_markdown(html: str) -> str | None:
        return None

    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _no_markdown)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/kenya-kiambu",
                config=BeanSourcingConfig(),
                http_client=client,
            )
        ).prompt_text
    assert text == bean_sourcing._extract_page_text(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]
    assert "Kenya Kiambu AA" in text


@pytest.mark.asyncio
async def test_fetch_page_text_bounds_a_hanging_markdown_extraction_with_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#590 slice C P1 fix: the trafilatura call runs AFTER the fetch's own
    ``asyncio.timeout`` block already closed, and under
    ``RoastService.draft_bean_from_url``'s ``_start_lock`` — SHARED with
    ``start_roast`` — so an unbounded call here would hang every roast
    start, not just this draft. A pathologically slow/hanging
    ``_extract_page_markdown`` must not hang past ``config.fetch_timeout_seconds``.

    #590 slice C P2 fix (Codex fold, #608): on that timeout the draft FALLS
    BACK to the linear-strip pass, exactly like the ``None``/exception
    cases — it must NOT raise ``BeanFetchError`` (a 422). Before this
    slice every page used the fast, synchronous linear-strip path; a
    slow-to-parse page timing out here must not regress that page from
    "draft succeeds via linear-strip" to a 422 that didn't exist before
    this slice. The lock-hold bound still holds (the wait is capped, only
    the OUTCOME changed from fail to fall back).

    #607: the hung worker now runs on the DEDICATED
    ``bean-sourcing-parse`` pool, not the process's shared default
    executor — see the ``test_extract_page_markdown_bounded_*`` tests
    below for that pool's own admission-control/isolation/recovery
    coverage. This test stays a pure integration check: the
    ``_fetch_page_text`` caller must still fall back promptly regardless
    of which pool the hang happens on.

    Drains the dedicated pool's admission counter back to zero (via
    :func:`_await_condition`) before returning: this test's own hung
    worker keeps running for up to a real ~1s AFTER the assertions below
    (the timeout only bounds the await, not the thread) — draining here,
    the same way the ``test_extract_page_markdown_bounded_*`` tests
    below drain their own occupying tasks, prevents that lingering
    worker's EVENTUAL release from firing during whichever test runs
    next and mutating ITS view of the shared counter."""

    _reset_parse_pool_state()

    def _hangs(html: str) -> str | None:
        # A REAL (synchronous) sleep — this runs on the dedicated
        # bean-sourcing-parse pool (#607), mirroring a genuinely
        # pathological/slow parse; asyncio.timeout can only stop the
        # AWAIT, not this thread, so it keeps running in the background
        # after the test's own timeout fires — kept short so that
        # residual cost stays negligible, and contained to the dedicated
        # pool alone rather than the shared default executor.
        time.sleep(1.0)
        return "should never be observed"  # pragma: no cover

    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _hangs)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        started = time.monotonic()
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                "https://vendor.example/products/kenya-kiambu",
                config=BeanSourcingConfig(fetch_timeout_seconds=0.1),
                http_client=client,
            )
        ).prompt_text
        elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"markdown-extraction timeout did not bound the call: {elapsed:.2f}s"
    # Falls back to the SAME linear-strip text the None/exception paths
    # use — the draft proceeds, it does not fail.
    assert text == bean_sourcing._extract_page_text(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]
    assert "Kenya Kiambu AA" in text
    # Drain: let the still-running hung worker actually finish and release
    # its slot before this test returns (see the docstring above).
    await _await_condition(
        lambda: bean_sourcing._inflight_parse_count == 0,  # pyright: ignore[reportPrivateUsage]
        timeout=2.0,
    )


# --- #607: dedicated, bounded executor + admission control for the
# untrusted trafilatura markdown parse ---
#
# Every test below drives a REAL (synchronous, cross-thread) hang via
# ``threading.Event`` — never a fake/mocked future — so the admission
# counter and the dedicated pool's actual worker occupancy are exercised
# for real, not simulated. Every blocking fake bounds its own wait
# (``event.wait(timeout=...)``) as a safety ceiling: even if a test's own
# ``finally``/cleanup failed to signal release, the underlying thread
# still returns on its own well within the test suite's timeout, so a
# broken test cannot leave a genuinely un-joinable non-daemon thread
# blocking process exit. Ad-hoc raw/warmup executors and futures created
# directly in a test (bypassing the module's own admission wrapper) are
# never explicitly ``.shutdown()``/torn down either — cleanup relies on
# GC finalizers and eventual process exit, matching the module's own
# process-lifetime singleton rather than adding per-test teardown.


def _reset_parse_pool_state() -> None:
    """Reset the module-level admission counter to zero.

    Defensive: guards each test below against a prior failure elsewhere
    in the suite having left :data:`bean_sourcing._inflight_parse_count`
    non-zero. The dedicated executor singleton itself is intentionally
    NOT torn down/recreated — matching production, where it lives for
    the whole process — only the counter is reset.
    """
    bean_sourcing._inflight_parse_count = 0  # pyright: ignore[reportPrivateUsage]


async def _await_condition(predicate: Callable[[], bool], *, timeout: float = 2.0) -> None:
    """Poll ``predicate`` on a short ``asyncio.sleep`` cadence until it is
    ``True`` or ``timeout`` elapses, without ever blocking the event loop
    (a plain busy-``while`` would starve the worker threads' own
    ``call_soon_threadsafe`` callbacks of a chance to run)."""
    started = time.monotonic()
    while not predicate():
        if time.monotonic() - started > timeout:
            raise AssertionError(f"condition not met within {timeout}s")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_skips_immediately_when_pool_saturated() -> None:
    """The cap works: with both dedicated-pool workers genuinely still
    busy (simulating orphaned hung parses from prior timeouts), the NEXT
    call must not queue behind them and wait out its own timeout — it
    should skip the markdown attempt and return ``None`` almost
    immediately (#607)."""
    _reset_parse_pool_state()
    release = threading.Event()
    entered = threading.Event()
    entered_count = 0
    count_lock = threading.Lock()

    def _hangs_until_released(html: str) -> str | None:
        nonlocal entered_count
        with count_lock:
            entered_count += 1
            if entered_count >= bean_sourcing._MAX_CONCURRENT_PARSES:  # pyright: ignore[reportPrivateUsage]
                entered.set()
        release.wait(timeout=5.0)  # safety ceiling — always released below
        return None

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _hangs_until_released):
        occupying = [
            asyncio.create_task(
                bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                    "<html></html>", timeout_seconds=10.0
                )
            )
            for _ in range(bean_sourcing._MAX_CONCURRENT_PARSES)  # pyright: ignore[reportPrivateUsage]
        ]
        try:
            # Wait until BOTH workers have genuinely started (are
            # occupying a real dedicated-pool thread), not merely
            # "submitted" — otherwise the assertion below could race
            # ahead of the pool actually filling up.
            await _await_condition(entered.is_set)
            assert (
                bean_sourcing._inflight_parse_count  # pyright: ignore[reportPrivateUsage]
                == bean_sourcing._MAX_CONCURRENT_PARSES  # pyright: ignore[reportPrivateUsage]
            )

            started_at = time.monotonic()
            result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                "<html></html>", timeout_seconds=10.0
            )
            elapsed = time.monotonic() - started_at
            assert result is None
            assert elapsed < 1.0, f"admission control did not skip immediately: {elapsed:.2f}s"
            # The skip path never touches the counter — still exactly the
            # cap, not cap+1.
            assert (
                bean_sourcing._inflight_parse_count  # pyright: ignore[reportPrivateUsage]
                == bean_sourcing._MAX_CONCURRENT_PARSES  # pyright: ignore[reportPrivateUsage]
            )
        finally:
            release.set()
            await asyncio.gather(*occupying)


@pytest.mark.asyncio
async def test_unrelated_to_thread_work_unaffected_by_saturated_parse_pool() -> None:
    """Orphan containment: unrelated ``asyncio.to_thread`` work (the
    process's SHARED default executor — what ``api.py``'s config
    load/persistence and device-enumeration calls use) must still
    complete promptly while the DEDICATED parse pool is fully saturated
    (#607) — proving a leak is contained to the dedicated pool alone."""
    _reset_parse_pool_state()
    release = threading.Event()
    entered = threading.Event()
    entered_count = 0
    count_lock = threading.Lock()

    def _hangs_until_released(html: str) -> str | None:
        nonlocal entered_count
        with count_lock:
            entered_count += 1
            if entered_count >= bean_sourcing._MAX_CONCURRENT_PARSES:  # pyright: ignore[reportPrivateUsage]
                entered.set()
        release.wait(timeout=5.0)
        return None

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _hangs_until_released):
        occupying = [
            asyncio.create_task(
                bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                    "<html></html>", timeout_seconds=10.0
                )
            )
            for _ in range(bean_sourcing._MAX_CONCURRENT_PARSES)  # pyright: ignore[reportPrivateUsage]
        ]
        try:
            await _await_condition(entered.is_set)
            assert (
                bean_sourcing._inflight_parse_count  # pyright: ignore[reportPrivateUsage]
                == bean_sourcing._MAX_CONCURRENT_PARSES  # pyright: ignore[reportPrivateUsage]
            )

            started_at = time.monotonic()
            result = await asyncio.to_thread(lambda: 21 + 21)
            elapsed = time.monotonic() - started_at
            assert result == 42
            assert elapsed < 1.0, (
                f"unrelated default-executor work was delayed by the saturated "
                f"dedicated pool: {elapsed:.2f}s"
            )
        finally:
            release.set()
            await asyncio.gather(*occupying)


@pytest.mark.asyncio
async def test_parse_pool_recovers_once_hung_workers_complete() -> None:
    """Recovery: once the previously-hung workers actually complete and
    release their slots, markdown extraction works again (#607) — the
    admission counter is not a permanent trip, only a reflection of
    current worker occupancy."""
    _reset_parse_pool_state()
    release = threading.Event()
    entered = threading.Event()
    entered_count = 0
    count_lock = threading.Lock()

    def _hangs_until_released(html: str) -> str | None:
        nonlocal entered_count
        with count_lock:
            entered_count += 1
            if entered_count >= bean_sourcing._MAX_CONCURRENT_PARSES:  # pyright: ignore[reportPrivateUsage]
                entered.set()
        release.wait(timeout=5.0)
        return None

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _hangs_until_released):
        occupying = [
            asyncio.create_task(
                bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                    "<html></html>", timeout_seconds=10.0
                )
            )
            for _ in range(bean_sourcing._MAX_CONCURRENT_PARSES)  # pyright: ignore[reportPrivateUsage]
        ]
        await _await_condition(entered.is_set)
        release.set()
        await asyncio.gather(*occupying)

    # The completion callbacks hop back onto the loop via
    # call_soon_threadsafe — give them a turn to actually run.
    await _await_condition(
        lambda: bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
    )

    def _recovered(html: str) -> str | None:
        return "recovered markdown"

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _recovered):
        result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
            "<html></html>", timeout_seconds=5.0
        )
    assert result == "recovered markdown"


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_normal_path_returns_markdown() -> None:
    """Regression: the ordinary (non-hung, non-saturated) draft path is
    unchanged — a normal, fast parse still returns its markdown through
    the new dedicated-pool dispatch (#607)."""
    _reset_parse_pool_state()
    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        _SAMPLE_HTML, timeout_seconds=5.0
    )
    assert result == bean_sourcing._extract_page_markdown(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_saturation_log_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The saturated-pool skip path logs a warning naming the occupied/
    capacity counts (#607) — observability for an operator diagnosing a
    stuck draft."""
    _reset_parse_pool_state()
    bean_sourcing._inflight_parse_count = (  # pyright: ignore[reportPrivateUsage]
        bean_sourcing._MAX_CONCURRENT_PARSES  # pyright: ignore[reportPrivateUsage]
    )
    try:
        with caplog.at_level(logging.WARNING, logger="roastpilot_agent.bean_sourcing"):
            result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                "<html></html>", timeout_seconds=5.0
            )
        assert result is None
        assert any("saturated" in record.message for record in caplog.records)
    finally:
        _reset_parse_pool_state()


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_timeout_falls_back_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A genuine (bounded, not pool-saturation) timeout still falls back
    to ``None`` and logs at debug — the pre-#607 timeout behavior,
    unchanged by the dedicated-pool dispatch."""
    _reset_parse_pool_state()
    release = threading.Event()

    def _hangs(html: str) -> str | None:
        release.wait(timeout=5.0)
        return None

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _hangs):
        try:
            with caplog.at_level(logging.DEBUG, logger="roastpilot_agent.bean_sourcing"):
                result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                    "<html></html>", timeout_seconds=0.1
                )
            assert result is None
            assert any("deadline" in record.message for record in caplog.records)
        finally:
            release.set()
            await _await_condition(
                lambda: bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
            )


def test_get_parse_executor_returns_the_same_singleton_across_calls() -> None:
    """:func:`_get_parse_executor`'s lazy singleton is created once and
    reused — a fresh executor per call would defeat the whole point of a
    bounded pool (#607)."""
    first = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]
    second = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]
    assert first is second


def test_parse_wrapper_entry_seam_is_a_noop() -> None:
    """#607 fold 5: unpatched, :func:`bean_sourcing._parse_wrapper_entry_seam`
    is a genuine no-op — production behavior is unaffected by its
    presence; only a test that deliberately monkeypatches it (see the
    reclaim-handshake test) changes anything."""
    assert bean_sourcing._parse_wrapper_entry_seam() is None  # pyright: ignore[reportPrivateUsage]


def test_release_parse_slot_once_clamps_at_zero() -> None:
    """Defensive floor: :func:`_release_parse_slot_once` never drives the
    counter negative, even if called with the counter already at zero
    (#607 fold 4 — should not happen in practice, since every increment
    has exactly one matching token release, but the floor is cheap
    insurance)."""
    _reset_parse_pool_state()
    token = bean_sourcing._ParseSlotToken()  # pyright: ignore[reportPrivateUsage]
    try:
        bean_sourcing._release_parse_slot_once(token)  # pyright: ignore[reportPrivateUsage]
        assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
    finally:
        _reset_parse_pool_state()


def test_release_parse_slot_once_is_idempotent() -> None:
    """#607 fold 4: a SECOND call for the same token (e.g. the
    submission-failure path racing the wrapper's own ``finally``) must
    decrement at most once — the token's own ``released`` flag, not the
    counter's floor-at-zero clamp, is what enforces this (the counter
    could otherwise be legitimately non-zero from an unrelated call and a
    double-decrement would silently steal that call's slot)."""
    _reset_parse_pool_state()
    bean_sourcing._inflight_parse_count = 2  # pyright: ignore[reportPrivateUsage]
    token = bean_sourcing._ParseSlotToken()  # pyright: ignore[reportPrivateUsage]
    try:
        bean_sourcing._release_parse_slot_once(token)  # pyright: ignore[reportPrivateUsage]
        bean_sourcing._release_parse_slot_once(token)  # pyright: ignore[reportPrivateUsage]
        assert bean_sourcing._inflight_parse_count == 1  # pyright: ignore[reportPrivateUsage]
    finally:
        _reset_parse_pool_state()


def test_release_parse_slot_once_runs_correctly_from_a_foreign_thread() -> None:
    """#607 fold 4: the token-guarded release has no event-loop
    dependency at all — running it directly from a plain background
    thread (simulating the wrapper's own ``finally``, which always runs
    on a worker thread) still releases the slot correctly."""
    _reset_parse_pool_state()
    bean_sourcing._inflight_parse_count = 1  # pyright: ignore[reportPrivateUsage]
    token = bean_sourcing._ParseSlotToken()  # pyright: ignore[reportPrivateUsage]
    errors: list[BaseException] = []

    def _run_release_with_no_event_loop() -> None:
        # A plain background thread — deliberately never creates or sets
        # an asyncio event loop of its own.
        try:
            bean_sourcing._release_parse_slot_once(token)  # pyright: ignore[reportPrivateUsage]
        except BaseException as exc:  # pragma: no cover - regression guard only
            errors.append(exc)

    thread = threading.Thread(target=_run_release_with_no_event_loop)
    thread.start()
    thread.join(timeout=5.0)
    assert not errors
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]


class _SubmitRaisingExecutor:
    """A test double whose ``submit`` raises immediately — simulating a
    thread-limit ``OSError`` (or any other submission-time failure) on a
    resource-constrained host (#607 fold 1). ``shutdown`` is a no-op: this
    fake never creates real threads/a real queue for
    :func:`_replace_poisoned_parse_executor` to drain."""

    def submit(
        self, func: Callable[[str], str | None], html: str
    ) -> concurrent.futures.Future[str | None]:
        raise OSError("can't start new thread (simulated, #607 fold 1 test)")

    def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
        pass


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_submission_failure_falls_back_and_frees_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex P2 + claude low fold: a submission failure must (a) fall back
    to linear-strip rather than raise past this function's fail-soft
    contract, and (b) release the slot it had just reserved — capacity
    for the VERY NEXT call must stay intact, not silently shrink by one
    forever (#607 fold 1). See
    ``test_extract_page_markdown_bounded_replaces_poisoned_executor_and_cancels_hidden_queue``
    below for fold 2's executor-replacement/hidden-queue coverage, which
    needs a REAL ``ThreadPoolExecutor`` rather than this simple fake."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_get_parse_executor", lambda: _SubmitRaisingExecutor())
    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html></html>", timeout_seconds=5.0
    )
    assert result is None
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]

    # Capacity intact: restore the REAL executor and confirm a normal
    # parse still succeeds right afterwards — the failed submission above
    # must not have permanently eaten a slot.
    monkeypatch.undo()
    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        _SAMPLE_HTML, timeout_seconds=5.0
    )
    assert result == bean_sourcing._extract_page_markdown(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_replaces_poisoned_executor_and_cancels_hidden_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 2 (Codex round 2), hardened by the qa pass on PR #629:
    the ORIGINAL version of this test patched ``_adjust_thread_count`` to
    raise on the ONLY submission the executor ever saw — no worker thread
    was ever created, so its "settle window gives a live worker every
    chance" claim was FALSE (the assertion passed vacuously; there was
    nothing that could ever have popped the item). This version submits a
    gated warmup FIRST (fold-4's pattern) so worker-1 is a genuinely LIVE,
    alive thread throughout the whole failure/replace/drain sequence —
    proving ``cancel_futures=True`` actually drains the hidden item out
    from under a pool that HAS a real thread, not an empty one.

    ``ThreadPoolExecutor.submit()`` puts its work item on the internal
    queue BEFORE calling ``_adjust_thread_count()`` — the call that can
    raise — so a submission failure leaves a HIDDEN, queued work item
    behind on a release-only handler's executor. Asserts the sentinel
    NEVER executes (checked again after worker-1 is released and given a
    settle window, with nothing left on the drained queue for it to pick
    up), the singleton executor was REPLACED (new object identity), and
    the very next call parses normally on the fresh executor with full
    capacity."""
    _reset_parse_pool_state()
    # Force a fresh, otherwise-untouched singleton so this test owns the
    # executor end-to-end and isn't sharing prior submissions with
    # whichever executor an earlier test happened to create.
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    original_executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]

    warmup_gate = threading.Event()

    def _warmup() -> str:
        warmup_gate.wait(timeout=5.0)
        return "warmup done"

    # Creates worker-1 as a REAL, alive thread — busy on the gate for the
    # whole failure/replace/drain sequence below, so it cannot race to
    # dequeue the hidden item itself (ad-hoc, GC-finalized — see the
    # section note above).
    original_executor.submit(_warmup)

    executed = threading.Event()

    def _sentinel_parse(html: str) -> str | None:
        # Must NEVER run — if the hidden queued item survives
        # cancel_futures=True, it would eventually be picked up here.
        executed.set()
        return "should never be observed"  # pragma: no cover

    def _raising_adjust_thread_count() -> None:
        # Reproduces the exact CPython failure shape: submit() has
        # ALREADY called self._work_queue.put(w) by the time this raises
        # (verified directly against ThreadPoolExecutor.submit's source),
        # so the sentinel's work item is genuinely enqueued first.
        raise OSError("can't start new thread (simulated, #607 fold 2 test)")

    real_extract_page_markdown = bean_sourcing._extract_page_markdown  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(original_executor, "_adjust_thread_count", _raising_adjust_thread_count)
    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _sentinel_parse)

    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html></html>", timeout_seconds=5.0
    )
    assert result is None
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]

    # The singleton was REPLACED, not merely reused: a fresh executor
    # object, distinct from the poisoned one.
    replaced_executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]
    assert replaced_executor is not original_executor

    # Release worker-1 NOW — after the cancel-and-replace above has
    # already run — and give it (a genuinely live thread) every remaining
    # chance to touch the (already-drained) queue.
    warmup_gate.set()
    await asyncio.sleep(0.2)
    assert not executed.is_set(), (
        "the hidden queued parse executed despite cancel_futures=True — "
        "the poisoned executor was not properly drained"
    )

    # Recovery: the NEXT call, on the SAME fresh (replaced) executor,
    # parses normally with full capacity. Restore ONLY the real
    # _extract_page_markdown here (rather than a blanket monkeypatch.undo()
    # — that would also revert bean_sourcing._parse_executor back to its
    # PRE-test value, discarding the very replacement this assertion means
    # to exercise).
    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", real_extract_page_markdown)
    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        _SAMPLE_HTML, timeout_seconds=5.0
    )
    assert result == real_extract_page_markdown(_SAMPLE_HTML)
    assert bean_sourcing._get_parse_executor() is replaced_executor  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_collateral_cancellation_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 3 (claude-review on PR #629's rebased head): a submission
    failure's ``cancel_futures=True`` replacement can collaterally cancel
    a DIFFERENT, concurrent call's still-queued item on the SAME executor.
    That call's own task was never told to cancel — it must fall back
    like any other parse failure, not let ``CancelledError`` escape.

    Call A is admitted and submitted (queued) on a fresh executor whose
    ``_adjust_thread_count`` is a no-op, so its item genuinely never gets
    picked up by a worker (deterministic "still queued", no timing race).
    Call B's submission is then made to raise, triggering
    ``_replace_poisoned_parse_executor`` — which ``shutdown``s the SAME
    executor with ``cancel_futures=True``, collaterally cancelling A's
    queued item."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]

    def _never_starts_a_thread() -> None:
        # A no-op _adjust_thread_count: submitted items sit on the queue
        # forever, with no worker ever spawned to pick them up.
        pass

    monkeypatch.setattr(executor, "_adjust_thread_count", _never_starts_a_thread)

    task_a = asyncio.create_task(
        bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
            "<html>A</html>", timeout_seconds=10.0
        )
    )
    # Let call A run up to its own await point — its item is now queued
    # (admitted, submitted, never picked up thanks to the no-op above).
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bean_sourcing._inflight_parse_count == 1  # pyright: ignore[reportPrivateUsage]

    def _raising_adjust_thread_count() -> None:
        raise OSError("can't start new thread (simulated, #607 fold 3 test)")

    monkeypatch.setattr(executor, "_adjust_thread_count", _raising_adjust_thread_count)
    result_b = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html>B</html>", timeout_seconds=5.0
    )
    assert result_b is None  # B's own submission failure falls back too

    # A's collaterally-cancelled item must ALSO fall back — never raise —
    # and release its slot.
    result_a = await task_a
    assert result_a is None
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_genuine_task_cancellation_propagates() -> None:
    """#607 fold 3: cancelling the TASK awaiting the parse (e.g. a client
    disconnect) must propagate ``CancelledError`` — never be swallowed as
    if it were a collateral cancellation — and the slot is still released
    via the done-callback once the (already-running) worker actually
    finishes."""
    _reset_parse_pool_state()
    started = threading.Event()
    release = threading.Event()

    def _blocks_until_released(html: str) -> str | None:
        started.set()
        release.wait(timeout=5.0)
        return "should not be observed by the cancelled caller"

    with unittest.mock.patch.object(
        bean_sourcing, "_extract_page_markdown", _blocks_until_released
    ):
        task = asyncio.create_task(
            bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                "<html></html>", timeout_seconds=10.0
            )
        )
        await _await_condition(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The worker was ALREADY RUNNING when cancelled (concurrent.futures
        # cannot cancel a running item), so it keeps going in the
        # background — release it and confirm the slot still comes back.
        release.set()
        await _await_condition(
            lambda: bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
        )


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_genuine_cancellation_of_a_queued_item_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 3 regression guard: a genuine task cancellation of an item
    that has NOT started running yet also cancels ``concurrent_future``
    (asyncio's own future-chaining propagates the Task's cancel down to
    it) — so ``concurrent_future.cancelled()`` alone cannot distinguish
    this from a collateral cancellation; only ``Task.cancelling() > 0``
    can. Without that check, this exact case would be misclassified as
    collateral and silently swallowed instead of propagating."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]

    def _never_starts_a_thread() -> None:
        pass

    monkeypatch.setattr(executor, "_adjust_thread_count", _never_starts_a_thread)

    task = asyncio.create_task(
        bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
            "<html></html>", timeout_seconds=10.0
        )
    )
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert bean_sourcing._inflight_parse_count == 1  # pyright: ignore[reportPrivateUsage]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_hidden_item_started_by_existing_worker_keeps_its_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 4 (Codex round 3): an EXISTING idle worker can dequeue
    and START a hidden queued item (left behind by a failed ``submit()``)
    BEFORE ``_replace_poisoned_parse_executor``'s ``cancel_futures=True``
    drain reaches it. A started item can never be cancelled and (pre-fold
    4) never had a done-callback attached either (``submit()`` raised
    before ``add_done_callback`` could even run) — an unconditional
    release would silently grow the 2-worker bound to 3 with an
    untracked, unaccounted-for hung parse.

    Deterministically reproduces the race (no reliance on OS scheduling
    luck): worker-1 is warmed up and kept BUSY on a gate so the hidden
    item sits on the queue; ``_replace_poisoned_parse_executor`` is
    wrapped to release that gate and WAIT for the hidden item to signal
    it has actually started running, before letting the real
    shutdown/cancel_futures call proceed — proving the shutdown reaches a
    queue the item has ALREADY been dequeued from."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]

    warmup_gate = threading.Event()

    def _warmup() -> str:
        warmup_gate.wait(timeout=5.0)
        return "warmup done"

    # Creates worker-1 as a real thread, BUSY (blocked on the gate) —
    # not yet idle, so the hidden item below sits on the queue rather
    # than being dequeued immediately.
    executor.submit(_warmup)

    hidden_started = threading.Event()
    hidden_release = threading.Event()

    def _hidden_parse(html: str) -> str | None:
        hidden_started.set()
        hidden_release.wait(timeout=5.0)
        return "hidden parse result"

    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _hidden_parse)

    real_adjust_thread_count = executor._adjust_thread_count  # pyright: ignore[reportPrivateUsage]

    def _raising_adjust_thread_count() -> None:
        raise OSError("can't start new thread (simulated, #607 fold 4 test)")

    monkeypatch.setattr(executor, "_adjust_thread_count", _raising_adjust_thread_count)

    real_replace = bean_sourcing._replace_poisoned_parse_executor  # pyright: ignore[reportPrivateUsage]

    def _replace_after_worker_grabs_item(
        poisoned: concurrent.futures.ThreadPoolExecutor,
    ) -> None:
        # Release worker-1's warmup gate so it returns to the idle loop
        # and dequeues the hidden item BEFORE the drain-and-cancel below
        # can reach it — deterministic, not a hope-for-the-best race.
        warmup_gate.set()
        assert hidden_started.wait(timeout=2.0), (
            "worker-1 never dequeued and started the hidden item in time"
        )
        real_replace(poisoned)

    monkeypatch.setattr(
        bean_sourcing, "_replace_poisoned_parse_executor", _replace_after_worker_grabs_item
    )

    result_hidden = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html>hidden</html>", timeout_seconds=5.0
    )
    assert result_hidden is None  # falls back to linear-strip like any failure

    # NOT over-freed: the hidden parse is genuinely running (worker-1),
    # so the counter still correctly reports it occupied.
    assert bean_sourcing._inflight_parse_count == 1  # pyright: ignore[reportPrivateUsage]

    # A legitimate second admission (worker-2, spawned normally — restore
    # REAL thread-creation on the SAME executor first; a blanket
    # monkeypatch.undo() here would also revert bean_sourcing._parse_executor
    # to its pre-test value, losing track of the very executor the hidden
    # parse is still running on) must still be admitted...
    monkeypatch.setattr(executor, "_adjust_thread_count", real_adjust_thread_count)
    legit_started = threading.Event()
    legit_release = threading.Event()

    def _legit_parse(html: str) -> str | None:
        legit_started.set()
        legit_release.wait(timeout=5.0)
        return "legit parse result"

    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _legit_parse)
    legit_task = asyncio.create_task(
        bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
            "<html>legit</html>", timeout_seconds=5.0
        )
    )
    await _await_condition(legit_started.is_set)
    assert bean_sourcing._inflight_parse_count == 2  # pyright: ignore[reportPrivateUsage]

    # ...but a THIRD admission must be refused: the bound stays at 2,
    # never silently growing to 3 with the hidden parse untracked.
    result_third = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html>third</html>", timeout_seconds=5.0
    )
    assert result_third is None

    # Release both — the slot returns once each parse actually completes.
    hidden_release.set()
    legit_release.set()
    legit_result = await legit_task
    assert legit_result == "legit parse result"
    await _await_condition(
        lambda: bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
    )


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_reclaim_prevents_worker_from_ever_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 5 (Codex round 4, SjHAl): a worker can dequeue the hidden
    item and mark its future RUNNING, then be DESCHEDULED before the
    wrapper ever reaches ``token.lock``. Deterministically reproduces
    that exact window via :func:`bean_sourcing._parse_wrapper_entry_seam`
    (a no-op-in-production test seam): the seam pauses the worker thread
    INSIDE the wrapper, before it ever touches the lock, while the
    submission-failure path reclaims the slot on the main coroutine.
    Asserts the parse body NEVER executes once reclaimed (not merely that
    the slot is released) and the count stays exact throughout — no leak,
    no over-free."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]

    warmup_gate = threading.Event()

    def _warmup() -> str:
        warmup_gate.wait(timeout=5.0)
        return "warmup done"

    executor.submit(_warmup)  # worker-1, busy on the gate

    parse_executed = threading.Event()

    def _sentinel_parse(html: str) -> str | None:
        parse_executed.set()
        return "should never run"  # pragma: no cover

    monkeypatch.setattr(bean_sourcing, "_extract_page_markdown", _sentinel_parse)

    seam_entered = threading.Event()
    seam_release = threading.Event()
    seam_resumed = threading.Event()

    def _seam() -> None:
        # Simulates the worker being descheduled right after dequeuing
        # the item (future already RUNNING) but before it ever reaches
        # the wrapper's own token.lock acquisition. seam_resumed is set
        # AFTER the wait returns — i.e. once the worker thread has
        # genuinely resumed executing, just before the seam itself
        # returns control to the wrapper's own handshake check.
        seam_entered.set()
        seam_release.wait(timeout=5.0)
        seam_resumed.set()

    monkeypatch.setattr(bean_sourcing, "_parse_wrapper_entry_seam", _seam)

    def _raising_adjust_thread_count() -> None:
        raise OSError("can't start new thread (simulated, #607 fold 5 test)")

    monkeypatch.setattr(executor, "_adjust_thread_count", _raising_adjust_thread_count)

    real_replace = bean_sourcing._replace_poisoned_parse_executor  # pyright: ignore[reportPrivateUsage]

    def _replace_after_worker_enters_seam(
        poisoned: concurrent.futures.ThreadPoolExecutor,
    ) -> None:
        # Release worker-1 so it dequeues the hidden item and enters the
        # wrapper's seam — confirmed via seam_entered — BEFORE the real
        # replace/reclaim logic below runs.
        warmup_gate.set()
        assert seam_entered.wait(timeout=2.0), "worker never reached the wrapper's seam in time"
        real_replace(poisoned)

    monkeypatch.setattr(
        bean_sourcing, "_replace_poisoned_parse_executor", _replace_after_worker_enters_seam
    )

    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        "<html>hidden</html>", timeout_seconds=5.0
    )
    assert result is None
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]

    # Let the worker resume past the seam — it must observe reclaimed and
    # bail out WITHOUT ever calling the parse. Assert on the explicit
    # "resumed" signal, not a blind sleep: by the time seam_resumed
    # fires, the worker has already made (or is making) its handshake
    # decision, so checking immediately after is deterministic.
    seam_release.set()
    assert seam_resumed.wait(timeout=2.0), "worker never resumed past the seam in time"
    assert not parse_executed.is_set(), (
        "the reclaimed item executed its parse body despite the handshake"
    )
    assert bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_timeout_while_queued_releases_no_leak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#607 fold 5 (Codex round 4, SjHAo): ``asyncio.timeout`` cancelling
    the wrapped future while the underlying ``concurrent.futures.Future``
    is still PENDING (never started, still queued) means
    ``_run_and_release``'s own ``finally`` NEVER runs — the old
    done-callback covered this, the wrapper-finally alone does not.
    Without the fold-5 fix, EACH timeout-while-queued event leaks one
    slot; two leaks alone would permanently saturate the 2-worker pool.
    Repeats the scenario 3x to prove there is no cumulative leak."""
    _reset_parse_pool_state()
    monkeypatch.setattr(bean_sourcing, "_parse_executor", None)
    executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]
    real_adjust_thread_count = executor._adjust_thread_count  # pyright: ignore[reportPrivateUsage]

    def _never_starts_a_thread() -> None:
        # No worker is EVER spawned to dequeue the item — it sits on the
        # queue, genuinely PENDING, until our own asyncio.timeout cancels
        # it (never via a race with a worker actually picking it up).
        pass

    monkeypatch.setattr(executor, "_adjust_thread_count", _never_starts_a_thread)

    for attempt in range(3):
        result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
            f"<html>attempt-{attempt}</html>", timeout_seconds=0.05
        )
        assert result is None, f"attempt {attempt} did not fall back"
        assert (
            bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
        ), f"attempt {attempt} leaked a slot"

    # A follow-up admission on a HEALTHY (real thread-creation restored)
    # executor still succeeds at full capacity — not silently shrunk by
    # the three repeated timeouts above.
    monkeypatch.setattr(executor, "_adjust_thread_count", real_adjust_thread_count)
    result = await bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
        _SAMPLE_HTML, timeout_seconds=5.0
    )
    assert result == bean_sourcing._extract_page_markdown(_SAMPLE_HTML)  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_extract_page_markdown_bounded_timeout_while_running_keeps_slot() -> None:
    """#607 (qa pass on PR #629): the one release-enumeration branch that
    had no dedicated test of its own — a still-RUNNING item's own wrapper
    releases the slot, never the ``TimeoutError`` handler.

    Warms the executor with a throwaway submission first so an ALREADY
    IDLE real worker thread picks up the call below in low-single-digit
    milliseconds (not a freshly-spawned OS thread's own startup latency),
    keeping the "genuinely running before the deadline" race practically
    zero without needing a long timeout. A ``started`` event confirms the
    real worker actually entered the parse callable before asserting
    anything about the timeout. Once ``timeout_seconds`` elapses while the
    parse is still blocked, asserts the call falls back (``None``) and —
    because ``concurrent.futures.Future.cancel()`` cannot succeed on an
    already-RUNNING item — the counter stays occupied immediately after
    the timeout fires (the bound is enforced against the hung-but-running
    parse, not silently freed). Only once the gate is released does the
    wrapper's own ``finally`` return the counter to 0."""
    _reset_parse_pool_state()
    warmup_executor = bean_sourcing._get_parse_executor()  # pyright: ignore[reportPrivateUsage]
    warmup_executor.submit(lambda: None).result(timeout=5.0)

    started = threading.Event()
    release = threading.Event()

    def _blocks_once_started(html: str) -> str | None:
        started.set()
        release.wait(timeout=5.0)
        return "should not be observed"  # pragma: no cover

    with unittest.mock.patch.object(bean_sourcing, "_extract_page_markdown", _blocks_once_started):
        task = asyncio.create_task(
            bean_sourcing._extract_page_markdown_bounded(  # pyright: ignore[reportPrivateUsage]
                "<html></html>", timeout_seconds=0.2
            )
        )
        await _await_condition(started.is_set)

        result = await task
        assert result is None
        # RUNNING (not PENDING) when the timeout fired: cancel() cannot
        # have succeeded, so the slot correctly stays reserved here — not
        # released by the TimeoutError handler.
        assert bean_sourcing._inflight_parse_count == 1  # pyright: ignore[reportPrivateUsage]

        release.set()
        await _await_condition(
            lambda: bean_sourcing._inflight_parse_count == 0  # pyright: ignore[reportPrivateUsage]
        )


@pytest.mark.asyncio
async def test_fetch_page_text_prepends_json_ld_ahead_of_trafilatura_markdown() -> None:
    """The slice-B JSON-LD prepend still lands ahead of the (now
    trafilatura-produced) page-body text (#590 slice C)."""
    page = _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT).replace(
        "<p>Tasting notes: blackcurrant, tomato, bright acidity.</p>",
        "<p>Tasting notes: blackcurrant, tomato, bright acidity.</p>"
        "<p>Roast Recommendation: filler prose so trafilatura keeps this paragraph too.</p>",
    )
    async with _mock_client(_html_response(200, page)) as client:
        text = (
            await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
                _MATCHING_JSON_LD_URL, config=BeanSourcingConfig(), http_client=client
            )
        ).prompt_text
    expected_markdown = bean_sourcing._extract_page_markdown(page)  # pyright: ignore[reportPrivateUsage]
    assert expected_markdown is not None
    json_ld_index = text.find("Structured data found in this page's JSON-LD")
    markdown_index = text.find(expected_markdown)
    assert json_ld_index == 0
    assert markdown_index > json_ld_index
    assert "KE-KIAMBU-AA" in text
    assert "Roast Recommendation" in text


# --- draft_bean_profile_from_url: full pipeline (fetch + extract + draft) ---


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_end_to_end() -> None:
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        draft = await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya-kiambu",
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=_function_model_returning(_identity_args()),
        )
    assert draft.name == "Kenya Kiambu AA (Washed)"
    assert draft.source_url == "https://vendor.example/products/kenya-kiambu"
    assert draft.processing == "washed"
    assert draft.field_sources["bean_varietal"] == "on_page"
    assert draft.field_sources["target_development_percent"] == "origin_estimated"
    assert draft.default_bean_weight_grams == 250.0


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_threads_extraction_timeout_from_sourcing_config() -> (
    None
):
    """#590 slice A, full pipeline: a short
    ``sourcing_config.extraction_timeout_seconds`` reaches the extraction
    call even though ``advisor_config.timeout_seconds`` is long — proves
    the decoupling holds end-to-end, not just at the unit level. Also #613,
    full pipeline: a timeout is DEPENDENCY-origin."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionUnavailableError, match="deadline"):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya-kiambu",
                advisor_config=AdvisorConfig(timeout_seconds=100.0),
                sourcing_config=BeanSourcingConfig(extraction_timeout_seconds=0.05),
                http_client=http_client,
                model=_function_model_hanging(),
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_threads_model_slug_from_sourcing_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#590 slice A, full pipeline: an EXPLICIT ``sourcing_config.model_slug``
    (not ``advisor_config.model_slug``, and overriding the provider-aware
    default too) reaches ``build_model`` when no ``model`` is injected."""
    captured: dict[str, object] = {}

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        captured["model_slug"] = model_slug
        return _function_model_returning(_identity_args())

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya-kiambu",
            advisor_config=AdvisorConfig(model_slug="openai/gpt-4o"),
            sourcing_config=BeanSourcingConfig(model_slug="x-ai/grok-4.3"),
            http_client=http_client,
        )
    assert captured["model_slug"] == "x-ai/grok-4.3"


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_native_provider_uses_advisor_model_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) full-pipeline P1 regression test: a NATIVE advisor provider with
    no ``sourcing_config`` at all must extract with
    ``advisor_config.model_slug`` — never the OpenRouter-only
    "openai/gpt-5-mini" default, which is invalid/wrong-vendor against a
    native provider's own API."""
    captured: dict[str, object] = {}

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        captured["model_slug"] = model_slug
        return _function_model_returning(_identity_args())

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya-kiambu",
            advisor_config=AdvisorConfig(provider="openai", model_slug="gpt-5-mini"),
            http_client=http_client,
        )
    assert captured["model_slug"] == "gpt-5-mini"


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_openai_compatible_non_openrouter_uses_advisor_model_slug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) full-pipeline P2 regression test: an ``openai_compatible``
    advisor pointed at a NON-OpenRouter endpoint, with no
    ``sourcing_config`` at all, must extract with
    ``advisor_config.model_slug`` — never the OpenRouter-only
    "openai/gpt-5-mini" default."""
    captured: dict[str, object] = {}

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        captured["model_slug"] = model_slug
        return _function_model_returning(_identity_args())

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya-kiambu",
            advisor_config=AdvisorConfig(
                provider="openai_compatible",
                provider_base_url="https://my-local-litellm.example/v1",
                model_slug="locally-served-model",
            ),
            http_client=http_client,
        )
    assert captured["model_slug"] == "locally-served-model"


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_propagates_fetch_error() -> None:
    with pytest.raises(BeanFetchError):
        await draft_bean_profile_from_url(
            "not-a-url",
            advisor_config=_ADVISOR_CONFIG,
            model=_function_model_returning(_identity_args()),
        )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_propagates_extraction_error() -> None:
    """#613: a malformed structured-output shape is DEPENDENCY-origin."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionUnavailableError):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_text("no structured output here"),
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_propagates_over_long_field_error() -> None:
    """#609, FULL pipeline (fetch + extract): an over-long ``name`` in the
    model's structured output exhausts pydantic-ai's validation retries the
    same way a malformed shape does — surfacing through
    ``draft_bean_profile_from_url`` (and therefore ``POST
    /api/beans/draft-from-url``, ``test_api.py``'s existing generic 503
    test) as ``BeanExtractionUnavailableError``, never a 422 as if the
    vendor page itself were bad (#613)."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionUnavailableError, match="malformed shape"):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(_identity_args(name="x" * 501)),
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_propagates_thin_page_extraction_error() -> None:
    """A page with no usable name/origin fails soft as a typed error, not a
    fabricated identity."""
    async with _mock_client(_html_response(200, "<p>Just a generic page.</p>")) as http_client:
        with pytest.raises(BeanExtractionError, match="could not determine"):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/thin",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(
                    _identity_args(name=None, bean_origin=None, country=None)
                ),
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_maps_build_model_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#587 fix 2, full pipeline: a missing optional provider dependency
    surfaced by ``build_model`` (via ``_bean_sourcing_agent``) must reach the
    caller as ``BeanExtractionError``, not the raw ``AdvisorDependencyError``.
    ``model`` is deliberately omitted so the ``build_model`` path is hit.
    #613: this is DEPENDENCY-origin, so the subclass, ``BeanExtractionUnavailableError``."""

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        raise AdvisorDependencyError(
            "advisor provider 'anthropic' needs an optional dependency: "
            "pip install 'roastpilot-agent[anthropic]'"
        )

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionUnavailableError, match="could not build its model"):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_maps_source_url_validation_error() -> None:
    """#587 fix 3: ``BeanProfileDraft.source_url`` runs a stricter validator
    (models.py — rejects a malformed port) than the earlier fetch-path
    checks for an INJECTED ``http_client`` (which skips the SSRF guard/port
    check entirely — that machinery only runs for the internally-constructed
    client, see :func:`_fetch_page_text`), so a URL that fetches fine can
    still fail ``BeanProfileDraft`` construction. That must fail soft as
    ``BeanExtractionError``, not an unhandled ``pydantic.ValidationError``.

    (The embedded-userinfo variant of this gap is now closed EARLIER and
    UNIVERSALLY — regardless of injected vs. owned client — by
    ``draft_bean_profile_from_url``'s own upfront credential check, #587
    P1; see ``test_draft_bean_profile_from_url_rejects_embedded_credentials``.)
    """
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionError, match="failed validation"):
            await draft_bean_profile_from_url(
                "https://vendor.example:99999/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(_identity_args()),
            )


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_rejects_embedded_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#587 P1: a URL with embedded basic-auth credentials must be rejected
    BEFORE any fetch or logging — proven here by an ``http_client`` that
    would ``pytest.fail`` if ever invoked — and every captured log line must
    carry only the REDACTED URL (no raw credentials), never the original."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("must not fetch a URL with embedded credentials")

    caplog.set_level(logging.INFO, logger="roastpilot_agent.bean_sourcing")
    async with _mock_client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(BeanFetchError, match="credentials"):
            await draft_bean_profile_from_url(
                "https://scraper:s3cr3t-token@vendor.example/products/kenya",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(_identity_args()),
            )
    assert caplog.records, "expected a redacted rejection log line"
    for record in caplog.records:
        message = record.getMessage()
        assert "s3cr3t-token" not in message
        assert "scraper:" not in message
    assert any("vendor.example" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_rejects_url_with_fragment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#587 P2 round 5: a URL fragment can carry a sensitive token (e.g. an
    OAuth redirect's ``#access_token=...``) — must be rejected BEFORE any
    fetch/logging, and the token must never appear in any log line."""

    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail("must not fetch a URL with a fragment")

    caplog.set_level(logging.INFO, logger="roastpilot_agent.bean_sourcing")
    async with _mock_client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(BeanFetchError, match="fragment"):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya#access_token=s3cr3t-token",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(_identity_args()),
            )
    assert caplog.records, "expected a redacted rejection log line"
    for record in caplog.records:
        message = record.getMessage()
        assert "s3cr3t-token" not in message
        assert "access_token" not in message
    assert any("vendor.example" in record.getMessage() for record in caplog.records)


def test_redact_url_credentials_strips_userinfo() -> None:
    redacted = bean_sourcing._redact_url_credentials(  # pyright: ignore[reportPrivateUsage]
        "https://scraper:s3cr3t-token@vendor.example:8443/products/kenya"
    )
    assert redacted == "https://vendor.example:8443/products/kenya"
    assert "s3cr3t-token" not in redacted
    assert "scraper" not in redacted


def test_redact_url_credentials_leaves_credential_free_url_unchanged() -> None:
    url = "https://vendor.example/products/kenya"
    assert bean_sourcing._redact_url_credentials(url) == url  # pyright: ignore[reportPrivateUsage]


def test_redact_url_credentials_strips_fragment() -> None:
    """#587 P2 round 5: a fragment can carry a sensitive token — the
    logging-safe redaction must strip it, same as userinfo."""
    redacted = bean_sourcing._redact_url_credentials(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya#access_token=s3cr3t"
    )
    assert redacted == "https://vendor.example/products/kenya"
    assert "s3cr3t" not in redacted


def test_redact_url_credentials_strips_query_param_values() -> None:
    """#587 P2 round 7: a credential-bearing query parameter
    (``?access_token=...``) must be redacted too — keys are kept so the
    redacted URL stays recognizable."""
    redacted = bean_sourcing._redact_url_credentials(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya?access_token=s3cr3t-token&ref=email"
    )
    assert redacted == "https://vendor.example/products/kenya?access_token=REDACTED&ref=REDACTED"
    assert "s3cr3t-token" not in redacted


def test_redact_url_credentials_leaves_a_query_free_url_unchanged() -> None:
    url = "https://vendor.example/products/kenya"
    assert bean_sourcing._redact_url_credentials(url) == url  # pyright: ignore[reportPrivateUsage]


def test_redact_query_redacts_bare_token_with_no_equals_wholesale() -> None:
    """A bare token with no ``=`` (e.g. a secret short-link id smuggled as
    a "key" with no value) is indistinguishable from a blank-value ``key=``
    pair via ``parse_qsl`` — redacted wholesale here instead, so the secret
    itself is never left sitting unredacted in the "key" position."""
    redact = bean_sourcing._redact_query  # pyright: ignore[reportPrivateUsage]
    assert redact("SECRET_SHORT_LINK_ID") == "REDACTED"


def test_redact_query_handles_multiple_params_and_empty_query() -> None:
    redact = bean_sourcing._redact_query  # pyright: ignore[reportPrivateUsage]
    assert redact("") == ""
    assert redact("a=1&b=2") == "a=REDACTED&b=REDACTED"
    assert redact("a=1;b=2") == "a=REDACTED&b=REDACTED"


def test_redact_query_skips_empty_segments_from_doubled_separators() -> None:
    redact = bean_sourcing._redact_query  # pyright: ignore[reportPrivateUsage]
    assert redact("a=1&&b=2") == "a=REDACTED&b=REDACTED"


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_never_logs_a_query_param_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full-pipeline proof: a URL with a sensitive query parameter must
    never appear un-redacted in ANY log line, including the normal
    (non-rejected) happy-path logging."""
    caplog.set_level(logging.INFO, logger="roastpilot_agent.bean_sourcing")
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        draft = await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya?access_token=s3cr3t-query-token",
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=_function_model_returning(_identity_args()),
        )
    assert draft.name
    assert caplog.records, "expected at least the fetching/drafted info logs"
    for record in caplog.records:
        assert "s3cr3t-query-token" not in record.getMessage()


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_json_ld_match_never_logs_a_query_param_secret(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#590 P2 fix regression guard: the identity-match locator now carries
    the query (previously dropped), so a page with a MATCHING JSON-LD block
    exercises that new code path on a secret-bearing requested URL — the
    locator is purely in-memory (never logged/stored); confirms
    ``_redact_url_credentials``/``_redact_query`` (#587, unchanged by this
    fix) still strip the token from every log line and the persisted
    ``source_url``. The JSON-LD block's own (query-less, canonical) url
    still matches per the query-less-side rule."""
    caplog.set_level(logging.INFO, logger="roastpilot_agent.bean_sourcing")
    page_with_json_ld = _html_with_json_ld(_MATCHING_JSON_LD_SCRIPT)
    async with _mock_client(_html_response(200, page_with_json_ld)) as http_client:
        draft = await draft_bean_profile_from_url(
            f"{_MATCHING_JSON_LD_URL}?access_token=s3cr3t-query-token",
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=_function_model_returning(_identity_args()),
        )
    assert draft.name
    assert draft.source_url is not None
    assert "s3cr3t-query-token" not in draft.source_url
    assert caplog.records, "expected at least the fetching/drafted info logs"
    for record in caplog.records:
        assert "s3cr3t-query-token" not in record.getMessage()


def test_redact_url_credentials_returns_url_unchanged_on_malformed_url() -> None:
    """This helper must NEVER raise, even on a URL that fails to parse at
    all (#587 P2 round 5) — bailing out of a logging call is worse than
    logging the (still credential-bearing, in this specific edge case)
    original."""
    url = "http://[::1"
    assert bean_sourcing._redact_url_credentials(url) == url  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_draft_bean_profile_from_url_uses_default_sourcing_config_when_omitted() -> None:
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        draft = await draft_bean_profile_from_url(
            "https://vendor.example/products/kenya-kiambu",
            advisor_config=_ADVISOR_CONFIG,
            http_client=http_client,
            model=_function_model_returning(_identity_args()),
            sourcing_config=None,
        )
    assert draft.name


# --- #573 invariant: cleanly separate from the roast advisor / control path ---


def test_bean_sourcing_module_does_not_directly_import_controller_safety_or_mcp_client() -> None:
    """Static check on this module's own import statements."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(bean_sourcing.__file__).read_text())
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)
        elif isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert imported_modules.isdisjoint(forbidden)


def test_bean_sourcing_never_transitively_imports_controller_safety_or_mcp_client() -> None:
    """Authoritative transitive-import check (#573 invariant): the roaster/
    control path must be unreachable from this feature.

    Runs in a FRESH subprocess — this pytest process may already have
    imported ``controller``/``safety``/``mcp_client`` via other test modules
    in the same session, which would make an in-process ``sys.modules``
    check a false pass. The subprocess only ever imports
    ``roastpilot_agent.bean_sourcing`` and inspects what THAT pulled in.
    """
    script = (
        "import sys\n"
        "import roastpilot_agent.bean_sourcing\n"
        "loaded = {m for m in sys.modules if m.startswith('roastpilot_agent.')}\n"
        "forbidden = {\n"
        "    'roastpilot_agent.controller',\n"
        "    'roastpilot_agent.safety',\n"
        "    'roastpilot_agent.mcp_client',\n"
        "}\n"
        "hit = sorted(loaded & forbidden)\n"
        "print(','.join(hit))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"bean_sourcing transitively imported forbidden modules: {result.stdout.strip()}\n"
        f"stderr: {result.stderr}"
    )
