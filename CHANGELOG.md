# Changelog

本文件记录 PromptEnhancer（提示词工坊 PromptForge）的显著变更（新增在前）。

## [2026-11-13] v0.1.0 · 项目整理为「纯代码 + 文档」初始版本（src 布局重构）

> 版本号保持 **v0.1.0** 不升号；本轮为目录结构与文档重组，无功能变更。

### Changed（目录结构重组）
- **标准 src 布局**：全部源码移入 `src/` 目录——`src/promptforge/`（核心包）与 `src/forge/`（指令构造引擎）；`webapp.py` 移入 `src/promptforge/webapp.py`；前端 `web/` 移入 `src/promptforge/web/`。
- **移除冗余入口**：删除根目录 `main.py`（其逻辑与 `promptforge/__main__.py` 完全等价），统一使用 `python -m promptforge` 启动桌面版/CLI。
- **移除测试目录**：按项目方要求，初始版本不含 `tests/`（原 158 例测试代码不纳入交付包）。

### Changed（代码适配 src 布局）
- **`database.py` BASE_DIR 路径**：`database.py` 位于 `src/promptforge/data/` 下，`_APP_DIR` 向上取目录层级由 3 级调整为 4 级，开发态数据目录仍正确落到项目根 `data/`。
- **`webapp.py` 导入改为相对导入**：移入包内后，`from promptforge.*` 全部改为 `from .*` 相对导入；`forge` 跨包引用保持绝对导入。
- **文档字符串路径更新**：`promptforge/__init__.py`、`promptforge/cli.py`、`forge/__main__.py`、`forge/cli.py`、`webapp.py` 中的 `python main.py` 引用统一改为 `python -m promptforge` / `python -m forge`。

### Added（新增构建配置）
- **`pyproject.toml`**：声明 src 布局（`package-dir = { "" = "src" }`）、`packages.find`、`package-data`（providers.json + web 前端资源）、三个 console_scripts 入口（`promptforge` / `promptforge-web` / `forge`）、可选依赖组（`web` / `desktop`）。

### Changed（构建/部署配置更新）
- **`Dockerfile`**：`COPY src ./src`，`ENV PYTHONPATH=/app/src`，`CMD` 改为 `python -m promptforge.webapp`。
- **`build_exe.bat`**：PyInstaller 入口改为 `src/promptforge/__main__.py`，新增 `--paths src`，`--add-data` 路径改为 `src/promptforge/config/providers.json`。
- **`PromptForge.spec`**：`Analysis` 入口脚本改为 `src/promptforge/__main__.py`，`pathex` 设为 `['src']`，`datas` 路径同步调整。
- **`.dockerignore`**：更新排除清单，排除 `pyproject.toml`、`requirements.txt`、部署脚本等非运行时文件。

### Changed（文档整合为 5 篇）
- 原 10 篇文档（需求规格 / 架构设计 / 使用说明 / 安全审查报告 / 开发日志 / 测试方案 / 部署运维 / 版本迭代说明 / 同类项目借鉴 / forge_sample_design）整合为 5 篇：
  - `docs/01-项目介绍与架构.md`（项目概述 + 架构设计 + 数据模型）
  - `docs/02-环境部署教程.md`（环境要求 + 部署 + 配置 + 故障排查）
  - `docs/03-功能使用说明.md`（各入口用法 + 新功能操作 + 已知限制）
  - `docs/04-安全设计说明.md`（安全架构 + 鉴权 + 输入校验 + 渗透验证）
  - `docs/05-开发日志.md`（BUG 修复 + 功能新增 + 版本沿革 + 同类项目借鉴）
- 删除 `DOCKER.md`（内容已整合入部署教程）。

### Verified（验证）
- `import promptforge` 成功；`python -m promptforge --help` 可运行；`python -m forge --help` 可运行；`python -m promptforge.webapp` 可启动并响应 `/healthz`。

## [2026-11-10] v0.1.0 · 正式化安全审查加固（第四轮）

> 版本号保持 **v0.1.0** 不升号；本轮为只读审计 + 低风险修复，无功能变更。

### Fixed（安全加固，均保持既有测试全量通过）
- **登录失败计数原子化**（`promptforge/data/users.py` `_bump_fail`）：原「读-改-写」
  两段式在并发登录失败时可能互相覆盖计数，导致锁定阈值被延迟/丢失；改为单条
  原子 UPDATE（CASE 以旧行值为基准），语义不变（第 5 次失败即锁 15 分钟）。
