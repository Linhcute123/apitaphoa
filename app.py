import os, json, sqlite3
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests
import datetime # === MỚI: Thêm import này ===

DB = os.getenv("DB_PATH", "store.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
DEFAULT_TIMEOUT = int(os.getenv("DEFAULT_TIMEOUT", "3"))

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
            base_url TEXT
        )""")
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
        try:
            con.execute("ALTER TABLE keymaps DROP COLUMN note")
        except:
            pass 
        con.commit()

init_db()

# ==========================================================
# === SỬA LỖI 6: Thu thập TẤT CẢ sản phẩm từ TẤT CẢ danh mục ===
# ==========================================================
def _collect_all_products(obj):
    """
    Thu thập TẤT CẢ các sản phẩm từ TẤT CẢ các danh mục.
    Cấu trúc API là: {'categories': [{'products': [...]}, ...]}
    """
    all_products = []
    if not isinstance(obj, dict):
        print(f"DEBUG: API response is not a dict: {str(obj)[:200]}")
        return None

    categories = obj.get('categories')
    if not isinstance(categories, list):
        print(f"DEBUG: 'categories' key not found or is not a list in API response.")
        return None # Không tìm thấy list 'categories'

    for category in categories:
        if isinstance(category, dict):
            products_in_category = category.get('products')
            if isinstance(products_in_category, list):
                all_products.extend(products_in_category) # Thêm tất cả sản phẩm vào list chung
    
    if not all_products: # Nếu không tìm thấy gì
        print(f"DEBUG: Found 'categories' list, but no 'products' lists were found inside them.")
        return None
        
    return all_products
# ==========================================================
# === KẾT THÚC SỬA LỖI ===
# ==========================================================


# ========= Helpers cho Provider 'mail72h' =========

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


def stock_mail72h(row):
    try:
        base_url = row['base_url'] or 'https://mail72h.com'
        # Đây là ID từ CSDL của bạn (ví dụ: "28")
        pid_to_find_str = str(row["product_id"])
        
        list_data = mail72h_product_list(base_url, row["api_key"])
        
        if list_data.get("status") != "success":
            print(f"STOCK_ERROR (API List): {list_data.get('message', 'unknown')}")
            return jsonify({"sum": 0}), 200

        # SỬA LỖI 6: Dùng hàm _collect_all_products
        products = _collect_all_products(list_data)

        if not products:
             # Ghi log chi tiết hơn
             print(f"STOCK_ERROR: Could not find 'categories' or 'products' list inside /products.php response. Raw data: {str(list_data)[:500]}")
             return jsonify({"sum": 0}), 200

        stock_val = 0
        found = False
        for item in products:
            if not isinstance(item, dict):
                continue
            
            item_id_raw = item.get("id")
            if item_id_raw is None:
                continue
            
            # === SỬA LỖI 3: XỬ LÝ ID LÀ SỐ THỰC (FLOAT) "28.0" ===
            try:
                item_id_str_cleaned = str(int(float(str(item_id_raw).strip())))
            except (ValueError, TypeError):
                print(f"STOCK_DEBUG: Skipping unparseable product ID: {item_id_raw}")
                continue
            
            if item_id_str_cleaned == pid_to_find_str:
                
                # ==========================================================
                # === SỬA LỖI 6: Đọc 'amount' thay vì 'stock' ===
                # ==========================================================
                stock_from_api = item.get("amount") 
                if not stock_from_api: # Xử lý None, "", 0
                    stock_from_api = 0
                
                stock_val = int(str(stock_from_api).replace(".", ""))
                # ==========================================================
                
                found = True
                break
        
        if not found:
            print(f"STOCK_ERROR: Product ID {pid_to_find_str} not found in *any* category. (Collected {len(products)} products, but ID mismatch. Check your admin config.)")
            return jsonify({"sum": 0}), 200 
        
        return jsonify({"sum": stock_val})

    except requests.HTTPError as e:
        err_msg = f"mail72h http error {e.response.status_code}"
        try:
            err_detail = e.response.json().get('message', e.response.text)
            err_msg = f"mail72h error: {err_detail}"
        except:
            err_msg = f"mail72h http error {e.response.status_code}: {e.response.text}"
        print(f"STOCK_ERROR (HTTP): {err_msg}")
        return jsonify({"sum": 0}), 200
    
    except Exception as e:
        print(f"STOCK_ERROR (Processing/Other): {e}")
        return jsonify({"sum": 0}), 200

def fetch_mail72h(row, qty):
    try:
        base_url = row['base_url'] or 'https://mail72h.com'
        res = mail72h_buy(base_url, row["api_key"], int(row["product_id"]), qty)
    
    except requests.HTTPError as e:
        err_msg = f"mail72h http error {e.response.status_code}"
        try:
            err_detail = e.response.json().get('message', e.response.text)
            err_msg = f"mail72h error: {err_detail}"
        except:
            err_msg = f"mail72h http error {e.response.status_code}: {e.response.text}"
        print(f"FETCH_ERROR (HTTP): {err_msg}")
        return jsonify([]), 200

    except Exception as e:
        print(f"FETCH_ERROR (Connect): {e}")
        return jsonify([]), 200

    if res.get("status") != "success":
        print(f"FETCH_ERROR (API): {res.get('message', 'mail72h buy failed')}")
        return jsonify([]), 200

    data = res.get("data")
    out = []
    if isinstance(data, list):
        for it in data:
            out.append({"product": (json.dumps(it, ensure_ascii=False) if isinstance(it, dict) else str(it))})
    else:
        t = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        out = [{"product": t} for _ in range(qty)]
    
    return jsonify(out)


# ========= Admin UI (Folder lồng nhau) =========
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
input{width:100%;padding:10px 12px;border:1px solid var(--bd);border-radius:10px;box-sizing:border-box;}
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
  
  <div class="card">
    <h3>Backup & Đồng bộ</h3>
    <div class="row">
      <div class="col-6">
        <h4>Tải Backup</h4>
        <p>Tải xuống toàn bộ cấu hình keys (bảng keymaps) dưới dạng file JSON.</p>
        <a href="{{ url_for('admin_backup_download') }}?admin_secret={{ asec }}" class="btn green">Tải xuống Backup (.json)</a>
      </div>
      <div class="col-6" style="border-left: 1px solid var(--bd); padding-left: 12px;">
        <h4>Upload (Restore)</h4>
        <p><strong>CẢNH BÁO:</strong> Thao tác này sẽ <strong style="color:red">XÓA SẠCH</strong> toàn bộ keys hiện tại và thay thế bằng dữ liệu từ file backup.</p>
        <form method="post" action="{{ url_for('admin_backup_upload') }}?admin_secret={{ asec }}" enctype="multipart/form-data" onsubmit="return confirm('Bạn có chắc chắn muốn XÓA SẠCH keys hiện tại và restore từ file?');">
          <input type="file" name="backup_file" accept=".json" required>
          <button type="submit" class="btn red">Upload và Restore</button>
        </form>
      </div>
    </div>
  </div>
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
          <input class="mono" name="provider_type" value="mail72h" required>
        </div>
        <div class="col-6">
          <label>Base URL (Web đấu API)</label>
          <input class="mono" name="base_url" placeholder="https://mail72h.com">
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
    const providerInput = form.querySelector('input[name="provider_type"]');
    const baseurlInput = form.querySelector('input[name="base_url"]');
    const apikeyInput = form.querySelector('input[name="api_key"]');

    folderInput.readOnly = isLocked;
    providerInput.readOnly = isLocked;
    baseurlInput.readOnly = isLocked;
    apikeyInput.readOnly = isLocked;

    if (isLocked) {
        folderInput.value = folder;
        providerInput.value = provider;
        baseurlInput.value = baseurl;
        apikeyInput.value = apikey;
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
    
    if not sku or not input_key or not product_id.isdigit() or not api_key:
        return "Thiếu thông tin quan trọng (sku, input_key, product_id, api_key)", 400
    
    if not base_url and provider_type == 'mail72h':
        base_url = 'https://mail72h.com'
    
    with db() as con:
        con.execute("""
            INSERT INTO keymaps(group_name, sku, input_key, product_id, api_key, is_active, provider_type, base_url)
            VALUES(?,?,?,?,?,1,?,?)
            ON CONFLICT(input_key) DO UPDATE SET
              group_name=excluded.group_name,
              sku=excluded.sku,
              product_id=excluded.product_id,
              api_key=excluded.api_key,
              is_active=1,
              provider_type=excluded.provider_type,
              base_url=excluded.base_url
        """, (group_name, sku, input_key, int(product_id), api_key, provider_type, base_url))
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

# ==========================================================
# === MỚI: Thêm 2 route cho Backup và Restore ===
# ==========================================================
@app.route("/admin/backup/download")
def admin_backup_download():
    require_admin()
    try:
        with db() as con:
            maps = con.execute("SELECT * FROM keymaps").fetchall()
        
        # Chuyển đổi list của sqlite3.Row thành list của dict
        data_to_export = [dict(row) for row in maps]
        
        # Tạo tên file
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"keymaps_backup_{timestamp}.json"
        
        # Tạo response JSON
        response = jsonify(data_to_export)
        # Thiết lập header để trình duyệt tải file về
        response.headers['Content-Disposition'] = f'attachment; filename={filename}'
        response.headers['Content-Type'] = 'application/json'
        return response
        
    except Exception as e:
        print(f"BACKUP_DOWNLOAD_ERROR: {e}")
        return "Lỗi khi tạo file backup", 500

@app.route("/admin/backup/upload", methods=["POST"])
def admin_backup_upload():
    require_admin()
    
    if 'backup_file' not in request.files:
        return "Không tìm thấy file trong request", 400
    file = request.files['backup_file']
    if file.filename == '':
        return "Chưa chọn file", 400
    
    if file and file.filename.endswith('.json'):
        try:
            # Đọc và parse file JSON
            file_content = file.read().decode('utf-8')
            data_to_import = json.loads(file_content)
            
            if not isinstance(data_to_import, list):
                return "Lỗi định dạng JSON: Nội dung file không phải là một danh sách (list).", 400
            
            with db() as con:
                # 1. XÓA SẠCH dữ liệu cũ
                con.execute("DELETE FROM keymaps")
                
                # 2. Thêm dữ liệu mới từ file
                for item in data_to_import:
                    con.execute("""
                        INSERT INTO keymaps(
                            id, sku, input_key, product_id, is_active, 
                            group_name, provider_type, base_url, api_key
                        ) 
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (
                        item.get('id'), # Cho phép khôi phục ID cũ
                        item.get('sku'),
                        item.get('input_key'),
                        item.get('product_id'),
                        item.get('is_active', 1),
                        item.get('group_name', 'DEFAULT'),
                        item.get('provider_type', 'mail72h'),
                        item.get('base_url'),
                        item.get('api_key')
                    ))
                
                con.commit()
            
            return redirect(url_for("admin_index", admin_secret=ADMIN_SECRET))
            
        except json.JSONDecodeError:
            return "File JSON không hợp lệ. Vui lòng kiểm tra lại.", 400
        except Exception as e:
            print(f"BACKUP_UPLOAD_ERROR: {e}")
            return f"Đã xảy ra lỗi trong quá trình restore: {e}", 500
    else:
        return "Loại file không hợp lệ. Vui lòng upload file .json.", 400
