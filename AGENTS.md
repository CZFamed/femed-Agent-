# AGENTS.md — Pulse 海外社媒 Agent

> 本文件对在本目录工作的**所有 agent 生效**（主 agent 与全部子 agent）。
> 开工前必读；与本文件冲突的临时指令，以本文为准并向 root 报告。

---

## 0. 项目定位

### 0.1 身份与角色边界（子 agent 必读）

- 本项目有 **root（集成与治理者）** 与 **6 个子 agent（A1–A6）**。**你是子 agent，你不是 root。**
- 子 agent 的职责是**写自己域内的代码**，不是写文档、不是改治理文件、不是协调其他 agent。
- **不要 spawn 子 agent。** 需要更多人力 → 消息 root。
- **不要修改** `AGENTS.md` / `pyproject.toml` / `pulse/contracts/` / `pulse/docs/` / `pulse/tasks/` / `pulse/shared/`。
- **不要创建 `.md` 文档**（除非 root 明确要求）。产出物必须是代码与测试。

> 这条规则来自 2026-09-10 的一次真实事故：一个子 agent 继承了 root 的完整上下文后**把自己当成了 root**，
> 转而去写治理文档、修改 `pyproject.toml`、甚至"中断并重新派工"其他 agent，导致三个工作域 30 分钟内
> **零代码产出**。详见 `pulse/docs/多Agent协同开发方案.md` §7.1。

为**菲美得**构建的多平台社媒内容生成与自动发布 Agent。

| 项目 | 内容 |
| --- | --- |
| 业务基线 | **B2B 工业铸件出海**（2026-09-10 确认） |
| 产品 | 铸件 + 粗加工一体化产能（阀体、箱体/支座、管件、机床件） |
| 目标客户 | 无自有铸造厂的海外机床整机厂（印度为主，美国/台湾次之） |
| 目标市场 | 印度、美国、台湾（土耳其已标记高风险） |

---

## 1. 文档优先级（冲突时以此顺序为准）

1. `Pulse需求校准_B2B工业铸件场景.md` — **最新业务基线**，覆盖 v0.2.1 的相反结论
2. `Pulse需求拆解_v0.2.1.md` — 需求条目化结果
3. `Pulse海外社媒Agent开发文档_v0.3.docx` — 原始需求与设计
   （v0.3 已含媒体资产库与召回策略、FR-15~FR-19；v0.2.1 及更早版本已被取代并删除，
   需要查旧版从 git 历史取回）
4. `pulse/contracts/INTERFACES.md` — **工程接口契约（冻结，只读）**
5. `pulse/docs/多Agent协同开发方案.md` — 分工原理与波次设计
6. `pulse/docs/dispatch/` — **子 agent 派工单（执行层）**：开工前读自己那份
7. `pulse/tasks/TASKS.md` — 任务板与状态

---

## 2. 目录所有权（硬约束）

每个目录有唯一所有者。**只写自己的目录，不写别人的。**

| 目录 | 所有者 | 说明 |
| --- | --- | --- |
| `pulse/contracts/` | **root** | 接口契约，冻结；他人只读 |
| `pulse/docs/`、`pulse/tasks/`、根目录 `*.md` | **root** | 治理与文档 |
| `pulse/services/content/` | A1 Content | 内容生产域 |
| `pulse/services/publish/` | A2 Publish | 发布网关域 |
| `pulse/services/scheduler/` | A3 Scheduler | 调度域 |
| `pulse/services/identity/` | A3 Scheduler | 账号与凭据域 |
| `pulse/services/compliance/` | A4 Compliance | 合规与治理域 |
| `pulse/api/` | A5 Platform | 接口层 |
| `pulse/console/` | **root**（2026-09-11 起） | 图形化控制台（素材库可视化） |
| `pulse/services/media/` | **root**（2026-09-11 新增） | 媒体资产库：素材入库 + 召回策略 |
| `pulse/tests/` | A6 Verifier | **跨域**契约/集成/风控测试 |
| `pulse/shared/` | **root** | 跨域共用类型（只读给他人） |
| `pulse/services/__init__.py` | **root** | 三个域的父包（只读给他人） |
| 根级配置 `pyproject.toml`、`conftest.py`、`.gitignore` | **root** | 影响所有域，**子 agent 一律不改** |

