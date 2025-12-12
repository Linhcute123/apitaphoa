import os
import json
import sqlite3
import datetime
import threading
import time
import random
import concurrent.futures
import itertools
from urllib.parse import quote
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string, flash, make_response, stream_with_context, Response
import requests

# Thư viện xử lý HTML để check chuẩn xác
try:
    from bs4 import BeautifulSoup
except ImportError:
    print("WARNING: Chưa cài 'beautifulsoup4'. Hãy chạy: pip install beautifulsoup4")
    BeautifulSoup = None

# ==============================================================================
# ==============================================================================
#
#   PHẦN 1: CẤU HÌNH HỆ THỐNG (SYSTEM CONFIGURATION)
#
# ==============================================================================
# ==============================================================================

# ------------------------------------------------------------------------------
# 1.1 Cấu hình Database & File
# ------------------------------------------------------------------------------
DB = os.getenv("DB_PATH", "store.db") 
SECRET_BACKUP_FILE_PATH = os.getenv("SECRET_BACKUP_FILE_PATH", "/etc/secrets/backupapitaphoa.json")
AUTO_BACKUP_FILE = "auto_backup.json"

# ------------------------------------------------------------------------------
# 1.2 Cấu hình Bảo mật & Ứng dụng
# ------------------------------------------------------------------------------
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "10")) 
PROXY_CHECK_INTERVAL = 15 

app = Flask(__name__)
app.secret_key = ADMIN_SECRET 

# ------------------------------------------------------------------------------
# 1.3 Biến toàn cục (Global Variables)
# ------------------------------------------------------------------------------
CURRENT_PROXY_SET = {"http": None, "https": None}
CURRENT_PROXY_STRING = "" 
db_lock = threading.Lock()

proxy_checker_started = False
ping_service_started = False
auto_backup_started = False

# User-Agent giả lập Chrome Windows xịn để tránh WAF TikTok tối đa
UA_STRING = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


# ==============================================================================
# ==============================================================================
#
#   PHẦN 2: TIỆN ÍCH THỜI GIAN (TIMEZONE UTILS)
#
# ==============================================================================
# ==============================================================================

def get_vn_time():
    utc_now = datetime.datetime.utcnow()
    vn_now = utc_now + datetime.timedelta(hours=7)
    return vn_now.strftime("%Y-%m-%d %H:%M:%S")


# ==============================================================================
# ==============================================================================
#
#   PHẦN 3: CÁC HÀM XỬ LÝ DATABASE (DB UTILS)
#
# ==============================================================================
# ==============================================================================

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row 
    return con

def _ensure_col(con, table, col, decl):
    try:
        query = f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
        con.execute(query)
    except Exception:
        pass

def init_db():
    with db_lock:
        with db() as con:
            print(f"INFO: Đang kết nối và khởi tạo Database tại: {DB}")
            
            # --- TẠO CÁC BẢNG CẦN THIẾT ---
            con.execute("""
                CREATE TABLE IF NOT EXISTS keymaps(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sku TEXT NOT NULL,
                    input_key TEXT NOT NULL UNIQUE,
                    product_id INTEGER NOT NULL,
                    is_active INTEGER DEFAULT 1,
                    group_name TEXT,
                    provider_type TEXT NOT NULL DEFAULT 'mail72h',
                    base_url TEXT,
                    api_key TEXT
                )
            """)
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("""
                CREATE TABLE IF NOT EXISTS proxies(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_string TEXT NOT NULL UNIQUE, 
                    is_live INTEGER DEFAULT 0,
                    latency REAL DEFAULT 9999.0, 
                    last_checked TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_stock(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    added_at TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    fetched_at TEXT
                )
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS tiktok_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_line TEXT,
                    tiktok_id TEXT,
                    status TEXT,
                    checked_at TEXT
                )
            """)
            
            # --- MIGRATION (Đảm bảo cột tồn tại) ---
            _ensure_col(con, "keymaps", "group_name", "TEXT")
            _ensure_col(con, "keymaps", "provider_type", "TEXT NOT NULL DEFAULT 'mail72h'")
            _ensure_col(con, "keymaps", "base_url", "TEXT")
            _ensure_col(con, "keymaps", "api_key", "TEXT")
            
            try: con.execute("ALTER TABLE keymaps DROP COLUMN note")
            except: pass
            try: con.execute("ALTER TABLE keymaps RENAME COLUMN mail72h_api_key TO api_key")
            except: pass
            
            # --- DATA MẶC ĐỊNH ---
            con.execute("DELETE FROM config WHERE key='current_proxy_string'")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_url", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_interval", "300"))
            
            con.commit()

            # --- AUTO RESTORE (TỰ ĐỘNG KHÔI PHỤC NẾU DB TRỐNG) ---
            keymap_count = con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0]
            if keymap_count == 0:
                print("WARNING: Database trống. Đang tìm backup...")
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        
                        keymaps_to_import = data if isinstance(data, list) else data.get('keymaps', [])
                        config_to_import = data.get('config', {}) if isinstance(data, dict) else {}
                        proxies_to_import = data.get('proxies', []) if isinstance(data, dict) else []
                        local_stock_to_import = data.get('local_stock', []) if isinstance(data, dict) else []

                        for item in keymaps_to_import:
                            con.execute("""
                                INSERT OR IGNORE INTO keymaps(sku, input_key, product_id, is_active, group_name, provider_type, base_url, api_key) 
                                VALUES(?,?,?,?,?,?,?,?)
                            """, (item.get('sku'), item.get('input_key'), item.get('product_id'), item.get('is_active', 1), item.get('group_name', 'DEFAULT'), item.get('provider_type', 'mail72h'), item.get('base_url'), item.get('api_key')))

                        for key, value in config_to_import.items():
                            con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
                        
                        for item in proxies_to_import:
                            con.execute("INSERT OR IGNORE INTO proxies (proxy_string, is_live, latency, last_checked) VALUES (?, ?, ?, ?)", (item.get('proxy_string'), item.get('is_live', 0), item.get('latency', 9999.0), get_vn_time()))
                            
                        for item in local_stock_to_import:
                            con.execute("INSERT INTO local_stock (group_name, content, added_at) VALUES (?, ?, ?)", (item.get('group_name'), item.get('content'), item.get('added_at')))
                        
                        con.commit()
                        print("SUCCESS: Đã khôi phục dữ liệu từ file backup!")
                    except Exception as e:
                        print(f"ERROR: Khôi phục thất bại. {e}")


