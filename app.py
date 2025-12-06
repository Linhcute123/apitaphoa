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
#   PHẦN 2: DATABASE UTILS
# ==============================================================================
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row 
    return con

def init_db():
    with db_lock:
        with db() as con:
            print(f"INFO: Init DB at {DB}")
            con.execute("CREATE TABLE IF NOT EXISTS keymaps(id INTEGER PRIMARY KEY, sku TEXT, input_key TEXT UNIQUE, product_id INTEGER, is_active INTEGER DEFAULT 1, group_name TEXT, provider_type TEXT DEFAULT 'mail72h', base_url TEXT, api_key TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS proxies(id INTEGER PRIMARY KEY, proxy_string TEXT UNIQUE, is_live INTEGER DEFAULT 0, latency REAL DEFAULT 9999.0, last_checked TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_stock(id INTEGER PRIMARY KEY, group_name TEXT, content TEXT, added_at TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS local_history(id INTEGER PRIMARY KEY, group_name TEXT, content TEXT, fetched_at TEXT)")
            
            # Migration columns if missing
            try: con.execute("ALTER TABLE keymaps ADD COLUMN group_name TEXT")
            except: pass
            try: con.execute("ALTER TABLE keymaps ADD COLUMN provider_type TEXT DEFAULT 'mail72h'")
            except: pass
            try: con.execute("ALTER TABLE keymaps ADD COLUMN base_url TEXT")
            except: pass
            try: con.execute("ALTER TABLE keymaps ADD COLUMN api_key TEXT")
            except: pass
            
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('selected_proxy_string', '')")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('ping_url', '')")
            con.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('ping_interval', '300')")
            con.commit()

            # Auto Restore Logic
            if con.execute("SELECT COUNT(*) FROM keymaps").fetchone()[0] == 0:
                if SECRET_BACKUP_FILE_PATH and os.path.exists(SECRET_BACKUP_FILE_PATH):
                    try:
                        with open(SECRET_BACKUP_FILE_PATH, 'r', encoding='utf-8') as f: data = json.load(f)
                        kms = data.get('keymaps', []) if isinstance(data, dict) else data
                        for k in kms: con.execute("INSERT OR IGNORE INTO keymaps(sku,input_key,product_id,is_active,group_name,provider_type,base_url,api_key) VALUES(?,?,?,?,?,?,?,?)", (k.get('sku'), k.get('input_key'), k.get('product_id'), k.get('is_active',1), k.get('group_name'), k.get('provider_type','mail72h'), k.get('base_url'), k.get('api_key')))
                        if isinstance(data, dict):
                            for p in data.get('proxies', []): con.execute("INSERT OR IGNORE INTO proxies(proxy_string,is_live,latency,last_checked) VALUES(?,?,?,?)", (p.get('proxy_string'),0,9999.0,get_vn_time()))
                            for l in data.get('local_stock', []): con.execute("INSERT INTO local_stock(group_name,content,added_at) VALUES(?,?,?)", (l.get('group_name'),l.get('content'),l.get('added_at')))
                        con.commit()
                        print("SUCCESS: Auto Restore Done.")
                    except Exception as e: print(f"RESTORE ERROR: {e}")

# ==============================================================================
#   PHẦN 3: PROXY & THREADS
# ==============================================================================
def check_proxy_live(proxy_string):
    if not proxy_string: return (0, 9999.0)
    parts = proxy_string.split(':')
    fmt = f"http://{parts[0]}:{parts[1]}" if len(parts)==2 else (f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}" if len(parts)==4 else None)
    if not fmt: return (0, 9999.0)
    try:
        t = time.time()
        requests.get("http://www.google.com/generate_204", proxies={"http":fmt,"https":fmt}, timeout=DEFAULT_TIMEOUT*2)
        return (1, time.time()-t)
    except: return (0, 9999.0)

def set_current_proxy(p_str):
    global CURRENT_PROXY_SET, CURRENT_PROXY_STRING
    if not p_str: 
        CURRENT_PROXY_SET = {"http":None,"https":None}; CURRENT_PROXY_STRING = ""
        return
    parts = p_str.split(':')
    fmt = f"http://{parts[0]}:{parts[1]}" if len(parts)==2 else (f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}" if len(parts)==4 else None)
    if fmt: CURRENT_PROXY_SET={"http":fmt,"https":fmt}; CURRENT_PROXY_STRING=p_str
    else: CURRENT_PROXY_SET={"http":None,"https":None}; CURRENT_PROXY_STRING=""

