#!/usr/bin/env python3
"""Automated price scraper for the Kärcher K5 Power Control.

Reads config/retailers.json and gets each retailer's price via the source best
suited to it, then writes:
  - data/latest.json   : current snapshot the website reads
  - data/history.json  : append-only price history for the charts

Per-retailer sources (config `source` field):
  - "serpapi" : one Google Shopping query (SerpApi) covers the big-box majors
                (Amazon, Walmart, Target, Home Depot, Lowe's) that block direct
                scraping — read from Google's aggregated data instead.
  - "bestbuy" : Best Buy's official free Products API (near-real-time price).
  - "direct"  : fetch the retailer page directly (scraping API if keyed, else a
                plain fetch + headless-browser fallback) for sites Google skips.

Design goals:
  - Never crash the whole run because one retailer fails (per-retailer guard).
  - Prefer stable structured data (JSON-LD) over brittle HTML scraping.
  - On failure, record status="unavailable" and preserve the last known price.

Fetch strategy:
  - If a scraping-API key is configured (env SCRAPER_API_KEY), every request is
    routed through that service, which rotates residential IPs and renders JS
    server-side. This is what gets past the big retailers' 403 anti-bot blocks.
    Set SCRAPER_API_PROVIDER to scraperapi (default), scrapingbee, or zenrows.
  - With no key, it falls back to a direct HTTP fetch plus a headless-browser
    render — free, but blocked by most big-box retailers.

Sanity guard:
  - config.product.expected_price_range = [min, max] rejects obviously-wrong
    scrapes (e.g. an accessory price picked up by an HTML fallback), so a bad
    value never shows on the site as the lowest price.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extractors import extract_price  # noqa: E402
from sources import (  # noqa: E402
    BESTBUY_API_KEY,
    SERPAPI_KEY,
    get_bestbuy_price,
    get_serpapi_prices,
)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "retailers.json"
DATA_DIR = ROOT / "data"
LATEST_PATH = DATA_DIR / "latest.json"
HISTORY_PATH = DATA_DIR / "history.json"

# A realistic desktop User-Agent reduces trivial bot-blocking.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

REQUEST_TIMEOUT = 25
# Non-render API calls are fast (~5-15s); JS-render calls are slow (up to ~70s)
# and cost ~10x more credits, so we only escalate to render when needed.
API_TIMEOUT_FAST = 60
API_TIMEOUT_RENDER = 70
MAX_RETRIES = 3
MAX_WORKERS = 4  # scrape retailers concurrently to keep wall-clock bounded

# Optional scraping-API passthrough (set as a GitHub Actions secret).
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()
# Use `or` (not a get-default) so an env var present-but-empty — which is what
# GitHub injects for an unset repo variable — still falls back to the default.
SCRAPER_API_PROVIDER = (os.environ.get("SCRAPER_API_PROVIDER") or "scraperapi").strip().lower()
# Proxy tier: "" (standard, 1 credit), "premium" (~10-25 credits, gets past most
# big-box anti-bot), or "ultra" (~30 credits, hardest sites). Costs more credits.
SCRAPER_API_PREMIUM = (os.environ.get("SCRAPER_API_PREMIUM") or "").strip().lower()


def build_proxy_url(url: str, render: bool = False):
    """Wrap a target URL in the configured scraping-API request URL.

    render=False fetches the raw HTML (fast/cheap — enough when the price is in
    JSON-LD/meta); render=True runs the page's JS server-side (slow/costly).
    The SCRAPER_API_PREMIUM tier adds premium/ultra proxies for hard sites.
    """
    if not SCRAPER_API_KEY:
        return None
    target = quote(url, safe="")
    flag = "true" if render else "false"
    if SCRAPER_API_PROVIDER == "scraperapi":
        extra = ""
        if SCRAPER_API_PREMIUM == "ultra":
            extra = "&ultra_premium=true"
        elif SCRAPER_API_PREMIUM in ("premium", "1", "true"):
            extra = "&premium=true"
        return (
            f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render={flag}&country_code=us{extra}"
        )
    if SCRAPER_API_PROVIDER == "scrapingbee":
        extra = "&premium_proxy=true" if SCRAPER_API_PREMIUM else ""
        return (
            f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render_js={flag}&country_code=us{extra}"
        )
    if SCRAPER_API_PROVIDER == "zenrows":
        base = (
            f"https://api.zenrows.com/v1/?apikey={SCRAPER_API_KEY}"
            f"&url={target}&premium_proxy=true&proxy_country=us"
        )
        return base + "&js_render=true" if render else base
    raise ValueError(f"Unknown SCRAPER_API_PROVIDER: {SCRAPER_API_PROVIDER!r}")


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return default


def fetch_html_via_api(url: str, render: bool = False):
    """Fetch through the scraping API. One bounded attempt (+1 retry only on a
    transient error) so a slow site can't stall the whole run."""
    proxy_url = build_proxy_url(url, render=render)
    if not proxy_url:
        return None
    timeout = API_TIMEOUT_RENDER if render else API_TIMEOUT_FAST
    for attempt in range(2):  # initial try + one retry on transient failure
        try:
            resp = requests.get(proxy_url, timeout=timeout)
            if resp.status_code == 200 and resp.text:
                return resp.text
            if resp.status_code in (429, 500, 502, 503):
                if attempt == 0:
                    time.sleep(3)
                    continue
                print(f"    API HTTP {resp.status_code} (transient) for {url[:60]}")
                return None
            # Surface auth/quota errors (401/403/etc.) so they're diagnosable.
            print(f"    API HTTP {resp.status_code}: {resp.text[:120].strip()}")
            return None
        except requests.RequestException as exc:
            if attempt == 0:
                time.sleep(3)
                continue
            print(f"    API request error for {url[:60]}: {str(exc)[:100]}")
    return None


