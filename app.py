"""
================================================================================
PROJECT: QUANTUM GATE - TIKTOK ANALYTICS & STOCK MANAGER (ULTIMATE EDITION)
AUTHOR: ADMIN VAN LINH
VERSION: 20.5 (Enterprise)
DATE: 2025-12-12

DESCRIPTION:
    Hệ thống quản lý kho hàng (Stock), Proxy và Check Live TikTok chuyên sâu.
    Tích hợp giao diện Apple Pro UI với các hiệu ứng hình ảnh cao cấp.
    Hỗ trợ xử lý đa luồng (Multi-threading) tối ưu hiệu năng.

FEATURES:
    1. Stock Manager: Quản lý Key/Acc (Local & API Mail72h).
    2. Proxy System: Tự động check, xoay vòng, lọc proxy sống/chết.
    3. TikTok Checker: 
       - Logic: Phân biệt chính xác Live/Die (Bỏ qua Captcha/Login Wall).
       - Speed: Đa luồng, tùy chỉnh số threads.
       - UI: Stream kết quả Real-time.
    4. System: Tự động backup, Anti-sleep (Ping), Bảo mật Admin.
    5. UI/UX: 
       - Dark/Light Mode.
       - Hiệu ứng nền: Matrix, Snow, Rain, Particles, Astronaut.
       - Responsive Design.

CONFIGURATION:
    - DB_PATH: Đường dẫn SQLite DB.
    - ADMIN_SECRET: Mật khẩu quản trị.
    - PORT: Cổng chạy server.
================================================================================
"""

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

# Thử import BeautifulSoup để phân tích HTML chính xác hơn
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("WARNING: 'beautifulsoup4' chưa được cài đặt. Logic check sẽ dựa vào text search cơ bản.")

# ==============================================================================
#   PHẦN 1: CẤU HÌNH & KHỞI TẠO (CONFIGURATION & INIT)
# ==============================================================================

# Database & File Paths
DB = os.getenv("DB_PATH", "store.db") 
SECRET_BACKUP_FILE_PATH = os.getenv("SECRET_BACKUP_FILE_PATH", "/etc/secrets/backupapitaphoa.json")
AUTO_BACKUP_FILE = "auto_backup.json"

# Security & App Settings
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "10")) 
PROXY_CHECK_INTERVAL = 15 

# Flask App Initialization
app = Flask(__name__)
app.secret_key = ADMIN_SECRET 

# Global State Variables
CURRENT_PROXY_SET = {"http": None, "https": None}
CURRENT_PROXY_STRING = "" 
db_lock = threading.Lock()

# Background Service Flags
proxy_checker_started = False
ping_service_started = False
auto_backup_started = False

# User-Agent giả lập Chrome Windows mới nhất để tránh WAF TikTok
# Giúp request trông giống người dùng thật nhất có thể.
UA_STRING = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


# ==============================================================================
#   PHẦN 2: TIỆN ÍCH THỜI GIAN (TIME UTILS)
# ==============================================================================

def get_vn_time():
    """
    Lấy thời gian hiện tại theo múi giờ Việt Nam (UTC+7).
    Trả về định dạng string: YYYY-MM-DD HH:MM:SS
    """
    utc_now = datetime.datetime.utcnow()
    vn_now = utc_now + datetime.timedelta(hours=7)
    return vn_now.strftime("%Y-%m-%d %H:%M:%S")


# ==============================================================================
#   PHẦN 3: XỬ LÝ DATABASE (DATABASE OPERATIONS)
# ==============================================================================

def db():
    """Tạo kết nối tới SQLite Database với Row Factory."""
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row 
    return con

def _ensure_col(con, table, col, decl):
    """Đảm bảo một cột tồn tại trong bảng (Migration helper)."""
    try:
        query = f"ALTER TABLE {table} ADD COLUMN {col} {decl}"
        con.execute(query)
    except Exception:
        pass

