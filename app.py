
import os, json, sqlite3
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

DB = os.getenv("DB_PATH", "store.db")
MAIL72H_BASE = os.getenv("MAIL72H_BASE", "https://mail72h.com/api")
MAIL72H_API_KEY = os.getenv("MAIL72H_API_KEY", "REPLACE_ME")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
MAIL72H_TIMEOUT = int(os.getenv("MAIL72H_TIMEOUT", "4"))  # giữ <5s tổng thể

app = Flask(__name__)

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS keymaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            input_key TEXT NOT NULL UNIQUE,
            product_id INTEGER NOT NULL,
            note TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        con.commit()

init_db()

def mail72h_buy(product_id: int, amount: int, coupon: str|None=None) -> dict:
    data = {
        "action": "buyProduct",
        "id": product_id,
        "amount": amount,
        "api_key": MAIL72H_API_KEY
    }
    if coupon:
        data["coupon"] = coupon
    r = requests.post(f"{MAIL72H_BASE}/buy_product", data=data, timeout=MAIL72H_TIMEOUT)
    r.raise_for_status()
    return r.json()

def find_map_by_key(key: str):
    with db() as con:
        row = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (key,)).fetchone()
        return row

ADMIN_TPL = """
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<title>Quản lý nhiều input_key (direct mode)</title>
<style>
  body { font-family: system-ui, Arial; padding: 24px; }
  table { border-collapse: collapse; width: 100%; margin-top: 16px; }
  th, td { border:1px solid #ddd; padding:8px; }
  th { background:#f5f5f5; text-align:left; }
  input[type=text], input[type=number] { width: 100%; padding:6px; }
  .row { display:flex; gap:12px; flex-wrap:wrap; }
  .card { border:1px solid #ddd; padding:16px; border-radius:8px; margin-bottom:16px; }
  .btn { padding:8px 12px; border:1px solid #333; background:#fff; cursor:pointer; }
  .btn.primary { background:#111; color:#fff; }
  code { background:#f4f4f4; padding:2px 4px; border-radius:4px; }
</style>
</head>
<body>
  <h2>Direct mode: lấy hàng trực tiếp mail72h (không buffer)</h2>

  <div class="card">
    <h3>Thêm/Cập nhật input_key</h3>
    <form method="post" action="{{ url_for('admin_add_keymap') }}?admin_secret={{ admin_secret }}">
      <div class="row">
        <div style="flex:1 1 160px">
          <label>SKU</label>
          <input type="text" name="sku" required placeholder="vd: edu24h">
        </div>
        <div style="flex:2 1 280px">
          <label>input_key (Tạp Hóa)</label>
          <input type="text" name="input_key" required placeholder="key-abc">
        </div>
        <div style="flex:1 1 160px">
          <label>product_id (mail72h)</label>
          <input type="number" name="product_id" required placeholder="12345">
        </div>
        <div style="flex:3 1 320px">
          <label>Ghi chú</label>
          <input type="text" name="note" placeholder="tuỳ chọn">
        </div>
      </div>
      <button class="btn primary" type="submit">Thêm / Cập nhật</button>
    </form>
  </div>

  <h3>Danh sách key ↔ product_id</h3>
  <table>
    <thead><tr><th>SKU</th><th>input_key</th><th>product_id</th><th>Active</th><th>Ghi chú</th><th>Hành động</th></tr></thead>
    <tbody>
    {% for m in maps %}
      <tr>
        <td>{{ m['sku'] }}</td>
        <td><code>{{ m['input_key'] }}</code></td>
        <td>{{ m['product_id'] }}</td>
        <td>{{ m['is_active'] }}</td>
        <td>{{ m['note'] or '' }}</td>
        <td>
          <form method="post" action="{{ url_for('admin_toggle_key', kmid=m['id']) }}?admin_secret={{ admin_secret }}" style="display:inline">
            <button class="btn" type="submit">{{ 'Disable' if m['is_active'] else 'Enable' }}</button>
          </form>
          <form method="post" action="{{ url_for('admin_delete_key', kmid=m['id']) }}?admin_secret={{ admin_secret }}" style="display:inline" onsubmit="return confirm('Xoá key {{m['input_key']}}?')">
            <button class="btn" type="submit">Xoá</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h3>Endpoint cho Tạp Hóa</h3>
  <pre>
Tồn kho (không gọi mail72h, trả giá trị lớn để không chặn):
  GET /stock?key=&lt;input_key&gt;   → {"sum": 9999}

Lấy hàng (gọi trực tiếp mail72h, timeout {{ timeout }}s):
  GET /fetch?key=&lt;input_key&gt;&order_id={order_id}&quantity={quantity}
  </pre>
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
        maps = con.execute("SELECT * FROM keymaps ORDER BY sku, id").fetchall()
    return render_template_string(ADMIN_TPL, maps=maps, admin_secret=ADMIN_SECRET, timeout=MAIL72H_TIMEOUT)

@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    sku = request.form.get("sku","").strip()
    input_key = request.form.get("input_key","").strip()
    product_id = request.form.get("product_id","").strip()
    note = request.form.get("note","").strip()
    if not sku or not input_key or not product_id.isdigit():
        abort(400)
    with db() as con:
        con.execute("""
            INSERT INTO keymaps(sku, input_key, product_id, note, is_active)
            VALUES(?,?,?,?,1)
            ON CONFLICT(input_key) DO UPDATE SET
              sku=excluded.sku,
              product_id=excluded.product_id,
              note=excluded.note,
              is_active=1
        """, (sku, input_key, int(product_id), note))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/keymap/<int:kmid>/toggle", methods=["POST"])
def admin_toggle_key(kmid):
    require_admin()
    with db() as con:
        row = con.execute("SELECT is_active FROM keymaps WHERE id=?", (kmid,)).fetchone()
        if not row: abort(404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE keymaps SET is_active=? WHERE id=?", (newv, kmid))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/keymap/<int:kmid>", methods=["POST"])
def admin_delete_key(kmid):
    require_admin()
    with db() as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (kmid,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

# ===== Public endpoints =====
@app.route("/stock")
def stock():
    key = request.args.get("key","").strip()
    if not key:
        return jsonify({"status":"error","msg":"missing key"}), 400
    row = find_map_by_key(key)
    if not row:
        return jsonify({"status":"error","msg":"unknown key"}), 404
    # Trả số lớn để Tạp Hóa không chặn mua (vì bạn muốn mua trực tiếp)
    return jsonify({"sum": 9999})

@app.route("/fetch")
def fetch():
    key = request.args.get("key","").strip()
    order_id = request.args.get("order_id","").strip()
    qty_s = request.args.get("quantity","").strip()
    if not key or not qty_s:
        return jsonify({"status":"error","msg":"missing key/quantity"}), 400
    try:
        qty = int(qty_s); 
        if qty<=0 or qty>1000: raise ValueError()
    except Exception:
        return jsonify({"status":"error","msg":"invalid quantity"}), 400
    row = find_map_by_key(key)
    if not row:
        return jsonify({"status":"error","msg":"unknown key"}), 404

    try:
        res = mail72h_buy(int(row["product_id"]), qty)
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 502
        return jsonify({"status":"error","msg":f"mail72h http {code}"}), 502
    except Exception as e:
        return jsonify({"status":"error","msg":f"mail72h error: {e}"}), 502

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
