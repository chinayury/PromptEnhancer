@echo off
rem ============================================================
rem 提示词工坊 PromptForge (PromptEnhancer) · 独立 Docker 部署脚本 (Windows)
rem 端口: 宿主 6085 -> 容器 8000
rem ============================================================
cd /d %~dp0
if not exist .env (
    copy .env.example .env >nul
    echo [!] 已生成 .env - 请先编辑填入 WEB_ACCESS_PASSWORD / WEB_ADMIN_PASSWORD 再重跑本脚本!
    exit /b 1
)
if not exist data mkdir data
docker compose up -d --build
echo.
echo PromptForge: http://127.0.0.1:6085
echo 登录口令见 .env (WEB_ACCESS_PASSWORD / WEB_ADMIN_PASSWORD)