需要改动非自己拥有的目录时：**发消息给对应所有者或 root**，不要直接改。

> 第 2 节的两个易踩点：① `pyproject.toml` 的 `testpaths` / `addopts` 是全局的，
> 改它会让**所有人的**测试收集行为变化；② 各域单测**不需要** `__init__.py`
> （已启用 `--import-mode=importlib`，跨域同名测试文件不会互相覆盖）。

### 2.1 单元测试放哪（避免三方在 `pulse/tests/` 撞车）

| 测试类型 | 位置 | 所有者 |
| --- | --- | --- |
| 域内单元测试 | `pulse/services/<domain>/tests/` | 各域自己 |
| 跨域契约 / 集成 / 风控演练 | `pulse/tests/` | A6 |

**各域的单元测试不要写进 `pulse/tests/`** —— 那里是 A6 的地盘，会被覆盖。

### 2.2 子 agent 任务名规范

spawn 时使用固定任务名，便于 root 定位与升级：

| 角色 | 任务名 |
| --- | --- |
| A1 内容生产 | `a1_content` |
| A2 发布网关 | `a2_publish` |
| A3 调度与账号 | `a3_scheduler` |
| A4 合规治理 | `a4_compliance` |
| A5 平台前端 | `a5_platform` |
| A6 独立验证 | `a6_verifier` |

每个子 agent 开工前必须读：`AGENTS.md`（本文件）→ 自己的派工单 `pulse/docs/dispatch/A*.md` → 契约相关章节。

---

## 3. 铁律

1. **不越界写。** 只创建/修改自己拥有的目录下的文件。
2. **不改契约。** `pulse/contracts/` 冻结。发现契约有问题 → 消息 root，说明冲突点与建议，等 root 更新。
3. **不动 Git。** 只有 root 执行 git 命令。子 agent 一律不运行 `git add/commit/checkout/reset`（共享工作区会争抢 index.lock）。
4. **不重排平台优先级。** P0-A = **LinkedIn + YouTube**；P1 = **Reddit + Facebook + VK**；P2 = Instagram；TikTok 暂不投入。
   **VK 已于 2026-09-11 法务评审通过、由"冻结"转为正式运营（P1）**，
   契约同步升至 v1.1（`Platform.VK`）。VK 转正附带**留痕义务**（不阻碍发布，但要有人负责）：
   每季度做一次制裁名单筛查（OFAC SDN / BIS Entity List / EU）、每个新询盘筛出口对象、
   保留俄语审校记录与 VK 独立素材台账。法务通过 ≠ 从此不用管，制裁名单是动态的。
   VK 是唯一非英语平台，对外文案为俄语、由英语母版翻译派生。
5. **合规红线。** 只用平台官方 API，**不做浏览器自动化发帖**、不绕风控；素材仅用自有/授权；不承诺"零封号"。
6. **工业件禁止用文生图生成产品图。** 会产出与实物不符的产品图，构成质量承诺风险。产品图只用实拍素材（`菲美得产品图片/`）或已加工素材。
7. **不伪造数据。** 产能、公差、材质、检测数据一律来自真实来源；缺失就标注 `TODO(need-real-data)`，不要编造数值。
8. **英文优先。** 面向客户的文案以英语为主；中英对照供审校。泰/越/俄语已移除。

---

## 4. 技术栈（已定版，勿擅改）

| 组件 | 选型 |
| --- | --- |
| 语言 | Python 3.11+ |
| Agent 编排 | LangGraph |
| 任务队列 | Celery + Redis |
| 数据库 | PostgreSQL |
| 对象存储 | S3 兼容（素材） |
| 向量库 | 已有 RAG 描述体系（`RAG知识库/`） |
| 凭据 | Vault / KMS |
| 测试 | pytest |

---

## 5. 环境与运行（**开工前必读，否则会卡住**）

### 5.0 仓库边界（先确认你在哪）

**仓库根 = `D:\agent开发\菲美得\agent`。** 所有相对路径（`pulse/…`、`pyproject.toml`、
`RAG知识库/…`）都以它为基准，命令的工作目录也必须是它。

