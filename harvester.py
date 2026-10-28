"""محرّك الحصاد: يمرّ على المتاجر المفعّلة في stores_config ويكتب live_inventory.

سجل التغيّرات (inventory_history) تكتبه القاعدة نفسها عند تغيّر السعر/الكمية، لا هذا الملف.

التشغيل:
  python harvester.py                       # كل المتاجر المفعّلة، مع الكتابة
  python harvester.py --dry-run --store 5   # متجر واحد بلا كتابة (للتجربة)
  python harvester.py --store 6 --include-inactive --dry-run
"""
import argparse
import concurrent.futures
import os
import sys
from datetime import datetime, timezone

from supabase import Client, create_client

import salla
import zid
from common import base_url_of, crawl_sitemap, filter_product_links

MAX_WORKERS = 2  # Zid: عاملان فقط لتجنّب استفزاز حماية Cloudflare
BATCH_SIZE = 500
CORE_COLUMNS = {'store_id', 'sku', 'product_name', 'price', 'quantity', 'product_url',
                'main_image', 'detected_category', 'last_updated'}
# أعمدة تضيفها الهجرة 001 في مستودع AshhabRadar؛ تُرسَل فقط إن وُجدت في القاعدة
OPTIONAL_COLUMNS = ['quantity_source', 'platform_product_id', 'sold_count']


def connect():
    url, key = os.environ.get('SUPABASE_URL'), os.environ.get('SUPABASE_KEY')
    if not url or not key:
        raise ValueError('⚠️ خطأ: لم يتم العثور على مفاتيح الاتصال بـ Supabase في البيئة السحابية.')
    return create_client(url, key)


def available_optional_columns(db: Client):
    cols = []
    for col in OPTIONAL_COLUMNS:
        try:
            db.table('live_inventory').select(col).limit(1).execute()
            cols.append(col)
        except Exception:
            pass
    return cols


def harvest_zid(store_id, sitemap_url, rules):
    pages, sitemaps = crawl_sitemap(sitemap_url, log=print)
    links = filter_product_links(pages, base_url_of(sitemap_url))
    print(f"   🗺️ {len(sitemaps)} ملف خريطة → {len(links)} رابط")
    rows, done = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for result in ex.map(lambda u: zid.fetch_product(u, store_id, rules), links):
            rows.extend(result)
            done += 1
            if done % 100 == 0 or done == len(links):
                print(f"   🔄 [{done}/{len(links)}]")
    return rows


def dedupe(rows):
    """مفتاح التعارض (store_id, sku). تكرار SKU التاجر بين منتجين مختلفين في سلة لا يُسقط أحدهما:
    يُميَّز الثاني بمعرّف منتجه. غير ذلك: آخر صف يفوز، كما كان."""
    out = {}
    for r in rows:
        key = (r['store_id'], r['sku'])
        prev = out.get(key)
        if prev and r.get('platform_product_id') and prev.get('platform_product_id') != r['platform_product_id']:
            r = {**r, 'sku': f"{r['sku']}-{r['platform_product_id']}"}
            key = (r['store_id'], r['sku'])
        out[key] = r
    return list(out.values())


def write_rows(db, rows, allowed_columns):
    for i in range(0, len(rows), BATCH_SIZE):
        batch = [{k: v for k, v in r.items() if k in allowed_columns} for r in rows[i:i + BATCH_SIZE]]
        db.table('live_inventory').upsert(batch, on_conflict='store_id,sku').execute()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--store', type=int, action='append', help='معرّف متجر (يتكرّر)')
    ap.add_argument('--dry-run', action='store_true', help='اجمع واطبع دون الكتابة في القاعدة')
    ap.add_argument('--include-inactive', action='store_true', help='اسمح بمتاجر is_active = false')
    args = ap.parse_args()

    db = connect()
    print('🚀 بدء تشغيل محرك الحصاد' + (' (تجربة بلا كتابة)' if args.dry_run else '') + '\n')

    q = db.table('stores_config').select('*').order('id')
    if not args.include_inactive:
        q = q.eq('is_active', True)
    if args.store:
        q = q.in_('id', args.store)
    stores = q.execute().data
    if not stores:
        print('⚠️ لا توجد متاجر مطابقة.')
        return

    allowed = CORE_COLUMNS | set(available_optional_columns(db))
    missing_opt = [c for c in OPTIONAL_COLUMNS if c not in allowed]
    if missing_opt:
        print(f"ℹ️ أعمدة غير موجودة بعد في live_inventory (طبّق الهجرة 001): {missing_opt}")

    failures = []
    for store in stores:
        store_id, platform = store['id'], (store['platform'] or '').lower()
        name = store.get('store_name', 'غير معروف')
        print('\n' + '=' * 50 + f"\n⚙️ متجر: {name} (رقم {store_id}، {platform})\n" + '=' * 50)

        rules_rows = db.table('store_rules').select('*').eq('store_id', store_id).order('id').execute().data
        rules = tuple([r['value'].strip() for r in rules_rows if r['rule_type'] == t]
                      for t in ('INCLUDE_KEYWORD', 'INCLUDE_CATEGORY', 'EXCLUDE_CATEGORY'))
        if any(rules):
            print(f"   🔍 كلمات: {rules[0]} · تصنيفات مطلوبة: {rules[1]} · مستبعدة: {rules[2]}")

        started = datetime.now(timezone.utc).isoformat()
        try:
            if platform == 'zid':
                rows = harvest_zid(store_id, store['sitemap_url'], rules)
            elif platform == 'salla':
                rows = salla.harvest_store(store_id, store['sitemap_url'], rules)
            else:
                print(f"⚠️ منصة غير مدعومة: {platform}")
                continue
        except Exception as e:  # متجر معطوب لا يوقف البقية
            print(f"❌ فشل المتجر {name}: {e!r}")
            failures.append(name)
            continue

        rows = dedupe(rows)
        # D3: بصمة كل تمريرة — حتى لو لم يتغيّر السعر ولا الكمية
        for r in rows:
            r['last_updated'] = started
        print(f"🎯 {len(rows)} صف فريد")
        if not rows:
            print(f"📭 لا منتجات من ({name}) — لن يُمسّ مخزونه الحالي.")
            continue
        if args.dry_run:
            for r in rows[:5]:
                print('   •', {k: r.get(k) for k in ('sku', 'product_name', 'price', 'quantity', 'quantity_source')})
            continue
        write_rows(db, rows, allowed)
        print(f"✅ رُفع ({name})")

    print('\n🎉 انتهى.' + (f" متاجر فشلت: {failures}" if failures else ''))
    if failures:
        sys.exit(1)


if __name__ == '__main__':
    main()