- **登录计时匀势**（`users.authenticate`）：用户不存在时也执行一次同等成本的
  PBKDF2（导入期一次性预算哑哈希），消除「账号存在与否」的响应时间差，
  封堵登录接口的账号枚举计时侧信道；文案仍统一「用户名或密码错误」。
- **会话 Cookie Secure 可配置**（`webapp.py`）：新增 `WEB_COOKIE_SECURE=1` 开关
  （默认关闭，行为不变），HTTPS/反代部署开启后浏览器仅在加密连接回传会话
  Cookie；`.env.example` 补充该项说明。

### Security（审计结论，详见 docs/安全审查报告.md 第四轮追加）
- 全量静态审计（webapp 全部 API / data 层 / core / forge）+ 隔离实例动态渗透
  （96 项用例）：未发现注入/越权/信息泄露/逻辑绕过漏洞；SQL 全参数化、
  路由权限矩阵完整、跨用户数据经工作区库级隔离实测 404、登录锁定+IP 限流
  实测生效、超大 payload 全部 400 拦截、并发固化无撞号/无 500、日志无敏感信息。

## [2026-11-08] v0.1.0 · 第三轮迭代（同类项目借鉴 + 全站视觉重构「黄铜工坊」）

> 版本号保持 **v0.1.0** 不升号（按本轮约束）；本条目记录第三轮迭代内容。

### Added（新增功能，组件级借鉴落地，详见 docs/同类项目借鉴.md）
- **Prompt Lint 静态体检（P0-1）**：新模块 `promptforge/core/lint.py`（纯本地规则，
  零模型调用）：模糊措辞检测（尽量/适当/尽可能/一些/之类/等等…）、缺角色、
  缺输出格式约束、输入过短/过长/超上限、未替换模板变量；`POST /api/lint`；
  前端「体检」实时条（输入即检、0-100 参考分、问题 chip 列表、可忽略不阻断）。
- **A/B 双版对比（P0-2）**：同一输入按两种风格并行各增强一次（两次都照常落库），
  弹窗双栏并排 + 字数/token/耗时三项量化对比 + 「采用此版」一键采纳（借鉴
  prompt-optimizer / gpt-prompt-engineer 的 human-in-the-loop 选版模式）。
- **键盘快捷键（P0-3）**：`Ctrl+Enter` 增强/批量/生成五槽 · `Ctrl+K` 聚焦打磨档案
  检索 · `/` 非输入态聚焦检索 · `Esc` 关闭模态 · 输入计数器（单条页双框）。
- **Toast 通知（P0-4）**：关键操作（保存/复制/增强/批量/评估/收藏）右上角气泡。
- **最近复用快捷 chips（P0-5）**：单条页顶部基于 /api/history 渲染最近 5 条输入，
  一键回填输入框。
- **场景预设（P0-6）**：单条页内置 5 个场景预设（代码助手/写作润色/专业翻译/
  SQL 生成/代码评审），选择后预填输入框（借鉴 dify 场景模板库）。
- **forge 模板变量记忆（P0-7）**：{{变量}} 输入值自动 localStorage 持久化，
  重新生成时自动回填最后填写值（借鉴 pezzo 变量体验）。
- **历史 Markdown 导出（P0-8）**：`/api/history/export?format=md` → 结构化 .md
  （每条一个小节：时间/厂商/风格/输入/输出代码块）；前端导出按钮旁新增格式下拉。
- **历史收藏（P1-1）**：`enhance_records` 惰性补列 `favorite`（INTEGER NOT NULL
  DEFAULT 0）；`POST /api/history/<rid>/favorite`（本人可切换、按工作区隔离）；
  列表/检索新增 `favorites=1` 过滤；历史页每条星标按钮 +「仅看收藏」开关。
- **评估分数趋势折线（P1-2）**：版本链面板内置 SVG 折线图（准确性/完整性/
  清晰度三线，≥2 条带评分版本时展示，纯前端零依赖）。
