"""D121 catalogue discovery and deterministic ranking tests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

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
    BeanFetchError,
    BeanSourcingDiagnostics,
    FetchedVendorPage,
    fetch_vendor_page,
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


def test_discovery_does_not_borrow_evidence_from_multi_product_wrapper() -> None:
    html = """
    <section>
      <a href="/products/kiambu">Kiambu Lot</a>
      <a href="/products/santos">Santos Lot</a>
      <span>Kenya · Washed</span>
    </section>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert [candidate.evidence for candidate in candidates] == ["Kiambu Lot", "Santos Lot"]


def test_discovery_stops_card_evidence_at_ancestor_cap() -> None:
    html = (
        "<section><span>Kenya Natural neighbour</span><div><div><div><div>"
        '<a href="/products/kiambu">Kiambu</a>'
        "</div></div></div></div></section>"
    )
    candidates = discover_catalogue_candidates(_page(html))
    assert candidates[0].evidence == "Kiambu"


def test_discovery_caps_card_evidence_text_nodes() -> None:
    nodes = "".join(f"<span>Node {index}</span>" for index in range(70))
    candidates = discover_catalogue_candidates(
        _page(f'<article><a href="/products/kiambu">Kiambu</a>{nodes}</article>')
    )
    assert "Node 62" in candidates[0].evidence
    assert "Node 69" not in candidates[0].evidence


def test_discovery_caps_anchor_label_text_nodes() -> None:
    nodes = "".join(f"<span>{index:02x}</span>" for index in range(70))
    candidates = discover_catalogue_candidates(
        _page(f'<a href="/products/kiambu">Kiambu{nodes}</a>')
    )
    assert "3e" in candidates[0].label
    assert "3f" not in candidates[0].label
    assert "45" not in candidates[0].label


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('aria-label="Kenya Kiambu"', "Kenya Kiambu"),
        ('title="Rwanda Nyamasheke"', "Rwanda Nyamasheke"),
        ('><img alt="Brazil Santos" src="bean.jpg"', "Brazil Santos"),
    ],
)
def test_discovery_uses_accessible_label_for_image_only_product_links(
    markup: str, expected: str
) -> None:
    if markup.startswith(">"):
        html = f'<a href="/products/kiambu"{markup}></a>'
    else:
        html = f'<a href="/products/kiambu" {markup}></a>'

    candidates = discover_catalogue_candidates(_page(html))

    assert len(candidates) == 1
    assert candidates[0].label == expected
    assert candidates[0].evidence == expected


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        (f'aria-label="{"A" * 320}"', "A" * 300),
        (f'title="{"T" * 320}"', "T" * 300),
        (f'><img alt="{"I" * 320}" src="bean.jpg"', "I" * 300),
    ],
)
def test_discovery_caps_accessible_label_characters(markup: str, expected: str) -> None:
    if markup.startswith(">"):
        html = f'<a href="/products/kiambu"{markup}></a>'
    else:
        html = f'<a href="/products/kiambu" {markup}></a>'

    candidates = discover_catalogue_candidates(_page(html))

    assert [candidate.label for candidate in candidates] == [expected]
    assert len(candidates[0].label) == 300


def test_discovery_skips_unlabelled_images_and_caps_alt_scan() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<a href="/products/kiambu"><img src="front.jpg">'
            '<img alt="Kenya Kiambu" src="back.jpg"></a>'
        )
    )
    assert [candidate.label for candidate in candidates] == ["Kenya Kiambu"]

    unlabelled = '<img src="bean.jpg">' * 64
    capped = discover_catalogue_candidates(
        _page(
            f'<a href="/products/kiambu">{unlabelled}<img alt="Outside bound" src="last.jpg"></a>'
        )
    )
    assert capped == []


def test_discovery_accessible_label_precedence_prefers_visible_text() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<a href="/products/kiambu" aria-label="ARIA" title="Title">Visible'
            '<img alt="Image alt" src="bean.jpg"></a>'
        )
    )
    assert [candidate.label for candidate in candidates] == ["Visible"]


def test_discovery_uses_name_from_product_schema_scope() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<a href="/products/kiambu" itemscope itemtype="https://schema.org/Product">'
            '<h3 itemprop="brand">Shop online</h3>'
            '<span itemprop="name">Kiambu Lot</span><span>Kenya · Washed</span></a>'
        )
    )

    assert candidates[0].label == "Kiambu Lot"
    assert candidates[0].evidence == "Shop online Kiambu Lot Kenya · Washed"


def test_discovery_uses_name_from_product_schema_scope_outside_anchor() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<article itemscope itemtype="https://schema.org/Product">'
            '<a href="/products/kiambu"><span itemprop="name">Kiambu Lot</span>'
            "<span>Kenya · Washed</span></a></article>"
        )
    )

    assert candidates[0].label == "Kiambu Lot"
    assert candidates[0].evidence == "Kiambu Lot Kenya · Washed"


