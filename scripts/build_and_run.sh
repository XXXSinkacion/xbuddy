#!/usr/bin/env bash
# 生产方式：构建前端后由 FastAPI 单进程托管（无 Docker 时使用）
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
(cd "$ROOT/frontend" && npm install --no-audit --no-fund && npm run build)
(cd "$ROOT/backend" && pip install -r requirements.txt && python -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}")