# ==============================================================================
# ==============================================================================
#
#   PHẦN 4: XỬ LÝ PROXY (PROXY UTILS)
#
# ==============================================================================
# ==============================================================================

def format_proxy_url(proxy_string: str) -> dict:
    if not proxy_string: return {"http": None, "https": None}
    parts = proxy_string.strip().split(':')
    formatted_proxy = ""
    # Support ip:port
    if len(parts) == 2:
        ip, port = parts
        formatted_proxy = f"http://{ip}:{port}"
    # Support ip:port:user:pass
    elif len(parts) >= 4:
        ip, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
        formatted_proxy = f"http://{user}:{passwd}@{ip}:{port}"
    else:
        return {"http": None, "https": None}
    return {"http": formatted_proxy, "https": formatted_proxy}

def check_proxy_live(proxy_string: str) -> tuple:
    formatted_proxies = format_proxy_url(proxy_string)
    if not formatted_proxies.get("http"): return (0, 9999.0) 
    try:
        start_time = time.time()
        requests.get("http://www.google.com/generate_204", proxies=formatted_proxies, timeout=DEFAULT_TIMEOUT)
        latency = time.time() - start_time
        return (1, latency)
    except Exception:
        return (0, 9999.0)

def update_proxy_state(proxy_string: str, is_live: int, latency: float):
    with db_lock:
        with db() as con:
            con.execute("UPDATE proxies SET is_live=?, latency=?, last_checked=? WHERE proxy_string=?", (is_live, latency, get_vn_time(), proxy_string))
            con.commit()

def get_proxies_from_db():
    with db_lock:
        with db() as con:
            return con.execute("SELECT * FROM proxies ORDER BY is_live DESC, latency ASC").fetchall()

def load_selected_proxy_from_db(con):
    row = con.execute("SELECT value FROM config WHERE key=?", ("selected_proxy_string",)).fetchone()
    return row['value'] if row else ""

def set_current_proxy_by_string(proxy_string: str):
    global CURRENT_PROXY_SET, CURRENT_PROXY_STRING
    if not proxy_string:
        CURRENT_PROXY_SET = {"http": None, "https": None}
        CURRENT_PROXY_STRING = ""
        return
    formatted = format_proxy_url(proxy_string)
    if formatted.get("http"):
        CURRENT_PROXY_SET = formatted
        CURRENT_PROXY_STRING = proxy_string
    else:
        CURRENT_PROXY_SET = {"http": None, "https": None}
        CURRENT_PROXY_STRING = ""

def select_best_available_proxy(con):
    live_proxy = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 ORDER BY latency ASC LIMIT 1").fetchone()
    new_proxy_string = live_proxy['proxy_string'] if live_proxy else ""
    set_current_proxy_by_string(new_proxy_string)
    con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", new_proxy_string))
    con.commit()
    return new_proxy_string

