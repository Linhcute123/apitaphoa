import os, json, sqlite3
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

# =========================
# Runtime configuration
# =========================
DB = os.getenv("DB_PATH", "store.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "5"))

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
        )""")
        # Legacy migrations / safety
        _ensure_col(con, "keymaps", "group_name", "TEXT")
        _ensure_col(con, "keymaps", "provider_type", "TEXT NOT NULL DEFAULT 'mail72h'")
        _ensure_col(con, "keymaps", "base_url", "TEXT")
        try:
            con.execute("ALTER TABLE keymaps RENAME COLUMN provider_api_key TO api_key")
        except: pass
        try:
            con.execute("ALTER TABLE keymaps RENAME COLUMN mail72h_api_key TO api_key")
        except: pass
        _ensure_col(con, "keymaps", "api_key", "TEXT")

        # === NEW: Generic provider configuration (per key) ===
        _ensure_col(con, "keymaps", "stock_path", "TEXT")
        _ensure_col(con, "keymaps", "stock_method", "TEXT")
        _ensure_col(con, "keymaps", "stock_params", "TEXT")
        _ensure_col(con, "keymaps", "stock_pointer", "TEXT")
        _ensure_col(con, "keymaps", "fetch_path", "TEXT")
        _ensure_col(con, "keymaps", "fetch_method", "TEXT")
        _ensure_col(con, "keymaps", "fetch_params", "TEXT")
        _ensure_col(con, "keymaps", "fetch_pointer", "TEXT")

        con.commit()

init_db()

# =========================
# Utils
# =========================

def json_or_none(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None

def substitute_placeholders(mapping, **values):
    """
    Replace tokens like "{api_key}" with provided values (strings/ints).
    mapping may be dict[str, Any] or list of pairs; we return a flat dict of strings.
    """
    out = {}
    if isinstance(mapping, list):
        items = mapping
    elif isinstance(mapping, dict):
        items = mapping.items()
    else:
        return out
    for k, v in items:
        if isinstance(v, str):
            s = v
            for name, val in values.items():
                s = s.replace("{%s}" % name, str(val))
            out[k] = s
        else:
            out[k] = v
    return out

def json_pick(obj, pointer: str):
    """
    Minimal "dot path with fallbacks" reader.
    pointer examples:
      "sum" or "data.stock" or "data.items[0].qty"
      Use "||" to provide fallbacks: "data.stock||stock||amount"
    """
    if not pointer:
        return None
    for alt in pointer.split("||"):
        alt = alt.strip()
        try:
            cur = obj
            # split by '.' but keep [index] tokens
            parts = []
            buff = ""
            i = 0
            while i < len(alt):
                c = alt[i]
                if c == ".":
                    if buff:
                        parts.append(buff); buff = ""
                    i += 1; continue
                elif c == "[":
                    # push current key
                    if buff:
                        parts.append(buff); buff = ""
                    j = alt.find("]", i+1)
                    if j == -1:
                        raise ValueError("unclosed [")
                    idx = alt[i+1:j]
                    parts.append(f"[{idx}]")
                    i = j + 1
                    continue
                else:
                    buff += c
                    i += 1
            if buff:
                parts.append(buff)

            for p in parts:
                if not p:
                    continue
                if p.startswith("[") and p.endswith("]"):
                    idxs = p[1:-1]
                    # support negative / int
                    idx = int(idxs)
                    cur = cur[idx]
                else:
                    if isinstance(cur, dict):
                        cur = cur.get(p)
                    else:
                        cur = getattr(cur, p) if hasattr(cur, p) else None
                if cur is None:
                    break
            if cur is not None:
                return cur
        except Exception:
            continue
    return None

# ==========================================================
# === Provider: mail72h (kept for compatibility)          ===
# ==========================================================

def mail72h_buy(base_url: str, api_key: str, product_id: int, amount: int) -> dict:
    data = {"action": "buyProduct", "id": product_id, "amount": amount, "api_key": api_key}
    url = f"{base_url.rstrip('/')}/api/buy_product"
    r = requests.post(url, data=data, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()

def mail72h_product_list(base_url: str, api_key: str) -> dict:
    params = {"api_key": api_key}
    url = f"{base_url.rstrip('/')}/api/products.php"
    r = requests.get(url, params=params, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _collect_all_products(obj):
    all_products = []
    if not isinstance(obj, dict):
        return None
    categories = obj.get('categories')
    if not isinstance(categories, list):
        return None
    for category in categories:
        if isinstance(category, dict):
            plist = category.get('products')
            if isinstance(plist, list):
                all_products.extend(plist)
    return all_products or None

def stock_mail72h(row):
    try:
        base_url = row['base_url'] or 'https://mail72h.com'
        pid_to_find_str = str(row["product_id"])
        list_data = mail72h_product_list(base_url, row["api_key"])
        if list_data.get("status") != "success":
            return jsonify({"sum": 0}), 200
        products = _collect_all_products(list_data)
        if not products:
            return jsonify({"sum": 0}), 200
        stock_val = 0
        for item in products:
            if not isinstance(item, dict):
                continue
            item_id_raw = item.get("id")
            if item_id_raw is None:
                continue
            try:
                item_id_str_cleaned = str(int(float(str(item_id_raw).strip())))
            except (ValueError, TypeError):
                continue
            if item_id_str_cleaned == pid_to_find_str:
                stock_from_api = item.get("amount") or 0
                stock_val = int(str(stock_from_api).replace(".", ""))
                break
        return jsonify({"sum": stock_val}), 200
    except Exception:
        return jsonify({"sum": 0}), 200

def fetch_mail72h(row, qty):
    try:
        base_url = row['base_url'] or 'https://mail72h.com'
        res = mail72h_buy(base_url, row["api_key"], int(row["product_id"]), qty)
    except Exception:
        return jsonify([]), 200

    if res.get("status") != "success":
        return jsonify([]), 200

    data = res.get("data")
    out = []
    if isinstance(data, list):
        for it in data:
            out.append({"product": (json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it))})
    else:
        t = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        out = [{"product": t} for _ in range(qty)]
    return jsonify(out), 200

# ==========================================================
# === NEW: Generic REST provider (configurable per key)   ===
# ==========================================================

def make_request(method, url, *, params=None, data=None):
    method = (method or "GET").upper()
    if method == "POST":
        r = requests.post(url, data=data or params or {}, timeout=DEFAULT_TIMEOUT)
    elif method == "PUT":
        r = requests.put(url, data=data or params or {}, timeout=DEFAULT_TIMEOUT)
    else:
        r = requests.get(url, params=params or {}, timeout=DEFAULT_TIMEOUT)
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        # fallback: try text number
        txt = r.text.strip()
        if txt.isdigit():
            return {"sum": int(txt)}
        return {"raw": txt}

def generic_stock(row):
    base_url = (row["base_url"] or "").rstrip("/")
    path = (row["stock_path"] or "/api/stock").strip()
    method = (row["stock_method"] or "GET").strip().upper()
    params_tpl = json_or_none(row["stock_params"]) or {"api_key": "{api_key}", "product_id": "{product_id}"}
    pointer = (row["stock_pointer"] or "sum||stock||data.stock||data.amount||result.stock").strip()

    # build params
    params = substitute_placeholders(params_tpl,
        api_key=row["api_key"],
        product_id=row["product_id"],
        quantity=1
    )

    url = f"{base_url}{path if path.startswith('/') else '/' + path}"
    data = None
    send_params = params
    if method in ("POST","PUT"):
        data = params
        send_params = None

    try:
        res = make_request(method, url, params=send_params, data=data)
    except Exception as e:
        print(f"GENERIC_STOCK_ERROR: {e}")
        return jsonify({"sum": 0}), 200

    # extract number
    val = None
    if isinstance(res, dict):
        val = json_pick(res, pointer)
    if val is None and isinstance(res, (int, float, str)):
        val = res
    try:
        if isinstance(val, str):
            # remove dots or commas between digits
            v = val.replace(".", "").replace(",", "")
            val = int(v)
        elif isinstance(val, float):
            val = int(val)
        elif isinstance(val, int):
            pass
        else:
            val = 0
    except Exception:
        val = 0

    return jsonify({"sum": int(val)}), 200

def generic_fetch(row, qty):
    base_url = (row["base_url"] or "").rstrip("/")
    path = (row["fetch_path"] or "/api/buy").strip()
    method = (row["fetch_method"] or "POST").strip().upper()
    params_tpl = json_or_none(row["fetch_params"]) or {
        "api_key": "{api_key}",
        "product_id": "{product_id}",
        "quantity": "{quantity}"
    }
    pointer = (row["fetch_pointer"] or "data||items||result||data.list").strip()

    params = substitute_placeholders(params_tpl,
        api_key=row["api_key"],
        product_id=row["product_id"],
        quantity=qty
    )
    url = f"{base_url}{path if path.startswith('/') else '/' + path}"
    data = None
    send_params = params
    if method in ("POST","PUT"):
        data = params
        send_params = None

    try:
        res = make_request(method, url, params=send_params, data=data)
    except Exception as e:
        print(f"GENERIC_FETCH_ERROR: {e}")
        return jsonify([]), 200

    # convert to standardized out
    items = None
    if isinstance(res, dict):
        items = json_pick(res, pointer)
    if items is None:
        # try if entire response is list-like or scalar
        if isinstance(res, list):
            items = res
        else:
            items = [res]

    out = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, (dict, list)):
                out.append({"product": json.dumps(it, ensure_ascii=False)})
            else:
                out.append({"product": str(it)})
    else:
        # single value
        if isinstance(items, (dict, list)):
            t = json.dumps(items, ensure_ascii=False)
        else:
            t = str(items)
        out = [{"product": t} for _ in range(qty)]
    return jsonify(out), 200

# =========================
# Admin helpers
# =========================

ADMIN_TPL = """
<!doctype html>
<html><head><meta charset="utf-8" />
<title>Multi-Provider (Per-Key API)</title>
<style>
:root { --bd:#e5e7eb; --bg-light: #f9fafb; }
body{font-family:system-ui,Arial;padding:28px;color:#111;background:var(--bg-light);}
.card{border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:18px;background:#fff;}
.row{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;align-items:end}
.col-1{grid-column:span 1}.col-2{grid-column:span 2}.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-12{grid-column:span 12}
label{font-size:12px;text-transform:uppercase;color:#444}
input, textarea, select{width:100%;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;box-sizing:border-box;font-family:ui-monospace, Menlo, Consolas, monospace;}
input:disabled, input[readonly] { background: #f3f4f6; color: #555; cursor: not-allowed; }
table{width:100%;border-collapse:collapse}
th,td{padding:10px 12px;border-bottom:1px solid var(--bd);text-align:left;word-break:break-all;}
code{background:#f3f4f6;padding:2px 6px;border-radius:6px}
button,.btn{padding:10px 14px;border-radius:10px;border:1px solid #111;background:#111;color:#fff;cursor:pointer;text-decoration:none}
.btn.red{background:#b91c1c;border-color:#991b1b}
.btn.blue{background:#2563eb;border-color:#1d4ed8}
.btn.green{background:#16a34a;border-color:#15803d}
.btn.gray{background:#6b7280;border-color:#4b5563}
.btn.small{padding: 5px 10px; font-size: 12px;}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
details { border: 1px solid var(--bd); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
details summary { padding: 12px 16px; cursor: pointer; font-weight: 600; background: #fff; }
details[open] summary { border-bottom: 1px solid var(--bd); }
details .content { padding: 16px; background: var(--bg-light); }
details .content .btn { margin-top: 10px; }
details details { margin-top: 10px; }
details details summary { background: #f3f4f6; }
</style>
</head>
<body>
  <h2>⚙️ Multi-Provider (Quản lý theo Folder)</h2>
  
  <div class="card" id="add-key-form-card">
    <h3>Thêm/Update Key</h3>
    <form method="post" action="{{ url_for('admin_add_keymap') }}?admin_secret={{ asec }}" id="main-key-form">
      <div class="row" style="margin-bottom:12px">
        <div class="col-3">
          <label>Folder / Người dùng</label>
          <input class="mono" name="group_name" placeholder="vd: user_linh" required>
        </div>
        <div class="col-3">
          <label>Provider Type</label>
          <select name="provider_type">
            <option value="mail72h">mail72h</option>
            <option value="generic">generic</option>
          </select>
        </div>
        <div class="col-6">
          <label>Base URL (Web đấu API)</label>
          <input class="mono" name="base_url" placeholder="https://supplier.com">
        </div>
      </div>

      <div class="row">
         <div class="col-2"><label>SKU</label><input class="mono" name="sku" placeholder="edu24h" required></div>
         <div class="col-3"><label>input_key (Tạp Hóa)</label><input class="mono" name="input_key" placeholder="key-abc" required></div>
         <div class="col-2"><label>product_id (của NCC)</label><input class="mono" name="product_id" type="number" placeholder="28" required></div>
         <div class="col-3"><label>API key (của NCC)</label><input class="mono" name="api_key" type="password" required></div>
         <div class="col-1"><button type="submit">Lưu key</button></div>
         <div class="col-1"><button type="reset" class="btn gray" id="reset-form-btn">Xóa form</button></div>
      </div>

      <details>
        <summary>⚡ Cấu hình nâng cao cho <b>generic</b> (để dùng mọi web)</summary>
        <div class="content">
          <div class="row">
            <div class="col-3"><label>stock_path</label><input class="mono" name="stock_path" placeholder="/api/stock"></div>
            <div class="col-2"><label>stock_method</label><input class="mono" name="stock_method" placeholder="GET"></div>
            <div class="col-7"><label>stock_params (JSON, dùng {api_key},{product_id})</label>
              <input class="mono" name="stock_params" placeholder='{"api_key":"{api_key}","product_id":"{product_id}"}'>
            </div>
          </div>
          <div class="row">
            <div class="col-12"><label>stock_pointer (đọc số tồn kho, có thể dùng "||" fallback)</label>
              <input class="mono" name="stock_pointer" placeholder="sum||stock||data.stock||data.amount||result.stock">
            </div>
          </div>
          <hr/>
          <div class="row">
            <div class="col-3"><label>fetch_path</label><input class="mono" name="fetch_path" placeholder="/api/buy"></div>
            <div class="col-2"><label>fetch_method</label><input class="mono" name="fetch_method" placeholder="POST"></div>
            <div class="col-7"><label>fetch_params (JSON, dùng {api_key},{product_id},{quantity})</label>
              <input class="mono" name="fetch_params" placeholder='{"api_key":"{api_key}","product_id":"{product_id}","quantity":"{quantity}"}'>
            </div>
          </div>
          <div class="row">
            <div class="col-12"><label>fetch_pointer (đọc list sản phẩm trả về)</label>
              <input class="mono" name="fetch_pointer" placeholder="data||items||result||data.list">
            </div>
          </div>
        </div>
      </details>
    </form>
  </div>

  <div class="card">
    <h3>Danh sách Keys (Theo Folder)</h3>
    {% if not grouped_data %}
      <p>Chưa có key nào. Vui lòng thêm key bằng form bên trên.</p>
    {% endif %}
    
    {% for folder, providers in grouped_data.items() %}
      <details class="folder">
        <summary>📁 Folder: {{ folder }}</summary>
        <div class="content">
          {% for provider, data in providers.items() %}
            <details class="provider">
              <summary>📦 Provider: {{ provider }} ({{ data.key_list|length }} keys)</summary>
              <div class="content">
                <table>
                  <thead>
                    <tr>
                      <th>SKU</th>
                      <th>input_key</th>
                      <th>product_id</th>
                      <th>Active</th>
                      <th>Hành động</th>
                    </tr>
                  </thead>
                  <tbody>
                  {% for key in data.key_list %}
                    <tr>
                      <td>{{ key['sku'] }}</td>
                      <td><code>{{ key['input_key'] }}</code></td>
                      <td>{{ key['product_id'] }}</td>
                      <td>{{ '✅' if key['is_active'] else '❌' }}</td>
                      <td>
                        <form method="post" action="{{ url_for('admin_toggle_key', kmid=key['id']) }}?admin_secret={{ asec }}" style="display:inline">
                          <button class="btn blue small" type="submit">{{ 'Disable' if key['is_active'] else 'Enable' }}</button>
                        </form>
                        <form method="post" action="{{ url_for('admin_delete_key', kmid=key['id']) }}?admin_secret={{ asec }}" style="display:inline" onsubmit="return confirm('Xoá key {{key['input_key']}}?')">
                          <button class="btn red small" type="submit">Xoá</button>
                        </form>
                      </td>
                    </tr>
                  {% endfor %}
                  </tbody>
                </table>
                <button class="btn green small add-key-helper" 
                        data-folder="{{ folder }}" 
                        data-provider="{{ provider }}" 
                        data-baseurl="{{ data['base_url'] }}"
                        data-apikey="{{ data.key_list[0]['api_key'] if data.key_list else '' }}">
                  + Thêm Key vào đây
                </button>
              </div>
            </details>
          {% endfor %}
        </div>
      </details>
    {% endfor %}
  </div>

<script>
function setLockedFields(isLocked, folder = '', provider = '', baseurl = '', apikey = '') {
    const form = document.getElementById('main-key-form');
    const folderInput = form.querySelector('input[name="group_name"]');
    const providerInput = form.querySelector('select[name="provider_type"]');
    const baseurlInput = form.querySelector('input[name="base_url"]');
    const apikeyInput = form.querySelector('input[name="api_key"]');

    folderInput.readOnly = isLocked;
    providerInput.disabled = isLocked;
    baseurlInput.readOnly = isLocked;
    apikeyInput.readOnly = isLocked;

    if (isLocked) {
        folderInput.value = folder;
        if (provider) providerInput.value = provider;
        baseurlInput.value = baseurl;
        apikeyInput.value = apikey;
    } else {
        providerInput.disabled = false;
    }
}

document.addEventListener('click', function(e) {
  if (e.target.classList.contains('add-key-helper')) {
    e.preventDefault();
    const folder = e.target.dataset.folder;
    const provider = e.target.dataset.provider;
    const baseurl = e.target.dataset.baseurl;
    const apikey = e.target.dataset.apikey; 
    
    setLockedFields(true, folder, provider, baseurl, apikey);
    
    const formCard = document.getElementById('add-key-form-card');
    formCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    formCard.querySelector('input[name="sku"]').focus();
  }
});

document.getElementById('reset-form-btn').addEventListener('click', function() {
    setLockedFields(false);
});
</script>
</body></html>
"""

def find_map_by_key(key: str):
    with db() as con:
        row = con.execute("SELECT * FROM keymaps WHERE input_key=? AND is_active=1", (key,)).fetchone()
        return row

def require_admin():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        abort(403)

@app.route("/admin")
def admin_index():
    require_admin()
    with db() as con:
        maps = con.execute("SELECT * FROM keymaps ORDER BY group_name, provider_type, sku, id").fetchall()
    
    grouped_data = {}
    for key in maps:
        folder = key['group_name'] or 'DEFAULT'
        provider = key['provider_type']
        
        if folder not in grouped_data:
            grouped_data[folder] = {}
        
        if provider not in grouped_data[folder]:
            grouped_data[folder][provider] = {"key_list": [], "base_url": key['base_url']}
        
        grouped_data[folder][provider]["key_list"].append(key)

    return render_template_string(ADMIN_TPL, grouped_data=grouped_data, asec=ADMIN_SECRET)

@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    f = request.form
    
    group_name = f.get("group_name","").strip() or 'DEFAULT'
    sku = f.get("sku","").strip()
    input_key = f.get("input_key","").strip()
    product_id = f.get("product_id","").strip()
    
    provider_type = f.get("provider_type","").strip().lower() or 'mail72h'
    base_url = f.get("base_url","").strip()
    api_key = f.get("api_key","").strip()

    # advanced (optional; only used by provider_type='generic')
    stock_path = f.get("stock_path","").strip() or None
    stock_method = f.get("stock_method","").strip() or None
    stock_params = f.get("stock_params","").strip() or None
    stock_pointer = f.get("stock_pointer","").strip() or None
    fetch_path = f.get("fetch_path","").strip() or None
    fetch_method = f.get("fetch_method","").strip() or None
    fetch_params = f.get("fetch_params","").strip() or None
    fetch_pointer = f.get("fetch_pointer","").strip() or None

    if not sku or not input_key or not product_id.isdigit() or not api_key:
        return "Thiếu thông tin quan trọng (sku, input_key, product_id, api_key)", 400
    
    if not base_url and provider_type == 'mail72h':
        base_url = 'https://mail72h.com'
    
    with db() as con:
        con.execute("""
            INSERT INTO keymaps(group_name, sku, input_key, product_id, api_key, is_active, provider_type, base_url,
                                stock_path, stock_method, stock_params, stock_pointer,
                                fetch_path, fetch_method, fetch_params, fetch_pointer)
            VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(input_key) DO UPDATE SET
              group_name=excluded.group_name,
              sku=excluded.sku,
              product_id=excluded.product_id,
              api_key=excluded.api_key,
              is_active=1,
              provider_type=excluded.provider_type,
              base_url=excluded.base_url,
              stock_path=excluded.stock_path,
              stock_method=excluded.stock_method,
              stock_params=excluded.stock_params,
              stock_pointer=excluded.stock_pointer,
              fetch_path=excluded.fetch_path,
              fetch_method=excluded.fetch_method,
              fetch_params=excluded.fetch_params,
              fetch_pointer=excluded.fetch_pointer
        """, (group_name, sku, input_key, int(product_id), api_key,
              provider_type, base_url,
              stock_path, stock_method, stock_params, stock_pointer,
              fetch_path, fetch_method, fetch_params, fetch_pointer))
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

# ========= Public endpoints =========
@app.route("/stock")
def stock():
    key = request.args.get("key","").strip()
    if not key:
        return jsonify({"sum": 0}), 200
        
    row = find_map_by_key(key)
    if not row:
        return jsonify({"sum": 0}), 200

    provider = row['provider_type']
    if provider == 'mail72h':
        return stock_mail72h(row)
    elif provider == 'generic':
        return generic_stock(row)
    else:
        # fallback attempt: try generic if config provided
        return generic_stock(row)

@app.route("/fetch")
def fetch():
    key = request.args.get("key","").strip()
    qty_s = request.args.get("quantity","").strip()
    
    if not key or not qty_s:
        return jsonify([]), 200
    try:
        qty = int(qty_s); 
        if qty<=0 or qty>1000: raise ValueError()
    except Exception:
        return jsonify([]), 200

    row = find_map_by_key(key)
    if not row:
        return jsonify([]), 200
    
    provider = row['provider_type']
    if provider == 'mail72h':
        return fetch_mail72h(row, qty)
    elif provider == 'generic':
        return generic_fetch(row, qty)
    else:
        return generic_fetch(row, qty)

@app.route("/")
def health():
    return "OK", 200

@app.route("/debugraw")
def debug_raw():
    """
    Admin-only. Call stock/fetch endpoint for a key and dump raw JSON for debugging.
    Example:
      /debugraw?key=<input_key>&type=stock
      /debugraw?key=<input_key>&type=fetch&quantity=2
    """
    require_admin()
    key = request.args.get("key","").strip()
    type_ = request.args.get("type","stock").strip()
    qty_s = request.args.get("quantity","1").strip()
    try:
        qty = int(qty_s)
    except:
        qty = 1
    row = find_map_by_key(key)
    if not row:
        return jsonify({"error":"unknown key"}), 404
    if type_ == "fetch":
        base_url = (row["base_url"] or "").rstrip("/")
        path = (row["fetch_path"] or "/api/buy").strip()
        method = (row["fetch_method"] or "POST").strip().upper()
        params_tpl = json_or_none(row["fetch_params"]) or {
            "api_key": "{api_key}",
            "product_id": "{product_id}",
            "quantity": "{quantity}"
        }
        params = substitute_placeholders(params_tpl,
            api_key=row["api_key"],
            product_id=row["product_id"],
            quantity=qty
        )
    else:
        base_url = (row["base_url"] or "").rstrip("/")
        path = (row["stock_path"] or "/api/stock").strip()
        method = (row["stock_method"] or "GET").strip().upper()
        params_tpl = json_or_none(row["stock_params"]) or {"api_key":"{api_key}","product_id":"{product_id}"}
        params = substitute_placeholders(params_tpl,
            api_key=row["api_key"],
            product_id=row["product_id"],
            quantity=1
        )
    url = f"{base_url}{path if path.startswith('/') else '/' + path}"
    data = None
    send_params = params
    if method in ("POST","PUT"):
        data = params
        send_params = None
    try:
        res = make_request(method, url, params=send_params, data=data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"url": url, "method": method, "params": params, "raw": res})

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
