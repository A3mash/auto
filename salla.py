"""محرّك Salla الجديد: واجهة الواجهة الأمامية (Storefront API) بدل كشط صفحة HTML لكل منتج.

المشكلة في المحرّك القديم:
  1. يقرأ ملف خريطة واحداً (sitemap-2.xml) فيفوته أي منتج في الملفات الأخرى.
  2. يفتح صفحة HTML كاملة لكل منتج ويقرأ dataLayer الذي لا يحمل المخزون.
  3. يكتب quantity = 10 ثابتة، فلا يتغيّر المخزون أبداً → صفر مبيعات وصفر نفاد (D4).

الخوارزمية الجديدة (ثلاث طبقات، كل طبقة تسدّ ثغرة التي قبلها):
  أ. اكتشاف معرّف المتجر (Store-Identifier) من الصفحة الرئيسية، ثم التحقق منه بطلب API واحد.
  ب. الجرد الشامل: ترقيم صفحات products?source=latest (عشرات المنتجات في الطلب الواحد)
     + اتحاده مع كل معرّفات /p{id} في فهرس الخرائط الكامل → لا يسقط منتج لأن أحد المصدرين ناقص.
  ج. الإثراء التكيّفي: المنتج الذي لا تحمل قائمته الكمية يُطلب تفصيله (products/{id}/details).
     إن أثبتت أول عيّنة أن المتجر يُخفي الكمية، نتوقّف عن طلب التفاصيل ونعتمد التوفّر فقط.
  د. الاحتياط: إن تعذّر الـAPI كلياً، نقرأ صفحة HTML لكل رابط (JSON-LD + dataLayer + og)
     ونأخذ منها السعر والتوفّر الحقيقي بدل الرقم 10.

مصدر الكمية يُوسَم في quantity_source:
  exact         الكمية الحقيقية من المتجر.
  max_quantity  المتجر يُخفي الكمية (سلة تُعيد -1) لكن سقف السلة يتغيّر بين المنتجات، فهو
                min(المخزون، حدّ الطلب): تقدير يهبط مع كل بيع ما دام المخزون تحت حدّ الطلب.
  availability  لا كمية ولا سقف مفيد: 1 = متوفّر، 0 = نافد. البيع يُرصَد عند النفاد فقط (حدّ أدنى).
"""
import concurrent.futures
import json
import random
import re
import time
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from common import (base_url_of, clean_sku, crawl_sitemap, extract_json_object, filter_product_links,
                    polite_get, random_device, to_int, to_number, validate_product)

API_BASE = 'https://api.salla.dev/store/v1/'
PER_PAGE = 50
MAX_LIST_PAGES = 400
DETAIL_WORKERS = 4
# بعد هذا العدد من طلبات التفاصيل بلا كمية واحدة، نحكم بأن المتجر يُخفي الكمية
HIDDEN_QTY_SAMPLE = 15

_PRODUCT_ID_IN_URL = re.compile(r'/p(\d{3,})/?(?:[?#].*)?$')
_STORE_ID_PATTERNS = [
    re.compile(r'Store-Identifier["\']?\s*[:=]\s*["\']?(\d{4,})', re.I),
    re.compile(r'"store"\s*:\s*\{[^{}]{0,400}?"id"\s*:\s*"?(\d{4,})'),
    re.compile(r'store[_-]?id["\']?\s*[:=]\s*["\']?(\d{4,})', re.I),
    re.compile(r'"merchant"\s*:\s*\{[^{}]{0,400}?"id"\s*:\s*"?(\d{4,})'),
    re.compile(r'storeId["\']?\s*[:=]\s*["\']?(\d{4,})'),
]
_QTY_KEYS = ('quantity', 'stock_quantity', 'available_quantity', 'stock')
_SOLD_KEYS = ('sold_quantity', 'sales_count', 'sold_count')


def product_id_from_url(url):
    m = _PRODUCT_ID_IN_URL.search(url or '')
    return m.group(1) if m else None


def normalize_url(url):
    """رابط بلا نطاق ولا بادئة لغة ولا ترميز، لمطابقة روابط الخريطة بروابط الـAPI."""
    path = unquote(urlparse(url or '').path).lower().rstrip('/')
    return re.sub(r'^/(ar|en)(?=/|$)', '', path)


def find_store_id_candidates(html):
    seen = []
    for pat in _STORE_ID_PATTERNS:
        for m in pat.finditer(html or ''):
            if m.group(1) not in seen:
                seen.append(m.group(1))
    return seen