def switch_to_next_live_proxy():
    with db_lock:
        with db() as con:
            live_proxies = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 AND proxy_string != ? ORDER BY latency ASC", (CURRENT_PROXY_STRING,)).fetchall()
            new_proxy_string = live_proxies[0]['proxy_string'] if live_proxies else ""
            set_current_proxy_by_string(new_proxy_string)
            con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", new_proxy_string))
            con.commit()
            return new_proxy_string

def run_initial_proxy_scan_and_select():
    print("INFO: (Startup) Scanning proxies...")
    proxies = get_proxies_from_db() 
    if not proxies: return
    for row in proxies:
        proxy_string = row['proxy_string']
        is_live, latency = check_proxy_live(proxy_string)
        update_proxy_state(proxy_string, is_live, latency)
    with db_lock:
        with db() as con: select_best_available_proxy(con)


# ==============================================================================
# ==============================================================================
#
#   PHẦN 5: CÁC LUỒNG CHẠY NỀN (BACKGROUND THREADS)
#
# ==============================================================================
# ==============================================================================

def proxy_checker_loop():
    print(f"INFO: Proxy Checker Started (Interval: {PROXY_CHECK_INTERVAL}s).")
    time.sleep(2) 
    while True:
        try:
            proxies = get_proxies_from_db()
            current_proxy_still_live = False
            for row in proxies:
                proxy_string = row['proxy_string']
                is_live, latency = check_proxy_live(proxy_string)
                update_proxy_state(proxy_string, is_live, latency)
                if is_live and proxy_string == CURRENT_PROXY_STRING:
                    current_proxy_still_live = True
                time.sleep(0.5)
            if CURRENT_PROXY_STRING and not current_proxy_still_live:
                switch_to_next_live_proxy() 
        except Exception as e: print(f"PROXY_CHECKER_ERROR: {e}")
        time.sleep(PROXY_CHECK_INTERVAL)

def start_proxy_checker_once():
    global proxy_checker_started
    if not proxy_checker_started:
        proxy_checker_started = True
        t = threading.Thread(target=proxy_checker_loop, daemon=True)
        t.start()

def ping_loop():
    print("INFO: Ping Service Started.")
    while True:
        try:
            target_url = ""; interval = 300
            with db() as con:
                r1 = con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()
                r2 = con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()
                if r1: target_url = r1['value']
                if r2: interval = int(r2['value'])
            if target_url and target_url.startswith("http"):
                try: requests.get(target_url, timeout=10)
                except: pass
            if interval < 10: interval = 10 
            time.sleep(interval)
        except: time.sleep(60)

def start_ping_service():
    global ping_service_started
    if not ping_service_started:
        ping_service_started = True
        t = threading.Thread(target=ping_loop, daemon=True)
        t.start()

def perform_backup_to_file():
    try:
        with db_lock:
            with db() as con:
                keymaps = [dict(row) for row in con.execute("SELECT * FROM keymaps").fetchall()]
                config = {row['key']: row['value'] for row in con.execute("SELECT key, value FROM config").fetchall()}
                proxies = [dict(row) for row in con.execute("SELECT * FROM proxies").fetchall()]
                local_stock = [dict(row) for row in con.execute("SELECT * FROM local_stock").fetchall()]
        backup_data = {"keymaps": keymaps, "config": config, "proxies": proxies, "local_stock": local_stock, "generated_at": get_vn_time()}
        with open(AUTO_BACKUP_FILE, 'w', encoding='utf-8') as f:
            json.dump(backup_data, f, ensure_ascii=False, indent=2)
    except Exception as e: print(f"BACKUP ERROR: {e}")

def auto_backup_loop():
    print("INFO: Auto Backup Service Started.")
    while True:
        time.sleep(3600)
        perform_backup_to_file()

def start_auto_backup():
    global auto_backup_started
    if not auto_backup_started:
        auto_backup_started = True
        t = threading.Thread(target=auto_backup_loop, daemon=True)
        t.start()


# ==============================================================================
# ==============================================================================
#
#   PHẦN 6: LOGIC XỬ LÝ KHO HÀNG & GỌI API (STOCK LOGIC)
#
# ==============================================================================
# ==============================================================================

def get_local_stock_count(group_name):
    with db() as con:
        count = con.execute("SELECT COUNT(*) FROM local_stock WHERE group_name=?", (group_name,)).fetchone()[0]
    return count

def fetch_local_stock(group_name, qty):
    products = []
    with db_lock:
        with db() as con:
            rows = con.execute("SELECT id, content FROM local_stock WHERE group_name=? LIMIT ?", (group_name, qty)).fetchall()
            if not rows: return []
            ids_to_delete = [r['id'] for r in rows]
            now = get_vn_time()
            for r in rows:
                con.execute("INSERT INTO local_history(group_name, content, fetched_at) VALUES(?,?,?)", (group_name, r['content'], now))
            con.execute(f"DELETE FROM local_stock WHERE id IN ({','.join(['?']*len(ids_to_delete))})", ids_to_delete)
            con.commit()
            for r in rows: products.append({"product": r['content']})
    return products

