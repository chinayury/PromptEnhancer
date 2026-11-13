# 提示词工坊 · PromptForge

一款**本地优先**的提示词工作台，一个仓库、两种形态：

1. **Web 多用户版**（主交付形态，Docker 部署）：Flask + Waitress，每个账号独立工作区，浏览器直接访问。含单条/批量增强、Prompt Lint 静态体检、双版对比配对判定、历史档案与全文检索、Token 用量与成本统计、提示词版本链与回滚、五槽指令构造、LLM 响应缓存、完整管理后台；界面为「钴蓝工坊 · Cobalt Forge」全浅色视觉体系。
2. **桌面单机版**（`python -m promptforge`）：customtkinter 单窗口，提示词增强 + 指令构造双模块，数据结构与 Web 版完全同构。

两大核心模块：

1. **提示词增强**：把一句原始提示词，借助任意**通用兼容**文本生成接口，改写为更清晰、更专业、更可执行的版本。
2. **指令构造器（五槽）**：把一份技术设计文档编译成「文档定位 + 本期切片 + 反向边界 + 可执行验收 + 歧义停工」的结构化指令，纯本地、零远程调用、省 token；支持 `{{变量名}}` 模板变量。

所有数据（连接配置、增强历史、清空审计、用量明细、LLM 响应缓存）均存于本地 SQLite，**默认不上云**。

---

## 一、技术栈

| 层 | 选型 |
|---|---|
| Web 服务 | Python 3.11 + Flask 3.1+ + Waitress 3.0+（`threads=8`） |
| 桌面 GUI | Python + customtkinter（仅桌面版一个第三方 GUI 依赖） |
| 增强引擎 | **纯标准库** `urllib` / `json` / `socket`，调用任意 OpenAI 兼容 `/chat/completions` |
| 持久化 | SQLite（标准库 `sqlite3`）+ FTS5 + trigram 分词全文检索（支持中文子串匹配，需 sqlite ≥ 3.34；不支持时自动回退 LIKE 全表扫描） |
| 口令哈希 | 标准库 `pbkdf2_hmac(sha256)`，240,000 轮 + 16B 随机盐 |
| 前端 | 原生 HTML/CSS/JS 单页（无构建、无框架），内联线性 SVG 图标 |
| 容器 | `python:3.11-slim`，`docker compose` 编排，数据卷 `./data` |

---

## 二、快速开始

### Web 版（推荐，Docker 部署）

```bash
./deploy.sh                 # 一键: 生成 .env(随机口令) → 构建 → 启动
# 或手动:
cp .env.example .env        # 编辑 WEB_ACCESS_PASSWORD / WEB_ADMIN_PASSWORD
docker compose up -d --build
```

浏览器打开 `http://服务器IP:6085`（宿主机 6085 → 容器 8000）。详见 [docs/02-环境部署教程.md](docs/02-环境部署教程.md)。

本地裸跑（不依赖 Docker）：

```bash
pip install -r requirements-web.txt && python -m promptforge.webapp   # 默认监听所有网卡，端口 8000
curl http://127.0.0.1:8000/healthz                                    # 返回 {"ok":true,"version":"<version>"}
```

### 桌面版 / 命令行

```bash
pip install -r requirements.txt         # 仅 customtkinter
python -m promptforge                   # 桌面 GUI
python -m promptforge cli --text "写一个登录页"   # 命令行模式
python -m forge --file <设计文档.md> --outline     # 构造器 CLI
```

### 安装为包（可选）

```bash
pip install -e .                       # 安装后可用 promptforge / promptforge-web / forge 命令
pip install -e ".[web]"                # 仅 Web 版依赖
pip install -e ".[desktop]"            # 仅桌面版依赖
```

---

## 三、工程结构

```
src/
├── promptforge/              核心包：提示词增强
│   ├── __init__.py
│   ├── __main__.py           桌面 GUI / CLI 入口（python -m promptforge）
│   ├── webapp.py             Web 版入口（全部 API 路由、环境变量、启动逻辑）
│   ├── cli.py                命令行模式
│   ├── core/                 业务层：engine（增强引擎）/ templates（风格白名单）/ lint（体检规则）
│   ├── data/                 数据层：database / users / usage / versions / llm_cache
│   ├── config/               配置层：providers.py + providers.json（厂商单一真源）
│   ├── gui/                  桌面 GUI（customtkinter）
│   ├── utils/                工具层：日志
│   └── web/                  前端：index.html / login.html / favicon.svg
│
└── forge/                   指令构造引擎：parser / assembler / gui_model / templates / cli / gui

docs/                        交付文档（5 篇）
pyproject.toml               构建配置（src 布局、入口点、包数据）
requirements.txt             桌面依赖（customtkinter）
requirements-web.txt         Web 依赖（锁定安全下界）
Dockerfile / docker-compose.yml    Web 版编排
deploy.sh / deploy.bat        一键部署脚本
build_exe.bat / PromptForge.spec  桌面版打包
.env.example                 环境变量模板
README.md / CHANGELOG.md
```

> `forge` 与 `promptforge` 是两个互相协作的包：`promptforge` 负责提示词增强，`forge` 负责五槽指令构造。`webapp.py` 与 `promptforge/gui/app.py` 均引用 `forge` 的控制器，二者缺一不可。

