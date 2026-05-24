import os
import json
import time
import random
import requests
import cloudscraper
import concurrent.futures
from bs4 import BeautifulSoup
from supabase import create_client, Client

# قراءة الإعدادات ديناميكياً من نظام التشغيل السحابي لحمايتها
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("⚠️ خطأ: لم يتم العثور على مفاتيح الاتصال بـ Supabase في البيئة السحابية.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
# ... بقية كود المحرك كما هو دون أي تغيير ...

print("🛠️ جاري تجهيز ترسانة الأجهزة الوهمية...")
scrapers = [
    cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}),
    cloudscraper.create_scraper(browser={'browser': 'firefox', 'platform': 'windows', 'desktop': True}),
    cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'darwin', 'desktop': True}),
    cloudscraper.create_scraper(browser={'browser': 'firefox', 'platform': 'darwin', 'desktop': True}),
    cloudscraper.create_scraper(browser={'browser': 'chrome', 'platform': 'linux', 'desktop': True})
]

for s in scrapers:
    adapter = requests.adapters.HTTPAdapter(pool_connections=50, pool_maxsize=50)
    s.mount('http://', adapter)
    s.mount('https://', adapter)

MAX_WORKERS = 14

# ==========================================
# 2. دوال مساعدة وعمومية
# ==========================================
def get_sitemap_links(sitemap_url, current_device):
    try:
        response = current_device.get(sitemap_url, timeout=20)
        soup = BeautifulSoup(response.text, 'xml')
        links = [loc.text for loc in soup.find_all('loc') if '/products/' in loc.text or '/p/' in loc.text or loc.text.count('/') > 3]
        return list(set(links))
    except Exception:
        return []

def extract_json_object(text, start_keyword):
    idx = text.find(start_keyword)
    if idx == -1: return None
    start_idx = text.find('{', idx)
    if start_idx == -1: return None

    brace_count = 0
    in_string = False
    escape = False
    for i in range(start_idx, len(text)):
        char = text[i]
        if escape: escape = False; continue
        if char == '\\': escape = True; continue
        if char == '"': in_string = not in_string; continue
        if not in_string:
            if char == '{': brace_count += 1
            elif char == '}': brace_count -= 1
            if brace_count == 0:
                try: return json.loads(text[start_idx:i+1])
                except: return None
    return None

# ==========================================
# 3. محرك الفلترة الذكي (ثلاثي الأبعاد)
# ==========================================
def validate_product(product_name, product_categories, include_kws, include_cats, exclude_cats):
    name_lower = product_name.lower()
    cats_lower = [c.lower() for c in product_categories]

    if exclude_cats:
        if any(ex_cat.lower() in cat for cat in cats_lower for ex_cat in exclude_cats):
            return False

    if not include_kws and not include_cats:
        return True

    passed_kw = False
    passed_cat = False

    if include_kws:
        passed_kw = any(kw.lower() in name_lower for kw in include_kws)

    if include_cats:
        passed_cat = any(inc_cat.lower() in cat for cat in cats_lower for inc_cat in include_cats)

    if include_kws and include_cats:
        return passed_kw or passed_cat
    elif include_kws:
        return passed_kw
    elif include_cats:
        return passed_cat

    return False