| 位置 | 内容 | 入库 |
| --- | --- | --- |
| `agent/` | 代码、契约、治理文档、CI（仓库根） | 是 |
| `agent/RAG知识库/` | 素材描述索引，内容域选素材用 | 否 |
| `agent/.venv/` | 唯一可用的 Python 解释器 | 否 |
| `..\菲美得产品图片\` | 实拍素材（341 个文件，其中图片 314 / 视频 19），产品图唯一来源 | 否（仓库外） |
| `..\外贸公司\` | 客群调研与业务资料 | 否（仓库外） |

引用素材时基准是**工作区**（`..\菲美得产品图片\…`），不是仓库根。

### 5.1 Python 解释器

系统的 `python` / `python3` 是 **Windows Store 占位符，不可用**（执行会报"系统无法访问此文件"）。

**唯一可用的解释器**（已装好 pytest / pytest-asyncio / pydantic / celery / redis / httpx）：

```
D:\agent开发\菲美得\agent\.venv\Scripts\python.exe
```

### 5.2 运行测试

**工作目录必须是仓库根** `D:\agent开发\菲美得\agent`（`pyproject.toml` 在此，`pythonpath = ["."]`）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'      # 避免中文输出乱码
& "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider
```

导入路径为 `pulse.services.<domain>` / `pulse.shared.*`，与 §6 命名规范一致。

**`-p no:cacheprovider` 不是可选项**：多个子 agent 共享同一个工作区，同时跑 pytest 会争写同一个
`.pytest_cache`。收集范围由 `pyproject.toml` 的 `testpaths = ["pulse"]` 决定（同时覆盖
`pulse/tests/` 与 `pulse/services/*/tests/`）。

### 5.3 装新依赖

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'uv-cache'   # 沙箱下默认缓存目录不可写，必须改
uv pip install --python "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" <包名>
```

**新增第三方依赖前先消息 root** —— 依赖是共享资源，不能各自添加。

### 5.4 已验证的基线

**基线测试已存在且全绿**：`pulse/shared/tests/test_contract_baseline.py`（21 项，root 所有）。

覆盖：`Platform` 白名单（**VK 已入枚举，TikTok 不得进入**）、`scheduled_at` 时区偏移、LinkedIn / YouTube / VK 必填
`options`、素材 `license_status`（禁止 `pending` 发布）、`compliance.blocked` 硬拦截、hashtag 规范、
`PublishResult` 的"受理 ≠ 发布"断言、`ErrorClass` 三分法（可重试 / 不可重试 / 轮询）、ID 前缀与时间前缀单调性、序列化。

```powershell
& "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider pulse/shared/tests
```

若你的改动让这些校验失败，**先怀疑自己的实现，不要改 `pulse/shared/`**（属 root 所有）。
确信某条断言与 `contracts/INTERFACES.md` 冲突 → 消息 root，附契约条款编号。

> **历史澄清（避免再次误传）**：本文曾写"`pulse/shared/` 已通过 16 项校验"，但当时仓库中并不存在测试文件；
> 随后又被改写成"当前没有任何自动化测试"。两者都不准确。**以上面这段为准**——基线是 21 项（v1.1 版），位置在
> `pulse/shared/tests/`，且属于 root 自己的域（各域单测仍在 `pulse/services/<域>/tests/`，A6 的跨域测试在 `pulse/tests/`）。

---

## 6. 交付要求

- 每个模块必须**可独立运行**：有单元测试，测试通过才算交付。
- 公开函数/类需类型标注与简短 docstring。
- 不引入未在契约中定义的外部依赖；需要新增依赖 → 消息 root。
- 交付时**必须**按 `pulse/docs/dispatch/README.md` §4 的模板回报，六节缺一不可：
  `改动文件清单` / `验证方式与结果` / `契约对齐声明` / `未决问题` / `越界声明` / `依赖请求`。
  其中"验证方式与结果"必须是**可直接复制运行的命令 + 真实输出**——没有它，"已完成"一律退回。

---

## 7. 命名规范

- 目录与文件：`snake_case`
- Python 包：`pulse.services.<domain>`
- Celery 任务名：`pulse.<domain>.<action>`，例如 `pulse.publish.dispatch`
- 数据库表与字段：`snake_case`，与 `contracts/INTERFACES.md` 完全一致
- ID 前缀：`acct_` / `b_` / `src_` / `var_` / `sched_` / `job_` / `up_`
