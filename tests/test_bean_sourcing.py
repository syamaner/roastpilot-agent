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
import ipaddress
import subprocess
import sys
from collections.abc import AsyncGenerator

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
from roastpilot_agent.config import AdvisorConfig, BeanSourcingConfig
from roastpilot_agent.models import BeanProfileDraft

# --- test doubles ---


def _mock_client(handler: httpx.MockTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=handler)


def _html_response(status_code: int, html: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=html.encode())

    return httpx.MockTransport(handler)


#: A fixed TEST-NET-3 (RFC 5737) address the no-op destination stub below
#: "resolves" every host to — not a real address, just a syntactically valid
#: public IP literal so the pinning code path (which now needs a real
#: ``ipaddress`` object to pin to) has something to pin to.
_STUB_PUBLIC_IP = ipaddress.ip_address("203.0.113.10")


async def _noop_assert_public_destination(
    url: str,
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Test double: skips the real DNS/IP validation (#587's SSRF guard),
    "resolving" every host to :data:`_STUB_PUBLIC_IP` so client-lifecycle/
    redirect-following tests can drive the (now pinning) fetch path without
    depending on a real resolver. Tests that need to assert the ORIGINAL
    hostname is preserved (Host header, SNI) still can — this only stubs the
    validated CONNECTION target; destination validation itself, and the
    real pinning mechanism, are covered separately."""
    return _STUB_PUBLIC_IP


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
        "is_blend": False,
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
async def test_fetch_page_text_success_extracts_text() -> None:
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as client:
        text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
            "https://vendor.example/products/kenya",
            config=BeanSourcingConfig(),
            http_client=client,
        )
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
    text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        "https://vendor.example/products/kenya", config=BeanSourcingConfig()
    )
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
        return httpx.Response(200, content=_SAMPLE_HTML.encode())

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
    text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        original_url, config=BeanSourcingConfig()
    )
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
        return httpx.Response(200, content=_SAMPLE_HTML.encode())

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
    text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        original_url, config=BeanSourcingConfig()
    )
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
        return httpx.Response(200, content=_SAMPLE_HTML.encode())

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        origin_url, config=BeanSourcingConfig()
    )
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
        return httpx.Response(200, content=_SAMPLE_HTML.encode())

    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args: object, **kwargs: object) -> httpx.AsyncClient:
        return real_async_client(transport=transport)

    monkeypatch.setattr("roastpilot_agent.bean_sourcing.httpx.AsyncClient", fake_async_client)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)

    text = await bean_sourcing._fetch_page_text(  # pyright: ignore[reportPrivateUsage]
        origin_url, config=BeanSourcingConfig()
    )
    assert "Kenya Kiambu AA" in text
    assert len(captured_requests) == 1
    sent = captured_requests[0]
    assert sent.url.host == public_ipv6
    assert str(sent.url).startswith(f"https://[{public_ipv6}]")
    assert sent.headers.get("host") == "example.test"


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
async def test_fetch_page_text_owns_client_raises_on_non_2xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-2xx status check inside ``_fetch_with_ssrf_guard`` (the
    owns-client path) — the equivalent injected-client check is covered by
    ``test_fetch_page_text_raises_on_non_2xx`` above."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"not found")

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
        return httpx.Response(200, content=huge_html.encode())

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


def test_bean_sourcing_agent_builds_model_from_advisor_config_when_none_injected() -> None:
    """Covers the model=None branch (build_model(advisor_config)) without any
    network call — build_model only constructs the Model object."""
    agent = bean_sourcing._bean_sourcing_agent(  # pyright: ignore[reportPrivateUsage]
        AdvisorConfig(provider="openai_compatible", api_key_env="ROASTPILOT_TEST_UNSET_KEY")
    )
    assert agent is not None


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
async def test_extract_bean_identity_timeout_raises_bean_extraction_error() -> None:
    """#587 fix 3: ``agent.run`` is bounded by ``advisor_config.timeout_seconds``
    — a hung provider must fail soft as ``BeanExtractionError``, not hang the
    drafting request forever."""
    model = _function_model_hanging()
    config = AdvisorConfig(timeout_seconds=0.05)
    with pytest.raises(BeanExtractionError, match="deadline"):
        await bean_sourcing._extract_bean_identity(  # pyright: ignore[reportPrivateUsage]
            "page text", advisor_config=config, model=model
        )


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


# --- _draft_from_identity: honest imputation + conservative targets ---


def test_draft_from_identity_marks_page_fields_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya"
    )
    assert isinstance(draft, BeanProfileDraft)
    for field in (
        "name",
        "country",
        "bean_origin",
        "farm",
        "bean_varietal",
        "processing",
        "altitude_m",
        "description",
    ):
        assert draft.field_sources[field] == "on_page", field
    # bean_species was NOT stated on the page (None in the fixture) — no
    # fabricated value, and no field_sources entry claiming it is on_page.
    assert draft.bean_species is None
    assert "bean_species" not in draft.field_sources
    # is_blend defaults to False in the fixture — the page never SAID
    # anything about blending, so no provenance should be recorded for it
    # either (#587 fix 4: absent-from-field_sources must mean "unset").
    assert draft.is_blend is False
    assert "is_blend" not in draft.field_sources


def test_draft_from_identity_marks_explicit_is_blend_true_as_on_page() -> None:
    """#587 fix 4: when the page explicitly states this IS a blend,
    ``is_blend`` must be recorded as ``"on_page"`` provenance — the prior
    ``_IDENTITY_FIELDS`` loop always skipped it (it defaults to False, never
    None/""), so an explicit True never got provenance recorded at all."""
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(is_blend=True)
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/blend"
    )
    assert draft.is_blend is True
    assert draft.field_sources["is_blend"] == "on_page"


def test_draft_from_identity_marks_every_roast_target_origin_estimated() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args()
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/kenya"
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


def test_draft_from_identity_bean_origin_falls_back_to_country_and_is_still_on_page() -> None:
    identity = bean_sourcing._ExtractedBeanIdentity.model_validate(  # pyright: ignore[reportPrivateUsage]
        _identity_args(bean_origin=None, country="Ethiopia")
    )
    draft = bean_sourcing._draft_from_identity(  # pyright: ignore[reportPrivateUsage]
        identity, url="https://vendor.example/products/eth"
    )
    assert draft.bean_origin == "Ethiopia"
    assert draft.field_sources["bean_origin"] == "on_page"


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
            identity, url="https://vendor.example/products/nope"
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
        identity, url="https://vendor.example/products/x"
    )
    assert draft.target_drop_temp_c == expected_drop
    assert draft.target_development_percent == expected_dev
    # Every scouting drop/dev stays inside the operator's proven de-risked
    # band (issue #573): drop <=195, dev in [13, 15].
    assert draft.target_drop_temp_c <= 195.0
    assert 13.0 <= draft.target_development_percent <= 15.0


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
    (models.py — rejects embedded userinfo and a malformed port) than
    ``_fetch_page_text``'s own scheme/host check, so a URL that fetches fine
    can still fail ``BeanProfileDraft`` construction. That must fail soft as
    ``BeanExtractionError``, not an unhandled ``pydantic.ValidationError``."""
    async with _mock_client(_html_response(200, _SAMPLE_HTML)) as http_client:
        with pytest.raises(BeanExtractionError, match="failed validation"):
            await draft_bean_profile_from_url(
                "https://user:pass@vendor.example/products/kenya-kiambu",
                advisor_config=_ADVISOR_CONFIG,
                http_client=http_client,
                model=_function_model_returning(_identity_args()),
            )


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