# ==========================================
# 4. دوال سحب المنتج (مع صياد الصور)
# ==========================================
def fetch_single_zid(url, store_id, include_kws, include_cats, exclude_cats):
    local_data = []
    current_device = random.choice(scrapers)
    try:
        response = current_device.get(url, timeout=15)
        if response.status_code != 200: return local_data

        soup = BeautifulSoup(response.text, 'html.parser')

        # صياد الصور
        og_image = soup.find("meta", property="og:image")
        final_image_url = og_image["content"] if og_image and og_image.has_attr("content") else ""

        target_data = None
        for script in soup.find_all('script'):
            if not script.string: continue
            if 'window.zidData' in script.string:
                data = extract_json_object(script.string, 'window.zidData')
                if data and isinstance(data, dict) and 'product' in data:
                    target_data = data['product']
                    break
            elif 'window.product' in script.string:
                data = extract_json_object(script.string, 'window.product')
                if data and isinstance(data, dict):
                    target_data = data
                    break

        if target_data:
            base_name = target_data.get('name', '')

            raw_categories = target_data.get('categories', [])
            product_categories = []
            if isinstance(raw_categories, list):
                for c in raw_categories:
                    if isinstance(c, dict) and 'name' in c: product_categories.append(c['name'])
                    elif isinstance(c, str): product_categories.append(c)

            if not validate_product(base_name, product_categories, include_kws, include_cats, exclude_cats):
                return local_data

            detected_cat_str = " / ".join(product_categories) if product_categories else "غير محدد"
            variants = target_data.get('variants', [])

            backup_image = target_data.get('image', '')
            if backup_image and not backup_image.startswith('http'):
                backup_image = "https://cdn.zid.store/" + backup_image

            resolved_image = final_image_url or backup_image

            if isinstance(variants, list) and len(variants) > 0:
                for variant in variants:
                    if not isinstance(variant, dict): continue
                    v_price = variant.get('sale_price') or variant.get('price') or 0
                    v_stock = variant.get('stock') if variant.get('stock') is not None else variant.get('quantity', 0)
                    local_data.append({
                        'store_id': store_id,
                        'sku': str(variant.get('sku') or variant.get('id', '')),
                        'product_name': variant.get('name') or base_name,
                        'price': float(v_price) if v_price else 0.0,
                        'quantity': int(v_stock) if v_stock else 0,
                        'product_url': url,
                        'main_image': resolved_image,
                        'detected_category': detected_cat_str
                    })
            else:
                price = target_data.get('sale_price') or target_data.get('price') or 0
                stock = target_data.get('stock') if target_data.get('stock') is not None else target_data.get('quantity', 0)
                local_data.append({
                    'store_id': store_id,
                    'sku': str(target_data.get('sku', '')),
                    'product_name': base_name,
                    'price': float(price) if price else 0.0,
                    'quantity': int(stock) if stock else 0,
                    'product_url': url,
                    'main_image': resolved_image,
                    'detected_category': detected_cat_str
                })
    except Exception:
        pass
    return local_data

def fetch_single_salla(url, store_id, include_kws, include_cats, exclude_cats):
    local_data = []
    current_device = random.choice(scrapers)
    try:
        response = current_device.get(url, timeout=15)
        soup = BeautifulSoup(response.text, 'html.parser')

        # صياد الصور
        og_image = soup.find("meta", property="og:image")
        final_image_url = og_image["content"] if og_image and og_image.has_attr("content") else ""

        for script in soup.find_all('script'):
            if script.string and 'window.dataLayer = window.dataLayer || [];' in script.string and 'ecommerce' in script.string:
                try:
                    json_text = script.string.split('window.dataLayer.push(')[1].split(');')[0]
                    data = json.loads(json_text)
                    if 'ecommerce' in data and 'detail' in data['ecommerce']:
                        prod_data = data['ecommerce']['detail']['products'][0]
                        prod_name = prod_data.get('name', '')

                        raw_categories = prod_data.get('categories', [])
                        product_categories = [c.get('name', '') for c in raw_categories if isinstance(c, dict)]

                        if validate_product(prod_name, product_categories, include_kws, include_cats, exclude_cats):
                            local_data.append({
                                'store_id': store_id,
                                'product_name': prod_name,
                                'sku': str(prod_data.get('id', '')),
                                'price': float(prod_data.get('price', 0)),
                                'quantity': int(prod_data.get('quantity', 0)),
                                'product_url': url,
                                'main_image': final_image_url or prod_data.get('image', ''),
                                'detected_category': " / ".join(product_categories) if product_categories else "غير محدد"
                            })
                        break
                except: pass
    except Exception:
        pass
    return local_data

