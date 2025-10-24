# Tạp Hóa – Multi Site Direct (Final Full)
- Admin: /admin?admin_secret=adminlinhdz
- /stock & /fetch gọi thẳng provider (mail72h style)
- Deploy: Render (gunicorn).

ENV:
ADMIN_SECRET=adminlinhdz
DB_PATH=store_v2.db
MAIL72H_TIMEOUT=4
DEBUG_ERRORS=0