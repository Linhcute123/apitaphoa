import os, json, sqlite3, re, traceback
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

DB = os.getenv("DB_PATH", "store_profiles.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adminlinhdz")
REQ_TIMEOUT = int(os.getenv("REQ_TIMEOUT", "4"))
DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "0") in ("1","true","True","yes","YES")

app = Flask(__name__)

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def _ensure_col(con, table, col, decl):
    try:
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    except Exception:
        pass

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS sites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL,
            api_key TEXT,
            detail_path TEXT,
            list_path TEXT,
            buy_path TEXT,
            key_param TEXT,
            stock_field TEXT
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS keymaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            input_key TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            is_active INTEGER DEFAULT 1,
            UNIQUE(input_key, site_id),
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        )""")
        _ensure_col(con,"sites","api_key","TEXT")
        _ensure_col(con,"sites","detail_path","TEXT")
        _ensure_col(con,"sites","list_path","TEXT")
        _ensure_col(con,"sites","buy_path","TEXT")
        _ensure_col(con,"sites","key_param","TEXT")
        _ensure_col(con,"sites","stock_field","TEXT")
        con.execute("""UPDATE sites SET
            detail_path = COALESCE(detail_path, '/api/product.php'),
            list_path   = COALESCE(list_path,   '/api/products.php'),
            buy_path    = COALESCE(buy_path,    '/api/buy_product')
        """)
        con.commit()
init_db()

DEFAULT_KEY_NAMES = ("key","api_key","api_keyy")

def _join_url(base, path):
    base = base.rstrip("/")
    if not path.startswith("/"):
        path = "/" + path
    return base + path

def _http_get_json(url, params):
    r = requests.get(url, params=params, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _http_post_json(url, data):
    r = requests.post(url, data=data, timeout=REQ_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _extract_int(value):
    if isinstance(value,(int,float)): return int(value)
    if value is None: return None
    m = re.search(r'[-+]?\\d[\\d,\\.]*', str(value))
    if not m: return None
    num = m.group(0).replace('.','').replace(',','')
    try: return int(num)
    except: return None

def _deep_find_stock(obj):
    keys = {'stock','tonkho','available','remain','left','quantity','qty','soluong','so_luong'}
    if isinstance(obj, dict):
        for k,v in obj.items():
            if k.lower() in keys:
                n = _extract_int(v)
                if n is not None: return n
            n = _deep_find_stock(v)
            if n is not None: return n
    elif isinstance(obj, list):
        for it in obj:
            n = _deep_find_stock(it)
            if n is not None: return n
    return None

def _walk_field(obj, dotted):
    cur = obj
    for p in dotted.split("."):
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return None
    return cur

def _choose_keynames(preferred):
    if preferred and preferred.strip():
        return (preferred.strip(),)
    return DEFAULT_KEY_NAMES

def provider_product_detail(site, pid):
    url = _join_url(site["base_url"], site["detail_path"] or "/api/product.php")
    for k in _choose_keynames(site["key_param"]):
        try:
            return _http_get_json(url, {k: site["api_key"], "id": pid})
        except Exception:
            continue
    raise Exception("detail_refused")

def provider_products_list(site):
    url = _join_url(site["base_url"], site["list_path"] or "/api/products.php")
    for k in _choose_keynames(site["key_param"]):
        try:
            return _http_get_json(url, {k: site["api_key"]})
        except Exception:
            continue
    raise Exception("list_refused")

def provider_buy(site, pid, amount):
    url = _join_url(site["base_url"], site["buy_path"] or "/api/buy_product")
    for k in _choose_keynames(site["key_param"]):
        try:
            return _http_post_json(url, {"action":"buyProduct","id":pid,"amount":amount,k:site["api_key"]})
        except Exception:
            continue
    raise Exception("buy_refused")

def provider_resolve_stock(site, pid):
    try:
        d = provider_product_detail(site, pid)
        n = _extract_int(_walk_field(d, site["stock_field"])) if site["stock_field"] else _deep_find_stock(d)
        if isinstance(n,int): return max(0,n)
    except Exception:
        pass
    try:
        lst = provider_products_list(site)
        items = []
        if isinstance(lst, dict):
            for k in ("products","data","items","result"):
                v = lst.get(k)
                if isinstance(v, list): items=v; break
        elif isinstance(lst, list):
            items = lst
        for it in items:
            if not isinstance(it, dict): continue
            pid2 = it.get("id", it.get("product_id"))
            if str(pid2)==str(pid):
                n = _extract_int(_walk_field(it, site["stock_field"])) if site["stock_field"] else _deep_find_stock(it)
                if isinstance(n,int): return max(0,n)
    except Exception:
        pass
    raise Exception("stock_not_found")

# -------- Admin UI --------
TPL = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Tạp Hóa – Multi Provider</title>
<style>
  :root { --bd:#e5e7eb; }
  body { font-family: system-ui, Arial; padding:28px; color:#111 }
  .card { border:1px solid var(--bd); border-radius:12px; padding:16px; margin-bottom:18px; }
  .row { display:grid; grid-template-columns: repeat(12, 1fr); gap:12px; align-items:end; }
  .col-2 { grid-column: span 2; } .col-3 { grid-column: span 3; } .col-4 { grid-column: span 4; } .col-6 { grid-column: span 6; } .col-12 { grid-column: span 12; }
  label { font-size:12px; text-transform:uppercase; color:#444; }
  input { width:100%; padding:10px 12px; border:1px solid var(--bd); border-radius:10px; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 12px; border-bottom:1px solid var(--bd); text-align:left; }
  /* === MỚI: Thêm word-break: keep-all cho TD để ưu tiên không xuống dòng === */
  td { word-break: keep-all; } 
  code { background:#f3f4f6; padding:2px 6px; border-radius:6px; }
  button, .btn { padding:10px 14px; border-radius:10px; border:1px solid #111; background:#111; color:#fff; cursor:pointer; text-decoration:none; }
  .btn.red { background:#b91c1c; border-color:#991b1b; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
</head>
<body>
  <h2>⚙️ Multi Provider – API key theo Site</h2>

  <div class="card">
    <h3>1) Thêm/Update Site</h3>
    <form method="post" action="{{ url_for('admin_add_site') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-3"><label>Site code</label><input class="mono" name="code" placeholder="mail72h" required></div>
        <div class="col-6"><label>Base URL</label><input class="mono" name="base_url" placeholder="https://mail72h.com" required></div>
        <div class="col-3"><label>API key</label><input class="mono" name="api_key" type="password" required></div>
        <div class="col-4"><label>Detail path</label><input class="mono" name="detail_path" value="/api/product.php"></div>
        <div class="col-4"><label>List path</label><input class="mono" name="list_path" value="/api/products.php"></div>
        <div class="col-4"><label>Buy path</label><input class="mono" name="buy_path" value="/api/buy_product"></div>
        <div class="col-4"><label>Key param</label><input class="mono" name="key_param" placeholder="auto | key | api_key"></div>
        <div class="col-4"><label>Stock field</label><input class="mono" name="stock_field" placeholder="vd: data.stock (để trống = auto)"></div>
        <div class="col-2"><button>Lưu site</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>2) Thêm/Update Key</h3>
    <form method="post" action="{{ url_for('admin_add_key') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-3"><label>Site</label><input class="mono" name="site_code" placeholder="mail72h" required></div>
        <div class="col-3"><label>SKU</label><input class="mono" name="sku" placeholder="edu24h" required></div>
        <div class="col-3"><label>input_key</label><input class="mono" name="input_key" placeholder="key-abc" required></div>
        <div class="col-3"><label>product_id</label><input class="mono" name="product_id" type="number" placeholder="28" required></div>
        <div class="col-2"><button>Lưu key</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>Sites</h3>
    <table>
      <thead><tr><th>Code</th><th>Base</th><th>Key param</th><th>Detail</th><th>List</th><th>Buy</th><th>Stock field</th><th>Xoá</th></tr></thead>
      <tbody>
      {% for s in sites %}
        <tr>
          <td><code>{{ s['code'] }}</code></td>
          <td class="mono">{{ s['base_url'] }}</td>
          <td class="mono">{{ s['key_param'] or 'auto' }}</td>
          <td class="mono">{{ s['detail_path'] }}</td>
          <td class="mono">{{ s['list_path'] }}</td>
          <td class="mono">{{ s['buy_path'] }}</td>
          <td class="mono">{{ s['stock_field'] or '' }}</td>
          <td><a class="btn red" href="{{ url_for('admin_delete_site') }}?admin_secret={{ asec }}&code={{ s['code'] }}" onclick="return confirm('Xoá site {{ s['code'] }}?')">Xoá</a></td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Keys</h3>
    <table>
      <thead><tr><th>ID</th><th>Site</th><th>SKU</th><th>input_key</th><th>product_id</th><th>Active</th><th>Test</th><th>Xoá</th></tr></thead>
      <tbody>
      {% for m in maps %}
        <tr>
          <td>{{ m['id'] }}</td>
          <td><code>{{ m['site_code'] }}</code></td>
          <td style="white-space: nowrap;">{{ m['sku'] }}</td>
          <td style="white-space: nowrap;"><code>{{ m['input_key'] }}</code></td>
          <td style="white-space: nowrap;">{{ m['product_id'] }}</td> 
          <td>{{ m['is_active'] }}</td>
          <td style="white-space: nowrap; min-width: 200px;"> <div style="display: flex; gap: 4px; align-items: center; flex-wrap: nowrap;">
                <a class="mono" href="{{ url_for('admin_test_stock') }}?admin_secret={{ asec }}&key={{ m['input_key'] }}" target="_blank">Test stock</a>
                &nbsp;|&nbsp;
                <a class="mono" href="{{ url_for('admin_test_fetch') }}?admin_secret={{ asec }}&key={{ m['input_key'] }}&quantity=1" target="_blank">Test fetch</a>
             </div>
          </td>
          <td style="white-space: nowrap;"><a class="btn red" href="{{ url_for('admin_delete_key') }}?admin_secret={{ asec }}&id={{ m['id'] }}" onclick="return confirm('Xoá key {{ m['input_key'] }}?')">Xoá</a></td>
          </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Endpoint cho Tạp Hóa</h3>
    <pre class="mono">
Tồn kho (đọc trực tiếp):
  GET /stock?key=&lt;input_key&gt;

Lấy hàng (mua trực tiếp):
  GET /fetch?key=&lt;input_key&gt;&order_id={order_id}&quantity={quantity}
    </pre>
  </div>

  <div class="card">
    <h3>Diag nhanh</h3>
    <form method="get" action="{{ url_for('admin_diag') }}">
      <input type="hidden" name="admin_secret" value="{{ asec }}"/>
      <div class="row">
        <div class="col-4"><label>input_key</label><input class="mono" name="key" placeholder="key-abc"></div>
        <div class="col-2"><label>product_id</label><input class="mono" name="pid" placeholder="28"></div>
        <div class="col-2"><button>Xem raw</button></div>
      </div>
    </form>
  </div>
</body>
</html>
"""