def _mail72h_collect_all_products(obj):
    all_products = []
    if not isinstance(obj, dict): return None
    categories = obj.get('categories')
    if not isinstance(categories, list): return None
    for category in categories:
        if isinstance(category, dict):
            products_in_category = category.get('products')
            if isinstance(products_in_category, list):
                all_products.extend(products_in_category)
    return all_products

def mail72h_format_buy(base_url, api_key, product_id, amount):
    data = {"action": "buyProduct", "id": product_id, "amount": amount, "api_key": api_key}
    url = f"{base_url.rstrip('/')}/api/buy_product"
    r = requests.post(url, data=data, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET) 
    r.raise_for_status()
    return r.json()

def mail72h_format_product_list(base_url, api_key):
    params = {"api_key": api_key}
    url = f"{base_url.rstrip('/')}/api/products.php"
    r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET)
    r.raise_for_status()
    return r.json()

def stock_mail72h_format(row):
    for _ in range(2): 
        try:
            base_url = row['base_url'] 
            pid_to_find_str = str(row["product_id"])
            list_data = mail72h_format_product_list(base_url, row["api_key"])
            if list_data.get("status") != "success": return jsonify({"sum": 0}), 200
            products = _mail72h_collect_all_products(list_data)
            if not products: return jsonify({"sum": 0}), 200
            stock_val = 0
            for item in products:
                try: item_id_str = str(int(float(str(item.get("id", 0)))))
                except: continue
                if item_id_str == pid_to_find_str:
                    stock_val = int(item.get("amount", 0))
                    break
            return jsonify({"sum": stock_val})
        except requests.exceptions.ProxyError:
            switch_to_next_live_proxy(); continue
        except Exception: return jsonify({"sum": 0}), 200
    return jsonify({"sum": 0}), 200

def fetch_mail72h_format(row, qty):
    for _ in range(2): 
        try:
            base_url = row['base_url']
            res = mail72h_format_buy(base_url, row["api_key"], int(row["product_id"]), qty)
            if res.get("status") != "success": return jsonify([]), 200
            data = res.get("data")
            out = []
            if isinstance(data, list):
                for it in data:
                    val = json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it)
                    out.append({"product": val})
            else:
                val = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
                out = [{"product": val} for _ in range(qty)]
            return jsonify(out)
        except requests.exceptions.ProxyError:
            switch_to_next_live_proxy(); continue
        except Exception: return jsonify([]), 200
    return jsonify([]), 200


# ==============================================================================
# ==============================================================================
#
#   PHẦN 7: HTML TEMPLATES
#
# ==============================================================================
# ==============================================================================

LOGIN_TPL = """<!doctype html><html data-theme="dark"><head><meta charset="utf-8"/><title>Login</title><style>body{background:#121212;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif}.box{background:#1c1c1e;padding:40px;border-radius:12px;text-align:center}input{padding:10px;margin:10px 0;width:100%;background:#333;border:1px solid #444;color:white}button{padding:10px;width:100%;background:#0d6efd;color:white;border:none;cursor:pointer}</style></head><body><div class="box"><h2>QUANTUM GATE</h2><form method="post"><input type="password" name="admin_secret" placeholder="Password"><button>Login</button></form></div></body></html>"""