- **diff 复制为 Markdown（P1-3）**：unified diff 包进 ```diff 围栏一键复制
  （在 ver-modal 内，最近一次 diff 后可用）。
- **常见问题折叠区（P1-4）**：单条增强页内置失败诊断树（鉴权 400 / 限流 429 /
  空结果 / 超时 504 / 低质量）。

### Changed（视觉重构，详见相同文档）
- **全站视觉重构「黄铜工坊 · Brass Workshop」**：铜棕主色 #A1550F + 熔金点缀
  #C89B3C + 暖砂白底 #F5F3EE + 暖炭墨标题 #26211C，衬线展示字体（Georgia/
  Noto Serif SC）+ 内联线性 SVG 图标（侧边栏/按钮/状态/模态全量替换 emoji），
  页面名称定版：单条提示增强（01）/ 批量提示增强（02）/ 打磨档案（03）/
  五槽指令铸造（04）/ 工坊管理台（05）；顶部面包屑「PROMPT FORGE › 编号」
  定位；侧边栏新增头像/角色徽标；状态栏改为带跳动圆点的工作台 Ticker；
  登录页重构为「门厅+登录台」双栏（左侧工坊特性清单，右侧进入工坊/开通工位）。
  功能、ID、交互逻辑、业务流程全部保持不变；全浅色、无暗色主题。
- 桌面版 customtkinter 调色板同步换为黄铜工坊体系（PRIMARY #A1550F 等），
  forge 指令构造面板与共享 widgets 一并对齐。
- 浏览器验证：登录/主界面/单条（lint 打分实时）/历史/管理台 5 个视图全部
  渲染正常，**console 0 错误 / 0 警告**；lint 端到端调用确认（score=20，
  6 条 issue 全部列出）。

### Fixed（BUG 修复）
- **`promptforge/core/__init__` 重导出 `lint` 函数遮蔽子模块名**：早期实现
  `from .lint import lint` 后 `from promptforge.core import lint` 拿到的是函数
  而非模块；新编测试 8 例随即暴露（lint_core.lint AttributeError）。现去掉该
  重导出，调用方统一 `from promptforge.core import lint`（模块）→ `lint.lint()`。
- 无用户侧 BUG 被报告/发现；既有用例回归全部通过。

### Security（本轮复核，详见 docs/安全审查报告.md 追加节）
- 新增接口全部 `@auth_required` 且复用工作区隔离（收藏接口按 get_record 存在性
  404 拦截，越权不可达）；lint 接口为纯本地规则，无 SQL/无 IO/无模型调用；
  导出 Markdown 格式白名单（csv|md，非法 400）；lint chip 等级以白名单渲染；
  评估趋势图分数做数值强制转换；无新增第三方依赖；无新增外部网络调用。
- 前端新增内容（chips/收藏星/预设/模态/趋势图）全部经 `esc()` 转义或 textContent。
- 既有安全基线（PBKDF2/锁定/限流/SameSite=Lax/HttpOnly/nosniff/DENY/CSP/依赖下界）
  全部保留。

### Verified（验证）
- `python -m pytest tests/ -q --basetemp=.pytest-tmp`：**104/104 通过**
  （基线 90 + 第三轮新增 14：lint 规则/收藏惰性迁移/toggle/过滤/MD 导出/模块导入）。
- 浏览器端到端：admin 三角色登录走通（口令+密码+平台口令）；
  lint/preview/保存/收藏/双版对比/版本链面板均可用；HTTP 200，0 console 错误。

## [2026-10-28] v0.1.0 · 第二轮迭代（F2 版本链与 diff + 全量排查修复）

> 版本号保持 **v0.1.0** 不升号（按本轮约束）；本条目记录第二轮迭代内容。

### Added（新增功能）
- **提示词版本链（F2）**：新增 `prompt_versions` 表（挂在历史记录上，
  UNIQUE(record_id, version_no) 防并发撞号、parent_id 成链、source 标记
  init/manual/enhance、created_by/created_at/model 元数据、score 评估列）与
  `version_audit` 回滚审计表；`enhance_records` 增 `current_version_id` 指针列。
  存量库由 `ensure_version_schema()` 惰性补列补表（历史页/版本接口入口统一
  先行调用），升级无感。版本行不可变：任何修改=插新行（version_no=MAX+1，
  IntegrityError 重试一次后按 409 返回）；首个版本且与记录原始输出一致自动判
  `init`；与当前版本内容+槽值完全一致拒绝 400「内容无变化」。新模块
  `promptforge/data/versions.py`（纯标准库）。
- **版本接口**：`POST/GET /api/history/<rid>/versions`（固化/列表，倒序+
  当前指针标记）、`POST /api/history/<rid>/versions/<vid>/activate`（指针式
  回滚，不产生新行，写审计+日志）、`GET /api/history/<rid>/diff?left&right`
  （结构化并排行对 + 槽值段逐槽统计 + 统一 diff 文本，left==right →
  `{identical:true}`）；管理员只读可见
  `/api/admin/users/<u>/history/<rid>/versions` 与 `/diff`（写操作仅限本人）。
- **diff（stdlib difflib，零新依赖）**：`ndiff` 解析为与调研同构的并排行对
  （added/removed/unchanged，空行单空格占位对齐）、`SequenceMatcher.get_opcodes`
  对改动行做字符级行内高亮（≤2000 字符行才附加，控 payload）、
  `unified_diff` 统一格式文本、槽值先 `json.dumps(sort_keys=True)` 规范化再
  逐槽统计（缺槽按空串对齐）。
- **评估裁判（加做项）**：`POST /api/history/<rid>/evaluate` 复用当前用户
  已配置模型对指定历史记录打分（准确性/完整性/清晰度各 1-10 + 一句建议，
  严格 JSON 输出+容错解析：剥代码围栏、字符串数字折算、分数钳位 1-10）；
  模型失败/输出非法降级为明确错误提示（502/400/429/504 按既有映射）；
  有当前版本时评分入版本链元数据并写审计。引擎新增 `raw_completion`
  （自由 system/user 调用，与增强共用 `_chat_request`）。
- **前端**：历史页每条记录新增「⛓ 版本链」「⭐ 评估」入口与版本链徽标；
  版本链面板（固化新版本表单：内容/说明≤80 字/来源/模型、版本列表 v{no}+
  来源徽标+当前 tag、回滚确认、双栏并排 diff+行内高亮+槽值统计+统一 diff
  折叠区）；单条增强成功后新增「⛓ 存为版本」按钮（/api/enhance 响应新增
  `record_id` 锚点，批量结果亦带）。

### Fixed（BUG 修复，详见 docs/开发日志.md 追加节）
- **上游 200 但响应非 JSON 裸抛 500**：`engine._http_post` 原把
  `json.loads`/`decode` 放在传输异常捕获范围内，网关返回 HTML 错误页时
  JSONDecodeError 直接穿透为未处理异常；现归一为 EnhanceError 走既有失败
  提示链路。
- **畸形 choices/usage 结构 AttributeError 500**：`choices[0]` 为 None 或
  `usage` 非对象时原实现 `.get` 直接崩；现 `_chat_request` 逐层 isinstance
  安全解析，异常响应按空内容处理。
- **workspace_stats 副作用建空库**：账号目录存在但库文件缺失时，
  `sqlite3.connect` 会静默创建空库文件污染磁盘；现先判存在再连接。
- **增强输入无长度上限**：单条/批量每条输入原来不设限，超长文本会全量发往
  模型并入库；现统一上限 `MAX_INPUT_CHARS = 100_000` 字符，超限 400 且不调用
  模型。

### Verified（验证）
- `python -m pytest tests/ -q --basetemp=.pytest-tmp`：**90/90 通过**
  （基线 56 + 本轮新增 34：版本入链/回滚/diff 输出/越权拦截/旧库惰性迁移/
  级联删除/评估裁判/引擎健壮性回归）。本机 Windows 环境默认 pytest 临时目录
  存在权限问题（pytest-of-*/pytest-current 解锁失败），需按项目惯例加
  `--basetemp=.pytest-tmp` 执行，结论不受影响。
- 新增功能安全自查：SQL 全参数化、版本读写入口均经 auth_required + 工作区
  隔离（越权读/写/diff/评估 404 拦截有测试）、管理路径 admin_required（403
  有测试）、source 白名单、content/commit_message/槽值长度上限、评分钳位、
  前端用户数据全量 esc() 转义。详见 docs/安全审查报告.md 追加节。

## [v0.1.0] - 2026-10-12 · 正式稳定版（内部 3.x 线归一）

> **版本口径说明**：自本条目起，对外统一使用 **v0.1.0** 单一正式版本号，
> 此前的内部 3.x 线（3.0 多用户 → 3.1 后台 → 3.2/3.2.1 自助注册 → 3.3
> 用量/检索/模板变量）以及更早的 v1.x noVNC 桌面方案、v2.0 Flask 单口令
> Web 版全部归并入这一正式版。完整沿革线索见下方历史条目与
> `docs/版本迭代说明.md`。本轮基于 3.3.0 全量排查（39 条 / 37 个独立问题，见
> `docs/开发日志.md`）完成 BUG 修复、安全加固与文档交付。

### Fixed（BUG 修复，详见 docs/开发日志.md）
- **用量统计对脏 usage 值的容错**：模型响应返回非数字 token 值（如
  `"prompt_tokens": "abc"`）时，原实现 `int()` 抛 ValueError 且外层仅捕获
  sqlite3.Error，会令**已成功**的增强请求返回 500。现 `_record` 安全整数化
  （`_safe_int`）、`usage.record_usage` 全异常兜底（告警不抛），单价折算
  意外失败该行 cost 记 NULL 不丢明细。
- **AuthError 状态码归位**：模型上游鉴权失败（Key 错误）原返回 502
  （"服务器内部错误"语义），现归位 400（客户端配置问题）；刻意不映射
  401 以免前端误判登录失效跳转登录页。
- **指令构造控制器有界缓存**：按用户缓存的控制器原只增不减（仅登出/
  删号清理），长期运行内存缓涨；现上限 200 条、超出按插入序淘汰最旧。
- **构造器文档名截断**：`/api/forge/parse` 的 name 参数截断至 200 字符，
  防超长字符串进入提示词正文。

### Security（安全加固，详见 docs/安全审查报告.md）
- 会话 Cookie 显式 `HttpOnly` + `SameSite=Lax`（配合全部写接口强制 JSON
  Content-Type，CSRF 双保险）。
- 统一安全响应头：`X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、
  `Referrer-Policy: no-referrer`、`Content-Security-Policy`（服务器渗透验证阶段补齐，
  与其余四系统同一基线；内联脚本取舍见安全报告遗留风险）。