# ==========================================
# تحويل منتج من الـAPI إلى صفوف live_inventory
# ==========================================
def _image(p):
    img = p.get('image') or p.get('thumbnail') or p.get('main_image')
    if isinstance(img, dict):
        return img.get('url') or ''
    if isinstance(img, str):
        return img
    imgs = p.get('images')
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        return first.get('url', '') if isinstance(first, dict) else str(first)
    return ''


def _categories(p):
    """None = لا نعرف تصنيف المنتج (القائمة لا تحمله)؛ [] = نعرف أنه بلا تصنيف."""
    out, known = [], False
    for key in ('categories', 'category'):
        val = p.get(key)
        if val is None:
            continue
        known = True
        for c in (val if isinstance(val, list) else [val]):
            name = c.get('name') if isinstance(c, dict) else c
            if isinstance(name, str) and name.strip():
                out.append(name.strip())
    return out if known else None


def _price(p):
    sale = to_number(p.get('sale_price'))
    if sale and (p.get('is_on_sale') or sale < (to_number(p.get('regular_price')) or float('inf'))):
        return sale
    return to_number(p.get('price')) or to_number(p.get('regular_price')) or sale or 0.0


def _is_out(p):
    status = str(p.get('status') or '').lower()
    return (p.get('is_available') is False or p.get('is_out_of_stock') is True
            or status in ('out', 'out-of-stock', 'out_of_stock'))


def _unlimited(p):
    return bool(p.get('unlimited_quantity') or p.get('is_infinite_quantity') or p.get('with_unlimited_quantity'))


def limited_max(p):
    """سقف السلة (max_quantity) لمنتج محدود المخزون. سلة لا تسمح بإضافة أكثر من المتبقّي،
    فحين يُخفي المتجر الكمية يبقى هذا السقف أثراً لها: min(المخزون، حدّ الطلب الواحد)."""
    if _unlimited(p):
        return None
    m = to_int(p.get('max_quantity'))
    return m if m and m > 0 else None


def max_quantity_informative(products):
    """هل يتتبّع max_quantity المخزون فعلاً؟ إن كان ثابتاً لكل المنتجات فهو حدّ طلب لا مخزون."""
    values = [m for m in (limited_max(p) for p in products) if m]
    return len(values) >= 5 and len(set(values)) >= 3


def read_quantity(p, use_max=False):
    """يُعيد (الكمية، المصدر) أو (None, None) إن لم يحمل الكائن أي معلومة مخزون.

    سلة تُعيد quantity = -1 حين يُخفي التاجر الكمية؛ هذا «مجهول» لا «صفر».
    """
    exact = None
    if not _unlimited(p) and not (p.get('is_hidden_quantity') or p.get('is_hidded_quantity')):
        for key in _QTY_KEYS:
            q = to_int(p.get(key))
            if q is not None and q >= 0:
                exact = q
                break
    estimated = limited_max(p) if use_max and exact is None else None
    if _is_out(p):
        return 0, 'exact' if exact is not None else ('max_quantity' if use_max else 'availability')
    if exact is not None:
        return exact, 'exact'
    if estimated:
        return estimated, 'max_quantity'
    if p.get('is_available') is True or _unlimited(p) or str(p.get('status') or '').lower() == 'sale':
        return 1, 'availability'
    return None, None


def read_sold(p):
    for key in _SOLD_KEYS:
        n = to_int(p.get(key))
        if n is not None:
            return n
    return None


def product_to_rows(p, store_id, fallback_url=None, use_max=False):
    pid = str(p.get('id') or '')
    if not pid:
        return []
    name = (p.get('name') or '').strip()
    qty, source = read_quantity(p, use_max)
    base = {
        'store_id': store_id,
        'product_name': name,
        'price': _price(p),
        'quantity': qty if qty is not None else 1,
        'quantity_source': source or 'availability',
        'product_url': p.get('url') or fallback_url or '',
        'main_image': _image(p),
        'detected_category': ' / '.join(_categories(p) or []) or 'غير محدد',
        'platform_product_id': pid,
        'sold_count': read_sold(p),
    }

    # المتغيّرات (مقاسات/ألوان): صف لكل متغيّر إن كان لكل منها سعر أو كمية خاصة
    variants = [v for v in (p.get('skus') or []) if isinstance(v, dict) and v.get('id')]
    if len(variants) >= 2:
        rows = []
        for v in variants:
            vq, vs = read_quantity(v, use_max)
            label = v.get('name') or ' / '.join(
                str(o.get('value') or o.get('name')) for o in (v.get('options') or v.get('related_options') or [])
                if isinstance(o, dict)) or str(v['id'])
            rows.append({**base,
                         'sku': clean_sku(v.get('sku'), f"{pid}-{v['id']}"),
                         'product_name': f"{name} - {label}" if label and label not in name else name,
                         'price': _price(v) or base['price'],
                         'quantity': vq if vq is not None else base['quantity'],
                         'quantity_source': vs or base['quantity_source']})
        return rows
    # بلا SKU تاجر: معرّف المنتج نفسه، كما كان المحرّك القديم يكتبه (dataLayer id)، فيبقى التاريخ موصولاً
    return [{**base, 'sku': clean_sku(p.get('sku'), pid)}]


