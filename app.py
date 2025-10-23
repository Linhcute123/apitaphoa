
import os, json, sqlite3
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

DB = os.getenv("DB_PATH", "store.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
MAIL_TIMEOUT = int(os.getenv("MAIL_TIMEOUT", "4"))  # giây, giữ <5s tổng

app = Flask(__name__)

# -------------- DB -----------------
def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        # "sites" = mỗi web cung cấp hàng (vd: mail72h, webA, webB)
        con.execute("""
        CREATE TABLE IF NOT EXISTS sites(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT NOT NULL UNIQUE,     -- ví dụ: mail72h
            base_url TEXT NOT NULL         -- ví dụ: https://mail72h.com/api
        )""")
        # mỗi input_key của Tạp Hóa map sang product_id + api_key trên *một site*
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

# -------------- Provider helpers (generic) --------------
def provider_product_detail(base_url: str, api_key: str, product_id: int) -> dict:
    """
    Chuẩn cho kiểu 'mail72h' (GET product.php?api_key=...&id=...).
    Với web khác, bạn có thể sửa adapter ở đây.
    """
    url = f"{base_url.rstrip('/')}/product.php"
    r = requests.get(url, params={"api_key": api_key, "id": product_id}, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

def provider_buy(base_url: str, api_key: str, product_id: int, amount: int) -> dict:
    """
    Chuẩn cho kiểu 'mail72h' (POST buy_product).
    Với web khác, sửa adapter này cho khớp.
    """
    url = f"{base_url.rstrip('/')}/buy_product"
    data = {"action": "buyProduct", "id": product_id, "amount": amount, "api_key": api_key}
    r = requests.post(url, data=data, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

# -------------- Admin UI -----------------
TPL = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Tạp Hóa – Multi Site Direct</title>
<style>
  :root { --b:#111; --g:#f5f5f7; --bd:#ddd; }
  body { font-family: system-ui, Arial; padding:28px; background:#fff; color:#111; }
  h2 { margin:0 0 12px; }
  h3 { margin:24px 0 8px; }
  .grid { display:grid; gap:12px; }
  .card { border:1px solid var(--bd); border-radius:12px; padding:16px; background:#fff; }
  label { font-size:12px; text-transform:uppercase; letter-spacing:.02em; color:#444; display:block; margin-bottom:6px; }
  input { width:100%; padding:10px 12px; border:1px solid var(--bd); border-radius:10px; outline:none; }
  input:focus { border-color:#333; }
  .row { display:grid; grid-template-columns: repeat(12, 1fr); gap:12px; align-items:end; }
  .col-3 { grid-column: span 3; } .col-4 { grid-column: span 4; } .col-5 { grid-column: span 5; }
  .col-6 { grid-column: span 6; } .col-8 { grid-column: span 8; } .col-9 { grid-column: span 9; } .col-12{ grid-column: span 12;}
  button { padding:10px 14px; border-radius:10px; border:1px solid #111; background:#111; color:#fff; cursor:pointer; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:10px 12px; border-bottom:1px solid var(--bd); text-align:left; }
  th { background:#fafafa; }
  code { background:#f3f3f3; padding:2px 6px; border-radius:6px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
</style>
</head>
<body>
  <h2>⚙️ Multi Site (Direct): lấy hàng thẳng từ site nhà cung cấp</h2>

  <div class="card">
    <h3>1) Thêm Site (web nhà cung cấp)</h3>
    <form method="post" action="{{ url_for('admin_add_site') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-4">
          <label>Site code (vd: mail72h)</label>
          <input class="mono" type="text" name="code" placeholder="mail72h" required>
        </div>
        <div class="col-8">
          <label>Base API URL</label>
          <input class="mono" type="text" name="base_url" placeholder="https://mail72h.com/api" required>
        </div>
        <div class="col-12"><button>Thêm/Update Site</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>2) Thêm key cho 1 Site</h3>
    <form method="post" action="{{ url_for('admin_add_key') }}?admin_secret={{ asec }}">
      <div class="row">
        <div class="col-3">
          <label>Site</label>
          <input class="mono" type="text" name="site_code" placeholder="mail72h" required>
        </div>
        <div class="col-3">
          <label>SKU</label>
          <input class="mono" type="text" name="sku" placeholder="edu24h" required>
        </div>
        <div class="col-3">
          <label>input_key (Tạp Hóa)</label>
          <input class="mono" type="text" name="input_key" placeholder="key-abc" required>
        </div>
        <div class="col-3">
          <label>product_id</label>
          <input class="mono" type="number" name="product_id" placeholder="12345" required>
        </div>
        <div class="col-6">
          <label>API key của site (dùng để mua hàng)</label>
          <input class="mono" type="password" name="provider_api_key" placeholder="paste API key" required>
        </div>
        <div class="col-12"><button>Thêm/Update Key</button></div>
      </div>
    </form>
  </div>

  <div class="card">
    <h3>Danh sách Sites</h3>
    <table>
      <thead><tr><th>ID</th><th>Code</th><th>Base URL</th></tr></thead>
      <tbody>
      {% for s in sites %}
        <tr><td>{{ s['id'] }}</td><td><code>{{ s['code'] }}</code></td><td class="mono">{{ s['base_url'] }}</td></tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Danh sách Key maps</h3>
    <table>
      <thead><tr><th>Site</th><th>SKU</th><th>input_key</th><th>product_id</th><th>Active</th><th>Hành động</th></tr></thead>
      <tbody>
      {% for m in maps %}
        <tr>
          <td><code>{{ m['site_code'] }}</code></td>
          <td>{{ m['sku'] }}</td>
          <td><code>{{ m['input_key'] }}</code></td>
          <td>{{ m['product_id'] }}</td>
          <td>{{ m['is_active'] }}</td>
          <td>
            <form method="post" action="{{ url_for('admin_toggle_key', kmid=m['id']) }}?admin_secret={{ asec }}" style="display:inline">
              <button>{{ 'Disable' if m['is_active'] else 'Enable' }}</button>
            </form>
            <form method="post" action="{{ url_for('admin_delete_key', kmid=m['id']) }}?admin_secret={{ asec }}" style="display:inline" onsubmit="return confirm('Xoá key {{m['input_key']}}?')">
              <button>Xoá</button>
            </form>
          </td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>

  <div class="card">
    <h3>Endpoint cho Tạp Hóa (bắt buộc truyền site)</h3>
    <pre class="mono">
Tồn kho (đọc trực tiếp từ site):
  GET /stock?site=&lt;site_code&gt;&key=&lt;input_key&gt;

Lấy hàng (mua trực tiếp từ site):
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
    if not code or not base: abort(400)
    with db() as con:
        con.execute("""
            INSERT INTO sites(code, base_url) VALUES(?,?)
            ON CONFLICT(code) DO UPDATE SET base_url=excluded.base_url
        """, (code, base))
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
    if not site_code or not sku or not input_key or not product_id.isdigit() or not provider_api_key:
        abort(400)
    with db() as con:
        s = con.execute("SELECT id FROM sites WHERE code=?", (site_code,)).fetchone()
        if not s: abort(400)
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

@app.route("/admin/key/<int:kmid>/toggle", methods=["POST"])
def admin_toggle_key(kmid):
    require_admin()
    with db() as con:
        row = con.execute("SELECT is_active FROM keymaps WHERE id=?", (kmid,)).fetchone()
        if not row: abort(404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE keymaps SET is_active=? WHERE id=?", (newv, kmid))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/key/<int:kmid>", methods=["POST"])
def admin_delete_key(kmid):
    require_admin()
    with db() as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (kmid,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

# ---------- Public endpoints (require ?site= & ?key=) -----------
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
    # đọc kho thật
    try:
        pd = provider_product_detail(s["base_url"], km["provider_api_key"], int(km["product_id"]))
        stock_val = int(pd.get("data", {}).get("stock", 0))
    except Exception:
        stock_val = 9999
    return jsonify({"sum": stock_val})

@app.route("/fetch")
def fetch():
    site = request.args.get("site","").strip()
    key = request.args.get("key","").strip()
    order_id = request.args.get("order_id","").strip()
    qty_s = request.args.get("quantity","").strip()
    if not site or not key or not qty_s:
        return jsonify({"status":"error","msg":"missing site/key/quantity"}), 400
    try:
        qty = int(qty_s); 
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
        return jsonify({"status":"error","msg":f"provider error: {e}"}), 502

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

@app.route("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