@pytest.mark.parametrize("page_type", ["WebPage", "CollectionPage"])
def test_discovery_uses_heading_inside_page_level_schema_scope(page_type: str) -> None:
    candidates = discover_catalogue_candidates(
        _page(
            f'<main itemscope itemtype="https://schema.org/{page_type}">'
            '<a href="/products/kiambu"><h2>Kiambu Lot</h2>'
            "<span>Kenya · Washed</span></a></main>"
        )
    )

    assert candidates[0].label == "Kiambu Lot"
    assert candidates[0].name_label_keys == frozenset({"kiambu lot"})


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ('aria-label="ARIA" title="Title"', "ARIA"),
        ('title="Title"><img alt="Image alt" src="bean.jpg"', "Title"),
    ],
)
def test_discovery_accessible_label_fallback_precedence(markup: str, expected: str) -> None:
    candidates = discover_catalogue_candidates(_page(f'<a href="/products/kiambu" {markup}></a>'))
    assert [candidate.label for candidate in candidates] == [expected]


def test_discovery_keeps_accessible_label_with_sibling_card_metadata() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<article><a href="/products/kiambu"><img alt="Kiambu AA" src="bean.jpg"></a>'
            "<span>Kenya · Washed</span></article>"
        )
    )
    assert candidates[0].label == "Kiambu AA"
    assert candidates[0].evidence == "Kiambu AA Kenya · Washed"


def test_discovery_keeps_sibling_metadata_when_anchor_has_extra_text() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<article><a href="/products/kiambu"><h2>Kiambu Lot</h2>'
            "<span>New</span></a><span>Kenya · Washed</span></article>"
        )
    )

    assert candidates[0].label == "Kiambu Lot"
    assert candidates[0].evidence == "Kiambu Lot New Kenya · Washed"


def test_discovery_preserves_name_when_long_duplicate_evidence_is_split() -> None:
    first_prefix = "P" * 700
    second_prefix = "Q" * 700
    candidates = discover_catalogue_candidates(
        _page(
            f'<article><a href="/products/kiambu">{first_prefix}'
            "<h2>Kiambu Lot</h2></a></article>"
            f'<article><a href="/products/kiambu">{second_prefix}'
            "<h2>Kiambu Lot</h2></a></article>"
        )
    )

    assert candidates[0].label == "Kiambu Lot"
    assert catalogue._page_states_value(  # pyright: ignore[reportPrivateUsage]
        candidates[0].evidence, "Kiambu Lot"
    )
    assert len(candidates[0].evidence) <= 1_200


def test_discovery_keeps_accessible_label_when_card_has_longer_prefix_word() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<article><a href="/products/kiambu"><img alt="Kenya AA" src="bean.jpg"></a>'
            "<span>Kenya AAA · Washed</span></article>"
        )
    )
    assert candidates[0].label == "Kenya AA"
    assert candidates[0].evidence == "Kenya AA Kenya AAA · Washed"


def test_discovery_climbs_past_wrapper_that_only_repeats_visible_label() -> None:
    candidates = discover_catalogue_candidates(
        _page(
            '<article><h2><a href="/products/kiambu">Kenya Kiambu</a></h2>'
            "<span>Washed · SL28</span></article>"
        )
    )
    assert candidates[0].label == "Kenya Kiambu"
    assert candidates[0].evidence == "Kenya Kiambu Washed · SL28"


def test_discovery_rejects_evidence_from_wrapper_beyond_link_scan_cap() -> None:
    repeated = "".join('<a href="/products/kiambu">Kiambu</a>' for _ in range(9))
    html = f"<section>{repeated}<span>Neighbour Kenya Natural</span></section>"
    candidates = discover_catalogue_candidates(_page(html))
    assert candidates[0].evidence == "Kiambu"


def test_discovery_merges_richer_card_evidence_for_duplicate_json_ld_url() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Kiambu Lot","url":"/products/kiambu"}
    </script>
    <article><a href="/products/kiambu">Kiambu Lot</a><span>Kenya · Washed</span></article>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert len(candidates) == 1
    assert candidates[0].evidence == "Kiambu Lot Kenya · Washed"


def test_discovery_keeps_richer_json_ld_evidence_for_sparse_duplicate_link() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Kiambu Lot","url":"/products/kiambu",
       "countryOfOrigin":{"name":"Kenya"},"process":"washed"}
    </script>
    <a href="/products/kiambu">Kiambu Lot</a>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert len(candidates) == 1
    assert "Kenya" in candidates[0].evidence
    assert "washed" in candidates[0].evidence


