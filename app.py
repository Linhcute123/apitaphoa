import os, json, sqlite3
from contextlib import closing
# Import 'request' để đọc cookies
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests
import datetime 

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
        pid_to_find_str = str(row["product_id"])
        
        list_data = mail72h_product_list(base_url, row["api_key"])
        
        if list_data.get("status") != "success":
            print(f"STOCK_ERROR (API List): {list_data.get('message', 'unknown')}")
            return jsonify({"sum": 0}), 200

        products = _collect_all_products(list_data)

        if not products:
             print(f"STOCK_ERROR: Could not find 'categories' or 'products' list inside /products.php response. Raw data: {str(list_data)[:500]}")
             return jsonify({"sum": 0}), 200

        stock_val = 0
        found = False
        for item in products:
            if not isinstance(item, dict): continue
            item_id_raw = item.get("id")
            if item_id_raw is None: continue
            
            try:
                item_id_str_cleaned = str(int(float(str(item_id_raw).strip())))
            except (ValueError, TypeError):
                print(f"STOCK_DEBUG: Skipping unparseable product ID: {item_id_raw}")
                continue
            
            if item_id_str_cleaned == pid_to_find_str:
                stock_from_api = item.get("amount") 
                if not stock_from_api: stock_from_api = 0
                stock_val = int(str(stock_from_api).replace(".", ""))
                found = True
                break
        
        if not found:
            print(f"STOCK_ERROR: Product ID {pid_to_find_str} not found in *any* category. (Collected {len(products)} products, but ID mismatch. Check your admin config.)")
            return jsonify({"sum": 0}), 200 
        
        return jsonify({"sum": stock_val})

    except requests.HTTPError as e:
        err_msg = f"mail72h http error {e.response.status_code}"
        try: err_detail = e.response.json().get('message', e.response.text); err_msg = f"mail72h error: {err_detail}"
        except: err_msg = f"mail72h http error {e.response.status_code}: {e.response.text}"
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
        try: err_detail = e.response.json().get('message', e.response.text); err_msg = f"mail72h error: {err_detail}"
        except: err_msg = f"mail72h http error {e.response.status_code}: {e.response.text}"
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
<html data-theme="light">
<head>
    <meta charset="utf-8" />
    <title>Multi-Provider (Per-Key API)</title>
    <style>
    :root { 
        --primary: #0d6efd; 
        --green: #198754; 
        --red: #dc3545; 
        --blue: #0d6efd;
        --gray: #6c757d;
        --shadow: 0 4px 12px rgba(0,0,0,0.05);
        
        /* Light Mode */
        --bg-light: #f8f9fa; 
        --border: #dee2e6;
        --card-bg: #ffffff;
        --text-dark: #212529;
        --text-light: #495057;
        --input-bg: #ffffff;
        --disabled-bg: #e9ecef;
        --disabled-text: #6c757d;
        --code-bg: #e9ecef;
        --nested-summary-bg: #f0f0f0;
    }
    :root[data-theme="dark"] {
        --primary: #3a86ff;
        --green: #20c997;
        --red: #f07167;
        --blue: #3a86ff;
        --gray: #adb5bd;
        --shadow: 0 4px 12px rgba(0,0,0,0.2);

        --bg-light: #121212;
        --border: #343a40;
        --card-bg: #1c1c1e;
        --text-dark: #e9ecef;
        --text-light: #adb5bd;
        --input-bg: #2c2c2e;
        --disabled-bg: #343a40;
        --disabled-text: #6c757d;
        --code-bg: #343a40;
        --nested-summary-bg: #2c2c2e;
    }

    body{
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
        padding:28px;
        color: var(--text-dark);
        background: var(--bg-light);
        line-height: 1.6;
        transition: background-color 0.2s, color 0.2s;
        position: relative;
    }
    .card{
        border: 1px solid var(--border);
        border-radius: 12px;
        padding: 20px 24px;
        margin-bottom: 24px;
        background: var(--card-bg);
        box-shadow: var(--shadow);
        transition: background-color 0.2s, border-color 0.2s;
    }
    .row{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;align-items:end}
    .col-1{grid-column:span 1}.col-2{grid-column:span 2}.col-3{grid-column:span 3}.col-4{grid-column:span 4}.col-6{grid-column:span 6}.col-8{grid-column:span 8}.col-12{grid-column:span 12}
    label{
        font-size: 13px;
        font-weight: 600;
        text-transform: uppercase;
        color: var(--text-light);
        margin-bottom: 4px;
        display: block;
    }
    input, select {
        width: 100%;
        padding: 10px 14px;
        border: 1px solid var(--border);
        border-radius: 8px;
        box-sizing: border-box;
        background: var(--input-bg);
        color: var(--text-dark);
        transition: border-color .2s, box-shadow .2s, background-color 0.2s, color 0.2s;
    }
    input:focus, select:focus {
        border-color: var(--primary);
        box-shadow: 0 0 0 3px rgba(13,110,253,0.25);
        outline: none;
    }
    input:disabled, input[readonly] { 
        background: var(--disabled-bg); 
        color: var(--disabled-text); 
        cursor: not-allowed; 
    }
    /* === MỚI: Bỏ table-layout: fixed; === */
    table{width:100%;border-collapse:collapse;margin-top: 10px;} 
    th,td{padding:12px 14px;border-bottom:1px solid var(--border);text-align:left;vertical-align: middle;} /* Bỏ word-break */
    th { font-size: 12px; text-transform: uppercase; color: var(--text-light); }
    code{background:var(--code-bg); color: var(--primary); padding:3px 6px;border-radius:6px;font-family:monospace;font-size: 0.9em;}
    
    button,.btn{
        padding: 10px 16px;
        border-radius: 8px;
        border: 1px solid transparent;
        background: var(--primary);
        color: #fff;
        cursor: pointer;
        text-decoration: none;
        font-weight: 600;
        transition: background-color .2s, transform .1s;
        display: inline-block;
        text-align: center;
        /* margin-bottom đã bị xóa */
    }
    button:hover, .btn:hover {
        filter: brightness(1.1);
        transform: translateY(-1px);
    }
    .btn.red{background:var(--red);border-color:var(--red)}
    .btn.blue{background:var(--blue);border-color:var(--blue)}
    .btn.green{background:var(--green);border-color:var(--green)}
    .btn.gray{background:var(--gray);border-color:var(--gray)}
    .btn.small{padding: 6px 12px; font-size: 13px; font-weight: 500;}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
    h2 {
        font-size: 28px;
        font-weight: 700;
        color: var(--primary);
        border-bottom: 2px solid var(--border);
        padding-bottom: 10px;
        margin-bottom: 20px;
    }
    h3 { margin-top: 0; margin-bottom: 16px; font-size: 22px; color: var(--text-dark); }
    h4 { margin-top: 0; margin-bottom: 8px; font-size: 18px; color: var(--text-dark); }
    details { border: 1px solid var(--border); border-radius: 10px; margin-bottom: 10px; overflow: hidden; }
    details summary { 
        padding: 14px 18px; 
        cursor: pointer; 
        font-weight: 600; 
        background: var(--card-bg); 
        transition: background-color 0.2s;
        font-size: 1.1em;
    }
    details summary:hover { filter: brightness(0.98); }
    details[open] summary { border-bottom: 1px solid var(--border); background-color: var(--card-bg);}
    details .content { padding: 16px; background: var(--bg-light); }
    details .content .btn { margin-top: 10px; }
    details details { margin-top: 10px; }
    details details summary { background: var(--nested-summary-bg); border-radius: 8px 8px 0 0; }

    .content-wrapper {
        position: relative;
    }
    .effect-canvas {
        position: fixed;
        top: 0;
        left: 0;
        width: 100vw;
        height: 100vh;
        pointer-events: none;
        z-index: 9999; 
    }

    </style>
    
    <script>
    (function() {
        var mode = document.cookie.split('; ').find(row => row.startsWith('admin_mode='))?.split('=')[1] || 'light';
        document.documentElement.setAttribute('data-theme', mode);
    })();
    </script>
