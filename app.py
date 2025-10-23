
import os, json, sqlite3, re, traceback
from contextlib import closing
from flask import Flask, request, jsonify, abort, redirect, url_for, render_template_string
import requests

DB = os.getenv("DB_PATH", "store.db")
ADMIN_SECRET = os.getenv("ADMIN_SECRET", "CHANGE_ME")
MAIL_TIMEOUT = int(os.getenv("MAIL_TIMEOUT", "4"))
DEBUG_ERRORS = os.getenv("DEBUG_ERRORS", "0") in ("1","true","True","yes","YES")

app = Flask(__name__)

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

def provider_product_detail(base_url: str, api_key: str, product_id: int) -> dict:
    url = f"{base_url.rstrip('/')}/product.php"
    r = requests.get(url, params={"api_key": api_key, "id": product_id}, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

def provider_buy(base_url: str, api_key: str, product_id: int, amount: int) -> dict:
    url = f"{base_url.rstrip('/')}/buy_product"
    data = {"action": "buyProduct", "id": product_id, "amount": amount, "api_key": api_key}
    r = requests.post(url, data=data, timeout=MAIL_TIMEOUT)
    r.raise_for_status()
    return r.json()

def _extract_int(value):
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    m = re.search(r'[-+]?\d[\d,\.]*', s)
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

BASE_TPL = "<h3>OK Admin</h3>"  # keep minimal; error likely before render, we just need to reach here

def require_admin():
    if request.args.get("admin_secret") != ADMIN_SECRET:
        abort(403)

@app.errorhandler(Exception)
def on_err(e):
    if DEBUG_ERRORS:
        tb = traceback.format_exc()
        return f"<h2>ERROR</h2><pre>{tb}</pre>", 500, {"Content-Type":"text/html; charset=utf-8"}
    raise e

@app.route("/admin")
def admin_index():
    require_admin()
    # just touch DB to force obvious schema issues
    with db() as con:
        con.execute("SELECT 1").fetchone()
        con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return BASE_TPL

# quick helpers to inspect
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