def test_candidate_evidence_merge_uses_word_boundaries() -> None:
    merge = catalogue._merge_candidate_evidence  # pyright: ignore[reportPrivateUsage]
    assert merge("Kiambu AAA Kenya", "Kiambu AA") == "Kiambu AAA Kenya Kiambu AA"
    assert merge("Kiambu AA Kenya", "Kiambu AA") == "Kiambu AA Kenya"


@pytest.mark.parametrize("existing", ["description " * 200, "x" * 1200])
def test_candidate_evidence_merge_reserves_space_for_late_card_metadata(existing: str) -> None:
    merge = catalogue._merge_candidate_evidence  # pyright: ignore[reportPrivateUsage]
    merged = merge(existing, "Kiambu Lot Kenya Washed")

    assert len(merged) <= 1200
    assert merged.endswith("Kiambu Lot Kenya Washed")


def test_discovery_preserves_bounded_duplicate_name_keys_beyond_prompt_cap() -> None:
    labels = [("A" * 299) + str(index) for index in range(4)]
    html = "".join(f'<a href="/products/same">{label}</a>' for label in labels)

    candidates = discover_catalogue_candidates(_page(html))

    assert candidates[0].name_label_keys == frozenset(label.casefold() for label in labels)
    assert len(candidates[0].evidence) <= 1200


def test_discovery_rejects_userinfo_and_non_product_anchor_paths() -> None:
    page = _page(
        '<a href="https://user:secret@vendor.example/products/a">A</a><a href="/about">About</a>'
    )
    assert discover_catalogue_candidates(page) == []