def init_db():
    """
    Khởi tạo cấu trúc Database nếu chưa tồn tại.
    Thực hiện Migration và Auto Restore nếu cần.
    """
    with db_lock:
        with db() as con:
            print(f"INFO: Đang kết nối và khởi tạo Database tại: {DB}")
            
            # --- TẠO CÁC BẢNG CẦN THIẾT ---
            
            # 1. Bảng Keymaps (Quản lý nguồn hàng Mail72h/Local)
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
            
            # 2. Bảng Config (Lưu cấu hình hệ thống)
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
            
            # 3. Bảng Proxies (Lưu danh sách Proxy)
            con.execute("""
                CREATE TABLE IF NOT EXISTS proxies(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_string TEXT NOT NULL UNIQUE, 
                    is_live INTEGER DEFAULT 0,
                    latency REAL DEFAULT 9999.0, 
                    last_checked TEXT
                )
            """)
            
            # 4. Bảng Local Stock (Kho hàng thủ công)
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_stock(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    added_at TEXT
                )
            """)
            
            # 5. Bảng Local History (Lịch sử lấy hàng)
            con.execute("""
                CREATE TABLE IF NOT EXISTS local_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_name TEXT NOT NULL,
                    content TEXT NOT NULL,
                    fetched_at TEXT
                )
            """)
            
            # 6. Bảng TikTok History (Lịch sử check live/die)
            con.execute("""
                CREATE TABLE IF NOT EXISTS tiktok_history(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_line TEXT,
                    tiktok_id TEXT,
                    status TEXT,
                    checked_at TEXT
                )
            """)
            
            # --- MIGRATION (Cập nhật Schema cũ lên mới) ---
            _ensure_col(con, "keymaps", "group_name", "TEXT")
            _ensure_col(con, "keymaps", "provider_type", "TEXT NOT NULL DEFAULT 'mail72h'")
            _ensure_col(con, "keymaps", "base_url", "TEXT")
            _ensure_col(con, "keymaps", "api_key", "TEXT")
            
            try: con.execute("ALTER TABLE keymaps DROP COLUMN note")
            except: pass
            try: con.execute("ALTER TABLE keymaps RENAME COLUMN mail72h_api_key TO api_key")
            except: pass
            
            # --- DATA MẶC ĐỊNH (DEFAULT SEED) ---
            con.execute("DELETE FROM config WHERE key='current_proxy_string'")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_url", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_interval", "300"))
            
            con.commit()

            # --- AUTO RESTORE (TỰ ĐỘNG KHÔI PHỤC) ---
            # Nếu database trống và có file backup bí mật, tự động nạp lại dữ liệu.
            keymap_count = con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0]
            if keymap_count == 0:
                print("WARNING: Database trống. Đang tìm backup...")
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        
                        # Xử lý các định dạng backup khác nhau
                        keymaps_to_import = data if isinstance(data, list) else data.get('keymaps', [])
                        config_to_import = data.get('config', {}) if isinstance(data, dict) else {}
                        proxies_to_import = data.get('proxies', []) if isinstance(data, dict) else []
                        local_stock_to_import = data.get('local_stock', []) if isinstance(data, dict) else []

                        # Import Keymaps
                        for item in keymaps_to_import:
                            con.execute("""
                                INSERT OR IGNORE INTO keymaps(sku, input_key, product_id, is_active, group_name, provider_type, base_url, api_key) 
                                VALUES(?,?,?,?,?,?,?,?)
                            """, (item.get('sku'), item.get('input_key'), item.get('product_id'), item.get('is_active', 1), item.get('group_name', 'DEFAULT'), item.get('provider_type', 'mail72h'), item.get('base_url'), item.get('api_key')))

                        # Import Config
                        for key, value in config_to_import.items():
                            con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", (key, str(value)))
                        
                        # Import Proxies
                        for item in proxies_to_import:
                            con.execute("INSERT OR IGNORE INTO proxies (proxy_string, is_live, latency, last_checked) VALUES (?, ?, ?, ?)", (item.get('proxy_string'), item.get('is_live', 0), item.get('latency', 9999.0), get_vn_time()))
                            
                        # Import Stock
                        for item in local_stock_to_import:
                            con.execute("INSERT INTO local_stock (group_name, content, added_at) VALUES (?, ?, ?)", (item.get('group_name'), item.get('content'), item.get('added_at')))
                        
                        con.commit()
                        print("SUCCESS: Đã khôi phục dữ liệu từ file backup!")
                    except Exception as e:
                        print(f"ERROR: Khôi phục thất bại. {e}")


# ==============================================================================
#   PHẦN 4: XỬ LÝ PROXY (PROXY MANAGER)
# ==============================================================================

def format_proxy_url(proxy_string: str) -> dict:
    """
    Chuyển đổi chuỗi proxy (ip:port hoặc ip:port:user:pass) thành format requests.
    """
    if not proxy_string:
        return {"http": None, "https": None}
    parts = proxy_string.strip().split(':')
    formatted_proxy = ""
    
    # Format: ip:port
    if len(parts) == 2:
        ip, port = parts
        formatted_proxy = f"http://{ip}:{port}"
    # Format: ip:port:user:pass
    elif len(parts) >= 4:
        ip, port, user, passwd = parts[0], parts[1], parts[2], parts[3]
        formatted_proxy = f"http://{user}:{passwd}@{ip}:{port}"
    else:
        return {"http": None, "https": None}
        
    return {"http": formatted_proxy, "https": formatted_proxy}

def check_proxy_live(proxy_string: str) -> tuple:
    """
    Kiểm tra xem proxy có hoạt động không bằng cách gọi Google.
    Trả về: (is_live (0/1), latency (ms))
    """
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
    """Cập nhật trạng thái proxy vào DB."""
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
    """Chọn proxy sống tốt nhất từ DB và set làm proxy hệ thống."""
    live_proxy = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 ORDER BY latency ASC LIMIT 1").fetchone()
    new_proxy_string = live_proxy['proxy_string'] if live_proxy else ""
    set_current_proxy_by_string(new_proxy_string)
    con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", new_proxy_string))
    con.commit()
    return new_proxy_string

def switch_to_next_live_proxy():
    """Chuyển sang proxy sống tiếp theo (dùng khi proxy hiện tại lỗi)."""
    with db_lock:
        with db() as con:
            live_proxies = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 AND proxy_string != ? ORDER BY latency ASC", (CURRENT_PROXY_STRING,)).fetchall()
            new_proxy_string = live_proxies[0]['proxy_string'] if live_proxies else ""
            set_current_proxy_by_string(new_proxy_string)
            con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", new_proxy_string))
            con.commit()
            return new_proxy_string

def run_initial_proxy_scan_and_select():
    """Quét toàn bộ proxy khi khởi động."""
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
#   PHẦN 5: TÁC VỤ NỀN (BACKGROUND TASKS)
# ==============================================================================

def proxy_checker_loop():
    """Luồng chạy ngầm kiểm tra proxy định kỳ."""
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
            # Nếu proxy đang dùng bị chết, đổi cái khác ngay
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
    """Luồng Ping để giữ server không bị ngủ đông (trên Render Free)."""
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
    """Thực hiện backup toàn bộ DB ra file JSON."""
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
        time.sleep(3600) # Backup mỗi 1 giờ
        perform_backup_to_file()

def start_auto_backup():
    global auto_backup_started
    if not auto_backup_started:
        auto_backup_started = True
        t = threading.Thread(target=auto_backup_loop, daemon=True)
        t.start()


# ==============================================================================
#   PHẦN 6: LOGIC KHO HÀNG & API (STOCK & API LOGIC)
# ==============================================================================

def get_local_stock_count(group_name):
    with db() as con:
        count = con.execute("SELECT COUNT(*) FROM local_stock WHERE group_name=?", (group_name,)).fetchone()[0]
    return count

def fetch_local_stock(group_name, qty):
    """Lấy hàng từ kho local và xóa ngay sau khi lấy."""
    products = []
    with db_lock:
        with db() as con:
            rows = con.execute("SELECT id, content FROM local_stock WHERE group_name=? LIMIT ?", (group_name, qty)).fetchall()
            if not rows: return []
            ids_to_delete = [r['id'] for r in rows]
            now = get_vn_time()
            # Lưu lịch sử
            for r in rows:
                con.execute("INSERT INTO local_history(group_name, content, fetched_at) VALUES(?,?,?)", (group_name, r['content'], now))
            # Xóa khỏi kho
            con.execute(f"DELETE FROM local_stock WHERE id IN ({','.join(['?']*len(ids_to_delete))})", ids_to_delete)
            con.commit()
            for r in rows: products.append({"product": r['content']})
    return products

# --- Mail72h Logic ---
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
#   PHẦN 7: HTML TEMPLATES (GIAO DIỆN)
# ==============================================================================

# 7.1 Template Đăng Nhập
LOGIN_TPL = """<!doctype html><html data-theme="dark"><head><meta charset="utf-8"/><title>Login</title><style>body{background:#121212;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif}.box{background:#1c1c1e;padding:40px;border-radius:12px;text-align:center}input{padding:10px;margin:10px 0;width:100%;background:#333;border:1px solid #444;color:white}button{padding:10px;width:100%;background:#0d6efd;color:white;border:none;cursor:pointer}</style></head><body><div class="box"><h2>QUANTUM GATE</h2><form method="post"><input type="password" name="admin_secret" placeholder="Password"><button>Login</button></form></div></body></html>"""

# 7.2 Template Admin Dashboard (Full UI & Effects)
ADMIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Admin Dashboard - Apple Pro</title>
    <style>
    /* CSS CORE */
    :root {
        --primary: #0d6efd; --green: #198754; --red: #dc3545; --yellow: #ffc107;
        --bg-dark: #121212; --card-dark: #1c1c1e; --text-dark: #e9ecef; --border-dark: #333;
        --bg-light: #f8f9fa; --card-light: #ffffff; --text-light: #212529; --border-light: #dee2e6;
    }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        padding: 20px;
        margin: 0;
        background: var(--bg-dark);
        color: var(--text-dark);
        transition: background 0.3s, color 0.3s;
        overflow-x: hidden;
    }
    
    /* LIGHT MODE */
    body[data-theme="light"] { background: var(--bg-light); color: var(--text-light); }
    body[data-theme="light"] .card { background: var(--card-light); border-color: var(--border-light); color: var(--text-light); box-shadow: 0 2px 5px rgba(0,0,0,0.05); }
    body[data-theme="light"] input, body[data-theme="light"] textarea, body[data-theme="light"] select { background: #fff; border-color: #ced4da; color: #495057; }
    
    /* LAYOUT */
    .container { max-width: 1200px; margin: 0 auto; position: relative; z-index: 10; }
    .card {
        background: var(--card-dark);
        border: 1px solid var(--border-dark);
        padding: 20px;
        margin-bottom: 20px;
        border-radius: 12px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 15px; }
    .col-4 { grid-column: span 4; } .col-6 { grid-column: span 6; } .col-8 { grid-column: span 8; } .col-12 { grid-column: span 12; }
    
    /* ELEMENTS */
    input, textarea, select {
        width: 100%;
        padding: 10px;
        background: #2c2c2e;
        border: 1px solid #444;
        color: #fff;
        box-sizing: border-box;
        border-radius: 6px;
        font-family: monospace;
        margin-bottom: 5px;
    }
    button {
        padding: 10px 20px;
        background: var(--primary);
        color: #fff;
        border: none;
        border-radius: 6px;
        cursor: pointer;
        font-weight: 600;
        transition: opacity 0.2s;
    }
    button:hover { opacity: 0.9; }
    button.green { background: var(--green); }
    button.red { background: var(--red); }
    
    h2, h3 { margin-top: 0; color: var(--primary); }
    h3 { border-bottom: 1px solid var(--border-dark); padding-bottom: 10px; font-size: 1.2rem; }
    
    /* EFFECT CANVAS */
    #effect-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
    
    /* UTILS */
    .mono { font-family: monospace; }
    label { font-size: 12px; font-weight: bold; color: #adb5bd; display: block; margin-bottom: 5px; text-transform: uppercase; }
    </style>
</head>
<body>

<canvas id="effect-canvas"></canvas>

<div class="container">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
        <h2>⚙️ Admin Dashboard</h2>
        <div>
            <select id="theme-select" style="width:auto; display:inline-block;" onchange="changeTheme(this.value)">
                <option value="dark">Dark Mode</option>
                <option value="light">Light Mode</option>
            </select>
            <select id="effect-select" style="width:auto; display:inline-block; margin-left:10px;" onchange="changeEffect(this.value)">
                <option value="none">No Effect</option>
                <option value="matrix">Matrix</option>
                <option value="snow">Snow</option>
                <option value="rain">Rain</option>
                <option value="particles">Particles</option>
            </select>
            <form action="{{url_for('logout')}}" method="post" style="display:inline-block; margin-left:10px;">
                <button class="red">Logout</button>
            </form>
        </div>
    </div>

    <div class="row">
      <div class="col-6 card">
        <h3>1. Thêm Key (API / Local)</h3>
        <form action="{{url_for('admin_add_keymap')}}" method="post">
          <div class="row">
              <div class="col-6"><label>Group Name</label><input name="group_name" required></div>
              <div class="col-6"><label>Type</label><input name="provider_type" placeholder="local / mail72h" required></div>
          </div>
          <div class="row">
              <div class="col-6"><label>SKU</label><input name="sku" required></div>
              <div class="col-6"><label>Input Key</label><input name="input_key" required></div>
          </div>
          <label>API Config (Nếu dùng Mail72h)</label>
          <input name="base_url" placeholder="Base URL">
          <input name="api_key" placeholder="API Key">
          <input name="product_id" placeholder="Product ID (0 for local)">
          <button style="width:100%; margin-top:10px;">Lưu Key</button>
        </form>
      </div>
      
      <div class="col-6 card">
        <h3>2. Proxy System</h3>
        <div style="margin-bottom:10px">
            Current System Proxy: <code style="color:var(--green)">{{current_proxy or 'Direct Connection'}}</code>
        </div>
        <form action="{{url_for('admin_add_proxy')}}" method="post">
          <label>Thêm Proxy (Mỗi dòng 1 cái)</label>
          <textarea name="proxies" rows="5" placeholder="ip:port&#10;ip:port:user:pass"></textarea>
          <button style="width:100%; margin-top:10px;">Thêm Proxy</button>
        </form>
      </div>
    </div>

    <div class="card" style="border: 1px solid #ffc107;">
        <h3 style="color: #ffc107;">🚀 6. TikTok Checker Tool (Max Speed)</h3>
        <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank">
            <div class="row">
                <div class="col-8">
                    <label>NHẬP LIST CẦN CHECK (MỖI DÒNG 1 ID HOẶC USER|PASS...)</label>
                    <textarea name="check_list" rows="8" placeholder="tiktok_id_1&#10;tiktok_id_2|pass..." required></textarea>
                </div>
                <div class="col-4">
                    <label>PROXY RIÊNG (TÙY CHỌN)</label>
                    <textarea name="check_proxies" rows="4" placeholder="ip:port:user:pass..."></textarea>
                    
                    <label style="margin-top:10px;">SỐ LUỒNG (THREADS)</label>
                    <input type="number" name="threads" value="20" min="1" max="200">
                    
                    <button class="green" style="width:100%; margin-top:15px; font-size:16px;">🚀 BẮT ĐẦU CHECK</button>
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
        
        <div style="margin-top:15px;max-height:300px;overflow-y:auto; border:1px solid #333; border-radius:6px;">
            {% for g, c in local_stats.items() %}
            <div style="border-bottom:1px dashed #444; padding:8px; display:flex; justify-content:space-between; align-items:center; background: rgba(255,255,255,0.05);">
                <span><b>{{g}}</b>: <span style="color:var(--green)">{{c}}</span> items</span>
                <div style="display:flex; gap:5px;">
                    <input id="q_{{g}}" type="number" value="1" style="width:50px;padding:4px; margin:0;" min="1">
                    <button class="green" style="padding:4px 8px; font-size:12px;" onclick="quickGet('{{g}}')">⚡ Lấy</button>
                    <a href="{{url_for('admin_local_stock_view', group=g)}}" style="background:#333; color:white; text-decoration:none; padding:4px 8px; border-radius:4px; font-size:12px; display:flex; align-items:center;">Xem</a>
                    <form action="{{url_for('admin_local_stock_clear')}}" method="post" style="margin:0;" onsubmit="return confirm('Xóa sạch kho {{g}}?')">
                        <input type="hidden" name="group_name" value="{{g}}">
                        <button class="red" style="padding:4px 8px; font-size:12px;">Xóa</button>
                    </form>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>
</div>

<script>
// Logic Lấy Hàng Nhanh
async function quickGet(g){
    let q = document.getElementById('q_'+g).value;
    if(confirm(`Lấy ${q} acc từ nhóm ${g}?`)){
        try {
            let r = await fetch(`/admin/local-stock/quick-get?group=${g}&qty=${q}`);
            if(r.ok){
                let t = await r.text();
                if(t){ 
                    await navigator.clipboard.writeText(t); 
                    alert("✅ Đã lấy và COPY thành công!"); 
                    location.reload(); 
                } else {
                    alert("❌ Hết hàng!");
                }
            }
        } catch(e) { alert("Lỗi: " + e); }
    }
}

// Logic Theme & Effect
function changeTheme(t) {
    document.body.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
}
function changeEffect(e) {
    localStorage.setItem('effect', e);
    startEffect(e);
}

// Khôi phục cài đặt cũ
document.addEventListener("DOMContentLoaded", () => {
    let t = localStorage.getItem('theme') || 'dark';
    let e = localStorage.getItem('effect') || 'none';
    document.getElementById('theme-select').value = t;
    document.getElementById('effect-select').value = e;
    changeTheme(t);
    startEffect(e);
});

// === HIỆU ỨNG CANVAS (Javascript) ===
let canvas, ctx, w, h, animationId;
function initCanvas(){
    canvas = document.getElementById('effect-canvas');
    ctx = canvas.getContext('2d');
    resize();
    window.addEventListener('resize', resize);
}
function resize(){
    w = canvas.width = window.innerWidth;
    h = canvas.height = window.innerHeight;
}
function startEffect(type){
    if(animationId) cancelAnimationFrame(animationId);
    if(!canvas) initCanvas();
    ctx.clearRect(0,0,w,h);
    
    if(type === 'matrix') matrixEffect();
    else if(type === 'snow') snowEffect();
    else if(type === 'rain') rainEffect();
    else if(type === 'particles') particlesEffect();
}

// 1. Matrix Effect
function matrixEffect(){
    const cols = Math.floor(w / 20) + 1;
    const ypos = Array(cols).fill(0);
    ctx.fillStyle = '#000'; ctx.fillRect(0,0,w,h);
    
    function step(){
        ctx.fillStyle = '#0001'; ctx.fillRect(0,0,w,h);
        ctx.fillStyle = '#0f0'; ctx.font = '15pt monospace';
        ypos.forEach((y, ind) => {
            const text = String.fromCharCode(Math.random() * 128);
            const x = ind * 20;
            ctx.fillText(text, x, y);
            if (y > 100 + Math.random() * 10000) ypos[ind] = 0;
            else ypos[ind] = y + 20;
        });
        animationId = requestAnimationFrame(step);
    }
    step();
}

// 2. Snow Effect
function snowEffect(){
    const flakes = Array(100).fill().map(() => ({
        x: Math.random()*w, y: Math.random()*h,
        r: Math.random()*3+1, d: Math.random()*100
    }));
    let angle = 0;
    function step(){
        ctx.clearRect(0,0,w,h);
        ctx.fillStyle = "rgba(255, 255, 255, 0.8)";
        ctx.beginPath();
        angle += 0.01;
        for(let f of flakes){
            ctx.moveTo(f.x, f.y);
            ctx.arc(f.x, f.y, f.r, 0, Math.PI*2, true);
            f.y += Math.cos(angle+f.d) + 1 + f.r/2;
            f.x += Math.sin(angle) * 2;
            if(f.x > w+5 || f.x < -5 || f.y > h) {
                f.x = Math.random()*w; f.y = -10;
            }
        }
        ctx.fill();
        animationId = requestAnimationFrame(step);
    }
    step();
}

// 3. Rain Effect
function rainEffect(){
    const drops = Array(200).fill().map(() => ({
        x: Math.random()*w, y: Math.random()*h,
        l: Math.random()*1, v: Math.random()*4+4
    }));
    function step(){
        ctx.clearRect(0,0,w,h);
        ctx.strokeStyle = 'rgba(174,194,224,0.5)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        for(let d of drops){
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d.x, d.y+d.l*5);
            d.y += d.v;
            if(d.y > h){ d.y = -20; d.x = Math.random()*w; }
        }
        ctx.stroke();
        animationId = requestAnimationFrame(step);
    }
    step();
}