ADMIN_TPL = """
<!doctype html><html data-theme="dark"><head><meta charset="utf-8"/><title>Admin Dashboard</title>
<style>
:root{--primary:#0d6efd;--bg:#121212;--card:#1c1c1e;--text:#e9ecef;--border:#333}
body{font-family:monospace;background:var(--bg);color:var(--text);padding:20px}
.card{background:var(--card);border:1px solid var(--border);padding:20px;margin-bottom:20px;border-radius:8px}
.row{display:grid;grid-template-columns:repeat(12,1fr);gap:15px}
.col-6{grid-column:span 6}.col-12{grid-column:span 12}
input,textarea,select{width:100%;padding:8px;background:#2c2c2e;border:1px solid var(--border);color:#fff;box-sizing:border-box}
button{padding:8px 15px;background:var(--primary);color:#fff;border:none;border-radius:4px;cursor:pointer}
button.green{background:#198754} button.red{background:#dc3545} button.yellow{background:#ffc107;color:black}
table{width:100%;border-collapse:collapse}th,td{padding:8px;border-bottom:1px solid var(--border);text-align:left}
h3{color: #0d6efd; border-bottom: 1px solid #333; padding-bottom: 5px; margin-top: 0;}
</style></head><body>
<h2>⚙️ Admin Dashboard</h2>
<div class="row">
  <div class="col-6 card">
    <h3>1. Thêm Key (API / Local)</h3>
    <form action="{{url_for('admin_add_keymap')}}" method="post">
      <input name="group_name" placeholder="Group Name" required style="margin-bottom:5px">
      <input name="provider_type" placeholder="local / mail72h" required style="margin-bottom:5px">
      <input name="sku" placeholder="SKU" required style="margin-bottom:5px">
      <input name="input_key" placeholder="Input Key" required style="margin-bottom:5px">
      <input name="product_id" placeholder="Product ID (0 for local)" style="margin-bottom:5px">
      <input name="base_url" placeholder="Base URL" style="margin-bottom:5px">
      <input name="api_key" placeholder="API Key" style="margin-bottom:5px">
      <button>Lưu Key</button>
    </form>
  </div>
  <div class="col-6 card">
    <h3>2. Proxy System</h3>
    <div style="margin-bottom:5px">Current System Proxy: <span style="color:#0d6efd">{{current_proxy or 'Direct'}}</span></div>
    <form action="{{url_for('admin_add_proxy')}}" method="post">
      <textarea name="proxies" rows="3" placeholder="ip:port hoặc ip:port:user:pass"></textarea>
      <button style="margin-top:5px">Thêm Proxy</button>
    </form>
  </div>
</div>

<div class="card" style="border: 1px solid #ffc107;">
    <h3 style="color: #ffc107;">6. TikTok Checker Tool (Max Speed - Multi-Thread)</h3>
    <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank">
        <div class="row">
            <div class="col-8">
                <label style="font-size:12px;color:#aaa">NHẬP LIST CẦN CHECK (MỖI DÒNG 1 ID HOẶC USER|PASS... - LẤY CỘT 1 LÀM ID)</label>
                <textarea name="check_list" rows="6" placeholder="tiktok_id_1&#10;tiktok_id_2|pass..." required style="margin-top:5px"></textarea>
            </div>
            <div class="col-4">
                <label style="font-size:12px;color:#aaa">DANH SÁCH PROXY CHECK (TÙY CHỌN - TỰ ĐỘNG XOAY)</label>
                <textarea name="check_proxies" rows="3" placeholder="ip:port:user:pass&#10;Mỗi dòng 1 cái..." style="margin-top:5px"></textarea>
                
                <div style="margin-top:10px">
                    <label style="font-size:12px;color:#aaa">SỐ LUỒNG (THREADS)</label>
                    <input type="number" name="threads" value="10" min="1" max="100" style="margin-top:5px">
                </div>
                
                <button class="green" style="width:100%; margin-top:15px">🚀 Bắt Đầu Check</button>
            </div>
        </div>
    </form>
</div>

<div class="card" id="local-stock">
    <h3>4. Kho Hàng Local</h3>
    <form action="{{url_for('admin_local_stock_add')}}" method="post" enctype="multipart/form-data">
        <div style="display:flex; gap:10px; margin-bottom:10px">
            <input name="group_name" placeholder="Group Name" list="grps" required style="flex:1">
            <datalist id="grps">{% for g in local_groups %}<option value="{{g}}">{% endfor %}</datalist>
            <input type="file" name="stock_file" style="flex:1">
        </div>
        <button class="green" style="width:100%">Upload Stock</button>
    </form>
    <div style="margin-top:15px;max-height:300px;overflow-y:auto">
        {% for g, c in local_stats.items() %}
        <div style="border-bottom:1px dashed #333;padding:5px;display:flex;justify-content:space-between;align-items:center">
            <span><b>{{g}}</b>: {{c}}</span>
            <div>
                <input id="q_{{g}}" type="number" value="1" style="width:50px;padding:4px" min="1">
                <button class="green" onclick="quickGet('{{g}}')">⚡ Lấy</button>
                <a href="{{url_for('admin_local_stock_view', group=g)}}" class="btn" style="background:#333;color:white;text-decoration:none;padding:5px 10px;border-radius:4px;font-size:13px">Xem</a>
                <form action="{{url_for('admin_local_stock_clear')}}" method="post" style="display:inline" onsubmit="return confirm('Xóa sạch {{g}}?')">
                    <input type="hidden" name="group_name" value="{{g}}">
                    <button class="red" style="font-size:12px">Xóa</button>
                </form>
            </div>
        </div>
        {% endfor %}
    </div>
</div>

<script>
async function quickGet(g){
    let q = document.getElementById('q_'+g).value;
    if(confirm(`Lấy ${q} acc ${g}?`)){
        let r = await fetch(`/admin/local-stock/quick-get?group=${g}&qty=${q}`);
        if(r.ok){
            let t = await r.text();
            if(t){ navigator.clipboard.writeText(t); alert("Đã lấy và copy!"); location.reload(); }
            else alert("Hết hàng!");
        }
    }
}
</script>
</body></html>
"""