def test_provider_text_redacts_url_forms_without_touching_product_words() -> None:
    redact = catalogue._redact_urls  # pyright: ignore[reportPrivateUsage]
    assert redact("Kenya HTTPS://vendor.example/products/a?token=x washed") == (
        "Kenya [link] washed"
    )
    assert redact("ß" * 45 + "https://vendor.example/products/a?token=x washed") == (
        "ß" * 45 + "[link] washed"
    )
    assert redact("//vendor.example/private?token=x washed") == "[link] washed"
    assert redact("www.vendor.example/private?token=x washed") == "[link] washed"
    assert redact("vendor.example/private?token=x washed") == "[link] washed"
    assert redact("ftp://user:secret@vendor.example/private washed") == "[link] washed"
    assert redact("https:products/kenya-washed Kenya") == "[link] Kenya"
    assert redact(r"https:\\vendor.example\products\kenya-washed Kenya") == "[link] Kenya"
    assert redact("data:text/plain;base64,c2VjcmV0 washed") == "[link] washed"
    assert redact("mailto:sales@vendor.example washed") == "[link] washed"
    assert redact("javascript:alert(1) washed") == "[link] washed"
    assert redact("https://vendor.example/page?ref=(x)&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=(a(b)c)&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=(a(b(c)d)e)&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=(a(b(c(d)e)f)g)&token=SECRET more") == (
        "[link] more"
    )
    assert redact("https://vendor.example/page?ref=%28x%29&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=(x&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=x)&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/page?ref=(x))&token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/a_(b).html?token=SECRET more") == "[link] more"
    assert redact("https://vendor.example/a_(b):Country=Kenya washed") == (
        "[link]:Country=Kenya washed"
    )
    assert redact("https://[2001:db8::1]:8443/products/private Kenya") == "[link] Kenya"
    assert redact("https://vendor.example/page?ref=(x),Kenya washed") == "[link],Kenya washed"
    assert redact("<https://vendor.example/products/a>Kenya washed") == "<[link]>Kenya washed"
    assert redact("“https://vendor.example/products/a”Kenya washed") == "“[link]”Kenya washed"
    assert redact("<https://vendor.example/products/a>Country=Kenya more") == (
        "<[link]>Country=Kenya more"
    )
    assert redact("<https://vendor.example/products/a>&token=SECRET more") == "<[link] more"
    assert redact("“https://vendor.example/products/a”&token=SECRET more") == "“[link] more"
    assert redact("“https://vendor.example/products/a”%2523private more") == "“[link] more"
    assert redact("(https://vendor.example/page?ref=(x)&token=SECRET) more") == "([link]) more"
    assert redact("(checkout?ref=(x)&token=SECRET) more") == "([link]) more"
    assert redact("((checkout?ref=(x)&token=SECRET)) more") == "(([link])) more"
    assert redact("“https://vendor.example/a?token=x” Kenya") == "“[link]” Kenya"
    assert redact("{https://vendor.example/a?token=x} Kenya") == "{[link]} Kenya"
    assert redact("[https://[2001:db8::1]/a?token=x] Kenya") == "[[link]] Kenya"
    assert redact("https://vendor.example/a?token=x and https://vendor.example/b?token=y") == (
        "[link] and [link]"
    )
    assert (
        redact(
            "https://vendor.example/a?ref=(x)&token=a and https://vendor.example/b?ref=x)&token=b"
        )
        == "[link] and [link]"
    )
    assert redact("https://vendor.example/a?ref=(x[y)&token=SECRET] more") == "[link] more"
    assert redact("192.0.2.1/private?token=x washed") == "[link] washed"
    assert redact("example.xn--p1ai/private?token=x washed") == "[link] washed"
    assert redact("https://[2001:db8::1]/private?token=x washed") == "[link] washed"
    assert redact("/products/a?token=secret washed") == "[link] washed"
    assert redact("./products/a?token=secret washed") == "[link] washed"
    assert redact("../products/a?token=secret washed") == "[link] washed"
    assert redact("products/a?token=secret washed") == "[link] washed"
    assert redact("products/a washed") == "[link] washed"
    assert redact("catalog/kenya washed") == "[link] washed"
    assert redact("catalogue/kenya washed") == "[link] washed"
    assert redact("?token=secret washed") == "[link] washed"
    assert redact("#access_token=secret washed") == "[link] washed"
    assert redact("checkout?token=secret washed") == "[link] washed"
    assert redact("checkout?flag&token=secret washed") == "[link] washed"
    assert redact("checkout?flag#section washed") == "checkout?flag#section washed"
    assert redact("(checkout?token=secret) washed") == "([link]) washed"
    assert redact("((( washed") == "((( washed"
    assert redact("orders@vendor.example/track?id=1 washed") == "[link] washed"
    assert redact("misc/path?token=secret washed") == "[link] washed"
    assert redact("https%3A%2F%2Fvendor.example%2Fproducts%2Fa%3Ftoken%3Dx washed") == (
        "[link] washed"
    )
    assert redact("redirect=https%253A%252F%252Fvendor.example%252Fa%253Ftoken%253Dx washed") == (
        "[link] washed"
    )
    assert redact("https%25253A%25252F%25252Fvendor.example%25252Fa washed") == "[link] washed"
    assert (
        redact(
            "https&#58;&#47;&#47;vendor.example&#47;products&#47;kenya&#63;token&#61;secret washed"
        )
        == "[link] washed"
    )
    assert redact("https&amp;#58;&amp;#47;&amp;#47;vendor.example&amp;#47;private washed") == (
        "[link] washed"
    )
    assert redact("Fair &amp; Trade Caf&eacute; washed") == "Fair &amp; Trade Caf&eacute; washed"
    assert redact("products%2Fkenya%3Ftoken%3DSECRET washed") == "[link] washed"
    assert redact("products%252Fkenya%253Ftoken%253DSECRET washed") == "[link] washed"
    assert redact("1 /2 lb and 1/2 kg") == "1 /2 lb and 1/2 kg"
    assert redact("SL28/SL34 and Caturra/Castillo") == "SL28/SL34 and Caturra/Castillo"
    assert redact("washed/natural 12oz/340g AA/AB") == "washed/natural 12oz/340g AA/AB"
    assert redact("faq/shipping washed") == "faq/shipping washed"
    assert redact("Country: Kenya Process: washed") == "Country: Kenya Process: washed"
    assert redact("Country:Kenya Process:Washed SKU:ABC123") == (
        "Country:Kenya Process:Washed SKU:ABC123"
    )
    assert redact("Country=Kenya;Process=Washed") == "Country=Kenya;Process=Washed"
    assert redact("Country=Kenya&Process=Washed") == "Country=Kenya&Process=Washed"
    assert redact("farmer@vendor.example washed") == "farmer@vendor.example washed"
    benign_unmatched = "Loved this batch (well, mostly - Sumatra Mandheling tasted clean"
    assert redact(benign_unmatched) == benign_unmatched


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


def test_json_ld_evidence_preserves_structured_fields_before_long_description() -> None:
    evidence = catalogue._json_ld_product_evidence(  # pyright: ignore[reportPrivateUsage]
        {
            "description": "marketing " * 300,
            "countryOfOrigin": {"name": "Kenya"},
            "process": "washed",
        },
        "Kiambu Lot",
    )

    assert len(evidence) <= 1200
    assert "Kenya" in evidence
    assert "washed" in evidence


