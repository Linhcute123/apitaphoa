# Direct mode (no buffer)
- Quản lý nhiều `input_key` ↔ `product_id`.
- `/stock?key=...` trả 9999 (không chặn mua).
- `/fetch?key=...&quantity=...` gọi trực tiếp mail72h với timeout 4s để đáp ứng <5s.