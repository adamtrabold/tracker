# Kärcher K5 Power Control CHK — Price Tracker

A hands-off website that tracks the price of the **Kärcher K5 Power Control CHK** (Car & Home Kit)
pressure washer across US retailers. Prices are gathered automatically on a
schedule — there's nothing to update by hand.

## How it works

Each retailer is priced via the source best suited to it — because the big-box
stores block direct scraping, but their prices are available elsewhere:

```
GitHub Actions (cron, every 6h) → runs scraper/scrape.py
        │
        ├─ SerpApi Google Shopping  → Amazon, Walmart, Target, Home Depot, Lowe's
        │     (one query returns all of them from Google's aggregated data)
        ├─ Best Buy Products API     → Best Buy (official, free)
        └─ Direct fetch (JSON-LD)    → NFM, Kärcher, QVC (sites Google skips)
        │
        ▼
write data/latest.json + append data/history.json → commit back to the repo
        ▼
GitHub Pages serves the site → live table + price-history chart
```

Everything runs on free GitHub infrastructure plus free API tiers. No server,
no database, no manual price entry.

## Project layout

| Path | What it is |
|------|-----------|
| `config/retailers.json` | The one file you edit — the product + each retailer and its `source`. |
| `scraper/scrape.py` | Dispatches each retailer to its source, writes the data files. |
| `scraper/sources.py` | SerpApi (Google Shopping) + Best Buy API price fetchers. |
| `scraper/extractors.py` | Price extraction for direct fetches (JSON-LD, meta, HTML). |
| `data/latest.json` | Current price per retailer (the site reads this). |
| `data/history.json` | Append-only price history (powers the chart). |
| `index.html`, `app.js`, `styles.css` | Static website, served from the repo root. |
| `.github/workflows/scrape.yml` | The scheduled run + auto-commit. |

## One-time setup

1. **Enable GitHub Pages**: repo **Settings → Pages → Build and deployment →
   Deploy from a branch**, pick the **`main`** branch and the root folder (`/`).
   The site is then served at `https://<you>.github.io/tracker/`.
2. **Enable Actions write access** (if not already): **Settings → Actions →
   General → Workflow permissions → Read and write permissions**. This lets the
   scraper commit updated prices.
3. **Add the two free API keys** as repo secrets (**Settings → Secrets and
   variables → Actions → New repository secret**). Both have free tiers that
   comfortably cover a 6-hourly (or daily) schedule:
   - **`SERPAPI_KEY`** — from [SerpApi](https://serpapi.com/) (free 250
     searches/month). One search per run covers Amazon, Walmart, Target, Home
     Depot, and Lowe's. This is the key that unlocks the big-box retailers.
   - **`BESTBUY_API_KEY`** — from the [Best Buy Developer portal](https://developer.bestbuy.com/)
     (free). Official near-real-time pricing for Best Buy.

   Without a key, that source's retailers show **unavailable**; the others
   still work. (Optional: `SCRAPER_API_KEY` also routes the "direct" retailers
   through a scraping API — see below.)
4. That's it. The run happens every 6 hours, or trigger it manually from the
   **Actions** tab → *Scrape prices* → *Run workflow*.

## Adjusting what's tracked

Edit `config/retailers.json`. Each retailer has a `source` that picks how its
price is fetched:

```json
{ "id": "homedepot", "name": "The Home Depot", "source": "serpapi",
  "match": ["home depot"], "currency": "USD", "url": "https://..." }
{ "id": "bestbuy", "name": "Best Buy", "source": "bestbuy",
  "manufacturer_part_number": "1.324-571.0", "currency": "USD", "url": "https://..." }
{ "id": "nfm", "name": "Nebraska Furniture Mart", "source": "direct",
  "currency": "USD", "url": "https://..." }
```

- `source: "serpapi"` — matched from the Google Shopping results by the `match`
  keywords against the result's retailer name. Good for big-box stores.
- `source: "bestbuy"` — Best Buy's API, by `manufacturer_part_number` (or a
  `bestbuy_sku`).
- `source: "direct"` — fetch the retailer `url` directly. Good for niche stores
  Google doesn't index.
- The product's `serpapi_query` and `variant_match_terms` (in `product`) tune
  the search and keep results to the right variant (the **CHK**, not the base).

## Running locally

```bash
pip install -r scraper/requirements.txt
python -m playwright install chromium   # optional; only for the direct fallback

export SERPAPI_KEY=your_key_here        # from serpapi.com
export BESTBUY_API_KEY=your_key_here    # from developer.bestbuy.com
python scraper/scrape.py                # writes data/latest.json + data/history.json

# preview the site
python -m http.server 8000                     # then open http://localhost:8000
```

## Honest limitations

- **Big-box retailers** (Amazon, Walmart, Target, Home Depot, Lowe's) actively
  block direct scraping, so their prices come from **Google Shopping via
  SerpApi** — i.e. Google's aggregated snapshot, which can lag the retailer
  slightly and occasionally omit a store. Best Buy uses its **official API**.
  Only niche stores (NFM, Kärcher, QVC) are fetched directly.
- If a source has no key or returns nothing for a retailer, that retailer shows
  **unavailable** and keeps its last known price — the rest keep working.
- A `product.expected_price_range` guard rejects implausible values (e.g. an
  accessory price), and `variant_match_terms` keeps results to the intended
  variant, so a wrong number never shows. Adjust both if you track another SKU.
- Free API tiers (SerpApi 250 searches/mo, Best Buy free) comfortably cover a
  6-hourly schedule; the cadence lives in `.github/workflows/scrape.yml`.
- Always confirm the price on the retailer's own site before buying.
