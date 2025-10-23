# Multi-key per product + buffer (<5s)
- Quản lý **nhiều input_key** cho **một SKU**, mỗi key map tới một `product_id` trên mail72h.
- Endpoint hỗ trợ:
  - `/stock?key=<input_key>` (khuyên dùng) hoặc `/stock?sku=<sku>&key=<input_key>`
  - `/fetch?key=<input_key>&order_id=...&quantity=...` (cũng hỗ trợ thêm `sku=`)
- Buffer local để đảm bảo trả hàng <5s; thiếu thì gọi mail72h với timeout ngắn.