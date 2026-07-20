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
import gzip
import ipaddress
import logging
import socket
import subprocess
import sys
import time
import zlib
from collections.abc import AsyncGenerator, Callable
from typing import Literal

import extruct  # type: ignore[import-untyped]
import httpx
import pytest
from pydantic_ai import ModelHTTPError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel

from roastpilot_agent import bean_sourcing
from roastpilot_agent.advisor import AdvisorDependencyError
from roastpilot_agent.bean_sourcing import (
    BeanExtractionError,
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
    model = _function_model_text("here is some prose, not the tool call")
    with pytest.raises(BeanExtractionError, match="malformed"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG, model=model
        )


@pytest.mark.asyncio
async def test_extract_bean_identity_maps_provider_error() -> None:
    model = _function_model_raising(
        ModelHTTPError(status_code=503, model_name="x", body="upstream down")
    )
    with pytest.raises(BeanExtractionError, match="provider error"):
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
    all with a 100s advisor budget)."""
    model = _function_model_hanging()
    advisor_config = AdvisorConfig(timeout_seconds=100.0)
    sourcing_config = BeanSourcingConfig(extraction_timeout_seconds=0.05)
    with pytest.raises(BeanExtractionError, match="deadline"):
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
    escaped uncaught instead of failing soft as ``BeanExtractionError``."""

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        raise AdvisorDependencyError(
            "advisor provider 'anthropic' needs an optional dependency: "
            "pip install 'roastpilot-agent[anthropic]'"
        )

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    with pytest.raises(BeanExtractionError, match="could not build its model"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=_ADVISOR_CONFIG
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
    for them is untouched by #590 D2b/D2c. ``processing``/``bean_species``/
    ``is_blend`` stay unconditionally demoted regardless of evidence
    quality too — deferred to slice E
    (:data:`bean_sourcing._ENUM_FIELDS_DEFERRED_TO_E`). ``altitude_m`` is
    the one field that DOES diverge on evidence now the citation gate is
    ENABLED (#590 D2c): a genuine, bound citation ("Altitude: 1775m." —
    adjacent digit run + "altitude" cue, verbatim on the page) flips it to
    ``"on_page"``; no evidence quote at all leaves it
    ``"origin_estimated"``, identically to before D2c."""
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
    # The one field that DOES diverge: a genuine bound citation flips
    # altitude_m to on_page; no citation at all leaves it origin_estimated.
    assert draft_with_evidence.field_sources["altitude_m"] == "on_page"
    assert draft_with_evidence.field_sources["processing"] == "origin_estimated"
    assert draft_without_evidence.field_sources["altitude_m"] == "origin_estimated"
    assert draft_without_evidence.field_sources["processing"] == "origin_estimated"
    assert "bean_species" not in draft_with_evidence.field_sources
    assert "is_blend" not in draft_with_evidence.field_sources


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
# The gate is ENABLED (_ALTITUDE_CITATION_GATE_ENABLED = True, #615/D2c
# folded five hardening fixes — cue-value binding, suffix-only glued-m,
# non-metre unit rejection, the segmenter's thousands-period exception,
# and range-endpoint rejection — then flipped the flag). Tests below that
# assert a "_demotes" outcome via _draft_from_identity now exercise the
# ENABLED runtime path directly, not merely a dormant mechanism.


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


def test_quote_supports_altitude_glued_metre_unit_flips_on_page() -> None:
    """#590 D2b fix 2 (Codex round, SXVDR): the exact compact form
    ``_EXTRACTION_INSTRUCTIONS`` itself exemplifies ("1,850m") could never
    verify before this fix — bare "m" isn't a recognized elevation cue on
    its own, so a unit GLUED directly onto the value-matching digits is
    now accepted as unambiguous (distance 0). #590 D2c fold 2 narrows the
    shortcut to a SUFFIX-only match (:func:`bean_sourcing._is_glued_metre_suffix`)
    — see ``test_quote_supports_altitude_prefix_glued_m_does_not_verify``
    for the prefix form ("M1800") this narrowing excludes."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1850, "grown at 1,850m", "This lot is grown at 1,850m above the valley floor."
        )
        is True
    )


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
        "altitude of 1800",
        "Elevation: 1,800",
    ],
)
def test_quote_supports_altitude_genuinely_verified_flips_on_page(evidence_quote: str) -> None:
    """A quote that both (a) is an authentic single-segment page span and
    (b) genuinely supports the value — a matching digit run with an
    elevation cue BOUND to it (:func:`bean_sourcing._cue_binds_to_value`),
    in any of the accepted forms (comma-grouped digits, a colon-adjacent
    cue, a different cue word before or after the value, the complete
    "above sea level" phrase, or a stopword-bridged "altitude of 1800") —
    the ENABLED gate returns ``True``."""
    corpus = f"{_IDENTITY_PAGE_TEXT} This lot is {evidence_quote}."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, evidence_quote, corpus
        )
        is True
    )


