#!/usr/bin/env bash
# ============================================================
# 提示词工坊 PromptForge (PromptEnhancer) · 独立 Docker 部署脚本
# 用法:  ./deploy.sh
#        docker compose logs -f promptforge   跟踪日志
#        docker compose down                  停止
# 端口:  宿主 6085 -> 容器 8000 (改 docker-compose.yml ports 左半边)
# ============================================================
set -euo pipefail
cd "$(dirname "$0")"

gen_pwd() (  # 18 位字母数字强随机口令 (子 shell 关闭 pipefail, 规避 SIGPIPE 误报)
    set +o pipefail
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 18
    else
        head -c 256 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c 18
    fi
)

# 首次部署: 生成 .env, 占位口令自动换成强随机口令并回显
if [ ! -f .env ]; then
    cp .env.example .env
    echo "[env] 已生成 .env"
fi
if grep -q "^WEB_ACCESS_PASSWORD=change-me" .env 2>/dev/null; then
    ACCESS="$(gen_pwd)"; ADMIN="$(gen_pwd)"
    sed -i "s|^WEB_ACCESS_PASSWORD=.*|WEB_ACCESS_PASSWORD=${ACCESS}|" .env
    sed -i "s|^WEB_ADMIN_PASSWORD=.*|WEB_ADMIN_PASSWORD=${ADMIN}|" .env
    echo "[env] 已随机生成口令并写入 .env:"
    echo "      WEB_ACCESS_PASSWORD (平台访问口令)   = ${ACCESS}"
    echo "      WEB_ADMIN_PASSWORD  (admin 初始口令) = ${ADMIN}   ← 登录后请修改"
fi

mkdir -p data
docker compose up -d --build

echo
echo "=============================================="
echo " PromptForge 已启动:  http://127.0.0.1:6085"
echo " 登录口令见 .env: WEB_ACCESS_PASSWORD (进入平台) /"
echo "                  admin + WEB_ADMIN_PASSWORD (管理员)"
echo " 数据目录: ./data (账号库/历史记录/每用户工作区) — 备份即打包"
echo "=============================================="