@pytest.mark.parametrize(
    ("value", "require_product_path"),
    [
        ("https://vendor.example:bad/products/a", False),
        ("ftp://vendor.example/products/a", False),
        ("https://other.example/products/a", False),
        ("/collections/all", True),
        ("/products", True),
        ("/products?page=2", True),
        ("/shop/product/", True),
        ("/shop/all", True),
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


def test_discovery_navigation_root_does_not_displace_first_twelve_products() -> None:
    navigation = '<a href="/products?page=2">All products</a>'
    products = "".join(f'<a href="/products/{index}">Bean {index}</a>' for index in range(1, 13))
    candidates = discover_catalogue_candidates(_page(navigation + products))
    assert [candidate.product_url for candidate in candidates[:12]] == [
        f"https://vendor.example/products/{index}" for index in range(1, 13)
    ]


def test_candidate_url_normalization_preserves_query_order_and_default_ports() -> None:
    normalize = catalogue._same_origin_product_url  # pyright: ignore[reportPrivateUsage]
    value = "https://vendor.example:443/products/a?variant=red&variant=blue&sig=a%2Fb"
    assert normalize(
        value,
        base_url="https://vendor.example/collections/green",
        require_product_path=True,
    ) == ("https://vendor.example/products/a?variant=red&variant=blue&sig=a%2Fb")
    assert (
        normalize(
            "https://vendor.example:8443/products/a",
            base_url="https://vendor.example:8443/collections/green",
            require_product_path=True,
        )
        == "https://vendor.example:8443/products/a"
    )
    assert (
        normalize(
            "https://[2001:db8::1]/products/a",
            base_url="https://[2001:db8::1]/collections/green",
            require_product_path=True,
        )
        == "https://[2001:db8::1]/products/a"
    )
    assert (
        normalize(
            "/coffee/kenya-aa",
            base_url="https://vendor.example/collections/green",
            require_product_path=True,
        )
        == "https://vendor.example/coffee/kenya-aa"
    )
    assert (
        normalize(
            "/shop/kenya-aa",
            base_url="https://vendor.example/collections/green",
            require_product_path=True,
        )
        == "https://vendor.example/shop/kenya-aa"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://vendor.example:0/products/a",
        "https://vendor.example/products/\u202ea",
    ],
)
def test_candidate_url_normalization_rejects_wrong_port_and_bidi(value: str) -> None:
    normalize = catalogue._same_origin_product_url  # pyright: ignore[reportPrivateUsage]
    assert (
        normalize(
            value,
            base_url="https://vendor.example/collections/green",
            require_product_path=True,
        )
        is None
    )


def test_candidate_url_normalization_rejects_browser_ambiguous_characters() -> None:
    normalize = catalogue._same_origin_product_url  # pyright: ignore[reportPrivateUsage]
    for value in (r"/\evil.example/products/a", "/products/a\nignored"):
        assert (
            normalize(
                value,
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


@pytest.mark.parametrize(
    "unusable_url", ["", "https://vendor.example:bad/product", "https://evil.example/products/a"]
)
def test_discovery_falls_back_to_json_ld_id_when_url_is_unusable(unusable_url: str) -> None:
    html = f"""
    <script type="application/ld+json">
      {{"@type":"Product","name":"Kiambu Lot","url":"{unusable_url}",
       "@id":"/products/kiambu"}}
    </script>
    """

    candidates = discover_catalogue_candidates(_page(html))

    assert [(item.label, item.product_url) for item in candidates] == [
        ("Kiambu Lot", "https://vendor.example/products/kiambu")
    ]


def test_discovery_ignores_fragment_only_json_ld_product_identifier() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Not a locator","@id":"#product"}
    </script>
    """
    assert discover_catalogue_candidates(_page(html)) == []


def test_discovery_rejects_opaque_and_non_product_json_ld_identifiers() -> None:
    html = """
    <script type="application/ld+json">
      {"@type":"Product","name":"Opaque","@id":"sku-123"}
    </script>
    <script type="application/ld+json">
      {"@type":"Product","name":"Collection","@id":"/collections/green#sku-123"}
    </script>
    <script type="application/ld+json">
      {"@type":"Product","name":"Locator","@id":"/products/locator#sku-123"}
    </script>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert [(item.label, item.product_url) for item in candidates] == [
        ("Locator", "https://vendor.example/products/locator")
    ]


def test_discovery_accepts_decoded_xhtml_with_xml_encoding_declaration() -> None:
    html = '<?xml version="1.0" encoding="UTF-8"?><a href="/products/a">Café A</a>'
    candidates = discover_catalogue_candidates(_page(html))
    assert [(item.label, item.product_url) for item in candidates] == [
        ("Café A", "https://vendor.example/products/a")
    ]


def test_discovery_caps_matching_json_ld_after_skipping_ordinary_scripts() -> None:
    html = "".join("<script>analytics</script>" for _ in range(24))
    html += """
    <script type="Application/LD+JSON; charset=utf-8">
      {"@type":"Product","name":"Bean Route","url":"/beans/route"}
    </script>
    """
    candidates = discover_catalogue_candidates(_page(html))
    assert [(candidate.label, candidate.product_url) for candidate in candidates] == [
        ("Bean Route", "https://vendor.example/beans/route")
    ]


def test_discovery_fails_soft_on_unexpected_parser_escape(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def failed(page: FetchedVendorPage) -> list[catalogue.CatalogueCandidate]:
        del page
        raise RuntimeError("synthetic parser escape")

    monkeypatch.setattr(catalogue, "_discover_catalogue_candidates_unchecked", failed)
    assert discover_catalogue_candidates(_page("<p>ignored</p>")) == []
    assert "catalogue discovery failed soft" in caplog.text


def test_discovery_caps_candidates_at_twenty_four() -> None:
    html = "".join(f'<a href="/products/{i}">Bean {i}</a>' for i in range(40))
    candidates = discover_catalogue_candidates(_page(html))
    assert len(candidates) == 24
    assert candidates[-1].candidate_id == "candidate-24"

    json_ld_html = "".join(
        '<script type="application/ld+json">'
        f'{{"@type":"Product","name":"Bean {index}","url":"/beans/{index}"}}'
        "</script>"
        for index in range(25)
    )
    json_ld_candidates = discover_catalogue_candidates(_page(json_ld_html))
    assert len(json_ld_candidates) == 24
    assert json_ld_candidates[-1].label == "Bean 23"


def test_discovery_caps_anchor_inspection_work() -> None:
    html = "".join(f'<a href="/about/{index}">Noise {index}</a>' for index in range(192))
    html += '<a href="/products/after-cap">After cap</a>'
    assert discover_catalogue_candidates(_page(html)) == []


def test_deterministic_rank_combines_roster_gap_and_rated_affinity() -> None:
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/brazil",
            label="Brazil Santos",
            evidence="Brazil Santos",
            source_order=0,
        ),
        catalogue.CatalogueCandidate(
            candidate_id="candidate-02",
            product_url="https://vendor.example/products/kenya",
            label="Kenya Kiambu",
            evidence="Kenya Kiambu",
            source_order=1,
        ),
    ]
    extracted = [
        catalogue._ExtractedCatalogueCandidate.model_validate(  # pyright: ignore[reportPrivateUsage]
            {
                "candidate_id": "candidate-01",
                "name": "Brazil Santos",
                "country": "Brazil",
                "processing": "natural",
            }
        ),
        catalogue._ExtractedCatalogueCandidate.model_validate(  # pyright: ignore[reportPrivateUsage]
            {
                "candidate_id": "candidate-02",
                "name": "Kenya Kiambu",
                "country": "Kenya",
                "processing": "washed",
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
        "candidate-02",
        "candidate-01",
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


def test_rank_maps_server_candidate_validation_drift_to_typed_error() -> None:
    candidate = catalogue.CatalogueCandidate(
        candidate_id="candidate-01",
        product_url=r"https://vendor.example/\evil.example/products/a",
        label="Kiambu",
        evidence="Kiambu",
        source_order=0,
    )
    extracted = catalogue._ExtractedCatalogueCandidate(  # pyright: ignore[reportPrivateUsage]
        candidate_id="candidate-01", name="Kiambu"
    )
    empty = CatalogueRankingContext(
        roster_countries=frozenset(),
        roster_processes=frozenset(),
        roster_pairs=frozenset(),
        rated_pairs=frozenset(),
    )
    with pytest.raises(BeanExtractionError, match="failed output validation"):
        rank_catalogue_candidates([candidate], [extracted], empty)
    oversized = [
        catalogue.CatalogueCandidate(
            candidate_id=f"candidate-{index:02d}",
            product_url=f"https://vendor.example/products/{index}",
            label=f"Bean {index}",
            evidence=f"Bean {index}",
            source_order=index,
        )
        for index in range(1, 26)
    ]
    with pytest.raises(BeanExtractionError, match="list failed validation"):
        rank_catalogue_candidates(oversized, [], empty)


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
async def test_full_catalogue_pipeline_fetches_once_extracts_once_and_ranks_locally(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
                                "name": "Kenya Kiambu Washed",
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
    caplog.set_level("INFO")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        result = await recommend_from_catalogue(
            "https://vendor.example/collections/green?secret=no-log",
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
    assert "fetching catalogue page" in caplog.text
    assert "vendor.example" not in caplog.text
    assert "collections/green" not in caplog.text
    assert "secret=no-log" not in caplog.text


@pytest.mark.asyncio
async def test_catalogue_transport_log_suppression_is_task_scoped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    collection_url = "https://vendor.example/collections/private?secret=no-log"

    async def fetch(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("request=%s", request.url)
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            stream=_BytesStream(b"<html><body>catalogue</body></html>"),
            request=request,
        )

    caplog.set_level("DEBUG")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fetch)) as client:
        catalogue_fetch = asyncio.create_task(
            fetch_vendor_page(
                collection_url,
                config=BeanSourcingConfig(),
                http_client=client,
                log_url=False,
            )
        )
        await entered.wait()
        logging.getLogger("httpx").info("unrelated request marker")
        release.set()
        await catalogue_fetch

    assert "unrelated request marker" in caplog.text
    assert "collections/private" not in caplog.text
    assert "secret=no-log" not in caplog.text


@pytest.mark.asyncio
async def test_catalogue_transport_log_suppression_cleans_up_after_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    collection_url = "https://vendor.example/collections/private?secret=no-log"
    logger_names = ("httpx", "httpcore.http11")
    original_filters = {name: tuple(logging.getLogger(name).filters) for name in logger_names}

    def fail(request: httpx.Request) -> httpx.Response:
        logging.getLogger("httpcore.http11").debug("request=%s", request.url)
        raise httpx.ConnectError("catalogue transport failed", request=request)

    caplog.set_level("DEBUG")
    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        with pytest.raises(BeanFetchError, match="ConnectError"):
            await fetch_vendor_page(
                collection_url,
                config=BeanSourcingConfig(),
                http_client=client,
                log_url=False,
            )

    logging.getLogger("httpx").info("post-failure transport marker")
    assert "post-failure transport marker" in caplog.text
    assert "collections/private" not in caplog.text
    assert "secret=no-log" not in caplog.text
    assert {
        name: tuple(logging.getLogger(name).filters) for name in logger_names
    } == original_filters


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@vendor.example/collections/green",
        "https://vendor.example/collections/green#access_token=secret",
    ],
)
@pytest.mark.asyncio
async def test_catalogue_rejection_logs_no_collection_locator(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger="roastpilot_agent.bean_sourcing")

    with pytest.raises(BeanFetchError):
        await recommend_from_catalogue(
            url,
            context=CatalogueRankingContext(
                roster_countries=frozenset(),
                roster_processes=frozenset(),
                roster_pairs=frozenset(),
                rated_pairs=frozenset(),
            ),
            advisor_config=AdvisorConfig(),
            sourcing_config=BeanSourcingConfig(),
            diagnostics=BeanSourcingDiagnostics(),
        )

    assert "rejected a catalogue URL" in caplog.text
    assert "vendor.example" not in caplog.text
    assert "collections/green" not in caplog.text
    assert "secret" not in caplog.text


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
async def test_extraction_maps_malformed_candidate_id_to_typed_unavailable_error() -> None:
    page = _page('<a href="/products/kenya">Kenya Kiambu</a>')
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"candidates": [{"candidate_id": "https://evil.example", "name": "Kenya"}]},
                )
            ]
        )

    with pytest.raises(BeanExtractionUnavailableError, match="no usable result"):
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
async def test_extraction_cannot_ground_metadata_in_redacted_url_tokens() -> None:
    page = _page("")
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/mystery",
            label="Mystery Lot",
            evidence=(
                "Mystery Lot https://vendor.example/kenya/washed?token=secret "
                "https%3A%2F%2Fvendor.example%2Fproducts%2Fa%3Fencoded_token%3Dsecret "
                "https%253A%252F%252Fvendor.example%252Fa%253Fnested_token%253Dsecret "
                "products%252Fkenya%253Frelative_token%253Dsecret"
            ),
            source_order=0,
        )
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        rendered = str(messages)
        assert "token=secret" not in rendered
        assert "kenya/washed" not in rendered.casefold()
        assert "encoded_token" not in rendered
        assert "nested_token" not in rendered
        assert "relative_token" not in rendered
        assert "https%3a" not in rendered.casefold()
        assert "https%253a" not in rendered.casefold()
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Mystery Lot",
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
    assert extracted[0].name == "Mystery Lot"
    assert extracted[0].country is None
    assert extracted[0].processing is None