# ==========================================
# العميل
# ==========================================
class SallaStore:
    def __init__(self, store_id, sitemap_url, rules, log=print):
        self.store_id = store_id
        self.sitemap_url = sitemap_url or ''
        self.base = base_url_of(self.sitemap_url)
        self.rules = rules
        self.log = log
        self.identifier = None
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': random_device().headers.get('User-Agent', 'Mozilla/5.0'),
            'Accept': 'application/json',
            'Accept-Language': 'ar',
            'currency': 'SAR',
            'Origin': self.base,
            'Referer': self.base + '/',
        })
        self.stats = {'listed': 0, 'sitemap_ids': 0, 'details': 0, 'html': 0,
                      'exact': 0, 'max_quantity': 0, 'availability': 0, 'filtered_out': 0, 'unknown_category': 0}

    # ---------- الـAPI ----------
    def api(self, path, params=None, identifier=None, absolute=False):
        url = path if absolute else API_BASE + path.lstrip('/')
        headers = {'Store-Identifier': str(identifier or self.identifier)}
        for attempt in range(3):
            try:
                r = self.session.get(url, params=params, headers=headers, timeout=20)
            except requests.RequestException:
                time.sleep(2 ** attempt * 2)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(float(r.headers.get('Retry-After') or 0) or 2 ** attempt * 3, 60))
                continue
            if r.status_code != 200:
                return None
            try:
                return r.json()
            except ValueError:
                return None
        return None

    def discover(self):
        """أ. اكتشاف Store-Identifier والتحقق منه."""
        candidates = []
        for page in (self.base + '/', self.base + '/products'):
            r = polite_get(page)
            if r is not None and r.status_code == 200:
                candidates += [c for c in find_store_id_candidates(r.text) if c not in candidates]
            if candidates:
                break
        for cid in candidates[:6]:
            data = self.api('products', {'source': 'latest', 'per_page': 1}, identifier=cid)
            if isinstance(data, dict) and isinstance(data.get('data'), list):
                self.identifier = cid
                self.log(f"   🔑 معرّف متجر سلة: {cid}")
                return True
        self.log(f"   ⚠️ تعذّر التحقق من معرّف المتجر (المرشّحون: {candidates[:6]}) → وضع HTML الاحتياطي")
        return False

    def list_all(self):
        """ب-1. ترقيم صفحات القائمة. يتبع cursor.next إن وُجد، وإلا رقم الصفحة."""
        products, page, next_url = {}, 1, None
        while page <= MAX_LIST_PAGES:
            if next_url:
                data = self.api(next_url, absolute=True)
            else:
                data = self.api('products', {'source': 'latest', 'per_page': PER_PAGE, 'page': page})
            items = data.get('data') if isinstance(data, dict) else None
            if not items:
                break
            new = 0
            for p in items:
                if isinstance(p, dict) and p.get('id') is not None and str(p['id']) not in products:
                    products[str(p['id'])] = p
                    new += 1
            if new == 0:  # الخادم يُعيد نفس الصفحة → انتهى الترقيم
                break
            cursor = data.get('cursor') or (data.get('pagination') or {}).get('links') or {}
            next_url = cursor.get('next') if isinstance(cursor, dict) else None
            if not next_url and len(items) < PER_PAGE:
                break
            page += 1
            time.sleep(random.uniform(0.4, 1.0))
        self.stats['listed'] = len(products)
        return products

    def sitemap_products(self):
        """ب-2. فهرس الخرائط الكامل (لا ملف واحد) → {مفتاح: رابط}.

        المفتاح معرّف المنتج الرقمي إن كان في الرابط (/p123)، وإلا الرابط المُطبَّع
        (بعض متاجر سلة تستخدم روابط مختصرة بلا معرّف رقمي).
        """
        pages, _ = crawl_sitemap(self.base + '/sitemap.xml', log=self.log)
        if not pages and self.sitemap_url:
            pages, _ = crawl_sitemap(self.sitemap_url.replace('.com//', '.com/'), log=self.log)
        found = {}
        for url in filter_product_links(pages, self.base):
            found.setdefault(product_id_from_url(url) or 'u:' + normalize_url(url), url)
        self.stats['sitemap_ids'] = len(found)
        return found

    def details(self, pid):
        time.sleep(random.uniform(0.3, 0.9))
        data = self.api(f'products/{pid}/details')
        p = data.get('data') if isinstance(data, dict) else None
        self.stats['details'] += 1
        return p if isinstance(p, dict) else None

    # ---------- الاحتياط: HTML ----------
    def html_product(self, url):
        """د. صفحة المنتج: JSON-LD (السعر + التوفّر) ثم dataLayer ثم og."""
        time.sleep(random.uniform(1.5, 3.5))
        r = polite_get(url, timeout=15)
        self.stats['html'] += 1
        if r is None or r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, 'html.parser')
        p = {'id': product_id_from_url(url) or url.rstrip('/').split('/')[-1], 'url': url}

        for tag in soup.find_all('script', type='application/ld+json'):
            try:
                ld = json.loads(tag.string or '')
            except ValueError:
                continue
            for node in (ld if isinstance(ld, list) else ld.get('@graph', [ld]) if isinstance(ld, dict) else []):
                if isinstance(node, dict) and node.get('@type') == 'Product':
                    p['name'] = node.get('name')
                    p['sku'] = node.get('sku') or node.get('mpn')
                    img = node.get('image')
                    p['image'] = img[0] if isinstance(img, list) and img else img
                    offers = node.get('offers') or {}
                    offers = offers[0] if isinstance(offers, list) and offers else offers
                    if isinstance(offers, dict):
                        p['price'] = offers.get('price') or offers.get('lowPrice')
                        avail = str(offers.get('availability') or '')
                        if avail:
                            p['is_available'] = 'InStock' in avail or 'PreOrder' in avail
                        if offers.get('inventoryLevel') is not None:
                            lvl = offers['inventoryLevel']
                            p['quantity'] = lvl.get('value') if isinstance(lvl, dict) else lvl

        for script in soup.find_all('script'):
            s = script.string or ''
            if 'dataLayer' in s and 'ecommerce' in s:
                data = extract_json_object(s, 'ecommerce')
                try:
                    prod = data['detail']['products'][0]
                except (TypeError, KeyError, IndexError):
                    continue
                p.setdefault('name', prod.get('name'))
                p['price'] = p.get('price') or prod.get('price')
                p['categories'] = [c.get('name') if isinstance(c, dict) else c
                                   for c in prod.get('categories', []) if c]
                break

        if not p.get('name'):
            og = soup.find('meta', property='og:title')
            h1 = soup.find('h1')
            p['name'] = og['content'] if og and og.has_attr('content') else (h1.text.strip() if h1 else '')
        if not p.get('price'):
            meta = soup.find('meta', property='product:price:amount')
            p['price'] = meta['content'] if meta and meta.has_attr('content') else None
        if 'is_available' not in p:
            meta = soup.find('meta', property='product:availability')
            if meta and meta.has_attr('content'):
                p['is_available'] = 'out' not in meta['content'].lower()
        if not p.get('image'):
            og = soup.find('meta', property='og:image')
            p['image'] = og['content'] if og and og.has_attr('content') else ''
        if not p.get('categories'):
            crumbs = [b.text.strip() for b in soup.select('.breadcrumb-item, nav[aria-label*=breadcrumb] li')]
            p['categories'] = [c for c in crumbs[1:-1] if c]
        return p if p.get('name') else None

    # ---------- التنسيق ----------
    def _passes(self, p):
        cats = _categories(p)
        include_kws, include_cats, exclude_cats = self.rules
        if cats is None:
            self.stats['unknown_category'] += 1
            # التصنيف غير معروف: لا نُسقط المنتج بقاعدة تصنيف لا نستطيع تقييمها
            if include_kws and not include_cats:
                return validate_product(p.get('name', ''), [], include_kws, [], [])
            return True
        return validate_product(p.get('name', ''), cats, include_kws, include_cats, exclude_cats)

    def _fetch_details(self, products, missing, need_detail):
        """ج. الإثراء التكيّفي. الغائبون عن القائمة يُطلبون دائماً؛ طلبات الكمية تتوقّف
        بعد عيّنة إن ثبت أن التفاصيل لا تضيف كمية حقيقية ولا سقف سلة يتتبّع المخزون."""
        listing_has_max = max_quantity_informative(products.values())
        queue, stop, done = missing + need_detail, False, []
        step = DETAIL_WORKERS * 5
        with concurrent.futures.ThreadPoolExecutor(max_workers=DETAIL_WORKERS) as ex:
            for i in range(0, len(queue), step):
                chunk = [pid for pid in queue[i:i + step] if not (stop and pid in products)]
                for pid, det in zip(chunk, ex.map(self.details, chunk)):
                    if det:
                        products[pid] = {**products.get(pid, {}), **det}
                        done.append(pid)
                if stop or self.stats['details'] < HIDDEN_QTY_SAMPLE:
                    continue
                fetched = [products[p] for p in done]
                if any(read_quantity(p)[1] == 'exact' for p in fetched):
                    continue
                if listing_has_max or not max_quantity_informative(fetched):
                    stop = True
                    self.log("   🙈 المتجر يُخفي الكمية والتفاصيل لا تضيف جديداً → إيقاف طلبات التفاصيل")

    def harvest(self):
        products = {}
        sitemap = self.sitemap_products()
        self.log(f"   🗺️ فهرس الخرائط: {len(sitemap)} رابط منتج")

        if self.discover():
            products = self.list_all()
            self.log(f"   📃 القائمة عبر الـAPI: {len(products)} منتج")
            api_urls = {normalize_url(p.get('url')) for p in products.values() if p.get('url')}
            missing = [k for k in sitemap if k not in products and not k.startswith('u:')]
            need_detail = [pid for pid, p in products.items() if read_quantity(p)[1] != 'exact']
            self.log(f"   🔎 تفاصيل مطلوبة: {len(missing)} غائب عن القائمة + {len(need_detail)} بلا كمية")
            self._fetch_details(products, missing, need_detail)
            for k, url in sitemap.items():
                if k in products and not products[k].get('url'):
                    products[k]['url'] = url
            still_missing = [k for k in sitemap if k not in products and k[2:] not in api_urls]
        else:
            still_missing = list(sitemap)

        # د. HTML للباقي. سقف أمان: فرق كبير يعني غالباً أن الروابط لم تُطابَق (لا منتجات ناقصة)،
        # وقراءتها كلها تُكرّر منتجات موجودة بمفاتيح أخرى.
        if still_missing and products and len(still_missing) > max(30, len(products) // 4):
            self.log(f"   ⚠️ {len(still_missing)} رابط خريطة بلا مطابق في الـAPI — تُرك دون قراءة HTML. "
                     f"أمثلة: {[sitemap[k] for k in still_missing[:3]]}")
            still_missing = []
        if still_missing:
            self.log(f"   🧾 قراءة HTML احتياطية لـ {len(still_missing)} منتج")
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
                for k, p in zip(still_missing, ex.map(self.html_product, [sitemap[x] for x in still_missing])):
                    if p:
                        products[k] = p

        use_max = max_quantity_informative(products.values())
        if use_max:
            self.log("   📐 الكمية مخفية لكن سقف السلة (max_quantity) يتغيّر بين المنتجات → يُستخدم تقديراً للمخزون")
        sample = next(iter(products.values()), None)
        if sample:
            keys = ('quantity', 'is_hidden_quantity', 'can_show_remained_quantity', 'status', 'is_available',
                    'unlimited_quantity', 'max_quantity', 'sold_quantity', 'sku', 'url')
            self.log(f"   🧪 عيّنة خام: { {k: sample.get(k) for k in keys} }")
        maxes = sorted({limited_max(p) for p in products.values()} - {None})
        self.log(f"   🧪 قيم max_quantity المختلفة: {len(maxes)} → {maxes[:15]}")

        rows = []
        for k, p in products.items():
            if not self._passes(p):
                self.stats['filtered_out'] += 1
                continue
            for row in product_to_rows(p, self.store_id, sitemap.get(k), use_max):
                self.stats[row['quantity_source']] += 1
                rows.append(row)
        q = [r['quantity'] for r in rows]
        self.log(f"   📊 {self.stats} · الكميات: صفر={q.count(0)} · واحد={q.count(1)} · أكثر={sum(x > 1 for x in q)}")
        return rows


def harvest_store(store_id, sitemap_url, rules, log=print):
    return SallaStore(store_id, sitemap_url, rules, log).harvest()
