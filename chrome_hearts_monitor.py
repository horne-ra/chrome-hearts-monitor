#!/usr/bin/env python3
"""
Chrome Hearts new-product monitor (always-on / Railway edition).

Runs as a long-lived worker: hot categories poll every minute, while cold
categories, the homepage and sitemaps are spread across ten minutes. Each
successful poll diffs products against persistent PID history and posts new
products (name + price + link) to Discord. On Railway, keep CH_STATE_FILE on
the existing mounted volume. Blocked access pauses polling for single-page probes.

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
    CH_HOT_SECONDS=60               # interval per hot category
    CH_COLD_SECONDS=600             # interval per cold page
    CH_HOT_CATEGORIES=hat,eyewear,sunglasses,hoodie,jacket,t-shirt,sweatpants,belt,bag,earring,scarf,pants,denim
    CH_MAX_INDIVIDUAL=8             # items per message in a large batch
    CH_STARTUP_PING=1               # private notice after a successful first sweep
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, asdict, replace
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urljoin

import requests
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

BASE = "https://www.chromehearts.com"
STATE_FILE = Path(os.environ.get("CH_STATE_FILE", "seen_products.json"))
POLL_SECONDS = int(os.environ.get("CH_POLL_SECONDS", "30"))
HOT_SECONDS = max(1, int(os.environ.get("CH_HOT_SECONDS", "60")))
COLD_SECONDS = max(1, int(os.environ.get("CH_COLD_SECONDS", "600")))
HOT_CATEGORIES = list(dict.fromkeys(
    category.strip().strip("/") for category in os.environ.get(
        "CH_HOT_CATEGORIES", "hat,eyewear,sunglasses,hoodie,jacket,t-shirt,"
        "sweatpants,belt,bag,earring,scarf,pants,denim").split(",")
    if category.strip().strip("/")))
if not HOT_CATEGORIES or any(not re.fullmatch(r"[a-z0-9-]+", c) for c in HOT_CATEGORIES):
    raise ValueError("CH_HOT_CATEGORIES must contain comma-separated category slugs")
MAX_INDIVIDUAL = int(os.environ.get("CH_MAX_INDIVIDUAL", "8"))
STARTUP_PING = os.environ.get("CH_STARTUP_PING", "1") == "1"
SUMMARY_MESSAGE_LIMIT = 1850  # leave headroom below Discord's 2,000-char limit
SITEMAP_INDEX = BASE + "/sitemap_index.xml"
SITEMAP_REFRESH_SECONDS = 600
HEALTH_FILE = Path(os.environ.get("CH_HEALTH_FILE", str(STATE_FILE.with_suffix(".health.json"))))
HEALTH_FAILURE_THRESHOLD = max(1, int(os.environ.get("CH_HEALTH_FAILURE_THRESHOLD", "3")))
BACKOFF_MAX_SECONDS = min(3600, max(300, int(os.environ.get("CH_BACKOFF_MAX_SECONDS", "3600"))))
NOTIFICATION_RETRY_SECONDS = 60
HEALTH_REMINDER_SECONDS = max(0, int(os.environ.get("CH_HEALTH_REMINDER_SECONDS", "21600")))

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

# Some storefront categories are only exposed through Search-Show menu links.
# SWEATPANTS currently redirects to its sole product, rather than a product grid.
CATEGORY_IDS = ["SWEATPANTS"]
CATEGORY_ENDPOINT = "/on/demandware.store/Sites-ChromeHearts-Site/en_US/Search-Show"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Minimum jittered gap between HTTP requests, including product redirects.
# Tier scheduling supplies the longer gaps between ordinary page polls.
INTRA_DELAY = (0.2, 0.6)
REQUEST_TIMEOUT = 25
MAX_RETRIES = 3

METADATA_RE = re.compile(
    r'<span[^>]*class="[^"]*product-metadata[^"]*"[^>]*></span>',
    re.IGNORECASE | re.DOTALL,
)
ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"', re.IGNORECASE)
HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)
LINK_RE_TMPL = r'href="(/[a-z0-9\-]+(?:/[a-z0-9\-]+)*/{pid}\.html(?:\?[^\"]*)?)"'

UTILITY_SLUGS = {
    "account", "cart", "checkout", "contact", "contact-us", "customer-service",
    "locations", "login", "magazine", "order-status", "privacy-policy",
    "search", "shop", "stores", "wishlist",
}

_sitemap_paths: list[str] = []
_sitemap_checked_at = 0.0

SIZE_CODES = {
    "XSM": "Extra Small",
    "SML": "Small",
    "MED": "Medium",
    "LRG": "Large",
    "1XL": "XL",
    "2XL": "2XL",
    "3XL": "3XL",
    "4XL": "4XL",
    "5XL": "5XL",
    # Chrome Hearts sometimes uses XXX as a size-like SKU segment; keep it raw
    # instead of guessing wrong in a time-sensitive alert.
    "XXX": "XXX",
    "OS": "One Size",
}

SIZED_CATEGORY_SLUGS = {
    "boxers-leggings", "denim", "hoodie", "hoodies", "intimates", "jacket",
    "pants", "shirt", "shirts", "shorts", "sweater", "sweatshirt",
    "sweatshirts", "t-shirt", "t-shirts",
}


@dataclass
class Product:
    pid: str
    name: str
    price: str
    category: str
    url: str
    size: str = ""

    def pretty(self) -> str:
        price = f"${self.price}" if self.price else "price n/a"
        size = f"\nSize: {self.size}" if self.size else ""
        return f"{self.name} ({price}){size}\n{self.url}"


# --------------------------------------------------------------------------- #
# Crawl + parse
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}", flush=True)


class MonitorFailure(RuntimeError):
    """A failed sweep with an optional server-requested cooldown."""

    def __init__(self, message: str, *, retry_after: float = 0):
        """Keep the cooldown available to the loop's incident tracker."""
        super().__init__(message)
        self.retry_after = retry_after


