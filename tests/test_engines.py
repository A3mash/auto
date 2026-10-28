"""اختبارات بلا شبكة لمنطق المحرّكَين. أشكال الردود هنا مُحاكاة لا مأخوذة من متجر حقيقي."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
os.environ.setdefault('SUPABASE_URL', 'http://localhost')
os.environ.setdefault('SUPABASE_KEY', 'x')

import salla  # noqa: E402
from common import clean_sku, filter_product_links  # noqa: E402
from harvester import dedupe  # noqa: E402


class CommonTests(unittest.TestCase):
    def test_clean_sku_rejects_junk(self):
        self.assertEqual(clean_sku('_', 'fb'), 'fb')
        self.assertEqual(clean_sku('  ', 'fb'), 'fb')
        self.assertEqual(clean_sku('0', 'fb'), 'fb')
        self.assertEqual(clean_sku('123', 'fb'), '123')  # قصير لكنه مفتاح حقيقي في متجره
        self.assertEqual(clean_sku('11289-62020', 'fb'), '11289-62020')
        self.assertEqual(clean_sku('48510', 'fb'), '48510')

    def test_filter_product_links(self):
        links = ['https://s.com', 'https://s.com/ابواب/c123456', 'https://s.com/باب-شاص/p98765',
                 'https://s.com/pages/about', 'https://cdn.salla.sa/x.jpg', 'https://s.com/products/abc']
        self.assertEqual(filter_product_links(links, 'https://s.com/'),
                         ['https://s.com/products/abc', 'https://s.com/باب-شاص/p98765'])


class SallaParsingTests(unittest.TestCase):
    def test_product_id_from_url(self):
        self.assertEqual(salla.product_id_from_url('https://alsdlan.com/شمعة/p1234567'), '1234567')
        self.assertIsNone(salla.product_id_from_url('https://alsdlan.com/قسم/c1234567'))

    def test_store_id_candidates(self):
        html = '<script>salla.init({"store":{"id":1234567,"name":"x"}});</script>'
        self.assertEqual(salla.find_store_id_candidates(html), ['1234567'])

    def test_read_quantity(self):
        self.assertEqual(salla.read_quantity({'quantity': 4}), (4, 'exact'))
        self.assertEqual(salla.read_quantity({'quantity': 4, 'status': 'out'}), (0, 'exact'))
        self.assertEqual(salla.read_quantity({'status': 'out'}), (0, 'availability'))
        self.assertEqual(salla.read_quantity({'is_available': True}), (1, 'availability'))
        self.assertEqual(salla.read_quantity({'quantity': 9, 'unlimited_quantity': True}), (1, 'availability'))
        self.assertEqual(salla.read_quantity({}), (None, None))

    def test_minus_one_means_hidden_not_zero(self):
        # الشكل الحقيقي من متجر السدلان (2026-10-28): الكمية مخفية = -1
        real = {'quantity': -1, 'status': 'sale', 'is_available': True, 'is_out_of_stock': False,
                'unlimited_quantity': False, 'max_quantity': 7}
        self.assertEqual(salla.read_quantity(real), (1, 'availability'))
        self.assertEqual(salla.read_quantity(real, use_max=True), (7, 'max_quantity'))
        # بروف كار: بلا حدود، سقف 500 ثابت → ليس مخزوناً
        unlimited = {**real, 'unlimited_quantity': True, 'max_quantity': 500}
        self.assertEqual(salla.read_quantity(unlimited, use_max=True), (1, 'availability'))

    def test_max_quantity_informative(self):
        self.assertTrue(salla.max_quantity_informative([{'max_quantity': m} for m in (1, 1, 2, 7, 3)]))
        self.assertFalse(salla.max_quantity_informative([{'max_quantity': 5}] * 10))
        self.assertFalse(salla.max_quantity_informative(
            [{'max_quantity': m, 'unlimited_quantity': True} for m in (1, 2, 3, 4, 5)]))

    def test_product_to_rows_single(self):
        p = {'id': 55, 'name': 'فانوس', 'sku': '_', 'price': {'amount': 200}, 'sale_price': 150,
             'is_on_sale': True, 'quantity': 3, 'image': {'url': 'i.jpg'}, 'url': 'u',
             'categories': [{'name': 'إنارة'}], 'sold_quantity': 12}
        [row] = salla.product_to_rows(p, 5)
        self.assertEqual(row['sku'], '55')
        self.assertEqual((row['price'], row['quantity'], row['quantity_source']), (150, 3, 'exact'))
        self.assertEqual((row['detected_category'], row['sold_count']), ('إنارة', 12))

    def test_product_to_rows_variants(self):
        p = {'id': 9, 'name': 'كفر', 'price': 100, 'skus': [
            {'id': 1, 'sku': 'K-RED', 'price': 100, 'quantity': 2, 'name': 'أحمر'},
            {'id': 2, 'sku': '', 'price': 120, 'quantity': 0, 'name': 'أسود'}]}
        rows = salla.product_to_rows(p, 5)
        self.assertEqual([r['sku'] for r in rows], ['K-RED', '9-2'])
        self.assertEqual([r['quantity'] for r in rows], [2, 0])
        self.assertEqual(rows[1]['product_name'], 'كفر - أسود')

    def test_dedupe_keeps_distinct_products_sharing_sku(self):
        rows = [{'store_id': 5, 'sku': 'A1', 'platform_product_id': '1'},
                {'store_id': 5, 'sku': 'A1', 'platform_product_id': '2'}]
        self.assertEqual(sorted(r['sku'] for r in dedupe(rows)), ['A1', 'A1-2'])


class SallaHarvestFlowTests(unittest.TestCase):
    """التنسيق: القائمة + الخريطة + التفاصيل + اكتشاف إخفاء الكمية."""

    def make_store(self, listing, details, sitemap):
        store = salla.SallaStore(5, 'https://shop.test/sitemap-2.xml', ([], [], []), log=lambda *_: None)
        pages = [listing[i:i + salla.PER_PAGE] for i in range(0, len(listing), salla.PER_PAGE)]

        def fake_api(path, params=None, identifier=None, absolute=False):
            if path == 'products':
                page = (params or {}).get('page', 1)
                return {'data': pages[page - 1] if page <= len(pages) else []}
            pid = path.split('/')[1]
            return {'data': details[pid]} if pid in details else None

        store.api = fake_api
        store.discover = lambda: True
        store.sitemap_products = lambda: sitemap
        store.html_product = mock.Mock(return_value=None)
        return store

    def setUp(self):
        sleep = mock.patch('salla.time.sleep')
        sleep.start()
        self.addCleanup(sleep.stop)

    def test_paginates_and_fills_sitemap_gaps(self):
        listing = [{'id': i, 'name': f'n{i}', 'price': 10, 'quantity': 5} for i in range(1, 121)]
        details = {'999': {'id': 999, 'name': 'من الخريطة فقط', 'price': 30, 'quantity': 1}}
        rows = self.make_store(listing, details, {'999': 'https://shop.test/x/p999'}).harvest()
        self.assertEqual(len(rows), 121)
        self.assertTrue(all(r['quantity_source'] == 'exact' for r in rows))

    def test_stops_detail_calls_when_store_hides_quantity(self):
        listing = [{'id': i, 'name': f'n{i}', 'price': 10, 'is_available': True} for i in range(1, 201)]
        details = {str(i): {'id': i, 'name': f'n{i}', 'price': 10, 'is_available': True} for i in range(1, 201)}
        store = self.make_store(listing, details, {})
        rows = store.harvest()
        self.assertEqual(len(rows), 200)
        self.assertLess(store.stats['details'], 60)  # لم يطلب 200 تفصيلاً
        self.assertTrue(all(r['quantity'] == 1 and r['quantity_source'] == 'availability' for r in rows))

    def test_hidden_quantity_with_varying_max_uses_it_as_stock(self):
        listing = [{'id': i, 'name': f'n{i}', 'price': 10, 'quantity': -1} for i in range(1, 41)]
        details = {str(i): {'id': i, 'name': f'n{i}', 'price': 10, 'quantity': -1, 'is_available': True,
                            'unlimited_quantity': False, 'max_quantity': (i % 4) + 1} for i in range(1, 41)}
        store = self.make_store(listing, details, {})
        rows = store.harvest()
        self.assertEqual(store.stats['details'], 40)  # التفاصيل تضيف السقف فلا توقّف
        self.assertTrue(all(r['quantity_source'] == 'max_quantity' for r in rows))
        self.assertEqual(sorted({r['quantity'] for r in rows}), [1, 2, 3, 4])

    def test_unmatched_sitemap_urls_are_capped_not_scraped(self):
        listing = [{'id': i, 'name': f'n{i}', 'price': 10, 'quantity': 2, 'url': f'https://shop.test/x/{i}'}
                   for i in range(1, 11)]
        sitemap = {f'u:/y/{i}': f'https://shop.test/y/{i}' for i in range(100)}
        store = self.make_store(listing, {}, sitemap)
        self.assertEqual(len(store.harvest()), 10)
        store.html_product.assert_not_called()

    def test_html_fallback_when_api_unavailable(self):
        store = self.make_store([], {}, {'7': 'https://shop.test/x/p7'})
        store.discover = lambda: False
        store.html_product = mock.Mock(return_value={'id': '7', 'name': 'x', 'price': '50', 'is_available': False})
        [row] = store.harvest()
        self.assertEqual((row['price'], row['quantity']), (50.0, 0))


if __name__ == '__main__':
    unittest.main()