def proxy_loop():
    while True:
        try:
            with db() as con: pxs = con.execute("SELECT * FROM proxies").fetchall()
            alive = False
            for r in pxs:
                s = r['proxy_string']
                l, lat = check_proxy_live(s)
                with db_lock:
                    with db() as con: con.execute("UPDATE proxies SET is_live=?, latency=?, last_checked=? WHERE id=?", (l,lat,get_vn_time(),r['id'])); con.commit()
                if l and s == CURRENT_PROXY_STRING: alive = True
            if CURRENT_PROXY_STRING and not alive:
                with db_lock:
                    with db() as con:
                        best = con.execute("SELECT proxy_string FROM proxies WHERE is_live=1 ORDER BY latency ASC LIMIT 1").fetchone()
                        new_p = best['proxy_string'] if best else ""
                        set_current_proxy(new_p)
                        con.execute("INSERT OR REPLACE INTO config(key,value) VALUES('selected_proxy_string',?)", (new_p,)); con.commit()
        except: pass
        time.sleep(PROXY_CHECK_INTERVAL)

def ping_loop():
    while True:
        try:
            with db() as con: 
                u = con.execute("SELECT value FROM config WHERE key='ping_url'").fetchone()
                i = con.execute("SELECT value FROM config WHERE key='ping_interval'").fetchone()
                url = u['value'] if u else ""; sec = int(i['value']) if i else 300
            if url.startswith("http"): requests.get(url, timeout=10)
            time.sleep(max(10, sec))
        except: time.sleep(60)

def backup_loop():
    while True:
        time.sleep(3600)
        try:
            with db_lock:
                with db() as con:
                    data = {
                        "keymaps": [dict(r) for r in con.execute("SELECT * FROM keymaps").fetchall()],
                        "proxies": [dict(r) for r in con.execute("SELECT * FROM proxies").fetchall()],
                        "local_stock": [dict(r) for r in con.execute("SELECT * FROM local_stock").fetchall()],
                        "config": {r['key']:r['value'] for r in con.execute("SELECT * FROM config").fetchall()},
                        "generated_at": get_vn_time()
                    }
            with open(AUTO_BACKUP_FILE, 'w', encoding='utf-8') as f: json.dump(data, f, ensure_ascii=False)
        except: pass

# ==============================================================================
#   PHẦN 4: LOGIC KHO & API
# ==============================================================================
def get_stock_count(row):
    if row['provider_type'] == 'local':
        with db() as con: return con.execute("SELECT COUNT(*) FROM local_stock WHERE group_name=?", (row['group_name'],)).fetchone()[0]
    # Mail72h Logic
    for _ in range(2):
        try:
            r = requests.get(f"{row['base_url'].rstrip('/')}/api/products.php", params={"api_key":row['api_key']}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET).json()
            if r.get("status")!="success": return 0
            # Flatten categories
            items = []
            for c in r.get('categories',[]): items.extend(c.get('products',[]))
            pid = str(row['product_id'])
            for i in items:
                if str(int(float(str(i.get('id',0))))) == pid: return int(i.get('amount',0))
            return 0
        except: 
            # Failover logic handled by proxy loop implicitly, but simple retry here
            continue
    return 0

def fetch_item(row, qty):
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
    # Mail72h
    for _ in range(2):
        try:
            url = f"{row['base_url'].rstrip('/')}/api/buy_product"
            res = requests.post(url, data={"action":"buyProduct","id":row['product_id'],"amount":qty,"api_key":row['api_key']}, timeout=DEFAULT_TIMEOUT, proxies=CURRENT_PROXY_SET).json()
            if res.get("status")!="success": return []
            d = res.get("data")
            if isinstance(d, list): return [{"product": json.dumps(x, ensure_ascii=False) if isinstance(x, dict) else str(x)} for x in d]
            return [{"product": json.dumps(d, ensure_ascii=False) if isinstance(d, dict) else str(d)} for _ in range(qty)]
        except: continue
    return []