- 登录接口新增**每 IP 失败限流**（20 次/10 分钟，仅计失败）：阻断换用户
  名字典慢速喷洒绕过账号级 5 次锁定的口令猜测路径。
- **用户名→工作区路径纵深防御**：`users.workspace_dir` 按建号同款正则二次
  校验，即便 pf_users 被绕过应用直接篡改注入 `../x` 之类用户名也不会拼出
  越界路径；`global_stats`/`workspace_stats` 对非法用户名计 broken/零值，
  不中断聚合与列表。
- 依赖锁定安全下界：`flask>=3.1.3`（CVE-2025-62465）、`waitress>=3.0.2`
  （CVE-2024-49768）、显式传递依赖 `werkzeug>=3.1.3`（CVE-2025-47278 /
  CVE-2025-5365）、`jinja2>=3.1.6`（CVE-2025-27516）；customtkinter 无已知
  CVE（核查记录见安全报告）。

### Added（3 项小而实用的新增，详见 docs/开发日志.md）
- **会话过期友好提示**：登录态失效时前端跳 `/login?expired=1` 并显示
  「登录已过期，请重新登录」。
- **历史档案状态筛选**：新增「全部/仅成功/仅失败/仅低质量」下拉，与全文
  检索叠加生效（前端过滤，零接口改动）。