def require_admin():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        abort(403)

@app.errorhandler(Exception)
def on_err(e):
    if DEBUG_ERRORS:
        return f"<pre>{traceback.format_exc()}</pre>", 500
    raise e

@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        sites = con.execute("SELECT * FROM sites ORDER BY code").fetchall()
        maps = con.execute("""
            SELECT k.*, s.code AS site_code
            FROM keymaps k JOIN sites s ON s.id = k.site_id
            ORDER BY s.code, k.id
        """).fetchall()
    return render_template_string(TPL, sites=sites, maps=maps, asec=ADMIN_SECRET)

@app.route("/admin/site", methods=["POST"])
def admin_add_site():
    require_admin()
    f = request.form
    code = f.get("code","").strip()
    base_url = f.get("base_url","").strip()
    api_key = f.get("api_key","").strip()
    detail_path = f.get("detail_path","/api/product.php").strip() or "/api/product.php"
    list_path   = f.get("list_path","/api/products.php").strip() or "/api/products.php"
    buy_path    = f.get("buy_path","/api/buy_product").strip() or "/api/buy_product"
    key_param   = f.get("key_param","").strip()
    stock_field = f.get("stock_field","").strip()
    if not code or not base_url or not api_key:
        return "Missing code/base_url/api_key", 400
    with db() as con:
        con.execute("""
        INSERT INTO sites(code, base_url, api_key, detail_path, list_path, buy_path, key_param, stock_field)
        VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(code) DO UPDATE SET
            base_url=excluded.base_url,
            api_key=excluded.api_key,
            detail_path=excluded.detail_path,
            list_path=excluded.list_path,
            buy_path=excluded.buy_path,
            key_param=excluded.key_param,
            stock_field=excluded.stock_field
        """, (code, base_url, api_key, detail_path, list_path, buy_path, key_param, stock_field))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key", methods=["POST"])
