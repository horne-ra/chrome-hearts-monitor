import unittest
import tempfile
import sys
import json
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

import chrome_hearts_monitor as monitor
import notifier


PRODUCT_HTML = '''
<span class="product-metadata d-none" data-pid="190372BLKXXX01W"
      data-name="BLACK SWEATPANTS" data-price="730.00"
      data-category="HOODIE + SWEATPANTS"></span>
'''
PRODUCT_URL = ("https://www.chromehearts.com/black-sweatpants/190372BLKXXX01W.html"
               "?dwvar_190372BLKXXX01W_size=XSM")


def response(status=200, html=PRODUCT_HTML, headers=None, url=PRODUCT_URL):
    """Build an HTTP response fixture without network I/O."""
    result = monitor.requests.Response()
    result.status_code = status
    result.url = url
    result.request = monitor.requests.Request('GET', url).prepare()
    result._content = html.encode()
    result.headers.update(headers or {"Content-Type": "text/html"})
    return result


class ProductDiscoveryTests(unittest.TestCase):
    def test_eyewear_redirect_discovers_and_notifies_mio_lord(self):
        """Reproduce the missed release using its redirected category and metadata."""
        url = "https://www.chromehearts.com/eyewear/mio-lord/21978827FE50010.html"
        html = ('<span class="product-metadata d-none" data-pid="21978827FE50010" '
                'data-name="MIO LORD" data-price="1470.00" data-category="EYEWEAR"></span>')
        self.assertIn("eyewear", monitor.CATEGORIES)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "seen.json"
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "CATEGORIES", ["eyewear"]), \
                 patch.object(monitor, "CATEGORY_IDS", []), \
                 patch.object(monitor, "sitemap_category_paths", return_value=[]), \
                 patch.object(monitor, "fetch", side_effect=[response(html="", url=monitor.BASE),
                                                           response(html=html, url=url)]), \
                 patch.object(monitor.time, "sleep"), \
                 patch.object(monitor, "notify_new") as notify:
                monitor.sweep(object(), seed=False, dry_run=False)
            product = notify.call_args.args[0][0]
            self.assertEqual((product.pid, product.name, product.url),
                             ("21978827FE50010", "MIO LORD", url))
            self.assertIn(product.pid, json.loads(state.read_text()))

    def test_search_show_category_link_is_discovered(self):
        """Verify search show category link is discovered."""
        html = ('<a href="/on/demandware.store/Sites-ChromeHearts-Site/en_US/'
                'Search-Show?cgid=SWEATPANTS">SWEATPANTS</a>')
        self.assertIn(f"{monitor.CATEGORY_ENDPOINT}?cgid=SWEATPANTS",
                      monitor.discover_category_paths(html))

    def test_redirected_product_uses_its_real_url(self):
        """Verify redirected product uses its real url."""
        product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)["190372BLKXXX01W"]
        self.assertEqual(product.url, PRODUCT_URL)
        self.assertEqual(product.price, "730.00")

    def test_single_segment_product_link_is_recognized(self):
        """Verify single segment product link is recognized."""
        html = PRODUCT_HTML + '<a href="/black-sweatpants/190372BLKXXX01W.html">View</a>'
        product = monitor.parse_products(html)["190372BLKXXX01W"]
        self.assertEqual(product.url, PRODUCT_URL.split("?")[0])

    def test_crawl_checks_sweatpants_category(self):
        """Verify crawl checks sweatpants category."""
        class Response:
            status_code = 200
            text = PRODUCT_HTML
            url = PRODUCT_URL

        with patch.object(monitor, "CATEGORIES", []), \
             patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor, "fetch", side_effect=lambda _, url: Response()
                          if "cgid=SWEATPANTS" in url else None), \
             patch.object(monitor.time, "sleep"):
            result = monitor.crawl(object())
        self.assertEqual(result.catalog["190372BLKXXX01W"].url, PRODUCT_URL)
        self.assertEqual(result.errors, 1)

    def test_sitemap_adds_categories_missing_from_static_list(self):
        """Verify sitemap adds categories missing from static list."""
        index = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.chromehearts.com/sitemap_0.xml</loc></sitemap></sitemapindex>'
        catalog = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.chromehearts.com/new-category</loc></url><url><loc>https://www.chromehearts.com/shop</loc></url></urlset>'

        class Session:
            def get(self, url, timeout):
                """Return deterministic sitemap content for this test."""
                content = index if url.endswith('sitemap_index.xml') else catalog
                return response(html=content.decode(), url=url)

        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor, "_sitemap_paths", []):
            self.assertEqual(monitor.sitemap_category_paths(Session()), ['/new-category'])

    def test_sitemap_rejects_entity_declarations(self):
        """Verify sitemap rejects entity declarations."""
        class Session:
            def get(self, url, timeout):
                """Return deterministic sitemap content for this test."""
                return response(html='<!DOCTYPE x [<!ENTITY x "bad">]><sitemapindex><loc>&x;</loc></sitemapindex>', url=url)

        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor, "_sitemap_paths", ['/known']):
            with self.assertRaises(monitor.MonitorFailure):
                monitor.sitemap_category_paths(Session())
            self.assertEqual(monitor._sitemap_paths, ['/known'])

    def test_dry_run_does_not_mark_product_seen(self):
        """Verify dry run does not mark product seen."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 0)), \
                 patch.object(monitor, "notify_new") as notify:
                monitor.sweep(object(), seed=False, dry_run=True)
            self.assertEqual(state.read_text(), '{"old": {}}')
            notify.assert_not_called()

    def test_missing_state_cannot_silently_seed(self):
        """Verify missing state cannot silently seed."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing.json'
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 0)):
                with self.assertRaises(FileNotFoundError):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertFalse(state.exists())

    def test_empty_crawl_cannot_update_state(self):
        """Verify empty crawl cannot update state."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult({}, 0)):
                with self.assertRaisesRegex(RuntimeError, 'no products'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertEqual(state.read_text(), '{"old": {}}')

    def test_failed_delivery_does_not_mark_product_seen(self):
        """Verify failed delivery does not mark product seen."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 0)), \
                 patch.object(monitor, "notify_new", side_effect=RuntimeError('delivery failed')):
                with self.assertRaisesRegex(RuntimeError, 'delivery failed'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertEqual(state.read_text(), '{"old": {}}')

    def test_partial_fetch_reports_degraded_coverage(self):
        """Verify partial fetch reports degraded coverage."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 1)), \
                 patch.object(monitor, "notify_new") as notify:
                with self.assertRaisesRegex(RuntimeError, 'coverage may be incomplete'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            notify.assert_not_called()
            self.assertEqual(state.read_text(), '{"old": {}}')

    def test_discord_http_error_is_a_delivery_failure(self):
        """Verify discord http error is a delivery failure."""
        class Response:
            status_code = 429
            text = 'rate limited'

        with patch.dict(notifier.os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.test/webhook'}), \
             patch.object(notifier.requests, 'post', return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429: 'rate limited'"):
                notifier._send_discord('test')

    def test_twilio_http_error_includes_bounded_response(self):
        """Verify twilio http error includes bounded response."""
        class Response:
            status_code = 400
            text = 'x' * 250

        env = {'TWILIO_ACCOUNT_SID': 'test', 'TWILIO_AUTH_TOKEN': 'test',
               'TWILIO_FROM': '+10000000000', 'TWILIO_TO': '+19999999999'}
        with patch.dict(notifier.os.environ, env), \
             patch.object(notifier.requests, 'post', return_value=Response()):
            with self.assertRaises(RuntimeError) as caught:
                notifier._send_twilio('test')
        self.assertIn('HTTP 400', str(caught.exception))
        self.assertIn('x' * 200, str(caught.exception))
        self.assertNotIn('x' * 201, str(caught.exception))

    def test_wrapped_http_error_keeps_response_content(self):
        """Verify wrapped http error keeps response content."""
        response = notifier.requests.Response()
        response.status_code = 403
        response._content = b'forbidden'
        error = notifier.requests.HTTPError(response=response)
        with patch.dict(notifier.os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.test/webhook'}), \
             patch.object(notifier.requests, 'post', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403: 'forbidden'"):
                notifier._send_discord('test')

class FetchDiagnosticsTests(unittest.TestCase):
    def test_sitemap_block_stops_before_category_requests(self):
        """Verify sitemap block stops before category requests."""
        session = monitor.requests.Session()
        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(session, "get", return_value=response(403)) as get:
            result = monitor.crawl(session)
        get.assert_called_once_with(monitor.SITEMAP_INDEX, timeout=10)
        self.assertEqual(result.catalog, {})
        self.assertIn("sitemap refresh blocked", result.summary)

    def test_child_sitemap_rate_limit_preserves_cooldown(self):
        """Verify child sitemap rate limit preserves cooldown."""
        index = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.chromehearts.com/sitemap_0.xml</loc></sitemap></sitemapindex>'
        session = monitor.requests.Session()
        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(session, "get", side_effect=[response(html=index), response(429, headers={"Retry-After": "900"})]) as get:
            result = monitor.crawl(session)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(result.retry_after, 900)

    def test_sitemap_503_honors_server_cooldown(self):
        """Verify sitemap 503 honors server cooldown."""
        index = '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.chromehearts.com/sitemap_0.xml</loc></sitemap></sitemapindex>'
        for child in (False, True):
            for retry_after in (5, 900):
                with self.subTest(child=child, retry_after=retry_after):
                    responses = ([response(html=index)] if child else []) + [response(503, headers={"Retry-After": str(retry_after)})]
                    session = monitor.requests.Session()
                    with patch.object(monitor, "_sitemap_checked_at", 0.0), \
                         patch.object(session, "get", side_effect=responses) as get:
                        result = monitor.crawl(session)
                    self.assertEqual(result.retry_after, retry_after)
                    self.assertEqual(get.call_count, 2 if child else 1)

    def test_paused_sitemap_is_retried_on_next_permitted_sweep(self):
        """Verify paused sitemap is retried on next permitted sweep."""
        session = monitor.requests.Session()
        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor.time, "monotonic", return_value=1000), \
             patch.object(monitor, "CATEGORIES", []), patch.object(monitor, "CATEGORY_IDS", []), \
             patch.object(monitor.time, "sleep"), \
             patch.object(session, "get", side_effect=[response(403), response(html='<sitemapindex/>'), response()]) as get:
            self.assertEqual(monitor.crawl(session).errors, 1)
            result = monitor.crawl(session)
        self.assertEqual(get.call_count, 3)
        self.assertEqual(len(result.catalog), 1)
        self.assertEqual(result.errors, 0)

    def test_other_sitemap_http_failures_keep_cache_but_fail_the_sweep(self):
        """Verify other sitemap http failures keep cached categories."""
        session = monitor.requests.Session()
        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor, "_sitemap_paths", ["/known"]), \
             patch.object(session, "get", return_value=response(503)):
            with self.assertRaises(monitor.MonitorFailure):
                monitor.sitemap_category_paths(session)
            self.assertEqual(monitor._sitemap_paths, ["/known"])
            self.assertEqual(monitor._sitemap_checked_at, 0)

    def test_failed_crawl_preserves_cooldown_without_processing(self):
        """Coverage failures stop state reads, writes and notifications before processing."""
        products = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
        for target in ("load_state", "save_state", "notify_new"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "seen.json"
                state.write_text('{"old": {}}')
                with patch.object(monitor, "STATE_FILE", state), \
                     patch.object(monitor, "crawl", return_value=monitor.CrawlResult(products, 1, "HTTP 429", 900)), \
                     patch.object(monitor, "notify_new"), \
                     patch.object(monitor, target, side_effect=RuntimeError("later failure")) as processing:
                    with self.assertRaises(monitor.MonitorFailure) as caught:
                        monitor.sweep(object(), seed=False, dry_run=False)
                self.assertEqual(caught.exception.retry_after, 900)
                processing.assert_not_called()

    def test_product_logs_strip_queries_without_changing_notification_urls(self):
        """Verify product logs strip queries without changing notification urls."""
        url = PRODUCT_URL + "&token=secret"
        products = monitor.parse_products(PRODUCT_HTML, url)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "seen.json"
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(products, 0)), \
                 patch.object(monitor, "log") as log, \
                 patch.object(monitor, "notify_new") as notify:
                monitor.sweep(object(), seed=False, dry_run=False)
            self.assertNotIn("secret", str(log.call_args_list))
            self.assertEqual(notify.call_args.args[0][0].url, url)

    def test_partial_dry_run_keeps_cooldown_without_writing_product_history(self):
        """Verify partial dry run keeps cooldown without writing product history."""
        products = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
        for previous in ('{"old": {}}', json.dumps({pid: {} for pid in products})):
            with self.subTest(previous=previous), tempfile.TemporaryDirectory() as directory:
                state = Path(directory) / "seen.json"
                state.write_text(previous)
                with patch.object(monitor, "STATE_FILE", state), \
                     patch.object(monitor, "crawl", return_value=monitor.CrawlResult(products, 1, "HTTP 429", 900)), \
                     patch.object(monitor, "notify_new") as notify:
                    with self.assertRaises(monitor.MonitorFailure) as caught:
                        monitor.sweep(object(), seed=False, dry_run=True)
                self.assertEqual(caught.exception.retry_after, 900)
                self.assertEqual(state.read_text(), previous)
                notify.assert_not_called()

    def test_rate_limit_stops_requests_and_preserves_retry_after(self):
        """Verify rate limit stops requests and preserves retry after."""
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(429, headers={"Retry-After": "120"})) as get, \
             patch.object(monitor.time, "sleep") as sleep, \
             patch.object(monitor, "log") as log:
            with self.assertRaises(monitor.FetchPaused) as caught:
                monitor.fetch(session, monitor.BASE + "/socks?token=secret")
        self.assertEqual(caught.exception.retry_after, 120)
        get.assert_called_once()
        sleep.assert_not_called()
        self.assertIn("HTTP 429", str(caught.exception))
        self.assertIn("final=https://www.chromehearts.com/black-sweatpants/", str(caught.exception))
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("HTTP 429", str(log.call_args))

    def test_403_stops_the_entire_crawl(self):
        """Verify 403 stops the entire crawl."""
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor.requests.Session, "get", return_value=response(403)) as get:
            result = monitor.crawl(monitor.requests.Session())
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.catalog, {})
        self.assertIn("HTTP 403", result.summary)
        get.assert_called_once()

    def test_challenge_with_http_200_is_not_a_successful_empty_page(self):
        """Verify challenge with http 200 is not a successful empty page."""
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(html="<title>Just a moment...</title>")):
            with self.assertRaisesRegex(monitor.FetchPaused, "challenge=True"):
                monitor.fetch(session, monitor.BASE)
        self.assertFalse(monitor.is_challenge(response(html="<!-- Needed for Recaptcha -->")))
        self.assertTrue(monitor.is_challenge(response(headers={"cf-mitigated": "challenge"})))
        self.assertTrue(monitor.is_challenge(response(
            403, html="<title>Attention Required! | Cloudflare</title>")))

    def test_retry_after_accepts_http_dates_and_bounds_invalid_values(self):
        """Verify retry after accepts http dates and bounds invalid values."""
        with patch.object(monitor.time, "time", return_value=1000):
            self.assertEqual(monitor.retry_after_seconds(formatdate(1120, usegmt=True)), 120)
        self.assertEqual(monitor.retry_after_seconds("not a date"), 0)
        self.assertEqual(monitor.retry_after_seconds("-5"), 0)
        self.assertEqual(monitor.retry_after_seconds("99999"), 99999)
        self.assertEqual(monitor.retry_after_seconds("inf"), 0)
        self.assertEqual(monitor.retry_after_seconds("nan"), 0)

    def test_server_errors_retry_without_sleeping_after_final_attempt(self):
        """Verify server errors retry without sleeping after final attempt."""
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(503)) as get, \
             patch.object(monitor.time, "sleep") as sleep, \
             patch.object(monitor.random, "uniform", return_value=0):
            self.assertIsNone(monitor.fetch(session, monitor.BASE))
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.5, 3])

    def test_server_retry_after_defers_next_sweep_without_more_requests(self):
        """Verify server retry after defers next sweep without more requests."""
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(503, headers={"Retry-After": "600"})) as get:
            with self.assertRaises(monitor.FetchPaused) as caught:
                monitor.fetch(session, monitor.BASE)
        self.assertEqual(caught.exception.retry_after, 600)
        get.assert_called_once()

    def test_network_outage_stops_after_three_failed_pages(self):
        """Verify network outage stops after three failed pages."""
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor, "fetch", return_value=None) as fetch, \
             patch.object(monitor.time, "sleep"):
            result = monitor.crawl(object())
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result.errors, 3)
        self.assertIn("stopped this crawl early", result.summary)

    def test_empty_http_200_pages_include_diagnostic_samples(self):
        """Verify empty http 200 pages include diagnostic samples."""
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor, "CATEGORIES", []), patch.object(monitor, "CATEGORY_IDS", []), \
             patch.object(monitor, "fetch", return_value=response(html="<html>Welcome</html>")), \
             patch.object(monitor.time, "sleep"):
            result = monitor.crawl(object())
        self.assertIn("HTTP counts={200: 1}", result.summary)
        self.assertIn("empty page samples", result.summary)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(monitor, "STATE_FILE", Path(directory) / "seen.json"), \
             patch.object(monitor, "crawl", return_value=result):
            with self.assertRaisesRegex(monitor.MonitorFailure, "empty page samples"):
                monitor.sweep(object(), seed=False, dry_run=False)

    def test_products_before_blocking_are_not_delivered_or_recorded(self):
        """Products collected before a block must not change history or notify."""
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "seen.json"
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "sitemap_category_paths", return_value=[]), \
                 patch.object(monitor, "fetch", side_effect=[response(), monitor.FetchPaused("HTTP 429", retry_after=600)]), \
                 patch.object(monitor.time, "sleep"), \
                 patch.object(monitor, "notify_new") as notify:
                with self.assertRaises(monitor.MonitorFailure) as caught:
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertEqual(caught.exception.retry_after, 600)
            notify.assert_not_called()
            self.assertEqual(state.read_text(), '{"old": {}}')


class NotificationRoutingTests(unittest.TestCase):
    def test_monitor_notices_use_only_the_private_webhook(self):
        """Send operational messages to the dedicated route regardless of drop backend."""
        env = {"DISCORD_WEBHOOK_URL": "https://example.test/drops",
               "DISCORD_MONITOR_WEBHOOK_URL": "https://example.test/issues",
               "NOTIFY_METHOD": "twilio"}
        with patch.dict(notifier.os.environ, env, clear=True), \
             patch.object(notifier.requests, "post", return_value=response(204)) as post:
            monitor._send_health("monitor issue")
        self.assertEqual(post.call_args.args[0], env["DISCORD_MONITOR_WEBHOOK_URL"])
        self.assertEqual(post.call_args.kwargs["json"]["allowed_mentions"], {"parse": []})
        self.assertEqual(post.call_args.kwargs["json"]["flags"], 1 << 2)

    def test_missing_monitor_webhook_never_falls_back_to_group(self):
        """Keep configuration mistakes from exposing operational notices to the group."""
        env = {"DISCORD_WEBHOOK_URL": "https://example.test/drops"}
        with patch.dict(notifier.os.environ, env, clear=True), \
             patch.object(notifier.requests, "post") as post:
            with self.assertRaisesRegex(RuntimeError, "DISCORD_MONITOR_WEBHOOK_URL"):
                monitor._send_health("monitor issue")
        post.assert_not_called()

    def test_failed_monitor_webhook_never_falls_back_to_group(self):
        """Retry delivery through health tracking without attempting the drop destination."""
        env = {"DISCORD_WEBHOOK_URL": "https://example.test/drops",
               "DISCORD_MONITOR_WEBHOOK_URL": "https://example.test/issues"}
        with patch.dict(notifier.os.environ, env, clear=True), \
             patch.object(notifier.requests, "post", return_value=response(500)) as post:
            with self.assertRaisesRegex(RuntimeError, "HTTP 500"):
                monitor._send_health("monitor issue")
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], env["DISCORD_MONITOR_WEBHOOK_URL"])

    def test_product_drop_still_uses_existing_group_webhook(self):
        """Keep normal product alerts on the previously configured destination."""
        env = {"DISCORD_WEBHOOK_URL": "https://example.test/drops",
               "DISCORD_MONITOR_WEBHOOK_URL": "https://example.test/issues"}
        product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)["190372BLKXXX01W"]
        with patch.dict(notifier.os.environ, env, clear=True), \
             patch.object(notifier.requests, "post", return_value=response(204)) as post, \
             patch.object(monitor.time, "sleep"):
            monitor.notify_new([product])
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], env["DISCORD_WEBHOOK_URL"])


class HealthIncidentTests(unittest.TestCase):
    def setUp(self):
        """Isolate persisted health state and retry settings for each test."""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "seen.health.json"
        for patcher in (patch.object(monitor, "HEALTH_FILE", self.path),
                        patch.object(monitor, "HEALTH_FAILURE_THRESHOLD", 3),
                        patch.object(monitor, "POLL_SECONDS", 30),
                        patch.object(monitor, "HEALTH_REMINDER_SECONDS", 21600),
                        patch.object(monitor, "BACKOFF_MAX_SECONDS", 300)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_transient_failures_clear_silently(self):
        """Verify transient failures clear silently."""
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor, "_send_health") as send:
            health.failure(RuntimeError("temporary"))
            health.failure(RuntimeError("temporary"))
            health.success()
        send.assert_not_called()
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_one_warning_survives_restarts_and_new_incident_alerts_after_recovery(self):
        """Verify one warning survives restarts and new incident alerts after recovery."""
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send_health") as send:
            for _ in range(3):
                health.failure(RuntimeError("empty crawl"))
            self.assertEqual(send.call_count, 1)
            health = monitor.HealthMonitor(dry_run=False)
            health.failure(RuntimeError("still empty"))
            self.assertEqual(send.call_count, 1)
        with patch.object(monitor.time, "time", return_value=1200), \
             patch.object(monitor, "_send_health") as send:
            health.success()
            self.assertIn("recovered", send.call_args.args[0])
            health.success()
            self.assertEqual(send.call_count, 1)
            for _ in range(3):
                health.failure(RuntimeError("new incident"))
            self.assertEqual(send.call_count, 2)

    def test_backoff_and_server_cooldown_survive_restart(self):
        """Verify backoff and server cooldown survive restart."""
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send_health"):
            expected_delays = [60, 120, 240, 300, 300]
            for delay in expected_delays:
                health.failure(RuntimeError("network failure"))
                self.assertEqual(health.state.next_sweep_at, 1000 + delay)
            health.failure(monitor.MonitorFailure("rate limit", retry_after=600))
        self.assertEqual(monitor.HealthMonitor(dry_run=False).state.next_sweep_at, 1600)

    def test_failed_warning_retries_then_stays_suppressed(self):
        """Verify failed warning retries then stays suppressed."""
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send_health", side_effect=RuntimeError("Discord unavailable")) as send:
            for _ in range(4):
                health.failure(RuntimeError("empty crawl"))
            send.assert_called_once()
        self.assertFalse(monitor.load_health().alerted)
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1100), \
             patch.object(monitor, "_send_health") as send:
            health.failure(RuntimeError("empty crawl"))
            health.failure(RuntimeError("empty crawl"))
            send.assert_called_once()
        self.assertTrue(monitor.load_health().alerted)

    def test_failed_recovery_retries_after_restart_without_another_warning(self):
        """Verify failed recovery retries after restart without another warning."""
        health = monitor.HealthMonitor(dry_run=False)
        health.state = monitor.HealthState(failures=3, alerted=True)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send_health", side_effect=RuntimeError("offline")):
            health.success()
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1100), \
             patch.object(monitor, "_send_health") as send:
            health.success()
            health.success()
            send.assert_called_once()
            self.assertIn("recovered", send.call_args.args[0])
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_dry_run_does_not_send_or_change_incident_file(self):
        """Verify dry run does not send or change incident file."""
        self.path.write_text('{"failures": 9, "alerted": true}')
        before = self.path.read_bytes()
        health = monitor.HealthMonitor(dry_run=True)
        with patch.object(monitor, "_send_health") as send:
            for _ in range(4):
                health.failure(RuntimeError("empty crawl"))
            health.success()
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_loop_respects_persisted_cooldown_and_suppresses_startup_ping(self):
        """Verify loop respects persisted cooldown and suppresses startup ping."""
        monitor.write_json(self.path, {"failures": 3, "alerted": True, "next_sweep_at": 1600,
                                       "last_alert_at": monitor.time.time()})
        with patch.object(sys, "argv", ["monitor", "--loop"]), \
             patch.object(monitor, "STARTUP_PING", True), \
             patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "sweep", side_effect=RuntimeError("still down")) as sweep, \
             patch.object(monitor.time, "sleep", side_effect=StopIteration), \
             patch.object(monitor, "_send_health") as send:
            with self.assertRaises(StopIteration):
                monitor.main()
        sweep.assert_not_called()
        send.assert_not_called()

    def test_reminder_deadline_survives_restarts(self):
        """Keep a persistent outage visible without repeating each failed sweep."""
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send_health") as send:
            health = monitor.HealthMonitor(dry_run=False)
            for _ in range(3):
                health.failure(RuntimeError("HTTP 403"))
            send.assert_called_once()
        with patch.object(monitor.time, "time", return_value=22599), \
             patch.object(monitor, "_send_health") as send:
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            send.assert_not_called()
        with patch.object(monitor.time, "time", return_value=22600), \
             patch.object(monitor, "_send_health") as send:
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            send.assert_called_once()
            self.assertIn("is still failing", send.call_args.args[0])
            self.assertIn("Product alerts may be missing", send.call_args.args[0])
        self.assertEqual(monitor.load_health().last_alert_at, 22600)

    def test_legacy_active_incident_gets_reminder_with_history_age(self):
        """Upgrade an already-suppressed outage without resetting product history."""
        monitor.write_json(self.path, {"failures": 1900, "alerted": True})
        with patch.object(monitor.time, "time", return_value=10000), \
             patch.object(monitor.STATE_FILE.__class__, "stat") as stat, \
             patch.object(monitor, "_send_health") as send:
            stat.return_value.st_mtime = 2800
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            send.assert_called_once()
            self.assertIn("2.0 hours ago", send.call_args.args[0])

    def test_failed_reminder_retries_after_cooldown(self):
        """A failed reminder must not postpone retries for another six hours."""
        monitor.write_json(self.path, {"failures": 20, "alerted": True, "last_alert_at": 1000})
        with patch.object(monitor.time, "time", return_value=22600), \
             patch.object(monitor, "_send_health", side_effect=RuntimeError("offline")) as send:
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            send.assert_called_once()
        self.assertEqual(monitor.load_health().last_alert_at, 1000)
        with patch.object(monitor.time, "time", return_value=22660), \
             patch.object(monitor, "_send_health") as send:
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
            send.assert_called_once()
        self.assertEqual(monitor.load_health().last_alert_at, 22660)

    def test_reminders_can_be_disabled(self):
        """Allow the original one-warning policy through explicit configuration."""
        monitor.write_json(self.path, {"failures": 20, "alerted": True})
        with patch.object(monitor, "HEALTH_REMINDER_SECONDS", 0), \
             patch.object(monitor, "_send_health") as send:
            monitor.HealthMonitor(dry_run=False).failure(RuntimeError("HTTP 403"))
        send.assert_not_called()

    def test_initial_failed_sweep_does_not_announce_online(self):
        """A running process cannot announce successful monitoring before fetching."""
        with patch.object(sys, "argv", ["monitor", "--loop"]), \
             patch.object(monitor, "STARTUP_PING", True), \
             patch.object(monitor, "sweep", side_effect=RuntimeError("HTTP 403")), \
             patch.object(monitor.time, "sleep", side_effect=StopIteration), \
             patch.object(monitor, "_send_health") as send:
            with self.assertRaises(StopIteration):
                monitor.main()
        send.assert_not_called()

    def test_startup_notice_uses_health_route_only(self):
        """Send healthy startup notices privately while preserving product routing."""
        with patch.object(sys, "argv", ["monitor", "--loop"]), \
             patch.object(monitor, "STARTUP_PING", True), \
             patch.object(monitor, "sweep"), \
             patch.object(monitor.TierSchedule, "coverage_complete", return_value=True), \
             patch.object(monitor.time, "sleep", side_effect=StopIteration), \
             patch.object(monitor, "_send_health") as health_send, \
             patch.object(monitor, "_send") as product_send:
            with self.assertRaises(StopIteration):
                monitor.main()
        health_send.assert_called_once()
        self.assertIn("monitor online", health_send.call_args.args[0])
        product_send.assert_not_called()

    def test_dry_run_loop_honors_saved_cooldown_without_updating_it(self):
        """Verify dry run loop honors saved cooldown without updating it."""
        monitor.write_json(self.path, {"failures": 3, "alerted": True, "next_sweep_at": 1600})
        before = self.path.read_bytes()
        with patch.object(sys, "argv", ["monitor", "--loop", "--dry-run"]), \
             patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "sweep", side_effect=RuntimeError("still down")), \
             patch.object(monitor.time, "sleep", side_effect=StopIteration), \
             patch.object(monitor, "_send_health") as send:
            with self.assertRaises(StopIteration):
                monitor.main()
        self.assertEqual(self.path.read_bytes(), before)
        send.assert_not_called()

    def test_invalid_health_file_does_not_stop_monitor(self):
        """Verify invalid health file does not stop monitor."""
        self.path.write_text('{"failures": "corrupted"}')
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_unwritable_health_file_does_not_stop_monitor(self):
        """Verify unwritable health file does not stop monitor."""
        with patch.object(monitor, "write_json", side_effect=PermissionError), \
             patch.object(monitor, "_send_health"):
            health = monitor.HealthMonitor(dry_run=False)
            for _ in range(3):
                health.failure(RuntimeError("crawl failed"))
            self.assertTrue(health.state.alerted)


class TierPollingTests(unittest.TestCase):
    def run_loop(self, responder, *, duration=1300, dry_run=True, saved=None):
        clock = [0.0]
        sent = []
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        history = Path(directory.name) / 'history.json'
        health = Path(directory.name) / 'health.json'
        history.write_text('{"old": {}}')
        if saved:
            monitor.write_json(health, saved)

        def send(adapter, request, **kwargs):
            sent.append((clock[0], request.url))
            return responder(request.url, len(sent))

        with patch.object(sys, 'argv', ['monitor', '--loop', '--duration', str(duration)] +
                          (['--dry-run'] if dry_run else [])), \
             patch.object(monitor, 'STATE_FILE', history), \
             patch.object(monitor, 'HEALTH_FILE', health), \
             patch.object(monitor, 'HOT_CATEGORIES', ['hat', 'eyewear']), \
             patch.object(monitor, 'CATEGORIES', ['hat', 'eyewear', 'socks']), \
             patch.object(monitor, 'CATEGORY_IDS', []), \
             patch.object(monitor, 'BACKOFF_MAX_SECONDS', 3600), \
             patch.object(monitor.time, 'monotonic', side_effect=lambda: clock[0]), \
             patch.object(monitor.time, 'time', side_effect=lambda: 1000 + clock[0]), \
             patch.object(monitor.time, 'sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch.object(monitor.random, 'uniform', return_value=0), \
             patch.object(monitor.requests.adapters.HTTPAdapter, 'send', send), \
             patch.object(monitor, 'log'), \
             patch.object(monitor, 'notify_new') as notify, \
             patch.object(monitor, '_send_health') as health_send:
            monitor.main()
        return sent, history.read_bytes(), health, notify, health_send

    @staticmethod
    def healthy(url, count):
        html = '<sitemapindex/>' if 'sitemap' in url else PRODUCT_HTML
        return response(html=html, url=url)

    def test_expanded_defaults_include_verified_accessories_and_real_sweatpants_route(self):
        with patch.object(monitor.time, 'monotonic', return_value=0):
            schedule = monitor.TierSchedule()
        sweatpants = monitor.CATEGORY_ENDPOINT + '?cgid=SWEATPANTS'
        self.assertIn(sweatpants, schedule.hot)
        self.assertNotIn('/sweatpants', schedule.due)
        self.assertEqual(list(schedule.due).count(sweatpants), 1)
        self.assertLess(schedule.due[sweatpants], monitor.HOT_SECONDS)
        for category in ('belt', 'bag', 'earring', 'scarf', 'pants', 'denim'):
            self.assertIn('/' + category, schedule.hot)
            self.assertLess(schedule.due['/' + category], monitor.HOT_SECONDS)
        self.assertNotIn('/zip-up', schedule.due)
        self.assertNotIn('/accessories', schedule.due)
        self.assertEqual(len(schedule.hot), 13)
        self.assertEqual(len(schedule.due) - len(schedule.hot), 23)
        with patch.object(monitor.time, 'monotonic', return_value=100):
            for path in (sweatpants, '/pants', '/denim'):
                schedule.advance(path)
        for path in (sweatpants, '/pants', '/denim'):
            self.assertEqual(schedule.due[path], 160)

    def test_hot_categories_override_can_choose_search_show_categories(self):
        with patch.object(monitor, 'HOT_CATEGORIES', ['sweatpants', 'belt']):
            schedule = monitor.TierSchedule()
        self.assertEqual(schedule.hot, [monitor.CATEGORY_ENDPOINT + '?cgid=SWEATPANTS', '/belt'])
        self.assertNotIn('/hoodie', schedule.hot)
        self.assertIn('/hoodie', schedule.due)

    def test_hot_and_cold_requests_keep_independent_intervals(self):
        sent, _, _, _, _ = self.run_loop(self.healthy)
        by_path = {}
        for at, url in sent:
            by_path.setdefault(url.removeprefix(monitor.BASE), []).append(at)
        for path, times in by_path.items():
            interval = 60 if path in ('/hat', '/eyewear') else 600
            self.assertGreaterEqual(len(times), 2)
            self.assertTrue(all(abs(b - a - interval) < 0.001 for a, b in zip(times, times[1:])))
        self.assertEqual([by_path[p][0] for p in ('/', '/socks', '/sitemap_index.xml')], [100, 300, 500])

    def test_block_switches_loop_to_one_probe_and_resets_after_success(self):
        def responder(url, count):
            return response(403, url=url) if count <= 2 else self.healthy(url, count)
        sent, history, _, notify, _ = self.run_loop(responder, duration=1000)
        self.assertEqual(sent[:3], [(0, monitor.BASE + '/hat'),
                                   (300, monitor.BASE + '/hat'),
                                   (900, monitor.BASE + '/hat')])
        self.assertGreater(len(sent), 3)  # Normal tier polls resumed after probe.
        self.assertEqual(history, b'{"old": {}}')
        notify.assert_not_called()

    def test_persisted_block_waits_then_only_probes_and_preserves_history(self):
        saved = {'failures': 3, 'alerted': True, 'blocked': True, 'block_failures': 2,
                 'next_sweep_at': 1600, 'last_alert_at': 1000}
        sent, history, health, notify, _ = self.run_loop(
            lambda url, _: response(429, url=url), duration=1000, saved=saved)
        self.assertEqual(sent, [(600, monitor.BASE + '/hat')])
        self.assertEqual(history, b'{"old": {}}')
        self.assertEqual(json.loads(health.read_text()), saved)  # Dry run is read-only.
        notify.assert_not_called()

    def test_reminders_continue_during_long_retry_after_without_requests(self):
        saved = {'failures': 8, 'alerted': True, 'blocked': True, 'block_failures': 7,
                 'next_sweep_at': 100000, 'last_alert_at': 1}
        with patch.object(monitor, 'HEALTH_REMINDER_SECONDS', 100):
            sent, _, _, _, health_send = self.run_loop(self.healthy, duration=61,
                                                       saved=saved, dry_run=False)
        self.assertEqual(sent, [])
        health_send.assert_called_once()
        self.assertIn('is still failing', health_send.call_args.args[0])

    def test_pending_recovery_retries_without_waiting_for_another_full_cycle(self):
        saved = {'failures': 0, 'alerted': True, 'next_notification_at': 1300}
        _, _, _, _, health_send = self.run_loop(self.healthy, duration=400,
                                                saved=saved, dry_run=False)
        health_send.assert_called_once()
        self.assertIn('recovered', health_send.call_args.args[0])

    def test_discovered_categories_and_child_sitemaps_wait_for_cold_slots(self):
        with patch.object(monitor.time, 'monotonic', return_value=0):
            schedule = monitor.TierSchedule()
            session = monitor.requests.Session()
            with patch.object(session, 'get', return_value=response(
                    html='<a href="/brand-new">New</a>')) as get:
                monitor.scheduled_crawl(session, schedule, '/eyewear')
            get.assert_called_once()
            self.assertGreater(schedule.due['/brand-new'], 0)
            index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                     '<sitemap><loc>https://www.chromehearts.com/sitemap_0.xml</loc></sitemap></sitemapindex>')
            with patch.object(session, 'get', return_value=response(html=index)) as get:
                monitor.scheduled_crawl(session, schedule, '/sitemap_index.xml')
            get.assert_called_once()
            self.assertIn('/sitemap_0.xml', schedule.xml)
            self.assertGreater(schedule.due['/sitemap_0.xml'], 0)

    def test_resume_spreads_overdue_cold_pages_instead_of_catching_up(self):
        with patch.object(monitor.time, 'monotonic', return_value=0):
            schedule = monitor.TierSchedule()
        with patch.object(monitor.time, 'monotonic', return_value=10000):
            schedule.resume()
        cold_times = sorted(at for path, at in schedule.due.items() if path not in schedule.hot)
        self.assertGreater(cold_times[0], 10000)
        self.assertGreater(cold_times[-1] - cold_times[0], 500)
        self.assertFalse(schedule.completed)

    def test_empty_scheduled_page_leaves_history_untouched(self):
        with patch.object(monitor, 'load_state') as load, patch.object(monitor, 'save_state') as save:
            monitor.process_crawl(monitor.CrawlResult({}, 0, partial=True), seed=False, dry_run=False)
        load.assert_not_called()
        save.assert_not_called()

    def test_complete_empty_coverage_is_an_outage(self):
        schedule = monitor.TierSchedule()
        schedule.completed = set(schedule.due)
        with self.assertRaisesRegex(monitor.MonitorFailure, 'no products'):
            schedule.coverage_complete()

    def test_failure_does_not_clear_until_complete_coverage(self):
        def responder(url, count):
            return response(403, url=url) if count == 1 else self.healthy(url, count)
        sent, _, health, _, health_send = self.run_loop(responder, duration=400, dry_run=False)
        state = json.loads(health.read_text())
        self.assertFalse(state['blocked'])
        self.assertEqual(state['block_failures'], 0)
        self.assertGreater(state['failures'], 0)
        health_send.assert_not_called()  # Probe success alone cannot announce recovery.


class BlockBackoffTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'health.json'
        for p in (patch.object(monitor, 'HEALTH_FILE', self.path),
                  patch.object(monitor, 'BACKOFF_MAX_SECONDS', 3600),
                  patch.object(monitor.time, 'time', return_value=1000),
                  patch.object(monitor.random, 'uniform', return_value=0),
                  patch.object(monitor, '_send_health')):
            p.start()
            self.addCleanup(p.stop)

    def test_backoff_sequence_cap_and_restart(self):
        health = monitor.HealthMonitor(dry_run=False)
        for delay in (300, 600, 1200, 2400, 3600, 3600, 3600):
            health.failure(monitor.FetchPaused('HTTP 403'))
            health = monitor.HealthMonitor(dry_run=False)
            self.assertTrue(health.state.blocked)
            self.assertEqual(health.state.next_sweep_at, 1000 + delay)

    def test_backoff_jitter_is_bounded_and_still_applies_at_the_cap(self):
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.random, 'uniform', side_effect=lambda low, high: high):
            health.failure(monitor.FetchPaused('HTTP 403'))
            self.assertEqual(health.state.next_sweep_at, 1330)
            health.state.block_failures = 7
            health.failure(monitor.FetchPaused('HTTP 403'))
            self.assertEqual(health.state.next_sweep_at, 4240)

    def test_retry_after_takes_precedence_including_more_than_one_hour(self):
        health = monitor.HealthMonitor(dry_run=False)
        health.failure(monitor.FetchPaused('HTTP 429', retry_after=7200))
        self.assertEqual(health.state.next_sweep_at, 8200)
        health.failure(RuntimeError('probe connection failed'))
        self.assertEqual(health.state.next_sweep_at, 1600)
        self.assertTrue(health.state.blocked)

    def test_successful_probe_resets_backoff_but_keeps_incident(self):
        health = monitor.HealthMonitor(dry_run=False)
        for _ in range(4):
            health.failure(monitor.FetchPaused('HTTP 403'))
        health.probe_success()
        health = monitor.HealthMonitor(dry_run=False)
        self.assertEqual(health.state.block_failures, 0)
        self.assertEqual(health.state.next_sweep_at, 0)
        self.assertTrue(health.state.alerted)
        health.failure(monitor.FetchPaused('HTTP 429'))
        self.assertEqual(health.state.next_sweep_at, 1300)

    def test_probe_never_follows_redirects_retries_or_writes_history(self):
        session = monitor.requests.Session()
        for status in (200, 302, 403, 429, 503):
            with self.subTest(status=status), \
                 patch.object(session, 'get', return_value=response(status, headers={'Location': '/'})) as get, \
                 patch.object(monitor, 'save_state') as save:
                if status in (200, 302):
                    monitor.probe(session, '/hat')
                else:
                    with self.assertRaises(monitor.MonitorFailure):
                        monitor.probe(session, '/hat')
                get.assert_called_once_with(monitor.BASE + '/hat', timeout=monitor.REQUEST_TIMEOUT,
                                            allow_redirects=False)
                save.assert_not_called()

    def test_empty_category_redirect_skips_homepage_but_product_redirect_is_followed(self):
        session = monitor.requests.Session()
        with patch.object(session, 'get', return_value=response(302, headers={'Location': '/'})) as get:
            self.assertEqual(monitor.fetch(session, monitor.BASE + '/hat').status_code, 302)
        get.assert_called_once()
        with patch.object(session, 'get', side_effect=[
                response(302, headers={'Location': '/eyewear/mio-lord/21978827FE50010.html'}),
                response()]) as get:
            self.assertEqual(monitor.fetch(session, monitor.BASE + '/eyewear').status_code, 200)
        self.assertEqual(get.call_count, 2)

    def test_block_on_redirect_target_ends_without_retry(self):
        session = monitor.requests.Session()
        with patch.object(session, 'get', side_effect=[
                response(302, headers={'Location': '/product.html'}), response(403)]) as get:
            with self.assertRaises(monitor.FetchPaused):
                monitor.fetch(session, monitor.BASE + '/eyewear')
        self.assertEqual(get.call_count, 2)


class RequestAccountingTests(unittest.TestCase):
    def test_wire_redirects_and_blocked_responses_are_counted_once(self):
        session = monitor.MonitorSession()
        with patch.object(monitor.time, 'sleep'), \
             patch.object(monitor.requests.adapters.HTTPAdapter, 'send', side_effect=[
                 response(302, headers={'Location': '/target'}, url=monitor.BASE + '/start'),
                 response(403, url=monitor.BASE + '/target')]):
            session.get(monitor.BASE + '/start')
        self.assertEqual(session.accounting.total_requests, 2)
        self.assertEqual(session.accounting.total_blocked, 1)

    def test_failed_connection_is_still_a_sent_attempt(self):
        session = monitor.MonitorSession()
        with patch.object(monitor.requests.adapters.HTTPAdapter, 'send',
                          side_effect=monitor.requests.ConnectionError):
            with self.assertRaises(monitor.requests.ConnectionError):
                session.get(monitor.BASE)
        self.assertEqual(session.accounting.total_requests, 1)

    def test_hourly_log_rolls_over_and_retains_totals(self):
        with patch.object(monitor.time, 'monotonic', return_value=0):
            accounting = monitor.RequestAccounting()
        accounting.requests = accounting.total_requests = 540
        accounting.blocked = accounting.total_blocked = 2
        with patch.object(monitor.time, 'monotonic', return_value=3600), \
             patch.object(monitor, 'log') as log:
            accounting.report()
            accounting.report()
        log.assert_called_once()
        self.assertIn('requests=540 blocked=2 requests_per_minute=9.00', log.call_args.args[0])
        self.assertEqual(accounting.requests, 0)
        self.assertEqual(accounting.total_requests, 540)


if __name__ == "__main__":
    unittest.main()