</head>
<body>
<div> 
  <h2>⚙️ Multi-Provider (Quản lý theo Website)</h2>
  
  <div class="card" id="add-key-form-card">
    <h3>Thêm/Update Key</h3>
    <form method="post" action="{{ url_for('admin_add_keymap') }}?admin_secret={{ asec }}" id="main-key-form">
      <div class="row" style="margin-bottom:12px">
        <div class="col-4">
          <label>Provider Type</label>
          <input class="mono" name="provider_type" placeholder="vd: mail72h" required>
        </div>
        <div class="col-8">
          <label>Base URL (Web đấu API)</label>
          <input class="mono" name="base_url" placeholder="https://mail72h.com" required>
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
    <h3>Danh sách Keys (Theo Website)</h3>
    {% if not grouped_data %}
      <p>Chưa có key nào. Vui lòng thêm key bằng form bên trên.</p>
    {% endif %}
    
    {% for folder, providers in grouped_data.items() %}
      <details class="folder">
        <summary>📁 Website: {{ folder }}</summary>
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
                      <th>Base URL</th>
                      <th>product_id</th>
                      <th>Active</th>
                      <th>Hành động</th>
                    </tr>
                  </thead>
                  <tbody>
                  {% for key in data.key_list %}
                    <tr>
                      <td style="white-space: nowrap;">{{ key['sku'] }}</td>
                      <td style="white-space: nowrap;"><code>{{ key['input_key'] }}</code></td>
                      <td style="white-space: nowrap;"><code>{{ key['base_url'] }}</code></td>
                      <td style="white-space: nowrap; min-width: 60px;">{{ key['product_id'] }}</td> 
                      <td>{{ '✅' if key['is_active'] else '❌' }}</td>
                      <td style="min-width: 210px;"> 
                        <div style="display: flex; gap: 4px; align-items: center; flex-wrap: nowrap;">
                            <button class="btn gray small edit-key-btn" 
                                    data-sku="{{ key['sku'] }}"
                                    data-inputkey="{{ key['input_key'] }}"
                                    data-productid="{{ key['product_id'] }}"
                                    data-apikey="{{ key['api_key'] }}"
                                    data-provider="{{ key['provider_type'] }}"
                                    data-baseurl="{{ key['base_url'] }}">
                              Sửa ✏️
                            </button>
    
                            <form method="post" action="{{ url_for('admin_toggle_key', kmid=key['id']) }}?admin_secret={{ asec }}">
                              <button class="btn blue small" type="submit">{{ 'Disable' if key['is_active'] else 'Enable' }}</button>
                            </form>
                            
                            <form method="post" action="{{ url_for('admin_delete_key', kmid=key['id']) }}?admin_secret={{ asec }}" onsubmit="return confirm('Xoá key {{key['input_key']}}?')">
                              <button class="btn red small" type="submit">Xoá</button>
                            </form>
                        </div>
                      </td>
                    </tr>
                  {% endfor %}
                  </tbody>
                </table>
                <button class="btn green small add-key-helper" 
                        style="margin-top: 10px;"
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

  <div class="card" style="padding: 16px;">
    <div class="row" style="align-items: center;">
      <div class="col-4">
        <label for="mode-switcher">Chọn Nền</label>
        <select id="mode-switcher" class="mono">
          <option value="light" {% if mode == 'light' %}selected{% endif %}>Sáng (Mặc định) ☀️</option>
          <option value="dark" {% if mode == 'dark' %}selected{% endif %}>Tối 🌙</option>
        </select>
      </div>
      <div class="col-4">
        <label for="effect-switcher">Chọn Hiệu Ứng</label>
        <select id="effect-switcher" class="mono">
          <option value="default" {% if effect == 'default' %}selected{% endif %}>Không có</option>
          <option value="snow" {% if effect == 'snow' %}selected{% endif %}>Tuyết Rơi (Xanh) ❄️</option>
          <option value="matrix" {% if effect == 'matrix' %}selected{% endif %}>Matrix (Ma Trận 💻)</option>
          <option value="sakura" {% if effect == 'sakura' %}selected{% endif %}>Hoa Anh Đào 🌸</option>
          <option value="particles" {% if effect == 'particles' %}selected{% endif %}>Hạt Nối Nét ✨</option>
          <option value="rain" {% if effect == 'rain' %}selected{% endif %}>Mưa Rơi 🌧️</option>
        </select>
      </div>
    </div>
  </div>

  <div class="card">
    <h3>Backup & Đồng bộ</h3>
    <div class="row">
      <div class="col-6">
        <h4>Tải Backup</h4>
        <p>Tải xuống toàn bộ cấu hình keys (bảng keymaps) dưới dạng file JSON.</p>
        <a href="{{ url_for('admin_backup_download') }}?admin_secret={{ asec }}" class="btn green">Tải xuống Backup (.json)</a>
      </div>
      <div class="col-6" style="border-left: 1px solid var(--border); padding-left: 20px;">
        <h4>Upload (Restore)</h4>
        <p><strong>CẢNH BÁO:</strong> Thao tác này sẽ <strong style="color:var(--red)">XÓA SẠCH</strong> toàn bộ keys hiện tại và thay thế bằng dữ liệu từ file backup.</p>
        <form method="post" action="{{ url_for('admin_backup_upload') }}?admin_secret={{ asec }}" enctype="multipart/form-data" onsubmit="return confirm('Bạn có chắc chắn muốn XÓA SẠCH keys hiện tại và restore từ file?');">
          <input type="file" name="backup_file" accept=".json" required>
          <button type="submit" class="btn red" style="margin-top: 8px;">Upload và Restore</button>
        </form>
      </div>
    </div>
  </div>