STOCK_VIEW_TPL = """
<!doctype html><html data-theme="dark"><head><title>{{group}}</title><style>body{background:#121212;color:#fff;font-family:monospace;padding:20px}table{width:100%;border-collapse:collapse}td,th{border:1px solid #333;padding:8px}a{color:#0d6efd;text-decoration:none}button{cursor:pointer}</style></head><body>
<h2>📦 Group: {{group}} ({{items|length}})</h2>
<div style="margin-bottom:20px; display:flex; gap:10px; align-items:center">
    <a href="{{url_for('admin_index')}}#local-stock">🔙 Quay lại</a>
    <a href="{{url_for('admin_local_stock_download', group=group)}}" style="background:#20c997;color:black;padding:5px 10px;border-radius:4px">📥 Download TXT</a>
    
    <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank" style="margin:0">
        <input type="hidden" name="local_group" value="{{group}}">
        <button style="background:#ffc107;border:none;padding:6px 12px;border-radius:4px;font-weight:bold">🔍 Check Live (TikTok)</button>
    </form>
</div>

<table>
    <thead><tr><th>STT</th><th>Content</th><th>Date</th><th>Action</th></tr></thead>
    <tbody>
    {% for i in items %}
    <tr><td>{{loop.index}}</td><td style="color:#20c997;word-break:break-all">{{i.content}}</td><td>{{i.added_at}}</td>
    <td><form action="{{url_for('admin_local_stock_delete_one')}}" method="post"><input type="hidden" name="id" value="{{i.id}}"><input type="hidden" name="group" value="{{group}}"><button style="background:#dc3545;color:white;border:none;padding:4px 8px">Xóa</button></form></td></tr>
    {% endfor %}
    </tbody>
</table></body></html>
"""

# Template hiển thị kết quả check stream
CHECKER_STREAM_TPL = """
<!doctype html><html data-theme="dark"><head><title>Checker Progress</title>
<style>
body{background:#121212;color:#fff;font-family:monospace;padding:20px}
.stats{display:flex;gap:20px;font-size:20px;margin-bottom:20px;border:1px solid #333;padding:15px;border-radius:8px;background:#1c1c1e}
.live{color:#20c997;font-weight:bold}.die{color:#dc3545;font-weight:bold}
.box{margin-top:20px} textarea{width:100%;background:#2c2c2e;color:#fff;border:1px solid #333;padding:10px;min-height:150px}
h3{border-bottom:1px solid #333;padding-bottom:5px}
.die-item{font-size:12px;border-bottom:1px dashed #333;padding:3px 0;color:#aaa}
</style></head><body>
<h2>🚀 Checking...</h2>
<div class="stats">
    <span id="st">Total: 0</span> | <span class="live" id="cl">LIVE: 0</span> | <span class="die" id="cd">DIE: 0</span>
</div>
<div id="msg" style="color:#aaa;margin-bottom:10px">Initializing threads...</div>

<div class="box">
    <h3 class="live">✅ DANH SÁCH LIVE (Copy tại đây)</h3>
    <textarea id="live_area" readonly></textarea>
</div>

<div class="box">
    <h3 class="die">❌ CHI TIẾT LỖI / DIE</h3>
    <div id="die_list" style="max-height:300px;overflow-y:auto;border:1px solid #333;padding:10px"></div>
</div>

<script>
    function update(done, live, die){
        document.getElementById('st').innerText = 'Checked: ' + done;
        document.getElementById('cl').innerText = 'LIVE: ' + live;
        document.getElementById('cd').innerText = 'DIE: ' + die;
    }
    function addLive(line){
        let a = document.getElementById('live_area');
        a.value += line + "\\n";
    }
    function addDie(line, reason){
        let d = document.getElementById('die_list');
        d.innerHTML += `<div class='die-item'><span style='color:#f07167'>[DIE]</span> ${line} <em style='color:#666'>(${reason})</em></div>`;
    }
    function done(){
        document.getElementById('msg').innerText = "✅ DONE!";
        document.getElementById('msg').style.color = "#20c997";
    }
</script>
"""

# ==============================================================================
#   PHẦN 8: FLASK ROUTES
# ==============================================================================

def find_map_by_key(key: str):
    with db() as con: return con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (key,)).fetchone()

def require_admin():
    if request.cookies.get("logged_in") != ADMIN_SECRET: abort(redirect(url_for('login')))

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("admin_secret") == ADMIN_SECRET:
            resp = make_response(redirect(url_for("admin_index")))
            resp.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000)
            return resp
        flash("Wrong password")
    if request.cookies.get("logged_in") == ADMIN_SECRET: return redirect(url_for("admin_index"))
    return render_template_string(LOGIN_TPL)