# ==========================================================
# === KẾT THÚC KHỐI ROUTE MỚI ===
# ==========================================================


# ========= Public endpoints (Bộ định tuyến) =========
@app.route("/stock")
def stock():
    key = request.args.get("key","").strip()
    if not key:
        print("STOCK_ERROR: Missing key")
        return jsonify({"sum": 0}), 200
        
    row = find_map_by_key(key)
    if not row:
        print(f"STOCK_ERROR: Unknown key {key}")
        return jsonify({"sum": 0}), 200

    provider = row['provider_type']
    
    if provider == 'mail72h':
        return stock_mail72h(row)
    else:
        print(f"STOCK_ERROR: Provider '{provider}' not supported")
        return jsonify({"sum": 0}), 200


@app.route("/fetch")
def fetch():
    key = request.args.get("key","").strip()
    qty_s = request.args.get("quantity","").strip()
    
    if not key or not qty_s:
        print("FETCH_ERROR: Missing key/quantity")
        return jsonify([]), 200
    try:
        qty = int(qty_s); 
        if qty<=0 or qty>1000: raise ValueError()
    except Exception:
        print(f"FETCH_ERROR: Invalid quantity '{qty_s}'")
        return jsonify([]), 200

    row = find_map_by_key(key)
    if not row:
        print(f"FETCH_ERROR: Unknown key {key}")
        return jsonify([]), 200
    
    provider = row['provider_type']

    if provider == 'mail72h':
        return fetch_mail72h(row, qty)
    else:
        print(f"FETCH_ERROR: Provider '{provider}' not supported")
        return jsonify([]), 200

@app.route("/")
def health():
    return "OK", 200

# ==========================================================
# === ROUTE DEBUG: ĐỂ XEM DANH SÁCH SẢN PHẨM TỪ NCC ===
# ==========================================================
@app.route("/debuglist")
def debug_list_products():
    # 1. Bảo mật: Yêu cầu admin secret
    require_admin()
    
    # 2. Lấy key từ URL (ví dụ: ?key=key-abc)
    key = request.args.get("key","").strip()
    if not key:
        return "Vui lòng cung cấp ?key=... (dùng key đang bị lỗi)", 400
        
    row = find_map_by_key(key)
    if not row:
        return f"Không tìm thấy key: {key}", 404
    
    if row['provider_type'] != 'mail72h':
        return f"Key này không dùng provider 'mail72h'", 400
        
    try:
        # 3. Gọi thẳng đến API của nhà cung cấp
        base_url = row['base_url'] or 'https://mail72h.com'
        api_key = row["api_key"]
        list_data = mail72h_product_list(base_url, api_key)
        
        # 4. Trả về JSON thô
        return jsonify(list_data)
        
    except Exception as e:
        return f"Lỗi khi gọi API nhà cung cấp: {e}", 500
# ==========================================================


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
