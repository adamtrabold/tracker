"""Alternative price sources that don't require scraping the retailer directly.

- SerpApi Google Shopping: one query discovers every site Google lists selling
  the exact product, filtered to the target variant. (The big US chains don't
  appear in this feed and also block scraping, so we surface whichever sites
  Google does list rather than a fixed retailer list.)
- Best Buy Developer API: official, free, near-real-time pricing by SKU / part
  number — no scraping at all.

Both take an API key (GitHub Actions secret).
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


def _slug(text: str) -> str:
    """Turn a retailer/source name into a stable id (e.g. 'Northern Tool')."""
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s or "seller"


# ---------------------------------------------------------------------------
# SerpApi Google Shopping — auto-discover sellers of the exact variant
# ---------------------------------------------------------------------------

def get_serpapi_sellers(query, match_terms, exclude_terms=None, price_range=None,
                        max_sellers=15):
    """Discover every site selling the target product via one Google Shopping
    query, keeping only listings that match the exact variant.

    Big US chains don't appear in this feed (and block scraping), so rather than
    a fixed retailer list we surface whichever sites Google actually lists. A
    listing is kept when its title contains a `match_terms` token (e.g. "chk"
    or the SKU), contains none of `exclude_terms` (other variants like "flex" /
    "premium"), and its price is within `price_range`.

    Returns a list of {"id","name","price","url","source"}, one per seller
    (lowest price kept when a seller lists it more than once).
    """
    if not SERPAPI_KEY:
        return []
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
            return []
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        print(f"    SerpApi request error: {str(exc)[:120]}")
        return []

    results = data.get("shopping_results") or []
    print(f"    SerpApi: {len(results)} shopping_results for {query!r}")
    for it in results[:12]:
        print(f"      [{it.get('source')}] {(it.get('title') or '')[:55]} "
              f"= {it.get('extracted_price') or it.get('price')}")
    if not results and data.get("error"):
        print(f"    SerpApi error field: {data.get('error')}")

    lo, hi = (price_range or (None, None))
    exclude_terms = [t.lower() for t in (exclude_terms or [])]
    by_seller = {}
    for item in results:
        title = item.get("title", "")
        low = title.lower()
        if not _title_matches(title, match_terms):
            continue
        if any(x in low for x in exclude_terms):
            continue
        price = _extract_price(item.get("extracted_price") or item.get("price"))
        if price is None:
            continue
        if lo is not None and not (lo <= price <= hi):
            continue
        source = item.get("source") or "Unknown"
        sid = "serp-" + _slug(source)
        link = item.get("product_link") or item.get("link") or ""
        prev = by_seller.get(sid)
        if prev is None or price < prev["price"]:
            by_seller[sid] = {
                "id": sid, "name": source, "price": price,
                "url": link, "source": source,
            }

    sellers = sorted(by_seller.values(), key=lambda s: s["price"])[:max_sellers]
    print(f"    SerpApi: {len(sellers)} seller(s) matched the exact variant "
          f"({', '.join(s['name'] for s in sellers) or 'none'})")
    return sellers


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
