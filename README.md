# Tạp Hóa ↔ Mail72h (Render)

## Chạy local
```bash
pip install -r requirements.txt
set ADMIN_SECRET=your-admin
set MAIL72H_API_KEY=your-mail72h-key
python app.py
# open http://localhost:8000/admin/products?admin_secret=your-admin
```

## Deploy Render (cách nhanh dùng `render.yaml`)
1. Push repo này lên GitHub.
2. Vào render.com → New + → **Blueprint** → chọn repo này.
3. Sau khi hiện màn hình cấu hình, đặt env vars: `ADMIN_SECRET`, `MAIL72H_API_KEY`.
4. Deploy. Render sẽ cho URL dạng https://your-app.onrender.com

## Endpoint cho Tạp Hóa
- Tồn kho: `GET /stock?sku=<sku>&key=<input_key>` → `{"sum": <int>}`
- Lấy hàng: `GET /fetch?sku=<sku>&order_id={order_id}&quantity={quantity}&key=<input_key>` → `[{ "product": "..." }, ...]`