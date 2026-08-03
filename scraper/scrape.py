#!/usr/bin/env python3
"""Automated price scraper for the Kärcher K5 Power Control.

Reads config/retailers.json, fetches each retailer's product page, extracts the
price, and writes:
  - data/latest.json   : current snapshot the website reads
  - data/history.json  : append-only price history for the charts

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
API_TIMEOUT_FAST = 35
API_TIMEOUT_RENDER = 70
MAX_RETRIES = 3
MAX_WORKERS = 4  # scrape retailers concurrently to keep wall-clock bounded

# Optional scraping-API passthrough (set as a GitHub Actions secret).
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()
# Use `or` (not a get-default) so an env var present-but-empty — which is what
# GitHub injects for an unset repo variable — still falls back to the default.
SCRAPER_API_PROVIDER = (os.environ.get("SCRAPER_API_PROVIDER") or "scraperapi").strip().lower()


def build_proxy_url(url: str, render: bool = False):
    """Wrap a target URL in the configured scraping-API request URL.

    render=False fetches the raw HTML (fast/cheap — enough when the price is in
    JSON-LD/meta); render=True runs the page's JS server-side (slow/costly).
    """
    if not SCRAPER_API_KEY:
        return None
    target = quote(url, safe="")
    flag = "true" if render else "false"
    if SCRAPER_API_PROVIDER == "scraperapi":
        return (
            f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render={flag}&country_code=us"
        )
    if SCRAPER_API_PROVIDER == "scrapingbee":
        return (
            f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render_js={flag}&country_code=us"
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


def scrape_retailer(retailer: dict, price_range=None):
    """Return a result dict for one retailer (always returns; never raises).

    price_range: optional (min, max) sanity bounds; a scraped price outside the
    range is rejected as likely-wrong rather than shown.
    """
    rid = retailer["id"]
    result = {
        "retailer_id": rid,
        "name": retailer["name"],
        "url": retailer["url"],
        "currency": retailer.get("currency", "USD"),
        "price": None,
        "status": "unavailable",
        "method": None,
        "timestamp": now_iso(),
    }
    try:
        price = currency = method = None

        if SCRAPER_API_KEY:
            # Fast path first (raw HTML, cheap): enough when the price is in
            # JSON-LD/meta, which is most retailers.
            html = fetch_html_via_api(retailer["url"], render=False)
            if html:
                price, currency, method = extract_price(html, rid)
                if method:
                    method = f"{method}+api"
            # Escalate to JS-render only if the fast path found no price.
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
            # Only spin up the browser if the cheap path found nothing.
            if price is None:
                rendered = fetch_html_rendered(retailer["url"])
                if rendered:
                    price, currency, method = extract_price(rendered, rid)
                    if method:
                        method = f"{method}+rendered"

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

    price_range = config.get("product", {}).get("expected_price_range")
    mode = f"scraping API ({SCRAPER_API_PROVIDER})" if SCRAPER_API_KEY else "direct fetch"
    print(f"Fetch mode: {mode}\n")

    # Scrape retailers concurrently so one slow site doesn't gate the rest.
    retailers = config["retailers"]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        by_id = dict(
            zip(
                (r["id"] for r in retailers),
                pool.map(lambda r: scrape_retailer(r, price_range=price_range), retailers),
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