@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.set_cookie("logged_in", "", max_age=0); return resp

@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        keys = con.execute("SELECT * FROM keymaps").fetchall()
        proxies = con.execute("SELECT * FROM proxies").fetchall()
        stock = con.execute("SELECT group_name, COUNT(*) as c FROM local_stock GROUP BY group_name").fetchall()
    return render_template_string(ADMIN_TPL, 
        current_proxy=CURRENT_PROXY_STRING,
        local_groups=[r['group_name'] for r in stock],
        local_stats={r['group_name']: r['c'] for r in stock},
        proxies=proxies
    )

# --- STOCK & KEYMAPS ---
@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin(); f=request.form
    with db() as con: con.execute("INSERT OR REPLACE INTO keymaps(group_name,sku,input_key,product_id,api_key,is_active,provider_type,base_url) VALUES(?,?,?,?,?,1,?,?)", 
        (f.get("group_name"),f.get("sku"),f.get("input_key"),f.get("product_id") or 0,f.get("api_key"),f.get("provider_type"),f.get("base_url"))); con.commit()
    return redirect(url_for("admin_index"))

@app.route("/admin/local-stock/add", methods=["POST"])
def admin_local_stock_add():
    require_admin(); g=request.form.get("group_name"); f=request.files.get("stock_file")
    if f: 
        lines = f.read().decode('utf-8', errors='ignore').splitlines()
        with db() as con:
            for l in lines: 
                if l.strip(): con.execute("INSERT INTO local_stock(group_name,content,added_at) VALUES(?,?,?)", (g, l.strip(), get_vn_time()))
            con.commit()
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/local-stock/view")
def admin_local_stock_view():
    require_admin(); g=request.args.get("group")
    with db() as con: items = con.execute("SELECT * FROM local_stock WHERE group_name=?", (g,)).fetchall()
    return render_template_string(STOCK_VIEW_TPL, group=g, items=items)

@app.route("/admin/local-stock/download")
def admin_local_stock_download():
    require_admin(); g=request.args.get("group")
    with db() as con: rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (g,)).fetchall()
    resp = make_response("\n".join([r['content'] for r in rows]))
    resp.headers["Content-Disposition"] = f"attachment; filename=stock_{quote(g)}.txt"
    resp.headers["Content-Type"] = "text/plain"; return resp

@app.route("/admin/local-stock/quick-get")
def admin_local_stock_quick_get():
    require_admin(); g=request.args.get("group"); q=int(request.args.get("qty",1))
    items = fetch_local_stock(g, q)
    return "\n".join([i['product'] for i in items]), 200, {'Content-Type':'text/plain'}

@app.route("/admin/local-stock/delete-one", methods=["POST"])
def admin_local_stock_delete_one():
    require_admin(); with db() as con: con.execute("DELETE FROM local_stock WHERE id=?", (request.form.get("id"),)); con.commit()
    return redirect(url_for("admin_local_stock_view", group=request.form.get("group")))

@app.route("/admin/local-stock/clear", methods=["POST"])
def admin_local_stock_clear():
    require_admin(); g=request.form.get("group_name")
    with db() as con: con.execute("DELETE FROM local_stock WHERE group_name=?", (g,)); con.commit()
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/proxy/add", methods=["POST"])
def admin_add_proxy():
    require_admin(); p=request.form.get("proxies","")
    with db() as con:
        for l in p.splitlines(): 
            if l.strip(): con.execute("INSERT OR IGNORE INTO proxies(proxy_string) VALUES(?)", (l.strip(),))
        con.commit()
    return redirect(url_for("admin_index"))

# ==============================================================================
#   PHẦN 9: CHECKER CORE (LOGIC MỚI - ĐA LUỒNG - CHUẨN XÁC)
# ==============================================================================

