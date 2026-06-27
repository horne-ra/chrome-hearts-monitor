#!/usr/bin/env python3
"""
Chrome Hearts new-product monitor (always-on / Railway edition).

Runs as a long-lived worker: every ~30s it sweeps all Chrome Hearts category
pages, builds the live product set, diffs against the last sweep, and posts any
new product (name + price + link) to Discord. State persists to disk so it
survives restarts -- on Railway, point CH_STATE_FILE at a mounted volume.

Chrome Hearts is a Salesforce Commerce Cloud (Demandware/SFRA) storefront behind
Cloudflare. Live category pages render product tiles server-side; each tile has a
`product-metadata` span (data-pid / data-name / data-price / data-category) plus
a canonical `/cat/subcat/PID.html` link. Empty/sold-out categories 200-redirect
to the homepage (no tiles) and simply contribute nothing.

Run modes:
    python chrome_hearts_monitor.py --loop     # always-on (Railway start cmd)
    python chrome_hearts_monitor.py --once      # one sweep, then exit (testing)
    python chrome_hearts_monitor.py --seed      # record catalog, notify nothing
    python chrome_hearts_monitor.py --once --dry-run   # detect + print, no send

Key env vars:
    NOTIFY_METHOD=discord            (see notifier.py)
    DISCORD_WEBHOOK_URL=...
    CH_STATE_FILE=/data/seen_products.json   # persistent volume path on Railway
    CH_POLL_SECONDS=30              # target seconds between sweep starts
    CH_MAX_INDIVIDUAL=8             # >this many new at once -> one summary msg
    CH_STARTUP_PING=1               # send a "monitor online" Discord msg on boot
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE = "https://www.chromehearts.com"
STATE_FILE = Path(os.environ.get("CH_STATE_FILE", "seen_products.json"))
POLL_SECONDS = int(os.environ.get("CH_POLL_SECONDS", "30"))
MAX_INDIVIDUAL = int(os.environ.get("CH_MAX_INDIVIDUAL", "8"))
STARTUP_PING = os.environ.get("CH_STARTUP_PING", "1") == "1"

# Broad net: every known category slug. Live ones render grids; the rest are
# valid-but-usually-empty and populate when a drop lands -- which is the point.
# De-duplicated by PID, so alias slugs (bag/bags, hoodie/hoodies) are harmless.
CATEGORIES = [
    # usually live
    "socks", "scents", "baccarat", "intimates", "boxers-leggings",
    # valid, drops land here
    "hat", "eyewear", "sunglasses", "eyewear-accessories", "bag", "bags",
    "belt", "earring", "t-shirt", "t-shirts", "shirt", "shirts", "hoodie",
    "hoodies", "sweatshirt", "sweatshirts", "sweater", "jacket", "denim",
    "pants", "shorts", "shoes", "boots", "slippers", "home", "gloves",
    "scarf", "tie",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Small jittered gap between page fetches within a sweep so 34 pages read like a
# steady trickle (~1 req/s) rather than a synchronized burst that trips Cloudflare.
INTRA_DELAY = (0.2, 0.6)
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3

METADATA_RE = re.compile(
    r'<span[^>]*class="[^"]*product-metadata[^"]*"[^>]*></span>',
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"', re.IGNORECASE)
LINK_RE_TMPL = r'href="(/[a-z0-9\-]+/[a-z0-9\-]+/{pid}\.html)"'


@dataclass
class Product:
    pid: str
    name: str
    price: str
    category: str
    url: str

    def pretty(self) -> str:
        price = f"${self.price}" if self.price else "price n/a"
        return f"{self.name} ({price})\n{self.url}"


# --------------------------------------------------------------------------- #
# Crawl + parse
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
            if r.status_code in (200, 404):
                return r
        except requests.RequestException as exc:
            log(f"  ! {url} attempt {attempt}: {exc}")
        time.sleep(attempt * 1.5)
    return None


def parse_products(html: str) -> dict[str, Product]:
    found: dict[str, Product] = {}
    for span in METADATA_RE.findall(html):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(span)}
        pid = attrs.get("pid", "").strip()
        if not pid:
            continue
        m = re.search(LINK_RE_TMPL.format(pid=re.escape(pid)), html)
        path = m.group(1) if m else f"/p/{pid}.html"
        found[pid] = Product(
            pid=pid,
            name=attrs.get("name", "").strip() or "(unnamed)",
            price=attrs.get("price", "").strip(),
            category=attrs.get("category", "").strip(),
            url=BASE + path,
        )
    return found


def crawl(session: requests.Session) -> dict[str, Product]:
    catalog: dict[str, Product] = {}
    for path in ["/"] + [f"/{c}" for c in CATEGORIES]:
        r = fetch(session, BASE + path)
        time.sleep(random.uniform(*INTRA_DELAY))
        if r is None or r.status_code != 200:
            continue
        catalog.update(parse_products(r.text))  # empty pages add nothing
    return catalog


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(catalog: dict[str, Product]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
    tmp.write_text(json.dumps({pid: asdict(p) for pid, p in catalog.items()},
                              indent=2, ensure_ascii=False))
    tmp.replace(STATE_FILE)  # atomic, so a crash mid-write can't corrupt state


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #

def _send(body: str) -> None:
    from notifier import send_notification
    send_notification(body)


def notify_new(products: list[Product]) -> None:
    """Per-item messages, but collapse to one summary if a big batch drops."""
    if len(products) <= MAX_INDIVIDUAL:
        for p in products:
            _send(f"\U0001f6a8 New Chrome Hearts drop\n{p.pretty()}")
            time.sleep(1)
        return
    lines = [f"\U0001f6a8 {len(products)} new Chrome Hearts items:"]
    for p in products[:MAX_INDIVIDUAL]:
        price = f"${p.price}" if p.price else ""
        lines.append(f"\u2022 {p.name} {price} {p.url}")
    lines.append(f"...and {len(products) - MAX_INDIVIDUAL} more.")
    _send("\n".join(lines))


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #

def sweep(session: requests.Session, *, seed: bool, dry_run: bool) -> None:
    catalog = crawl(session)
    previous = load_state()

    if seed or not previous:
        save_state(catalog)
        log(f"seeded {len(catalog)} products (no notifications).")
        return

    new = [catalog[pid] for pid in catalog if pid not in previous]
    if not new:
        log(f"{len(catalog)} live, 0 new.")
        save_state(catalog)
        return

    log(f"{len(catalog)} live, {len(new)} NEW:")
    for p in new:
        log("   + " + p.pretty().replace("\n", " | "))
    if dry_run:
        log("[dry-run] not sending.")
    else:
        notify_new(new)
        log("notified.")
    save_state(catalog)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description="Chrome Hearts new-product monitor")
    ap.add_argument("--loop", action="store_true", help="run forever (Railway)")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--seed", action="store_true", help="record catalog, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="detect but never send")
    args = ap.parse_args()

    session = requests.Session()
    session.headers.update(HEADERS)

    if args.seed:
        sweep(session, seed=True, dry_run=True)
        return 0
    if args.once:
        sweep(session, seed=False, dry_run=args.dry_run)
        return 0
    if not args.loop:
        ap.error("choose a mode: --loop, --once, or --seed")

    log(f"Chrome Hearts monitor online. {len(CATEGORIES)} categories + homepage, "
        f"~{POLL_SECONDS}s sweeps. state={STATE_FILE}")
    if STARTUP_PING and not args.dry_run:
        try:
            _send(f"\U0001f7e2 Chrome Hearts monitor online — watching "
                  f"{len(CATEGORIES)} categories, ~{POLL_SECONDS}s sweeps.")
        except Exception as exc:  # don't die if the first ping fails
            log(f"startup ping failed: {exc}")

    while True:
        t0 = time.monotonic()
        try:
            sweep(session, seed=False, dry_run=args.dry_run)
        except Exception as exc:  # never let one bad sweep kill the worker
            log(f"sweep error (continuing): {exc!r}")
        elapsed = time.monotonic() - t0
        time.sleep(max(2.0, POLL_SECONDS - elapsed) + random.uniform(0, 4))


if __name__ == "__main__":
    raise SystemExit(main())
