import os
import json
import sqlite3
import datetime
import threading
import time
import random
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string, flash, make_response
import requests

# ==============================================================================
#   PHẦN 1: CẤU HÌNH HỆ THỐNG
# ==============================================================================
DB = os.getenv("DB_PATH", "store.db") 
SECRET_BACKUP_FILE_PATH = os.getenv("SECRET_BACKUP_FILE_PATH", "/etc/secrets/backupapitaphoa.json")
AUTO_BACKUP_FILE = "auto_backup.json"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "5")) 
PROXY_CHECK_INTERVAL = 15 

app = Flask(__name__)
app.secret_key = ADMIN_SECRET 

CURRENT_PROXY_SET = { "http": None, "https": None }
CURRENT_PROXY_STRING = "" 
db_lock = threading.Lock()
proxy_checker_started = False
ping_service_started = False
auto_backup_started = False

def get_vn_time():
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=7)).strftime("%Y-%m-%d %H:%M:%S")

# ==============================================================================
#   PHẦN 2: DATABASE
# ==============================================================================
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row 
    return con

def _ensure_col(con, table, col, decl):
    try: con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except: pass

def init_db():
    with db_lock:
        with db() as con:
            print(f"INFO: Init Database: {DB}")
            con.execute("CREATE TABLE IF NOT EXISTS keymaps(id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, input_key TEXT NOT NULL UNIQUE, product_id INTEGER NOT NULL, is_active INTEGER DEFAULT 1, group_name TEXT, provider_type TEXT NOT NULL DEFAULT 'mail72h', base_url TEXT, api_key TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS proxies(id INTEGER PRIMARY KEY AUTOINCREMENT, proxy_string TEXT NOT NULL UNIQUE, is_live INTEGER DEFAULT 0, latency REAL DEFAULT 9999.0, last_checked TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_stock(id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, content TEXT NOT NULL, added_at TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_history(id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, content TEXT NOT NULL, fetched_at TEXT)")
            
            _ensure_col(con, "keymaps", "group_name", "TEXT")
            _ensure_col(con, "keymaps", "provider_type", "TEXT NOT NULL DEFAULT 'mail72h'")
            _ensure_col(con, "keymaps", "base_url", "TEXT")
            _ensure_col(con, "keymaps", "api_key", "TEXT")
            
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('selected_proxy_string', '')")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('ping_url', '')")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('ping_interval', '300')")
            con.commit()

            # Auto Restore
            if con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0] == 0:
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
                        kms = data.get('keymaps', []) if isinstance(data, dict) else data
                        for k in kms: con.execute("INSERT OR IGNORE INTO keymaps(sku,input_key,product_id,is_active,group_name,provider_type,base_url,api_key) VALUES(?,?,?,?,?,?,?,?)", (k.get('sku'), k.get('input_key'), k.get('product_id'), k.get('is_active',1), k.get('group_name'), k.get('provider_type','mail72h'), k.get('base_url'), k.get('api_key')))
                        if isinstance(data, dict):
                            for p in data.get('proxies', []): con.execute("INSERT OR IGNORE INTO proxies(proxy_string,is_live,latency,last_checked) VALUES(?,?,?,?)", (p.get('proxy_string'),0,9999.0,get_vn_time()))
                            for l in data.get('local_stock', []): con.execute("INSERT INTO local_stock(group_name,content,added_at) VALUES(?,?,?)", (l.get('group_name'),l.get('content'),l.get('added_at')))
                            for c in data.get('config', {}).items(): con.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", c)
                        con.commit()
                        print("SUCCESS: Data Restored.")
                    except Exception as e: print(f"RESTORE ERROR: {e}")

# ==============================================================================
#   PHẦN 3: LOGIC PROXY
# ==============================================================================
def format_proxy_url(p):
    if not p: return {"http": None, "https": None}
    parts = p.split(':')
    fmt = f"http://{parts[0]}:{parts[1]}" if len(parts)==2 else (f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}" if len(parts)==4 else None)
    return {"http": fmt, "https": fmt} if fmt else {"http": None, "https": None}

def check_proxy_live(p):
    fmt = format_proxy_url(p)
    if not fmt.get("http"): return (0, 9999.0)
    try:
        t = time.time()
        requests.get("http://www.google.com/generate_204", proxies=fmt, timeout=DEFAULT_TIMEOUT*2)
        return (1, time.time()-t)
    except: return (0, 9999.0)

def set_current_proxy(p):
    global CURRENT_PROXY_SET, CURRENT_PROXY_STRING
    CURRENT_PROXY_SET = format_proxy_url(p)
    CURRENT_PROXY_STRING = p if CURRENT_PROXY_SET.get("http") else ""

def select_best_proxy(con):
    row = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 ORDER BY latency ASC LIMIT 1").fetchone()
    p = row['proxy_string'] if row else ""
    set_current_proxy(p)
    con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('selected_proxy_string',?)", (p,))
    con.commit()

def switch_proxy():
    with db_lock:
        with db() as con:
            row = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 AND proxy_string != ? ORDER BY latency ASC LIMIT 1", (CURRENT_PROXY_STRING,)).fetchone()
            p = row['proxy_string'] if row else ""
            set_current_proxy(p)
            con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('selected_proxy_string',?)", (p,))
            con.commit()

# ==============================================================================
#   PHẦN 4: THREADS
# ==============================================================================
def proxy_checker():
    time.sleep(2)
    while True:
        try:
            with db_lock:
                with db() as con: proxies = con.execute("SELECT * FROM proxies").fetchall()
            alive = False
            for r in proxies:
                l, lat = check_proxy_live(r['proxy_string'])
                with db_lock:
                    with db() as con: con.execute("UPDATE proxies SET is_live=?, latency=?, last_checked=? WHERE id=?", (l,lat,get_vn_time(),r['id'])); con.commit()
                if l and r['proxy_string'] == CURRENT_PROXY_STRING: alive = True
                time.sleep(0.5)
            if CURRENT_PROXY_STRING and not alive: switch_proxy()
        except: pass
        time.sleep(PROXY_CHECK_INTERVAL)

def ping_service():
    while True:
        try:
            with db() as con:
                u = con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()
                i = con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()
                url = u['value'] if u else ""; sec = int(i['value']) if i else 300
            if url.startswith("http"): requests.get(url, timeout=10)
            time.sleep(max(10, sec))
        except: time.sleep(60)

