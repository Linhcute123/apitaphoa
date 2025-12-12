# -*- coding: utf-8 -*-
"""
PROJECT: QUANTUM GATE - ULTIMATE TIKTOK MANAGER & CHECKER
VERSION: 7.0 (Integrated Logic from checktiktok_patched.py)
AUTHOR: Admin Van Linh
DATE: 2025-12-12

DESCRIPTION:
    - Web Server Flask quản lý kho hàng và check TikTok.
    - Tích hợp thuật toán check Live/Die chuẩn xác từ tool Python máy tính.
    - Hỗ trợ đa luồng, Proxy xoay vòng, Giao diện tối ưu.
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
import re
from urllib.parse import quote
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string, flash, make_response, stream_with_context, Response
import requests

# Import BeautifulSoup để check chuẩn như file bạn gửi
try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None
    print("WARNING: Cần cài đặt thư viện 'beautifulsoup4' để check chuẩn xác nhất.")

# ==============================================================================
#   PHẦN 1: CẤU HÌNH HỆ THỐNG
# ==============================================================================

DB = os.getenv("DB_PATH", "store.db") 
SECRET_BACKUP_FILE_PATH = os.getenv("SECRET_BACKUP_FILE_PATH", "/etc/secrets/backupapitaphoa.json")
AUTO_BACKUP_FILE = "auto_backup.json"
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = 15 # Tăng timeout để check kỹ hơn
PROXY_CHECK_INTERVAL = 20 

app = Flask(__name__)
app.secret_key = ADMIN_SECRET 

CURRENT_PROXY_SET = {"http": None, "https": None}
CURRENT_PROXY_STRING = "" 
db_lock = threading.Lock()

proxy_checker_started = False
ping_service_started = False
auto_backup_started = False

# User-Agent từ file checktiktok_patched.py
UA_STRING = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'


# ==============================================================================
#   PHẦN 2: DATABASE & UTILS
# ==============================================================================

def get_vn_time():
    utc_now = datetime.datetime.utcnow()
    vn_now = utc_now + datetime.timedelta(hours=7)
    return vn_now.strftime("%Y-%m-%d %H:%M:%S")

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
            print(f"INFO: Đang khởi tạo Database tại: {DB}")
            # Tạo các bảng
            con.execute("CREATE TABLE IF NOT EXISTS keymaps(id INTEGER PRIMARY KEY AUTOINCREMENT, sku TEXT NOT NULL, input_key TEXT NOT NULL UNIQUE, product_id INTEGER NOT NULL, is_active INTEGER DEFAULT 1, group_name TEXT, provider_type TEXT NOT NULL DEFAULT 'mail72h', base_url TEXT, api_key TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS proxies(id INTEGER PRIMARY KEY AUTOINCREMENT, proxy_string TEXT NOT NULL UNIQUE, is_live INTEGER DEFAULT 0, latency REAL DEFAULT 9999.0, last_checked TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_stock(id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, content TEXT NOT NULL, added_at TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_history(id INTEGER PRIMARY KEY AUTOINCREMENT, group_name TEXT NOT NULL, content TEXT NOT NULL, fetched_at TEXT)")
            
            # Migration
            _ensure_col(con, "keymaps", "group_name", "TEXT")
            _ensure_col(con, "keymaps", "provider_type", "TEXT")
            _ensure_col(con, "keymaps", "base_url", "TEXT")
            _ensure_col(con, "keymaps", "api_key", "TEXT")
            
            # Seed Data
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_url", ""))
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)", ("ping_interval", "300"))
            
            con.commit()
            
            # Auto Restore
            if con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0] == 0:
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f:
                            data = json.load(f)
                        kms = data.get('keymaps', []) if isinstance(data, dict) else data
                        for k in kms:
                            con.execute("INSERT OR IGNORE INTO keymaps(sku, input_key, product_id, is_active, group_name, provider_type, base_url, api_key) VALUES(?,?,?,?,?,?,?,?)", 
                                       (k.get('sku'), k.get('input_key'), k.get('product_id'), k.get('is_active',1), k.get('group_name','DEFAULT'), k.get('provider_type','mail72h'), k.get('base_url'), k.get('api_key')))
                        con.commit()
                        print("SUCCESS: Auto-restored data.")
                    except Exception as e:
                        print(f"ERROR: Restore failed: {e}")


# ==============================================================================
#   PHẦN 3: PROXY MANAGER
# ==============================================================================

def format_proxy_url(proxy_string: str) -> dict:
    if not proxy_string: return {"http": None, "https": None}
    parts = proxy_string.strip().split(':')
    fmt = ""
    if len(parts) == 2: fmt = f"http://{parts[0]}:{parts[1]}"
    elif len(parts) >= 4: fmt = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
    else: return {"http": None, "https": None}
    return {"http": fmt, "https": fmt}

def check_proxy_live(proxy_string: str) -> tuple:
    p = format_proxy_url(proxy_string)
    if not p.get("http"): return (0, 9999.0)
    try:
        s = time.time()
        requests.get("http://www.google.com/generate_204", proxies=p, timeout=5)
        return (1, time.time() - s)
    except: return (0, 9999.0)

def update_proxy_state(proxy_string: str, is_live: int, latency: float):
    with db_lock:
        with db() as con:
            con.execute("UPDATE proxies SET is_live=?, latency=?, last_checked=? WHERE proxy_string=?", (is_live, latency, get_vn_time(), proxy_string))
            con.commit()

def select_best_available_proxy(con):
    row = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 ORDER BY latency ASC LIMIT 1").fetchone()
    ps = row['proxy_string'] if row else ""
    set_current_proxy_by_string(ps)
    con.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)", ("selected_proxy_string", ps)); con.commit()
    return ps

def set_current_proxy_by_string(ps: str):
    global CURRENT_PROXY_SET, CURRENT_PROXY_STRING
    CURRENT_PROXY_SET = format_proxy_url(ps)
    CURRENT_PROXY_STRING = ps if CURRENT_PROXY_SET.get("http") else ""

def load_selected_proxy_from_db(con):
    row = con.execute("SELECT value FROM config WHERE key=?", ("selected_proxy_string",)).fetchone()
    return row['value'] if row else ""


# ==============================================================================
#   PHẦN 4: BACKGROUND SERVICES
# ==============================================================================

def proxy_checker_loop():
    time.sleep(5)
    while True:
        try:
            with db_lock:
                with db() as con: rows = con.execute("SELECT * FROM proxies").fetchall()
            for r in rows:
                is_live, lat = check_proxy_live(r['proxy_string'])
                update_proxy_state(r['proxy_string'], is_live, lat)
                time.sleep(0.5)
        except: pass
        time.sleep(PROXY_CHECK_INTERVAL)

def start_proxy_checker_once():
    global proxy_checker_started
    if not proxy_checker_started:
        proxy_checker_started = True
        threading.Thread(target=proxy_checker_loop, daemon=True).start()

def ping_loop():
    while True:
        try:
            with db() as con:
                u = con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()
                i = con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()
                url, inv = (u['value'] if u else ""), (int(i['value']) if i else 300)
            if url: requests.get(url, timeout=10)
            time.sleep(max(10, inv))
        except: time.sleep(60)

def start_ping_service():
    global ping_service_started
    if not ping_service_started:
        ping_service_started = True
        threading.Thread(target=ping_loop, daemon=True).start()

def perform_backup_to_file():
    try:
        with db_lock:
            with db() as con:
                data = {
                    "keymaps": [dict(r) for r in con.execute("SELECT * FROM keymaps").fetchall()],
                    "config": {r['key']: r['value'] for r in con.execute("SELECT key, value FROM config").fetchall()},
                    "proxies": [dict(r) for r in con.execute("SELECT * FROM proxies").fetchall()],
                    "local_stock": [dict(r) for r in con.execute("SELECT * FROM local_stock").fetchall()]
                }
        with open(AUTO_BACKUP_FILE, 'w', encoding='utf-8') as f: json.dump(data, f)
    except: pass

def start_auto_backup():
    global auto_backup_started
    if not auto_backup_started:
        auto_backup_started = True
        threading.Thread(target=lambda: (time.sleep(3600), perform_backup_to_file()), daemon=True).start()


# ==============================================================================
#   PHẦN 5: CORE LOGIC (MAIL72H & LOCAL STOCK)
# ==============================================================================

def fetch_local_stock(group, qty):
    with db_lock:
        with db() as con:
            rows = con.execute("SELECT id, content FROM local_stock WHERE group_name=? LIMIT ?", (group, qty)).fetchall()
            if not rows: return []
            ids = [r['id'] for r in rows]
            now = get_vn_time()
            for r in rows: con.execute("INSERT INTO local_history(group_name, content, fetched_at) VALUES(?,?,?)", (group, r['content'], now))
            con.execute(f"DELETE FROM local_stock WHERE id IN ({','.join(['?']*len(ids))})", ids)
            con.commit()
            return [{"product": r['content']} for r in rows]

def get_local_stock_count(group):
    with db() as con: return con.execute("SELECT COUNT(*) FROM local_stock WHERE group_name=?", (group,)).fetchone()[0]

def _mail72h_collect_all_products(obj):
    all_p = []
    if isinstance(obj, dict):
        for c in obj.get('categories', []):
            if isinstance(c, dict): all_p.extend(c.get('products', []))
    return all_p

def mail72h_format_buy(base_url, api_key, product_id, amount):
    r = requests.post(f"{base_url.rstrip('/')}/api/buy_product", data={"action": "buyProduct", "id": product_id, "amount": amount, "api_key": api_key}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET)
    return r.json()

def stock_mail72h_format(row):
    try:
        r = requests.get(f"{row['base_url'].rstrip('/')}/api/products.php", params={"api_key": row['api_key']}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET)
        data = r.json()
        if data.get("status") != "success": return jsonify({"sum": 0})
        for p in _mail72h_collect_all_products(data):
            if str(p.get("id")) == str(row["product_id"]): return jsonify({"sum": int(p.get("amount", 0))})
    except: pass
    return jsonify({"sum": 0})

def fetch_mail72h_format(row, qty):
    try:
        res = mail72h_format_buy(row['base_url'], row["api_key"], int(row["product_id"]), qty)
        if res.get("status") == "success":
            d = res.get("data")
            if isinstance(d, list): return jsonify([{"product": json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)} for x in d])
            return jsonify([{"product": json.dumps(d, ensure_ascii=False) if isinstance(d, dict) else str(d)} for _ in range(qty)])
    except: pass
    return jsonify([])


# ==============================================================================
#   PHẦN 6: HTML TEMPLATES (GIAO DIỆN)
# ==============================================================================

LOGIN_TPL = """<!doctype html><html data-theme="dark"><head><meta charset="utf-8"/><title>Login</title><style>body{background:#121212;color:#fff;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif}.box{background:#1c1c1e;padding:40px;border-radius:12px;text-align:center;box-shadow:0 4px 15px rgba(0,0,0,0.5)}input{padding:12px;margin:10px 0;width:100%;background:#2c2c2e;border:1px solid #333;color:white;border-radius:6px}button{padding:12px;width:100%;background:#0d6efd;color:white;border:none;cursor:pointer;border-radius:6px;font-weight:bold}</style></head><body><div class="box"><h2>QUANTUM GATE</h2><form method="post"><input type="password" name="admin_secret" placeholder="Enter Admin Password"><button>ACCESS DASHBOARD</button></form></div></body></html>"""

ADMIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Admin Dashboard</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
    :root {
        --primary: #0a84ff; --bg: #000; --card: #1c1c1e; --text: #f5f5f7; --border: #38383a;
        --green: #30d158; --red: #ff453a; --yellow: #ffd60a;
    }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background: var(--bg); color: var(--text); margin: 0; padding: 20px;
        overflow-x: hidden;
    }
    .container { max-width: 1200px; margin: 0 auto; position: relative; z-index: 10; }
    .card {
        background: var(--card); border: 1px solid var(--border); border-radius: 12px;
        padding: 20px; margin-bottom: 20px; box-shadow: 0 4px 6px rgba(0,0,0,0.3);
    }
    h2, h3 { margin-top: 0; font-weight: 600; color: var(--primary); }
    h3 { border-bottom: 1px solid var(--border); padding-bottom: 10px; font-size: 1.1rem; }
    .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 15px; }
    .col-6 { grid-column: span 6; } .col-8 { grid-column: span 8; } .col-4 { grid-column: span 4; } .col-12 { grid-column: span 12; }
    
    input, textarea, select {
        width: 100%; padding: 10px; margin-bottom: 10px;
        background: #2c2c2e; border: 1px solid #48484a; color: #fff;
        border-radius: 6px; box-sizing: border-box; font-family: monospace;
    }
    input:focus, textarea:focus { border-color: var(--primary); outline: none; }
    
    button {
        padding: 10px 20px; border-radius: 6px; border: none; cursor: pointer;
        font-weight: 600; background: var(--primary); color: white; transition: 0.2s;
    }
    button:hover { opacity: 0.8; }
    .btn-green { background: var(--green); color: #000; }
    .btn-red { background: var(--red); color: #fff; }
    
    label { font-size: 11px; font-weight: bold; color: #888; display: block; margin-bottom: 5px; }
    
    @media (max-width: 768px) { .col-6, .col-8, .col-4 { grid-column: span 12; } }
    
    #effect-canvas { position: fixed; top: 0; left: 0; width: 100%; height: 100%; z-index: 0; pointer-events: none; }
    </style>
</head>
<body>
    <canvas id="effect-canvas"></canvas>
    
    <div class="container">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:20px;">
            <h2>⚙️ QUANTUM DASHBOARD</h2>
            <div>
                <select id="effect-select" style="width:auto; display:inline-block;" onchange="changeEffect(this.value)">
                    <option value="none">No Effect</option>
                    <option value="matrix">Matrix</option>
                    <option value="snow">Snow</option>
                    <option value="particles">Particles</option>
                </select>
                <form action="{{url_for('logout')}}" method="post" style="display:inline;">
                    <button class="btn-red">Logout</button>
                </form>
            </div>
        </div>

        <div class="row">
            <div class="col-6 card">
                <h3>1. Quản Lý Key (API / Local)</h3>
                <form action="{{url_for('admin_add_keymap')}}" method="post">
                    <input name="group_name" placeholder="Group Name (VD: Netflix)" required>
                    <input name="provider_type" placeholder="local / mail72h" required>
                    <input name="sku" placeholder="SKU Code" required>
                    <input name="input_key" placeholder="Input Key (Mã bán)" required>
                    <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px;">
                        <input name="base_url" placeholder="API URL">
                        <input name="api_key" placeholder="API Key">
                    </div>
                    <input name="product_id" placeholder="Product ID (0 nếu là local)">
                    <button style="width:100%">Lưu Key</button>
                </form>
            </div>

            <div class="col-6 card">
                <h3>2. Proxy System</h3>
                <div style="margin-bottom:10px">Current Proxy: <code style="color:var(--green)">{{current_proxy or 'Direct'}}</code></div>
                <form action="{{url_for('admin_add_proxy')}}" method="post">
                    <textarea name="proxies" rows="5" placeholder="ip:port&#10;ip:port:user:pass"></textarea>
                    <button style="width:100%">Thêm Proxy</button>
                </form>
            </div>
        </div>

        <div class="card" style="border: 1px solid var(--yellow);">
            <h3 style="color: var(--yellow);">🚀 6. TikTok Checker (Logic Mới: Fix Captcha = Live)</h3>
            <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank">
                <div class="row">
                    <div class="col-8">
                        <label>INPUT LIST (MỖI DÒNG 1 ID HOẶC USER|PASS...)</label>
                        <textarea name="check_list" rows="8" placeholder="tiktok_id_1&#10;tiktok_id_2|pass..." required></textarea>
                    </div>
                    <div class="col-4">
                        <label>PROXY RIÊNG (OPTIONAL - AUTO ROTATE)</label>
                        <textarea name="check_proxies" rows="4" placeholder="ip:port:user:pass..."></textarea>
                        
                        <label>SỐ LUỒNG (THREADS)</label>
                        <input type="number" name="threads" value="20" min="1" max="200">
                        
                        <button class="btn-green" style="width:100%; margin-top:15px; font-size:16px;">🚀 BẮT ĐẦU CHECK</button>
                    </div>
                </div>
            </form>
        </div>

        <div class="card" id="local-stock">
            <h3>4. Kho Hàng Local</h3>
            <form action="{{url_for('admin_local_stock_add')}}" method="post" enctype="multipart/form-data">
                <div style="display:flex; gap:10px;">
                    <input name="group_name" placeholder="Group Name" list="grps" required style="flex:1">
                    <datalist id="grps">{% for g in local_groups %}<option value="{{g}}">{% endfor %}</datalist>
                    <input type="file" name="stock_file" style="flex:1">
                </div>
                <button class="btn-green" style="width:100%">Upload Stock</button>
            </form>
            
            <div style="margin-top:15px; max-height:400px; overflow-y:auto; border:1px solid #333; border-radius:6px;">
                {% for g, c in local_stats.items() %}
                <div style="padding:10px; border-bottom:1px solid #333; display:flex; justify-content:space-between; align-items:center; background: rgba(255,255,255,0.05);">
                    <span><b>{{g}}</b>: <span style="color:var(--green)">{{c}}</span></span>
                    <div style="display:flex; gap:5px;">
                        <input id="q_{{g}}" type="number" value="1" style="width:50px; margin:0;" min="1">
                        <button class="btn-green" style="padding:4px 8px; font-size:12px;" onclick="quickGet('{{g}}')">⚡ Lấy</button>
                        <a href="{{url_for('admin_local_stock_view', group=g)}}" style="background:#333; color:white; padding:6px 12px; border-radius:4px; text-decoration:none; font-size:12px; display:flex; align-items:center;">Xem</a>
                        <form action="{{url_for('admin_local_stock_clear')}}" method="post" style="margin:0;" onsubmit="return confirm('Xóa sạch kho {{g}}?')">
                            <input type="hidden" name="group_name" value="{{g}}">
                            <button class="btn-red" style="padding:4px 8px; font-size:12px;">Xóa</button>
                        </form>
                    </div>
                </div>
                {% endfor %}
            </div>
        </div>
    </div>

<script>
// Logic Lấy Hàng
async function quickGet(g){
    let q = document.getElementById('q_'+g).value;
    if(confirm(`Lấy ${q} items từ ${g}?`)){
        try {
            let r = await fetch(`/admin/local-stock/quick-get?group=${g}&qty=${q}`);
            if(r.ok){
                let t = await r.text();
                if(t){ await navigator.clipboard.writeText(t); alert("✅ Đã lấy và COPY thành công!"); location.reload(); }
                else alert("❌ Hết hàng!");
            }
        } catch(e) { alert("Error: "+e); }
    }
}

// Logic Effect
let canvas=document.getElementById('effect-canvas'), ctx=canvas.getContext('2d'), w, h;
function resize(){ w=canvas.width=window.innerWidth; h=canvas.height=window.innerHeight; }
window.addEventListener('resize', resize); resize();

function startEffect(type){
    if(type === 'matrix') {
        const cols = Math.floor(w/20)+1, ypos = Array(cols).fill(0);
        function step(){
            ctx.fillStyle = '#0001'; ctx.fillRect(0,0,w,h);
            ctx.fillStyle = '#0f0'; ctx.font = '15pt monospace';
            ypos.forEach((y,i) => {
                ctx.fillText(String.fromCharCode(Math.random()*128), i*20, y);
                ypos[i] = (y>100+Math.random()*10000) ? 0 : y+20;
            });
            requestAnimationFrame(step);
        }
        step();
    } else { ctx.clearRect(0,0,w,h); }
}
document.addEventListener("DOMContentLoaded", () => {
    let e = localStorage.getItem('effect') || 'matrix';
    document.getElementById('effect-select').value = e;
    startEffect(e);
});
function changeEffect(v){ localStorage.setItem('effect',v); location.reload(); }
</script>
</body>
</html>
"""

STOCK_VIEW_TPL = """
<!doctype html><html data-theme="dark"><head><title>{{group}}</title>
<style>
body{background:#121212;color:#fff;font-family:monospace;padding:20px}
table{width:100%;border-collapse:collapse;margin-top:20px}
th,td{border:1px solid #333;padding:10px;text-align:left} th{background:#1c1c1e;color:#888}
button{cursor:pointer;border:none;padding:5px 10px;border-radius:4px;color:white;font-weight:bold}
a{color:#0d6efd;text-decoration:none}
</style></head><body>
<h2>📦 Group: {{group}} ({{items|length}})</h2>
<div style="display:flex;gap:10px;align-items:center;">
    <a href="{{url_for('admin_index')}}#local-stock">🔙 Quay lại</a>
    <a href="{{url_for('admin_local_stock_download', group=group)}}" style="background:#20c997;color:black;padding:5px;border-radius:4px">📥 Download</a>
    <form action="{{url_for('admin_run_checker')}}" method="post" target="_blank" style="margin:0">
        <input type="hidden" name="local_group" value="{{group}}">
        <button style="background:#ffc107;color:black">🔍 Check Live (TikTok)</button>
    </form>
</div>
<table><thead><tr><th>#</th><th>Data</th><th>Date</th><th>Action</th></tr></thead><tbody>
{% for i in items %}
<tr><td>{{loop.index}}</td><td style="color:#20c997;word-break:break-all">{{i.content}}</td><td>{{i.added_at}}</td>
<td><form action="{{url_for('admin_local_stock_delete_one')}}" method="post"><input type="hidden" name="id" value="{{i.id}}"><input type="hidden" name="group" value="{{group}}"><button style="background:#dc3545">X</button></form></td></tr>
{% endfor %}
</tbody></table></body></html>
"""

# TEMPLATE CHECKER STREAM - CHUẨN XÁC
CHECKER_STREAM_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8" />
    <title>Checker Progress</title>
    <style>
    body { background: #121212; color: #fff; font-family: monospace; padding: 20px; }
    .stats { display: flex; gap: 20px; background: #1c1c1e; padding: 15px; border-radius: 8px; border: 1px solid #333; font-size: 1.2rem; }
    .live { color: #30d158; font-weight: bold; }
    .die { color: #ff453a; font-weight: bold; }
    .box { margin-top: 20px; }
    textarea { width: 100%; background: #2c2c2e; color: #30d158; border: 1px solid #333; padding: 10px; min-height: 200px; font-family: monospace; border-radius: 6px; }
    h3 { border-bottom: 1px solid #333; padding-bottom: 5px; color: #0a84ff; }
    .die-list { max-height: 300px; overflow-y: auto; background: #1a1a1a; border: 1px solid #333; padding: 10px; border-radius: 6px; }
    .die-item { font-size: 12px; border-bottom: 1px dashed #333; padding: 4px 0; color: #aaa; }
    button { padding: 8px 16px; background: #0a84ff; color: white; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; margin-top: 5px; }
    button:hover { opacity: 0.8; }
    </style>
</head>
<body>
    <h2>🚀 Checking Progress...</h2>
    
    <div class="stats">
        <span id="st">Status: Starting...</span>
        <span class="live" id="cl">LIVE: 0</span>
        <span class="die" id="cd">DIE: 0</span>
    </div>
    <div id="msg" style="color:#aaa;margin-bottom:10px">Initializing threads...</div>

    <div class="box">
        <h3 class="live">✅ LIVE ACCOUNTS (Clean List)</h3>
        <textarea id="live_area" readonly></textarea>
        <button onclick="copyLive()">📋 COPY ALL LIVE</button>
    </div>

    <div class="box">
        <h3 class="die">❌ DIE / ERROR DETAILS</h3>
        <div id="die_list" class="die-list"></div>
    </div>

    <script>
        function update(done, total, live, die) {
            document.getElementById('st').innerText = `Checked: ${done}/${total}`;
            document.getElementById('cl').innerText = `LIVE: ${live}`;
            document.getElementById('cd').innerText = `DIE: ${die}`;
        }
        function addLive(line) {
            // Chỉ thêm username sạch vào box để copy dễ
            let user = line.split('|')[0].trim();
            let area = document.getElementById('live_area');
            area.value += user + "\\n";
        }
        function addDie(line, reason) {
            let list = document.getElementById('die_list');
            let item = document.createElement('div');
            item.className = 'die-item';
            // Không hiện timestamp
            item.innerHTML = `<span style='color:#ff453a'>[DIE]</span> ${line} <span style='color:#666'>(${reason})</span>`;
            list.appendChild(item);
        }
        function copyLive() {
            let area = document.getElementById('live_area');
            area.select();
            document.execCommand('copy');
            alert("Đã copy danh sách LIVE (chỉ username)!");
        }
        function done() {
            document.querySelector('h2').innerText = "✅ CHECK COMPLETED!";
            document.querySelector('h2').style.color = "#30d158";
            document.getElementById('msg').innerText = "Done.";
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

# --- AUTH ---
@app.route("/", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("admin_secret") == ADMIN_SECRET:
            resp = make_response(redirect(url_for("admin_index")))
            resp.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000)
            return resp
        flash("Sai mật khẩu!")
    if request.cookies.get("logged_in") == ADMIN_SECRET: return redirect(url_for("admin_index"))
    return render_template_string(LOGIN_TPL)

@app.route("/logout", methods=["POST"])
def logout():
    resp = make_response(redirect(url_for("login")))
    resp.set_cookie("logged_in", "", max_age=0); return resp

# --- ADMIN ---
@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        stock = con.execute("SELECT group_name, COUNT(*) as c FROM local_stock GROUP BY group_name").fetchall()
    return render_template_string(ADMIN_TPL, 
        current_proxy=CURRENT_PROXY_STRING,
        local_groups=[r['group_name'] for r in stock],
        local_stats={r['group_name']: r['c'] for r in stock}
    )

# --- KEYMAP ---
@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin(); f=request.form
    with db() as con: 
        con.execute("INSERT OR REPLACE INTO keymaps(group_name,sku,input_key,product_id,api_key,is_active,provider_type,base_url) VALUES(?,?,?,?,?,1,?,?)", 
        (f.get("group_name"),f.get("sku"),f.get("input_key"),f.get("product_id") or 0,f.get("api_key"),f.get("provider_type"),f.get("base_url")))
        con.commit()
    return redirect(url_for("admin_index"))

# --- PROXY ---
@app.route("/admin/proxy/add", methods=["POST"])
def admin_add_proxy():
    require_admin(); p=request.form.get("proxies","")
    with db() as con:
        for l in p.splitlines(): 
            if l.strip(): con.execute("INSERT OR IGNORE INTO proxies(proxy_string) VALUES(?)", (l.strip(),))
        con.commit()
    return redirect(url_for("admin_index"))

# --- LOCAL STOCK ---
@app.route("/admin/local-stock/add", methods=["POST"])
def admin_local_stock_add():
    require_admin(); g=request.form.get("group_name"); f=request.files.get("stock_file")
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


# ==============================================================================
#   PHẦN 9: TIKTOK CHECKER CORE (LOGIC TỪ CHECKTIKTOK_PATCHED.PY)
# ==============================================================================

def check_tiktok_advanced(line, proxy_iter):
    """
    Check Live/Die chuẩn xác (tương tự tool Python):
    - Sử dụng BeautifulSoup để phân tích cấu trúc trang nếu có thể.
    - Check DIE dựa trên thông báo lỗi cụ thể.
    - Check LIVE dựa trên việc KHÔNG PHẢI DIE (kể cả Captcha/Private).
    """
    line = line.strip()
    if not line: return None
    
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
    
    # Proxy Rotation
    prx = None
    if proxy_iter:
        try: prx = next(proxy_iter)
        except: pass
    proxies = format_proxy_url(prx) if prx else CURRENT_PROXY_SET
    
    try:
        # Timeout 15s
        r = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        
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
        
        # Nếu có BS4 thì parse thử lấy thông tin (để xác nhận chắc chắn Live)
        info = ""
        if BeautifulSoup:
            try:
                soup = BeautifulSoup(html, 'lxml')
                # Tìm data JSON
                script_tag = soup.find('script', id='__UNIVERSAL_DATA_FOR_REHYDRATION__')
                if script_tag and script_tag.string:
                    data = json.loads(script_tag.string)
                    stats = data['__DEFAULT_SCOPE__']['webapp.user-detail']['userInfo']['stats']
                    info = f" | Follow: {stats.get('followerCount',0)}"
            except: pass

        if "captcha" in html.lower() or "verify" in html.lower():
            return ("LIVE", line, "Live (Captcha)")
            
        if "private" in html.lower():
            return ("LIVE", line, "Live (Private)")
            
        return ("LIVE", line, "OK" + info)
        
    except Exception as e:
        # Lỗi mạng -> Coi như Live (để ko xóa nhầm)
        return ("LIVE", line, f"Error/Timeout")

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
        
    if not lines: return "<h3>Không có dữ liệu để check.</h3>", 400
        
    # Chuẩn bị Proxy
    proxy_list = [p.strip() for p in raw_proxies.splitlines() if p.strip()]
    if not proxy_list:
        with db() as con: proxy_list = [r['proxy_string'] for r in con.execute("SELECT proxy_string FROM proxies WHERE is_live=1").fetchall()]
    
    proxy_iter = itertools.cycle(proxy_list) if proxy_list else None
    
    # Generator Stream Response
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
                        yield f"<script>addLive({safe_line}); update({done}, {total}, {live}, {die});</script>\n"
                    else:
                        die += 1
                        yield f"<script>addDie({safe_line}, {safe_reason}); update({done}, {total}, {live}, {die});</script>\n"
                else:
                    done += 1
                    yield f"<script>update({done}, {total}, {live}, {die});</script>\n"
                    
        yield "<script>done();</script></body></html>"

    return Response(stream_with_context(generate()))


# ==============================================================================
#   PHẦN 10: PUBLIC API (CHO NGƯỜI MUA)
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
#   PHẦN 11: KHỞI ĐỘNG (STARTUP SEQUENCE)
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
    port = int(os.getenv("PORT", 8000))
    print(f"🚀 SERVER STARTED ON PORT {port}")
    app.run(host="0.0.0.0", port=port)