class FetchPaused(MonitorFailure):
    """Stop a sweep when the server asks us to back off or blocks access."""


@dataclass
class CrawlResult:
    """Products and coverage diagnostics, including any server cooldown."""
    catalog: dict[str, Product]
    errors: int
    summary: str = ""
    retry_after: float = 0
    blocked: bool = False
    partial: bool = False


def diagnostic_url(url: str) -> str:
    """Log public paths without query tokens or other session information."""
    parsed = urlparse(url)
    result = f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
    cgid = parse_qs(parsed.query).get("cgid", [])
    if len(cgid) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", cgid[0]):
        result += f"?cgid={cgid[0]}"
    return result


def retry_after_seconds(value: str) -> float:
    """Parse Retry-After seconds or HTTP dates without shortening server delays."""
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if math.isfinite(seconds) else 0


def is_challenge(response: requests.Response) -> bool:
    """Recognize access challenges without mistaking ordinary reCAPTCHA assets."""
    if response.headers.get("cf-mitigated", "").lower() == "challenge":
        return True
    html = response.text.lower()
    if "product-metadata" in html:
        return False
    return any(marker in html for marker in (
        "<title>just a moment", "<title>access denied",
        "<title>attention required! | cloudflare",
        "/cdn-cgi/challenge-platform/", "verify you are human",
    ))


def check_response(response: requests.Response, url: str) -> str:
    """Describe a response and stop requests on blocking or rate limits."""
    challenge = is_challenge(response)
    detail = (f"{diagnostic_url(url)} -> HTTP {response.status_code} "
              f"final={diagnostic_url(response.url)} "
              f"type={response.headers.get('Content-Type', 'unknown')} "
              f"bytes={len(response.content)} challenge={challenge}")
    retry_after = retry_after_seconds(response.headers.get("Retry-After", ""))
    if (response.status_code in (403, 429) or challenge or
            (response.status_code >= 500 and retry_after > 0)):
        log(f"  ! crawl paused: {detail}")
        raise FetchPaused(detail, retry_after=retry_after)
    return detail


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    """Fetch a page, retry transient errors, and report blocked access."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            target = url
            for redirect in range(11):
                r = session.get(target, timeout=REQUEST_TIMEOUT, allow_redirects=False)
                detail = check_response(r, target)
                if r.status_code not in (301, 302, 303, 307, 308):
                    break
                location = r.headers.get("Location", "")
                destination = urljoin(target, location)
                if (not location or urlparse(destination).scheme != "https" or
                        urlparse(destination).netloc != urlparse(BASE).netloc):
                    return None
                # Empty categories redirect home. The homepage has its own cold
                # poll; do not download the same empty destination on every poll.
                if destination.rstrip("/") == BASE:
                    return r
                if redirect == 10:
                    return None
                target = destination
            if r.status_code in (200, 404):
                return r
            log(f"  ! attempt {attempt}/{MAX_RETRIES}: {detail}")
            if r.status_code < 500:
                return None
            delay = max(1.5 * 2 ** (attempt - 1), retry_after_seconds(
                r.headers.get("Retry-After", "")))
        except requests.RequestException as exc:
            log(f"  ! {diagnostic_url(url)} attempt {attempt}/{MAX_RETRIES}: "
                f"{type(exc).__name__}")
            delay = 1.5 * 2 ** (attempt - 1)
        if attempt < MAX_RETRIES:
            time.sleep(delay + random.uniform(0, 0.5))
    return None


class RequestAccounting:
    """Count wire requests (including redirects/retries) in hourly windows."""

    def __init__(self):
        self.started = time.monotonic()
        self.requests = 0
        self.blocked = 0
        self.total_requests = 0
        self.total_blocked = 0

    def report(self, *, force: bool = False) -> None:
        elapsed = time.monotonic() - self.started
        if elapsed < 3600 and not force:
            return
        log(f"request accounting: window_seconds={elapsed:.1f} requests={self.requests} "
            f"blocked={self.blocked} requests_per_minute={self.requests * 60 / max(1, elapsed):.2f} "
            f"total_requests={self.total_requests} total_blocked={self.total_blocked}")
        self.started = time.monotonic()
        self.requests = self.blocked = 0


class MonitorSession(requests.Session):
    """Account for every HTTP send, even automatic redirects and failures."""

    def __init__(self):
        super().__init__()
        self.accounting = RequestAccounting()
        self.hooks["response"].append(self._record_response)
        self.next_send = 0.0

    def _record_response(self, response, *args, **kwargs):
        # Requests invokes this hook before a redirect's nested self.send().
        self.next_send = time.monotonic() + random.uniform(*INTRA_DELAY)
        if response.status_code in (403, 429) or is_challenge(response):
            self.accounting.blocked += 1
            self.accounting.total_blocked += 1

    def send(self, request, **kwargs):
        remaining = self.next_send - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self.accounting.report()
        self.accounting.requests += 1
        self.accounting.total_requests += 1
        try:
            return super().send(request, **kwargs)
        finally:
            # Also pace requests after transport failures or slow body reads.
            self.next_send = max(self.next_send, time.monotonic() + random.uniform(*INTRA_DELAY))


def category_path(category: str) -> str:
    """Resolve configured slugs to the storefront's actual category routes."""
    for category_id in CATEGORY_IDS:
        if category == category_id.lower():
            return f"{CATEGORY_ENDPOINT}?cgid={category_id}"
    return f"/{category}"


