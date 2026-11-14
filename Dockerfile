# ============================================================
# 提示词工坊 PromptForge · Docker Web 版（浏览器直接访问）
# 注:不写 # syntax=docker/dockerfile:1 —— 该指令强制 BuildKit 联网拉前端镜像,
# 隔离网络/代理失效环境(群晖 NAS 等)构建必败;内建前端足够。
# 当前版本：v0.1.1（镜像 promptforge:v0.1.1）。
# ============================================================
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app/src

WORKDIR /app

# 先装 Python 依赖(层缓存)
# Web 版必须装 requirements-web.txt (Flask + Waitress);
# 桌面版 customtkinter 不进镜像。
# pip 源: compose 默认传清华镜像 (国内网络); 海外服务器改回 https://pypi.org/simple
ARG PIP_INDEX_URL=https://pypi.org/simple
ENV PIP_INDEX_URL=${PIP_INDEX_URL}
COPY requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

# 源码：src 布局，核心层 + Web 层（桌面 GUI 层不进入镜像）
COPY src ./src

RUN mkdir -p /app/data

EXPOSE 8000
VOLUME ["/app/data"]

CMD ["python", "-m", "promptforge.webapp"]
