# AGENTS.md — Pulse 海外社媒 Agent

> 本文件对在本目录工作的**所有 agent 生效**（主 agent 与全部子 agent）。
> 开工前必读；与本文件冲突的临时指令，以本文为准并向 root 报告。

---

## 0. 项目定位

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
3. `Pulse海外社媒Agent开发文档_v0.2.1.docx` — 原始需求与设计
4. `pulse/contracts/INTERFACES.md` — **工程接口契约（冻结，只读）**
5. `pulse/docs/多Agent协同开发方案.md` — 分工与协同规则
6. `pulse/tasks/TASKS.md` — 任务板与状态

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
| `pulse/api/`、`pulse/console/` | A5 Platform | 接口层与控制台 |
| `pulse/tests/` | A6 Verifier | **跨域**契约/集成/风控测试 |
| `pulse/shared/` | **root** | 跨域共用类型（只读给他人） |

需要改动非自己拥有的目录时：**发消息给对应所有者或 root**，不要直接改。

### 2.1 单元测试放哪（避免三方在 `pulse/tests/` 撞车）

| 测试类型 | 位置 | 所有者 |
| --- | --- | --- |
| 域内单元测试 | `pulse/services/<domain>/tests/` | 各域自己 |
| 跨域契约 / 集成 / 风控演练 | `pulse/tests/` | A6 |

**各域的单元测试不要写进 `pulse/tests/`** —— 那里是 A6 的地盘，会被覆盖。

---

## 3. 铁律

1. **不越界写。** 只创建/修改自己拥有的目录下的文件。
2. **不改契约。** `pulse/contracts/` 冻结。发现契约有问题 → 消息 root，说明冲突点与建议，等 root 更新。
3. **不动 Git。** 只有 root 执行 git 命令。子 agent 一律不运行 `git add/commit/checkout/reset`（共享工作区会争抢 index.lock）。
4. **不重排平台优先级。** P0-A = **LinkedIn + YouTube**；Reddit/Facebook = P1；**VK 冻结**（合规红线，见校准文档 §2.1）；TikTok 暂不投入。
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

### 5.1 Python 解释器

系统的 `python` / `python3` 是 **Windows Store 占位符，不可用**（执行会报"系统无法访问此文件"）。

**唯一可用的解释器**（已装好 pytest / pytest-asyncio / pydantic / celery / redis / httpx）：

```
D:\agent开发\菲美得\.venv\Scripts\python.exe
```

### 5.2 运行测试

**工作目录必须是仓库根** `D:\agent开发\菲美得`（`pyproject.toml` 在此，`pythonpath = ["."]`）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'      # 避免中文输出乱码
& "D:\agent开发\菲美得\.venv\Scripts\python.exe" -m pytest
```

导入路径为 `pulse.services.<domain>` / `pulse.shared.*`，与 §6 命名规范一致。

### 5.3 装新依赖

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'uv-cache'   # 沙箱下默认缓存目录不可写，必须改
uv pip install --python "D:\agent开发\菲美得\.venv\Scripts\python.exe" <包名>
```

**新增第三方依赖前先消息 root** —— 依赖是共享资源，不能各自添加。

### 5.4 已验证的基线

`pulse/shared/` 契约层已通过 16 项校验（含时区、平台必填项、授权状态、合规拦截、幂等约束）。
若你的改动让这些校验失败，**先怀疑自己的实现，不要改 `pulse/shared/`**。

---

## 6. 交付要求

- 每个模块必须**可独立运行**：有单元测试，测试通过才算交付。
- 公开函数/类需类型标注与简短 docstring。
- 不引入未在契约中定义的外部依赖；需要新增依赖 → 消息 root。
- 交付时在消息中报告：`改动的文件清单` / `验证方式与结果` / `未决问题`。

---

## 7. 命名规范

- 目录与文件：`snake_case`
- Python 包：`pulse.services.<domain>`
- Celery 任务名：`pulse.<domain>.<action>`，例如 `pulse.publish.dispatch`
- 数据库表与字段：`snake_case`，与 `contracts/INTERFACES.md` 完全一致
- ID 前缀：`acct_` / `b_` / `src_` / `var_` / `sched_` / `job_` / `up_`