class TierSchedule:
    """Schedule individual pages; never catch up missed polls in a burst."""

    def __init__(self):
        now = time.monotonic()
        self.hot = [category_path(c) for c in HOT_CATEGORIES]
        cold = list(dict.fromkeys(
            ["/"] + [f"/{c}" for c in CATEGORIES] +
            [f"{CATEGORY_ENDPOINT}?cgid={cid}" for cid in CATEGORY_IDS] +
            ["/sitemap_index.xml"]))
        cold = [path for path in cold if path not in self.hot]
        self.xml = {"/sitemap_index.xml"}
        self.due = {path: now + i * HOT_SECONDS / len(self.hot)
                    for i, path in enumerate(self.hot)}
        self.due.update({path: now + (i + 0.5) * COLD_SECONDS / len(cold)
                         for i, path in enumerate(cold)})
        self.completed: set[str] = set()
        self.had_products = False

    def add(self, path: str, *, xml: bool = False) -> None:
        if xml:
            self.xml.add(path)
        if path not in self.due:
            cold = [p for p in self.due if p not in self.hot]
            gap = COLD_SECONDS / (len(cold) + 1)
            self.due[path] = max(time.monotonic(), *(self.due[p] for p in cold)) + gap
            log(f"scheduled discovered {'sitemap' if xml else 'category'}: {path}")

    def next(self) -> tuple[str, float]:
        # After cooldown an overdue cold page must run even if the first hot
        # page fails every time. Equal deadlines therefore favor cold pages.
        path = min(self.due, key=lambda p: (self.due[p], p in self.hot))
        return path, self.due[path]

    def advance(self, path: str) -> None:
        self.due[path] = time.monotonic() + (HOT_SECONDS if path in self.hot else COLD_SECONDS)

    def reset_coverage(self) -> None:
        self.completed.clear()
        self.had_products = False

    def resume(self) -> None:
        """Preserve cold deadlines and fairly pace overdue pages after cooldown."""
        now = time.monotonic()
        cold = sorted((path for path in self.due if path not in self.hot), key=self.due.get)
        self.due.update({path: now + i * HOT_SECONDS / len(self.hot)
                         for i, path in enumerate(self.hot)})
        next_cold = now
        for path in cold:
            # Never pull a future poll forward or restart its waiting period.
            # The oldest overdue page runs now; the rest keep the cold gap.
            self.due[path] = max(self.due[path], next_cold)
            next_cold = self.due[path] + COLD_SECONDS / len(cold)
        self.reset_coverage()

    def coverage_complete(self) -> bool:
        if self.completed >= self.due.keys():
            if not self.had_products:
                raise MonitorFailure("full sweep found no products; refusing to update state")
            log(f"full sweep succeeded: {len(self.due)} scheduled pages covered; products detected")
            self.reset_coverage()
            return True
        return False