</div>

<script>
function setLockedFields(isLocked, provider = '', baseurl = '', apikey = '') {
    const form = document.getElementById('main-key-form');
    const providerInput = form.querySelector('input[name="provider_type"]');
    const baseurlInput = form.querySelector('input[name="base_url"]');
    const apikeyInput = form.querySelector('input[name="api_key"]');

    providerInput.readOnly = isLocked;
    baseurlInput.readOnly = isLocked;
    apikeyInput.readOnly = isLocked;

    if (isLocked) {
        providerInput.value = provider;
        baseurlInput.value = baseurl;
        apikeyInput.value = apikey;
    }
}

document.addEventListener('click', function(e) {
  // Logic cho nút "+ Thêm Key vào đây"
  if (e.target.classList.contains('add-key-helper')) {
    e.preventDefault();
    const provider = e.target.dataset.provider;
    const baseurl = e.target.dataset.baseurl;
    const apikey = e.target.dataset.apikey; 
    
    setLockedFields(true, provider, baseurl, apikey); 
    
    const form = document.getElementById('main-key-form');
    form.querySelector('input[name="sku"]').value = '';
    form.querySelector('input[name="input_key"]').value = '';
    form.querySelector('input[name="product_id"]').value = '';
    
    const formCard = document.getElementById('add-key-form-card');
    formCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    formCard.querySelector('input[name="sku"]').focus();
  }

  // Logic cho nút "Sửa ✏️"
  if (e.target.classList.contains('edit-key-btn')) {
    e.preventDefault();
    setLockedFields(false);

    const form = document.getElementById('main-key-form');
    form.querySelector('input[name="provider_type"]').value = e.target.dataset.provider;
    form.querySelector('input[name="base_url"]').value = e.target.dataset.baseurl;
    form.querySelector('input[name="sku"]').value = e.target.dataset.sku;
    form.querySelector('input[name="input_key"]').value = e.target.dataset.inputkey;
    form.querySelector('input[name="product_id"]').value = e.target.dataset.productid;
    form.querySelector('input[name="api_key"]').value = e.target.dataset.apikey;

    const formCard = document.getElementById('add-key-form-card');
    formCard.scrollIntoView({ behavior: 'smooth', block: 'start' });
    formCard.querySelector('input[name="sku"]').focus();
  }
  
});