# ==============================================================================
#   PHẦN 5: GIAO DIỆN ADMIN
# ==============================================================================
ADMIN_TPL = """
<!doctype html>
<html data-theme="dark">
<head>
    <meta charset="utf-8">
    <title>Admin Dashboard</title>
    <style>
        :root { --primary: #5a7dff; --bg: #121212; --card: #1c1c1e; --text: #e9ecef; --border: #343a40; }
        body { font-family: monospace; background: var(--bg); color: var(--text); padding: 20px; }
        .card { background: var(--card); border: 1px solid var(--border); padding: 20px; border-radius: 8px; margin-bottom: 20px; }
        input, select, textarea { width: 100%; padding: 10px; background: #2c2c2e; border: 1px solid var(--border); color: #fff; box-sizing: border-box; margin-bottom: 10px; }
        .row { display: grid; grid-template-columns: repeat(12, 1fr); gap: 10px; }
        .col-4 { grid-column: span 4; } .col-6 { grid-column: span 6; } .col-12 { grid-column: span 12; }
        button { padding: 10px; background: var(--primary); color: white; border: none; cursor: pointer; border-radius: 4px; }
        button.red { background: #dc3545; } button.green { background: #198754; } button.yellow { background: #ffc107; color: black; }
        table { width: 100%; border-collapse: collapse; margin-top: 10px; }
        th, td { border: 1px solid var(--border); padding: 8px; text-align: left; }
        
        /* Tabs Styles */
        .tab-btn { background: #343a40; opacity: 0.6; }
        .tab-btn.active { background: var(--primary); opacity: 1; font-weight: bold; }
        .hidden { display: none; }
    </style>
</head>
<body>
    {% with messages = get_flashed_messages(with_categories=true) %}
        {% if messages %}{% for c, m in messages %}<div style="padding:10px; margin-bottom:10px; background:#333; border-left:4px solid var(--primary);">{{ m }}</div>{% endfor %}{% endif %}
    {% endwith %}

    <h2>🛠️ Admin Dashboard (Proxy: <span style="color:#20c997">{{ current_proxy }}</span>)</h2>

    <div class="card">
        <div style="display:flex; justify-content:space-between; margin-bottom:15px;">
            <h3>1. Thêm Key Bán Hàng</h3>
            <div>
                <button type="button" class="tab-btn active" onclick="setMode('single', this)">Thêm Lẻ</button>
                <button type="button" class="tab-btn" onclick="setMode('bulk', this)">Thêm Hàng Loạt (Local)</button>
            </div>
        </div>

        <form method="post" action="{{ url_for('admin_add_keymap') }}" id="form-single">
            <div class="row">
                <div class="col-4"><input name="group_name" placeholder="Group Name (Netflix...)" required></div>
                <div class="col-4">
                    <select name="provider_type" onchange="toggleFields(this.value)" id="type-sel">
                        <option value="mail72h">API Mail72h</option>
                        <option value="local">Local Stock (Kho riêng)</option>
                    </select>
                </div>
                <div class="col-4 api-field"><input name="base_url" placeholder="API Base URL"></div>
            </div>
            <div class="row">
                <div class="col-4"><input name="sku" placeholder="SKU (Mã sp)" required></div>
                <div class="col-4"><input name="input_key" placeholder="Input Key (Mã bán)" required></div>
                <div class="col-4 api-field"><input name="product_id" placeholder="Product ID"></div>
            </div>
            <div class="api-field"><input name="api_key" placeholder="API Key" type="password"></div>
            <button type="submit" class="green" style="width:100%">Lưu Key</button>
        </form>

        <form method="post" action="{{ url_for('admin_add_keymap_bulk') }}" id="form-bulk" class="hidden">
            <div class="row">
                <div class="col-6">
                    <label>Chọn Kho Hàng (Group Name)</label>
                    <input name="group_name" list="group_hints" required placeholder="Nhập tên nhóm...">
                    <datalist id="group_hints">{% for g in groups %}<option value="{{ g }}">{% endfor %}</datalist>
                </div>
                <div class="col-6">
                    <label>SKU Prefix (Tùy chọn)</label>
                    <input name="sku_prefix" placeholder="VD: NF_ (=> NF_Key123)">
                </div>
            </div>
            <label>Danh sách Input Key (Mỗi dòng 1 key)</label>
            <textarea name="bulk_keys" rows="8" placeholder="KEY_ABC_1&#10;KEY_ABC_2&#10;..." required></textarea>
            <button type="submit" class="yellow" style="width:100%">🚀 Thêm Hàng Loạt Key (Local)</button>
            <small style="display:block; margin-top:5px; color:#aaa;">* Các key này sẽ tự động trỏ về kho Local bạn chọn ở trên.</small>
        </form>
    </div>

    <div class="card">
        <h3>2. Danh Sách Key Đang Hoạt Động</h3>
        {% for grp, providers in keymaps.items() %}
            <details open style="margin-bottom:10px; border:1px solid #333; border-radius:5px;">
                <summary style="padding:10px; background:#222; cursor:pointer;">📂 {{ grp }}</summary>
                <div style="padding:10px;">
                {% for prov, keys in providers.items() %}
                    <div style="margin-left:15px; margin-bottom:10px;">
                        <h4 style="margin:5px 0; color:#aaa;">{{ prov }}</h4>
                        <table>
                            <tr><th>SKU</th><th>Key</th><th>Action</th></tr>
                            {% for k in keys %}
                            <tr>
                                <td>{{ k.sku }}</td>
                                <td style="color:#20c997">{{ k.input_key }}</td>
                                <td>
                                    <form action="{{ url_for('admin_delete_key', id=k.id) }}" method="post" style="display:inline" onsubmit="return confirm('Xóa?')">
                                        <button class="red" style="padding:4px 8px;">X</button>
                                    </form>
                                </td>
                            </tr>
                            {% endfor %}
                        </table>
                    </div>
                {% endfor %}
                </div>
            </details>
        {% endfor %}
    </div>

    <div class="card" id="local">
        <h3>3. Kho Hàng (Local Stock)</h3>
        <form method="post" action="{{ url_for('admin_local_add') }}" enctype="multipart/form-data">
            <div class="row">
                <div class="col-6"><input name="group_name" list="group_hints" placeholder="Tên kho..." required></div>
                <div class="col-6"><input type="file" name="file"></div>
            </div>
            <textarea name="content" rows="3" placeholder="Paste hàng vào đây..."></textarea>
            <button class="green" style="width:100%">Thêm Hàng Vào Kho</button>
        </form>
        <div style="margin-top:15px; max-height:300px; overflow-y:auto;">
            {% for g, c in stock_stats.items() %}
                <div style="padding:8px; border-bottom:1px dashed #333; display:flex; justify-content:space-between;">
                    <span>📦 <b>{{ g }}</b>: {{ c }} items</span>
                    <div>
                        <a href="{{ url_for('admin_local_view', group=g) }}" style="color:#5a7dff; margin-right:10px;">Xem/Tải</a>
                        <form action="{{ url_for('admin_local_clear') }}" method="post" style="display:inline" onsubmit="return confirm('Xóa sạch kho {{g}}?')"><button class="red" style="padding:2px 5px;">X</button></form>
                    </div>
                </div>
            {% endfor %}
        </div>
    </div>
    
    <div class="card">
        <h3>4. Proxy & Backup</h3>
        <form method="post" action="{{ url_for('admin_proxy_add') }}"><textarea name="proxies" rows="3" placeholder="ip:port"></textarea><button class="green">Thêm Proxy</button></form>
        <hr style="border-color:#333; margin:15px 0;">
        <a href="{{ url_for('admin_backup_dl') }}"><button>⬇️ Tải Backup</button></a>
        <form action="{{ url_for('admin_backup_up') }}" method="post" enctype="multipart/form-data" style="display:inline"><input type="file" name="f" onchange="this.form.submit()" style="display:none;" id="bf"><label for="bf" style="padding:10px; background:#dc3545; color:white; border-radius:4px; cursor:pointer;">⬆️ Restore</label></form>
    </div>

    <script>
        function setMode(mode, btn) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            if(mode==='single'){
                document.getElementById('form-single').classList.remove('hidden');
                document.getElementById('form-bulk').classList.add('hidden');
            } else {
                document.getElementById('form-single').classList.add('hidden');
                document.getElementById('form-bulk').classList.remove('hidden');
            }
        }
        function toggleFields(val) {
            document.querySelectorAll('.api-field').forEach(e => e.style.display = val==='local'?'none':'block');
        }
        toggleFields(document.getElementById('type-sel').value);
    </script>
</body>
</html>
"""