def scheduled_crawl(session: requests.Session, schedule: TierSchedule, path: str) -> CrawlResult:
    """Fetch one scheduled page; discovery queues future cold polls only."""
    try:
        response = fetch(session, BASE + path)
    except FetchPaused as exc:
        return CrawlResult({}, 1, str(exc), exc.retry_after, blocked=True, partial=True)
    if response is None:
        return CrawlResult({}, 1, f"failed scheduled page {path}", partial=True)
    if path in schedule.xml and response.status_code != 200:
        return CrawlResult({}, 1, f"sitemap {path}: HTTP {response.status_code}", partial=True)
    products = {}
    if response.status_code == 200:
        if path in schedule.xml:
            try:
                root = ET.fromstring(response.content)
                is_index = root.tag.rsplit('}', 1)[-1] == 'sitemapindex'
                locations = [n.text for n in root.iter() if n.tag.endswith('}loc') and n.text]
                for url in locations[:4] if is_index else locations:
                    parsed = urlparse(url)
                    if parsed.scheme != "https" or parsed.netloc != "www.chromehearts.com":
                        continue
                    candidate = parsed.path.rstrip("/")
                    if is_index:
                        schedule.add(candidate, xml=True)
                    elif (re.fullmatch(r"/[a-z0-9-]+", candidate) and
                          candidate.lstrip("/") not in UTILITY_SLUGS):
                        schedule.add(candidate)
            except (ET.ParseError, DefusedXmlException) as exc:
                return CrawlResult({}, 1, f"invalid sitemap {path}: {type(exc).__name__}", partial=True)
        else:
            products = parse_products(response.text, response.url)
            for discovered in discover_category_paths(response.text):
                schedule.add(discovered)
    log(f"scheduled {'hot' if path in schedule.hot else 'cold'} page: {path} "
        f"HTTP {response.status_code}, {len(products)} products")
    return CrawlResult(products, 0, partial=True)


def probe(session: requests.Session, path: str) -> None:
    """Make exactly one HTTP request; a normal same-site redirect also proves access."""
    response = session.get(BASE + path, timeout=REQUEST_TIMEOUT, allow_redirects=False)
    detail = check_response(response, BASE + path)
    location = urlparse(response.headers.get("Location", ""))
    redirect_ok = (response.status_code in (301, 302, 303, 307, 308) and
                   bool(response.headers.get("Location")) and
                   location.netloc in ("", "www.chromehearts.com") and
                   location.scheme in ("", "https"))
    if response.status_code != 200 and not redirect_ok:
        raise MonitorFailure("probe failed: " + detail,
                             retry_after=retry_after_seconds(response.headers.get("Retry-After", "")))
    log("access probe succeeded; resuming tiered polling")


def parse_products(html: str, page_url: str = "") -> dict[str, Product]:
    found: dict[str, Product] = {}
    for span in METADATA_RE.findall(html):
        attrs = {k.lower(): v for k, v in ATTR_RE.findall(span)}
        pid = attrs.get("pid", "").strip()
        if not pid:
            continue
        m = re.search(LINK_RE_TMPL.format(pid=re.escape(pid)), html)
        page_path = urlparse(page_url).path
        if m:
            path = m.group(1)
        elif page_path.endswith(f"/{pid}.html"):
            path = page_url.removeprefix(BASE)
        else:
            path = f"/p/{pid}.html"
        category = attrs.get("category", "").strip()
        category_hint = category or path.strip("/").split("/", 1)[0]
        found[pid] = Product(
            pid=pid,
            name=attrs.get("name", "").strip() or "(unnamed)",
            price=attrs.get("price", "").strip(),
            category=category,
            url=BASE + path,
            size=infer_size(pid, category_hint),
        )
    return found