def test_quote_supports_altitude_thousands_period_glued_metre_flips_on_page() -> None:
    """#590 D2c fold 4 (SX60a): a European-style thousands-period number
    glued to a bare "m" unit ("1.850m") must verify end-to-end — the
    segmenter (:func:`bean_sourcing._split_corpus_segments`) must not treat
    that period as a sentence boundary, or the authentic-span check
    (fold 3, #590 D2b) could never see the quote as a single segment."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1850, altitude_m_evidence="grown at 1.850m")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="This farm is grown at 1.850m above the valley floor.",
    )
    assert draft.field_sources["altitude_m"] == "on_page"


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


def test_quote_supports_altitude_fabricated_quote_demotes() -> None:
    """A quote that genuinely WOULD support the value if it were real page
    text, but is never actually on the page (a fabricated citation), must
    still demote — the authentic-single-segment-span check is the first
    gate. Calls the gate function directly (dormant at the
    :func:`_draft_from_identity` level, see
    ``test_quote_supports_altitude_ships_dormant``)."""
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800,
            "grown at 1,800 masl",
            _IDENTITY_PAGE_TEXT,  # never mentions 1800/masl at all
        )
        is False
    )


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


def test_quote_supports_altitude_real_single_sentence_quote_verifies() -> None:
    """The positive mirror of the cross-sentence-splice case: a genuine
    sentence among many others still verifies — a quote that genuinely
    sits within ONE sentence, surrounded by other sentences on both sides."""
    corpus = "Random intro text. This farm sits at 1800 masl elevation. More text follows after."
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "1800 masl elevation", corpus
        )
        is True
    )


# --- #590 D2c fold 1: cue-value BINDING ---


def test_quote_supports_altitude_unbound_cue_across_a_real_noun_phrase_demotes() -> None:
    """#590 D2c fold 1 (SX60U) HEADLINE repro: the old plain ``abs(distance)
    <= window`` scan let "High-altitude coffee with 1,800 reviews" bind the
    "altitude" cue to an unrelated review count 3 words away. Binding now
    requires adjacency or ONLY stopwords intervening
    (:func:`bean_sourcing._cue_binds_to_value`) — "coffee"/"with" are real
    nouns, so this must demote."""
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


def test_cue_binds_to_value_stopword_bridge_allows_binding() -> None:
    """A cue up to 4 positions away binds when ONLY small stopwords
    intervene (:func:`bean_sourcing._cue_binds_to_value`, #590 D2c fold 1)."""
    binds = bean_sourcing._cue_binds_to_value  # pyright: ignore[reportPrivateUsage]
    tokens = ["altitude", "of", "about", "1800"]
    assert binds(tokens, 3, 0) is True
    assert binds(["altitude", "grown", "with", "care", "1800"], 4, 0) is False


def test_cue_binds_to_value_beyond_the_window_never_binds() -> None:
    """A cue more than :data:`bean_sourcing._ALTITUDE_CUE_PROXIMITY_WINDOW`
    positions away never binds, even over pure stopwords — exercises the
    outer-bound guard directly."""
    binds = bean_sourcing._cue_binds_to_value  # pyright: ignore[reportPrivateUsage]
    tokens = ["altitude", "of", "at", "is", "about", "1800"]
    assert binds(tokens, 5, 0) is False


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


def test_quote_supports_altitude_glued_feet_unit_demotes() -> None:
    """The glued-unit variant of fold 3: "1800ft" must reject just as the
    space-separated form does."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="Elevation 1800ft here")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Elevation 1800ft here at the visitor centre.",
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


# --- #590 D2c fold 5: range-endpoint rejection ---


@pytest.mark.parametrize(
    "corpus",
    [
        "This farm sits at 1,600-1,800 masl on the slope.",
        "This farm sits at 1600 to 1800 masl on the slope.",
        "This farm sits at between 1600 and 1800 masl on the slope.",
    ],
)
def test_quote_supports_altitude_range_endpoint_demotes(corpus: str) -> None:
    """#590 D2c fold 5 (SXVDY) HEADLINE repro: a page-stated RANGE
    ("1,600-1,800 masl" / "1600 to 1800 masl" / "between 1600 and 1800
    masl") cropped down to a quote naming only the upper bound must not
    certify a scalar reading of 1800 — checked on the AUTHENTIC SEGMENT,
    not the cropped quote (:func:`bean_sourcing._value_is_range_endpoint`)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="1,800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya", corpus=corpus
    )
    assert draft.field_sources["altitude_m"] == "origin_estimated"


def test_quote_supports_altitude_scalar_reading_is_not_mistaken_for_a_range() -> None:
    """The positive control for fold 5: a genuine SCALAR altitude
    statement (no preceding digit, no to/and-digit pattern) must still
    verify — the range check must not over-fire on an ordinary sentence."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="Altitude: 1800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus="Altitude: 1800 masl above sea level on this farm.",
    )
    assert draft.field_sources["altitude_m"] == "on_page"


def test_value_is_range_endpoint_true_for_dash_separated_range() -> None:
    """Direct unit coverage: the preceding-digit-token branch."""
    is_range_endpoint = bean_sourcing._value_is_range_endpoint  # pyright: ignore[reportPrivateUsage]
    assert is_range_endpoint(1800, "grown at 1,600-1,800 masl") is True


def test_value_is_range_endpoint_true_for_and_separated_range() -> None:
    """Direct unit coverage: the "and"-preceded-by-digit branch."""
    is_range_endpoint = bean_sourcing._value_is_range_endpoint  # pyright: ignore[reportPrivateUsage]
    assert is_range_endpoint(1800, "grown between 1600 and 1800 masl") is True


def test_value_is_range_endpoint_false_when_value_is_the_first_token() -> None:
    """The ``i == 0`` guard: a value token with nothing preceding it in
    the segment is never a range endpoint."""
    is_range_endpoint = bean_sourcing._value_is_range_endpoint  # pyright: ignore[reportPrivateUsage]
    assert is_range_endpoint(1800, "1800 masl on this farm") is False


def test_value_is_range_endpoint_false_for_a_scalar_reading() -> None:
    is_range_endpoint = bean_sourcing._value_is_range_endpoint  # pyright: ignore[reportPrivateUsage]
    assert is_range_endpoint(1800, "grown at 1800 masl on this farm") is False


def test_draft_from_identity_processing_and_species_always_demote_in_d2b() -> None:
    """#590 D2b Codex round-1 (SH8b4/SH8b7): ``processing``/``bean_species``
    demote unconditionally, even with a genuine, well-cued evidence quote —
    sound enum verification is deferred to slice E."""
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


def test_draft_from_identity_is_blend_still_demotes_with_a_genuine_evidence_quote() -> None:
    """``is_blend`` stays demoted unconditionally through D2b regardless of
    how genuine its (unconsumed) evidence quote is — polarity verification
    is deferred to slice E, not this citation gate."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True, is_blend_evidence="a blend of three origins")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/blend",
        corpus="This is a blend of three origins, roasted together.",
    )
    assert draft.field_sources["is_blend"] == "origin_estimated"


