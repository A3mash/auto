"""أدوات مشتركة بين محرّكَي Zid وSalla: الشبكة، خرائط الموقع، فك JSON، والفلترة."""
import json
import random
import re
import time
from urllib.parse import urlparse

import cloudscraper
import requests
from bs4 import BeautifulSoup

# ==========================================
# 1. الأجهزة الوهمية (جلسات HTTP)
# ==========================================
_BROWSERS = [
    ('chrome', 'windows'), ('firefox', 'windows'), ('chrome', 'darwin'),
    ('firefox', 'darwin'), ('chrome', 'linux'),
]


def _make_scraper(browser, platform):
    s = cloudscraper.create_scraper(browser={'browser': browser, 'platform': platform, 'desktop': True})
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


scrapers = [_make_scraper(b, p) for b, p in _BROWSERS]


def random_device():
    return random.choice(scrapers)


def polite_get(url, *, device=None, timeout=20, retries=3, headers=None, params=None):
    """GET مع إعادة محاولة وتراجع أُسّي عند 429/5xx وأخطاء الشبكة. يُعيد Response أو None."""
    device = device or random_device()
    for attempt in range(retries):
        try:
            r = device.get(url, timeout=timeout, headers=headers, params=params)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get('Retry-After') or 0) or (2 ** attempt) * 3
                time.sleep(min(wait, 60) + random.uniform(0, 1.5))
                continue
            return r
        except requests.RequestException:
            time.sleep((2 ** attempt) * 2 + random.uniform(0, 1))
    return None


def base_url_of(url):
    p = urlparse(url if '://' in url else 'https://' + url)
    return f"{p.scheme or 'https'}://{p.netloc}"


# ==========================================
# 2. خرائط الموقع (تدعم الفهارس المتداخلة)
# ==========================================
EXCLUDED_PATTERNS = [
    '/categories', '/category', '/tags', '/pages', '/page/', '/blog', '/cart',
    '/shipping', '/privacy', '/about', '/contact', '/search', '/brands',
]
_SALLA_CATEGORY = re.compile(r'/c\d+/?$')


def crawl_sitemap(sitemap_url, *, max_sitemaps=200, log=None):
    """يجمع كل روابط <loc> من الخريطة، ويتبع فهارس <sitemapindex> إلى نهايتها.

    يُعيد (روابط الصفحات، الخرائط التي قُرئت). الخريطة القديمة كانت تُقرأ كملف واحد،
    فأي متجر يقسّم منتجاته على عدّة ملفات (sitemap-2, sitemap-3…) كان يُحصد جزئياً.
    """
    queue, seen, pages = [sitemap_url], set(), []
    while queue and len(seen) < max_sitemaps:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        r = polite_get(url, device=scrapers[0])
        if r is None or r.status_code != 200:
            if log:
                log(f"   ⚠️ خريطة {url} → {'لا رد' if r is None else r.status_code}")
            continue
        soup = BeautifulSoup(r.content, 'xml')
        locs = [loc.text.strip() for loc in soup.find_all('loc') if loc.text]
        if soup.find('sitemapindex'):
            queue.extend(locs)
        else:
            pages.extend(locs)
        if log:
            log(f"   🗺️ {url} → {len(locs)} رابط{' (فهرس)' if soup.find('sitemapindex') else ''}")
    return pages, sorted(seen)


def filter_product_links(links, store_base):
    """يستبعد الصفحات العامة والتصنيفات والصور ورابط المتجر نفسه، فيبقى ما يُرجَّح أنه منتج."""
    store_base = store_base.rstrip('/')
    out = set()
    for url in links:
        low = url.lower()
        if 'cdn.salla.sa' in low or low.endswith(('.jpg', '.jpeg', '.png', '.webp')):
            continue
        path = urlparse(url).path.lower()
        if any(p in path for p in EXCLUDED_PATTERNS) or _SALLA_CATEGORY.search(path):
            continue
        if url.rstrip('/') == store_base:
            continue
        out.add(url)
    return sorted(out)


# ==========================================
# 3. فك JSON من داخل السكربتات
# ==========================================
def extract_json_object(text, start_keyword):
    """يعزل أول كائن JSON متوازن الأقواس بعد الكلمة المفتاحية."""
    idx = text.find(start_keyword)
    if idx == -1:
        return None
    start_idx = text.find('{', idx)
    if start_idx == -1:
        return None
    depth, in_string, escape = 0, False, False
    for i in range(start_idx, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == '\\':
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if not in_string:
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start_idx:i + 1])
                    except ValueError:
                        return None
    return None


def to_number(value):
    """يقبل 12 أو "12.5" أو {"amount": 12} ويُعيد float أو None."""
    if isinstance(value, dict):
        value = value.get('amount', value.get('value'))
    if value is None or value == '':
        return None
    try:
        return float(str(value).replace(',', ''))
    except ValueError:
        return None


def to_int(value):
    n = to_number(value)
    return None if n is None else int(n)


# ==========================================
# 4. الـSKU: لا "_" ولا فراغ (D7)
# ==========================================
_JUNK_SKU = re.compile(r'^[\s_\-.0]*$')


def clean_sku(raw, fallback):
    """يُعيد SKU التاجر، أو المعرّف البديل إن كان فارغاً أو رموزاً بلا معنى مثل "_".

    SKU القصير مثل "487" يبقى كما هو: هو مفتاح المنتج داخل متجره وعليه يُبنى سجل
    تغيّراته (992 صفاً في 2026-10-28). تغييره يقطع تاريخ المنتج. أما المطابقة الكاذبة بين
    المتاجر على SKU قصير (D1) فمكان إصلاحها طبقة المقارنة، لا مفتاح التخزين.
    """
    s = str(raw or '').strip()
    return fallback if _JUNK_SKU.match(s) else s


# ==========================================
# 5. محرّك الفلترة (اسم · تصنيف مطلوب · تصنيف مستبعد)
# ==========================================
def validate_product(product_name, product_categories, include_kws, include_cats, exclude_cats):
    name_lower = (product_name or '').lower()
    cats_lower = [c.lower() for c in product_categories]

    if exclude_cats and any(ex.lower() in cat for cat in cats_lower for ex in exclude_cats):
        return False
    if not include_kws and not include_cats:
        return True

    passed_kw = bool(include_kws) and any(kw.lower() in name_lower for kw in include_kws)
    passed_cat = bool(include_cats) and any(inc.lower() in cat for cat in cats_lower for inc in include_cats)
    return passed_kw or passed_cat
