
import os, json, sqlite3, re, requests
from flask import Flask, request, jsonify

DB = os.getenv("DB_PATH", "store_v2.db")
MAIL_TIMEOUT = 4
_KEY_CANDIDATES = ("key","api_key","api_keyy")

app = Flask(__name__)

def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c

def _bases_to_try(base):
    base = base.rstrip("/")
    return [base, base+"/api"] if not base.endswith("/api") else [base]

def _http_get_json(url,params):
    r=requests.get(url,params=params,timeout=MAIL_TIMEOUT);r.raise_for_status();return r.json()
def _http_post_json(url,data):
    r=requests.post(url,data=data,timeout=MAIL_TIMEOUT);r.raise_for_status();return r.json()

def _extract_int(v):
    if isinstance(v,(int,float)):return int(v)
    if not v:return None
    m=re.search(r"\d+",str(v))
    return int(m.group(0)) if m else None

def _deep_find_stock(o):
    keys={'stock','quantity','qty','remain','available','tonkho','soluong','so_luong','left'}
    if isinstance(o,dict):
        for k,v in o.items():
            if k.lower() in keys:
                n=_extract_int(v)
                if n is not None:return n
            n=_deep_find_stock(v)
            if n is not None:return n
    if isinstance(o,list):
        for it in o:
            n=_deep_find_stock(it)
            if n is not None:return n
    return None

def provider_stock(base,api_key,pid):
    for b in _bases_to_try(base):
        for key in _KEY_CANDIDATES:
            try:
                data=_http_get_json(f"{b}/product.php",{key:api_key,"id":pid})
                n=_deep_find_stock(data)
                if n is not None:return n
            except:pass
    for b in _bases_to_try(base):
        for key in _KEY_CANDIDATES:
            try:
                data=_http_get_json(f"{b}/products.php",{key:api_key})
                items=[]
                if isinstance(data,dict):
                    for k in ("data","products","items","result"):
                        if isinstance(data.get(k),list):items=data[k];break
                elif isinstance(data,list):items=data
                for it in items:
                    if str(it.get("id") or it.get("product_id"))==str(pid):
                        n=_deep_find_stock(it)
                        if n is not None:return n
            except:pass
    raise Exception("no stock info")

@app.route("/stock")
def stock():
    key=request.args.get("key")
    if not key:return jsonify({"status":"error","msg":"missing key"}),400
    with db() as c:
        km=c.execute("SELECT k.*,s.base_url FROM keymaps k JOIN sites s ON s.id=s.id WHERE k.input_key=?",(key,)).fetchone()
    if not km:return jsonify({"status":"error","msg":"unknown key"}),404
    try:
        s=provider_stock(km["base_url"],km["provider_api_key"],km["product_id"])
        return jsonify({"sum":int(s)})
    except Exception as e:
        return jsonify({"status":"error","msg":str(e)}),502

@app.route("/fetch")
def fetch():
    key=request.args.get("key");qty=request.args.get("quantity","1");oid=request.args.get("order_id","")
    if not key:return jsonify({"status":"error","msg":"missing key"}),400
    try:qty=int(qty)
    except:return jsonify({"status":"error","msg":"bad quantity"}),400
    with db() as c:
        km=c.execute("SELECT k.*,s.base_url FROM keymaps k JOIN sites s ON s.id=s.id WHERE k.input_key=?",(key,)).fetchone()
    if not km:return jsonify({"status":"error","msg":"unknown key"}),404
    for b in _bases_to_try(km["base_url"]):
        for k in _KEY_CANDIDATES:
            try:
                r=_http_post_json(f"{b}/buy_product",{"action":"buyProduct","id":km["product_id"],"amount":qty,k:km["provider_api_key"]})
                return jsonify(r)
            except:pass
    return jsonify({"status":"error","msg":"buy failed"}),502

@app.route("/")
def root():return "OK",200

if __name__=="__main__":
    app.run(host="0.0.0.0",port=8000)