- **管理员账号列表过滤**：用户管理页新增关键字输入框，按用户名/角色/状态
  即时过滤（前端过滤）。
- 新增 `tests/test_v010_fixes.py`（20 例：usage 容错/防穿越/登录限流/Cookie
  属性/安全响应头/AuthError 状态码/文档名截断/有界缓存/越权 403）。

### Changed
- 版本号 3.3.0 → **v0.1.0**：`promptforge.__version__`、docker-compose 镜像
  标签 `promptforge:v0.1.0`（并移除已废弃的 compose `version:` 字段）、
  README/DOCKER.md 及代码注释中的内部版本标记全部归一。
- 批量增强前端增加规模确认：>20 条时弹确认框提示耗时口径（服务端 200 条
  上限不变）。
- README.md 重写为 Web + 桌面双形态项目说明；DOCKER.md 含 3.3.0→v0.1.0
  升级注意。

### 升级注意（3.3.0 → v0.1.0）
- **无需任何数据迁移**：表结构未变，`data/` 目录原样沿用。
- 镜像标签变更：`docker compose build` 后新镜像为 `promptforge:v0.1.0`；
  已推私服的环境需重新打标签推送。
- 升级后首次登录若遇 "尝试过于频繁" 为新登录限流生效（10 分钟窗口），
  正常使用不会触发。
- 回滚：代码还原 3.3.0 + compose 镜像标签改回即可；本轮无库结构变更，
  新增数据（usage 容错后的 0 值行）对旧版只读无害。

## [3.3.0] - 2026-10-12 · 用量统计 / 模板变量 / 历史全文检索

