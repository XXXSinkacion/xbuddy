#!/usr/bin/env bash
# macOS / Linux 本地开发：后端 8000 + 前端 5173（代理 /api）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$ROOT/.env" ] || { cp "$ROOT/.env.example" "$ROOT/.env"; echo "已创建 .env（请按需填写密钥）"; }
(cd "$ROOT/backend" && python -m uvicorn app.main:app --reload --port 8000) &
(cd "$ROOT/frontend" && npm run dev) &
echo "后端 http://127.0.0.1:8000  前端 http://127.0.0.1:5173"
wait
