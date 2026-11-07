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
    result = monitor.requests.Response()
    result.status_code = status
    result.url = url
    result._content = html.encode()
    result.headers.update(headers or {"Content-Type": "text/html"})
    return result


class ProductDiscoveryTests(unittest.TestCase):
    def test_search_show_category_link_is_discovered(self):
        html = ('<a href="/on/demandware.store/Sites-ChromeHearts-Site/en_US/'
                'Search-Show?cgid=SWEATPANTS">SWEATPANTS</a>')
        self.assertIn(f"{monitor.CATEGORY_ENDPOINT}?cgid=SWEATPANTS",
                      monitor.discover_category_paths(html))

    def test_redirected_product_uses_its_real_url(self):
        product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)["190372BLKXXX01W"]
        self.assertEqual(product.url, PRODUCT_URL)
        self.assertEqual(product.price, "730.00")

    def test_single_segment_product_link_is_recognized(self):
        html = PRODUCT_HTML + '<a href="/black-sweatpants/190372BLKXXX01W.html">View</a>'
        product = monitor.parse_products(html)["190372BLKXXX01W"]
        self.assertEqual(product.url, PRODUCT_URL.split("?")[0])

    def test_crawl_checks_sweatpants_category(self):
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
        index = b'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>https://www.chromehearts.com/sitemap_0.xml</loc></sitemap></sitemapindex>'
        catalog = b'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://www.chromehearts.com/new-category</loc></url><url><loc>https://www.chromehearts.com/shop</loc></url></urlset>'

        class Response:
            def __init__(self, content):
                self.content = content

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response(index if url.endswith('sitemap_index.xml') else catalog)

        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor, "_sitemap_paths", []):
            self.assertEqual(monitor.sitemap_category_paths(Session()), ['/new-category'])

    def test_sitemap_rejects_entity_declarations(self):
        class Response:
            content = b'<!DOCTYPE x [<!ENTITY x "bad">]><sitemapindex><loc>&x;</loc></sitemapindex>'

            def raise_for_status(self):
                pass

        class Session:
            def get(self, url, timeout):
                return Response()

        with patch.object(monitor, "_sitemap_checked_at", 0.0), \
             patch.object(monitor, "_sitemap_paths", ['/known']):
            self.assertEqual(monitor.sitemap_category_paths(Session()), ['/known'])

    def test_dry_run_does_not_mark_product_seen(self):
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
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing.json'
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 0)):
                with self.assertRaises(FileNotFoundError):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertFalse(state.exists())

    def test_empty_crawl_cannot_update_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult({}, 0)):
                with self.assertRaisesRegex(RuntimeError, 'no products'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertEqual(state.read_text(), '{"old": {}}')

    def test_failed_delivery_does_not_mark_product_seen(self):
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
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=monitor.CrawlResult(product, 1)), \
                 patch.object(monitor, "notify_new") as notify:
                with self.assertRaisesRegex(RuntimeError, 'coverage may be incomplete'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            notify.assert_called_once()
            self.assertIn('190372BLKXXX01W', state.read_text())

    def test_discord_http_error_is_a_delivery_failure(self):
        class Response:
            status_code = 429
            text = 'rate limited'

        with patch.dict(notifier.os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.test/webhook'}), \
             patch.object(notifier.requests, 'post', return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "HTTP 429: 'rate limited'"):
                notifier._send_discord('test')

    def test_twilio_http_error_includes_bounded_response(self):
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
        response = notifier.requests.Response()
        response.status_code = 403
        response._content = b'forbidden'
        error = notifier.requests.HTTPError(response=response)
        with patch.dict(notifier.os.environ, {'DISCORD_WEBHOOK_URL': 'https://example.test/webhook'}), \
             patch.object(notifier.requests, 'post', side_effect=error):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403: 'forbidden'"):
                notifier._send_discord('test')

class FetchDiagnosticsTests(unittest.TestCase):
    def test_rate_limit_stops_requests_and_preserves_retry_after(self):
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
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor.requests.Session, "get", return_value=response(403)) as get:
            result = monitor.crawl(monitor.requests.Session())
        self.assertEqual(result.errors, 1)
        self.assertEqual(result.catalog, {})
        self.assertIn("HTTP 403", result.summary)
        get.assert_called_once()

    def test_challenge_with_http_200_is_not_a_successful_empty_page(self):
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(html="<title>Just a moment...</title>")):
            with self.assertRaisesRegex(monitor.FetchPaused, "challenge=True"):
                monitor.fetch(session, monitor.BASE)
        self.assertFalse(monitor.is_challenge(response(html="<!-- Needed for Recaptcha -->")))
        self.assertTrue(monitor.is_challenge(response(headers={"cf-mitigated": "challenge"})))

    def test_retry_after_accepts_http_dates_and_bounds_invalid_values(self):
        with patch.object(monitor.time, "time", return_value=1000):
            self.assertEqual(monitor.retry_after_seconds(formatdate(1120, usegmt=True)), 120)
        self.assertEqual(monitor.retry_after_seconds("not a date"), 0)
        self.assertEqual(monitor.retry_after_seconds("-5"), 0)
        self.assertEqual(monitor.retry_after_seconds("99999"), 3600)

    def test_server_errors_retry_without_sleeping_after_final_attempt(self):
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(503)) as get, \
             patch.object(monitor.time, "sleep") as sleep, \
             patch.object(monitor.random, "uniform", return_value=0):
            self.assertIsNone(monitor.fetch(session, monitor.BASE))
        self.assertEqual(get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.5, 3])

    def test_server_retry_after_defers_next_sweep_without_more_requests(self):
        session = monitor.requests.Session()
        with patch.object(session, "get", return_value=response(503, headers={"Retry-After": "600"})) as get:
            with self.assertRaises(monitor.FetchPaused) as caught:
                monitor.fetch(session, monitor.BASE)
        self.assertEqual(caught.exception.retry_after, 600)
        get.assert_called_once()

    def test_network_outage_stops_after_three_failed_pages(self):
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor, "fetch", return_value=None) as fetch, \
             patch.object(monitor.time, "sleep"):
            result = monitor.crawl(object())
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(result.errors, 3)
        self.assertIn("stopped this crawl early", result.summary)

    def test_empty_http_200_pages_include_diagnostic_samples(self):
        with patch.object(monitor, "sitemap_category_paths", return_value=[]), \
             patch.object(monitor, "CATEGORIES", []), patch.object(monitor, "CATEGORY_IDS", []), \
             patch.object(monitor, "fetch", return_value=response(html="<html>Welcome</html>")), \
             patch.object(monitor.time, "sleep"):
            result = monitor.crawl(object())
        self.assertIn("HTTP counts={200: 1}", result.summary)
        self.assertIn("empty page samples", result.summary)
        with patch.object(monitor, "crawl", return_value=result):
            with self.assertRaisesRegex(monitor.MonitorFailure, "empty page samples"):
                monitor.sweep(object(), seed=False, dry_run=False)

    def test_products_before_blocking_are_still_delivered_and_recorded(self):
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
            notify.assert_called_once()
            self.assertIn("190372BLKXXX01W", json.loads(state.read_text()))


class HealthIncidentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "seen.health.json"
        for patcher in (patch.object(monitor, "HEALTH_FILE", self.path),
                        patch.object(monitor, "HEALTH_FAILURE_THRESHOLD", 3),
                        patch.object(monitor, "POLL_SECONDS", 30),
                        patch.object(monitor, "BACKOFF_MAX_SECONDS", 300)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_transient_failures_clear_silently(self):
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor, "_send") as send:
            health.failure(RuntimeError("temporary"))
            health.failure(RuntimeError("temporary"))
            health.success()
        send.assert_not_called()
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_one_warning_survives_restarts_and_new_incident_alerts_after_recovery(self):
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send") as send:
            for _ in range(3):
                health.failure(RuntimeError("empty crawl"))
            self.assertEqual(send.call_count, 1)
            health = monitor.HealthMonitor(dry_run=False)
            health.failure(RuntimeError("still empty"))
            self.assertEqual(send.call_count, 1)
        with patch.object(monitor.time, "time", return_value=1200), \
             patch.object(monitor, "_send") as send:
            health.success()
            self.assertIn("recovered", send.call_args.args[0])
            health.success()
            self.assertEqual(send.call_count, 1)
            for _ in range(3):
                health.failure(RuntimeError("new incident"))
            self.assertEqual(send.call_count, 2)

    def test_backoff_and_server_cooldown_survive_restart(self):
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send"):
            expected_delays = [60, 120, 240, 300, 300]
            for delay in expected_delays:
                health.failure(RuntimeError("network failure"))
                self.assertEqual(health.state.next_sweep_at, 1000 + delay)
            health.failure(monitor.MonitorFailure("rate limit", retry_after=600))
        self.assertEqual(monitor.HealthMonitor(dry_run=False).state.next_sweep_at, 1600)

    def test_failed_warning_retries_then_stays_suppressed(self):
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send", side_effect=RuntimeError("Discord unavailable")) as send:
            for _ in range(4):
                health.failure(RuntimeError("empty crawl"))
            send.assert_called_once()
        self.assertFalse(monitor.load_health().alerted)
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1100), \
             patch.object(monitor, "_send") as send:
            health.failure(RuntimeError("empty crawl"))
            health.failure(RuntimeError("empty crawl"))
            send.assert_called_once()
        self.assertTrue(monitor.load_health().alerted)

    def test_failed_recovery_retries_after_restart_without_another_warning(self):
        health = monitor.HealthMonitor(dry_run=False)
        health.state = monitor.HealthState(failures=3, alerted=True)
        with patch.object(monitor.time, "time", return_value=1000), \
             patch.object(monitor, "_send", side_effect=RuntimeError("offline")):
            health.success()
        health = monitor.HealthMonitor(dry_run=False)
        with patch.object(monitor.time, "time", return_value=1100), \
             patch.object(monitor, "_send") as send:
            health.success()
            health.success()
            send.assert_called_once()
            self.assertIn("recovered", send.call_args.args[0])
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_dry_run_does_not_send_or_change_incident_file(self):
        self.path.write_text('{"failures": 9, "alerted": true}')
        before = self.path.read_bytes()
        health = monitor.HealthMonitor(dry_run=True)
        with patch.object(monitor, "_send") as send:
            for _ in range(4):
                health.failure(RuntimeError("empty crawl"))
            health.success()
        send.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)

    def test_loop_respects_persisted_cooldown_and_suppresses_startup_ping(self):
        monitor.write_json(self.path, {"failures": 3, "alerted": True, "next_sweep_at": 1600})
        with patch.object(sys, "argv", ["monitor", "--loop"]), \
             patch.object(monitor, "STARTUP_PING", True), \
             patch.object(monitor, "wait_until") as wait, \
             patch.object(monitor, "sweep", side_effect=RuntimeError("still down")) as sweep, \
             patch.object(monitor.time, "sleep", side_effect=StopIteration), \
             patch.object(monitor, "_send") as send:
            with self.assertRaises(StopIteration):
                monitor.main()
        wait.assert_called_once_with(1600)
        sweep.assert_called_once()
        send.assert_not_called()

    def test_invalid_health_file_does_not_stop_monitor(self):
        self.path.write_text('{"failures": "corrupted"}')
        self.assertEqual(monitor.load_health(), monitor.HealthState())

    def test_unwritable_health_file_does_not_stop_monitor(self):
        with patch.object(monitor, "write_json", side_effect=PermissionError), \
             patch.object(monitor, "_send"):
            health = monitor.HealthMonitor(dry_run=False)
            for _ in range(3):
                health.failure(RuntimeError("crawl failed"))
            self.assertTrue(health.state.alerted)


if __name__ == "__main__":
    unittest.main()