def category_slug(category: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", category.lower()).strip("-")


def infer_size(pid: str, category: str = "") -> str:
    """Infer apparel size from the SKU/PID without adding any extra requests."""
    if category and category_slug(category) not in SIZED_CATEGORY_SLUGS:
        return ""
    upper_pid = pid.upper()
    for code in sorted(SIZE_CODES, key=len, reverse=True):
        prefix_guard = r"(?<!X)" if code == "XXX" else ""
        if re.search(rf"{prefix_guard}{re.escape(code)}\d{{3}}$", upper_pid):
            label = SIZE_CODES[code]
            return label if label == code else f"{label} ({code})"
    return ""


def discover_category_paths(html: str) -> list[str]:
    """Find same-site category links in fetched HTML, including Search-Show IDs."""
    menu_paths: list[str] = []
    paths: list[str] = []
    for href in HREF_RE.findall(html):
        parsed = urlparse(href)
        if parsed.netloc and parsed.netloc != "www.chromehearts.com":
            continue
        if parsed.path == CATEGORY_ENDPOINT:
            category_ids = parse_qs(parsed.query).get("cgid", [])
            if len(category_ids) == 1 and re.fullmatch(r"[A-Za-z0-9_-]+", category_ids[0]):
                candidate = f"{CATEGORY_ENDPOINT}?cgid={category_ids[0]}"
                if candidate not in menu_paths:
                    menu_paths.append(candidate)
            continue
        path = parsed.path.rstrip("/")
        parts = [p for p in path.split("/") if p]
        if len(parts) != 1:
            continue
        slug = parts[0].lower()
        if slug in UTILITY_SLUGS or not re.fullmatch(r"[a-z0-9\-]+", slug):
            continue
        candidate = f"/{slug}"
        if candidate not in paths:
            paths.append(candidate)
    return menu_paths + paths


def sitemap_category_paths(session: requests.Session) -> list[str]:
    """Refresh the storefront's published category list at most every 10 minutes."""
    global _sitemap_paths, _sitemap_checked_at
    now = time.monotonic()
    if _sitemap_checked_at and now - _sitemap_checked_at < SITEMAP_REFRESH_SECONDS:
        return _sitemap_paths
    _sitemap_checked_at = now
    try:
        index = session.get(SITEMAP_INDEX, timeout=10)
        check_response(index, SITEMAP_INDEX)
        index.raise_for_status()
        sitemap_urls = [node.text for node in ET.fromstring(index.content).iter()
                        if node.tag.endswith("}loc") and node.text]
        paths = []
        for url in sitemap_urls[:4]:
            if urlparse(url).netloc != "www.chromehearts.com":
                continue
            response = session.get(url, timeout=10)
            check_response(response, url)
            response.raise_for_status()
            for node in ET.fromstring(response.content).iter():
                if not node.tag.endswith("}loc") or not node.text:
                    continue
                parsed = urlparse(node.text)
                if parsed.netloc != "www.chromehearts.com":
                    continue
                path = parsed.path.rstrip("/")
                slug = path.lstrip("/").lower()
                if (re.fullmatch(r"/[a-z0-9-]+", path) and
                        slug not in UTILITY_SLUGS and path not in paths):
                    paths.append(path)
        _sitemap_paths = paths
        return paths
    except FetchPaused:
        _sitemap_checked_at = 0.0
        raise
    except (requests.RequestException, ET.ParseError, DefusedXmlException) as exc:
        log(f"sitemap refresh failed: {exc}")
        _sitemap_checked_at = 0.0
        raise MonitorFailure(f"sitemap refresh failed: {type(exc).__name__}") from exc


def crawl(session: requests.Session) -> CrawlResult:
    """Collect products until coverage is complete or requests must pause."""
    catalog: dict[str, Product] = {}
    try:
        sitemap_paths = sitemap_category_paths(session)
    except MonitorFailure as exc:
        blocked = isinstance(exc, FetchPaused)
        summary = f"sitemap refresh {'blocked' if blocked else 'failed'}; " + str(exc)
        log(f"crawl coverage: {summary}")
        return CrawlResult({}, 1, summary, exc.retry_after, blocked=blocked)
    queued = (["/"] + [f"/{c}" for c in CATEGORIES] +
              [f"{CATEGORY_ENDPOINT}?cgid={cid}" for cid in CATEGORY_IDS] +
              sitemap_paths)
    seen: set[str] = set()
    discovered = 0
    fetched = 0
    missing = 0
    errors = 0
    consecutive_errors = 0
    statuses: Counter[int] = Counter()
    empty_pages: list[str] = []
    paused = ""
    retry_after = 0.0
    blocked = False
    for path in queued:
        if path in seen:
            continue
        seen.add(path)
        try:
            r = fetch(session, BASE + path)
        except FetchPaused as exc:
            blocked = True
            errors += 1
            paused = str(exc)
            retry_after = exc.retry_after
            break
        time.sleep(random.uniform(*INTRA_DELAY))
        if r is None:
            errors += 1
            consecutive_errors += 1
            if consecutive_errors >= 3:
                paused = "three consecutive page fetches failed; stopped this crawl early"
                break
            continue
        consecutive_errors = 0
        statuses[r.status_code] += 1
        if r.status_code != 200:
            missing += 1
            continue
        fetched += 1
        html = r.text
        products = parse_products(html, r.url)
        catalog.update(products)  # empty pages add nothing
        if not products and len(empty_pages) < 3:
            empty_pages.append(f"{diagnostic_url(BASE + path)} -> "
                               f"{diagnostic_url(r.url)} ({len(r.content)} bytes)")
        for discovered_path in discover_category_paths(html):
            if discovered_path in seen or discovered_path in queued:
                continue
            queued.append(discovered_path)
            discovered += 1
    summary = (f"{fetched} pages fetched, {missing} unavailable, {errors} fetch errors, "
               f"{discovered} discovered categories, {len(catalog)} products; "
               f"HTTP counts={dict(statuses)}")
    if paused:
        summary += f"; {paused}"
    if not catalog and empty_pages:
        summary += "; empty page samples: " + "; ".join(empty_pages)
    log(f"crawl coverage: {summary}")
    return CrawlResult(catalog, errors, summary, retry_after, blocked=blocked)


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #

def load_state() -> dict[str, dict]:
    return json.loads(STATE_FILE.read_text())


def save_state(catalog: dict[str, Product | dict]) -> None:
    """Persist accumulated product history without dropping previously seen IDs."""
    serialized = {
        pid: asdict(product) if isinstance(product, Product) else product
        for pid, product in catalog.items()
    }
    write_json(STATE_FILE, serialized)


def write_json(path: Path, value: dict) -> None:
    """Replace a JSON file atomically on its current filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    tmp.replace(path)  # atomic, so a crash mid-write can't corrupt state


@dataclass
class HealthState:
    """Incident status and deadlines that survive worker restarts."""
    failures: int = 0
    alerted: bool = False
    last_error: str = ""
    next_sweep_at: float = 0
    next_notification_at: float = 0
    last_alert_at: float = 0
    blocked: bool = False
    block_failures: int = 0


def load_health() -> HealthState:
    """Load persisted incident status without blocking on missing or invalid files."""
    try:
        state = HealthState(**json.loads(HEALTH_FILE.read_text()))
        if (type(state.failures) is not int or state.failures < 0 or
                type(state.blocked) is not bool or
                type(state.block_failures) is not int or state.block_failures < 0 or
                type(state.alerted) is not bool or
                not isinstance(state.last_error, str) or
                not all(isinstance(value, (int, float)) and 0 <= value < float("inf")
                        for value in (state.next_sweep_at, state.next_notification_at,
                                      state.last_alert_at))):
            raise ValueError("invalid health state")
        return state
    except FileNotFoundError:
        return HealthState()
    except (OSError, ValueError, TypeError) as exc:
        log(f"cannot read health state ({type(exc).__name__}); starting a new incident tracker")
        return HealthState()


class HealthMonitor:
    """Track failures and keep prolonged incidents visible privately."""

    def __init__(self, *, dry_run: bool):
        """Load cooldowns in every loop mode; dry runs never persist changes."""
        self.dry_run = dry_run
        self.state = load_health()

    def persist(self) -> None:
        """Save incident status while allowing recovery from volume write failures."""
        if not self.dry_run:
            try:
                write_json(HEALTH_FILE, asdict(self.state))
            except OSError as exc:
                log(f"health state could not be saved ({type(exc).__name__}); "
                    "restart deduplication requires a writable persistent volume")

    def failure(self, exc: Exception) -> None:
        """Record a failed sweep, defer requests, and warn after the threshold."""
        self.state.failures += 1
        self.state.last_error = str(exc)[:1000]
        if isinstance(exc, FetchPaused):
            self.state.blocked = True
        if self.state.blocked:
            self.state.block_failures += 1
            base = min(BACKOFF_MAX_SECONDS, 300 * 2 ** min(self.state.block_failures - 1, 16))
            jitter = random.uniform(0, base * 0.1)
            delay = base - jitter if base == BACKOFF_MAX_SECONDS else min(BACKOFF_MAX_SECONDS, base + jitter)
        else:
            delay = min(BACKOFF_MAX_SECONDS,
                        max(2, POLL_SECONDS) * 2 ** min(self.state.failures, 16))
        delay = max(delay, getattr(exc, "retry_after", 0))
        now = time.time()
        self.state.next_sweep_at = now + delay
        self.persist()
        log(f"{self.state.failures} consecutive failed sweeps; retrying in {delay:.0f}s")
        self.notify_failure()

    def notify_failure(self) -> None:
        """Retry warnings and reminders independently of long probe deadlines."""
        now = time.time()
        if (self.dry_run or
                self.state.failures < HEALTH_FAILURE_THRESHOLD or
                now < self.state.next_notification_at):
            return
        if self.state.alerted and (not HEALTH_REMINDER_SECONDS or
                (self.state.last_alert_at and
                 now - self.state.last_alert_at < HEALTH_REMINDER_SECONDS)):
            return
        self.state.next_notification_at = now + NOTIFICATION_RETRY_SECONDS
        self.persist()
        status = "is still failing" if self.state.alerted else "needs attention"
        try:
            updated_at = STATE_FILE.stat().st_mtime
            updated = datetime.fromtimestamp(updated_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            history = (f"Last product-history update: {updated} "
                       f"({max(0, now - updated_at) / 3600:.1f} hours ago).")
        except (OSError, ValueError, OverflowError):
            history = "Last product-history update: unavailable."
        reminder = (f"Private reminders repeat every {HEALTH_REMINDER_SECONDS / 3600:g} hours until recovery."
                    if HEALTH_REMINDER_SECONDS else "Further warnings are suppressed until recovery.")
        try:
            _send_health(f"\u26a0\ufe0f Chrome Hearts product monitor {status} "
                  f"after {self.state.failures} consecutive failed sweeps: "
                  f"{self.state.last_error}\n"
                  f"Product alerts may be missing. {history}\n"
                  f"Retrying with backoff. {reminder}")
        except Exception as alert_exc:
            log(f"health alert failed: {alert_exc!r}")
            return
        self.state.alerted = True
        self.state.last_alert_at = now
        self.persist()

    def probe_success(self) -> None:
        """Reset access backoff; defer recovery notice until full coverage succeeds."""
        self.state.blocked = False
        self.state.block_failures = 0
        self.state.next_sweep_at = 0
        self.persist()

    def success(self) -> None:
        """Clear failures and retry any pending recovery notification."""
        if not (self.state.failures or self.state.alerted):
            return
        self.state.failures = 0
        self.state.next_sweep_at = 0
        self.persist()
        if self.state.alerted and not self.dry_run:
            if time.time() < self.state.next_notification_at:
                return
            self.state.next_notification_at = time.time() + NOTIFICATION_RETRY_SECONDS
            self.persist()
            try:
                _send_health("\U0001f7e2 Chrome Hearts product monitor recovered — "
                      "products are being detected again and the sweep completed successfully.")
            except Exception as exc:
                log(f"recovery notification failed: {exc!r}")
                return
        self.state = HealthState()
        self.persist()


# --------------------------------------------------------------------------- #
# Notify
# --------------------------------------------------------------------------- #

def _send(body: str, *, suppress_embeds: bool = False) -> None:
    from notifier import send_notification
    send_notification(body, suppress_embeds=suppress_embeds)


def _send_health(body: str) -> None:
    """Keep issue warnings, recovery notices, and startup pings out of drop channels."""
    from notifier import send_monitor_notification
    send_monitor_notification(body)


def build_batch_messages(products: list[Product]) -> list[str]:
    """Format every item into readable, Discord-safe summary messages."""
    item_lines = []
    for p in products:
        price = f"${p.price}" if p.price else "price n/a"
        size = f" · {p.size}" if p.size else ""
        item_lines.append(
            f"• **{p.name}**{size} — {price} · [View item]({p.url})"
        )

    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for line in item_lines:
        if current and (len(current) >= MAX_INDIVIDUAL or
                        current_chars + len(line) + 1 > SUMMARY_MESSAGE_LIMIT):
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += len(line) + 1
    if current:
        chunks.append(current)

    total = len(chunks)
    return [
        "\n".join([
            f"\U0001f6a8 {len(products)} new Chrome Hearts items — "
            f"batch {index}/{total}",
            *chunk,
        ])
        for index, chunk in enumerate(chunks, 1)
    ]


def notify_new(products: list[Product]) -> None:
    """Send small drops individually and paginate every item in large drops."""
    if len(products) <= MAX_INDIVIDUAL:
        for p in products:
            _send(f"\U0001f6a8 New Chrome Hearts drop\n{p.pretty()}")
            time.sleep(1)
        return
    for message in build_batch_messages(products):
        _send(message, suppress_embeds=True)
        time.sleep(1)


# --------------------------------------------------------------------------- #
# Sweep
# --------------------------------------------------------------------------- #

def sweep(session: requests.Session, *, seed: bool, dry_run: bool,
          schedule: TierSchedule | None = None, path: str = "") -> None:
    """Process a crawl without losing a server cooldown if later work fails."""
    result = crawl(session) if schedule is None else scheduled_crawl(session, schedule, path)
    try:
        process_crawl(result, seed=seed, dry_run=dry_run)
        if schedule is not None:
            schedule.completed.add(path)
            schedule.had_products |= bool(result.catalog)
    except MonitorFailure:
        raise
    except Exception as exc:
        if not result.retry_after:
            raise
        raise MonitorFailure(f"{type(exc).__name__}: {exc}",
                             retry_after=result.retry_after) from exc


def process_crawl(result: CrawlResult, *, seed: bool, dry_run: bool) -> None:
    """Deliver new products and update history after checking crawl coverage."""
    catalog, fetch_errors = result.catalog, result.errors
    if fetch_errors:
        failure = FetchPaused if result.blocked else MonitorFailure
        raise failure("catalog coverage may be incomplete. " + result.summary,
                      retry_after=result.retry_after)
    if not catalog and result.partial:
        # Empty/sold-out categories and sitemap polls are normal; retain history.
        return
    if not catalog:
        raise MonitorFailure("crawl found no products; refusing to update state. " + result.summary,
                             retry_after=result.retry_after)
    if seed:
        save_state(catalog)
        log(f"seeded {len(catalog)} products (no notifications).")
        return
    previous = load_state()
    if not previous:
        raise RuntimeError("state is empty; run --seed explicitly after inspecting the catalog")

    new = [catalog[pid] for pid in catalog if pid not in previous]
    seen = {**previous, **{pid: asdict(p) for pid, p in catalog.items()}}
    if not new:
        log(f"{len(catalog)} live, 0 new.")
        if not dry_run:
            save_state(seen)
        return

    log(f"{len(catalog)} live, {len(new)} NEW:")
    for p in new:
        logged = replace(p, url=diagnostic_url(p.url))
        log("   + " + logged.pretty().replace("\n", " | "))
    if dry_run:
        log("[dry-run] not sending.")
    else:
        notify_new(new)
        log("notified.")
        save_state(seen)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    """Run the selected monitor mode and recover from failures in loop mode."""
    ap = argparse.ArgumentParser(description="Chrome Hearts new-product monitor")
    ap.add_argument("--duration", type=float, default=0, help="stop loop after this many seconds (0: forever)")
    ap.add_argument("--loop", action="store_true", help="run forever (Railway)")
    ap.add_argument("--once", action="store_true", help="one sweep then exit")
    ap.add_argument("--seed", action="store_true", help="record catalog, no alerts")
    ap.add_argument("--dry-run", action="store_true", help="detect but never send")
    args = ap.parse_args()

    session = MonitorSession()
    session.headers.update(HEADERS)

    if args.seed:
        sweep(session, seed=True, dry_run=True)
        return 0
    if args.once:
        sweep(session, seed=False, dry_run=args.dry_run)
        return 0
    if not args.loop:
        ap.error("choose a mode: --loop, --once, or --seed")
    if HEALTH_FILE.resolve() == STATE_FILE.resolve():
        ap.error("CH_HEALTH_FILE must differ from CH_STATE_FILE")

    health = HealthMonitor(dry_run=args.dry_run)
    schedule = TierSchedule()
    log(f"Chrome Hearts worker started; {len(schedule.hot)} hot pages every {HOT_SECONDS}s, "
        f"{len(schedule.due) - len(schedule.hot)} cold pages every {COLD_SECONDS}s. state={STATE_FILE}")
    startup_pending = (STARTUP_PING and not args.dry_run and
                       not (health.state.failures or health.state.alerted))
    end = time.monotonic() + args.duration if args.duration > 0 else float("inf")
    next_request = 0.0
    try:
        while time.monotonic() < end:
            session.accounting.report()
            if health.state.failures:
                health.notify_failure()
            elif health.state.alerted:
                health.success()  # Retry a pending recovery notice on its own deadline.
            path, due = schedule.next()
            delay = max(health.state.next_sweep_at - time.time(), next_request - time.monotonic(),
                        0 if health.state.blocked else due - time.monotonic())
            if delay > 0:
                time.sleep(min(delay, 30, max(0, end - time.monotonic())))
                continue
            if not health.state.blocked and health.state.next_sweep_at:
                schedule.resume()
                health.state.next_sweep_at = 0
                health.persist()
                path, due = schedule.next()
            if health.state.blocked:
                try:
                    probe(session, schedule.hot[0])
                except Exception as exc:
                    log(f"probe error (continuing): {exc!r}")
                    health.failure(exc)
                else:
                    health.probe_success()
                    schedule.resume()
                next_request = time.monotonic() + random.uniform(*INTRA_DELAY)
                continue
            try:
                sweep(session, seed=False, dry_run=args.dry_run, schedule=schedule, path=path)
                complete = schedule.coverage_complete()
            except Exception as exc:
                startup_pending = False
                log(f"sweep error (continuing): {exc!r}")
                schedule.reset_coverage()
                health.failure(exc)
            else:
                # A successful page is not proof of complete catalog coverage.
                if complete:
                    health.success()
                    if startup_pending:
                        startup_pending = False
                        try:
                            _send_health("\U0001f7e2 Chrome Hearts monitor online — "
                                         "first complete product sweep succeeded. "
                                         f"Watching {len(schedule.due)} scheduled pages.")
                        except Exception as exc:
                            log(f"startup ping failed: {exc}")
            schedule.advance(path)
            next_request = time.monotonic() + random.uniform(*INTRA_DELAY)
    finally:
        session.accounting.report(force=True)
        session.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