STOCK_VIEW_TPL = """
<!doctype html>
<html data-theme="dark">
<head><title>Kho {{ group }}</title><style>body{background:#121212;color:#fff;font-family:monospace;padding:20px;}table{width:100%;border-collapse:collapse;}td,th{border:1px solid #333;padding:8px;}a{color:#5a7dff;}</style></head>
<body>
    <h2>Kho: {{ group }} ({{ items|length }})</h2>
    <div style="margin-bottom:15px;">
        <a href="{{ url_for('admin_local_dl', group=group) }}" style="background:#20c997; color:#000; padding:5px 10px; text-decoration:none;">📥 Tải TXT</a>
        <a href="{{ url_for('admin_index') }}#local" style="margin-left:10px;">🔙 Quay lại</a>
    </div>
    <table>
        <tr><th>ID</th><th>Content</th><th>Date</th><th>Action</th></tr>
        {% for i in items %}
        <tr>
            <td>{{ i.id }}</td><td style="color:#ffc107">{{ i.content }}</td><td>{{ i.added_at }}</td>
            <td><form action="{{ url_for('admin_local_del_one') }}" method="post"><input type="hidden" name="id" value="{{ i.id }}"><input type="hidden" name="group" value="{{ group }}"><button style="background:red;color:white;border:none;cursor:pointer;">X</button></form></td>
        </tr>
        {% endfor %}
    </table>
</body>
</html>
"""

