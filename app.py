
import os, json, sqlite3, threading
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

DB = os.getenv("DB_PATH", "store.db")
MAIL72H_BASE = os.getenv("MAIL72H_BASE", "https://mail72h.com/api")
MAIL72H_API_KEY = os.getenv("MAIL72H_API_KEY", "REPLACE_ME")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
TIMEOUT = int(os.getenv("TIMEOUT", "25"))

app = Flask(__name__)
lock = threading.Lock()

def db_connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    with db_connect() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS products(
            sku TEXT PRIMARY KEY,
            input_key TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            note TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        con.commit()

db_init()

def mail72h_product_detail(product_id: int) -> dict:
    r = requests.get(f"{MAIL72H_BASE}/product.php",
                     params={"api_key": MAIL72H_API_KEY, "id": product_id},
                     timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def mail72h_buy(product_id: int, amount: int, coupon: str|None=None) -> dict:
    data = {
        "action": "buyProduct",
        "id": product_id,
        "amount": amount,
        "api_key": MAIL72H_API_KEY
    }
    if coupon:
        data["coupon"] = coupon
    r = requests.post(f"{MAIL72H_BASE}/buy_product", data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

@app.route("/stock")
def stock():
    sku = request.args.get("sku", "").strip()
    key = request.args.get("key", "").strip()
    if not sku or not key:
        return jsonify({"status":"error","msg":"missing sku/key"}), 400

    with closing(db_connect()) as con:
        row = con.execute("SELECT * FROM products WHERE sku=? AND is_active=1", (sku,)).fetchone()
        if not row:
            return jsonify({"status":"error","msg":"unknown sku"}), 404
        if key != row["input_key"]:
            return jsonify({"status":"error","msg":"bad key"}), 403

    try:
        pd = mail72h_product_detail(int(row["product_id"]))
        stock_left = int(pd.get("data", {}).get("stock", 9999))
    except Exception:
        stock_left = 9999

    return jsonify({"sum": stock_left})

@app.route("/fetch")
def fetch():
    sku = request.args.get("sku", "").strip()
    key = request.args.get("key", "").strip()
    order_id = request.args.get("order_id", "").strip()
    qty_s = request.args.get("quantity", "").strip()

    if not sku or not key or not qty_s:
        return jsonify({"status":"error","msg":"missing params"}), 400
    try:
        qty = int(qty_s)
        if qty <= 0 or qty > 1000:
            raise ValueError()
    except Exception:
        return jsonify({"status":"error","msg":"invalid quantity"}), 400

    with closing(db_connect()) as con:
        row = con.execute("SELECT * FROM products WHERE sku=? AND is_active=1", (sku,)).fetchone()
        if not row:
            return jsonify({"status":"error","msg":"unknown sku"}), 404
        if key != row["input_key"]:
            return jsonify({"status":"error","msg":"bad key"}), 403
        product_id = int(row["product_id"])

    try:
        res = mail72h_buy(product_id, qty)
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
        for item in data:
            text = json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else str(item)
            out.append({"product": text})
    else:
        text = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        out = [{"product": text} for _ in range(qty)]

    return jsonify(out)

@app.route("/admin/products")
def admin_products():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        return ("Forbidden", 403)
    with closing(db_connect()) as con:
        rows = con.execute("SELECT * FROM products ORDER BY sku").fetchall()
    return render_template_string(ADMIN_TPL, rows=rows, admin_secret=ADMIN_SECRET)

@app.route("/admin/products", methods=["POST"])
def admin_add_product():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        return ("Forbidden", 403)
    sku = request.form.get("sku","").strip()
    input_key = request.form.get("input_key","").strip()
    product_id = request.form.get("product_id","").strip()
    note = request.form.get("note","").strip()
    if not sku or not input_key or not product_id.isdigit():
        return ("Bad Request", 400)
    with closing(db_connect()) as con:
        con.execute("""
            INSERT INTO products(sku, input_key, product_id, note, is_active)
            VALUES(?,?,?,?,1)
            ON CONFLICT(sku) DO UPDATE SET
              input_key=excluded.input_key,
              product_id=excluded.product_id,
              note=excluded.note
        """, (sku, input_key, int(product_id), note))
        con.commit()
    return redirect(url_for("admin_products", admin_secret=ADMIN_SECRET))

@app.route("/admin/products/<sku>/toggle", methods=["POST"])
def admin_toggle_active(sku):
    if request.args.get("admin_secret") != ADMIN_SECRET:
        return ("Forbidden", 403)
    with closing(db_connect()) as con:
        row = con.execute("SELECT is_active FROM products WHERE sku=?", (sku,)).fetchone()
        if not row:
            return ("Not Found", 404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE products SET is_active=? WHERE sku=?", (newv, sku))
        con.commit()
    return redirect(url_for("admin_products", admin_secret=ADMIN_SECRET))

@app.route("/admin/products/<sku>", methods=["POST"])
def admin_delete_product(sku):
    if request.args.get("admin_secret") != ADMIN_SECRET:
        return ("Forbidden", 403)
    with closing(db_connect()) as con:
        con.execute("DELETE FROM products WHERE sku=?", (sku,))
        con.commit()
    return redirect(url_for("admin_products", admin_secret=ADMIN_SECRET))

@app.route("/")
def health():
    return "OK", 200

ADMIN_TPL = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Quản lý sản phẩm</title>
  <style>
    body { font-family: system-ui, Arial, sans-serif; padding: 24px; }
    table { border-collapse: collapse; width: 100%; margin-top: 16px; }
    th, td { border:1px solid #ddd; padding:8px; }
    th { background:#f5f5f5; text-align:left; }
    input[type=text], input[type=number] { width: 100%; padding:6px; }
    .row { display:flex; gap:12px; }
    .card { border:1px solid #ddd; padding:16px; border-radius:8px; }
    .btn { padding:8px 12px; border:1px solid #333; background:#fff; cursor:pointer; }
    .btn.primary { background:#111; color:#fff; }
  </style>
</head>
<body>
  <h2>Quản lý sản phẩm (per-product input_key & product_id)</h2>
  <div class="card">
    <form method="post" action="{{ url_for('admin_add_product') }}?admin_secret={{ admin_secret }}">
      <div class="row">
        <div style="flex:1">
          <label>SKU (duy nhất)</label>
          <input type="text" name="sku" required placeholder="vd: edu24h">
        </div>
        <div style="flex:1">
          <label>input_key (cho Tạp Hóa gọi)</label>
          <input type="text" name="input_key" required placeholder="key-rieng-cho-sku-nay">
        </div>
        <div style="flex:1">
          <label>product_id (mail72h)</label>
          <input type="number" name="product_id" required placeholder="12345">
        </div>
      </div>
      <div style="margin-top:8px;">
        <label>Ghi chú</label>
        <input type="text" name="note" placeholder="tuỳ chọn">
      </div>
      <div style="margin-top:12px;">
        <button class="btn primary" type="submit">Thêm / Cập nhật</button>
      </div>
    </form>
  </div>

  <table>
    <thead>
      <tr>
        <th>SKU</th><th>input_key</th><th>product_id</th><th>note</th><th>active</th><th>Hành động</th>
      </tr>
    </thead>
    <tbody>
    {% for r in rows %}
      <tr>
        <td>{{ r['sku'] }}</td>
        <td><code>{{ r['input_key'] }}</code></td>
        <td>{{ r['product_id'] }}</td>
        <td>{{ r['note'] or '' }}</td>
        <td>{{ r['is_active'] }}</td>
        <td>
          <form method="post" action="{{ url_for('admin_toggle_active', sku=r['sku']) }}?admin_secret={{ admin_secret }}" style="display:inline">
            <button class="btn" type="submit">{{ 'Disable' if r['is_active'] else 'Enable' }}</button>
          </form>
          <form method="post" action="{{ url_for('admin_delete_product', sku=r['sku']) }}?admin_secret={{ admin_secret }}" style="display:inline" onsubmit="return confirm('Xoá {{r['sku']}}?')">
            <button class="btn" type="submit">Xoá</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 style="margin-top:24px;">Cách cấu hình bên Tạp Hóa</h3>
  <pre>
API tồn kho:
  GET https://YOUR-DOMAIN/stock?sku=&lt;sku&gt;&key=&lt;input_key&gt;

API lấy hàng:
  GET https://YOUR-DOMAIN/fetch?sku=&lt;sku&gt;&order_id={order_id}&quantity={quantity}&key=&lt;input_key&gt;
  </pre>
</body>
</html>
"""
if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