# ==========================================
# 5. الموجه والمنسق المتوازي
# ==========================================
def run_concurrent_harvest(platform, store_id, sitemap_url, include_kws, include_cats, exclude_cats):
    # استخدمنا الجهاز الأول لسحب الخريطة
    product_links = get_sitemap_links(sitemap_url, scrapers[0])
    harvested_data = []

    if not product_links:
        return harvested_data

    print(f"⚡ بدء المعالجة المتوازية لـ {len(product_links)} رابط...")
    target_func = fetch_single_zid if platform == 'zid' else fetch_single_salla

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(target_func, url, store_id, include_kws, include_cats, exclude_cats): url for url in product_links}

        completed = 0
        for future in concurrent.futures.as_completed(futures):
            completed += 1
            result = future.result()
            if result:
                harvested_data.extend(result)

            if completed % 100 == 0 or completed == len(product_links):
                print(f"🔄 تمت معالجة [{completed}/{len(product_links)}] رابط...")

    return harvested_data

# ==========================================
# 6. الدورة الرئيسية (Main) - متكاملة تماماً
# ==========================================
def main():
    print("🚀 بدء تشغيل محرك الحصاد الذكي (نسخة الإطلاق النهائية)...\n")
    # أضفنا order('id') لضمان الترتيب من 1 فصاعداً
    response = supabase.table("stores_config").select("*").eq("is_active", True).order('id').execute()
    active_stores = response.data

    if not active_stores:
        print("⚠️ لا توجد متاجر مفعلة في النظام.")
        return

    for store in active_stores:
        store_id = store['id']
        platform = store['platform'].lower()
        sitemap_url = store['sitemap_url']
        store_name = store.get('store_name', 'غير معروف')

        print(f"\n" + "="*50)
        print(f"⚙️ معالجة متجر: {store_name} (رقم: {store_id})")
        print("="*50)

        rules_response = supabase.table("store_rules").select("*").eq("store_id", store_id).execute()
        rules = rules_response.data

        include_kws = [r['value'].strip() for r in rules if r['rule_type'] == 'INCLUDE_KEYWORD']
        include_cats = [r['value'].strip() for r in rules if r['rule_type'] == 'INCLUDE_CATEGORY']
        exclude_cats = [r['value'].strip() for r in rules if r['rule_type'] == 'EXCLUDE_CATEGORY']

        if not include_kws and not include_cats and not exclude_cats:
            print("📋 وضع الحصاد: شامل وعام (سحب كامل المخزون)")
        else:
            if include_kws: print(f"🔍 فلاتر الاسم: {include_kws}")
            if include_cats: print(f"📂 فلاتر التصنيف المطلوبة: {include_cats}")
            if exclude_cats: print(f"🛑 فلاتر التصنيف المستبعدة: {exclude_cats}")

        if platform in ['zid', 'salla']:
            products = run_concurrent_harvest(platform, store_id, sitemap_url, include_kws, include_cats, exclude_cats)
            print(f"🎯 الحصيلة المقبولة والمطابقة: {len(products)} منتج.")

            if products:
                # تنظيف المنتجات المكررة لحماية قاعدة البيانات
                unique_products_dict = {}
                for p in products:
                    unique_key = f"{p['store_id']}_{p['sku']}"
                    unique_products_dict[unique_key] = p

                clean_products = list(unique_products_dict.values())

                print(f"📦 جاري ضخ بيانات متجر ({store_name}) ({len(clean_products)} منتج فريد) إلى Supabase...")
                batch_size = 500
                for i in range(0, len(clean_products), batch_size):
                    batch = clean_products[i:i+batch_size]
                    supabase.table("live_inventory").upsert(batch, on_conflict="store_id,sku").execute()
                print(f"✅ تم رفع بيانات متجر ({store_name}) بنجاح! ننتقل للمتجر التالي...")
            else:
                print(f"📭 لم يتم العثور على منتجات مطابقة في متجر ({store_name}).")

        else:
            print(f"⚠️ منصة غير مدعومة: {platform}")

    print("\n🎉 تم الانتهاء من جميع المتاجر وتحديث المخزون الحي بنجاح تام!")

if __name__ == "__main__":
    main()