// Logic cho nút "Xóa form"
document.getElementById('reset-form-btn').addEventListener('click', function() {
    setLockedFields(false);
});

// Logic cho Effect Switcher
document.getElementById('effect-switcher').addEventListener('change', function() {
    const selectedEffect = this.value;
    document.cookie = `admin_effect=${selectedEffect};path=/;max-age=31536000;SameSite=Lax`;
    location.reload();
});

// Logic cho Mode Switcher (Sáng/Tối)
document.getElementById('mode-switcher').addEventListener('change', function() {
    const selectedMode = this.value;
    document.cookie = `admin_mode=${selectedMode};path=/;max-age=31536000;SameSite=Lax`;
    document.documentElement.setAttribute('data-theme', selectedMode);
});
</script>

<script>
function createEffectCanvas(id) {
    if (document.getElementById(id)) return null; 
    var canvas = document.createElement('canvas');
    canvas.id = id;
    canvas.className = 'effect-canvas'; // Sử dụng class CSS
    document.body.appendChild(canvas);
    
    var ctx = canvas.getContext('2d');
    var W = window.innerWidth;
    var H = window.innerHeight;
    canvas.width = W;
    canvas.height = H;
    
    var resizeTimer;
    window.addEventListener('resize', function() {
        clearTimeout(resizeTimer);
        resizeTimer = setTimeout(function() {
            W = window.innerWidth;
            H = window.innerHeight;
            canvas.width = W;
            canvas.height = H;
        }, 250);
    });
    
    return { canvas, ctx, W, H };
}
</script>