---

## 四、文档索引

| 文档 | 内容 |
|---|---|
| [docs/01-项目介绍与架构.md](docs/01-项目介绍与架构.md) | 项目概述、核心设计原则、模块结构、分层原则、关键技术选型、数据模型 |
| [docs/02-环境部署教程.md](docs/02-环境部署教程.md) | 环境要求、安装方式、目录与文件、配置说明、启动方式、常见问题 |
| [docs/03-功能使用说明.md](docs/03-功能使用说明.md) | 各入口（Web/桌面/CLI）用法、参数说明、新功能操作、已知限制 |
| [docs/04-安全设计说明.md](docs/04-安全设计说明.md) | 安全架构、鉴权、输入校验、凭据管理、路径防护、降级策略、渗透验证 |
| [docs/05-开发日志.md](docs/05-开发日志.md) | 缺陷修复（现象→根因→解决方案）、功能新增、版本迭代、同类项目借鉴 |
| [CHANGELOG.md](CHANGELOG.md) | 变更日志（只追加，含四轮迭代条目） |

---

## 五、安全特性与已知限制

### 5.1 安全特性

- **管理员三重认证**：用户名 + 密码 + 平台口令（`WEB_ACCESS_PASSWORD`）；口令错与密码错同文案，防管理员账号枚举。
- **登录双维限流**：账号连续失败 **5 次锁定 15 分钟**（DB 持久化，原子 UPDATE 消竞态）+ 每 IP 失败 **20 次/10 分钟**（内存态）。
- **登录计时匀势**：对不存在的用户也执行一次同等成本 PBKDF2，封堵账号枚举计时侧信道。
- **会话 Cookie**：`HttpOnly` + `SameSite=Lax` 默认开启；`WEB_COOKIE_SECURE=1` 可开启 `Secure`。
- **CSRF 防护**：会话 Cookie `SameSite=Lax` 拦截跨站 POST 携带 Cookie；全部写接口以 `request.get_json(silent=True)` 解析请求体。
- **安全响应头**：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、CSP。
- **数据隔离**：每用户独立工作区一套库，跨用户访问实测 404；用户名→工作区路径双重正则校验。
- **密钥不外泄**：API Key 不进日志；管理端只回「是否已设置」掩码；可用 `PROMPTFORGE_API_KEY` 注入避免落库；缓存键与缓存内容均不含凭证。
- **强制首登改密**：管理员建号/重置下发的口令未更换前，业务与管理接口全拦。
- **依赖锁定安全下界**：Flask / Waitress / Werkzeug / Jinja2 均锁定安全最低版本（具体版本与对应 CVE 修复见 `requirements-web.txt` 注释）。
- **版本链自愈**：`repair_consistency()` 幂等收敛指针，带 rollback 审计的指针刻意豁免。

### 5.2 已知限制与设计取舍

- **前端 CSP 需 `'unsafe-inline'`**：前端为内联脚本/样式单页，`script-src`/`style-src` 必须放开 `unsafe-inline`。
- **SSRF 属设计取舍**：增强请求的 Base URL 由各用户自填（自带 Key/自建端点是产品能力），内网自建端点为主用例；缓解手段为内网部署/反代 + HTTPS。**请勿将本服务无保护暴露到不受信网络。**
- **登录 IP 限流为内存态**：重启清零（内网单机规模够用）。
- **A/B 判定为静态指标参考**：非统计显著性检验，指标接近时输出「证据不足」而非硬给赢家。
- **单条增强状态栏不显示缓存标记**：`cached=true` 目前仅在双版对比弹窗的版本元信息行体现。
- **成本统计需配置单价**：未配置模型单价时成本列显示 —，且只对配置后新增的调用生效。

---

## 六、版本信息

- 当前版本号以 `pyproject.toml` 与包内 `__version__` 为准，代码、构建元数据与发布产物保持同一版本口径。
- 迭代摘要（按日期）：提示词版本链与 diff、评估裁判接口；同类项目检索与三轮筛选、组件级借鉴、全站视觉重构；Prompt Lint 升级、A/B 配对判定、LLM 响应缓存、统一错误分类、版本链自愈、安全加固与测试扩充。
- 完整沿革见 [CHANGELOG.md](CHANGELOG.md) 与 [docs/05-开发日志.md](docs/05-开发日志.md)。

---

## 许可与信息
开发者：智维ITMSKY
版权：Copyright (C) 2026 智维ITMSKY
官网：http://www.itmsky.com
许可证：GNU Affero General Public License v3.0 or later（AGPL-3.0-or-later，见 LICENSE）
SPDX-License-Identifier: AGPL-3.0-or-later

> 本程序是自由软件：你可以遵照 GNU Affero 通用公共许可证第3版或后续版本的规定，重新分发和/或修改它。
> 本程序旨在广泛共享，但不提供任何担保；甚至不包含适销性和特定用途适用性的默示保证。详情请参阅 GNU Affero General Public License。
> 如果你修改本程序并通过网络向公众提供服务（SaaS、Web、API等在线服务），你必须向该服务的使用者提供对应修改后的完整源代码。
> 你应当随本程序一同收到 GNU Affero General Public License 的副本；如未收到，详见 <https://www.gnu.org/licenses/>。