@pytest.mark.asyncio
async def test_extraction_does_not_ground_redaction_sentinel() -> None:
    assert not catalogue._page_states_value(  # pyright: ignore[reportPrivateUsage]
        "Mystery Lot [link]", "link"
    )
    page = _page("")
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/mystery",
            label="Mystery Lot",
            evidence="Mystery Lot https://vendor.example/private?token=secret",
            source_order=0,
        )
    ]

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert "[link]" in str(messages)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Mystery Lot",
                                "country": "link",
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


@pytest.mark.asyncio
async def test_extraction_does_not_ground_ambiguous_other_processing() -> None:
    page = _page("")
    candidates = [
        catalogue.CatalogueCandidate(
            candidate_id="candidate-01",
            product_url="https://vendor.example/products/mystery",
            label="Mystery Lot",
            evidence="Mystery Lot and other origins",
            source_order=0,
        )
    ]

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
                                "name": "Mystery Lot",
                                "processing": "other",
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
                                "name": "Kenya Kiambu Washed",
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


@pytest.mark.asyncio
async def test_name_grounding_uses_candidate_label_not_card_metadata() -> None:
    page = _page(
        '<article><a href="/products/kiambu">Kiambu Lot</a><span>Kenya · Washed</span></article>'
    )
    candidates = discover_catalogue_candidates(page)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"candidates": [{"candidate_id": "candidate-01", "name": "Kenya"}]},
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
async def test_name_grounding_accepts_each_duplicate_product_label() -> None:
    page = _page(
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Kiambu","url":"/products/kiambu"}'
        "</script>"
        '<article><a href="/products/kiambu">Kiambu AA Washed Coffee</a>'
        "<span>Kenya</span></article>"
    )
    candidates = discover_catalogue_candidates(page)
    assert candidates[0].name_label_keys == frozenset({"kiambu", "kiambu aa washed coffee"})

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
                                "name": "Kiambu AA Washed Coffee",
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

    assert [item.name for item in extracted] == ["Kiambu AA Washed Coffee"]


