"""Alternative price sources that don't require scraping the retailer directly.

- SerpApi Google Shopping: one query returns prices from the major retailers
  Google indexes (Amazon, Walmart, Target, Home Depot, Lowe's, ...). This gets
  past the big-box anti-bot blocks because we read Google's aggregated data.
- Best Buy Developer API: official, free, near-real-time pricing by SKU / part
  number — no scraping at all.

Both take an API key (GitHub Actions secret) and return plain floats.
"""

from __future__ import annotations

import os

import requests

SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
BESTBUY_API_KEY = os.environ.get("BESTBUY_API_KEY", "").strip()

TIMEOUT = 30


def _extract_price(value):
    """Coerce SerpApi/Best Buy price values into a float, or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "")
    import re

    m = re.search(r"\d+(?:\.\d+)?", text)
    return float(m.group(0)) if m else None


def _title_matches(title: str, match_terms) -> bool:
    """True if the listing title looks like the intended variant (e.g. CHK)."""
    if not match_terms:
        return True
    low = (title or "").lower()
    return any(term.lower() in low for term in match_terms)


# ---------------------------------------------------------------------------
# SerpApi Google Shopping
# ---------------------------------------------------------------------------

def get_serpapi_prices(query, retailers, match_terms=None, price_range=None):
    """Run one Google Shopping search and map results onto our retailers.

    retailers: list of {id, match:[keywords]} for the serpapi-sourced entries.
    Returns {retailer_id: {"price", "url", "source"}} for matches found.
    """
    out = {}
    if not SERPAPI_KEY or not retailers:
        return out
    try:
        resp = requests.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_shopping",
                "q": query,
                "gl": "us",
                "hl": "en",
                "api_key": SERPAPI_KEY,
            },
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"    SerpApi HTTP {resp.status_code}: {resp.text[:120].strip()}")
            return out
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    SerpApi request error: {str(exc)[:120]}")
        return out

    results = data.get("shopping_results") or []
    # Diagnostics: show what came back so mismatches are debuggable.
    print(f"    SerpApi: {len(results)} shopping_results for {query!r}")
    for it in results[:10]:
        print(f"      [{it.get('source')}] {(it.get('title') or '')[:55]} "
              f"= {it.get('extracted_price') or it.get('price')}")
    if not results and data.get("error"):
        print(f"    SerpApi error field: {data.get('error')}")

    lo, hi = (price_range or (None, None))
    matched = 0
    for item in results:
        title = item.get("title", "")
        if not _title_matches(title, match_terms):
            continue
        price = _extract_price(item.get("extracted_price") or item.get("price"))
        if price is None:
            continue
        if lo is not None and not (lo <= price <= hi):
            continue
        source = (item.get("source") or "").lower()
        link = item.get("product_link") or item.get("link") or ""
        for r in retailers:
            rid = r["id"]
            if rid in out:  # keep the first (best-ranked) match per retailer
                continue
            if any(kw.lower() in source for kw in r.get("match", [])):
                out[rid] = {"price": price, "url": link, "source": item.get("source")}
                matched += 1
    print(f"    SerpApi matched {matched} retailer(s)")
    return out


# ---------------------------------------------------------------------------
# Best Buy Developer API
# ---------------------------------------------------------------------------

def get_bestbuy_price(retailer, price_range=None):
    """Fetch the current Best Buy price via the official Products API.

    Uses the configured `bestbuy_sku` if present, else searches by the Kärcher
    manufacturer part number, else a keyword search. Returns {"price","url"}.
    """
    if not BESTBUY_API_KEY:
        return None

    show = "sku,name,salePrice,regularPrice,onlineAvailability,url"
    sku = retailer.get("bestbuy_sku")
    part = retailer.get("manufacturer_part_number")
    if sku:
        selector = f"(sku={sku})"
    elif part:
        selector = f"(manufacturerPartNumber={part})"
    else:
        terms = retailer.get("bestbuy_search", ["karcher", "k5", "power", "control", "chk"])
        selector = "(" + "&".join(f"search={t}" for t in terms) + ")"

    url = f"https://api.bestbuy.com/v1/products{selector}"
    try:
        resp = requests.get(
            url,
            params={"apiKey": BESTBUY_API_KEY, "format": "json", "show": show, "pageSize": 5},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            print(f"    Best Buy HTTP {resp.status_code}: {resp.text[:120].strip()}")
            return None
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    Best Buy request error: {str(exc)[:120]}")
        return None

    lo, hi = (price_range or (None, None))
    for prod in data.get("products", []):
        price = _extract_price(prod.get("salePrice") or prod.get("regularPrice"))
        if price is None:
            continue
        if lo is not None and not (lo <= price <= hi):
            continue
        return {"price": price, "url": prod.get("url") or retailer.get("url")}
    return None
