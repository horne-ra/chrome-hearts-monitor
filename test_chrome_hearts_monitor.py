import unittest
import tempfile
import sys
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
            catalog, errors = monitor.crawl(object())
        self.assertEqual(catalog["190372BLKXXX01W"].url, PRODUCT_URL)
        self.assertEqual(errors, 1)

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
                 patch.object(monitor, "crawl", return_value=(product, 0)), \
                 patch.object(monitor, "notify_new") as notify:
                monitor.sweep(object(), seed=False, dry_run=True)
            self.assertEqual(state.read_text(), '{"old": {}}')
            notify.assert_not_called()

    def test_missing_state_cannot_silently_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'missing.json'
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=(product, 0)):
                with self.assertRaises(FileNotFoundError):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertFalse(state.exists())

    def test_empty_crawl_cannot_update_state(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=({}, 0)):
                with self.assertRaisesRegex(RuntimeError, 'no products'):
                    monitor.sweep(object(), seed=False, dry_run=False)
            self.assertEqual(state.read_text(), '{"old": {}}')

    def test_failed_delivery_does_not_mark_product_seen(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'seen.json'
            state.write_text('{"old": {}}')
            product = monitor.parse_products(PRODUCT_HTML, PRODUCT_URL)
            with patch.object(monitor, "STATE_FILE", state), \
                 patch.object(monitor, "crawl", return_value=(product, 0)), \
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
                 patch.object(monitor, "crawl", return_value=(product, 1)), \
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

    def test_failed_health_alert_retries_then_throttles_after_success(self):
        with patch.object(sys, 'argv', ['monitor', '--loop']), \
             patch.object(monitor, 'STARTUP_PING', False), \
             patch.object(monitor.requests, 'Session'), \
             patch.object(monitor, 'sweep', side_effect=RuntimeError('crawl failed')), \
             patch.object(monitor, '_send', side_effect=[RuntimeError('send failed'), None]) as send, \
             patch.object(monitor.time, 'monotonic', return_value=10.0), \
             patch.object(monitor.time, 'sleep', side_effect=[None, None, StopIteration]):
            with self.assertRaises(StopIteration):
                monitor.main()
        self.assertEqual(send.call_count, 2)


if __name__ == "__main__":
    unittest.main()
