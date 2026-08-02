"""Price extraction helpers for the Kärcher K5 Power Control tracker.

Extraction strategy, in order of preference:
  1. schema.org JSON-LD  (most stable across retailers)
  2. embedded JSON / meta tags  (itemprop="price", og:price:amount, etc.)
  3. per-retailer HTML/regex fallback
  4. (caller) Playwright render, then re-run 1-3 on the rendered HTML
"""

from __future__ import annotations

import json
import re
from typing import Optional

from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PRICE_RE = re.compile(r"(\d{1,4}(?:[.,]\d{2})?)")


def _to_float(value) -> Optional[float]:
    """Coerce a price-ish value ('1,299.00', '$299', 299) into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        price = float(m.group(0))
    except ValueError:
        return None
    # Sanity bound: a K5 realistically sits well under $2000.
    if 20 <= price <= 2000:
        return price
    return None


def _iter_offers(node):
    """Yield every dict that looks like it may carry a price, walking nested
    JSON-LD structures (Product -> offers -> [Offer], @graph, etc.)."""
    if isinstance(node, dict):
        yield node
        for key in ("offers", "@graph", "hasVariant", "itemOffered"):
            child = node.get(key)
            if child is not None:
                yield from _iter_offers(child)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_offers(item)


# ---------------------------------------------------------------------------
# 1. JSON-LD
# ---------------------------------------------------------------------------

def json_ld_price(html: str):
    """Return (price, currency) from schema.org JSON-LD, or (None, None)."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            # Some sites embed multiple concatenated JSON objects; try a
            # lenient recovery of the first {...} block.
            try:
                data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
            except (ValueError, TypeError):
                continue
        for offer in _iter_offers(data):
            if not isinstance(offer, dict):
                continue
            price = offer.get("price") or offer.get("lowPrice") or offer.get("highPrice")
            price = _to_float(price)
            if price is not None:
                currency = offer.get("priceCurrency") or "USD"
                return price, currency
    return None, None


# ---------------------------------------------------------------------------
# 2. Meta tags / itemprop
# ---------------------------------------------------------------------------

def meta_price(html: str):
    """Return (price, currency) from common meta/itemprop patterns."""
    soup = BeautifulSoup(html, "html.parser")

    # og:price:amount / product:price:amount
    for prop in ("product:price:amount", "og:price:amount"):
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            price = _to_float(tag["content"])
            if price is not None:
                cur_tag = soup.find(
                    "meta", attrs={"property": prop.replace("amount", "currency")}
                )
                currency = (cur_tag.get("content") if cur_tag else None) or "USD"
                return price, currency

    # itemprop="price"
    tag = soup.find(attrs={"itemprop": "price"})
    if tag:
        raw = tag.get("content") or tag.get_text()
        price = _to_float(raw)
        if price is not None:
            cur_tag = soup.find(attrs={"itemprop": "priceCurrency"})
            currency = (cur_tag.get("content") if cur_tag else None) or "USD"
            return price, currency

    return None, None


# ---------------------------------------------------------------------------
# 3. Per-retailer HTML fallback
# ---------------------------------------------------------------------------

# Retailer-specific CSS selectors, tried in order. Kept intentionally small and
# easy to extend — add a selector here when a retailer changes its markup.
_HTML_SELECTORS = {
    # Target the buy-box core only — a bare ".a-offscreen" matches accessory and
    # "buy it with" prices too, which produced a wrong $109.99 in testing.
    "amazon": [
        "#corePrice_feature_div .a-price .a-offscreen",
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#price_inside_buybox",
        "#priceblock_ourprice",
    ],
    "homedepot": ['[data-testid="price"]', ".price-format__main-price", ".price"],
    "lowes": ['[data-testid="price"]', ".screen-reader", ".main-price"],
    "walmart": ['[itemprop="price"]', '[data-testid="price-wrap"] span', 'span[data-automation-id="product-price"]'],
    "target": ['[data-test="product-price"]'],
    "bestbuy": ['[data-testid="customer-price"]', ".priceView-customer-price span", ".pricing-price__regular-price"],
    "nfm": ['[itemprop="price"]', ".product-price", ".price"],
    "qvc": [".ProductPrice-currentPrice", '[data-test="product-price"]', ".price"],
    "karcher": [".product-detail__price", ".price"],
}


def html_fallback_price(html: str, retailer_id: str):
    """Return (price, currency) via retailer-specific CSS selectors."""
    soup = BeautifulSoup(html, "html.parser")
    for selector in _HTML_SELECTORS.get(retailer_id, []):
        for el in soup.select(selector):
            price = _to_float(el.get_text())
            if price is not None:
                return price, "USD"
    return None, None


# ---------------------------------------------------------------------------
# Combined static extraction
# ---------------------------------------------------------------------------

def extract_price(html: str, retailer_id: str):
    """Run every static extractor in order; return (price, currency, method)."""
    for name, fn in (
        ("json-ld", lambda: json_ld_price(html)),
        ("meta", lambda: meta_price(html)),
        ("html", lambda: html_fallback_price(html, retailer_id)),
    ):
        price, currency = fn()
        if price is not None:
            return price, currency, name
    return None, None, None