def fetch_html(url: str):
    """Plain HTTP fetch with retry/backoff. Returns HTML text or None."""
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                return resp.text
            # 403/429/503 => likely bot-blocked; brief backoff then retry.
            if resp.status_code in (403, 429, 503):
                time.sleep(2 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


def fetch_html_rendered(url: str):
    """Headless-browser fallback for JS-rendered pages. Returns HTML or None.

    Playwright/Chromium are optional; if unavailable we simply skip this step.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(user_agent=HEADERS["User-Agent"])
            try:
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)  # let late price JS settle
                html = page.content()
            finally:
                browser.close()
            return html
    except Exception:
        return None


def scrape_direct(retailer: dict):
    """Fetch + extract a price by hitting the retailer page directly.

    Uses the scraping API when SCRAPER_API_KEY is set (fast-first, then render),
    otherwise a plain fetch + headless-browser fallback. Returns (price,
    currency, method) with price=None when nothing usable was found.
    """
    rid = retailer["id"]
    price = currency = method = None
    if SCRAPER_API_KEY:
        html = fetch_html_via_api(retailer["url"], render=False)
        if html:
            price, currency, method = extract_price(html, rid)
            if method:
                method = f"{method}+api"
        if price is None:
            html = fetch_html_via_api(retailer["url"], render=True)
            if html:
                price, currency, method = extract_price(html, rid)
                if method:
                    method = f"{method}+api-render"
    else:
        html = fetch_html(retailer["url"])
        if html:
            price, currency, method = extract_price(html, rid)
        if price is None:
            rendered = fetch_html_rendered(retailer["url"])
            if rendered:
                price, currency, method = extract_price(rendered, rid)
                if method:
                    method = f"{method}+rendered"
    return price, currency, method


def resolve_retailer(retailer: dict, serpapi_map: dict, price_range=None):
    """Produce a result dict for one retailer, dispatching by its `source`.

    serpapi_map: precomputed {retailer_id: {price, url, source}} from the single
    Google Shopping query, so serpapi-sourced retailers are just a dict lookup.
    """
    result = {
        "retailer_id": retailer["id"],
        "name": retailer["name"],
        "url": retailer["url"],
        "currency": retailer.get("currency", "USD"),
        "price": None,
        "status": "unavailable",
        "method": None,
        "timestamp": now_iso(),
    }
    source = retailer.get("source", "direct")
    try:
        price = currency = method = None
        url_override = None

        if source == "serpapi":
            hit = serpapi_map.get(retailer["id"])
            if hit:
                price, method = hit["price"], "serpapi"
                url_override = hit.get("url") or None
        elif source == "bestbuy":
            hit = get_bestbuy_price(retailer, price_range=price_range)
            if hit:
                price, method = hit["price"], "bestbuy-api"
                url_override = hit.get("url") or None
        else:  # direct
            price, currency, method = scrape_direct(retailer)

        # Sanity guard: drop implausible values (e.g. an accessory price).
        if price is not None and price_range:
            lo, hi = price_range
            if not (lo <= price <= hi):
                result["rejected_price"] = price
                result["reject_reason"] = f"outside expected range {lo}-{hi}"
                price = None

        if price is not None:
            result.update(
                price=price,
                currency=currency or result["currency"],
                status="ok",
                method=method,
            )
            if url_override:
                result["url"] = url_override
    except Exception as exc:  # defensive: one retailer must not break the run
        result["error"] = str(exc)[:200]
    return result


def main() -> int:
    config = load_json(CONFIG_PATH, None)
    if not config or "retailers" not in config:
        print(f"ERROR: could not read {CONFIG_PATH}", file=sys.stderr)
        return 1

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    previous_latest = load_json(LATEST_PATH, {})
    prev_prices = {r["retailer_id"]: r for r in previous_latest.get("retailers", [])}
    history = load_json(HISTORY_PATH, [])

    product = config.get("product", {})
    price_range = product.get("expected_price_range")
    retailers = config["retailers"]

    # Report which sources are active.
    active = []
    if SERPAPI_KEY:
        active.append("SerpApi (Google Shopping)")
    if BESTBUY_API_KEY:
        active.append("Best Buy API")
    active.append("scraping API" if SCRAPER_API_KEY else "direct fetch")
    print("Sources: " + ", ".join(active) + "\n")

    # One Google Shopping query covers every serpapi-sourced retailer at once.
    serpapi_retailers = [r for r in retailers if r.get("source") == "serpapi"]
    serpapi_map = get_serpapi_prices(
        product.get("serpapi_query") or product.get("name", ""),
        serpapi_retailers,
        match_terms=product.get("variant_match_terms"),
        price_range=price_range,
    )

    # Resolve the rest (direct + Best Buy) concurrently; serpapi ones are a
    # dict lookup so they cost nothing extra here.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        by_id = dict(
            zip(
                (r["id"] for r in retailers),
                pool.map(
                    lambda r: resolve_retailer(r, serpapi_map, price_range=price_range),
                    retailers,
                ),
            )
        )
    results = [by_id[r["id"]] for r in retailers]  # keep config order

    for res in results:
        # Preserve last known price when this run failed to fetch one.
        if res["status"] != "ok":
            prev = prev_prices.get(res["retailer_id"])
            if prev and prev.get("price") is not None:
                res["last_known_price"] = prev["price"]
                res["last_known_at"] = prev.get("timestamp")

        status_word = f"${res['price']}" if res["price"] is not None else res["status"]
        print(f"  {res['name']:<32} {status_word}  ({res.get('method') or '-'})")

        # Record every observation (ok only) in history for the charts.
        if res["status"] == "ok":
            history.append(
                {
                    "retailer_id": res["retailer_id"],
                    "price": res["price"],
                    "currency": res["currency"],
                    "timestamp": res["timestamp"],
                }
            )

    latest = {
        "product": config.get("product", {}),
        "updated_at": now_iso(),
        "retailers": results,
    }

    LATEST_PATH.write_text(json.dumps(latest, indent=2) + "\n", encoding="utf-8")
    HISTORY_PATH.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")

    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"\nDone: {ok}/{len(results)} retailers returned a price.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
