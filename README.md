# Chrome Hearts new-product monitor (Railway, always-on)

Watches [chromehearts.com](https://www.chromehearts.com) continuously and posts
to Discord the moment a new product appears — name, price, and direct link.

```
🚨 New Chrome Hearts drop
CH LOGO SOCKS ($255.00)
https://www.chromehearts.com/socks/ch-logo-socks/176354XXXXXX349.html
```

For apparel links that encode size in the product id, alerts include the
inferred size without making any extra requests:

```
🚨 New Chrome Hearts drop
SHORT SLEEVE POCKET CREW ($320.00)
Size: Medium (MED)
https://www.chromehearts.com/shirt/short-sleeve-pocket-crew/129111BLKMED756.html
```

## How it works

Chrome Hearts runs a Salesforce Commerce Cloud (Demandware/SFRA) storefront
behind Cloudflare with a small, fast-rotating web catalog. Live category pages
render product tiles server-side; each tile exposes a `product-metadata` span
(product id, name, price) plus a canonical `/cat/subcat/PID.html` link. Sold-out
or empty categories transparently redirect to the homepage (no tiles), so they
just contribute nothing.

The worker runs forever with two polling tiers:

1. Hot categories (`hat,eyewear,sunglasses,hoodie,jacket,t-shirt,sweatpants,belt,bag,earring,scarf,pants,denim`) each poll every
   60 seconds by default. Their first polls are staggered across that minute.
2. All categories outside the configured hot list, the homepage and sitemap
   documents each poll every 600 seconds. Cold polls are spread across the interval.
   Newly discovered categories and child sitemaps join future cold slots; they are
   never fetched recursively in a burst. Missed polls are re-spread after cooldowns.
3. Each successful page poll parses products and diffs them against persistent
   PID history. Only unseen PIDs are new. Size inference and Discord formatting
   remain the same. Empty/sold-out pages leave history untouched.
4. Category redirects to the homepage stop at the redirect: the homepage has its
   own cold poll. Product redirects are still followed. Every HTTP request keeps
   the existing 0.2–0.6 second jittered minimum gap.

The hot defaults include sweatpants, belts, bags, earrings and scarves as well as
hats, eyewear, sunglasses, hoodies, jackets, T-shirts, pants and denim. `sweatpants` maps to
`Search-Show?cgid=SWEATPANTS`, rather than an unverified `/sweatpants` URL.
The live sitemap/menu did not expose a dedicated zip-up category, and `/zip-up`
and generic `/accessories` returned 404 during verification. Hoodies and jackets
remain hot; individual accessory routes are used instead of a generic route.
Pants and denim are both hot. Their routes currently redirect home when empty;
`/jeans` returned 404, so jeans coverage uses the known `/denim` route.

Defaults schedule 13 hot polls/minute plus about 2.3 cold polls/minute. Actual HTTP
traffic also includes product redirects, new categories and transient retries;
use request-accounting logs to measure the real rate.

Keying on PID means it catches genuinely new items even inside categories that
already had products. Seen PIDs remain in history when products sell out or a
category temporarily fails to load, so ordinary restocks and transient crawl
gaps do not generate false "new" alerts. The initial snapshot must be created
explicitly with `--seed`. A missing or unreadable state file raises a monitor
health warning instead of silently treating a new release as already seen. A
sweep finding zero products also refuses to update state. `--dry-run` does not
record newly found products as seen. Any failed or blocked poll refuses both
notification and history changes,
including products collected before a full diagnostic crawl failed. Successful
polls still merge products into history; missing products are never deleted.
Seeding is blocked until the entire diagnostic crawl succeeds.

Health warnings are sent after three consecutive failed sweeps by default, with
private reminders every six hours while failures continue. Set
`CH_HEALTH_REMINDER_SECONDS` to change the interval, or `0` to disable reminders.
Warnings explicitly say product alerts may be missing and show the last product
history update and its age. This timestamp reflects the latest successful page
poll; it is not proof that every category was covered.
A separate `seen_products.health.json` file next to the product
history stores the failure count, notification status, and retry deadline, so
restarts on the same persistent volume do not repeat an already delivered
warning before its reminder deadline. Existing incidents created before reminder
tracking receive a reminder on the next notice check. A complete cycle covering
every scheduled page and finding products clears the
failure count and sends one recovery message if a warning was delivered.
A successful access probe alone cannot announce recovery. Failed warning/recovery
deliveries are
retried with at least a minute between attempts. Startup pings are suppressed
while an incident is active, and are sent only after a successful first sweep.
Product history is never reset by health tracking.

Product-monitor warnings, recovery messages, and startup notices go exclusively
to `DISCORD_MONITOR_WEBHOOK_URL`. Product drops continue to use the existing
`DISCORD_WEBHOOK_URL` and `NOTIFY_METHOD` configuration. Set the monitor webhook
as a secret variable on the product worker in Railway, pointing to your private
issues channel. Operational notices disable Discord mentions and link previews.
If this webhook is missing or delivery fails, the worker logs the error and
retries incident/recovery notices; it never falls back to the group drop channel.
Existing incident deduplication and product history remain intact when changing
the destination. Appointment-slot notifications keep their current routing.

HTTP 403, HTTP 429, and detected challenge pages stop the current crawl
immediately. The worker persists blocked mode and sends exactly one HTTP request
to the first configured hot category per probe attempt. Probes do not retry or
follow redirects; an ordinary same-site redirect is evidence of access. The base
delays are 5, 10, 20, 40, then 60 minutes, with up to 10% jitter (downward at the
cap so the delay stays within 60 minutes). A successful probe resets access
backoff and resumes staggered tier polling. Network errors while probing keep
blocked mode active. Other failures keep the existing exponential retry policy,
now with a one-hour default cap.

Server `Retry-After` values (seconds or HTTP dates) take precedence when longer
than the backoff, even beyond one hour. Invalid/non-finite values are ignored.
Cooldowns and blocked mode survive restarts on the existing health volume.
Warnings, failed-notice retries and six-hour reminders run independently of probe
deadlines, so a long cooldown cannot silence an active incident. No challenge
bypass, client identity changes or network changes are introduced.

`--loop --dry-run` honors saved deadlines without writing either state file or
sending notifications. Explicit `--once` and `--seed` remain full diagnostic
crawls and do not load loop deadlines; do not use them to repeatedly retry a
blocked production service. `--loop --dry-run --duration 900` runs a bounded
15-minute verification against an existing local history file. A dry run with
unseen products can print them repeatedly because it never marks them seen.

Every hour of process uptime, `request accounting:` logs `window_seconds`,
`requests`, `blocked`, `requests_per_minute`, `total_requests`, and `total_blocked`.
These count HTTP sends, including sitemap requests, redirect hops, retries and
failed connection attempts. Blocked counts include 403/429 and detected challenges.
Idle backoff windows still log. Counters restart with the process; bounded runs
and graceful Python exits also emit a final partial-window report.

Logs include a per-crawl HTTP status summary. Failed HTTP requests include the
requested path, final URL, status, content type, response size, and challenge
detection. Empty crawls include sample empty-page redirects to distinguish
normal homepage redirects from unexpected responses. URL query tokens and page
bodies are not logged. For an outage, inspect these deployment logs before
changing hosting or resetting state. A provider incident may explain failures,
but does not by itself establish the cause of an individual crawl failure.

Notification HTTP errors fail the sweep without saving the new PIDs, so the next
sweep retries them. If a batch partly succeeded, the retry may repeat a message.

Small drops are sent as one message per product. Large batches are split into
numbered Discord messages containing every item, up to `CH_MAX_INDIVIDUAL` items
per message. Product names are bold and links are compact, which avoids a wall
of link-preview cards while keeping every product directly clickable.

## Deploy on Railway

1. Push this repo to GitHub.
2. Railway -> **New Project -> Deploy from GitHub repo** -> pick this repo.
   Railway auto-detects Python and uses the `Procfile`
   (`worker: python chrome_hearts_monitor.py --loop`). No port/domain needed —
   it's a background worker.
3. **Variables** (Settings -> Variables): add
   - `NOTIFY_METHOD = discord`
   - `DISCORD_WEBHOOK_URL = <your webhook>`
   - `DISCORD_MONITOR_WEBHOOK_URL = <your private monitor-issues webhook>`
   - `CH_STATE_FILE = /data/seen_products.json`
4. **Volume** (so state survives restarts — important): add a Volume to the
   service mounted at `/data`. Before starting the normal loop, initialize
   `/data/seen_products.json` with an explicit `--seed` run after checking the
   current catalog. A missing volume or state file now triggers a health warning
   instead of silently reseeding.
   Health tracking automatically uses `/data/seen_products.health.json` when
   the product state is `/data/seen_products.json`; no new variable is required.
5. Deploy. After a successful first sweep, the private issues channel receives a
   "monitor online" notice. If crawling fails, the worker follows the incident
   warning policy instead. Railway's green service status confirms that the
   process is running; check crawl logs to confirm successful product detection.

> The container stays on continuously. Hot categories have about a one-minute
> detection window; cold categories have about a ten-minute window, plus request
> latency. Blocking pauses detection until access recovers.

### Roll out this polling/backoff change to the existing worker

1. Review and merge the PR. In Railway open **vibrant-achievement → production →
   worker**. Deploy the reviewed `main` commit to that existing service (or confirm
   its GitHub autodeploy picked up that commit).
2. Keep exactly one product worker using the existing volume at `/data`,
   `CH_STATE_FILE=/data/seen_products.json`, and the existing health file and
   webhook secrets. Keep the start command `python chrome_hearts_monitor.py --loop`.
   Do not run `--seed`, delete history, or add another notifying service.
3. Set/verify `CH_HOT_CATEGORIES=hat,eyewear,sunglasses,hoodie,jacket,t-shirt,sweatpants,belt,bag,earring,scarf,pants,denim`,
   `CH_HOT_SECONDS=60`, `CH_COLD_SECONDS=600`, and `CH_BACKOFF_MAX_SECONDS=3600`.
   An existing explicit `CH_HOT_CATEGORIES` value overrides the expanded default
   list and needs updating. An old explicit `CH_BACKOFF_MAX_SECONDS=300` also
   overrides the new default and needs updating. `CH_POLL_SECONDS` no longer sets polling frequency. Redeploy
   after variable changes. These are operator steps; this PR does not apply them.
4. Confirm the startup log says `13 hot pages every 60s, 23 cold pages every 600s`.
   While the old outbound address remains blocked, expect the first 403 to end
   fetching and then only single-category probes at increasing intervals. The
   existing private outage incident/reminders should continue.
5. After this lower-rate code is deployed, open **worker → Settings → Networking
   → Enable Static IPs**, review the assigned addresses, then **Redeploy**.
   Railway requires Pro for this feature and applies the assignment on redeploy:
   [Static Outbound IPs documentation](https://docs.railway.com/networking/static-outbound-ips).
   Leave the volume and state files attached. This PR does not change networking.
6. A persisted cooldown still applies after redeploy; allow its remaining time
   (normally at most an hour, or longer for `Retry-After`). Look for
   `access probe succeeded; resuming tiered polling`, then successful `scheduled
   hot page` / `scheduled cold page` entries, including eyewear products and
   sitemap coverage. Normal redirects and absent-category 404s are expected.
7. Confirm `full sweep succeeded: ... scheduled pages covered; products detected`.
   This requires every known page, including newly queued child sitemaps and
   categories; allow several cold intervals during initial discovery. An individual
   successful page or Railway's green process status is not enough. Watch for the
   private recovery notice, no further block errors, and an hourly `request
   accounting:` report consistent with the measured rate. If the new address is
   refused, leave backoff in place and inspect the diagnostics.

### Live validation — November 13, 2026

Both runs used the local Mac against the live storefront with notifications
suppressed and an isolated temporary history fixture. No production state or
settings were accessed.

| Run | Measured window | HTTP requests | HTTP requests/minute | Blocked |
| --- | --- | --- | --- | --- |
| Original `fd8b71c`, original loop cadence | 60.0003 seconds, 2 sweeps | 134 | 134.00 | 0 |
| Initial tiered configuration, six hot categories | 900 seconds (12:37:15–12:52:15 Pacific) | 150 | 10.00 | 0 |
| Earlier expanded configuration, eleven hot categories | 900 seconds (12:57:34–13:12:34 Pacific) | 217 | 14.47 | 0 |

That initial six-category run made **135 scheduled page polls = 9.00 polls/minute**; the additional
15 requests followed eyewear redirects to MIO LORD. The before/after HTTP counts
include actual redirect traffic, which the earlier ~68 page-polls/minute estimate
excluded. MIO LORD (`21978827FE50010`) was detected on all 15 eyewear polls.
There were no fetch errors. The temporary history stayed byte-for-byte identical,
and dry-run created no health file. The initial child sitemap was queued for a
future cold slot, so this 15-minute run did not yet emit full-coverage success;
full coverage and recovery timing also have deterministic clock tests.

```text
request accounting: window_seconds=900.0 requests=150 blocked=0 requests_per_minute=10.00 total_requests=150 total_blocked=0
```

The expanded eleven-category run made **202 scheduled polls =
13.47 polls/minute**, with 0 fetch errors and
15 MIO LORD detections. Its temporary history was also byte-for-byte
unchanged, with no health file created. The additional hot categories account
for the higher rate; the 60-second hot interval is unchanged.

The current thirteen-category list adds `pants` and `denim` to that measured
eleven-category configuration. Its initial schedule is 15.3 page polls/minute;
with the same eyewear redirect, about 16.3 HTTP requests/minute is projected.
The recorded 15-minute rates above apply to the earlier configurations, not
this final list. Both added routes were checked live, and scheduling tests
verify that they occupy hot slots and repeat at the hot interval.

Validation: `.venv/bin/python -m unittest -q` — 74 tests passed; `git diff --check`
passed. Hourly production logs will establish the ongoing rate after discovery.

## Discord webhook

Discord -> Server Settings -> Integrations -> Webhooks -> New Webhook -> pick a
channel -> Copy Webhook URL -> use it as `DISCORD_WEBHOOK_URL`. The URL itself is
the secret; keep it in Railway Variables, never in the repo. Install the Discord
app and enable that channel's notifications to get pings on your phone.

## Appointment-slot monitor

`ww_slots_monitor.py` is a second always-on worker that watches the Waitwhile
calendar for the Chrome Hearts New York West Village store and sends one Discord
message whenever appointment slots become bookable. It uses Waitwhile's public,
unauthenticated availability endpoint, which returns the whole configured window
in one request; a slot is open only when `numAvailableSpots > 0`. Service names
come from the public location endpoint at startup. The monitor deliberately does
not use `first-available-dates`, because those are calendar-selectable dates, not
proof of real capacity. It snapshots currently open datetimes, so a slot that is
booked and later reopens alerts again, while multiple openings in one sweep are
grouped into one message.

Alert links start at Waitwhile's service-selection step rather than linking
directly to the calendar. A calendar URL without a selected service can display
times but rejects the user's choice later in the flow. Waitwhile supports
service and party-size query parameters, but the monitor intentionally leaves
those choices to the customer because openings can apply to multiple services
and party size is user-specific.

### Production deployment

The appointment monitor is deployed in the existing Railway project as a
separate service named `chrome-hearts-appointments`. It uses this repository's
`main` branch with the following **Custom Start Command**:

```text
python ww_slots_monitor.py --loop
```

This service has its own Railway Volume mounted at `/data` and sends appointment
notifications to the Discord `#nyc-instore` channel. The webhook URL is stored
only in Railway Variables and must never be committed to the repository.

The production variables are:

| Variable | Value / purpose |
|----------|-----------------|
| `DISCORD_WEBHOOK_URL` | Secret webhook for `#nyc-instore` |
| `NOTIFY_METHOD` | `discord` |
| `WW_LOCATION` | `chromehearts` |
| `WW_STATE_FILE` | `/data/ww_slots.json` |
| `WW_POLL_SECONDS` | `20` |
| `WW_DAYS_AHEAD` | `21` |
| `WW_STARTUP_PING` | `1` |

The deployment was verified end to end: Railway mounted the dedicated volume,
resolved “Chrome Hearts - New York West Village” and its five live service
types, seeded 324 slots, completed subsequent polls, and successfully delivered
both the startup notification and a labeled test message to Discord.

To recreate the service, add the same GitHub repository as a second Railway
service, apply the custom start command and variables above, attach a dedicated
volume at `/data`, and deploy. The first sweep seeds silently; subsequent newly
open slots trigger alerts. The repo-level `Procfile` remains reserved for the
product-monitor service.

## Local testing

```bash
pip install -r requirements.txt
cp .env.example .env            # fill in DISCORD_WEBHOOK_URL
set -a && source .env && set +a

python chrome_hearts_monitor.py --seed             # record catalog, no alerts
python chrome_hearts_monitor.py --once --dry-run   # detect + print, send nothing
python chrome_hearts_monitor.py --once             # one real sweep
python chrome_hearts_monitor.py --loop             # what Railway runs

python ww_slots_monitor.py --seed                  # record open slots, no alerts
python ww_slots_monitor.py --once --dry-run        # detect + print, send nothing
python ww_slots_monitor.py --once                  # one real appointment sweep
python ww_slots_monitor.py --loop                  # appointment Railway service
```

## Tuning (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `CH_HOT_CATEGORIES` | `hat,eyewear,sunglasses,hoodie,jacket,t-shirt,sweatpants,belt,bag,earring,scarf,pants,denim` | comma-separated hot category slugs; `sweatpants` resolves to its Search-Show route; at least one required |
| `CH_HOT_SECONDS` | `60` | interval per hot category |
| `CH_COLD_SECONDS` | `600` | interval per cold category, homepage and sitemap document |
| `CH_POLL_SECONDS` | `30` | legacy setting used only as the base for non-block failure retries; no longer controls polling |
| `CH_MAX_INDIVIDUAL` | `8` | maximum items per message in a paginated large batch |
| `CH_STARTUP_PING` | `1` | private notice after a successful first sweep |
| `DISCORD_MONITOR_WEBHOOK_URL` | unset | private destination for product-monitor warnings, recovery messages, and startup notices; no fallback to the drop webhook |
| `CH_STATE_FILE` | `seen_products.json` | snapshot path (set to `/data/...` on Railway) |
| `CH_HEALTH_FILE` | product state path with `.health.json` suffix | persistent incident tracker; keep on the same volume |
| `CH_HEALTH_FAILURE_THRESHOLD` | `3` | consecutive failed sweeps before an incident warning |
| `CH_HEALTH_REMINDER_SECONDS` | `21600` | private reminder interval during an ongoing incident; `0` disables reminders |
| `CH_BACKOFF_MAX_SECONDS` | `3600` | maximum exponential delay (bounded to 300–3600); a longer server `Retry-After` takes precedence |

Appointment monitor:

| Var | Default | Meaning |
|-----|---------|---------|
| `WW_LOCATION` | `chromehearts` | Waitwhile location shortname |
| `WW_POLL_SECONDS` | `20` | target seconds between availability sweeps |
| `WW_DAYS_AHEAD` | `21` | number of calendar days to request |
| `WW_SERVICE_FILTER` | empty | optional service-ID or name-substring filter |
| `WW_STARTUP_PING` | `1` | send a "slot monitor online" Discord ping on boot |
| `WW_STATE_FILE` | `ww_slots.json` | open-slot snapshot path (use `/data/ww_slots.json` on Railway) |

### Miami appointments

Miami uses the same Waitwhile API and `ww_slots_monitor.py` worker as New York,
configured with `WW_LOCATION=chromeheartsmiami`. Its public location record is
“Chrome Hearts - Miami”; the API currently exposes the same five service types
as New York and falls back to `America/New_York` because Waitwhile returns no
timezone for this location. The booking link starts at service selection:
`https://waitwhile.com/locations/chromeheartsmiami/services?registration=booking`.

The Railway service uses `python ww_slots_monitor.py --loop`, its own Discord
webhook, `WW_STATE_FILE=/data/ww_slots_miami.json`, and a dedicated volume at
`/data`. Set `WW_BOOK_URL` to the service-selection URL above.

### Los Angeles appointments

Los Angeles books through Appointedd rather than Waitwhile, so
`appointedd_slots_monitor.py` watches Appointedd's GraphQL availability-interval
API. It discovers the widget's static public access token at startup and retries
token discovery after a 401. `AvailableInterval` entries are real openings;
group-booking intervals are ignored. Times are converted from UTC to
`America/Los_Angeles` before alerts are formatted.

By default the service watches LA In-store only
(`5f649cb02f6894080a6a49e4`). Malibu and the repair services remain opt-in via
`APPT_SERVICES`. The Railway service uses
`python appointedd_slots_monitor.py --loop`, its own Discord webhook,
`APPT_STATE_FILE=/data/appt_slots.json`, and a dedicated `/data` volume.

To recreate either service, add this same GitHub repository as another Railway
service, set its custom start command and variables, mount a separate volume at
`/data`, and deploy. Each service seeds its initial state silently and sends its
own online ping.

## Categories

`CATEGORIES` in `chrome_hearts_monitor.py` is the full known slug list — the few
that are usually live plus ~28 valid-but-usually-empty ones that populate when a
drop lands. Fetched homepage/category HTML also discovers same-site top-level category links.
New slugs join the cold schedule, so they can be checked before being added to
the static list.
`CATEGORY_IDS` covers menu categories that use Salesforce's `Search-Show?cgid=`
route instead of a top-level slug. The `SWEATPANTS` category uses this route and
currently redirects to the `BLACK SWEATPANTS` product page.
The official sitemap is a second source for top-level categories; it currently
omits `SWEATPANTS`, so the menu link and explicit category ID remain needed.

**Known gap:** fine jewelry (rings/necklaces/bracelets) has no working top-level
slug observed live; `/ring`, `/jewelry`, etc. 404. The cold homepage poll plus
dynamic link discovery is the safety net for featured/newly linked drops
meanwhile — once a real jewelry drop is caught, read its category from the
product URL and add that slug here.

## Other notification backends

`send_notification()` in `notifier.py` also supports Twilio SMS and carrier
email-to-SMS; set `NOTIFY_METHOD` accordingly with the matching env vars.

This scrapes a public site for personal use — keep the cadence reasonable.