def auto_backup():
    while True:
        time.sleep(3600)
        try:
            with db_lock:
                with db() as con:
                    data = {
                        "keymaps": [dict(r) for r in con.execute("SELECT * FROM keymaps").fetchall()],
                        "config": {r['key']:r['value'] for r in con.execute("SELECT * FROM config").fetchall()},
                        "proxies": [dict(r) for r in con.execute("SELECT * FROM proxies").fetchall()],
                        "local_stock": [dict(r) for r in con.execute("SELECT * FROM local_stock").fetchall()],
                        "generated_at": get_vn_time()
                    }
            with open(AUTO_BACKUP_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False)
        except: pass

# ==============================================================================
#   PHẦN 5: API LOGIC
# ==============================================================================
def stock_logic(row):
    if row['provider_type'] == 'local':
        with db() as con: return con.execute("SELECT COUNT(*) FROM local_stock WHERE group_name=?", (row['group_name'],)).fetchone()[0]
    
    for _ in range(2):
        try:
            r = requests.get(f"{row['base_url'].rstrip('/')}/api/products.php", params={"api_key":row['api_key']}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET).json()
            if r.get("status")!="success": return 0
            all_p = []
            for c in r.get('categories',[]): all_p.extend(c.get('products',[]))
            pid = str(row['product_id'])
            for p in all_p:
                if str(int(float(str(p.get('id',0))))) == pid: return int(p.get('amount',0))
            return 0
        except: switch_proxy(); continue
    return 0

def fetch_logic(row, qty):
    if row['provider_type'] == 'local':
        with db_lock:
            with db() as con:
                items = con.execute("SELECT id, content FROM local_stock WHERE group_name=? LIMIT ?", (row['group_name'], qty)).fetchall()
                if not items: return []
                ids = [str(i['id']) for i in items]
                now = get_vn_time()
                for i in items: con.execute("INSERT INTO local_history(group_name,content,fetched_at) VALUES(?,?,?)", (row['group_name'], i['content'], now))
                con.execute(f"DELETE FROM local_stock WHERE id IN ({','.join(ids)})")
                con.commit()
                return [{"product": i['content']} for i in items]
    
    for _ in range(2):
        try:
            r = requests.post(f"{row['base_url'].rstrip('/')}/api/buy_product", data={"action":"buyProduct","id":row['product_id'],"amount":qty,"api_key":row['api_key']}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET).json()
            if r.get("status")!="success": return []
            d = r.get("data")
            if isinstance(d, list): return [{"product": json.dumps(x, ensure_ascii=False) if isinstance(x,dict) else str(x)} for x in d]
            return [{"product": json.dumps(d, ensure_ascii=False) if isinstance(d,dict) else str(d)} for _ in range(qty)]
        except: switch_proxy(); continue
    return []

# ==============================================================================
#   PHẦN 6: TEMPLATES (GIAO DIỆN GỐC - KHÔNG ĐỔI CSS)
# ==============================================================================

LOGIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Đăng Nhập Quản Trị - Quantum Gate</title>
    <style>
        :root { --primary: #5a7dff; --red: #f07167; --bg-light: #121212; --border: #343a40; --card-bg: #1c1c1e; --text-dark: #e9ecef; --text-light: #adb5bd; --input-bg: #2c2c2e; --shadow: 0 4px 12px rgba(0,0,0,0.4); --space-gradient-start: #0a0a1a; --space-gradient-end: #20204a; --star-color: #e0e0e0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; color: var(--text-dark); background: linear-gradient(135deg, var(--space-gradient-start) 0%, var(--space-gradient-end) 100%); min-height: 100vh; display: flex; justify-content: center; align-items: center; margin: 0; position: relative; overflow: hidden; }
        .login-container { width: 100%; max-width: 400px; padding: 40px 30px; border-radius: 12px; background: var(--card-bg); box-shadow: var(--shadow); position: relative; z-index: 10; text-align: left; }
        .logo { width: 40px; height: 40px; background: linear-gradient(45deg, #3a86ff, #5a7dff); border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 20px; color: white; margin-right: 15px; font-weight: bold; box-shadow: 0 0 10px rgba(90, 125, 255, 0.5); }
        h1 { font-size: 28px; font-weight: 700; color: var(--text-dark); margin: 0 0 10px 0; }
        input { width: 100%; padding: 14px 16px; margin-bottom: 30px; border: 1px solid var(--border); border-radius: 10px; box-sizing: border-box; background: var(--input-bg); color: var(--text-dark); font-size: 16px; }
        button { width: 100%; padding: 15px 16px; border-radius: 10px; border: none; background: linear-gradient(90deg, #3a86ff, #5a7dff); color: #fff; cursor: pointer; font-weight: 700; font-size: 16px; }
        .flash-alert { padding: 12px; margin-bottom: 20px; border-radius: 8px; font-weight: 600; background-color: #f8d7da; border-color: #f5c2c7; color: #842029; }
        #space-background { position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; overflow: hidden; z-index: 0; }
        .star { position: absolute; background-color: var(--star-color); border-radius: 50%; opacity: 0; animation: twinkle 5s infinite ease-in-out; z-index: 0; }
        @keyframes twinkle { 0%, 100% { opacity: 0; transform: scale(0.5); } 50% { opacity: 1; transform: scale(1.2); } }
    </style>
</head>
<body>
<div id="space-background"></div>
<div class="login-container">
    <div style="display:flex;align-items:center;margin-bottom:30px;"><div class="logo">∞</div><div><p style="font-size:16px;font-weight:600;margin:0">QUANTUM SECURITY GATE</p></div></div>
    <h1>Đăng nhập</h1>
    <p style="font-size:14px;color:var(--text-light);margin-bottom:25px;">Nhập mật khẩu quản trị để truy cập DashBoard.</p>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}{% for category, message in messages %}<div class="flash-alert {{ category }}">{{ message }}</div>{% endfor %}{% endif %}
    {% endwith %}
    <form method="post"><input type="password" name="admin_secret" placeholder="Nhập mật khẩu..." required autofocus><button type="submit">🚀 Truy Cập</button></form>
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
    :root { --primary: #5a7dff; --green: #20c997; --red: #f07167; --blue: #3a86ff; --gray: #adb5bd; --shadow: 0 4px 12px rgba(0,0,0,0.2); --bg-light: #121212; --border: #343a40; --card-bg: #1c1c1e; --text-dark: #e9ecef; --text-light: #adb5bd; --input-bg: #2c2c2e; --code-bg: #343a40; --star-color: #e0e0e0; }
    :root[data-theme="light"] { --primary: #0d6efd; --green: #198754; --red: #dc3545; --blue: #0d6efd; --gray: #6c757d; --shadow: 0 4px 12px rgba(0,0,0,0.05); --bg-light: #f8f9fa; --border: #dee2e6; --card-bg: #ffffff; --text-dark: #212529; --text-light: #495057; --input-bg: #ffffff; --code-bg: #e9ecef; --star-color: #888888; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; padding: 28px; color: var(--text-dark); background: linear-gradient(135deg, var(--bg-light) 0%, #20204a 100%); line-height: 1.6; min-height: 100vh; margin: 0; position: relative; overflow-x: hidden; }
    .card { border: 1px solid var(--border); border-radius: 12px; padding: 24px; margin-bottom: 24px; background: var(--card-bg); box-shadow: var(--shadow); position: relative; z-index: 10; }
    .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; align-items: end; }
    .col-2 { grid-column: span 2; } .col-3 { grid-column: span 3; } .col-4 { grid-column: span 4; } .col-6 { grid-column: span 6; } .col-8 { grid-column: span 8; } .col-12 { grid-column: span 12; }
    label { font-size: 12px; font-weight: 700; text-transform: uppercase; color: var(--text-light); margin-bottom: 6px; display: block; }
    input, select, textarea { width: 100%; padding: 12px 14px; border: 1px solid var(--border); border-radius: 8px; box-sizing: border-box; background: var(--input-bg); color: var(--text-dark); font-size: 14px; transition: border-color 0.2s, box-shadow 0.2s; font-family: monospace; }
    input:focus { border-color: var(--primary); outline: none; box-shadow: 0 0 0 3px rgba(90, 125, 255, 0.25); }
    button, .btn { padding: 10px 20px; border-radius: 8px; border: none; background: var(--primary); color: #fff; font-weight: 600; cursor: pointer; transition: filter 0.2s, transform 0.1s; }
    button:hover, .btn:hover { filter: brightness(1.1); transform: translateY(-1px); }
    .btn.red { background: var(--red); } .btn.green { background: var(--green); } .btn.blue { background: var(--blue); } .btn.gray { background: var(--gray); }
    .btn.small { padding: 6px 12px; font-size: 12px; }
    table { width: 100%; border-collapse: collapse; margin-top: 15px; font-size: 13px; }
    th, td { padding: 12px 15px; border-bottom: 1px solid var(--border); text-align: left; vertical-align: middle; }
    th { font-size: 12px; text-transform: uppercase; color: var(--text-light); letter-spacing: 0.5px; }
    details.folder { border: 1px solid var(--border); border-radius: 10px; margin-bottom: 15px; overflow: hidden; }
    details.folder > summary { padding: 15px 20px; cursor: pointer; font-weight: 700; font-size: 16px; background: var(--card-bg); color: var(--primary); list-style: none; }
    details.folder > .content { padding: 20px; background: var(--bg-light); border-top: 1px solid var(--border); }
    details.provider { margin-top: 15px; border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }
    details.provider > summary { padding: 12px 15px; cursor: pointer; font-weight: 600; font-size: 14px; background: #2a2a2d; color: #fff; }
    details.provider > .content { padding: 0; background: transparent; }
    .provider-table { width: 100%; border-collapse: collapse; }
    .provider-table th { background: #1f1f22; font-size: 11px; color: #aaa; padding: 10px 15px; border-bottom: 1px solid #333; }
    .provider-table td { border-bottom: 1px solid #333; padding: 10px 15px; font-size: 13px; color: #e0e0e0; white-space: nowrap; }
    .truncate-sku-cell { white-space: nowrap; overflow: hidden; max-width: 300px; display: block; font-size: 11px; }
    .badge-key { display: inline-block; background: rgba(58, 134, 255, 0.15); color: #5a7dff; padding: 4px 8px; border-radius: 4px; font-family: monospace; font-weight: bold; border: 1px solid rgba(58, 134, 255, 0.3); white-space: nowrap; }
    .badge-url { background: #343a40; color: #adb5bd; padding: 3px 6px; border-radius: 4px; font-size: 12px; font-family: monospace; }
    .space-background { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
    .star { position: absolute; background-color: var(--star-color); border-radius: 50%; opacity: 0; animation: twinkle 5s infinite; }
    .astronaut { position: absolute; width: 120px; height: 120px; background-image: url('https://freepng.flyclipart.com/thumb/cat-astronaut-space-suit-moon-outer-space-png-sticker-31913.png'); background-size: contain; animation: floatAstronaut 25s infinite ease-in-out; z-index: 1; opacity: 0.8; pointer-events: none; }
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
            <label>Provider Type</label>
            <input class="mono" name="provider_type" list="ptypes" placeholder="mail72h / local" required oninput="checkProviderType(this)" id="pt_input">
            <datalist id="ptypes"><option value="mail72h"><option value="local"></datalist>
        </div>
        <div class="col-4" id="div_base_url"><label>Base URL (API)</label><input class="mono" name="base_url" placeholder="https://api.website.com"></div>
      </div>
      <div class="row">
         <div class="col-2"><label>SKU</label><input class="mono" name="sku" required></div>
         <div class="col-3"><label>Input Key (Mã bán)</label><input class="mono" name="input_key" required></div>
         <div class="col-2" id="div_prod_id"><label>Product ID</label><input class="mono" name="product_id" placeholder="ID..."></div>
         <div class="col-3" id="div_api_key"><label>API Key</label><input class="mono" name="api_key" type="password"></div>
         <div class="col-2"><button type="submit" style="width: 100%; height: 42px; margin-top: 20px;">Lưu Key</button></div>
      </div>
    </form>
    
    <details style="margin-top: 15px; border-top: 1px dashed var(--border); padding-top: 10px;">
        <summary style="cursor: pointer; color: var(--green); font-weight: bold;">➕ Thêm Key Hàng Loạt (Dành cho Local)</summary>
        <form method="post" action="{{ url_for('admin_add_keymap_bulk') }}" style="margin-top: 15px;">
            <div class="row">
                <div class="col-4"><label>Group Name</label><input class="mono" name="group_name" required placeholder="Nhập tên nhóm..."></div>
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
                  <thead><tr><th style="width: 25%;">SKU</th><th style="width: 25%;">INPUT KEY</th><th style="width: 20%;">BASE URL</th><th style="width: 5%;">ID</th><th style="width: 5%;">ACTIVE</th><th style="width: 20%;">HÀNH ĐỘNG</th></tr></thead>
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
                            <button class="btn gray small edit-btn" data-group="{{ k.group_name }}" data-provider="{{ k.provider_type }}" data-url="{{ k.base_url }}" data-sku="{{ k.sku }}" data-key="{{ k.input_key }}" data-pid="{{ k.product_id }}" data-apikey="{{ k.api_key }}" type="button">Sửa ✏️</button>
                            <form method="post" action="{{ url_for('admin_toggle_key', kmid=k.id) }}" style="margin:0;"><button class="btn blue small" type="submit">{{ 'Tắt' if k.is_active else 'Bật' }}</button></form>
                            <form method="post" action="{{ url_for('admin_delete_key', kmid=k.id) }}" onsubmit="return confirm('Xác nhận xóa key này?');" style="margin:0;"><button class="btn red small" type="submit">Xoá</button></form>
                        </div>
                      </td>
                    </tr>
                  {% endfor %}
                  </tbody>
                </table>
                <button class="btn green small add-key-helper" style="margin: 10px;" data-provider="{{ provider }}" data-baseurl="{{ keys[0]['base_url'] if keys else '' }}" data-apikey="{{ keys[0]['api_key'] if keys else '' }}" data-groupname="{{ folder }}">+ Thêm Key vào Provider này</button>
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
        <p style="color: var(--text-light); margin-bottom: 15px;">Render sẽ xóa sạch dữ liệu khi Restart. Hãy tải file này thường xuyên.</p>
        <a href="{{ url_for('admin_backup_download') }}" class="btn green">⬇️ Tải Xuống Backup</a>
      </div>
      <div class="col-6" style="border-left: 1px solid var(--border); padding-left: 20px;">
        <h4>Restore Thủ Công</h4>
        <p style="color: var(--text-light); margin-bottom: 15px;">Upload file JSON để khôi phục dữ liệu ngay lập tức.</p>
        <form method="post" action="{{ url_for('admin_backup_upload') }}" enctype="multipart/form-data" onsubmit="return confirm('Dữ liệu cũ sẽ bị xóa sạch?');"><input type="file" name="backup_file" accept=".json" required style="margin-bottom: 10px;"><button type="submit" class="btn red">⬆️ Upload & Restore</button></form>
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
            {% for g, c in local_stats.items() %}<div style="display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed var(--border);"><span><b style="color: var(--primary);">{{ g }}</b>: <span style="background: var(--input-bg); padding: 2px 6px; border-radius: 4px;">{{ c }} items</span></span><div><a href="{{ url_for('admin_local_stock_view', group=g) }}" class="btn blue small">Xem</a><form action="{{ url_for('admin_local_stock_clear') }}" method="post" style="display: inline;" onsubmit="return confirm('XÓA SẠCH kho {{g}}?');"><input type="hidden" name="group_name" value="{{ g }}"><button class="btn red small">Xóa</button></form></div></div>{% else %}<p style="text-align: center; color: var(--text-light); padding: 10px;">Kho đang trống.</p>{% endfor %}
        </div>
    </div>
    <div class="col-6 card">
        <h3>5. Quản Lý Proxy & Ping</h3>
        <div style="margin-bottom: 15px;">Proxy Đang Dùng: <code class="mono" style="color: var(--green); font-size: 1.1em;">{{ current_proxy or 'Direct Connection' }}</code></div>
        <form method="post" action="{{ url_for('admin_add_proxy') }}">
            <label>Thêm Danh Sách Proxy (ip:port)</label><textarea class="mono" name="proxies" rows="4" placeholder="ip:port&#10;ip:port:user:pass"></textarea><button type="submit" class="btn green" style="margin-top: 10px; width: 100%;">➕ Thêm Proxy</button>
        </form>
        <div style="margin-top: 20px; max-height: 200px; overflow-y: auto; border: 1px solid var(--border); border-radius: 6px;">
            <table style="margin: 0;"><thead><tr><th>Proxy</th><th>Status</th><th>Ping</th><th>Xóa</th></tr></thead><tbody>{% for p in proxies %}<tr><td class="mono" style="font-size: 11px;">{{ p.proxy_string }}</td><td style="font-weight: bold; color: {{ 'var(--green)' if p.is_live else 'var(--red)' }};">{{ 'LIVE' if p.is_live else 'DIE' }}</td><td>{{ "%.2f"|format(p.latency) }}s</td><td><form action="{{ url_for('admin_delete_proxy') }}" method="post"><input type="hidden" name="id" value="{{ p.id }}"><button class="btn red small" style="padding: 2px 6px;">x</button></form></td></tr>{% endfor %}</tbody></table>
        </div>
        <hr style="border-color: var(--border); margin: 25px 0;">
        <h4>🌐 Cấu Hình Ping (Anti-Sleep)</h4>
        <form method="post" action="{{ url_for('admin_save_ping') }}"><div class="row"><div class="col-8"><label>URL Web</label><input class="mono" name="ping_url" value="{{ ping.url }}" placeholder="https://..."></div><div class="col-4"><label>Chu kỳ (s)</label><input class="mono" name="ping_interval" type="number" value="{{ ping.interval }}" placeholder="300"></div></div><button type="submit" class="btn blue" style="width: 100%; margin-top: 15px;">Lưu Cấu Hình</button></form>
    </div>
  </div>
  <div class="card" style="padding: 20px;">
    <div class="row" style="align-items: center;">
      <div class="col-4"><label>Giao diện</label><select id="mode-switcher" class="mono"><option value="dark" {% if mode == 'dark' %}selected{% endif %}>Tối (Dark)</option><option value="light" {% if mode == 'light' %}selected{% endif %}>Sáng (Light)</option></select></div>
      <div class="col-4"><label>Hiệu ứng nền</label><select id="effect-switcher" class="mono"><option value="default" {% if effect == 'default' %}selected{% endif %}>Tắt Hiệu Ứng</option><option value="astronaut" {% if effect == 'astronaut' %}selected{% endif %}>Phi hành gia (Astronaut)</option><option value="snow" {% if effect == 'snow' %}selected{% endif %}>Tuyết Rơi (Snow)</option><option value="matrix" {% if effect == 'matrix' %}selected{% endif %}>Ma Trận (Matrix)</option><option value="rain" {% if effect == 'rain' %}selected{% endif %}>Mưa Rơi (Rain)</option><option value="particles" {% if effect == 'particles' %}selected{% endif %}>Hạt Kết Nối (Particles)</option><option value="sakura" {% if effect == 'sakura' %}selected{% endif %}>Hoa Anh Đào (Sakura)</option></select></div>
      <div class="col-4"><label>&nbsp;</label><form method="post" action="{{ url_for('logout') }}"><button class="btn red" type="submit" style="width: 100%;">Đăng Xuất Hệ Thống</button></form></div>
    </div>
  </div>
</div> 
<script>
function checkProviderType(input) {
    const val = input ? input.value : document.getElementById('pt_input').value;
    const isLocal = val === 'local';
    document.getElementById('div_prod_id').style.display = isLocal ? 'none' : 'block';
    document.getElementById('div_base_url').style.display = isLocal ? 'none' : 'block';
    document.getElementById('div_api_key').style.display = isLocal ? 'none' : 'block';
}
checkProviderType();

document.getElementById('effect-switcher').addEventListener('change', function() { document.cookie = `admin_effect=${this.value};path=/;max-age=31536000;SameSite=Lax`; location.reload(); });
document.getElementById('mode-switcher').addEventListener('change', function() { document.cookie = `admin_mode=${this.value};path=/;max-age=31536000;SameSite=Lax`; location.reload(); });

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
function createEffectCanvas(id) { if (document.getElementById(id)) return null; var canvas = document.createElement('canvas'); canvas.id = id; canvas.className = 'effect-canvas'; document.body.appendChild(canvas); var ctx = canvas.getContext('2d'); var W = window.innerWidth; var H = window.innerHeight; canvas.width = W; canvas.height = H; window.addEventListener('resize', function() { W = window.innerWidth; H = window.innerHeight; canvas.width = W; canvas.height = H; }); return { canvas, ctx, W, H }; }
</script>
{% if effect == 'astronaut' %}<script>(function() { const spaceBackground = document.getElementById('space-background'); if (!spaceBackground) return; for (let i = 0; i < 100; i++) { let star = document.createElement('div'); star.className = 'star'; star.style.width = star.style.height = `${Math.random() * 3 + 1}px`; star.style.left = `${Math.random() * 100}%`; star.style.top = `${Math.random() * 100}%`; star.style.animationDelay = `${Math.random() * 5}s`; spaceBackground.appendChild(star); } let astronaut = document.createElement('div'); astronaut.className = 'astronaut'; astronaut.style.left = '10%'; astronaut.style.top = '20%'; spaceBackground.appendChild(astronaut); })();</script>{% endif %}
{% if effect == 'snow' %}<script>(function() { var a = createEffectCanvas('snow-canvas'); if (!a) return; var ctx = a.ctx, W = a.W, H = a.H; var mp = 100; var flakes = []; for(var i = 0; i < mp; i++) { flakes.push({ x: Math.random() * W, y: Math.random() * H, r: Math.random() * 4 + 1, d: Math.random() * 100 }); } var angle = 0; function draw() { ctx.clearRect(0, 0, W, H); ctx.fillStyle = "rgba(255, 255, 255, 0.8)"; ctx.beginPath(); for(var i = 0; i < 100; i++) { var f = flakes[i]; ctx.moveTo(f.x, f.y); ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2, true); } ctx.fill(); update(); requestAnimationFrame(draw); } function update() { angle += 0.01; for(var i = 0; i < 100; i++) { var f = flakes[i]; f.y += Math.cos(angle + f.d) + 1 + f.r / 2; f.x += Math.sin(angle) * 2; if(f.x > W + 5 || f.x < -5 || f.y > H) { if(i % 3 > 0) { flakes[i] = {x: Math.random() * W, y: -10, r: f.r, d: f.d}; } else { if(Math.sin(angle) > 0) { flakes[i] = {x: -5, y: Math.random() * H, r: f.r, d: f.d}; } else { flakes[i] = {x: W + 5, y: Math.random() * H, r: f.r, d: f.d}; } } } } } draw(); })();</script>{% endif %}
{% if effect == 'matrix' %}<script>(function() { var a = createEffectCanvas('matrix-canvas'); if (!a) return; var ctx = a.ctx, W = a.W, H = a.H; var font_size = 14; var columns = Math.floor(W / font_size); var drops = []; for(var x = 0; x < columns; x++) drops[x] = 1; var chars = "0123456789ABCDEF@#$%^&*()".split(""); function draw() { ctx.clearRect(0, 0, W, H); ctx.fillStyle = "rgba(0, 0, 0, 0.05)"; ctx.fillRect(0, 0, W, H); ctx.fillStyle = "#0F0"; ctx.font = font_size + "px monospace"; for(var i = 0; i < drops.length; i++) { var text = chars[Math.floor(Math.random() * chars.length)]; ctx.fillText(text, i * font_size, drops[i] * font_size); if(drops[i] * font_size > H && Math.random() > 0.975) { drops[i] = 0; } drops[i]++; } } setInterval(draw, 33); })();</script>{% endif %}
{% if effect == 'rain' %}<script>(function() { var a = createEffectCanvas('rain-canvas'); if (!a) return; var ctx = a.ctx, W = a.W, H = a.H; var drops = []; var dropCount = 500; for (var i = 0; i < dropCount; i++) { drops.push({ x: Math.random() * W, y: Math.random() * H, l: Math.random() * 1, v: Math.random() * 4 + 4 }); } function draw() { ctx.clearRect(0, 0, W, H); ctx.strokeStyle = "rgba(174, 194, 224, 0.5)"; ctx.lineWidth = 1; ctx.beginPath(); for (var i = 0; i < dropCount; i++) { var d = drops[i]; ctx.moveTo(d.x, d.y); ctx.lineTo(d.x, d.y + d.l * 5); d.y += d.v; if (d.y > H) { d.y = -20; d.x = Math.random() * W; } } ctx.stroke(); requestAnimationFrame(draw); } draw(); })();</script>{% endif %}
{% if effect == 'particles' %}<script>(function() { var a = createEffectCanvas('particles-canvas'); if (!a) return; var ctx = a.ctx, W = a.W, H = a.H; var particleCount = 80; var particles = []; for (var i = 0; i < particleCount; i++) { particles.push({ x: Math.random() * W, y: Math.random() * H, vx: (Math.random() - 0.5) * 1, vy: (Math.random() - 0.5) * 1 }); } function draw() { ctx.clearRect(0, 0, W, H); ctx.fillStyle = "rgba(200, 200, 200, 0.5)"; ctx.strokeStyle = "rgba(200, 200, 200, 0.1)"; for (var i = 0; i < particles.length; i++) { var p = particles[i]; ctx.beginPath(); ctx.arc(p.x, p.y, 2, 0, Math.PI * 2); ctx.fill(); p.x += p.vx; p.y += p.vy; if (p.x < 0 || p.x > W) p.vx *= -1; if (p.y < 0 || p.y > H) p.vy *= -1; for (var j = i + 1; j < particles.length; j++) { var p2 = particles[j]; var dx = p.x - p2.x; var dy = p.y - p2.y; var dist = Math.sqrt(dx * dx + dy * dy); if (dist < 100) { ctx.beginPath(); ctx.moveTo(p.x, p.y); ctx.lineTo(p2.x, p2.y); ctx.stroke(); } } } requestAnimationFrame(draw); } draw(); })();</script>{% endif %}
{% if effect == 'sakura' %}<script>(function() { var a = createEffectCanvas('sakura-canvas'); if (!a) return; var ctx = a.ctx, W = a.W, H = a.H; var mp = 60; var petals = []; for(var i = 0; i < mp; i++) { petals.push({ x: Math.random() * W, y: Math.random() * H, r: Math.random() * 4 + 2, d: Math.random() * mp, c: (Math.random() > 0.5) ? "#ffc0cb" : "#ffffff" }); } var angle = 0; function draw() { ctx.clearRect(0, 0, W, H); for(var i = 0; i < 60; i++) { var p = petals[i]; ctx.fillStyle = p.c; ctx.globalAlpha = 0.7; ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2, true); ctx.fill(); } angle += 0.01; for(var i = 0; i < 60; i++) { var p = petals[i]; p.y += Math.cos(angle + p.d) + 1 + p.r / 2; p.x += Math.sin(angle); if(p.x > W + 5 || p.x < -5 || p.y > H) { p.x = Math.random() * W; p.y = -10; } } requestAnimationFrame(draw); } draw(); })();</script>{% endif %}
</body>
</html>
"""

STOCK_VIEW_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Chi tiết kho {{ group }}</title>
    <style>body{background:#121212;color:#e9ecef;font-family:monospace;padding:20px;}h2{color:#5a7dff;border-bottom:1px solid #333;padding-bottom:10px;display:flex;justify-content:space-between;align-items:center;}a{color:#5a7dff;text-decoration:none;font-size:16px;}a:hover{text-decoration:underline;}table{width:100%;border-collapse:collapse;margin-top:20px;}th,td{border:1px solid #333;padding:10px;text-align:left;}th{background:#1c1c1e;color:#adb5bd;}tr:hover{background:#1c1c1e;}button{cursor:pointer;padding:6px 12px;background:#dc3545;color:white;border:none;border-radius:4px;font-weight:bold;}button:hover{background:#bb2d3b;}.tools-bar{display:flex;gap:10px;margin-bottom:15px;}input[type="text"]{padding:8px;border-radius:4px;border:1px solid #444;background:#222;color:#fff;width:300px;}</style>
</head>
<body>
    <h2>
        <span>📦 Group: {{ group }} ({{ items|length }} items)</span>
        <div>
             <a href="{{ url_for('admin_local_stock_download', group=group) }}" style="margin-right: 15px; font-size: 14px; background:#20c997; color:#000; padding:4px 8px; border-radius:4px; text-decoration:none;">📥 Tải File TXT</a>
             <a href="{{ url_for('admin_local_history_view') }}?group={{ group }}" style="margin-right: 15px; font-size: 14px;">📜 Xem Lịch Sử</a>
             <form action="{{ url_for('admin_local_stock_dedup') }}" method="post" style="display:inline;" onsubmit="return confirm('Bạn có chắc muốn xóa các dòng trùng lặp?');"><input type="hidden" name="group_name" value="{{ group }}"><button style="background: #ffc107; color: #000;">🧹 Quét Trùng</button></form>
        </div>
    </h2>
    <div class="tools-bar"><a href="{{ url_for('admin_index') }}#local-stock">🔙 Quay lại Dashboard</a><form method="get" style="margin-left: auto;"><input type="hidden" name="group" value="{{ group }}"><input type="text" name="q" placeholder="Tìm kiếm acc..." value="{{ request.args.get('q', '') }}"><button type="submit" style="background: #0d6efd;">Tìm</button></form></div>
    <table>
        <thead><tr><th style="width: 50px;">STT</th><th>Nội dung</th><th style="width: 200px;">Ngày thêm</th><th style="width: 100px;">Hành động</th></tr></thead>
        <tbody>{% for i in items %}<tr><td>{{ loop.index }}</td><td style="word-break: break-all; color: #20c997;">{{ i.content }}</td><td>{{ i.added_at }}</td><td><form action="{{ url_for('admin_local_stock_delete_one') }}" method="post" onsubmit="return confirm('Xóa dòng này?');"><input type="hidden" name="id" value="{{ i.id }}"><input type="hidden" name="group" value="{{ group }}"><button type="submit">Xóa</button></form></td></tr>{% else %}<tr><td colspan="4" style="text-align: center; padding: 30px; color: #adb5bd;">Không tìm thấy dữ liệu phù hợp.</td></tr>{% endfor %}</tbody>
    </table>
</body>
</html>
"""

HISTORY_VIEW_TPL = """
<!doctype html>
<html data-theme="dark"><head><meta charset="utf-8" /><title>Lịch sử lấy hàng</title><style>body{background:#121212;color:#e9ecef;font-family:monospace;padding:20px;}h2{color:#a0a0ff;border-bottom:1px solid #333;padding-bottom:10px;}a{color:#5a7dff;text-decoration:none;font-size:16px;}table{width:100%;border-collapse:collapse;margin-top:20px;}th,td{border:1px solid #333;padding:10px;text-align:left;}th{background:#1c1c1e;color:#adb5bd;}tr:hover{background:#1c1c1e;}</style></head>
<body><h2>📜 Lịch Sử Xuất Kho ({{ group if group else 'Tất Cả' }})</h2><a href="{{ url_for('admin_local_stock_view', group=group) if group else url_for('admin_index') }}">🔙 Quay lại</a>
    <table><thead><tr><th style="width: 50px;">ID</th><th>Group</th><th>Nội dung đã lấy</th><th style="width: 200px;">Thời gian lấy (VN)</th></tr></thead><tbody>{% for i in items %}<tr><td>{{ i.id }}</td><td>{{ i.group_name }}</td><td style="word-break: break-all; color: #ffc107;">{{ i.content }}</td><td>{{ i.fetched_at }}</td></tr>{% else %}<tr><td colspan="4" style="text-align: center; padding: 30px; color: #adb5bd;">Chưa có lịch sử nào.</td></tr>{% endfor %}</tbody></table>
</body></html>
"""

# ==============================================================================
#   PHẦN 7: ROUTES
# ==============================================================================
def require_admin():
    if request.cookies.get("logged_in") != ADMIN_SECRET: abort(redirect(url_for('login')))

@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("admin_secret") == ADMIN_SECRET:
            r = make_response(redirect(url_for("admin_index")))
            r.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000)
            return r
        else: flash("Mật khẩu Admin không chính xác.", "error")
    return render_template_string(LOGIN_TPL)

@app.route("/logout", methods=["POST"])
def logout():
    r = make_response(redirect(url_for("login")))
    r.set_cookie("logged_in", "", max_age=0) 
    return r

@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        maps = con.execute("SELECT * FROM keymaps ORDER BY group_name, provider_type, sku, id").fetchall()
        grouped_data = {}
        for key in maps:
            f = key['group_name'] or 'DEFAULT' 
            p = key['provider_type']
            if f not in grouped_data: grouped_data[f] = {}
            if p not in grouped_data[f]: grouped_data[f][p] = []
            grouped_data[f][p].append(key)
        
        proxies = con.execute("SELECT * FROM proxies ORDER BY is_live DESC, latency ASC").fetchall()
        ping = {"url": con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()['value'], 
                "interval": con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()['value']}
        
        stock_rows = con.execute("SELECT group_name, COUNT(*) as cnt FROM local_stock GROUP BY group_name").fetchall()
        local_stats = {r['group_name']: r['cnt'] for r in stock_rows}
        local_groups = [r['group_name'] for r in stock_rows]

    effect = request.cookies.get('admin_effect', 'astronaut')
    mode = request.cookies.get('admin_mode', 'dark') 
    return render_template_string(ADMIN_TPL, grouped_data=grouped_data, proxies=proxies, current_proxy=CURRENT_PROXY_STRING, ping=ping, local_stats=local_stats, local_groups=local_groups, effect=effect, mode=mode)

@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    f = request.form
    try:
        pt = f.get("provider_type", "mail72h").strip()
        pid = f.get("product_id", "").strip()
        # FIX: Nếu local thì id = 0
        if pt == 'local': pid = 0
        
        with db() as con:
            con.execute("""INSERT INTO keymaps(group_name, sku, input_key, product_id, api_key, is_active, provider_type, base_url) VALUES(?,?,?,?,?,1,?,?) ON CONFLICT(input_key) DO UPDATE SET group_name=excluded.group_name, sku=excluded.sku, product_id=excluded.product_id, api_key=excluded.api_key, provider_type=excluded.provider_type, base_url=excluded.base_url, is_active=1""", 
                        (f.get("group_name"), f.get("sku"), f.get("input_key"), pid, f.get("api_key"), pt, f.get("base_url")))
            con.commit()
        flash(f"Đã lưu key '{f.get('input_key')}' thành công!", "success")
    except Exception as e: flash(f"Lỗi: {e}", "error")
    return redirect(url_for("admin_index"))

# NEW: Add Bulk Key for Local
@app.route("/admin/keymap/bulk", methods=["POST"])
def admin_add_keymap_bulk():
    require_admin()
    f = request.form
    grp = f.get("group_name", "").strip()
    prefix = f.get("sku_prefix", "").strip()
    keys = f.get("bulk_keys", "").strip()
    
    if not grp or not keys:
        flash("Thiếu tên Group hoặc danh sách Key", "error")
        return redirect(url_for("admin_index"))
    
    cnt = 0
    with db() as con:
        for k in keys.split('\n'):
            k = k.strip()
            if k:
                sku = f"{prefix}{k}" if prefix else k
                try:
                    con.execute("INSERT INTO keymaps(group_name,sku,input_key,product_id,is_active,provider_type,base_url,api_key) VALUES(?,?,?,0,1,'local','','') ON CONFLICT(input_key) DO NOTHING", (grp, sku, k))
                    cnt+=1
                except: pass
        con.commit()
    flash(f"Đã thêm {cnt} key hàng loạt vào nhóm '{grp}'", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/keymap/delete/<int:kmid>", methods=["POST"])
def admin_delete_key(kmid):
    require_admin()
    with db() as con: con.execute("DELETE FROM keymaps WHERE id=?", (kmid,)); con.commit()
    flash("Đã xóa key.", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/keymap/toggle/<int:kmid>", methods=["POST"])
def admin_toggle_key(kmid):
    require_admin()
    with db() as con: con.execute("UPDATE keymaps SET is_active = NOT is_active WHERE id=?", (kmid,)); con.commit()
    return redirect(url_for("admin_index"))

@app.route("/admin/local-stock/add", methods=["POST"])
def admin_local_stock_add():
    require_admin()
    grp = request.form.get("group_name", "").strip()
    content = request.form.get("content", "").strip()
    file = request.files.get("stock_file")
    lines = []
    if file and file.filename: lines = file.read().decode('utf-8', errors='ignore').splitlines()
    elif content: lines = content.split('\n')
    
    c=0
    with db() as con:
        now = get_vn_time()
        for l in lines:
            if l.strip(): con.execute("INSERT INTO local_stock(group_name, content, added_at) VALUES(?,?,?)", (grp, l.strip(), now)); c+=1
        con.commit()
    flash(f"Đã thêm {c} dòng vào kho '{grp}'.", "success")
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/local-stock/view")
def admin_local_stock_view():
    require_admin()
    grp = request.args.get("group")
    q = request.args.get("q", "").strip()
    with db() as con:
        if q: items = con.execute("SELECT * FROM local_stock WHERE group_name=? AND content LIKE ?", (grp, f"%{q}%")).fetchall()
        else: items = con.execute("SELECT * FROM local_stock WHERE group_name=?", (grp,)).fetchall()
    return render_template_string(STOCK_VIEW_TPL, group=grp, items=items, request=request)

# NEW: Download Route
@app.route("/admin/local-stock/download")
def admin_local_stock_download():
    require_admin()
    grp = request.args.get("group")
    with db() as con: rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (grp,)).fetchall()
    # Xuất ra file .txt, mỗi dòng là 1 content
    out = "\n".join([r['content'] for r in rows])
    resp = make_response(out)
    resp.headers["Content-Disposition"] = f"attachment; filename={grp}.txt"
    return resp

@app.route("/admin/local-history/view")
def admin_local_history_view():
    require_admin()
    grp = request.args.get("group")
    with db() as con:
        if grp: items = con.execute("SELECT * FROM local_history WHERE group_name=? ORDER BY id DESC LIMIT 500", (grp,)).fetchall()
        else: items = con.execute("SELECT * FROM local_history ORDER BY id DESC LIMIT 500").fetchall()
    return render_template_string(HISTORY_VIEW_TPL, group=grp, items=items)

@app.route("/admin/local-stock/dedup", methods=["POST"])
def admin_local_stock_dedup():
    require_admin()
    grp = request.form.get("group_name")
    with db() as con: con.execute("DELETE FROM local_stock WHERE group_name=? AND id NOT IN (SELECT MIN(id) FROM local_stock WHERE group_name=? GROUP BY content)", (grp, grp)); con.commit()
    flash(f"Đã quét trùng {grp}.", "success")
    return redirect(url_for("admin_local_stock_view", group=grp))

@app.route("/admin/local-stock/delete-one", methods=["POST"])
def admin_local_stock_delete_one():
    require_admin()
    with db() as con: con.execute("DELETE FROM local_stock WHERE id=?", (request.form.get("id"),)); con.commit()
    return redirect(url_for("admin_local_stock_view", group=request.form.get("group")))

@app.route("/admin/local-stock/clear", methods=["POST"])
def admin_local_stock_clear():
    require_admin()
    grp = request.form.get("group_name")
    with db() as con: con.execute("DELETE FROM local_stock WHERE group_name=?", (grp,)); con.commit()
    flash(f"Đã xóa sạch kho '{grp}'.", "success")
    return redirect(url_for("admin_index") + "#local-stock")

@app.route("/admin/proxy/add", methods=["POST"])
def admin_add_proxy():
    require_admin()
    b = request.form.get("proxies", "").strip()
    with db() as con:
        for l in b.split('\n'): 
            if l.strip(): con.execute("INSERT OR IGNORE INTO proxies (proxy_string, is_live, last_checked) VALUES (?, 0, ?)", (l.strip(), get_vn_time()))
        con.commit()
        if not CURRENT_PROXY_STRING: select_best_proxy(con)
    flash("Đã thêm proxy.", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/proxy/delete", methods=["POST"])
def admin_delete_proxy():
    require_admin()
    with db() as con: con.execute("DELETE FROM proxies WHERE id=?", (request.form.get("id"),)); con.commit()
    return redirect(url_for("admin_index"))

@app.route("/admin/ping/save", methods=["POST"])
def admin_save_ping():
    require_admin()
    with db() as con:
        con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('ping_url', ?)", (request.form.get("ping_url"),))
        con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('ping_interval', ?)", (request.form.get("ping_interval"),))
        con.commit()
    flash("Đã lưu Ping config.", "success")
    return redirect(url_for("admin_index"))

@app.route("/admin/backup/download")
def admin_backup_download():
    require_admin()
    auto_backup()
    if os.path.exists(AUTO_BACKUP_FILE):
        return jsonify(json.load(open(AUTO_BACKUP_FILE)))
    return "Chưa có file backup", 404

@app.route("/admin/backup/upload", methods=["POST"])
def admin_backup_upload():
    require_admin()
    f = request.files.get('backup_file')
    if f:
        try:
            d = json.load(f)
            with db() as con:
                con.execute("DELETE FROM keymaps"); con.execute("DELETE FROM proxies"); con.execute("DELETE FROM local_stock")
                kms = d.get('keymaps', []) if isinstance(d, dict) else d
                for k in kms: con.execute("INSERT INTO keymaps(sku,input_key,product_id,is_active,group_name,provider_type,base_url,api_key) VALUES(?,?,?,?,?,?,?,?)", (k.get('sku'), k.get('input_key'), k.get('product_id'), k.get('is_active',1), k.get('group_name'), k.get('provider_type'), k.get('base_url'), k.get('api_key')))
                if isinstance(d, dict):
                    for p in d.get('proxies', []): con.execute("INSERT OR IGNORE INTO proxies(proxy_string,is_live,latency,last_checked) VALUES(?,?,?,?)", (p.get('proxy_string'), 0, 9999.0, get_vn_time()))
                    for l in d.get('local_stock', []): con.execute("INSERT INTO local_stock(group_name, content, added_at) VALUES(?,?,?)", (l.get('group_name'), l.get('content'), l.get('added_at')))
                    for k,v in d.get('config', {}).items(): con.execute("INSERT OR REPLACE INTO config(key,value) VALUES(?,?)", (k,str(v)))
                con.commit()
            flash("Restore thành công", "success")
        except: flash("Lỗi file backup", "error")
    return redirect(url_for("admin_index"))

@app.route("/stock")
def stock():
    with db() as con: r = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (request.args.get("key", "").strip(),)).fetchone()
    return jsonify({"sum": stock_logic(r) if r else 0})

@app.route("/fetch")
def fetch():
    try: q = int(request.args.get("quantity", "1"))
    except: return jsonify([])
    with db() as con: r = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (request.args.get("key", "").strip(),)).fetchone()
    return jsonify(fetch_logic(r, q) if r else [])

# ==============================================================================
#   STARTUP
# ==============================================================================
init_db()
if not proxy_checker_started: proxy_checker_started = True; threading.Thread(target=proxy_checker, daemon=True).start()
if not ping_service_started: ping_service_started = True; threading.Thread(target=ping_service, daemon=True).start()
if not auto_backup_started: auto_backup_started = True; threading.Thread(target=auto_backup, daemon=True).start()

try:
    with db() as con: 
        p = con.execute("SELECT value FROM config WHERE key='selected_proxy_string'").fetchone()
        if p and p['value']: set_current_proxy(p['value'])
        else: select_best_proxy(con)
except: pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
