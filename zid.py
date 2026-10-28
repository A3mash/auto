"""محرّك Zid: صفحة لكل منتج، والبيانات من window.zidData / window.product."""
import random
import time

from bs4 import BeautifulSoup

from common import clean_sku, extract_json_object, polite_get, to_int, to_number, validate_product


def _categories(target):
    out = []
    raw = target.get('categories', [])
    if isinstance(raw, list):
        for c in raw:
            if isinstance(c, dict) and 'name' in c:
                out.append(c['name'])
            elif isinstance(c, str):
                out.append(c)
    return out


def _stock(obj):
    v = obj.get('stock') if obj.get('stock') is not None else obj.get('quantity')
    if isinstance(v, dict):
        v = v.get('available_quantity', v.get('quantity'))
    q = to_int(v)
    return max(q, 0) if q is not None else 0


def fetch_product(url, store_id, rules):
    time.sleep(random.uniform(1.5, 4.0))  # تمويه بشري لتجنّب الحظر
    r = polite_get(url, timeout=15)
    if r is None or r.status_code != 200:
        return []

    soup = BeautifulSoup(r.text, 'html.parser')
    og_image = soup.find('meta', property='og:image')
    og_image = og_image['content'] if og_image and og_image.has_attr('content') else ''

    target = None
    for script in soup.find_all('script'):
        if not script.string:
            continue
        if 'window.zidData' in script.string:
            data = extract_json_object(script.string, 'window.zidData')
            if isinstance(data, dict) and 'product' in data:
                target = data['product']
                break
        elif 'window.product' in script.string:
            data = extract_json_object(script.string, 'window.product')
            if isinstance(data, dict):
                target = data
                break
    if not target:
        return []

    base_name = target.get('name', '')
    cats = _categories(target)
    if not validate_product(base_name, cats, *rules):
        return []

    image = target.get('image', '')
    if isinstance(image, dict):
        image = image.get('url', '')
    if image and not image.startswith('http'):
        image = 'https://cdn.zid.store/' + image
    image = og_image or image
    category = ' / '.join(cats) if cats else 'غير محدد'
    product_id = target.get('id', '')

    variants = [v for v in (target.get('variants') or []) if isinstance(v, dict)]
    rows = []
    for v in variants or [target]:
        price = to_number(v.get('sale_price')) or to_number(v.get('price')) or 0.0
        rows.append({
            'store_id': store_id,
            # بلا SKU حقيقي نرجع لمعرّف المتغيّر كما كان سابقاً، فلا تنقطع سلسلة التاريخ
            'sku': clean_sku(v.get('sku'), str(v.get('id') or product_id or url)),
            'product_name': v.get('name') or base_name,
            'price': price,
            'quantity': _stock(v),
            'product_url': url,
            'main_image': image,
            'detected_category': category,
            'quantity_source': 'exact',
        })
    return rows