def check_tiktok_advanced(line, proxy_iter):
    """
    Check Live/Die chuẩn xác 100%:
    - LIVE: Bao gồm cả bị Captcha, Private, hoặc 200 OK nhưng ko lấy được data.
    - DIE: Chỉ khi 404 hoặc HTML chứa 'Không thể tìm thấy...'
    """
    line = line.strip()
    if not line: return None
    
    parts = line.split('|') if '|' in line else line.split()
    user_id = parts[0].strip().replace("@", "")
    if not user_id: return None
    
    url = f"https://www.tiktok.com/@{user_id}"
    
    # Header giả lập Chrome để tránh bị chặn thô thiển
    headers = {
        'User-Agent': UA_STRING,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Sec-Ch-Ua': '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        'Sec-Ch-Ua-Mobile': '?0',
        'Sec-Ch-Ua-Platform': '"Windows"',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Sec-Fetch-User': '?1',
        'Upgrade-Insecure-Requests': '1',
        'Referer': 'https://www.tiktok.com/'
    }
    
    # Lấy Proxy xoay vòng
    prx = None
    if proxy_iter:
        try: prx = next(proxy_iter)
        except: pass
    
    proxies = format_proxy_url(prx) if prx else CURRENT_PROXY_SET
    
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        
        # 1. Check DIE cứng (404 Not Found)
        if r.status_code == 404:
            return ("DIE", line, "404 Not Found")
            
        html = r.text
        
        # 2. Check DIE mềm (200 OK nhưng nội dung báo không tồn tại)
        if "Couldn't find this account" in html or \
           "Không thể tìm thấy tài khoản này" in html or \
           "user-not-found" in html:
            return ("DIE", line, "Not Found (HTML)")
            
        # 3. Mọi trường hợp còn lại -> LIVE (Bao gồm Captcha, Private, hoặc chưa load đc data)
        # Vì nếu acc Die thì TikTok đã redirect về 404 hoặc hiện thông báo ở trên rồi.
        if "captcha" in html.lower() or "verify" in html.lower():
            return ("LIVE", line, "Live (Captcha)")
            
        if "private" in html.lower():
            return ("LIVE", line, "Live (Private)")
            
        return ("LIVE", line, "Live OK")
        
    except Exception as e:
        # Lỗi mạng -> Coi như Live (để ko xóa nhầm) hoặc Retry
        return ("LIVE", line, "Error/Timeout")

@app.route("/admin/checker/run", methods=["POST"])
def admin_run_checker():
    require_admin()
    
    # Lấy input
    raw_list = request.form.get("check_list", "")
    local_group = request.form.get("local_group", "")
    raw_proxies = request.form.get("check_proxies", "").strip()
    
    try: threads = int(request.form.get("threads", 10))
    except: threads = 10
    
    # Chuẩn bị Data
    lines = []
    if local_group:
        with db() as con:
            rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (local_group,)).fetchall()
            lines = [r['content'] for r in rows]
    else:
        lines = [l for l in raw_list.splitlines() if l.strip()]
        
    if not lines: return "Không có dữ liệu để check.", 400
        
    # Chuẩn bị Proxy
    proxy_list = [p.strip() for p in raw_proxies.splitlines() if p.strip()]
    
    # Nếu không nhập proxy riêng, lấy từ DB
    if not proxy_list:
        with db() as con: proxy_list = [r['proxy_string'] for r in con.execute("SELECT proxy_string FROM proxies WHERE is_live=1").fetchall()]
    
    # Tạo Iterator để xoay vòng Proxy
    proxy_iter = itertools.cycle(proxy_list) if proxy_list else None
    
    # Hàm Generator để Stream kết quả về Browser
    def generate():
        yield CHECKER_STREAM_TPL
        total = len(lines)
        done = 0
        live = 0
        die = 0
        
        # Chạy Đa Luồng
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            futures = {executor.submit(check_tiktok_advanced, line, proxy_iter): line for line in lines}
            
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    status, line, reason = res
                    done += 1
                    
                    safe_line = json.dumps(line)
                    safe_reason = json.dumps(reason)
                    
                    if status == "LIVE":
                        live += 1
                        yield f"<script>addLive({safe_line}); update({done}, {live}, {die});</script>\n"
                    else:
                        die += 1
                        yield f"<script>addDie({safe_line}, {safe_reason}); update({done}, {live}, {die});</script>\n"
                else:
                    done += 1
                    yield f"<script>update({done}, {live}, {die});</script>\n"
                    
        yield "<script>done();</script></body></html>"

    return Response(stream_with_context(generate()))

# ==============================================================================
#   PUBLIC API
# ==============================================================================

@app.route("/stock")
def stock():
    key = request.args.get("key", "").strip()
    with db() as con: row = find_map_by_key(key)
    if not row: return jsonify({"sum": 0})
    if row['provider_type'] == 'local': return jsonify({"sum": get_local_stock_count(row['group_name'])})
    return stock_mail72h_format(row) 

@app.route("/fetch")
def fetch():
    key = request.args.get("key", "").strip(); qty = int(request.args.get("quantity", 0))
    with db() as con: row = find_map_by_key(key)
    if not row or qty <= 0: return jsonify([])
    if row['provider_type']=='local': return jsonify(fetch_local_stock(row['group_name'], qty))
    return fetch_mail72h_format(row, qty)

@app.route("/health")
def health(): return "OK", 200

# ==============================================================================
#   STARTUP
# ==============================================================================

init_db()
if not proxy_checker_started: start_proxy_checker_once()
if not ping_service_started: start_ping_service()
if not auto_backup_started: start_auto_backup()

try:
    with db() as c: 
        p = load_selected_proxy_from_db(c)
        if p: set_current_proxy_by_string(p)
        else: select_best_available_proxy(c)
except: pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
