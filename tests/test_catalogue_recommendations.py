"""D121 catalogue discovery and deterministic ranking tests."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic_ai import ModelAPIError
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models import Model
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

from roastpilot_agent import catalogue_recommendations as catalogue
from roastpilot_agent.advisor import AdvisorError
from roastpilot_agent.bean_sourcing import (
    BeanExtractionError,
    BeanExtractionUnavailableError,
    BeanSourcingDiagnostics,
    FetchedVendorPage,
)
from roastpilot_agent.catalogue_recommendations import (
    CatalogueRankingContext,
    discover_catalogue_candidates,
    rank_catalogue_candidates,
    recommend_from_catalogue,
)
from roastpilot_agent.config import AdvisorConfig, BeanSourcingConfig


class _BytesStream(httpx.AsyncByteStream):
    """One-shot raw response body for the fetch streaming contract."""

    def __init__(self, data: bytes) -> None:
        self._data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._data


def _page(html: str) -> FetchedVendorPage:
    return FetchedVendorPage(
        prompt_text="",
        extracted_text="Kenya Kiambu Washed\nBrazil Santos Natural",
        json_ld_values="",
        raw_html=html,
        final_url="https://vendor.example/collections/green-coffee",
    )


def test_discovery_prefers_json_ld_and_bounds_links_to_same_origin_products() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Kenya Kiambu","url":"/products/kenya"}
    </script>
    <a href="/products/kenya">Duplicate Kenya</a>
    <a href="/product/brazil?size=1#reviews">Brazil Santos</a>
    <a href="/collections/all">All coffee</a>
    <a href="https://evil.example/products/secret">Off origin</a>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert [(item.candidate_id, item.product_url, item.label) for item in candidates] == [
        ("candidate-01", "https://vendor.example/products/kenya", "Kenya Kiambu"),
        ("candidate-02", "https://vendor.example/product/brazil?size=1", "Brazil Santos"),
    ]


def test_discovery_retains_candidate_local_card_and_json_ld_evidence() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Rwanda Nyamasheke","url":"/products/rwanda",
       "countryOfOrigin":{"name":"Rwanda"},"process":"honey"}
    </script>
    <article><a href="/products/kiambu">Kiambu Lot</a><span>Kenya · Washed</span></article>
    <article><a href="/products/santos">Santos Lot</a><span>Brazil · Natural</span></article>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert "Rwanda" in candidates[0].evidence
    assert "honey" in candidates[0].evidence
    assert candidates[1].evidence == "Kiambu Lot Kenya · Washed"
    assert "Brazil" not in candidates[1].evidence
    assert candidates[2].evidence == "Santos Lot Brazil · Natural"
    assert "Kenya" not in candidates[2].evidence


def test_discovery_rejects_userinfo_and_non_product_anchor_paths() -> None:
    page = _page(
        '<a href="https://user:secret@vendor.example/products/a">A</a><a href="/about">About</a>'
    )
    assert discover_catalogue_candidates(page) == []


def test_provider_text_redacts_absolute_urls_without_touching_product_words() -> None:
    redact = catalogue._redact_absolute_urls  # pyright: ignore[reportPrivateUsage]
    assert redact("Kenya HTTPS://vendor.example/products/a?token=x washed") == (
        "Kenya [link] washed"
    )
    assert redact("ß" * 45 + "https://vendor.example/products/a?token=x washed") == (
        "ß" * 45 + "[link] washed"
    )


def test_json_ld_flattener_handles_graph_item_list_nested_item_and_noise() -> None:
    flatten = catalogue._json_ld_product_blocks  # pyright: ignore[reportPrivateUsage]
    blocks = flatten(
        [
            1,
            {
                "@graph": [{"@type": ["Thing", "https://schema.org/Product"], "name": "A"}],
                "itemListElement": [{"item": {"@type": "Product", "name": "B"}}],
            },
        ]
    )
    assert [block["name"] for block in blocks] == ["A", "B"]
    evidence = catalogue._json_ld_product_evidence(  # pyright: ignore[reportPrivateUsage]
        {"origin": {"name": 7}, "description": "Washed lot"}, "A"
    )
    assert evidence == "A Washed lot"


@pytest.mark.parametrize(
    ("value", "require_product_path"),
    [
        ("https://vendor.example:bad/products/a", False),
        ("ftp://vendor.example/products/a", False),
        ("https://other.example/products/a", False),
        ("/collections/all", True),
    ],
)
def test_candidate_url_normalization_fails_closed(value: str, require_product_path: bool) -> None:
    normalize = catalogue._same_origin_product_url  # pyright: ignore[reportPrivateUsage]
    assert (
        normalize(
            value,
            base_url="https://vendor.example/collections/green",
            require_product_path=require_product_path,
        )
        is None
    )


def test_candidate_url_normalization_rejects_oversized_product_url() -> None:
    normalize = catalogue._same_origin_product_url  # pyright: ignore[reportPrivateUsage]
    oversized = "/products/" + "a" * 4096
    assert (
        normalize(
            oversized,
            base_url="https://vendor.example/collections/green",
            require_product_path=True,
        )
        is None
    )


def test_discovery_fails_soft_on_empty_and_malformed_json_ld() -> None:
    assert discover_catalogue_candidates(_page("")) == []
    assert (
        discover_catalogue_candidates(
            _page(
                "<script>ordinary script</script>"
                '<script type="application/ld+json"></script>'
                '<script type="application/ld+json">{bad</script>'
                '<script type="application/ld+json">'
                '{"@type":"Product","url":"/products/nameless"}'
                "</script>"
                '<a href="/products/empty"></a>'
            )
        )
        == []
    )


def test_discovery_fails_soft_on_unexpected_parser_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(page: FetchedVendorPage) -> list[catalogue.CatalogueCandidate]:
        del page
        raise RuntimeError("synthetic parser escape")

    monkeypatch.setattr(catalogue, "_discover_catalogue_candidates_unchecked", failed)
    assert discover_catalogue_candidates(_page("<p>ignored</p>")) == []


def test_discovery_caps_candidates_at_twenty_four() -> None:
    html = "".join(f'<a href="/products/{i}">Bean {i}</a>' for i in range(40))
    candidates = discover_catalogue_candidates(_page(html))
    assert len(candidates) == 24
    assert candidates[-1].candidate_id == "candidate-24"


def test_discovery_caps_anchor_inspection_work() -> None:
    html = "".join(f'<a href="/about/{index}">Noise {index}</a>' for index in range(192))
    html += '<a href="/products/after-cap">After cap</a>'
    assert discover_catalogue_candidates(_page(html)) == []


def test_deterministic_rank_combines_roster_gap_and_rated_affinity() -> None:
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/kenya",
            label="Kenya Kiambu",
            evidence="Kenya Kiambu",
            source_order=0,
        ),
        catalogue.CatalogueCandidate(
            candidate_id="candidate-02",
            product_url="https://vendor.example/products/brazil",
            label="Brazil Santos",
            evidence="Brazil Santos",
            source_order=1,
        ),
    ]
    extracted = [
        catalogue._ExtractedCatalogueCandidate.model_validate(  # pyright: ignore[reportPrivateUsage]
            {
                "candidate_id": "candidate-01",
                "name": "Kenya Kiambu",
                "country": "Kenya",
                "processing": "washed",
            }
        ),
        catalogue._ExtractedCatalogueCandidate.model_validate(  # pyright: ignore[reportPrivateUsage]
            {
                "candidate_id": "candidate-02",
                "name": "Brazil Santos",
                "country": "Brazil",
                "processing": "natural",
            }
        ),
    ]
    context = CatalogueRankingContext(
        roster_countries=frozenset({"brazil"}),
        roster_processes=frozenset({"natural"}),
        roster_pairs=frozenset({("brazil", "natural")}),
        rated_pairs=frozenset({("kenya", "washed")}),
    )
    result = rank_catalogue_candidates(candidates, extracted, context)
    assert [item.candidate_id for item in result.recommendations] == [
        "candidate-01",
        "candidate-02",
    ]
    assert result.recommendations[0].score == 4
    assert result.recommendations[0].reason_codes == [
        "missing_country",
        "missing_processing",
        "novel_country_processing",
        "rated_pair_affinity",
    ]
    assert result.recommendations[1].score == 0


def test_rank_ties_preserve_collection_source_order_and_caps_at_three() -> None:
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id=f"candidate-{index:02d}",
            product_url=f"https://vendor.example/products/{index}",
            label=f"Bean {index}",
            evidence=f"Bean {index}",
            source_order=index - 1,
        )
        for index in range(1, 5)
    ]
    extracted = [
        catalogue._ExtractedCatalogueCandidate(  # pyright: ignore[reportPrivateUsage]
            candidate_id=item.candidate_id, name=item.label
        )
        for item in candidates
    ]
    empty = CatalogueRankingContext(
        roster_countries=frozenset(),
        roster_processes=frozenset(),
        roster_pairs=frozenset(),
        rated_pairs=frozenset(),
    )
    result = rank_catalogue_candidates(candidates, extracted, empty)
    assert [item.candidate_id for item in result.recommendations] == [
        "candidate-01",
        "candidate-02",
        "candidate-03",
    ]
    assert result.discovered_count == 4
    assert result.extracted_count == 4


def test_rated_affinity_requires_an_exact_country_processing_pair() -> None:
    candidate = catalogue.CatalogueCandidate(
        candidate_id="candidate-01",
        product_url="https://vendor.example/products/ethiopia-natural",
        label="Ethiopia Natural",
        evidence="Ethiopia Natural",
        source_order=0,
    )
    extracted = catalogue._ExtractedCatalogueCandidate(  # pyright: ignore[reportPrivateUsage]
        candidate_id="candidate-01",
        name="Ethiopia Natural",
        country="Ethiopia",
        processing="natural",
    )
    context = CatalogueRankingContext(
        roster_countries=frozenset({"ethiopia"}),
        roster_processes=frozenset({"natural"}),
        roster_pairs=frozenset({("ethiopia", "natural")}),
        rated_pairs=frozenset({("ethiopia", "washed"), ("colombia", "natural")}),
    )
    result = rank_catalogue_candidates([candidate], [extracted], context)
    assert result.recommendations[0].score == 0
    assert "rated_pair_affinity" not in result.recommendations[0].reason_codes
    unknown = catalogue._ExtractedCatalogueCandidate(  # pyright: ignore[reportPrivateUsage]
        candidate_id="candidate-02", name="Unknown"
    )
    assert rank_catalogue_candidates([candidate], [unknown], context).recommendations == []


@pytest.mark.asyncio
async def test_full_catalogue_pipeline_fetches_once_extracts_once_and_ranks_locally() -> None:
    html = b"""
    <html><body>
      <a href="/products/kenya">Kenya Kiambu Washed</a>
      <p>Kenya Kiambu is a washed green coffee.</p>
      <a href="/products/private">https://vendor.example/private?token=x</a>
    </body></html>
    """
    fetches = 0

    def fetch(request: httpx.Request) -> httpx.Response:
        nonlocal fetches
        fetches += 1
        return httpx.Response(200, stream=_BytesStream(html), request=request)

    model_calls = 0

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        rendered = str(messages)
        assert "candidate-01" in rendered
        assert "private?token=x" not in rendered
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Kenya Kiambu",
                                "country": "Kenya",
                                "processing": "washed",
                            }
                        ]
                    },
                )
            ],
            usage=RequestUsage(input_tokens=10, output_tokens=4),
        )

    diagnostics = BeanSourcingDiagnostics()
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await recommend_from_catalogue(
            "https://vendor.example/collections/green",
            context=CatalogueRankingContext(
                roster_countries=frozenset(),
                roster_processes=frozenset(),
                roster_pairs=frozenset(),
                rated_pairs=frozenset(),
            ),
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=diagnostics,
            http_client=client,
            model=FunctionModel(respond),
        )
    assert fetches == 1
    assert model_calls == 1
    assert result.recommendations[0].candidate_id == "candidate-01"
    assert result.recommendations[0].score == 3
    assert (diagnostics.request_tokens, diagnostics.response_tokens) == (10, 4)


@pytest.mark.asyncio
async def test_extraction_drops_unknown_and_invented_candidate_results() -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {"candidate_id": "candidate-24", "name": "Unknown"},
                            {"candidate_id": "candidate-01", "name": "Invented Name"},
                        ]
                    },
                )
            ]
        )

    with pytest.raises(BeanExtractionError, match="no supported"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(respond),
        )


@pytest.mark.asyncio
async def test_extraction_drops_unstated_country_and_processing_metadata() -> None:
    page = FetchedVendorPage(
        prompt_text="",
        extracted_text="Kenya Kiambu is available now.",
        json_ld_values="",
        raw_html='<a href="/products/kenya">Kenya Kiambu</a>',
        final_url="https://vendor.example/collections/green-coffee",
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Kenya Kiambu",
                                "country": "Ethiopia",
                                "processing": "honey",
                            }
                        ]
                    },
                )
            ]
        )

    extracted = await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
        page,
        candidates,
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(),
        diagnostics=BeanSourcingDiagnostics(),
        model=FunctionModel(respond),
    )
    assert extracted[0].country is None
    assert extracted[0].processing is None


@pytest.mark.asyncio
async def test_extraction_requires_candidate_local_metadata_evidence() -> None:
    page = FetchedVendorPage(
        prompt_text="",
        extracted_text="Kenya Kiambu Washed\nBrazil Santos Natural",
        json_ld_values="",
        raw_html=(
            '<a href="/products/kenya">Kenya Kiambu Washed</a>'
            '<a href="/products/brazil">Brazil Santos Natural</a>'
        ),
        final_url="https://vendor.example/collections/green-coffee",
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Kenya Kiambu",
                                "country": "Brazil",
                                "processing": "natural",
                            }
                        ]
                    },
                )
            ]
        )

    extracted = await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
        page,
        candidates,
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(),
        diagnostics=BeanSourcingDiagnostics(),
        model=FunctionModel(respond),
    )
    assert extracted[0].country is None
    assert extracted[0].processing is None


@pytest.mark.asyncio
async def test_extraction_preserves_metadata_from_candidate_local_card_context() -> None:
    page = FetchedVendorPage(
        prompt_text="",
        extracted_text="Kiambu Lot Kenya Washed",
        json_ld_values="",
        raw_html=(
            '<article><a href="/products/kiambu">Kiambu Lot</a>'
            "<span>Kenya · Washed</span></article>"
        ),
        final_url="https://vendor.example/collections/green-coffee",
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        rendered = str(messages)
        assert "Kiambu Lot Kenya · Washed" in rendered
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Kiambu Lot",
                                "country": "Kenya",
                                "processing": "washed",
                            }
                        ]
                    },
                )
            ]
        )

    extracted = await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
        page,
        candidates,
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(),
        diagnostics=BeanSourcingDiagnostics(),
        model=FunctionModel(respond),
    )
    assert extracted[0].country == "Kenya"
    assert extracted[0].processing == "washed"


@pytest.mark.asyncio
async def test_provider_input_is_capped_at_twelve_server_candidates() -> None:
    html = "".join(f'<a href="/products/{index}">Bean {index}</a>' for index in range(1, 14))
    page = FetchedVendorPage(
        prompt_text="",
        extracted_text=" ".join(f"Bean {index}" for index in range(1, 14)),
        json_ld_values="",
        raw_html=html,
        final_url="https://vendor.example/collections/green-coffee",
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        rendered = str(messages)
        assert "candidate-12" in rendered
        assert "candidate-13" not in rendered
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {"candidate_id": "candidate-12", "name": "Bean 12"},
                            {"candidate_id": "candidate-13", "name": "Bean 13"},
                        ]
                    },
                )
            ]
        )

    extracted = await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
        page,
        candidates,
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(),
        diagnostics=BeanSourcingDiagnostics(),
        model=FunctionModel(respond),
    )
    assert [item.candidate_id for item in extracted] == ["candidate-12"]


@pytest.mark.asyncio
async def test_name_evidence_requires_normalized_word_boundaries() -> None:
    page = FetchedVendorPage(
        prompt_text="",
        extracted_text="JavaScript coffee catalogue",
        json_ld_values="",
        raw_html='<a href="/products/javascript">JavaScript coffee</a>',
        final_url="https://vendor.example/collections/green-coffee",
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"candidates": [{"candidate_id": "candidate-01", "name": "Java"}]},
                )
            ]
        )

    with pytest.raises(BeanExtractionError, match="no supported"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(respond),
        )
    assert not catalogue._page_states_value("punctuation only", "---")  # pyright: ignore[reportPrivateUsage]


def test_extracted_candidate_rejects_non_text_name_after_bidi_filter() -> None:
    with pytest.raises(ValueError, match="name"):
        catalogue._ExtractedCatalogueCandidate.model_validate(  # pyright: ignore[reportPrivateUsage]
            {"candidate_id": "candidate-01", "name": 7}
        )


@pytest.mark.asyncio
async def test_provider_error_is_mapped_to_typed_unavailable_error() -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        raise ModelAPIError("test", "down")

    with pytest.raises(BeanExtractionUnavailableError, match="no usable"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(respond),
        )


@pytest.mark.asyncio
async def test_unexpected_provider_escape_is_mapped_to_typed_unavailable_error() -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        raise RuntimeError("synthetic sdk teardown")

    with pytest.raises(BeanExtractionUnavailableError, match="RuntimeError"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(respond),
        )


@pytest.mark.asyncio
async def test_model_build_error_is_mapped_to_typed_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    def fail_agent(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AdvisorError("missing provider")

    monkeypatch.setattr(catalogue, "_agent", fail_agent)
    with pytest.raises(BeanExtractionUnavailableError, match="could not build"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
            model=None,
        )


@pytest.mark.asyncio
async def test_provider_deadline_is_mapped_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    class SlowAgent:
        async def run(self, prompt: str, *, usage: object) -> object:
            del prompt, usage
            await asyncio.sleep(0.02)
            raise AssertionError("timeout failed to cancel provider")

    def slow_agent(
        advisor_config: AdvisorConfig,
        sourcing_config: BeanSourcingConfig,
        *,
        model: Model | None,
    ) -> SlowAgent:
        del advisor_config, sourcing_config, model
        return SlowAgent()

    monkeypatch.setattr(catalogue, "_agent", slow_agent)

    diagnostics = BeanSourcingDiagnostics()
    with pytest.raises(BeanExtractionUnavailableError, match="exceeded"):
        await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
            page,
            candidates,
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(extraction_timeout_seconds=0.001),
            diagnostics=diagnostics,
            model=None,
        )
    assert diagnostics.timed_out_runs == 1
    assert diagnostics.usage_unreported_requests == 1


@pytest.mark.asyncio
async def test_catalogue_discovery_boundary_unavailable_maps_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fetched(*args: object, **kwargs: object) -> FetchedVendorPage:
        del args, kwargs
        return _page('<a href="/products/kenya">Kenya Kiambu</a>')

    async def unavailable_parse(*args: object, **kwargs: object) -> None:
        del args, kwargs
        return None

    monkeypatch.setattr(catalogue, "fetch_vendor_page", fetched)
    monkeypatch.setattr(catalogue, "run_untrusted_parse_bounded", unavailable_parse)
    with pytest.raises(BeanExtractionUnavailableError, match="temporarily unavailable"):
        await recommend_from_catalogue(
            "https://vendor.example/collections/green",
            context=CatalogueRankingContext(
                roster_countries=frozenset(),
                roster_processes=frozenset(),
                roster_pairs=frozenset(),
                rated_pairs=frozenset(),
            ),
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(fetch_timeout_seconds=0.001),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(lambda messages, info: ModelResponse(parts=[])),
        )


@pytest.mark.asyncio
async def test_catalogue_end_to_end_deadline_maps_to_dependency_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def slow_fetch(*args: object, **kwargs: object) -> FetchedVendorPage:
        del args, kwargs
        await asyncio.sleep(0.02)
        return _page("")

    monkeypatch.setattr(catalogue, "fetch_vendor_page", slow_fetch)
    with pytest.raises(BeanExtractionUnavailableError, match="end-to-end"):
        await recommend_from_catalogue(
            "https://vendor.example/collections/green",
            context=CatalogueRankingContext(
                roster_countries=frozenset(),
                roster_processes=frozenset(),
                roster_pairs=frozenset(),
                rated_pairs=frozenset(),
            ),
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(
                fetch_timeout_seconds=0.001, extraction_timeout_seconds=0.001
            ),
            diagnostics=BeanSourcingDiagnostics(),
            model=FunctionModel(lambda messages, info: ModelResponse(parts=[])),
        )


@pytest.mark.asyncio
async def test_full_pipeline_rejects_catalogue_without_product_links() -> None:
    def fetch(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=_BytesStream(b"<p>No products</p>"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        with pytest.raises(BeanExtractionError, match="no same-origin"):
            await recommend_from_catalogue(
                "https://vendor.example/collections/green",
                context=CatalogueRankingContext(
                    roster_countries=frozenset(),
                    roster_processes=frozenset(),
                    roster_pairs=frozenset(),
                    rated_pairs=frozenset(),
                ),
                advisor_config=AdvisorConfig(),
                sourcing_config=BeanSourcingConfig(),
                diagnostics=BeanSourcingDiagnostics(),
                http_client=client,
                model=FunctionModel(lambda messages, info: ModelResponse(parts=[])),
            )


def test_catalogue_module_does_not_import_roast_control_modules() -> None:
    """Catalogue extraction stays directly outside the safety/control envelope."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(catalogue.__file__).read_text())
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = {
        "roastpilot_agent.controller",
        "roastpilot_agent.safety",
        "roastpilot_agent.mcp_client",
    }
    assert imported.isdisjoint(forbidden)


def test_catalogue_module_does_not_transitively_import_roast_control_modules() -> None:
    """A fresh catalogue import cannot make controller write paths reachable."""
    import subprocess
    import sys

    script = (
        "import sys\n"
        "import roastpilot_agent.catalogue_recommendations\n"
        "loaded = {m for m in sys.modules if m.startswith('roastpilot_agent.')}\n"
        "forbidden = {'roastpilot_agent.controller', 'roastpilot_agent.safety', "
        "'roastpilot_agent.mcp_client'}\n"
        "print(','.join(sorted(loaded & forbidden)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "", result.stderr