@pytest.mark.asyncio
async def test_name_grounding_rejects_metadata_inside_whole_card_anchor() -> None:
    page = _page(
        '<a href="/products/kiambu" itemscope itemtype="https://schema.org/Product">'
        '<span itemprop="brand" itemscope itemtype="https://schema.org/Brand">'
        '<h3 itemprop="name">Acme Roasters</h3></span>'
        '<span itemprop="name">Kiambu Lot</span><span>Kenya · Washed</span></a>'
    )
    candidates = discover_catalogue_candidates(page)
    assert candidates[0].name_label_keys == frozenset({"kiambu lot"})
    assert candidates[0].evidence == "Acme Roasters Kiambu Lot Kenya · Washed"

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"candidates": [{"candidate_id": "candidate-01", "name": "Kenya"}]},
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

    def respond_with_heading_name(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"candidates": [{"candidate_id": "candidate-01", "name": "Kiambu Lot"}]},
                )
            ]
        )

    extracted = await catalogue._extract(  # pyright: ignore[reportPrivateUsage]
        page,
        candidates,
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(),
        diagnostics=BeanSourcingDiagnostics(),
        model=FunctionModel(respond_with_heading_name),
    )
    assert [item.name for item in extracted] == ["Kiambu Lot"]


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
async def test_provider_owns_full_timeout_and_records_usage_after_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timeout_budgets: list[float | None] = []
    active_timeout_budgets: list[float | None] = []
    provider_timeout_context: tuple[float | None, ...] | None = None
    real_timeout = asyncio.timeout

    @asynccontextmanager
    async def tracked_timeout(delay: float | None) -> AsyncGenerator[asyncio.Timeout]:
        timeout_budgets.append(delay)
        async with real_timeout(delay) as deadline:
            active_timeout_budgets.append(delay)
            try:
                yield deadline
            finally:
                assert active_timeout_budgets.pop() == delay

    async def fetched(*args: object, **kwargs: object) -> FetchedVendorPage:
        del args, kwargs
        return _page('<a href="/products/kenya">Kenya Kiambu</a>')

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages
        nonlocal provider_timeout_context
        provider_timeout_context = tuple(active_timeout_budgets)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {
                        "candidates": [
                            {
                                "candidate_id": "candidate-01",
                                "name": "Kenya Kiambu",
                            }
                        ]
                    },
                )
            ],
            usage=RequestUsage(input_tokens=5, output_tokens=2),
        )

    monkeypatch.setattr(catalogue, "fetch_vendor_page", fetched)
    monkeypatch.setattr(asyncio, "timeout", tracked_timeout)
    diagnostics = BeanSourcingDiagnostics()
    result = await recommend_from_catalogue(
        "https://vendor.example/collections/green",
        context=CatalogueRankingContext(
            roster_countries=frozenset(),
            roster_processes=frozenset(),
            roster_pairs=frozenset(),
            rated_pairs=frozenset(),
        ),
        advisor_config=AdvisorConfig(),
        sourcing_config=BeanSourcingConfig(
            fetch_timeout_seconds=2.0,
            extraction_timeout_seconds=11.0,
        ),
        diagnostics=diagnostics,
        model=FunctionModel(respond),
    )
    assert result.recommendations[0].candidate_id == "candidate-01"
    assert timeout_budgets == [6.0, 2.0, 11.0]
    assert provider_timeout_context == (11.0,)
    assert diagnostics.timed_out_runs == 0
    assert (diagnostics.request_tokens, diagnostics.response_tokens) == (5, 2)


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