def admin_add_key():
    require_admin()
    f = request.form
    site_code = f.get("site_code","").strip()
    sku = f.get("sku","").strip()
    input_key = f.get("input_key","").strip()
    product_id = f.get("product_id","").strip()
    if not all([site_code, sku, input_key, product_id]) or not product_id.isdigit():
        return "Missing/invalid fields", 400
    with db() as con:
        s = con.execute("SELECT id FROM sites WHERE code=?", (site_code,)).fetchone()
        if not s:
            return "Site not found", 400
        con.execute("""
        INSERT INTO keymaps(site_id, sku, input_key, product_id, is_active)
        VALUES(?,?,?,?,1)
        ON CONFLICT(input_key, site_id) DO UPDATE SET
            sku=excluded.sku,
            product_id=excluded.product_id,
            is_active=1
        """, (s["id"], sku, input_key, int(product_id)))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/delete/site")
def admin_delete_site():
    require_admin()
    code = request.args.get("code","").strip()
    if not code: return "missing code", 400
    with db() as con:
        con.execute("DELETE FROM sites WHERE code=?", (code,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/delete/key")
def admin_delete_key():
    require_admin()
    kid = request.args.get("id","").strip()
    if not kid or not kid.isdigit(): return "missing id", 400
    with db() as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (int(kid),))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

# ---- Diag raw ----
@app.route("/admin/diag")
def admin_diag():
    require_admin()
    key = request.args.get("key","").strip()
    pid = request.args.get("pid","").strip()
    if not key or not pid:
        return jsonify({"status":"error","msg":"missing key/pid"}), 400
    km = _get_site_and_key(key)
    if not km:
        return jsonify({"status":"error","msg":"unknown key"}), 404
    site = km
    out = {}
    # detail
    try:
        out["detail"] = provider_product_detail(site, int(pid))
    except Exception as e:
        out["detail_error"] = str(e)
    # list
    try:
        out["list"] = provider_products_list(site)
    except Exception as e:
        out["list_error"] = str(e)
    return jsonify(out)

def _get_site_and_key(input_key):
    with db() as con:
        row = con.execute("""
        SELECT k.*, s.code AS site_code, s.base_url, s.api_key, s.detail_path, s.list_path, s.buy_path, s.key_param, s.stock_field
        FROM keymaps k JOIN sites s ON s.id = k.site_id
        WHERE k.input_key=? AND k.is_active=1
        """, (input_key,)).fetchone()
    return row

# ---- Admin tests ----
@app.route("/admin/test/stock")
def admin_test_stock():
    require_admin()
    key = request.args.get("key","").strip()
    if not key: return jsonify({"status":"error","msg":"missing key"}), 400
    km = _get_site_and_key(key)
    if not km: return jsonify({"status":"error","msg":"unknown key"}), 404
    try:
        s = provider_resolve_stock(km, int(km["product_id"]))
        return jsonify({"parsed_stock": s})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

@app.route("/admin/test/fetch")
def admin_test_fetch():
    require_admin()
    key = request.args.get("key","").strip()
    qty = int(request.args.get("quantity","1") or "1")
    km = _get_site_and_key(key)
    if not km: return jsonify({"status":"error","msg":"unknown key"}), 404
    try:
        r = provider_buy(km, int(km["product_id"]), qty)
        return jsonify(r)
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

# ---- Public ----
@app.route("/stock")
def stock():
    key = request.args.get("key","").strip()
    if not key: return jsonify({"status":"error","msg":"missing key"}), 400
    km = _get_site_and_key(key)
    if not km: return jsonify({"status":"error","msg":"unknown key"}), 404
    try:
        s = provider_resolve_stock(km, int(km["product_id"]))
        return jsonify({"sum": int(s)})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

@app.route("/fetch")
def fetch():
    key = request.args.get("key","").strip()
    qty_s = request.args.get("quantity","").strip()
    order_id = request.args.get("order_id","").strip()
    if not key or not qty_s: return jsonify({"status":"error","msg":"missing key/quantity"}), 400
    try:
        qty = int(qty_s)
        if qty <= 0 or qty > 1000: raise ValueError()
    except Exception:
        return jsonify({"status":"error","msg":"invalid quantity"}), 400
    km = _get_site_and_key(key)
    if not km: return jsonify({"status":"error","msg":"unknown key"}), 404
    try:
        r = provider_buy(km, int(km["product_id"]), qty)
        return jsonify(r)
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

@app.route("/")
def health(): return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT","8000")))
