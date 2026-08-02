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
API_TIMEOUT = 75  # scraping APIs render JS server-side, so allow more time
MAX_RETRIES = 3

# Optional scraping-API passthrough (set as a GitHub Actions secret).
SCRAPER_API_KEY = os.environ.get("SCRAPER_API_KEY", "").strip()
SCRAPER_API_PROVIDER = os.environ.get("SCRAPER_API_PROVIDER", "scraperapi").strip().lower()


def build_proxy_url(url: str):
    """Wrap a target URL in the configured scraping-API request URL."""
    if not SCRAPER_API_KEY:
        return None
    target = quote(url, safe="")
    if SCRAPER_API_PROVIDER == "scraperapi":
        return (
            f"https://api.scraperapi.com/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render=true&country_code=us"
        )
    if SCRAPER_API_PROVIDER == "scrapingbee":
        return (
            f"https://app.scrapingbee.com/api/v1/?api_key={SCRAPER_API_KEY}"
            f"&url={target}&render_js=true&country_code=us"
        )
    if SCRAPER_API_PROVIDER == "zenrows":
        return (
            f"https://api.zenrows.com/v1/?apikey={SCRAPER_API_KEY}"
            f"&url={target}&js_render=true&premium_proxy=true&proxy_country=us"
        )
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


def fetch_html_via_api(url: str):
    """Fetch through the configured scraping API (rotating IP + JS render)."""
    proxy_url = build_proxy_url(url)
    if not proxy_url:
        return None
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(proxy_url, timeout=API_TIMEOUT)
            if resp.status_code == 200 and resp.text:
                return resp.text
            # API transient errors (rate limit / upstream retry) -> back off.
            if resp.status_code in (429, 500, 502, 503):
                time.sleep(3 * (attempt + 1))
                continue
            return None
        except requests.RequestException:
            time.sleep(3 * (attempt + 1))
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
            # Route through the scraping API: rotates IPs + renders JS, so this
            # single call replaces both the direct fetch and the browser render.
            html = fetch_html_via_api(retailer["url"])
            if html:
                price, currency, method = extract_price(html, rid)
                if method:
                    method = f"{method}+api"
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

    results = []
    for retailer in config["retailers"]:
        res = scrape_retailer(retailer, price_range=price_range)

        # Preserve last known price when this run failed to fetch one.
        if res["status"] != "ok":
            prev = prev_prices.get(res["retailer_id"])
            if prev and prev.get("price") is not None:
                res["last_known_price"] = prev["price"]
                res["last_known_at"] = prev.get("timestamp")

        status_word = f"${res['price']}" if res["price"] is not None else res["status"]
        print(f"  {res['name']:<32} {status_word}  ({res.get('method') or '-'})")
        results.append(res)

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