# ==============================================================================
#   PHẦN 6: FLASK ROUTES
# ==============================================================================
@app.route("/", methods=["GET","POST"])
def login():
    if request.method=="POST":
        if request.form.get("admin_secret")==ADMIN_SECRET:
            r = make_response(redirect(url_for('admin_index')))
            r.set_cookie("logged_in", ADMIN_SECRET, max_age=31536000)
            return r
    return render_template_string(LOGIN_TPL)

@app.route("/admin")
def admin_index():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return redirect(url_for('login'))
    with db() as con:
        kms = con.execute("SELECT * FROM keymaps ORDER BY group_name").fetchall()
        k_map = {}
        for k in kms:
            g = k['group_name'] or 'Unknown'; p = k['provider_type']
            if g not in k_map: k_map[g]={}
            if p not in k_map[g]: k_map[g][p]=[]
            k_map[g][p].append(k)
        stats = {r['group_name']:r['c'] for r in con.execute("SELECT group_name, COUNT(*) as c FROM local_stock GROUP BY group_name").fetchall()}
        grps = [r['group_name'] for r in con.execute("SELECT DISTINCT group_name FROM local_stock").fetchall()]
    return render_template_string(ADMIN_TPL, keymaps=k_map, stock_stats=stats, groups=grps, current_proxy=CURRENT_PROXY_STRING)

# --- KEY MAP ROUTES ---
@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    f=request.form
    try:
        pt = f.get("provider_type")
        pid = 0 if pt=='local' else f.get("product_id")
        with db() as con:
            con.execute("INSERT INTO keymaps(group_name,sku,input_key,product_id,is_active,provider_type,base_url,api_key) VALUES(?,?,?,?,1,?,?,?) ON CONFLICT(input_key) DO UPDATE SET group_name=excluded.group_name, provider_type=excluded.provider_type", 
                       (f.get("group_name"),f.get("sku"),f.get("input_key"),pid,pt,f.get("base_url"),f.get("api_key")))
            con.commit()
    except Exception as e: flash(f"Err: {e}", "error")
    return redirect(url_for('admin_index'))

@app.route("/admin/keymap/bulk", methods=["POST"])
def admin_add_keymap_bulk():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    grp = request.form.get("group_name", "").strip()
    raw = request.form.get("bulk_keys", "").strip()
    prefix = request.form.get("sku_prefix", "").strip()
    
    if not grp or not raw:
        flash("Thiếu tên Group hoặc danh sách Key", "error")
        return redirect(url_for('admin_index'))
    
    cnt = 0
    with db() as con:
        for line in raw.split('\n'):
            k = line.strip()
            if k:
                sku = f"{prefix}{k}" if prefix else k
                try:
                    con.execute("INSERT INTO keymaps(group_name,sku,input_key,product_id,is_active,provider_type,base_url,api_key) VALUES(?,?,?,0,1,'local','','') ON CONFLICT(input_key) DO NOTHING", (grp, sku, k))
                    cnt+=1
                except: pass
        con.commit()
    flash(f"Đã thêm {cnt} key vào kho '{grp}'", "success")
    return redirect(url_for('admin_index'))

@app.route("/admin/keymap/del/<int:id>", methods=["POST"])
def admin_delete_key(id):
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    with db() as con: con.execute("DELETE FROM keymaps WHERE id=?",(id,)); con.commit()
    return redirect(url_for('admin_index'))

