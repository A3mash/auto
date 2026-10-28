"""فحص قراءة فقط لقاعدة رادار الأشهب عبر PostgREST. لا يكتب شيئاً ولا يطبع المفتاح.

python inspect_db.py   (يحتاج SUPABASE_URL و SUPABASE_KEY)
"""
import base64
import json
import os
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import requests

URL = os.environ['SUPABASE_URL'].rstrip('/') + '/rest/v1/'
KEY = os.environ['SUPABASE_KEY']
H = {'apikey': KEY, 'Authorization': f'Bearer {KEY}'}

TABLES = ['live_inventory', 'inventory_history', 'stores_config', 'store_rules']
VIEWS = ['view_market_radar', 'view_sold_items_detailed', 'view_restocked_items', 'view_price_changes_alerts',
         'view_store_extended_stats', 'view_store_profitability', 'view_general_stats', 'view_top_product_today']


def key_role():
    if KEY.startswith('sb_'):
        return KEY.split('_')[1]  # publishable | secret
    try:
        payload = KEY.split('.')[1]
        return json.loads(base64.urlsafe_b64decode(payload + '=' * (-len(payload) % 4))).get('role')
    except Exception:
        return 'unknown'


def get(path, params=None, count=False, timeout=60):
    headers = dict(H)
    if count:
        headers.update({'Prefer': 'count=exact', 'Range': '0-0'})
    t = time.time()
    r = requests.get(URL + path, params=params, headers=headers, timeout=timeout)
    ms = int((time.time() - t) * 1000)
    total = None
    if count and 'content-range' in r.headers:
        total = r.headers['content-range'].split('/')[-1]
    try:
        body = r.json()
    except ValueError:
        body = r.text[:300]
    return r.status_code, body, total, ms


def section(title):
    print(f"\n{'=' * 60}\n{title}\n{'=' * 60}")


def page_all(path, select, order, extra=None, cap=20000):
    rows, start = [], 0
    while start < cap:
        params = {'select': select, 'order': order, 'limit': 1000, 'offset': start, **(extra or {})}
        status, body, _, _ = get(path, params)
        if status != 200 or not body:
            break
        rows += body
        if len(body) < 1000:
            break
        start += 1000
    return rows


