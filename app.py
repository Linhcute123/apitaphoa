
import os, json, sqlite3, re, traceback
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

# ====== ENV ======
DB = os.getenv("DB_PATH", "store_v2.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "adminlinhdz")
MAIL_TIMEOUT = int(os.getenv("MAIL72H_TIMEOUT", "4"))  # giữ tổng < 5s cho TapHoa
DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "0") in ("1","true","True","yes","YES")

app = Flask(__name__)

# ====== DB ======
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS sites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,
            base_url TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS keymaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_id INTEGER NOT NULL,
            sku TEXT NOT NULL,
            input_key TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            provider_api_key TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            UNIQUE(input_key, site_id),
            FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE
        )""")
        con.commit()
init_db()

# ====== Provider adapters (Mail72h-style, auto key param & /api suffix) ======
_KEY_CANDIDATES = ("key", "api_key", "api_keyy")

def _bases_to_try(base_url: str):
    base = base_url.rstrip("/")
    cands = [base]
    if not re.search(r"/api/?$", base, re.IGNORECASE):
        cands.append(base + "/api")
    return cands

def _http_get_json(url, params):
    r = requests.get(url, params=params, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _http_post_json(url, data):
    r = requests.post(url, data=data, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

def provider_product_detail(base_url: str, api_key: str, product_id: int) -> dict:
    last_err = None
    for b in _bases_to_try(base_url):
        url = f"{b}/product.php"
        for keyname in _KEY_CANDIDATES:
            try:
                params = {keyname: api_key, "id": product_id}
                data = _http_get_json(url, params)
                return data
            except Exception as e:
                last_err = e
                continue
    raise Exception(f"product.php failed on all bases/key formats: {last_err}")

def provider_buy(base_url: str, api_key: str, product_id: int, amount: int) -> dict:
    last_err = None
    for b in _bases_to_try(base_url):
        url = f"{b}/buy_product"
        for keyname in _KEY_CANDIDATES:
            try:
                data = {"action": "buyProduct", "id": product_id, "amount": amount, keyname: api_key}
                res = _http_post_json(url, data)
                return res
            except Exception as e:
                last_err = e
                continue
    raise Exception(f"buy_product failed on all bases/key formats: {last_err}")

# ====== Utilities ======
def _extract_int(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    m = re.search(r'[-+]?\\d[\\d,\\.]*', s)
    if not m:
        return None
    num = m.group(0).replace('.', '').replace(',', '')
    try:
        return int(num)
    except Exception:
        return None

def _deep_find_stock(obj):
    keys = {'stock','tonkho','kho','remain','available','quantity','qty','so_luong','soluong','stock_left','left'}
    best = None
    if isinstance(obj, dict):
        for k,v in obj.items():
            if k.lower() in keys:
                val = _extract_int(v)
                if val is not None:
                    best = val if best is None else max(best, val)
            val2 = _deep_find_stock(v)
            if val2 is not None:
                best = val2 if best is None else max(best, val2)
    elif isinstance(obj, list):
        for it in obj:
            val2 = _deep_find_stock(it)
            if val2 is not None:
                best = val2 if best is None else max(best, val2)
    return best

# ====== Error handler (optional debug) ======
@app.errorhandler(Exception)
def on_err(e):
    if DEBUG_ERRORS:
        tb = traceback.format_exc()
        return f"<h2>ERROR</h2><pre>{tb}</pre>", 500, {"Content-Type":"text/html; charset=utf-8"}
    raise e

# ====== Admin UI ======
TPL = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Tạp Hóa – Multi Site Direct</title>
<style>
  :root { --b:#111; --g:#f5f5f7; --bd:#ddd; }
  body { font-family: system-ui, Arial; padding:28px; background:#fff; color:#111; }
  h2 { margin:0 0 14px; }
  h3 { margin:24px 0 8px; }
  .card { border:1px solid var(--bd); border-radius:12px; padding:16px; background:#fff; }
  label { font-size:12px; text-transform:uppercase; letter-spacing:.02em; color:#444; display:block; margin-bottom:6px; }
  input { width:100%; padding:10px 12px; border:1px solid var(--bd); border-radius:10px; outline:none; }
  .row { display:grid; grid-template-columns: repeat(12, 1fr); gap:12px; align-items:end; }
  .col-3 { grid-column: span 3; } .col-6 { grid-column: span 6; } .col-12 { grid-column: span 12; }
  button { padding:10px 14px; border-radius:10px; border:1px solid #111; background:#111; color:#fff; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 12px; border-bottom:1px solid var(--bd); text-align:left; }
  th { background:#fafafa; }
  code { background:#f3f3f3; padding:2px 6px; border-radius:6px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
  .actions form { display:inline }
</style>
</head>
<body>
  <h2>⚙️ Multi Site (Direct) – lấy hàng trực tiếp, đúng kho từ provider</h2>

  <div class="card">
    <h3>1) Thêm/Update Site</h3>
    <form method="post" action="{{ url_for('admin_add_site') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-3"><label>Site code</label><input class="mono" type="text" name="code" placeholder="mail72h" required></div>
        <div class="col-6"><label>Base API URL</label><input class="mono" type="text" name="base_url" placeholder="https://mail72h.com/api" required></div>
        <div class="col-3"><button>Save Site</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>2) Thêm/Update Key</h3>
    <form method="post" action="{{ url_for('admin_add_key') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-3"><label>Site</label><input class="mono" type="text" name="site_code" placeholder="mail72h" required></div>
        <div class="col-3"><label>SKU</label><input class="mono" type="text" name="sku" placeholder="edu24h" required></div>
        <div class="col-3"><label>input_key</label><input class="mono" type="text" name="input_key" placeholder="key-abc" required></div>
        <div class="col-3"><label>product_id</label><input class="mono" type="number" name="product_id" placeholder="12345" required></div>
        <div class="col-6"><label>API key của site</label><input class="mono" type="password" name="provider_api_key" placeholder="paste API key" required></div>
        <div class="col-3"><button>Save Key</button></div>
      </div>
    </form>
    <details style="margin-top:12px;">
      <summary><b>Thêm nhanh nhiều dòng</b> (mỗi dòng: <code>site_code,sku,input_key,product_id,api_key</code>)</summary>
      <form method="post" action="{{ url_for('admin_bulk_key') }}?admin_secret={{ asec }}">
        <textarea name="bulk" rows="6" style="width:100%;padding:10px;border:1px solid #ddd;border-radius:10px;font-family:ui-monospace" placeholder="mail72h,edu24h,key-abc,12345,APIKEY...
mail72h,edu30d,key-xyz,29,APIKEY..."></textarea>
        <div style="margin-top:8px"><button>Import</button></div>
      </form>
    </details>
  </div>

  <div class="card">
    <h3>Sites</h3>
    <table>
      <thead><tr><th>ID</th><th>Code</th><th>Base URL</th><th>Actions</th></tr></thead>
      <tbody>
      {% for s in sites %}
        <tr>
          <td>{{ s['id'] }}</td>
          <td><code>{{ s['code'] }}</code></td>
          <td class="mono">{{ s['base_url'] }}</td>
          <td class="actions">
            <form method="post" action="{{ url_for('admin_del_site', site_id=s['id']) }}?admin_secret={{ asec }}" onsubmit="return confirm('Xoá site {{s['code']}}?')"><button>Delete</button></form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Keys</h3>
    <table>
      <thead><tr><th>ID</th><th>Site</th><th>SKU</th><th>input_key</th><th>product_id</th><th>Active</th><th>Test</th><th>Actions</th></tr></thead>
      <tbody>
      {% for m in maps %}
        <tr>
          <td>{{ m['id'] }}</td>
          <td><code>{{ m['site_code'] }}</code></td>
          <td>{{ m['sku'] }}</td>
          <td><code>{{ m['input_key'] }}</code></td>
          <td>{{ m['product_id'] }}</td>
          <td>{{ m['is_active'] }}</td>
          <td>
            <a class="mono" href="{{ url_for('admin_test_stock') }}?admin_secret={{ asec }}&site={{ m['site_code'] }}&key={{ m['input_key'] }}" target="_blank">Test stock</a>
            &nbsp;|&nbsp;
            <a class="mono" href="{{ url_for('admin_test_fetch') }}?admin_secret={{ asec }}&site={{ m['site_code'] }}&key={{ m['input_key'] }}&quantity=1" target="_blank">Test fetch</a>
          </td>
          <td class="actions">
            <form method="post" action="{{ url_for('admin_toggle_key', kmid=m['id']) }}?admin_secret={{ asec }}"><button>{{ 'Disable' if m['is_active'] else 'Enable' }}</button></form>
            <form method="post" action="{{ url_for('admin_del_key', kmid=m['id']) }}?admin_secret={{ asec }}" onsubmit="return confirm('Xoá key {{m['input_key']}}?')"><button>Delete</button></form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Endpoint cho Tạp Hóa</h3>
    <pre class="mono">
Tồn kho (đọc trực tiếp từ provider):
  GET /stock?site=&lt;site_code&gt;&key=&lt;input_key&gt;

Lấy hàng (mua trực tiếp từ provider):
  GET /fetch?site=&lt;site_code&gt;&key=&lt;input_key&gt;&order_id={order_id}&quantity={quantity}
    </pre>
  </div>

</body>
</html>
"""

