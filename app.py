
import os, json, sqlite3, threading, time
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

# ====== Config ======
DB = os.getenv("DB_PATH", "store.db")
MAIL72H_BASE = os.getenv("MAIL72H_BASE", "https://mail72h.com/api")
MAIL72H_API_KEY = os.getenv("MAIL72H_API_KEY", "REPLACE_ME")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
MAIL72H_TIMEOUT = int(os.getenv("MAIL72H_TIMEOUT", "3"))
LOW_WATERMARK = int(os.getenv("LOW_WATERMARK", "20"))
TOPUP_BATCH = int(os.getenv("TOPUP_BATCH", "50"))

app = Flask(__name__)

# ====== DB ======
def db_connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def db_init():
    with db_connect() as con:
        # SKU master (tuỳ chọn dùng để nhóm)
        con.execute("""
        CREATE TABLE IF NOT EXISTS products(
            sku TEXT PRIMARY KEY,
            note TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        # Bảng key-map: mỗi input_key của Tạp Hóa map đến product_id của mail72h (và thuộc về 1 sku)
        con.execute("""
        CREATE TABLE IF NOT EXISTS keymaps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            input_key TEXT NOT NULL UNIQUE,
            product_id INTEGER NOT NULL,
            note TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        # Buffer theo sku (hoặc theo key). Ta buffer theo sku để gom nhóm.
        con.execute("""
        CREATE TABLE IF NOT EXISTS items(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sku TEXT NOT NULL,
            data TEXT NOT NULL,
            used INTEGER DEFAULT 0,
            order_id TEXT,
            created_at TIMESTAMP DEFAULT (strftime('%Y-%m-%d %H:%M:%f','now'))
        )""")
        con.commit()

db_init()

# ====== Helpers ======
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

def _find_map(sku: str|None, key: str|None):
    """Trả về (sku, product_id) từ sku+key hoặc chỉ key."""
    with closing(db_connect()) as con:
        if key:
            m = con.execute("SELECT sku, product_id, is_active FROM keymaps WHERE input_key=?", (key,)).fetchone()
            if m and m["is_active"]:
                return m["sku"], int(m["product_id"])
        if sku:
            # Lấy map đầu tiên của sku đang active (nếu key không truyền)
            m = con.execute("SELECT sku, product_id FROM keymaps WHERE sku=? AND is_active=1 ORDER BY id LIMIT 1", (sku,)).fetchone()
            if m:
                return m["sku"], int(m["product_id"])
    return None, None

# ====== Admin UI ======
ADMIN_TPL = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Quản lý SKU & nhiều input_key</title>
  <style>
    body { font-family: system-ui, Arial, sans-serif; padding: 24px; }
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
  <h2>Quản lý SKU & nhiều input_key (mỗi key ↔ product_id)</h2>

  <div class="card">
    <h3>Thêm/Cập nhật SKU</h3>
    <form method="post" action="{{ url_for('admin_add_sku') }}?admin_secret={{ admin_secret }}">
      <div class="row">
        <div style="flex:1 1 220px">
          <label>SKU</label>
          <input type="text" name="sku" required placeholder="vd: edu24h">
        </div>
        <div style="flex:3 1 420px">
          <label>Ghi chú</label>
          <input type="text" name="note" placeholder="tuỳ chọn">
        </div>
      </div>
      <button class="btn primary" type="submit">Thêm / Cập nhật</button>
    </form>
  </div>

  <div class="card">
    <h3>Thêm/Cập nhật input_key cho SKU</h3>
    <form method="post" action="{{ url_for('admin_add_keymap') }}?admin_secret={{ admin_secret }}">
      <div class="row">
        <div style="flex:1 1 160px">
          <label>SKU</label>
          <input type="text" name="sku" required placeholder="vd: edu24h">
        </div>
        <div style="flex:2 1 280px">
          <label>input_key (từ Tạp Hóa)</label>
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
      <button class="btn primary" type="submit">Thêm / Cập nhật key</button>
    </form>
  </div>

  <div class="card">
    <h3>Nạp buffer (mua sẵn từ mail72h)</h3>
    <form method="post" action="{{ url_for('admin_buy_buffer') }}?admin_secret={{ admin_secret }}">
      <div class="row">
        <div style="flex:1 1 160px">
          <label>SKU</label>
          <input type="text" name="sku" required>
        </div>
        <div style="flex:1 1 160px">
          <label>Số lượng</label>
          <input type="number" name="amount" value="50" min="1" required>
        </div>
      </div>
      <button class="btn primary" type="submit">Nạp buffer</button>
    </form>
    <p>Buffer theo SKU. Bạn có thể nạp từ bất kỳ key/product_id nào của SKU đó.</p>
  </div>

  <h3>Danh sách SKU</h3>
  <table>
    <thead><tr><th>SKU</th><th>Ghi chú</th><th>Active</th><th>Buffer còn</th><th>Hành động</th></tr></thead>
    <tbody>
    {% for r in skus %}
      <tr>
        <td>{{ r['sku'] }}</td>
        <td>{{ r['note'] or '' }}</td>
        <td>{{ r['is_active'] }}</td>
        <td>{{ r['buffer_left'] }}</td>
        <td>
          <form method="post" action="{{ url_for('admin_toggle_sku', sku=r['sku']) }}?admin_secret={{ admin_secret }}" style="display:inline">
            <button class="btn" type="submit">{{ 'Disable' if r['is_active'] else 'Enable' }}</button>
          </form>
          <form method="post" action="{{ url_for('admin_delete_sku', sku=r['sku']) }}?admin_secret={{ admin_secret }}" style="display:inline" onsubmit="return confirm('Xoá SKU {{r['sku']}}?')">
            <button class="btn" type="submit">Xoá</button>
          </form>
        </td>
      </tr>
    {% endfor %}
    </tbody>
  </table>

  <h3 style="margin-top:24px;">Danh sách key ↔ product_id</h3>
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

  <h3 style="margin-top:24px;">Endpoint cho Tạp Hóa</h3>
  <pre>
Tồn kho:
  GET /stock?key=<input_key>            (khuyến nghị)
  hoặc /stock?sku=<sku>&key=<input_key> (tương thích cũ)

Lấy hàng:
  GET /fetch?key=<input_key>&order_id={order_id}&quantity={quantity}
  (cũng hỗ trợ /fetch?sku=<sku>&key=...&... nếu bạn vẫn muốn truyền sku)
  </pre>
</body>
</html>
"""

def require_admin():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        abort(403)

@app.route("/admin")
@app.route("/admin/keys")
def admin_index():
    require_admin()
    with closing(db_connect()) as con:
        skus = con.execute("SELECT * FROM products ORDER BY sku").fetchall()
        out_skus = []
        for r in skus:
            c = con.execute("SELECT COUNT(*) AS c FROM items WHERE sku=? AND used=0", (r["sku"],)).fetchone()["c"]
            d = dict(r)
            d["buffer_left"] = c
            out_skus.append(d)
        maps = con.execute("SELECT * FROM keymaps ORDER BY sku, id").fetchall()
    return render_template_string(ADMIN_TPL, admin_secret=ADMIN_SECRET, skus=out_skus, maps=maps)

@app.route("/admin/sku", methods=["POST"])
def admin_add_sku():
    require_admin()
    sku = request.form.get("sku","").strip()
    note = request.form.get("note","").strip()
    if not sku: abort(400)
    with closing(db_connect()) as con:
        con.execute("""
            INSERT INTO products(sku, note, is_active) VALUES(?, ?, 1)
            ON CONFLICT(sku) DO UPDATE SET note=excluded.note
        """, (sku, note))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/sku/<sku>/toggle", methods=["POST"])
def admin_toggle_sku(sku):
    require_admin()
    with closing(db_connect()) as con:
        row = con.execute("SELECT is_active FROM products WHERE sku=?", (sku,)).fetchone()
        if not row: abort(404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE products SET is_active=? WHERE sku=?", (newv, sku))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/sku/<sku>", methods=["POST"])
def admin_delete_sku(sku):
    require_admin()
    with closing(db_connect()) as con:
        con.execute("DELETE FROM products WHERE sku=?", (sku,))
        con.execute("DELETE FROM keymaps WHERE sku=?", (sku,))
        con.execute("DELETE FROM items WHERE sku=?", (sku,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    sku = request.form.get("sku","").strip()
    input_key = request.form.get("input_key","").strip()
    product_id = request.form.get("product_id","").strip()
    note = request.form.get("note","").strip()
    if not sku or not input_key or not product_id.isdigit():
        abort(400)
    with closing(db_connect()) as con:
        # Ensure sku exists
        con.execute("INSERT OR IGNORE INTO products(sku, is_active) VALUES(?, 1)", (sku,))
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
    with closing(db_connect()) as con:
        row = con.execute("SELECT is_active FROM keymaps WHERE id=?", (kmid,)).fetchone()
        if not row: abort(404)
        newv = 0 if row["is_active"] else 1
        con.execute("UPDATE keymaps SET is_active=? WHERE id=?", (newv, kmid))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/keymap/<int:kmid>", methods=["POST"])
def admin_delete_key(kmid):
    require_admin()
    with closing(db_connect()) as con:
        con.execute("DELETE FROM keymaps WHERE id=?", (kmid,))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

@app.route("/admin/buffer/buy", methods=["POST"])
def admin_buy_buffer():
    require_admin()
    sku = request.form.get("sku","").strip()
    amount = int(request.form.get("amount","0"))
    if not sku or amount <= 0: abort(400)
    # Lấy product_id từ keymaps đầu tiên đang active của SKU
    with closing(db_connect()) as con:
        m = con.execute("SELECT product_id FROM keymaps WHERE sku=? AND is_active=1 ORDER BY id LIMIT 1", (sku,)).fetchone()
        if not m: return ("Chưa có key/product_id cho SKU này", 400)
        pid = int(m["product_id"])
    res = mail72h_buy(pid, amount)
    if res.get("status") != "success":
        return (f"mail72h trả lỗi: {json.dumps(res, ensure_ascii=False)}", 400)
    data = res.get("data")
    if isinstance(data, list):
        vals = [json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it) for it in data]
    else:
        t = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        vals = [t]*amount
    with closing(db_connect()) as con:
        for v in vals:
            con.execute("INSERT INTO items(sku, data) VALUES(?,?)", (sku, v))
        con.commit()
    return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))

# ====== Public endpoints ======
@app.route("/stock")
def stock():
    sku = request.args.get("sku","").strip() or None
    key = request.args.get("key","").strip() or None
    if not key and not sku:
        return jsonify({"status":"error","msg":"missing key or sku"}), 400
    sku2, pid = _find_map(sku, key)
    if not sku2 or not pid:
        return jsonify({"status":"error","msg":"unknown key/sku"}), 404
    with closing(db_connect()) as con:
        c = con.execute("SELECT COUNT(*) AS c FROM items WHERE sku=? AND used=0", (sku2,)).fetchone()["c"]
    return jsonify({"sum": int(c)})

@app.route("/fetch")
def fetch():
    t0 = time.time()
    sku = request.args.get("sku","").strip() or None
    key = request.args.get("key","").strip() or None
    order_id = request.args.get("order_id","").strip()
    qty_s = request.args.get("quantity","").strip()
    if not key and not sku:
        return jsonify({"status":"error","msg":"missing key or sku"}), 400
    if not qty_s: return jsonify({"status":"error","msg":"missing quantity"}), 400
    try:
        qty = int(qty_s)
        if qty <= 0 or qty > 1000: raise ValueError()
    except Exception:
        return jsonify({"status":"error","msg":"invalid quantity"}), 400

    sku2, pid = _find_map(sku, key)
    if not sku2 or not pid:
        return jsonify({"status":"error","msg":"unknown key/sku"}), 404

    with closing(db_connect()) as con:
        rows = con.execute("SELECT id, data FROM items WHERE sku=? AND used=0 LIMIT ?", (sku2, qty)).fetchall()
        got = len(rows)
        out = [{"product": r["data"]} for r in rows]
        if got >= qty:
            ids = [r["id"] for r in rows]
            con.execute(f"UPDATE items SET used=1, order_id=? WHERE id IN ({','.join('?'*len(ids))})",
                        (order_id, *ids))
            con.commit()
            return jsonify(out)

    # Thiếu -> gọi mail72h nhanh (timeout ngắn) để bổ sung phần còn thiếu
    need = qty - got
    try:
        res = mail72h_buy(pid, need)
        if res.get("status") != "success":
            # Không đủ hàng/tiền -> trả phần lấy từ buffer (nếu có) + báo lỗi 409
            return jsonify({"status":"error","msg":res, "partial": out}), 409
        data = res.get("data")
        if isinstance(data, list):
            out += [{"product": (json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it))} for it in data]
        else:
            t = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
            out += [{"product": t} for _ in range(need)]
    except Exception as e:
        # Lỗi (timeout, network ...) -> trả nhanh lỗi; nếu đã có phần buffer, kèm partial
        return jsonify({"status":"error","msg":f"mail72h error: {e}", "partial": out}), 502

    # Mark những item đã dùng trong buffer
    if got > 0:
        with closing(db_connect()) as con:
            ids = [r["id"] for r in rows]
            con.execute(f"UPDATE items SET used=1, order_id=? WHERE id IN ({','.join('?'*len(ids))})",
                        (order_id, *ids))
            con.commit()

    # Background top-up nếu buffer thấp
    def _topup_bg(sku_local, pid_local):
        try:
            with closing(db_connect()) as con2:
                left = con2.execute("SELECT COUNT(*) AS c FROM items WHERE sku=? AND used=0", (sku_local,)).fetchone()["c"]
            if left >= LOW_WATERMARK: 
                return
            res2 = mail72h_buy(pid_local, TOPUP_BATCH)
            if res2.get("status") != "success": 
                return
            items = res2.get("data")
            if isinstance(items, list):
                vals = [json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it) for it in items]
            else:
                t = json.dumps(items, ensure_ascii=False) if isinstance(items, dict) else str(items)
                vals = [t]*TOPUP_BATCH
            with closing(db_connect()) as con2:
                for v in vals:
                    con2.execute("INSERT INTO items(sku, data) VALUES(?,?)", (sku_local, v))
                con2.commit()
        except Exception:
            pass

    threading.Thread(target=_topup_bg, args=(sku2, pid), daemon=True).start()
    return jsonify(out)

@app.route("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