def test_quote_supports_altitude_ships_enabled_end_to_end() -> None:
    """#590 D2c: the citation gate is ENABLED
    (:data:`bean_sourcing._ALTITUDE_CITATION_GATE_ENABLED` is ``True``) — a
    PERFECT citation (genuine, bound, authentic, not a range endpoint) now
    flips ``altitude_m`` to ``"on_page"`` all the way through
    :func:`_draft_from_identity`, not merely at the gate-function level
    (the #590 D2b predecessor of this test, ``test_quote_supports_altitude
    _ships_dormant``, proved the opposite runtime behaviour while the flag
    was ``False``)."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(altitude_m=1800, altitude_m_evidence="grown at 1,800 masl")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity,
        url="https://vendor.example/products/kenya",
        corpus=f"{_IDENTITY_PAGE_TEXT} This lot is grown at 1,800 masl.",
    )
    assert draft.field_sources["altitude_m"] == "on_page"
    assert (
        bean_sourcing._quote_supports_altitude(  # pyright: ignore[reportPrivateUsage]
            1800, "grown at 1,800 masl", f"{_IDENTITY_PAGE_TEXT} This lot is grown at 1,800 masl."
        )
        is True
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


def test_numeric_tokens_elides_thousands_separator_commas() -> None:
    numeric_tokens = bean_sourcing._numeric_tokens  # pyright: ignore[reportPrivateUsage]
    assert numeric_tokens("grown at 1,800 masl") == {"1800"}


def test_numeric_tokens_ignores_a_leading_orphan_comma() -> None:
    """A comma with no digits accumulated yet (nothing to elide into) must
    not raise or corrupt the scan — the comma is simply dropped."""
    numeric_tokens = bean_sourcing._numeric_tokens  # pyright: ignore[reportPrivateUsage]
    assert numeric_tokens(",800 masl") == {"800"}


def test_numeric_tokens_flushes_a_trailing_digit_run_at_string_end() -> None:
    """A digit run with no trailing non-digit character (the string simply
    ends) must still be captured via the post-loop flush."""
    numeric_tokens = bean_sourcing._numeric_tokens  # pyright: ignore[reportPrivateUsage]
    assert numeric_tokens("elevation 1850") == {"1850"}


def test_numeric_tokens_does_not_elide_a_decimal_comma_with_two_digits() -> None:
    """#590 D2b fix 1: a genuine 2-digit decimal (cents) must NOT collapse
    into a hallucinated thousands-grouped number."""
    numeric_tokens = bean_sourcing._numeric_tokens  # pyright: ignore[reportPrivateUsage]
    assert numeric_tokens("18,00") == {"18", "00"}
    assert numeric_tokens("1,234,567 units") == {"1234567"}


def test_elides_as_thousands_separator_requires_exactly_three_digits() -> None:
    elides = bean_sourcing._elides_as_thousands_separator  # pyright: ignore[reportPrivateUsage]
    assert elides("1,800", 1) is True
    assert elides("18,00", 2) is False  # only 2 digits follow
    assert elides("1,8a0", 1) is False  # non-digit within the next 3
    assert elides("1,8000", 1) is False  # 4 digits follow, not exactly 3
    assert elides("1,80", 1) is False  # fewer than 3 chars remain


def test_alpha_runs_splits_on_digits_and_casefolds() -> None:
    alpha_runs = bean_sourcing._alpha_runs  # pyright: ignore[reportPrivateUsage]
    assert alpha_runs("MASL") == ["masl"]
    assert alpha_runs("1800masl") == ["masl"]
    assert alpha_runs("800masl") == ["masl"]
    assert alpha_runs("1800") == []
    assert alpha_runs("parameter") == ["parameter"]
    # letters followed by digits (mid-token flush, not just the post-loop one)
    assert alpha_runs("masl1800") == ["masl"]
    assert alpha_runs("masl1800m") == ["masl", "m"]


def test_token_carries_altitude_cue_matches_a_whole_word_case_insensitively() -> None:
    """#590 D2b (claude-review): a SUBSTRING check let ``"meter"`` match
    inside ``"parameter"``/``"diameter"``/``"kilometer"`` — reopening the
    laundering class even after fix 1 dropped standalone above/sea/level.
    A token only carries a cue when one of its whole alphabetic runs
    (:func:`_alpha_runs`) EQUALS an :data:`_ALTITUDE_CUES` member."""
    token_carries_cue = bean_sourcing._token_carries_altitude_cue  # pyright: ignore[reportPrivateUsage]
    assert token_carries_cue("MASL") is True
    assert token_carries_cue("1800masl") is True
    assert token_carries_cue("800masl") is True
    assert token_carries_cue("meters") is True
    assert token_carries_cue("shipping") is False
    assert token_carries_cue("level") is False  # #590 D2b fix 1: no longer a cue
    assert token_carries_cue("parameter") is False  # the substring bug this fix closes
    assert token_carries_cue("diameter") is False
    assert token_carries_cue("kilometer") is False
    assert token_carries_cue("kilometres") is False  # != "metres"
    assert token_carries_cue("1800parameter") is False  # glued form of the same bug


def test_proximity_tokens_splits_on_punctuation_preserving_grouped_numbers() -> None:
    """#590 D2b fix 2 (SH8b9): colon/semicolon/hyphen are hard boundaries,
    but a comma/period directly between two digits is preserved."""
    proximity_tokens = bean_sourcing._proximity_tokens  # pyright: ignore[reportPrivateUsage]
    assert proximity_tokens("Reviews:1,800;shipping;shop-elevation-map") == [
        "Reviews",
        "1800",
        "shipping",
        "shop",
        "elevation",
        "map",
    ]
    assert proximity_tokens("1800masl") == ["1800masl"]


def test_proximity_tokens_boundary_at_string_end_and_orphan_separators() -> None:
    """A trailing comma right after a digit (nothing following it) is a
    boundary, not an elision — covers the ``i + 1 < length`` guard — and a
    leading/orphan punctuation mark with no accumulated token is dropped
    without producing a spurious empty token."""
    proximity_tokens = bean_sourcing._proximity_tokens  # pyright: ignore[reportPrivateUsage]
    assert proximity_tokens("1800,") == ["1800"]
    assert proximity_tokens(",,1800") == ["1800"]
    assert proximity_tokens("abc,123") == ["abc", "123"]


def test_above_sea_level_cue_indices_finds_the_contiguous_phrase() -> None:
    """#590 D2c fold 1: returns BOTH edge anchors ("above" and "level"),
    not the phrase's middle — so a value token on EITHER side of the
    phrase can bind from its nearest edge (:func:`bean_sourcing._cue_binds_to_value`)."""
    indices = bean_sourcing._above_sea_level_cue_indices  # pyright: ignore[reportPrivateUsage]
    assert indices(["1800", "above", "sea", "level"]) == [1, 3]
    assert indices(["1800", "above", "the", "level"]) == []
    assert indices(["above", "sea"]) == []


def test_altitude_evidence_supports_value_no_matching_digit_token_returns_false() -> None:
    """A quote that never mentions the value's digits at all (even with
    cues present) must not verify — exercises the ``digit_indices``
    empty-list branch directly."""
    supports_value = bean_sourcing._altitude_evidence_supports_value  # pyright: ignore[reportPrivateUsage]
    assert supports_value(1800, "This is a wonderful high altitude lot") is False


def test_altitude_evidence_supports_value_no_cue_token_returns_false() -> None:
    """A matching digit token with NO cue token anywhere in the quote."""
    supports_value = bean_sourcing._altitude_evidence_supports_value  # pyright: ignore[reportPrivateUsage]
    assert supports_value(1800, "priced at 1800 dollars") is False


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
    English words that collide with unrelated page prose, so D1
    demoted it UNCONDITIONALLY. #590 D2b now verifies it via the
    citation gate (:func:`bean_sourcing._quote_supports_value`) — but
    with NO ``processing_evidence`` quote supplied, the gate still
    demotes, even when the page GENUINELY states "washed" and the model
    correctly returned the value (see :data:`bean_sourcing._TYPED_CITATION_FIELDS`)."""
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
    so D1 never even tries; it demotes unconditionally either way."""
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
    """Same D1 scoping fold as processing, for the other deferred enum
    field: ``bean_species`` demotes unconditionally even when genuinely
    stated on the page."""
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


def test_draft_from_identity_is_blend_true_is_always_demoted() -> None:
    """``is_blend`` is DEFERRED to slice E (post-D2b, still unconditional —
    see :func:`bean_sourcing._draft_from_identity`'s ``is_blend``
    handling) — token presence alone is unsafe positional evidence (a
    single-origin product page can still contain the word "blend" via an
    unrelated "shop our house blend" cross-sell link; true verification
    needs LOCALITY). An explicit ``True`` the model returned always
    demotes, even when the page genuinely says "blend"."""
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


def test_draft_from_identity_is_blend_false_is_always_demoted_in_d1() -> None:
    """The mirror case: an explicit ``False`` always demotes too, even
    when the page genuinely says "single origin"."""
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
    the OUTCOME changed from fail to fall back)."""

    def _hangs(html: str) -> str | None:
        # A REAL (synchronous) sleep — this runs on the asyncio.to_thread
        # worker thread, mirroring a genuinely pathological/slow parse;
        # asyncio.timeout can only stop the AWAIT, not this thread, so it
        # keeps running in the background after the test's own timeout
        # fires (tracked separately as #607) — kept short so that residual
        # cost stays negligible.
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
    the decoupling holds end-to-end, not just at the unit level."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionError, match="deadline"):
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
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionError):
            await draft_bean_profile_from_url(
                "https://vendor.example/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_text("no structured output here"),
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
    ``model`` is deliberately omitted so the ``build_model`` path is hit."""

    def fake_build_model(config: AdvisorConfig, *, model_slug: str | None = None) -> Model:
        raise AdvisorDependencyError(
            "advisor provider 'anthropic' needs an optional dependency: "
            "pip install 'roastpilot-agent[anthropic]'"
        )

    monkeypatch.setattr(bean_sourcing, "build_model", fake_build_model)
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionError, match="could not build its model"):
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