# --- LOCAL STOCK ROUTES ---
@app.route("/admin/local/add", methods=["POST"])
def admin_local_add():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    grp = request.form.get("group_name"); f = request.files.get("file"); c = request.form.get("content")
    lines = []
    if f: lines = f.read().decode('utf-8', errors='ignore').splitlines()
    elif c: lines = c.split('\n')
    with db() as con:
        n = get_vn_time()
        for l in lines: 
            if l.strip(): con.execute("INSERT INTO local_stock(group_name,content,added_at) VALUES(?,?,?)", (grp, l.strip(), n))
        con.commit()
    return redirect(url_for('admin_index')+"#local")

@app.route("/admin/local/view")
def admin_local_view():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return redirect(url_for('login'))
    g = request.args.get("group")
    with db() as con: items = con.execute("SELECT * FROM local_stock WHERE group_name=?",(g,)).fetchall()
    return render_template_string(STOCK_VIEW_TPL, group=g, items=items)

@app.route("/admin/local/dl")
def admin_local_dl():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    g = request.args.get("group")
    with db() as con: rows = con.execute("SELECT content FROM local_stock WHERE group_name=?", (g,)).fetchall()
    resp = make_response("\n".join([r['content'] for r in rows]))
    resp.headers["Content-Disposition"] = f"attachment; filename={g}.txt"
    return resp

@app.route("/admin/local/del_one", methods=["POST"])
def admin_local_del_one():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    with db() as con: con.execute("DELETE FROM local_stock WHERE id=?",(request.form.get("id"),)); con.commit()
    return redirect(url_for('admin_local_view', group=request.form.get("group")))

@app.route("/admin/local/clear", methods=["POST"])
def admin_local_clear():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    g = request.args.get("group_name")
    with db() as con: con.execute("DELETE FROM local_stock WHERE group_name=?",(g,)); con.commit()
    return redirect(url_for('admin_index')+"#local")

# --- PROXY & BACKUP ROUTES ---
@app.route("/admin/proxy/add", methods=["POST"])
def admin_proxy_add():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    p = request.form.get("proxies","").split('\n')
    with db() as con: 
        for x in p: 
            if x.strip(): con.execute("INSERT OR IGNORE INTO proxies(proxy_string) VALUES(?)",(x.strip(),))
        con.commit()
    return redirect(url_for('admin_index'))

@app.route("/admin/backup/dl")
def admin_backup_dl():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    if os.path.exists(AUTO_BACKUP_FILE): return jsonify(json.load(open(AUTO_BACKUP_FILE)))
    return "No backup", 404

@app.route("/admin/backup/up", methods=["POST"])
def admin_backup_up():
    if request.cookies.get("logged_in")!=ADMIN_SECRET: return "Auth fail", 403
    f = request.files.get("f")
    if f:
        d = json.load(f)
        with db() as con:
            con.execute("DELETE FROM keymaps"); con.execute("DELETE FROM local_stock"); con.execute("DELETE FROM proxies")
            for k in d.get('keymaps',[]): con.execute("INSERT INTO keymaps(sku,input_key,product_id,is_active,group_name,provider_type,base_url,api_key) VALUES(?,?,?,?,?,?,?,?)",(k['sku'],k['input_key'],k['product_id'],k['is_active'],k['group_name'],k['provider_type'],k['base_url'],k['api_key']))
            for l in d.get('local_stock',[]): con.execute("INSERT INTO local_stock(group_name,content,added_at) VALUES(?,?,?)",(l['group_name'],l['content'],l['added_at']))
            con.commit()
    return redirect(url_for('admin_index'))

# --- PUBLIC API ---
@app.route("/stock")
def api_stock():
    k = request.args.get("key","").strip()
    with db() as con: r = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (k,)).fetchone()
    return jsonify({"sum": get_stock_count(r) if r else 0})

@app.route("/fetch")
def api_fetch():
    k = request.args.get("key","").strip()
    try: q = int(request.args.get("quantity","1"))
    except: q=1
    with db() as con: r = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (k,)).fetchone()
    return jsonify(fetch_item(r, q) if r else [])

# ==============================================================================
#   STARTUP
# ==============================================================================
init_db()
if not proxy_checker_started: proxy_checker_started=True; threading.Thread(target=proxy_loop, daemon=True).start()
if not ping_service_started: ping_service_started=True; threading.Thread(target=ping_loop, daemon=True).start()
if not auto_backup_started: auto_backup_started=True; threading.Thread(target=backup_loop, daemon=True).start()

try:
    with db() as c: set_current_proxy(c.execute("SELECT value FROM config WHERE key='selected_proxy_string'").fetchone()['value'])
except: pass

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