def require_admin():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        abort(403)

@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        sites = con.execute("SELECT * FROM sites ORDER BY id").fetchall()
        maps = con.execute("""
            SELECT k.*, s.code AS site_code FROM keymaps k
            JOIN sites s ON s.id = k.site_id
            ORDER BY s.code, k.id
        """).fetchall()
    return render_template_string(TPL, sites=sites, maps=maps, asec=ADMIN_SECRET)

@app.route("/admin/site", methods=["POST"])
def admin_add_site():
    require_admin()
    code = request.form.get("code","").strip()
    base = request.form.get("base_url","").strip()
    if not code or not base:
        return ("Missing code/base_url", 400)
    with db() as con:
        con.execute("""
            INSERT INTO sites(code, base_url) VALUES(?,?)
            ON CONFLICT(code) DO UPDATE SET base_url=excluded.base_url
        """, (code, base))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/site/<int:site_id>", methods=["POST"])
def admin_del_site(site_id):
    require_admin()
    with db() as con:
        con.execute("DELETE FROM sites WHERE id=?", (site_id,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key", methods=["POST"])
def admin_add_key():
    require_admin()
    site_code = request.form.get("site_code","").strip()
    sku = request.form.get("sku","").strip()
    input_key = request.form.get("input_key","").strip()
    product_id = request.form.get("product_id","").strip()
    provider_api_key = request.form.get("provider_api_key","").strip()
    if not all([site_code, sku, input_key, product_id, provider_api_key]) or not product_id.isdigit():
        return ("Missing/invalid fields", 400)
    with db() as con:
        s = con.execute("SELECT id FROM sites WHERE code=?", (site_code,)).fetchone()
        if not s:
            return ("Site not found", 400)
        con.execute("""
            INSERT INTO keymaps(site_id, sku, input_key, product_id, provider_api_key, is_active)
            VALUES(?,?,?,?,?,1)
            ON CONFLICT(input_key, site_id) DO UPDATE SET
              sku=excluded.sku,
              product_id=excluded.product_id,
              provider_api_key=excluded.provider_api_key,
              is_active=1
        """, (s["id"], sku, input_key, int(product_id), provider_api_key))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key/<int:kmid>", methods=["POST"])
def admin_del_key(kmid):
    require_admin()
    with db() as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (kmid,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key/<int:kmid>/toggle", methods=["POST"])
def admin_toggle_key(kmid):
    require_admin()
    with db() as con:
        row = con.execute("SELECT is_active FROM keymaps WHERE id=?", (kmid,)).fetchone()
        if not row:
            return ("Not found", 404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE keymaps SET is_active=? WHERE id=?", (newv, kmid))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key/bulk", methods=["POST"])
def admin_bulk_key():
    require_admin()
    raw = request.form.get("bulk","")
    ok, fail = 0, 0
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            fail += 1
            continue
        site_code, sku, input_key, product_id, api_key = parts[:5]
        if not product_id.isdigit():
            fail += 1; continue
        with db() as con:
            s = con.execute("SELECT id FROM sites WHERE code=?", (site_code,)).fetchone()
            if not s:
                fail += 1; continue
            con.execute("""
                INSERT INTO keymaps(site_id, sku, input_key, product_id, provider_api_key, is_active)
                VALUES(?,?,?,?,?,1)
                ON CONFLICT(input_key, site_id) DO UPDATE SET
                  sku=excluded.sku,
                  product_id=excluded.product_id,
                  provider_api_key=excluded.provider_api_key,
                  is_active=1
            """, (s["id"], sku, input_key, int(product_id), api_key))
            con.commit()
            ok += 1
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

# ====== Admin quick tests ======
@app.route("/admin/test/stock")
def admin_test_stock():
    require_admin()
    site = request.args.get("site","").strip()
    key = request.args.get("key","").strip()
    if not site or not key:
        return jsonify({"status":"error","msg":"missing site/key"}), 400
    s, km = _resolve_site_key(site, key)
    if not s: return jsonify({"status":"error","msg":"unknown site"}), 404
    if not km: return jsonify({"status":"error","msg":"unknown key"}), 404
    try:
        raw = provider_product_detail(s["base_url"], km["provider_api_key"], int(km["product_id"]))
        stock_val = _deep_find_stock(raw)
        return jsonify({"parsed_stock": stock_val, "raw": raw})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

@app.route("/admin/test/fetch")
def admin_test_fetch():
    require_admin()
    site = request.args.get("site","").strip()
    key = request.args.get("key","").strip()
    qty_s = request.args.get("quantity","1").strip()
    qty = int(qty_s) if qty_s.isdigit() else 1
    s, km = _resolve_site_key(site, key)
    if not s or not km:
        return jsonify({"status":"error","msg":"bad site/key"}), 400
    try:
        res = provider_buy(s["base_url"], km["provider_api_key"], int(km["product_id"]), qty)
        return jsonify(res)
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

# ====== Public endpoints ======
def _resolve_site_key(site_code: str, key: str):
    with db() as con:
        s = con.execute("SELECT * FROM sites WHERE code=?", (site_code,)).fetchone()
        if not s: return None, None
        k = con.execute("SELECT * FROM keymaps WHERE site_id=? AND input_key=? AND is_active=1",
                        (s["id"], key)).fetchone()
        if not k: return s, None
        return s, k

@app.route("/stock")
def stock():
    site = request.args.get("site","").strip()
    key = request.args.get("key","").strip()
    if not site or not key:
        return jsonify({"status":"error","msg":"missing site/key"}), 400
    s, km = _resolve_site_key(site, key)
    if not s: return jsonify({"status":"error","msg":"unknown site"}), 404
    if not km: return jsonify({"status":"error","msg":"unknown key for this site"}), 404

    try:
        raw = provider_product_detail(s["base_url"], km["provider_api_key"], int(km["product_id"]))
        stock_val = _deep_find_stock(raw)
        if stock_val is None:
            stock_val = 9999
        return jsonify({"sum": int(stock_val)})
    except requests.HTTPError as e:
        return jsonify({"status":"error","msg":f"http {e.response.status_code}"}), 502
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}), 502

@app.route("/fetch")
def fetch():
    site = request.args.get("site","").strip()
    key = request.args.get("key","").strip()
    order_id = request.args.get("order_id","").strip()
    qty_s = request.args.get("quantity","").strip()
    if not site or not key or not qty_s:
        return jsonify({"status":"error","msg":"missing site/key/quantity"}), 400
    try:
        qty = int(qty_s)
        if qty<=0 or qty>1000: raise ValueError()
    except Exception:
        return jsonify({"status":"error","msg":"invalid quantity"}), 400

    s, km = _resolve_site_key(site, key)
    if not s: return jsonify({"status":"error","msg":"unknown site"}), 404
    if not km: return jsonify({"status":"error","msg":"unknown key for this site"}), 404

    try:
        res = provider_buy(s["base_url"], km["provider_api_key"], int(km["product_id"]), qty)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        return jsonify({"status":"error","msg":f"provider http {code}"}), 502
    except Exception as e:
        return jsonify({"status":"error","msg":"provider error: {e}"}), 502

    if res.get("status") != "success":
        return jsonify({"status":"error","msg":res}), 409

    data = res.get("data")
    out = []
    if isinstance(data, list):
        for it in data:
            out.append({"product": (json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it))})
    else:
        t = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        out = [{"product": t} for _ in range(qty)]
    return jsonify(out)

@app.route("/debug")
def debug_info():
    info = {
        "env": {
            "DB_PATH": DB,
            "ADMIN_SECRET_set": bool(ADMIN_SECRET),
            "MAIL_TIMEOUT": MAIL_TIMEOUT,
            "DEBUG_ERRORS": DEBUG_ERRORS
        }
    }
    try:
        with db() as con:
            tables = con.execute("SELECT name, sql FROM sqlite_master WHERE type='table'").fetchall()
            info["tables"] = [{ "name": t["name"], "sql": t["sql"] } for t in tables]
    except Exception as e:
        info["db_error"] = str(e)
    return jsonify(info)

@app.route("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
