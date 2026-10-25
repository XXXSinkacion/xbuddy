# Windows 本地开发：后端 8000 + 前端 5173（代理 /api）
# 用法：在项目根目录执行  powershell -ExecutionPolicy Bypass -File scripts/dev.ps1
$root = Split-Path -Parent $PSScriptRoot
if (-not (Test-Path "$root/.env")) { Copy-Item "$root/.env.example" "$root/.env"; Write-Host "已创建 .env（请按需填写密钥）" }
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root/backend'; python -m uvicorn app.main:app --reload --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-Command", "cd '$root/frontend'; npm run dev"
Write-Host "后端 http://127.0.0.1:8000  前端 http://127.0.0.1:5173"