### Added
- **F1 Token 用量与成本统计**：全局库（data/promptforge.db）新增 `api_usage`
  明细表（user_id/model/prompt_tokens/completion_tokens/total_tokens/cost/created_at），
  Web 端每次增强成功记一行（模型响应缺失 usage 按 0 记，不报错；失败调用不记）。
  新增本人用量接口 `GET /api/usage`（按模型/按日近 30 天）、管理员全员汇总
  `GET /api/admin/usage`（总览+按用户+按模型）；历史页新增「Token 用量」卡，
  管理后台新增「全员 Token 用量」卡。成本可空：仅当管理员通过
  `GET/POST /api/admin/usage-prices` 配置了模型单价（存 pf_settings.usage_prices，
  单位 元/百万 tokens）后，新增调用才计 cost，未配置为 NULL，不写死单价表。
- **F5 五槽构造器支持模板变量**：槽位/文档内容支持 `{{变量名}}` 占位符。
  首次生成时服务端检测变量清单并返回，前端在构造界面动态列出输入框，
  提交后用 jinja2 严格渲染（StrictUndefined，缺变量报「存在未填变量：xx」）。
  安全边界：变量名仅允许 `[A-Za-z0-9_]`（正则校验）；渲染前拒绝 `{%`/`{#`
  语句/注释语法，形如 `{{...}}` 但非纯标识符的片段一律拒绝（防模板表达式
  注入）；不含变量的模板完全不走渲染路径，行为与旧版一致。
- **F7 历史档案全文检索**：优先 SQLite FTS5（trigram 分词，支持中文子串，
  需 sqlite >= 3.34）建 `hist_fts` external-content 索引虚表，由 AFTER
  INSERT/DELETE/UPDATE 触发器随 enhance_records 同步增删改，清空历史自动
  清索引，行数不一致（如恢复备份）时自动 rebuild；环境不支持 fts5/trigram
  时自动回退 LIKE 全表扫描（%/_ 转义）。历史页新增搜索框，输入即查（300ms
  防抖），只检索本人历史；trigram 需 >=3 字符，更短关键字自动退 LIKE。
- 新增 `tests/test_v330_features.py`（F5 渲染校验 8 例 / F7 检索 6 例 /
  F1 用量落库 1 例，兼容 FTS 与 LIKE 两种模式）。

### Changed
- 版本号 3.2.1 → 3.3.0；docker-compose.yml 镜像标签 promptforge:3.3.0；
  DOCKER.md 补三项新功能用法简述。
- `webapp._record` 在成功增强后额外写一行用量明细（统计失败仅告警，
  不影响增强主流程）；`webapp.main` 启动时建 api_usage 表（幂等）。
- 数据层 `load_records` 行映射抽取为 `_record_dicts` 供检索复用，行为不变。

### 回滚说明
- 代码回滚到 3.2.1 即可（还原本次改动文件）；数据层向前兼容，新增对象
  留在库中不影响旧版本运行。如需彻底清理：
  - 全局库执行 `DROP TABLE IF EXISTS api_usage;`
  - 各用户工作区库执行 `DROP TABLE IF EXISTS hist_fts;` 并删除
    `hist_fts_ai` / `hist_fts_ad` / `hist_fts_au` 三个触发器；
  - `pf_settings.usage_prices`（模型单价）回滚后不再被读取，可留可删。
- 回滚后影响：用量不再累计（3.2.1 不写 api_usage）、历史页搜索框消失
  （数据无损）、模板变量占位符按普通文本输出。

## [3.2.1] - 2026-10-11 · 拆分为独立 Docker 项目

### Fixed
- **修复 Dockerfile 依赖装错**：Web 版误装桌面版 `requirements.txt`（customtkinter），
  容器启动即 `ModuleNotFoundError: flask`；现改为安装 `requirements-web.txt`
  （Flask + Waitress），与 webapp.py 实际依赖一致。

### Added
- 新增 `deploy.sh`（自动生成 .env、随机生成平台/管理员口令并回显）与 `deploy.bat` 独立一键部署脚本。
- 新增本 CHANGELOG。

### Changed
- Dockerfile pip 依赖层增加 `PIP_INDEX_URL` 构建参数；compose 默认传清华镜像，
  保证国内网络可构建。
- DOCKER.md 重写为自包含部署文档，补齐口令矩阵（访问口令/admin 初始口令）、数据备份与构建源说明。

### 影响
- 本目录自此可独立交付部署，不依赖上级目录的 deploy.sh / README.md。