def main():
    section(f"المفتاح: دور = {key_role()}")
    status, body, _, _ = get('')
    if status == 200 and isinstance(body, dict):
        paths = sorted(p.strip('/') for p in body.get('paths', {}) if p != '/')
        print('كائنات مكشوفة في OpenAPI:', [p for p in paths if not p.startswith('rpc/')])
        print('دوال RPC:', [p for p in paths if p.startswith('rpc/')])
        defs = body.get('definitions', {})
        for t in TABLES:
            if t in defs:
                print(f"  أعمدة {t}: {list(defs[t].get('properties', {}))}")
    else:
        print(f"OpenAPI: HTTP {status}")

    section('عدد الصفوف وزمن الاستجابة لكل كائن')
    for obj in TABLES + VIEWS:
        col = {'stores_config': 'id', 'store_rules': 'id', 'view_general_stats': 'total_products',
               'view_store_extended_stats': 'store_id', 'view_store_profitability': 'store_id',
               'view_top_product_today': 'sku'}.get(obj, 'sku')
        status, body, total, ms = get(obj, {'select': col}, count=True)
        err = '' if status in (200, 206) else f" ⚠️ {str(body)[:160]}"
        print(f"  {obj:28} HTTP {status}  صفوف={total}  {ms}ms{err}")

    section('Q9: استعلام الرادار الرئيس')
    for order in ('price_gap.desc', 'total_sold.desc'):
        status, body, _, ms = get('view_market_radar', {'select': '*', 'order': order, 'limit': 24})
        print(f"  order={order}: HTTP {status} {ms}ms {'' if status == 200 else str(body)[:200]}")
    status, body, _, ms = get('view_market_radar', {
        'select': 'sku,store_id,price,min_price,max_price,price_gap,stores_count',
        'order': 'price_gap.desc', 'limit': 24})
    print(f"  بلا total_sold/last_old_price: HTTP {status} {ms}ms")

    section('المتاجر والقواعد')
    _, stores, _, _ = get('stores_config', {'select': '*', 'order': 'id'})
    for s in stores:
        print(f"  {s}")
    _, rules, _, _ = get('store_rules', {'select': '*', 'order': 'id'})
    for r in rules:
        print(f"  {r}")

    section('طزاجة البيانات لكل متجر')
    for s in stores:
        sid = s['id']
        _, lu, cnt, _ = get('live_inventory', {'select': 'last_updated', 'store_id': f'eq.{sid}',
                                               'order': 'last_updated.desc.nullslast', 'limit': 1}, count=True)
        _, dh, hcnt, _ = get('inventory_history', {'select': 'detected_at', 'store_id': f'eq.{sid}',
                                                   'order': 'detected_at.desc', 'limit': 1}, count=True)
        print(f"  متجر {sid} {s.get('store_name')}: live={cnt} آخر last_updated={lu[0]['last_updated'] if lu else None}"
              f" · history={hcnt} آخر detected_at={dh[0]['detected_at'] if dh else None}")

    section('inventory_history: أنواع التغيّر لكل متجر (آخر 14 يوماً)')
    since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    hist = page_all('inventory_history', 'store_id,change_type,old_quantity,new_quantity,old_price,new_price,detected_at',
                    'detected_at.desc', {'detected_at': f'gte.{since}'})
    print(f"  صفوف: {len(hist)}")
    by = Counter((h['store_id'], h['change_type']) for h in hist)
    for (sid, ct), n in sorted(by.items()):
        print(f"  متجر {sid} · {ct}: {n}")
    days = Counter(h['detected_at'][:10] for h in hist)
    print('  حسب اليوم:', dict(sorted(days.items())))
    if hist:
        print('  عيّنة:', hist[:3])

    section('live_inventory: جودة SKU والكميات')
    live = page_all('live_inventory', 'store_id,sku,quantity,price,last_updated', 'store_id,sku')
    print(f"  صفوف مقروءة: {len(live)}")
    junk = [r for r in live if not r['sku'].strip('_-. 0') or (r['sku'].isdigit() and len(r['sku']) <= 4)]
    print(f"  SKU هزيل (_ أو ≤4 أرقام): {len(junk)} · حسب المتجر: {dict(Counter(r['store_id'] for r in junk))}")
    for sid in sorted({r['store_id'] for r in live}):
        q = [r['quantity'] for r in live if r['store_id'] == sid]
        print(f"  متجر {sid}: صفوف={len(q)} صفر={q.count(0)} عشرة={q.count(10)} توزيع شائع={Counter(q).most_common(5)}")
    for sid in sorted({r['store_id'] for r in live}):
        ss = [r['sku'] for r in live if r['store_id'] == sid]
        print(f"  متجر {sid}: أمثلة SKU={ss[:4]} · رقمية بالكامل={sum(x.isdigit() for x in ss)}/{len(ss)}")
    skus = Counter(r['sku'] for r in live)
    shared = [s for s, n in skus.items() if n > 1]
    print(f"  SKU مشترك بين أكثر من متجر: {len(shared)} · منها هزيل: {sum(1 for s in shared if len(s) <= 4)}")

    section('العروض المجمّعة')
    for v in ('view_general_stats', 'view_store_extended_stats', 'view_store_profitability', 'view_top_product_today'):
        status, body, _, ms = get(v, {'select': '*', 'limit': 10})
        print(f"  {v}: HTTP {status} {ms}ms → {body if status == 200 else str(body)[:200]}")

    section('S3: هل يقبل هذا المفتاح الكتابة؟ (PATCH على صف غير موجود id=-1 — لا يغيّر شيئاً)')
    r = requests.patch(URL + 'stores_config', params={'id': 'eq.-1'}, json={'is_active': True},
                       headers={**H, 'Content-Type': 'application/json', 'Prefer': 'return=minimal'}, timeout=30)
    print(f"  HTTP {r.status_code} {r.text[:200]}")


if __name__ == '__main__':
    main()