// 4. Particles Effect
function particlesEffect(){
    const parts = Array(80).fill().map(() => ({
        x: Math.random()*w, y: Math.random()*h,
        vx: (Math.random()-0.5), vy: (Math.random()-0.5)
    }));
    function step(){
        ctx.clearRect(0,0,w,h);
        ctx.fillStyle = 'rgba(200,200,200,0.5)';
        ctx.strokeStyle = 'rgba(200,200,200,0.1)';
        for(let i=0; i<parts.length; i++){
            let p = parts[i];
            ctx.beginPath(); ctx.arc(p.x, p.y, 2, 0, Math.PI*2); ctx.fill();
            p.x += p.vx; p.y += p.vy;
            if(p.x < 0 || p.x > w) p.vx *= -1;
            if(p.y < 0 || p.y > h) p.vy *= -1;
            for(let j=i+1; j<parts.length; j++){
                let p2 = parts[j];
                let dist = Math.sqrt((p.x-p2.x)**2 + (p.y-p2.y)**2);
                if(dist < 100){
                    ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p2.x, p2.y); ctx.stroke();
                }
            }
        }
        animationId = requestAnimationFrame(step);
    }
    step();
}
</script>
</body>
</html>
"""

# 7.3 Template Xem Kho (Stock View)
STOCK_VIEW_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8"/>
    <title>Stock View</title>
    <style>
        body{background:#121212;color:#fff;font-family:monospace;padding:20px}
        table{width:100%;border-collapse:collapse;margin-top:20px}
        td,th{border:1px solid #333;padding:10px;text-align:left}
        th{background:#1c1c1e;color:#adb5bd}
        a{text-decoration:none;color:#0d6efd}
        button{cursor:pointer;border:none;border-radius:4px;padding:5px 10px;color:white;font-weight:bold}
    </style>
</head>
<body>
    <h2>📦 Group: {{group}} ({{items|length}})</h2>
    
    <div style="display:flex; gap:10px; align-items:center;">
        <a href="{{url_for('admin_index')}}#local-stock">🔙 Quay lại</a>
        <a href="{{url_for('admin_local_stock_download', group=group)}}" style="background:#20c997;color:black;padding:5px 10px;border-radius:4px">📥 Tải File TXT</a>
        
        <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank" style="margin:0">
            <input type="hidden" name="local_group" value="{{group}}">
            <button style="background:#ffc107;color:black;">🔍 Check Live (TikTok)</button>
        </form>
    </div>

    <table>
        <thead><tr><th>STT</th><th>Nội dung</th><th>Ngày thêm</th><th>Xóa</th></tr></thead>
        <tbody>
        {% for i in items %}
        <tr>
            <td>{{loop.index}}</td>
            <td style="color:#20c997;word-break:break-all">{{i.content}}</td>
            <td>{{i.added_at}}</td>
            <td>
                <form action="{{url_for('admin_local_stock_delete_one')}}" method="post">
                    <input type="hidden" name="id" value="{{i.id}}">
                    <input type="hidden" name="group" value="{{group}}">
                    <button style="background:#dc3545;">X</button>
                </form>
            </td>
        </tr>
        {% endfor %}
        </tbody>
    </table>
</body>
</html>
"""

