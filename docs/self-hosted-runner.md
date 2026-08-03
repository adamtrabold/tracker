# Running from your home IP (the free way past big-box blocks)

Retailers like Home Depot, Lowe's, Walmart, Target, and Best Buy block
automated requests from **datacenter IPs** — which is what GitHub's hosted
runners use. A **self-hosted runner on your home internet** uses a
**residential IP**, which those sites don't pre-flag, so direct scraping can
work from it for free.

This is the only genuinely free "get around the blocking yourself" option —
you can't build a residential proxy without renting residential IPs, and a
home connection *is* a residential IP.

## What you need

- A machine at home that can stay on (an old laptop, a Mini PC, a Raspberry Pi
  4/5, or a NAS that runs Docker). It only needs to be awake when the scraper
  runs (every 6 hours by default).
- ~10 minutes to register the runner.

## Setup

1. **Register the runner**: repo → **Settings → Actions → Runners → New
   self-hosted runner**. Pick your OS and follow the shown commands — they
   download the runner and connect it to this repo. Example (Linux):
   ```bash
   mkdir actions-runner && cd actions-runner
   curl -o actions-runner.tar.gz -L <URL_GITHUB_SHOWS_YOU>
   tar xzf actions-runner.tar.gz
   ./config.sh --url https://github.com/adamtrabold/tracker --token <TOKEN_GITHUB_SHOWS_YOU>
   ```

2. **Run it as a service** so it stays available and restarts on reboot:
   ```bash
   sudo ./svc.sh install
   sudo ./svc.sh start
   ```
   (On Windows/macOS the runner app offers an equivalent "run as service"
   option.)

3. **Install prerequisites** on that machine once: Python 3.12+ and the ability
   to `pip install` (the workflow installs the rest, including Chromium).

4. **Enable the workflow**: the `Scrape prices (self-hosted)` workflow
   (`.github/workflows/scrape-selfhosted.yml`) targets `runs-on: [self-hosted]`,
   so it will start using your runner automatically. Trigger it once from the
   **Actions** tab → *Scrape prices (self-hosted)* → *Run workflow* to test.

5. **Optional — go key-free**: on a residential IP the `direct` source can often
   reach the big-box sites itself. Try switching those retailers to
   `"source": "direct"` in `config/retailers.json` and see how many resolve; if
   they do, you won't need SerpApi at all. The scraper already uses a
   real-browser TLS fingerprint (`curl_cffi`) on direct fetches, which further
   improves success from a residential IP.

## Honest caveats

- A residential IP defeats the **IP-reputation** layer, and `curl_cffi` handles
  the **TLS-fingerprint** layer — but the hardest sites (Walmart/Target run
  PerimeterX/Akamai) also throw **JS challenges** and watch request *behavior*.
  Those may still resist a plain fetch; the headless-browser fallback helps, but
  it's not guaranteed. Expect "most" rather than "all," and keep SerpApi as a
  fallback for any holdouts.
- Scrape politely: the default 6-hour cadence over a handful of product pages is
  gentle. Don't crank the frequency up — hammering a retailer from your home IP
  is how that IP eventually gets rate-limited.
- Your home machine must be on when the schedule fires. If it's asleep, that run
  is skipped (the next one just picks up).
- You can run **both** workflows: hosted (SerpApi/Best Buy/API) and self-hosted
  (direct). Whichever commits a price for a retailer wins that slot.
