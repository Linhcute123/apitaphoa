import os
import json
import sqlite3
import datetime
import threading
import time
import random
import re # MỚI: Import để xử lý Regex làm sạch dòng chữ DIE/LIVE
from urllib.parse import quote 
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string, flash, make_response
import requests

# ==============================================================================
# ==============================================================================
#
#   PHẦN 1: CẤU HÌNH HỆ THỐNG (SYSTEM CONFIGURATION)
#
# ==============================================================================
# ==============================================================================

# ------------------------------------------------------------------------------
# 1.1 Cấu hình Database
# ------------------------------------------------------------------------------
DB = os.getenv("DB_PATH", "store.db") 

# ------------------------------------------------------------------------------
# 1.2 Cấu hình Backup & Restore
# ------------------------------------------------------------------------------
SECRET_BACKUP_FILE_PATH = os.getenv("SECRET_BACKUP_FILE_PATH", "/etc/secrets/backupapitaphoa.json")
AUTO_BACKUP_FILE = "auto_backup.json"

# ------------------------------------------------------------------------------
# 1.3 Cấu hình Bảo mật & Ứng dụng
# ------------------------------------------------------------------------------
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "5")) 
PROXY_CHECK_INTERVAL = 15 

# Khởi tạo ứng dụng Flask.
app = Flask(__name__)
app.secret_key = ADMIN_SECRET 

# ------------------------------------------------------------------------------
# 1.4 Biến toàn cục (Global Variables)
# ------------------------------------------------------------------------------
CURRENT_PROXY_SET = {
    "http": None, 
    "https": None
}
CURRENT_PROXY_STRING = "" 
db_lock = threading.Lock()

proxy_checker_started = False
ping_service_started = False
auto_backup_started = False


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
            
            # TẠO BẢNG KEYMAPS
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
            
            # TẠO BẢNG CONFIG
            con.execute("""
                CREATE TABLE IF NOT EXISTS config(
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            
            # TẠO BẢNG PROXIES
            con.execute("""
                CREATE TABLE IF NOT EXISTS proxies(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_string TEXT NOT NULL UNIQUE, 
                    is_live INTEGER DEFAULT 0,
                    latency REAL DEFAULT 9999.0, 
                    last_checked TEXT
                )
            """)
            
            # TẠO BẢNG LOCAL STOCK
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_stock(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    added_at TEXT
                )
            """)

            # TẠO BẢNG LOCAL HISTORY
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    fetched_at TEXT
                )
            """)

            # TẠO BẢNG TIKTOK HISTORY
            con.execute("""
                CREATE TABLE IF NOT EXISTS tiktok_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_line TEXT,
                    tiktok_id TEXT,
                    status TEXT,
                    checked_at TEXT
                )
            """)
            
            # MIGRATION
            _ensure_col(con, "keymaps", "group_name", "TEXT")
            _ensure_col(con, "keymaps", "provider_type", "TEXT NOT NULL DEFAULT 'mail72h'")
            _ensure_col(con, "keymaps", "base_url", "TEXT")
            _ensure_col(con, "keymaps", "api_key", "TEXT")
            
            try: con.execute("ALTER TABLE keymaps DROP COLUMN note")
            except: pass
            try: con.execute("ALTER TABLE keymaps RENAME COLUMN mail72h_api_key TO api_key")
            except: pass
            
            # KHỞI TẠO DỮ LIỆU MẶC ĐỊNH
            con.execute("DELETE FROM config WHERE key='current_proxy_string'")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_url", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_interval", "300"))
            
            con.commit()

            # AUTO RESTORE LOGIC
            keymap_count = con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0]
            if keymap_count == 0:
                print("WARNING: Database trống. Đang tìm backup...")
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        
                        keymaps_to_import = []
                        config_to_import = {}
                        proxies_to_import = []
                        local_stock_to_import = []

                        if isinstance(data, list):
                            keymaps_to_import = data
                        elif isinstance(data, dict):
                            keymaps_to_import = data.get('keymaps', [])
                            config_to_import = data.get('config', {})
                            proxies_to_import = data.get('proxies', [])
                            local_stock_to_import = data.get('local_stock', [])

                        for item in keymaps_to_import:
                            con.execute("""
                                INSERT OR IGNORE INTO keymaps(sku, input_key, product_id, is_active, group_name, provider_type, base_url, api_key) 
                                VALUES(?,?,?,?,?,?,?,?)
                            """, (item.get('sku'), item.get('input_key'), item.get('product_id'), item.get('is_active', 1), item.get('group_name', item.get('base_url', 'DEFAULT')), item.get('provider_type', 'mail72h'), item.get('base_url'), item.get('api_key')))

                        for key, value in config_to_import.items():
                            con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
                        
                        for item in proxies_to_import:
                            con.execute("INSERT OR IGNORE INTO proxies (proxy_string, is_live, latency, last_checked) VALUES (?, ?, ?, ?)", (item.get('proxy_string'), item.get('is_live', 0), item.get('latency', 9999.0), get_vn_time()))
                            
                        for item in local_stock_to_import:
                            con.execute("INSERT INTO local_stock (group_name, content, added_at) VALUES (?, ?, ?)", (item.get('group_name'), item.get('content'), item.get('added_at')))
                        
                        con.commit()
                        print("SUCCESS: Đã khôi phục dữ liệu!")
                    except Exception as e:
                        print(f"ERROR: Khôi phục thất bại. {e}")
            else:
                 print("INFO: Database đã có dữ liệu.")


# ==============================================================================
# ==============================================================================
#
#   PHẦN 4: XỬ LÝ PROXY (PROXY UTILS)
#
# ==============================================================================
# ==============================================================================

def format_proxy_url(proxy_string: str) -> dict:
    if not proxy_string:
        return {"http": None, "https": None}
    parts = proxy_string.split(':')
    formatted_proxy = ""
    if len(parts) == 2:
        ip, port = parts
        formatted_proxy = f"http://{ip}:{port}"
    elif len(parts) == 4:
        ip, port, user, passwd = parts
        formatted_proxy = f"http://{user}:{passwd}@{ip}:{port}"
    else:
        return {"http": None, "https": None}
    return {"http": formatted_proxy, "https": formatted_proxy}

def check_proxy_live(proxy_string: str) -> tuple:
    formatted_proxies = format_proxy_url(proxy_string)
    if not formatted_proxies.get("http"): return (0, 9999.0) 
    try:
        start_time = time.time()
        requests.get("http://www.google.com/generate_204", proxies=formatted_proxies, timeout=DEFAULT_TIMEOUT * 2)
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
                print(f"WARNING: Proxy {CURRENT_PROXY_STRING} died. Switching...")
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

LOGIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Đăng Nhập Quản Trị - Quantum Gate</title>
    <style>
        :root { 
            --primary: #5a7dff; --red: #f07167; --bg-light: #121212; --border: #343a40;
            --card-bg: #1c1c1e; --text-dark: #e9ecef; --text-light: #adb5bd; --input-bg: #2c2c2e;
            --shadow: 0 4px 12px rgba(0,0,0,0.4); --space-gradient-start: #0a0a1a;
            --space-gradient-end: #20204a; --star-color: #e0e0e0;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            color: var(--text-dark);
            background: linear-gradient(135deg, var(--space-gradient-start) 0%, var(--space-gradient-end) 100%);
            min-height: 100vh; display: flex; justify-content: center; align-items: center;
            margin: 0; position: relative; overflow: hidden;
        }
        
        .login-container {
            width: 100%; max-width: 400px; padding: 40px 30px; border-radius: 12px;
            background: var(--card-bg); box-shadow: var(--shadow); position: relative; z-index: 10;
            text-align: left; 
        }
        
        .header-info { display: flex; align-items: center; margin-bottom: 30px; flex-wrap: wrap; }
        
        .logo {
            width: 40px; height: 40px; background: linear-gradient(45deg, #3a86ff, #5a7dff);
            border-radius: 50%; display: flex; justify-content: center; align-items: center;
            font-size: 20px; color: white; margin-right: 15px; font-weight: bold;
            box-shadow: 0 0 10px rgba(90, 125, 255, 0.5);
        }
        
        .title-group { flex-grow: 1; line-height: 1.3; }
        
        .title-group p { margin: 0; font-size: 14px; color: var(--text-light); }
        
        h1 {
            font-size: 28px; font-weight: 700; color: var(--text-dark); margin: 0 0 10px 0;
        }
        
        .subtitle { font-size: 14px; color: var(--text-light); margin-bottom: 25px; }
        
        label {
            font-size: 14px; font-weight: 600; color: var(--text-dark); margin-bottom: 10px; display: block; text-align: left;
        }
        
        input {
            width: 100%; padding: 14px 16px; margin-bottom: 30px; border: 1px solid var(--border);
            border-radius: 10px; box-sizing: border-box; background: var(--input-bg);
            color: var(--text-dark); transition: border-color .2s, box-shadow .2s; font-size: 16px;
        }
        
        input:focus { border-color: var(--primary); box-shadow: 0 0 0 3px rgba(90, 125, 255, 0.25); outline: none; }
        
        button {
            width: 100%; padding: 15px 16px; border-radius: 10px; border: none;
            background: linear-gradient(90deg, #3a86ff, #5a7dff); color: #fff; cursor: pointer;
            font-weight: 700; font-size: 16px; box-shadow: 0 4px 15px rgba(90, 125, 255, 0.4);
            transition: opacity .2s, transform .1s; display: flex; justify-content: center; align-items: center;
        }
        
        button:hover { opacity: 0.9; transform: translateY(-1px); }
        
        .flash-alert { padding: 12px; margin-bottom: 20px; border-radius: 8px; font-weight: 600; background-color: #f8d7da; border-color: #f5c2c7; color: #842029; }
        
        #space-background { position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; z-index: 0; }
        
        .star { position: absolute; background-color: var(--star-color); border-radius: 50%; opacity: 0; animation: twinkle 5s infinite ease-in-out; z-index: 0; }
        
        @keyframes twinkle { 0%, 100% { opacity: 0; transform: scale(0.5); } 50% { opacity: 1; transform: scale(1.2); } }
    </style>
</head>
<body>
<div id="space-background"></div>
<div class="login-container">
    <div class="header-info"><div class="logo">∞</div><div><p style="font-size: 16px; font-weight: 600;">QUANTUM SECURITY GATE</p></div></div>
    <h1>Đăng nhập</h1>
    <p class="subtitle">Nhập mật khẩu quản trị để truy cập DashBoard.</p>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}{% for category, message in messages %}<div class="flash-alert {{ category }}">{{ message }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <form method="post" action="{{ url_for('login') }}"><input type="password" id="admin_secret" name="admin_secret" placeholder="Nhập mật khẩu..." required autofocus><button type="submit">🚀 Truy Cập</button></form>
</div>
<script>(function(){const s=document.getElementById('space-background');for(let i=0;i<100;i++){let d=document.createElement('div');d.className='star';d.style.width=Math.random()*3+'px';d.style.height=d.style.width;d.style.left=Math.random()*100+'%';d.style.top=Math.random()*100+'%';d.style.animationDelay=Math.random()*5+'s';s.appendChild(d)}})();</script>
</body>
</html>
"""

ADMIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Multi-Provider Admin Dashboard</title>
    <style>
    /* --- CẤU HÌNH MÀU SẮC & BIẾN TOÀN CỤC --- */
    :root { 
        --primary: #5a7dff; --green: #20c997; --red: #f07167; --blue: #3a86ff; --gray: #adb5bd;
        --shadow: 0 4px 12px rgba(0,0,0,0.2);
        --bg-light: #121212; --border: #343a40; --card-bg: #1c1c1e;
        --text-dark: #e9ecef; --text-light: #adb5bd; --input-bg: #2c2c2e;
        --code-bg: #343a40; --star-color: #e0e0e0;
    }

    /* Light Mode Variables */
    :root[data-theme="light"] {
        --primary: #0d6efd; --green: #198754; --red: #dc3545; --blue: #0d6efd; --gray: #6c757d;
        --shadow: 0 4px 12px rgba(0,0,0,0.05);
        --bg-light: #f8f9fa; --border: #dee2e6; --card-bg: #ffffff;
        --text-dark: #212529; --text-light: #495057; --input-bg: #ffffff;
        --code-bg: #e9ecef; --star-color: #888888;
    }

    /* --- BASE STYLES --- */
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        padding: 28px; color: var(--text-dark);
        background: linear-gradient(135deg, var(--bg-light) 0%, #20204a 100%);
        line-height: 1.6; min-height: 100vh; margin: 0; position: relative; overflow-x: hidden;
    }

    /* --- CARD COMPONENT --- */
    .card {
        border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px;
        background: var(--card-bg); box-shadow: var(--shadow); position: relative; z-index: 10;
    }

    /* --- GRID SYSTEM --- */
    .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; align-items: end; }
    .col-2 { grid-column: span 2; } .col-3 { grid-column: span 3; } .col-4 { grid-column: span 4; } .col-6 { grid-column: span 6; } .col-8 { grid-column: span 8; } .col-12 { grid-column: span 12; }

    /* --- FORM ELEMENTS --- */
    label { font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--text-light); margin-bottom: 6px; display: block; }
    input, select, textarea {
        width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px;
        box-sizing: border-box; background: var(--input-bg); color: var(--text-dark); font-size: 14px; transition: border-color 0.2s, box-shadow 0.2s; font-family: monospace;
    }
    input:focus { border-color: var(--primary); outline: none; box-shadow: 0 0 0 3px rgba(90, 125, 255, 0.25); }

    /* --- BUTTONS --- */
    button, .btn { padding: 10px 20px; border-radius: 8px; border: none; background: var(--primary); color: #fff; font-weight: 600; cursor: pointer; transition: filter 0.2s, transform 0.1s; }
    button:hover, .btn:hover { filter: brightness(1.1); transform: translateY(-1px); }
    .btn.red { background: var(--red); } .btn.green { background: var(--green); } .btn.blue { background: var(--blue); } .btn.gray { background: var(--gray); }
    .btn.small { padding: 6px 12px; font-size: 12px; }

    /* --- TABLES (DÙNG CHO LOCAL STOCK & PROXY) --- */
    table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }
    th, td { padding: 12px 15px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
    th { font-size: 12px; text-transform: uppercase; color: var(--text-light); letter-spacing: 0.5px; }
    
    /* --- NESTED DETAILS / SUMMARY (DÙNG CHO DANH SÁCH KEY) --- */
    details.folder { border: 1px solid var(--border); border-radius: 10px; margin-bottom: 15px; overflow: hidden; }
    details.folder > summary { padding: 15px 20px; cursor: pointer; font-weight: 700; font-size: 16px; background: var(--card-bg); color: var(--primary); list-style: none; }
    details.folder > .content { padding: 20px; background: var(--bg-light); border-top: 1px solid var(--border); }
    details.provider { margin-top: 15px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    details.provider > summary { padding: 12px 15px; cursor: pointer; font-weight: 600; font-size: 14px; background: #2a2a2d; color: #fff; }
    details.provider > .content { padding: 0; background: transparent; }

    /* Cấp 3: Bảng Key Chi Tiết (FIX WRAPPING) */
    .provider-table { width: 100%; border-collapse: collapse; }
    .provider-table th { background: #1f1f22; font-size: 11px; color: #aaa; padding: 10px 15px; border-bottom: 1px solid #333; }
    .provider-table td { border-bottom: 1px solid #333; padding: 10px 15px; font-size: 13px; color: #e0e0e0; white-space: nowrap; } /* FIX PRODUCT ID WRAP */
    
    /* FIX: SKU Truncation (Thu nhỏ lại và giữ trên 1 dòng) */
    .truncate-sku-cell {
        white-space: nowrap; 
        overflow: hidden; 
        max-width: 300px; 
        display: block; 
        font-size: 11px; /* Thu nhỏ chữ */
    }

    /* BADGES */
    .badge-key {
        display: inline-block; background: rgba(58, 134, 255, 0.15); color: #5a7dff; 
        padding: 4px 8px; border-radius: 4px; font-family: monospace; font-weight: bold;
        border: 1px solid rgba(58, 134, 255, 0.3); white-space: nowrap; /* GIỮ KEY TRÊN 1 DÒNG */
    }
    .badge-url { background: #343a40; color: #adb5bd; padding: 3px 6px; border-radius: 4px; font-size: 12px; font-family: monospace; }
    
    /* ANIMATIONS & UTILS */
    .space-background { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
    .star { position: absolute; background-color: var(--star-color); border-radius: 50%; opacity: 0; animation: twinkle 5s infinite; }
    .astronaut { position: absolute; width: 120px; height: 120px; background-image: url('https://freepng.flyclipart.com/thumb/cat-astronaut-space-suit-moon-outer-space-png-sticker-31913.png'); background-size: contain; animation: floatAstronaut 25s infinite ease-in-out; z-index: 1; opacity: 0.8; pointer-events: none; }
    .status-live { color: var(--green); font-weight: bold; }
    .status-dead { color: var(--red); font-weight: bold; }
    </style>
    
    <script>(function(){var m=document.cookie.split('; ').find(r=>r.startsWith('admin_mode='))?.split('=')[1]||'dark';document.documentElement.setAttribute('data-theme',m)})();</script>
</head>
<body>

{% if effect == 'astronaut' %}<div class="space-background" id="space-background"></div>{% endif %}

<div id="main-content" style="position: relative; z-index: 10;"> 
  
  {% with messages = get_flashed_messages(with_categories=true) %}
    {% if messages %}{% for category, message in messages %}<div class="flash-alert {{ category }}">{{ message }}</div>{% endfor %}{% endif %}
  {% endwith %}
  
  <h2>⚙️ Multi-Provider Admin Dashboard</h2>
  
  <div class="card" id="add-key-form-card">
    <h3>1. Thêm Key & Cấu Hình</h3>
    <form method="post" action="{{ url_for('admin_add_keymap') }}" id="main-key-form">
      <div class="row" style="margin-bottom: 20px;">
        <div class="col-4"><label>Group Name (Nhóm Website)</label><input class="mono" name="group_name" placeholder="VD: Netflix, Spotify..." required></div>
        <div class="col-4">
            <label>Provider Type (Loại)</label>
            <input class="mono" name="provider_type" list="ptypes" placeholder="mail72h / local" required oninput="checkProviderType(this)" id="pt_input">
            <datalist id="ptypes"><option value="mail72h"><option value="local"></datalist>
        </div>
        <div class="col-4" id="div_base_url"><label>Base URL (Nếu dùng API)</label><input class="mono" name="base_url" placeholder="https://api.website.com"></div>
      </div>
      
      <div class="row">
         <div class="col-2"><label>SKU</label><input class="mono" name="sku" required></div>
         <div class="col-3"><label>Input Key (Mã bán)</label><input class="mono" name="input_key" required></div>
         <div class="col-2" id="div_prod_id"><label>Product ID</label><input class="mono" name="product_id" placeholder="ID..."></div>
         <div class="col-3" id="div_api_key"><label>API Key (Nếu có)</label><input class="mono" name="api_key" type="password"></div>
         <div class="col-2"><button type="submit" style="width: 100%; height: 42px; margin-top: 20px;">Lưu Key</button></div>
      </div>
      
      <p style="font-size: 12px; color: var(--text-light); margin-top: 8px;">* <b>Lưu ý:</b> Nếu chọn Type là <b>local</b>, hệ thống sẽ lấy hàng từ "Kho Hàng Thủ Công" (Mục 4) dựa theo tên Group Name.</p>
    </form>
    
    <details style="margin-top: 15px; border-top: 1px dashed var(--border); padding-top: 10px;">
        <summary style="cursor: pointer; color: var(--green); font-weight: bold;">➕ Thêm Input Key Hàng Loạt (Dành cho Local)</summary>
        <form method="post" action="{{ url_for('admin_add_keymap_bulk') }}" style="margin-top: 15px;">
            <div class="row">
                <div class="col-4"><label>Group Name (Kho Hàng)</label><input class="mono" name="group_name" required placeholder="Nhập tên nhóm..."></div>
                <div class="col-4"><label>SKU Prefix (Optional)</label><input class="mono" name="sku_prefix" placeholder="VD: NF_"></div>
                <div class="col-4"><button type="submit" class="btn green" style="width: 100%; height: 42px; margin-top: 20px;">🚀 Thêm Ngay</button></div>
            </div>
            <label style="margin-top: 10px;">Danh sách Input Key (Mỗi dòng 1 key)</label>
            <textarea class="mono" name="bulk_keys" rows="5" placeholder="KEY_1&#10;KEY_2&#10;..." required></textarea>
        </form>
    </details>
  </div>

  <div class="card">
    <h3>2. Danh Sách Keymaps (Theo Website)</h3>
    {% if not grouped_data %}<p style="text-align: center; color: var(--text-light); padding: 20px;">Chưa có key nào được thêm.</p>{% endif %}

    {% for folder, providers in grouped_data.items() %}
      <details class="folder">
        <summary>📁 Website: {{ folder }}</summary>
        <div class="content">
          
          {% for provider, keys in providers.items() %}
            <details class="provider">
              <summary>📦 Provider: {{ provider }} ({{ keys|length }} keys)</summary>
              <div class="content">
                
                <table class="provider-table">
                  <thead>
                    <tr>
                      <th style="width: 25%;">SKU</th>
                      <th style="width: 25%;">INPUT KEY</th>
                      <th style="width: 20%;">BASE URL</th>
                      <th style="width: 5%;">ID</th>
                      <th style="width: 5%;">ACTIVE</th>
                      <th style="width: 20%;">HÀNH ĐỘNG</th>
                    </tr>
                  </thead>
                  <tbody>
                  {% for k in keys %}
                    <tr>
                      <td><span class="truncate-sku-cell">{{ k.sku }}</span></td>
                      <td><span class="badge-key">{{ k.input_key }}</span></td>
                      <td><span class="badge-url">{{ k.base_url }}</span></td>
                      <td>{{ k.product_id }}</td> 
                      <td>{% if k.is_active %}<span style="color: var(--green);">✅</span>{% else %}<span style="color: var(--red);">❌</span>{% endif %}</td>
                      <td> 
                        <div style="display: flex; gap: 5px;">
                            <button class="btn gray small edit-btn" 
                                    data-group="{{ k.group_name }}" data-provider="{{ k.provider_type }}" data-url="{{ k.base_url }}"
                                    data-sku="{{ k.sku }}" data-key="{{ k.input_key }}" data-pid="{{ k.product_id }}"
                                    data-apikey="{{ k.api_key }}"
                                    type="button">Sửa ✏️</button>
                            
                            <form method="post" action="{{ url_for('admin_toggle_key', kmid=k.id) }}" style="margin:0;"><button class="btn blue small" type="submit">{{ 'Tắt' if k.is_active else 'Bật' }}</button></form>
                            <form method="post" action="{{ url_for('admin_delete_key', kmid=k.id) }}" onsubmit="return confirm('Xác nhận xóa key này?');" style="margin:0;"><button class="btn red small" type="submit">Xoá</button></form>
                        </div>
                      </td>
                    </tr>
                  {% endfor %}
                  </tbody>
                </table>
                
                <button class="btn green small add-key-helper" 
                        style="margin: 10px;"
                        data-provider="{{ provider }}" 
                        data-baseurl="{{ keys[0]['base_url'] if keys else '' }}"
                        data-apikey="{{ keys[0]['api_key'] if keys else '' }}"
                        data-groupname="{{ folder }}">
                  + Thêm Key vào Provider này
                </button>
                
              </div>
            </details>
          {% endfor %}
          
        </div>
      </details>
    {% endfor %}
  </div>

  <div class="card">
    <h3>3. Backup & Restore</h3>
    <div class="row">
      <div class="col-6">
        <h4>Tải Backup (JSON)</h4>
        <p style="color: var(--text-light); margin-bottom: 15px;">Render sẽ xóa sạch dữ liệu khi Restart. Hãy tải file này thường xuyên và cập nhật vào <b>Secret File</b> trên Dashboard của Render.</p>
        <a href="{{ url_for('admin_backup_download') }}" class="btn green">⬇️ Tải Xuống Backup</a>
      </div>
      <div class="col-6" style="border-left: 1px solid var(--border); padding-left: 20px;">
        <h4>Restore Thủ Công</h4>
        <p style="color: var(--text-light); margin-bottom: 15px;">Upload file JSON để khôi phục dữ liệu ngay lập tức. Hành động này sẽ <b>GHI ĐÈ</b> toàn bộ dữ liệu hiện tại.</p>
        <form method="post" action="{{ url_for('admin_backup_upload') }}" enctype="multipart/form-data" onsubmit="return confirm('CẢNH BÁO: Hành động này sẽ XÓA SẠCH dữ liệu hiện tại và thay thế bằng file backup. Tiếp tục?');">
          <input type="file" name="backup_file" accept=".json" required style="margin-bottom: 10px;"><button type="submit" class="btn red">⬆️ Upload & Restore</button>
        </form>
      </div>
    </div>
  </div>

  <div class="row">
    <div class="col-6 card" id="local-stock">
        <h3 style="color: var(--green);">📦 4. Kho Hàng Thủ Công (Local Stock)</h3>
        
        <form method="post" action="{{ url_for('admin_local_stock_add') }}" enctype="multipart/form-data">
            <div style="margin-bottom: 15px;"><label>Group Name (Phải trùng với Keymap đã tạo)</label><input class="mono" name="group_name" list="group_hints" required placeholder="VD: Netflix"><datalist id="group_hints">{% for g in local_groups %}<option value="{{ g }}">{% endfor %}</datalist></div>
            
            <div class="row">
                <div class="col-6"><div style="border: 1px dashed var(--border); padding: 10px; border-radius: 6px;"><label style="color: var(--primary);">Cách 1: Upload File .txt</label><input type="file" name="stock_file" accept=".txt" class="mono" style="margin-top: 5px;"></div></div>
                <div class="col-6"><label>Cách 2: Dán Dữ Liệu (Mỗi dòng 1 acc)</label><textarea class="mono" name="content" rows="3" placeholder="user|pass..."></textarea></div>
            </div>
            
            <button type="submit" class="btn green" style="width: 100%; margin-top: 15px;">⬆️ Up Hàng Vào Kho</button>
        </form>
        
        <h4 style="margin-top: 25px; border-bottom: 1px solid var(--border); padding-bottom: 5px;">Thống Kê Tồn Kho</h4>
        <div style="max-height: 250px; overflow-y: auto;">
            {% for g, c in local_stats.items() %}
            <div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed var(--border); align-items: center;">
                <span><b style="color: var(--primary);">{{ g }}</b>: <span style="background: var(--input-bg); padding: 2px 6px; border-radius: 4px;">{{ c }} items</span></span>
                <div style="display: flex; gap: 5px; align-items: center;">
                    <input type="number" id="q_{{g}}" value="1" style="width: 60px; padding: 4px; margin: 0; height: 28px;" min="1">
                    <button class="btn green small" style="height: 28px; line-height: 1;" onclick="quickGet('{{g}}')">⚡ Lấy & Copy</button>
                    
                    <a href="{{ url_for('admin_local_stock_view', group=g) }}" class="btn blue small" style="height: 28px; display: flex; align-items: center;">Xem</a>
                    <form action="{{ url_for('admin_local_stock_clear') }}" method="post" style="display: inline;" onsubmit="return confirm('XÓA SẠCH kho {{g}}?');"><input type="hidden" name="group_name" value="{{ g }}"><button class="btn red small" style="height: 28px;">Xóa</button></form>
                </div>
            </div>
            {% else %}<p style="text-align: center; color: var(--text-light); padding: 10px;">Kho đang trống.</p>{% endfor %}
        </div>
    </div>

    <div class="col-6 card">
        <h3>5. Quản Lý Proxy & Ping</h3>
        
        <div style="margin-bottom: 15px;">Proxy Đang Dùng: <code class="mono" style="color: var(--green); font-size: 1.1em;">{{ current_proxy or 'Direct Connection' }}</code></div>
        
        <form method="post" action="{{ url_for('admin_add_proxy') }}">
            <label>Thêm Danh Sách Proxy (Mỗi dòng 1 cái: ip:port)</label>
            <textarea class="mono" name="proxies" rows="4" placeholder="ip:port&#10;ip:port:user:pass"></textarea>
            <button type="submit" class="btn green" style="margin-top: 10px; width: 100%;">➕ Thêm Proxy</button>
        </form>
        
        <div style="margin-top: 20px; max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px;">
            <table style="margin: 0;">
                <thead><tr><th>Proxy</th><th>Status</th><th>Ping</th><th>Xóa</th></tr></thead>
                <tbody>
                {% for p in proxies %}
                    <tr>
                        <td class="mono" style="font-size: 11px;">{{ p.proxy_string }}</td>
                        <td style="font-weight: bold; color: {{ 'var(--green)' if p.is_live else 'var(--red)' }};">
                            {{ 'LIVE' if p.is_live else 'DIE' }}
                        </td>
                        <td>{{ "%.2f"|format(p.latency) }}s</td>
                        <td>
                            <form action="{{ url_for('admin_delete_proxy') }}" method="post">
                                <input type="hidden" name="id" value="{{ p.id }}">
                                <button class="btn red small" style="padding: 2px 6px;">x</button>
                            </form>
                        </td>
                    </tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
        
        <hr style="border-color: var(--border); margin: 25px 0;">
        
        <h4>🌐 Cấu Hình Ping (Anti-Sleep)</h4>
        <p style="font-size: 0.9em; color: var(--text-light); margin-bottom: 10px;">
            Giúp Website không bị ngủ đông trên Render Free Tier.
        </p>
        <form method="post" action="{{ url_for('admin_save_ping') }}">
            <div class="row">
                <div class="col-8">
                    <label>URL Web (https://...)</label>
                    <input class="mono" name="ping_url" value="{{ ping.url }}" placeholder="https://myapp.onrender.com">
                </div>
                <div class="col-4">
                    <label>Chu kỳ Ping (Giây)</label>
                    <input class="mono" name="ping_interval" type="number" value="{{ ping.interval }}" placeholder="300">
                </div>
            </div>
            <button type="submit" class="btn blue" style="width: 100%; margin-top: 15px;">Lưu Cấu Hình</button>
        </form>
    </div>
  </div>

  <div class="card">
    <h3>6. TikTok Checker Tool</h3>
    <form method="post" action="{{ url_for('admin_tiktok_checker') }}">
        <div class="row">
            <div class="col-12">
                <label>Nhập List Cần Check (Mỗi dòng 1 ID hoặc user|pass... - Hệ thống tự lấy cột 1 làm ID)</label>
                <textarea name="tiktok_list" rows="5" class="mono" placeholder="tiktok_id_1&#10;tiktok_id_2|pass..." required></textarea>
            </div>
            <div class="col-12">
                <button type="submit" class="btn green" style="width: 100%; margin-top: 10px;">🔍 Check Live/Die Ngay</button>
            </div>
        </div>
    </form>
    
    <details style="margin-top: 15px; border-top: 1px dashed var(--border); padding-top: 10px;">
        <summary style="cursor: pointer; color: var(--blue);">📜 Lịch sử Check (5 ngày gần nhất)</summary>
        <div style="max-height: 300px; overflow-y: auto; margin-top: 10px;">
            <table style="margin: 0;">
                <thead><tr><th>ID</th><th>Status</th><th>Time</th></tr></thead>
                <tbody>
                {% for h in tiktok_history %}
                    <tr>
                        <td class="mono" style="font-size: 12px;">{{ h.tiktok_id }}</td>
                        <td style="font-weight: bold; color: {{ 'var(--green)' if h.status=='LIVE' else 'var(--red)' }};">{{ h.status }}</td>
                        <td style="font-size: 11px;">{{ h.checked_at }}</td>
                    </tr>
                {% else %}
                    <tr><td colspan="3" style="text-align: center;">Chưa có lịch sử.</td></tr>
                {% endfor %}
                </tbody>
            </table>
        </div>
    </details>
  </div>

  <div class="card" style="padding: 20px;">
    <div class="row" style="align-items: center;">
      <div class="col-4"><label>Giao diện</label><select id="mode-switcher" class="mono"><option value="dark" {% if mode == 'dark' %}selected{% endif %}>Tối (Dark)</option><option value="light" {% if mode == 'light' %}selected{% endif %}>Sáng (Light)</option></select></div>
      <div class="col-4"><label>Hiệu ứng nền</label><select id="effect-switcher" class="mono"><option value="default" {% if effect == 'default' %}selected{% endif %}>Tắt Hiệu Ứng</option><option value="astronaut" {% if effect == 'astronaut' %}selected{% endif %}>Phi hành gia (Astronaut)</option><option value="snow" {% if effect == 'snow' %}selected{% endif %}>Tuyết Rơi (Snow)</option><option value="matrix" {% if effect == 'matrix' %}selected{% endif %}>Ma Trận (Matrix)</option><option value="rain" {% if effect == 'rain' %}selected{% endif %}>Mưa Rơi (Rain)</option><option value="particles" {% if effect == 'particles' %}selected{% endif %}>Hạt Kết Nối (Particles)</option><option value="sakura" {% if effect == 'sakura' %}selected{% endif %}>Hoa Anh Đào (Sakura)</option></select></div>
      <div class="col-4"><label>&nbsp;</label><form method="post" action="{{ url_for('logout') }}"><button class="btn red" type="submit" style="width: 100%;">Đăng Xuất Hệ Thống</button></form></div>
    </div>
  </div>

  <div style="text-align: center; color: var(--text-light); margin-top: 20px; font-size: 13px;">
      Bản quyền thuộc về <strong style="color: var(--primary);">Admin Văn Linh</strong>
      <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 512 512" fill="#3a86ff" style="vertical-align: -2px; margin-left: 3px;">
          <path d="M256 0C114.6 0 0 114.6 0 256s114.6 256 256 256 256-114.6 256-256S397.4 0 256 0zM371.8 211.8l-128 128C238.3 345.3 231.2 348 224 348s-14.3-2.7-19.8-8.2l-64-64c-10.9-10.9-10.9-28.7 0-39.6 10.9-10.9 28.7-10.9 39.6 0l44.2 44.2 108.2-108.2c10.9-10.9 28.7-10.9 39.6 0 10.9 10.9 10.9 28.7 0 39.6z"/>
      </svg>
  </div>

</div> 

<script>
// Hàm ẩn/hiện field theo provider type
function checkProviderType(input) {
    const val = input ? input.value : document.getElementById('pt_input').value;
    const isLocal = val === 'local';
    document.getElementById('div_prod_id').style.display = isLocal ? 'none' : 'block';
    document.getElementById('div_base_url').style.display = isLocal ? 'none' : 'block';
    document.getElementById('div_api_key').style.display = isLocal ? 'none' : 'block';
}
checkProviderType();

// MỚI: HÀM LẤY NHANH STOCK VÀ AUTO COPY
async function quickGet(group) {
    let qtyInput = document.getElementById('q_' + group);
    let qty = qtyInput.value;
    if(qty < 1) { alert("Số lượng phải > 0"); return; }
    
    if(!confirm(`Bạn chắc chắn muốn lấy ${qty} acc từ nhóm ${group}?`)) return;
    
    try {
        let res = await fetch(`/admin/local-stock/quick-get?group=${encodeURIComponent(group)}&qty=${qty}`);
        if(res.ok) {
            let text = await res.text();
            if(!text) {
                alert("Kho hết hàng hoặc không đủ số lượng!");
                return;
            }
            // Auto Copy
            await navigator.clipboard.writeText(text);
            alert(`✅ Đã lấy ${qty} acc và COPY vào clipboard thành công!`);
            location.reload();
        } else {
            alert("Lỗi server!");
        }
    } catch(e) {
        alert("Lỗi: " + e);
    }
}

document.getElementById('effect-switcher').addEventListener('change', function() {
    document.cookie = `admin_effect=${this.value};path=/;max-age=31536000;SameSite=Lax`;
    location.reload();
});

document.getElementById('mode-switcher').addEventListener('change', function() {
    document.cookie = `admin_mode=${this.value};path=/;max-age=31536000;SameSite=Lax`;
    location.reload();
});

document.querySelectorAll('.edit-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelector('input[name="group_name"]').value = btn.dataset.group;
        document.querySelector('input[name="provider_type"]').value = btn.dataset.provider;
        document.querySelector('input[name="base_url"]').value = btn.dataset.url;
        document.querySelector('input[name="sku"]').value = btn.dataset.sku;
        document.querySelector('input[name="input_key"]').value = btn.dataset.key;
        document.querySelector('input[name="product_id"]').value = btn.dataset.pid;
        document.querySelector('input[name="api_key"]').value = btn.dataset.apikey; 
        checkProviderType(document.querySelector('input[name="provider_type"]'));
        document.getElementById('add-key-form-card').scrollIntoView({behavior: 'smooth'});
    });
});

document.querySelectorAll('.add-key-helper').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelector('input[name="group_name"]').value = btn.dataset.groupname;
        document.querySelector('input[name="provider_type"]').value = btn.dataset.provider;
        document.querySelector('input[name="base_url"]').value = btn.dataset.baseurl;
        document.querySelector('input[name="api_key"]').value = btn.dataset.apikey; 
        document.querySelector('input[name="sku"]').value = '';
        document.querySelector('input[name="input_key"]').value = '';
        document.querySelector('input[name="product_id"]').value = '';
        checkProviderType(document.querySelector('input[name="provider_type"]'));
        document.getElementById('add-key-form-card').scrollIntoView({behavior: 'smooth'});
    });
});

function createEffectCanvas(id) {
    if (document.getElementById(id)) return null; 
    var canvas = document.createElement('canvas');
    canvas.id = id;
    canvas.className = 'effect-canvas'; 
    document.body.appendChild(canvas);
    var ctx = canvas.getContext('2d');
    var W = window.innerWidth;
    var H = window.innerHeight;
    canvas.width = W;
    canvas.height = H;
    window.addEventListener('resize', function() {
        W = window.innerWidth;
        H = window.innerHeight;
        canvas.width = W;
        canvas.height = H;
    });
    return { canvas, ctx, W, H };
}
</script>

{% if effect == 'astronaut' %}
<script>
(function() {
    const spaceBackground = document.getElementById('space-background');
    if (!spaceBackground) return;
    for (let i = 0; i < 100; i++) {
        let star = document.createElement('div');
        star.className = 'star';
        star.style.width = star.style.height = `${Math.random() * 3 + 1}px`;
        star.style.left = `${Math.random() * 100}%`;
        star.style.top = `${Math.random() * 100}%`;
        star.style.animationDelay = `${Math.random() * 5}s`;
        spaceBackground.appendChild(star);
    }
    let astronaut = document.createElement('div');
    astronaut.className = 'astronaut';
    astronaut.style.left = '10%';
    astronaut.style.top = '20%';
    spaceBackground.appendChild(astronaut);
})();
</script>
{% endif %}

{% if effect == 'snow' %}
<script>
(function() {
    var a = createEffectCanvas('snow-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    var mp = 100; 
    var flakes = [];
    for(var i = 0; i < mp; i++) {
        flakes.push({
            x: Math.random() * W, y: Math.random() * H,
            r: Math.random() * 4 + 1, d: Math.random() * 100
        });
    }
    var angle = 0;
    function draw() {
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = "rgba(255, 255, 255, 0.8)";
        ctx.beginPath();
        for(var i = 0; i < 100; i++) {
            var f = flakes[i];
            ctx.moveTo(f.x, f.y);
            ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2, true);
        }
        ctx.fill();
        update();
        requestAnimationFrame(draw);
    }
    function update() {
        angle += 0.01;
        for(var i = 0; i < 100; i++) {
            var f = flakes[i];
            f.y += Math.cos(angle + f.d) + 1 + f.r / 2;
            f.x += Math.sin(angle) * 2;
            if(f.x > W + 5 || f.x < -5 || f.y > H) {
                if(i % 3 > 0) { flakes[i] = {x: Math.random() * W, y: -10, r: f.r, d: f.d}; }
                else {
                    if(Math.sin(angle) > 0) { flakes[i] = {x: -5, y: Math.random() * H, r: f.r, d: f.d}; }
                    else { flakes[i] = {x: W + 5, y: Math.random() * H, r: f.r, d: f.d}; }
                }
            }
        }
    }
    draw();
})();
</script>
{% endif %}

{% if effect == 'matrix' %}
<script>
(function() {
    var a = createEffectCanvas('matrix-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    var font_size = 14;
    var columns = Math.floor(W / font_size);
    var drops = [];
    for(var x = 0; x < columns; x++) drops[x] = 1; 
    var chars = "0123456789ABCDEF@#$%^&*()";
    chars = chars.split("");
    function draw() {
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = "rgba(0, 0, 0, 0.05)";
        ctx.fillRect(0, 0, W, H);
        ctx.fillStyle = "#0F0"; 
        ctx.font = font_size + "px monospace";
        for(var i = 0; i < drops.length; i++) {
            var text = chars[Math.floor(Math.random() * chars.length)];
            ctx.fillText(text, i * font_size, drops[i] * font_size);
            if(drops[i] * font_size > H && Math.random() > 0.975) {
                drops[i] = 0;
            }
            drops[i]++;
        }
    }
    setInterval(draw, 33);
})();
</script>
{% endif %}

{% if effect == 'rain' %}
<script>
(function() {
    var a = createEffectCanvas('rain-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    var drops = [];
    var dropCount = 500;
    for (var i = 0; i < dropCount; i++) {
        drops.push({
            x: Math.random() * W, 
            y: Math.random() * H, 
            l: Math.random() * 1, 
            v: Math.random() * 4 + 4
        });
    }
    function draw() {
        ctx.clearRect(0, 0, W, H);
        ctx.strokeStyle = "rgba(174, 194, 224, 0.5)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        for (var i = 0; i < dropCount; i++) {
            var d = drops[i];
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d.x, d.y + d.l * 5);
            d.y += d.v;
            if (d.y > H) {
                d.y = -20;
                d.x = Math.random() * W;
            }
        }
        ctx.stroke();
        requestAnimationFrame(draw);
    }
    draw();
})();
</script>
{% endif %}

{% if effect == 'particles' %}
<script>
(function() {
    var a = createEffectCanvas('particles-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    var particleCount = 80;
    var particles = [];
    for (var i = 0; i < particleCount; i++) {
        particles.push({
            x: Math.random() * W,
            y: Math.random() * H,
            vx: (Math.random() - 0.5) * 1,
            vy: (Math.random() - 0.5) * 1
        });
    }
    function draw() {
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = "rgba(200, 200, 200, 0.5)";
        ctx.strokeStyle = "rgba(200, 200, 200, 0.1)";
        for (var i = 0; i < particles.length; i++) {
            var p = particles[i];
            ctx.beginPath();
            ctx.arc(p.x, p.y, 2, 0, Math.PI * 2);
            ctx.fill();
            p.x += p.vx;
            p.y += p.vy;
            if (p.x < 0 || p.x > W) p.vx *= -1;
            if (p.y < 0 || p.y > H) p.vy *= -1;
            for (var j = i + 1; j < particles.length; j++) {
                var p2 = particles[j];
                var dx = p.x - p2.x;
                var dy = p.y - p2.y;
                var dist = Math.sqrt(dx * dx + dy * dy);
                if (dist < 100) {
                    ctx.beginPath();
                    ctx.moveTo(p.x, p.y);
                    ctx.lineTo(p2.x, p2.y);
                    ctx.stroke();
                }
            }
        }
        requestAnimationFrame(draw);
    }
    draw();
})();
</script>
{% endif %}

{% if effect == 'sakura' %}
<script>
(function() {
    var a = createEffectCanvas('sakura-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    var mp = 60;
    var petals = [];
    for(var i = 0; i < mp; i++) {
        petals.push({
            x: Math.random() * W, 
            y: Math.random() * H,
            r: Math.random() * 4 + 2, 
            d: Math.random() * mp,
            c: (Math.random() > 0.5) ? "#ffc0cb" : "#ffffff"
        });
    }
    var angle = 0;
    function draw() {
        ctx.clearRect(0, 0, W, H);
        for(var i = 0; i < 60; i++) {
            var p = petals[i];
            ctx.fillStyle = p.c;
            ctx.globalAlpha = 0.7;
            ctx.beginPath();
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2, true);
            ctx.fill();
        }
        angle += 0.01;
        for(var i = 0; i < 60; i++) {
            var p = petals[i];
            p.y += Math.cos(angle + p.d) + 1 + p.r / 2;
            p.x += Math.sin(angle);
            if(p.x > W + 5 || p.x < -5 || p.y > H) {
                p.x = Math.random() * W;
                p.y = -10;
            }
        }
        requestAnimationFrame(draw);
    }
    draw();
})();
</script>
{% endif %}

</body>
</html>
"""

# ------------------------------------------------------------------------------
# 7.3 TEMPLATE XEM CHI TIẾT KHO HÀNG (STOCK_VIEW_TPL) - CÓ SEARCH & DEDUP & DOWNLOAD
# ------------------------------------------------------------------------------
STOCK_VIEW_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Chi tiết kho {{ group }}</title>
    <style>
        body {
            background: #121212;
            color: #e9ecef;
            font-family: monospace;
            padding: 20px;
        }
        
        h2 { 
            color: #5a7dff; 
            border-bottom: 1px solid #333; 
            padding-bottom: 10px; 
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        a { 
            color: #5a7dff; 
            text-decoration: none; 
            font-size: 16px; 
        }
        
        a:hover { text-decoration: underline; }
        
        table { 
            width: 100%; 
            border-collapse: collapse; 
            margin-top: 20px; 
        }
        
        th, td { 
            border: 1px solid #333; 
            padding: 10px; 
            text-align: left; 
        }
        
        th { 
            background: #1c1c1e; 
            color: #adb5bd; 
        }
        
        tr:hover { background: #1c1c1e; }
        
        button {
            cursor: pointer;
            padding: 6px 12px;
            background: #dc3545;
            color: white;
            border: none;
            border-radius: 4px;
            font-weight: bold;
        }
        
        button:hover { background: #bb2d3b; }
        
        /* Search & Tools */
        .tools-bar {
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }
        
        input[type="text"] {
            padding: 8px;
            border-radius: 4px;
            border: 1px solid #444;
            background: #222;
            color: #fff;
            width: 300px;
        }
    </style>
</head>
<body>

    <h2>
        <span>📦 Group: {{ group }} ({{ items|length }} items)</span>
        <div>
             <a href="{{ url_for('admin_local_stock_download', group=group) }}" style="margin-right: 15px; font-size: 14px; background:#20c997; color:#000; padding:4px 8px; border-radius:4px; text-decoration:none;">📥 Tải File TXT</a>
             <a href="{{ url_for('admin_local_history_view') }}?group={{ group }}" style="margin-right: 15px; font-size: 14px;">📜 Xem Lịch Sử</a>
             <form action="{{ url_for('admin_local_stock_dedup') }}" method="post" style="display:inline;" onsubmit="return confirm('Bạn có chắc muốn xóa các dòng trùng lặp?');">
                <input type="hidden" name="group_name" value="{{ group }}">
                <button style="background: #ffc107; color: #000;">🧹 Quét Trùng</button>
             </form>
        </div>
    </h2>
    
    <div class="tools-bar">
        <a href="{{ url_for('admin_index') }}#local-stock">🔙 Quay lại Dashboard</a>
        <form method="get" style="margin-left: auto;">
            <input type="hidden" name="group" value="{{ group }}">
            <input type="text" name="q" placeholder="Tìm kiếm acc..." value="{{ request.args.get('q', '') }}">
            <button type="submit" style="background: #0d6efd;">Tìm</button>
        </form>
    </div>

    <table>
        <thead>
            <tr>
                <th style="width: 50px;">STT</th>
                <th>Nội dung (Tài khoản/Key)</th>
                <th style="width: 200px;">Ngày thêm (VN)</th>
                <th style="width: 100px;">Hành động</th>
            </tr>
        </thead>
        <tbody>
        {% for i in items %}
            <tr>
                <td>{{ loop.index }}</td>
                <td style="word-break: break-all; color: #20c997;">{{ i.content }}</td>
                <td>{{ i.added_at }}</td>
                <td>
                    <form action="{{ url_for('admin_local_stock_delete_one') }}" method="post" onsubmit="return confirm('Xóa dòng này?');">
                        <input type="hidden" name="id" value="{{ i.id }}">
                        <input type="hidden" name="group" value="{{ group }}">
                        <button type="submit">Xóa</button>
                    </form>
                </td>
            </tr>
        {% else %}
            <tr>
                <td colspan="4" style="text-align: center; padding: 30px; color: #adb5bd;">
                    Không tìm thấy dữ liệu phù hợp.
                </td>
            </tr>
        {% endfor %}
        </tbody>
    </table>

</body>
</html>
"""

# ------------------------------------------------------------------------------
# 7.4 TEMPLATE LỊCH SỬ LẤY HÀNG (HISTORY_VIEW_TPL - MỚI)
# ------------------------------------------------------------------------------
HISTORY_VIEW_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Lịch sử lấy hàng</title>
    <style>
        body { background: #121212; color: #e9ecef; font-family: monospace; padding: 20px; }
        h2 { color: #a0a0ff; border-bottom: 1px solid #333; padding-bottom: 10px; }
        a { color: #5a7dff; text-decoration: none; font-size: 16px; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { border: 1px solid #333; padding: 10px; text-align: left; }
        th { background: #1c1c1e; color: #adb5bd; }
        tr:hover { background: #1c1c1e; }
    </style>
</head>
<body>
    <h2>📜 Lịch Sử Xuất Kho ({{ group if group else 'Tất Cả' }})</h2>
    <a href="{{ url_for('admin_local_stock_view', group=group) if group else url_for('admin_index') }}">🔙 Quay lại</a>

    <table>
        <thead>
            <tr>
                <th style="width: 50px;">ID</th>
                <th>Group</th>
                <th>Nội dung đã lấy</th>
                <th style="width: 200px;">Thời gian lấy (VN)</th>
            </tr>
        </thead>
        <tbody>
        {% for i in items %}
            <tr>
                <td>{{ i.id }}</td>
                <td>{{ i.group_name }}</td>
                <td style="word-break: break-all; color: #ffc107;">{{ i.content }}</td>
                <td>{{ i.fetched_at }}</td>
            </tr>
        {% else %}
            <tr><td colspan="4" style="text-align: center; padding: 30px; color: #adb5bd;">Chưa có lịch sử nào.</td></tr>
        {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""

# ------------------------------------------------------------------------------
# 7.5 MỚI: TEMPLATE KẾT QUẢ TIKTOK CHECKER (ĐÃ SỬA THEO YÊU CẦU)
# ------------------------------------------------------------------------------
TIKTOK_RESULT_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Kết Quả Check TikTok</title>
    <style>
        body { background: #121212; color: #e9ecef; font-family: monospace; padding: 20px; }
        .box { background: #1c1c1e; padding: 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #333; position: relative; }
        h2 { color: #5a7dff; margin-top: 0; }
        textarea { width: 100%; background: #2c2c2e; border: 1px solid #333; padding: 10px; height: 300px; font-family: monospace; }
        .stats { display: flex; gap: 20px; font-size: 18px; margin-bottom: 10px; }
        .live { color: #20c997; font-weight: bold; }
        .die { color: #dc3545; font-weight: bold; }
        .live-area { color: #20c997; }
        .die-area { color: #f07167; }
        
        .copy-btn {
            background: #5a7dff; color: white; border: none; padding: 5px 10px; border-radius: 4px; 
            cursor: pointer; font-size: 14px; margin-left: 10px; font-weight: bold;
        }
        .copy-btn:hover { opacity: 0.9; }
    </style>
    <script>
        function copyToClipboard(elementId) {
            var copyText = document.getElementById(elementId);
            copyText.select();
            copyText.setSelectionRange(0, 99999); 
            navigator.clipboard.writeText(copyText.value).then(function() {
                alert("Đã copy thành công!");
            }, function(err) {
                alert("Lỗi copy: " + err);
            });
        }
    </script>
</head>
<body>
    <h2>🔍 Kết Quả Check TikTok</h2>
    <a href="{{ url_for('admin_index') }}" style="color:#5a7dff; text-decoration:none;">🔙 Quay lại Dashboard</a>
    <br><br>
    
    <div class="stats">
        <span class="live">✅ LIVE: {{ live_count }}</span>
        <span class="die">❌ DIE: {{ die_count }}</span>
    </div>

    <div class="box">
        <h3 class="live">DANH SÁCH LIVE <button class="copy-btn" onclick="copyToClipboard('live_area')">Copy LIVE</button></h3>
        <textarea id="live_area" class="live-area" readonly>{{ live_content }}</textarea>
    </div>

    <div class="box">
        <h3 class="die">DANH SÁCH DIE <button class="copy-btn" onclick="copyToClipboard('die_area')">Copy DIE</button></h3>
        <textarea id="die_area" class="die-area" readonly>{{ die_content }}</textarea>
    </div>

</body>
</html>
"""


# ==============================================================================
# ------------------------------------------------------------------------------
#
#   PHẦN 8: FLASK ROUTES & CONTROLLERS (XỬ LÝ REQUEST)
#
# ------------------------------------------------------------------------------
# ==============================================================================

def find_map_by_key(key: str):
    """Tìm kiếm thông tin sản phẩm dựa trên Input Key"""
    with db() as con:
        row = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (key,)).fetchone()
        return row

def require_admin():
    """Middleware kiểm tra quyền Admin"""
    if request.cookies.get("logged_in") != ADMIN_SECRET:
        abort(redirect(url_for('login')))

@app.route("/", methods=["GET", "POST"])
def login():
    """Trang đăng nhập"""
    if request.method == "POST":
        secret = request.form.get("admin_secret")
        if secret == ADMIN_SECRET:
            response = make_response(redirect(url_for("admin_index")))
            # Cookie sống 1 năm
            response.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000, httponly=True, secure=True, samesite='Lax')
            return response
        else:
            flash("Mật khẩu Admin không chính xác. Vui lòng thử lại.", "error")
            return render_template_string(LOGIN_TPL)
    
    # Nếu đã login thì chuyển thẳng vào admin
    if request.cookies.get("logged_in") == ADMIN_SECRET:
        return redirect(url_for("admin_index"))
        
    return render_template_string(LOGIN_TPL)

@app.route("/logout", methods=["POST"])
def logout():
    """Đăng xuất"""
    response = make_response(redirect(url_for("login")))
    response.set_cookie("logged_in", "", max_age=0) 
    return response

@app.route("/admin")
def admin_index():
    """Trang Dashboard chính"""
    require_admin() 

    with db() as con:
        # 1. Lấy danh sách Keymaps
        maps = con.execute("SELECT * FROM keymaps ORDER BY group_name, provider_type, sku, id").fetchall()
        
        # Gom nhóm dữ liệu: Website -> Provider -> Key List
        # SỬ DỤNG LIST ĐỂ ĐẢM BẢO HIỂN THỊ ĐỦ TẤT CẢ KEY
        grouped_data = {}
        for key in maps:
            folder = key['group_name'] or 'DEFAULT' 
            provider = key['provider_type']
            
            if folder not in grouped_data:
                grouped_data[folder] = {}
            
            if provider not in grouped_data[folder]:
                grouped_data[folder][provider] = [] # Khởi tạo là List
            
            grouped_data[folder][provider].append(key) # Append vào list
        
        # 2. Lấy danh sách Proxy (Để hiển thị bảng)
        proxies = con.execute("SELECT * FROM proxies ORDER BY is_live DESC, latency ASC").fetchall()

        # 3. Lấy cấu hình Ping
        ping_url = con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()
        ping_int = con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()
        ping_config = {
            "url": ping_url['value'] if ping_url else "", 
            "interval": ping_int['value'] if ping_int else 300
        }

        # 4. Lấy thống kê Local Stock
        stock_rows = con.execute("SELECT group_name, COUNT(*) as cnt FROM local_stock GROUP BY group_name").fetchall()
        local_stats = {r['group_name']: r['cnt'] for r in stock_rows}
        
        # Tạo danh sách group để gợi ý input
        local_groups = [r['group_name'] for r in stock_rows]

        # 5. MỚI: Lấy lịch sử TikTok (50 dòng mới nhất)
        tiktok_history = con.execute("SELECT * FROM tiktok_history ORDER BY id DESC LIMIT 50").fetchall()

    # Lấy setting giao diện từ Cookie
    effect = request.cookies.get('admin_effect', 'astronaut')
    mode = request.cookies.get('admin_mode', 'dark') 
    
    return render_template_string(ADMIN_TPL, 
                                  grouped_data=grouped_data, 
                                  proxies=proxies, 
                                  current_proxy=CURRENT_PROXY_STRING, 
                                  ping=ping_config, 
                                  local_stats=local_stats,
                                  local_groups=local_groups,
                                  tiktok_history=tiktok_history,
                                  effect=effect,
                                  mode=mode)

# ------------------------------------------------------------------------------
# ROUTES: QUẢN LÝ KEYMAP
# ------------------------------------------------------------------------------
@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    f = request.form
    
    group_name = f.get("group_name", "").strip()
    sku = f.get("sku", "").strip()
    input_key = f.get("input_key", "").strip()
    product_id = f.get("product_id", "").strip()
    provider_type = f.get("provider_type", "").strip()
    base_url = f.get("base_url", "").strip()
    api_key = f.get("api_key", "").strip()
    
    if not input_key or not provider_type:
        flash("Lỗi: Thiếu thông tin bắt buộc.", "error")
        return redirect(url_for("admin_index"))
    
    # FIX: Nếu local thì id = 0, để tránh lỗi
    if provider_type == 'local': 
        product_id = 0
        
    try:
        with db() as con:
            con.execute("""
                INSERT INTO keymaps(group_name, sku, input_key, product_id, api_key, is_active, provider_type, base_url)
                VALUES(?,?,?,?,?,1,?,?)
                ON CONFLICT(input_key) DO UPDATE SET
                  group_name=excluded.group_name,
                  sku=excluded.sku,
                  product_id=excluded.product_id,
                  api_key=excluded.api_key,
                  provider_type=excluded.provider_type,
                  base_url=excluded.base_url,
                  is_active=1
            """, (group_name, sku, input_key, product_id, api_key, provider_type, base_url))
            con.commit()
        flash(f"Đã lưu key '{input_key}' thành công!", "success")
    except Exception as e:
        flash(f"Lỗi Database: {e}", "error")
        
    return redirect(url_for("admin_index"))

# NEW: Route xử lý thêm key hàng loạt cho Local
@app.route("/admin/keymap/bulk", methods=["POST"])
def admin_add_keymap_bulk():
    require_admin()
    f = request.form
    grp = f.get("group_name", "").strip()
    prefix = f.get("sku_prefix", "").strip()
    keys_raw = f.get("bulk_keys", "").strip()
    
    if not grp or not keys_raw:
        flash("Thiếu tên Group hoặc danh sách Key", "error")
        return redirect(url_for("admin_index"))
    
    cnt = 0
    with db() as con:
        for k in keys_raw.split('\n'):
            k = k.strip()
            if k:
                sku = f"{prefix}{k}" if prefix else k
                try:
                    con.execute("""
                        INSERT INTO keymaps(group_name, sku, input_key, product_id, is_active, provider_type, base_url, api_key)
                        VALUES(?,?,?,0,1,'local','','')
                        ON CONFLICT(input_key) DO NOTHING
                    """, (grp, sku, k))
                    cnt += 1
                except:
                    pass
        con.commit()
    flash(f"Đã thêm {cnt} key hàng loạt vào nhóm '{grp}'", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/keymap/delete/<int:kmid>", methods=["POST"])
def admin_delete_key(kmid):
    require_admin()
    with db() as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (kmid,))
        con.commit()
    flash("Đã xóa key thành công.", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/keymap/toggle/<int:kmid>", methods=["POST"])
def admin_toggle_key(kmid):
    require_admin()
    with db() as con:
        row = con.execute("SELECT is_active FROM keymaps WHERE id=?", (kmid,)).fetchone()
        if row:
            new_val = 0 if row['is_active'] else 1
            con.execute("UPDATE keymaps SET is_active=? WHERE id=?", (new_val, kmid))
            con.commit()
    return redirect(url_for("admin_index"))


# ------------------------------------------------------------------------------
# ROUTES: QUẢN LÝ LOCAL STOCK
# ------------------------------------------------------------------------------
@app.route("/admin/local-stock/add", methods=["POST"])
def admin_local_stock_add():
    require_admin()
    grp = request.form.get("group_name", "").strip()
    content = request.form.get("content", "").strip()
    file = request.files.get("stock_file")
    
    if not grp:
        flash("Thiếu tên Group.", "error")
        return redirect(url_for("admin_index") + "#local-stock")
    
    lines = []
    # Ưu tiên đọc file TXT
    if file and file.filename:
        try:
            lines = file.read().decode('utf-8', errors='ignore').splitlines()
        except Exception as e:
            flash(f"Lỗi đọc file: {e}", "error")
            return redirect(url_for("admin_index") + "#local-stock")
    # Nếu không có file thì đọc từ ô text
    elif content:
        lines = content.split('\n')
    
    count = 0
    if lines:
        with db() as con:
            now = get_vn_time() # Dùng giờ Việt Nam
            for line in lines:
                line = line.strip()
                if line:
                    con.execute("INSERT INTO local_stock(group_name, content, added_at) VALUES(?,?,?)", (grp, line, now))
                    count += 1
            con.commit()
        
    flash(f"Đã thêm {count} dòng vào kho '{grp}'.", "success")
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/local-stock/view")
def admin_local_stock_view():
    require_admin()
    grp = request.args.get("group")
    query = request.args.get("q", "").strip() # Lấy từ khóa tìm kiếm
    
    with db() as con:
        if query:
            # Tìm kiếm gần đúng (LIKE)
            items = con.execute("SELECT * FROM local_stock WHERE group_name=? AND content LIKE ?", (grp, f"%{query}%")).fetchall()
        else:
            items = con.execute("SELECT * FROM local_stock WHERE group_name=?", (grp,)).fetchall()
            
    return render_template_string(STOCK_VIEW_TPL, group=grp, items=items, request=request)

@app.route("/admin/local-stock/download")
def admin_local_stock_download():
    require_admin()
    grp = request.args.get("group")
    with db() as con:
        rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (grp,)).fetchall()
    
    # Xuất ra file .txt, mỗi dòng là 1 content
    out = "\n".join([r['content'] for r in rows])
    resp = make_response(out)
    
    # FIX: Tên file tiếng Việt
    filename = f"stock_{grp}.txt"
    try:
        encoded_filename = quote(filename)
        resp.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
    except:
        # Fallback nếu lỗi encode
        resp.headers["Content-Disposition"] = f"attachment; filename=stock_download.txt"
        
    resp.headers["Content-Type"] = "text/plain; charset=utf-8"
    return resp

# MỚI: ROUTE LẤY NHANH HÀNG (AJAX)
@app.route("/admin/local-stock/quick-get")
def admin_local_stock_quick_get():
    require_admin()
    grp = request.args.get("group")
    try:
        qty = int(request.args.get("qty", 1))
    except:
        return "Invalid Quantity", 400

    # Tái sử dụng hàm fetch (tự động xóa và lưu lịch sử)
    items = fetch_local_stock(grp, qty)
    
    # Trả về text thuần để JS copy
    if not items:
        return "", 200 # Hết hàng

    out = "\n".join([i['product'] for i in items])
    return out, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route("/admin/local-history/view")
def admin_local_history_view():
    require_admin()
    grp = request.args.get("group")
    with db() as con:
        if grp:
            items = con.execute("SELECT * FROM local_history WHERE group_name=? ORDER BY id DESC LIMIT 500", (grp,)).fetchall()
        else:
            items = con.execute("SELECT * FROM local_history ORDER BY id DESC LIMIT 500").fetchall()
    return render_template_string(HISTORY_VIEW_TPL, group=grp, items=items)

@app.route("/admin/local-stock/dedup", methods=["POST"])
def admin_local_stock_dedup():
    require_admin()
    grp = request.form.get("group_name")
    with db() as con:
        # Xóa các dòng trùng lặp, chỉ giữ lại dòng có ID nhỏ nhất
        con.execute("""
            DELETE FROM local_stock 
            WHERE group_name=? 
            AND id NOT IN (
                SELECT MIN(id) 
                FROM local_stock 
                WHERE group_name=? 
                GROUP BY content
            )
        """, (grp, grp))
        con.commit()
    flash(f"Đã quét trùng xong cho nhóm {grp}.", "success")
    return redirect(url_for("admin_local_stock_view", group=grp))

@app.route("/admin/local-stock/delete-one", methods=["POST"])
def admin_local_stock_delete_one():
    require_admin()
    mid = request.form.get("id")
    grp = request.form.get("group")
    with db() as con:
        con.execute("DELETE FROM local_stock WHERE id=?", (mid,))
        con.commit()
    return redirect(url_for("admin_local_stock_view", group=grp))

@app.route("/admin/local-stock/clear", methods=["POST"])
def admin_local_stock_clear():
    require_admin()
    grp = request.form.get("group_name")
    with db() as con:
        con.execute("DELETE FROM local_stock WHERE group_name=?", (grp,))
        con.commit()
    flash(f"Đã xóa sạch kho '{grp}'.", "success")
    return redirect(url_for("admin_index") + "#local-stock")

# ------------------------------------------------------------------------------
# MỚI: ROUTE TIKTOK CHECKER
# ------------------------------------------------------------------------------
def check_tiktok_live_status(tiktok_id):
    """
    Check theo nội dung trang web:
    - Chỉ báo DIE khi trong HTML có chuỗi báo lỗi "Không thể tìm thấy..."
    - Còn lại trả về LIVE (bao gồm cả lỗi mạng, proxy, captcha... thà báo nhầm là Live còn hơn xóa nhầm acc ngon)
    """
    url = f"https://www.tiktok.com/@{tiktok_id}"
    proxies = CURRENT_PROXY_SET
    try:
        # Thêm Accept-Language để TikTok ưu tiên trả về tiếng Việt/Anh để dễ bắt key
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36',
            'Accept-Language': 'vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7'
        }
        
        # Tăng timeout lên một chút để tránh lỗi mạng
        r = requests.get(url, headers=headers, proxies=proxies, timeout=10)
        
        content = r.text

        # CÁC DẤU HIỆU NHẬN BIẾT ACC DIE CHUẨN XÁC:
        if "Không thể tìm thấy tài khoản này" in content:
            return "DIE"
        if "Couldn't find this account" in content:
            return "DIE"
        if "user-not-found" in content: # Class CSS thường thấy khi die
            return "DIE"

        # Nếu không tìm thấy các dòng trên -> Acc LIVE (hoặc bị Captcha, cứ cho là LIVE)
        return "LIVE"

    except Exception as e:
        # Lỗi mạng, lỗi Proxy -> Trả về LIVE để giữ lại acc check sau
        # print(f"Lỗi check {tiktok_id}: {e}")
        return "LIVE"

@app.route("/admin/tiktok/check", methods=["POST"])
def admin_tiktok_checker():
    require_admin()
    raw_list = request.form.get("tiktok_list", "").strip()
    
    if not raw_list:
        flash("Vui lòng nhập danh sách cần check!", "error")
        return redirect(url_for("admin_index"))

    lines = raw_list.split('\n')
    live_lines = []
    die_lines = []
    
    live_count = 0
    die_count = 0
    
    with db() as con:
        # Xóa lịch sử cũ hơn 5 ngày
        con.execute("DELETE FROM tiktok_history WHERE checked_at < date('now', '-5 days')")
        
        now = get_vn_time()
        
        for line in lines:
            line = line.strip()
            if not line: continue

            # --- BƯỚC 1: LÀM SẠCH (CLEAN) DỮ LIỆU ĐẦU VÀO ---
            # Xóa [DIE], [LIVE] ở đầu dòng nếu có
            clean_line = re.sub(r'^\[(DIE|LIVE)\]\s*', '', line)
            # Xóa (Not Found) ở cuối dòng nếu có
            clean_line = clean_line.replace('(Not Found)', '').strip()

            # --- BƯỚC 2: TÁCH TIKTOK ID ---
            # Tách bằng | hoặc khoảng trắng
            if "|" in clean_line:
                parts = clean_line.split('|')
            else:
                parts = clean_line.split()
            
            tiktok_id = parts[0].strip()
            if not tiktok_id: continue
            
            # --- BƯỚC 3: CHECK STATUS ---
            status = check_tiktok_live_status(tiktok_id)
            
            # Lưu DB (Lưu dòng gốc để đối chiếu nếu cần)
            con.execute("INSERT INTO tiktok_history(input_line, tiktok_id, status, checked_at) VALUES(?,?,?,?)", (clean_line, tiktok_id, status, now))
            
            if status == "LIVE":
                live_count += 1
                live_lines.append(clean_line) # Chỉ thêm dòng sạch vào list kết quả
            else:
                die_count += 1
                die_lines.append(clean_line)  # Chỉ thêm dòng sạch vào list kết quả
        
        con.commit()

    live_content = "\n".join(live_lines)
    die_content = "\n".join(die_lines)
    
    return render_template_string(TIKTOK_RESULT_TPL, 
                                  live_count=live_count, 
                                  die_count=die_count, 
                                  live_content=live_content,
                                  die_content=die_content)


# ------------------------------------------------------------------------------
# ROUTES: QUẢN LÝ PROXY
# ------------------------------------------------------------------------------
@app.route("/admin/proxy/add", methods=["POST"])
def admin_add_proxy():
    require_admin()
    blob = request.form.get("proxies", "").strip()
    count = 0
    
    with db() as con:
        for line in blob.split('\n'):
            line = line.strip()
            if line:
                con.execute("INSERT OR IGNORE INTO proxies (proxy_string, is_live, last_checked) VALUES (?, 0, ?)", (line, get_vn_time()))
                count += 1
        con.commit()
        
        if not CURRENT_PROXY_STRING:
            select_best_available_proxy(con)
            
    flash(f"Đã thêm {count} proxy vào hệ thống.", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/proxy/delete", methods=["POST"])
def admin_delete_proxy():
    require_admin()
    with db() as con:
        con.execute("DELETE FROM proxies WHERE id=?", (request.form.get("id"),))
        con.commit()
    return redirect(url_for("admin_index"))


# ------------------------------------------------------------------------------
# ROUTES: QUẢN LÝ PING (ANTI-SLEEP)
# ------------------------------------------------------------------------------
@app.route("/admin/ping/save", methods=["POST"])
def admin_save_ping():
    require_admin()
    url = request.form.get("ping_url", "").strip()
    interval = request.form.get("ping_interval", "300").strip()
    
    with db() as con:
        con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('ping_url', ?)", (url,))
        con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('ping_interval', ?)", (interval,))
        con.commit()
        
    flash("Đã lưu cấu hình Ping Service.", "success")
    return redirect(url_for("admin_index"))


# ------------------------------------------------------------------------------
# ROUTES: BACKUP & RESTORE
# ------------------------------------------------------------------------------
@app.route("/admin/backup/download")
def admin_backup_download():
    require_admin()
    perform_backup_to_file()
    if os.path.exists(AUTO_BACKUP_FILE):
        with open(AUTO_BACKUP_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            data['export_time'] = get_vn_time()
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            response = jsonify(data)
            response.headers['Content-Disposition'] = f'attachment; filename=full_backup_{timestamp}.json'
            return response
    return "Chưa có dữ liệu backup.", 404

@app.route("/admin/backup/upload", methods=["POST"])
def admin_backup_upload():
    require_admin()
    file = request.files.get('backup_file')
    if file and file.filename.endswith('.json'):
        try:
            data = json.load(file)
            with db() as con:
                con.execute("DELETE FROM keymaps"); con.execute("DELETE FROM proxies"); con.execute("DELETE FROM local_stock")
                
                kms = data.get('keymaps', []) if isinstance(data, dict) else data
                pxs = data.get('proxies', []) if isinstance(data, dict) else []
                lcs = data.get('local_stock', []) if isinstance(data, dict) else []
                cfg = data.get('config', {}) if isinstance(data, dict) else {}

                for k in kms: con.execute("INSERT INTO keymaps(sku,input_key,product_id,is_active,group_name,provider_type,base_url,api_key) VALUES(?,?,?,?,?,?,?,?)", (k.get('sku'), k.get('input_key'), k.get('product_id'), k.get('is_active',1), k.get('group_name'), k.get('provider_type'), k.get('base_url'), k.get('api_key')))
                for p in pxs: con.execute("INSERT OR IGNORE INTO proxies(proxy_string, is_live, latency, last_checked) VALUES(?,?,?,?)", (p.get('proxy_string'), 0, 9999.0, get_vn_time()))
                for l in lcs: con.execute("INSERT INTO local_stock(group_name, content, added_at) VALUES(?,?,?)", (l.get('group_name'), l.get('content'), l.get('added_at')))
                for k, v in cfg.items(): con.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (k, str(v)))
                con.commit()
            flash("Restore thành công", "success")
        except Exception as e: flash(f"Lỗi khôi phục: {e}", "error")
    return redirect(url_for("admin_index"))


# ==============================================================================
# ------------------------------------------------------------------------------
#
#   PHẦN 9: PUBLIC API (CHO NGƯỜI MUA)
#
# ------------------------------------------------------------------------------
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
    key = request.args.get("key", "").strip(); qty_s = request.args.get("quantity", "").strip()
    try: qty = int(qty_s)
    except: return jsonify([])
    row = find_map_by_key(key)
    if not row or qty<=0: return jsonify([])
    if row['provider_type']=='local': return jsonify(fetch_local_stock(row['group_name'], qty))
    return fetch_mail72h_format(row, qty)

@app.route("/health")
def health():
    return "OK", 200


# ==============================================================================
# ------------------------------------------------------------------------------
#
#   PHẦN 10: KHỞI ĐỘNG (STARTUP)
#
# ------------------------------------------------------------------------------
# ==============================================================================

# QUAN TRỌNG: Chạy init_db() ngay khi file được import (để Gunicorn trên Render chạy nó)
print("INFO: Đang khởi tạo Database...")
init_db() 

# Khởi động các luồng chạy nền (Proxy checker, Ping, Backup)
if not proxy_checker_started:
    start_proxy_checker_once() 
if not ping_service_started:
    start_ping_service()
if not auto_backup_started:
    start_auto_backup()

# Logic khôi phục Proxy (chỉ chạy 1 lần khi khởi động)
try:
    with db() as con_startup:
        manual_proxy_choice = load_selected_proxy_from_db(con_startup)
        if manual_proxy_choice:
            print(f"INFO: Đang khôi phục proxy đã lưu: {manual_proxy_choice}")
            is_live, latency = check_proxy_live(manual_proxy_choice)
            if is_live:
                set_current_proxy_by_string(manual_proxy_choice)
                update_proxy_state(manual_proxy_choice, is_live, latency)
            else:
                print("WARNING: Proxy đã lưu bị chết. Đang quét lại...")
                run_initial_proxy_scan_and_select()
        else:
            run_initial_proxy_scan_and_select()
except Exception as e:
    print(f"STARTUP ERROR (Non-critical): {e}")

# Block này chỉ chạy khi bạn test trên máy tính (python app.py)
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    print(f"🚀 SERVER STARTED ON PORT {port}")
    app.run(host="0.0.0.0", port=port)