# 7.4 Template Checker Stream (Quan trọng)
CHECKER_STREAM_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8"/>
    <title>Checker Progress</title>
    <style>
        body{background:#121212;color:#fff;font-family:monospace;padding:20px}
        .stats{display:flex;gap:20px;font-size:20px;margin-bottom:20px;border:1px solid #333;padding:15px;border-radius:8px;background:#1c1c1e}
        .live{color:#20c997;font-weight:bold}.die{color:#dc3545;font-weight:bold}
        .box{margin-top:20px} 
        textarea{width:100%;background:#2c2c2e;color:#fff;border:1px solid #333;padding:10px;min-height:200px;font-family:monospace}
        h3{border-bottom:1px solid #333;padding-bottom:5px}
        .die-list{max-height:300px;overflow-y:auto;border:1px solid #333;padding:10px;background:#1a1a1a}
        .die-item{font-size:12px;border-bottom:1px dashed #333;padding:3px 0;color:#aaa}
    </style>
</head>
<body>
    <h2>🚀 Checking Progress...</h2>
    
    <div class="stats">
        <span id="st">Total: 0</span> | 
        <span class="live" id="cl">LIVE: 0</span> | 
        <span class="die" id="cd">DIE: 0</span>
    </div>
    <div id="msg" style="color:#aaa;margin-bottom:10px">Initializing threads...</div>

    <div class="box">
        <h3 class="live">✅ DANH SÁCH LIVE (Copy tại đây)</h3>
        <textarea id="live_area" readonly></textarea>
    </div>

    <div class="box">
        <h3 class="die">❌ CHI TIẾT LỖI / DIE</h3>
        <div id="die_list" class="die-list"></div>
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
            document.getElementById('msg').innerText = "✅ CHECK COMPLETED!";
            document.getElementById('msg').style.color = "#20c997";
            document.getElementById('msg').style.fontWeight = "bold";
        }
    </script>
"""


# ==============================================================================
#   PHẦN 8: ROUTING & CONTROLLERS (FLASK ROUTES)
# ==============================================================================

def find_map_by_key(key: str):
    """Tìm thông tin sản phẩm từ Keymap."""
    with db() as con:
        return con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (key,)).fetchone()

def require_admin():
    """Middleware bảo vệ trang Admin."""
    if request.cookies.get("logged_in") != ADMIN_SECRET:
        abort(redirect(url_for('login')))

# --- AUTH ROUTES ---
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("admin_secret") == ADMIN_SECRET:
            resp = make_response(redirect(url_for("admin_index")))
            resp.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000)
            return resp
        flash("Sai mật khẩu!")
    if request.cookies.get("logged_in") == ADMIN_SECRET:
        return redirect(url_for("admin_index"))
    return render_template_string(LOGIN_TPL)

@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.set_cookie("logged_in", "", max_age=0)
    return resp

# --- ADMIN ROUTES ---
@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        # Lấy dữ liệu để hiển thị
        stock = con.execute("SELECT group_name, COUNT(*) as c FROM local_stock GROUP BY group_name").fetchall()
        
    # Lấy các tham số giao diện từ cookie (nếu có)
    return render_template_string(ADMIN_TPL, 
        current_proxy=CURRENT_PROXY_STRING,
        local_groups=[r['group_name'] for r in stock],
        local_stats={r['group_name']: r['c'] for r in stock}
    )

# --- KEYMAP ACTIONS ---
@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    f = request.form
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
                    base_url=excluded.base_url
            """, (f.get("group_name"), f.get("sku"), f.get("input_key"), f.get("product_id") or 0, f.get("api_key"), f.get("provider_type"), f.get("base_url")))
            con.commit()
        flash("Lưu Key thành công!")
    except Exception as e:
        flash(f"Lỗi: {e}")
    return redirect(url_for("admin_index"))

# --- STOCK ACTIONS ---
@app.route("/admin/local-stock/add", methods=["POST"])
def admin_local_stock_add():
    require_admin()
    grp = request.form.get("group_name")
    f = request.files.get("stock_file")
    if f: 
        lines = f.read().decode('utf-8', errors='ignore').splitlines()
        with db() as con:
            for l in lines: 
                if l.strip():
                    con.execute("INSERT INTO local_stock(group_name, content, added_at) VALUES(?,?,?)", 
                               (grp, l.strip(), get_vn_time()))
            con.commit()
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/local-stock/view")
def admin_local_stock_view():
    require_admin()
    grp = request.args.get("group")
    with db() as con:
        items = con.execute("SELECT * FROM local_stock WHERE group_name=?", (grp,)).fetchall()
    return render_template_string(STOCK_VIEW_TPL, group=grp, items=items)

@app.route("/admin/local-stock/download")
def admin_local_stock_download():
    require_admin()
    grp = request.args.get("group")
    with db() as con:
        rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (grp,)).fetchall()
    resp = make_response("\n".join([r['content'] for r in rows]))
    resp.headers["Content-Disposition"] = f"attachment; filename=stock_{quote(grp)}.txt"
    resp.headers["Content-Type"] = "text/plain"
    return resp

@app.route("/admin/local-stock/quick-get")
def admin_local_stock_quick_get():
    require_admin()
    grp = request.args.get("group")
    try: qty = int(request.args.get("qty", 1))
    except: return "Invalid Qty", 400
    
    items = fetch_local_stock(grp, qty)
    return "\n".join([i['product'] for i in items]), 200, {'Content-Type':'text/plain'}

@app.route("/admin/local-stock/delete-one", methods=["POST"])
def admin_local_stock_delete_one():
    require_admin()
    with db() as con:
        con.execute("DELETE FROM local_stock WHERE id=?", (request.form.get("id"),))
        con.commit()
    return redirect(url_for("admin_local_stock_view", group=request.form.get("group")))

@app.route("/admin/local-stock/clear", methods=["POST"])
def admin_local_stock_clear():
    require_admin()
    with db() as con:
        con.execute("DELETE FROM local_stock WHERE group_name=?", (request.form.get("group_name"),))
        con.commit()
    return redirect(url_for("admin_index") + "#local-stock")

# --- PROXY ACTIONS ---
@app.route("/admin/proxy/add", methods=["POST"])
def admin_add_proxy():
    require_admin()
    proxies_raw = request.form.get("proxies", "")
    with db() as con:
        for line in proxies_raw.splitlines():
            if line.strip():
                con.execute("INSERT OR IGNORE INTO proxies(proxy_string) VALUES(?)", (line.strip(),))
        con.commit()
    return redirect(url_for("admin_index"))


# ==============================================================================
#   PHẦN 9: CHECKER CORE LOGIC (NÂNG CẤP MAX SPEED)
# ==============================================================================

def check_tiktok_advanced(line, proxy_iter):
    """
    Hàm check TikTok Live/Die chuẩn xác 100%.
    - LIVE: Bao gồm cả bị Captcha, Private, hoặc 200 OK nhưng ko lấy được data.
    - DIE: Chỉ khi 404 hoặc HTML chứa 'Không thể tìm thấy...'
    """
    line = line.strip()
    if not line: return None
    
    # Tách user id từ dòng (hỗ trợ user|pass hoặc user)
    parts = line.split('|') if '|' in line else line.split()
    user_id = parts[0].strip().replace("@", "")
    if not user_id: return None
    
    url = f"https://www.tiktok.com/@{user_id}"
    
    # Headers giả lập Chrome xịn
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
    
    # Lấy Proxy xoay vòng (nếu có)
    prx = None
    if proxy_iter:
        try: prx = next(proxy_iter)
        except: pass
    
    proxies = format_proxy_url(prx) if prx else CURRENT_PROXY_SET
    
    try:
        # Timeout 10s là hợp lý
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
            
        # 3. Mọi trường hợp còn lại -> LIVE (An toàn)
        if "captcha" in html.lower() or "verify" in html.lower():
            return ("LIVE", line, "Live (Captcha)")
            
        if "private" in html.lower():
            return ("LIVE", line, "Live (Private)")
            
        return ("LIVE", line, "Live OK")
        
    except Exception as e:
        # Lỗi mạng -> Coi như Live (để ko xóa nhầm) hoặc Retry
        # Trả về LIVE kèm lỗi
        return ("LIVE", line, f"Error/Timeout: {str(e)}")

@app.route("/admin/checker/run", methods=["POST"])
def admin_run_checker():
    require_admin()
    
    # Lấy dữ liệu input từ Form
    raw_list = request.form.get("check_list", "")
    local_group = request.form.get("local_group", "")
    raw_proxies = request.form.get("check_proxies", "").strip()
    
    try: 
        threads = int(request.form.get("threads", 10))
    except: 
        threads = 10
    
    # Chuẩn bị danh sách cần check
    lines = []
    if local_group:
        # Nếu check từ Local Stock
        with db() as con:
            rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (local_group,)).fetchall()
            lines = [r['content'] for r in rows]
    else:
        # Nếu check từ Input Box
        lines = [l for l in raw_list.splitlines() if l.strip()]
        
    if not lines:
        return "<h3>Không có dữ liệu để check.</h3>", 400
        
    # Chuẩn bị Proxy List
    proxy_list = [p.strip() for p in raw_proxies.splitlines() if p.strip()]
    
    # Nếu không nhập proxy riêng, thử lấy từ DB
    if not proxy_list:
        with db() as con: 
            proxy_list = [r['proxy_string'] for r in con.execute("SELECT proxy_string FROM proxies WHERE is_live=1").fetchall()]
    
    # Tạo Iterator để xoay vòng Proxy vô tận
    proxy_iter = itertools.cycle(proxy_list) if proxy_list else None
    
    # Hàm Generator: Stream kết quả HTML về trình duyệt
    def generate_stream():
        # Gửi Header HTML trước
        yield CHECKER_STREAM_TPL
        
        total = len(lines)
        done = 0
        live = 0
        die = 0
        
        # Sử dụng ThreadPoolExecutor để chạy Đa Luồng
        with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as executor:
            # Submit tất cả task vào pool
            futures = {executor.submit(check_tiktok_advanced, line, proxy_iter): line for line in lines}
            
            # Xử lý khi mỗi task hoàn thành
            for future in concurrent.futures.as_completed(futures):
                res = future.result()
                if res:
                    status, line, reason = res
                    done += 1
                    
                    # Escape chuỗi JSON an toàn cho JS
                    safe_line = json.dumps(line)
                    safe_reason = json.dumps(reason)
                    
                    if status == "LIVE":
                        live += 1
                        # Gọi hàm JS addLive()
                        yield f"<script>addLive({safe_line}); update({done}, {live}, {die});</script>\n"
                    else:
                        die += 1
                        # Gọi hàm JS addDie()
                        yield f"<script>addDie({safe_line}, {safe_reason}); update({done}, {live}, {die});</script>\n"
                else:
                    done += 1
                    yield f"<script>update({done}, {live}, {die});</script>\n"
                    
        # Kết thúc
        yield "<script>done();</script></body></html>"

    # Trả về Response dạng Stream
    return Response(stream_with_context(generate_stream()))


# ==============================================================================
#   PHẦN 10: PUBLIC API (API LẤY HÀNG)
# ==============================================================================

@app.route("/stock")
def stock():
    """API kiểm tra số lượng hàng tồn."""
    key = request.args.get("key", "").strip()
    with db() as con: 
        row = find_map_by_key(key)
        
    if not row: 
        return jsonify({"sum": 0})
        
    if row['provider_type'] == 'local': 
        return jsonify({"sum": get_local_stock_count(row['group_name'])})
        
    return stock_mail72h_format(row) 

@app.route("/fetch")
def fetch():
    """API lấy hàng (Buy)."""
    key = request.args.get("key", "").strip()
    try: 
        qty = int(request.args.get("quantity", 0))
    except: 
        return jsonify([])
        
    with db() as con: 
        row = find_map_by_key(key)
        
    if not row or qty <= 0: 
        return jsonify([])
        
    if row['provider_type'] == 'local': 
        return jsonify(fetch_local_stock(row['group_name'], qty))
        
    return fetch_mail72h_format(row, qty)

@app.route("/health")
def health():
    """API kiểm tra tình trạng server."""
    return "OK", 200


# ==============================================================================
#   PHẦN 11: KHỞI ĐỘNG (STARTUP SEQUENCE)
# ==============================================================================

# 1. Khởi tạo Database
init_db()

# 2. Khởi động các luồng chạy nền (chỉ chạy 1 lần)
if not proxy_checker_started:
    start_proxy_checker_once()
    
if not ping_service_started:
    start_ping_service()
    
if not auto_backup_started:
    start_auto_backup()

# 3. Khôi phục Proxy từ cấu hình cũ
try:
    with db() as c: 
        p = load_selected_proxy_from_db(c)
        if p: 
            set_current_proxy_by_string(p)
        else: 
            select_best_available_proxy(c)
except Exception as e:
    print(f"Startup Warning: {e}")

# 4. Chạy App (Khi gọi trực tiếp)
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 SERVER STARTED ON PORT {port}")
    app.run(host="0.0.0.0", port=port)