{% if effect == 'snow' %}
<script id="snow-effect-script">
(function() {
    var a = createEffectCanvas('snow-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    
    var mp = 100;
    var flakes = [];
    for(var i = 0; i < mp; i++) {
        flakes.push({
            x: Math.random() * W, y: Math.random() * H,
            r: Math.random() * 4 + 1, d: Math.random() * mp
        });
    }
    function draw() {
        ctx.clearRect(0, 0, W, H);
        ctx.fillStyle = "rgba(173, 216, 230, 0.8)"; // Màu xanh nhạt
        ctx.beginPath();
        for(var i = 0; i < mp; i++) {
            var f = flakes[i];
            ctx.moveTo(f.x, f.y);
            ctx.arc(f.x, f.y, f.r, 0, Math.PI * 2, true);
        }
        ctx.fill();
        update();
    }
    var angle = 0;
    function update() {
        angle += 0.01;
        W = window.innerWidth; H = window.innerHeight;
        for(var i = 0; i < mp; i++) {
            var f = flakes[i];
            f.y += Math.cos(angle + f.d) + 1 + f.r / 2;
            f.x += Math.sin(angle) * 2;
            if(f.x > W + 5 || f.x < -5 || f.y > H) {
                if(i % 3 > 0) { flakes[i] = {x: Math.random() * W, y: -10, r: f.r, d: f.d}; }
                else {
                    if(Math.sin(angle) > 0) { flakes[i] = {x: -5, y: Math.random() * H, r: f.r, d: f.d}; }
                    else { flakes[i] = {x: W + 5, y: Math.random() * H, r: f.r, d: f.d}; }
                }
            }
        }
    }
    setInterval(draw, 33);
})();
</script>
{% endif %}

{% if effect == 'matrix' %}
<script id="matrix-effect-script">
(function() {
    var a = createEffectCanvas('matrix-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    
    var font_size = 14;
    var columns = Math.floor(W / font_size);
    var drops = [];
    for(var x = 0; x < columns; x++) drops[x] = 1; 
    var chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890@#$%^&*()";
    chars = chars.split("");

    function draw() {
        W = window.innerWidth; H = window.innerHeight;
        
        var currentTheme = document.documentElement.getAttribute('data-theme');
        if(currentTheme === 'dark') {
            ctx.fillStyle = "rgba(18, 18, 18, 0.15)";
        } else {
            ctx.fillStyle = "rgba(248, 249, 250, 0.15)";
        }
        ctx.fillRect(0, 0, W, H);
        
        ctx.fillStyle = "#0F0"; 
        ctx.font = font_size + "px monospace";

        for(var i = 0; i < drops.length; i++) {
            var text = chars[Math.floor(Math.random() * chars.length)];
            ctx.fillText(text, i * font_size, drops[i] * font_size);
            
            if(drops[i] * font_size > H && Math.random() > 0.975) {
                drops[i] = 0;
            }
            drops[i]++;
        }
    }
    
    window.addEventListener('resize', function() {
        columns = Math.floor(window.innerWidth / font_size);
        if (columns > drops.length) {
            for(var x = drops.length; x < columns; x++) drops[x] = 1; 
        } else {
            drops.length = columns;
        }
    });
    setInterval(draw, 40);
})();
</script>
{% endif %}

{% if effect == 'sakura' %}
<script id="sakura-effect-script">
(function() {
    var a = createEffectCanvas('sakura-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;

    var mp = 75;
    var petals = [];
    for(var i = 0; i < mp; i++) {
        petals.push({
            x: Math.random() * W, y: Math.random() * H,
            r: Math.random() * 4 + 1, d: Math.random() * mp,
            c: (Math.random() > 0.5) ? "rgba(255, 192, 203, 0.8)" : "rgba(255, 255, 255, 0.8)"
        });
    }
    function draw() {
        ctx.clearRect(0, 0, W, H);
        for(var i = 0; i < mp; i++) {
            var p = petals[i];
            ctx.fillStyle = p.c;
            ctx.beginPath();
            ctx.moveTo(p.x, p.y);
            ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2, true);
            ctx.fill();
        }
        update();
    }
    var angle = 0;
    function update() {
        angle += 0.01;
        W = window.innerWidth; H = window.innerHeight;
        for(var i = 0; i < mp; i++) {
            var p = petals[i];
            p.y += Math.cos(angle + p.d) + 1 + p.r / 2;
            p.x += Math.sin(angle) * 2;
            if(p.x > W + 5 || p.x < -5 || p.y > H) {
                if(i % 3 > 0) { petals[i] = {x: Math.random() * W, y: -10, r: p.r, d: p.d, c: p.c}; }
                else {
                    if(Math.sin(angle) > 0) { petals[i] = {x: -5, y: Math.random() * H, r: p.r, d: p.d, c: p.c}; }
                    else { petals[i] = {x: W + 5, y: Math.random() * H, r: p.r, d: p.d, c: p.c}; }
                }
            }
        }
    }
    setInterval(draw, 33);
})();
</script>
{% endif %}

{% if effect == 'particles' %}
<script id="particles-effect-script">
(function() {
    var a = createEffectCanvas('particles-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    
    var particleCount = 100;
    var particles = [];
    var lineDistance = 120;

    function setup() {
        W = window.innerWidth; H = window.innerHeight;
        lineDistance = Math.min(W, H) / 8;
        particles = [];
        particleCount = Math.floor((W * H) / 20000);
        for (var i = 0; i < particleCount; i++) {
            particles.push({
                x: Math.random() * W, y: Math.random() * H,
                vx: Math.random() * 1 - 0.5, vy: Math.random() * 1 - 0.5
            });
        }
    }
    setup();
    window.addEventListener('resize', setup);

    function draw() {
        ctx.clearRect(0, 0, W, H);
        
        var currentTheme = document.documentElement.getAttribute('data-theme');
        if(currentTheme === 'dark') {
             ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
             ctx.strokeStyle = 'rgba(255, 255, 255, 0.2)';
        } else {
             ctx.fillStyle = 'rgba(0, 0, 0, 0.5)';
             ctx.strokeStyle = 'rgba(0, 0, 0, 0.2)';
        }

        particles.forEach(p1 => {
            ctx.beginPath();
            ctx.arc(p1.x, p1.y, 2, 0, Math.PI * 2);
            ctx.fill();

            p1.x += p1.vx;
            p1.y += p1.vy;

            if (p1.x < 0 || p1.x > W) p1.vx *= -1;
            if (p1.y < 0 || p1.y > H) p1.vy *= -1;

            particles.forEach(p2 => {
                var dx = p1.x - p2.x;
                var dy = p1.y - p2.y;
                var dist = Math.sqrt(dx * dx + dy * dy);

                if (dist < lineDistance) {
                    ctx.globalAlpha = 1 - (dist / lineDistance);
                    ctx.beginPath();
                    ctx.moveTo(p1.x, p1.y);
                    ctx.lineTo(p2.x, p2.y);
                    ctx.stroke();
                }
            });
            ctx.globalAlpha = 1;
        });
    }
    setInterval(draw, 33);
})();
</script>
{% endif %}

{% if effect == 'rain' %}
<script id="rain-effect-script">
(function() {
    var a = createEffectCanvas('rain-canvas');
    if (!a) return;
    var ctx = a.ctx, W = a.W, H = a.H;
    
    var drops = [];
    var dropCount = 500;
    
    function setup() {
        W = window.innerWidth; H = window.innerHeight;
        dropCount = Math.floor(W / 4);
        drops = [];
        for (var i = 0; i < dropCount; i++) {
            drops.push({
                x: Math.random() * W, y: Math.random() * H - H,
                l: Math.random() * 2 + 1, v: Math.random() * 5 + 5
            });
        }
    }
    setup();
    window.addEventListener('resize', setup);

    function draw() {
        ctx.clearRect(0, 0, W, H);
        
        var currentTheme = document.documentElement.getAttribute('data-theme');
        if(currentTheme === 'dark') {
            ctx.strokeStyle = 'rgba(173, 216, 230, 0.5)';
        } else {
            ctx.strokeStyle = 'rgba(0, 0, 139, 0.5)';
        }
        ctx.lineWidth = 1;

        for (var i = 0; i < dropCount; i++) {
            var d = drops[i];
            ctx.beginPath();
            ctx.moveTo(d.x, d.y);
            ctx.lineTo(d.x - 1, d.y + d.l * 2);
            ctx.stroke();
            
            d.y += d.v;
            
            if (d.y > H) {
                d.y = -d.l;
                d.x = Math.random() * W;
            }
        }
    }
    setInterval(draw, 16);
})();
</script>
{% endif %}

</body>
</html>
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

    # Đọc 2 cookies riêng biệt
    effect = request.cookies.get('admin_effect', 'default') # Cookie cho hiệu ứng
    mode = request.cookies.get('admin_mode', 'light') # Cookie cho Sáng/Tối
    
    return render_template_string(ADMIN_TPL, grouped_data=grouped_data, asec=ADMIN_SECRET, effect=effect, mode=mode)

@app.route("/admin/keymap", methods=["POST"])
def admin_add_keymap():
    require_admin()
    f = request.form
    
    sku = f.get("sku","").strip()
    input_key = f.get("input_key","").strip()
    product_id = f.get("product_id","").strip()
    
    provider_type = f.get("provider_type","").strip().lower() 
    base_url = f.get("base_url","").strip()
    api_key = f.get("api_key","").strip()
    
    group_name = base_url
    
    if not sku or not input_key or not product_id.isdigit() or not api_key or not provider_type or not base_url:
        return "Thiếu thông tin quan trọng (sku, input_key, product_id, api_key, provider_type, base_url)", 400
    
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
# === 2 route cho Backup và Restore ===
# ==========================================================
@app.route("/admin/backup/download")
def admin_backup_download():
    require_admin()
    try:
        with db() as con:
            maps = con.execute("SELECT * FROM keymaps").fetchall()
        
        data_to_export = [dict(row) for row in maps]
        
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"keymaps_backup_{timestamp}.json"
        
        response = jsonify(data_to_export)
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
            file_content = file.read().decode('utf-8')
            data_to_import = json.loads(file_content)
            
            if not isinstance(data_to_import, list):
                return "Lỗi định dạng JSON: Nội dung file không phải là một danh sách (list).", 400
            
            with db() as con:
                con.execute("DELETE FROM keymaps")
                
                for item in data_to_import:
                    con.execute("""
                        INSERT INTO keymaps(
                            id, sku, input_key, product_id, is_active, 
                            group_name, provider_type, base_url, api_key
                        ) 
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (
                        item.get('id'), 
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
    require_admin()
    
    key = request.args.get("key","").strip()
    if not key:
        return "Vui lòng cung cấp ?key=... (dùng key đang bị lỗi)", 400
        
    row = find_map_by_key(key)
    if not row:
        return f"Không tìm thấy key: {key}", 404
    
    if row['provider_type'] != 'mail72h':
        return f"Key này không dùng provider 'mail72h'", 400
        
    try:
        base_url = row['base_url'] or 'https://mail72h.com'
        api_key = row["api_key"]
        list_data = mail72h_product_list(base_url, api_key)
        
        return jsonify(list_data)
        
    except Exception as e:
        return f"Lỗi khi gọi API nhà cung cấp: {e}", 500
# ==========================================================


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
