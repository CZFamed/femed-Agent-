# Pulse 开发任务板

> 维护人：**root**　｜　更新：2026-09-10
> 状态：`TODO` / `IN_PROGRESS` / `BLOCKED` / `VERIFY`（待 A6 验证）/ `DONE`
> **子 agent 不修改本文件**，完成任务后在交付消息中报告，由 root 更新。

---

## 当前波次：W1（进行中）

| 域 | Agent 任务名 | 状态 | 启动时间 |
| --- | --- | --- | --- |
| A1 内容生产 | `/root/a1_content` | **IN_PROGRESS** | 2026-09-10 |
| A2 发布网关 | `/root/a2_publish` | **IN_PROGRESS** | 2026-09-10 |
| A3 调度与账号 | `/root/a3_scheduler` | **IN_PROGRESS** | 2026-09-10 |

W1 三个域的**开工指令与验收标准**见派工单 `pulse/docs/dispatch/A1_content.md` / `A2_publish.md` / `A3_scheduler_identity.md`
（**执行层唯一真源**；`多Agent协同开发方案.md` §9 为速查，冲突时以派工单为准，见该文 §9.8）。
并发位上限 4（含 root），W1 已占满——**W1 期间不得再拉 A4/A5/A6 进场**。

W1 全部交付后 → 进入 W2（A4 合规 / A5 平台前端 / A6 验证）。

---

## W0 · 契约与治理（Root）

| ID | 任务 | 负责 | 状态 | 交付物 |
| --- | --- | --- | --- | --- |
| W0-1 | 接口契约冻结 v1.0 | Root | **DONE** | `pulse/contracts/INTERFACES.md` |
| W0-2 | 治理文件与所有权约束 | Root | **DONE** | `AGENTS.md` |
| W0-3 | 协同方案与波次计划 | Root | **DONE** | `pulse/docs/多Agent协同开发方案.md` |
| W0-4 | 目录骨架与 shared 类型 | Root | **DONE** | `pulse/shared/`（4 文件，导入与契约版本校验通过） |
| W0-6 | 子 Agent 任务书（分工落地到可验收粒度） | Root | **DONE** | 协同方案 §9 |
| W0-7 | 根级共享文件归属 + pytest 收集口径 | Root | **DONE** | `pyproject.toml` `testpaths=["pulse"]`、`pulse/services/__init__.py` |
| W0-8 | 共享契约基线测试常驻化 | Root | **DONE** | `pulse/shared/tests/test_contract_baseline.py`（**21 项**，全绿；v1.1 起为 21 项） |
| W0-9 | 子 Agent 派工单（执行层） | Root | **DONE** | `pulse/docs/dispatch/`（README + A1–A6 六份） |
| W0-5 | Git 仓库初始化与首次提交 | Root | **DONE** | `main` 分支；提交见文末"提交记录" |
| W0-10 | 版本元数据统一为 **V1.0.0** | Root | **DONE** | `pyproject.toml`、`pulse/__init__.py`、`test_release_version.py` |
| W0-11 | 变更记录与仓库说明 | Root | **DONE** | `CHANGELOG.md`、`README.md`、`.gitattributes` |
| W0-12 | 发布标签与 CI | Root | **DONE** | 标签 `v1.0.0`；`.github/workflows/ci.yml` |

### W0-9 说明（分工定稿）

分工已落成两层：`pulse/docs/多Agent协同开发方案.md` 讲**为什么这么切**（原理层），
`pulse/docs/dispatch/` 讲**每个 agent 具体干什么、怎么算干完**（执行层）。

- 真源优先级：`AGENTS.md` > `contracts/INTERFACES.md` > `dispatch/A*.md` > 协同方案 §9 > 本任务板。
- 派工单含协同方案没有的**已核实事实**：素材库计数（**318 份素材描述 + 13 份汇总索引**）、
  描述文件 `source_folder` 的路径重映射陷阱、平台接入现状表、YouTube 配额上限。
- 交付报告模板统一为 `dispatch/README.md` §4 的**六节**（改动文件清单 / 验证方式与结果 /
  契约对齐声明 / 未决问题 / 越界声明 / 依赖请求）。
- **本轮治理修正**：`AGENTS.md` §5.4 的"当前没有任何自动化测试"与 `dispatch/A6_verifier.md` §4
  的"基线尚未建立"均为**过时表述**，已按 W0-8 的实际交付（基线测试）修正。

### W0-7 说明（影响所有域的根级改动）

首轮并行时，`pyproject.toml` 的 `testpaths` 从 `["pulse/tests"]` 改为 `["pulse"]`。

- **为什么必须改**：域内单测在 `pulse/services/<域>/tests/`，若 `testpaths` 只列 `pulse/tests`，
  各域单测会被**静默排除**——`pytest` 依然显示绿色，制造典型"虚假完成"。
- **归属**：`pyproject.toml` / `conftest.py` / `.gitignore` / `pulse/services/__init__.py` 一律归 **root**。
  子 agent 需要变更 → 消息 root（本次改动已由 root 追认）。
- **域内自测命令**：`& ".venv\Scripts\python.exe" -m pytest pulse/services/<域>`

### W0-8 说明（子 agent 必读）

- 共享层基线测试落在 **`pulse/shared/tests/`**（root 自己的域），共 **21 项**，覆盖：枚举白名单、
  平台必填项、时区偏移、素材授权、合规硬拦截、`受理≠发布` 约束、错误分级三分法、ID 前缀与时间前缀单调性、序列化。
- **若你的改动让 `pulse/shared/tests/` 失败，先怀疑自己的实现**，不要改 `pulse/shared/`。
- 例外：这些断言里断言的是**契约语义**而非实现细节。若你确信某条断言与 `contracts/INTERFACES.md` 冲突，
  消息 root 并附契约条款编号。
- 原 `pulse/services/content/tests/test_collection_probe.py`（root 的临时收集探针）已删除，
  其职责由本测试文件接管——**不要重新创建探针**。

---

## W1 · 三大主域（并行）

### A1 · 内容生产域

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W1-A1-1 | 服务骨架 + Python 包结构 | TODO | W0-4 |
| W1-A1-2 | brief 解析与 Pydantic 模型（严格对齐契约 §6） | TODO | – |
| W1-A1-3 | 平台 Prompt 模板：**LinkedIn + YouTube 优先** | TODO | – |
| W1-A1-4 | 话题标签库（品类词白名单，B2B 工业向） | TODO | – |
| W1-A1-5 | 素材选用器：从 `RAG知识库/图片描述/` 索引按语义挑实拍素材 | TODO | – |
| W1-A1-6 | 模型路由 + token 预算熔断（默认 60k，可配置） | TODO | – |
| W1-A1-7 | Variant 派生与落库（共用 source_id） | TODO | – |
| W1-A1-8 | 单元测试（生成链路用固定文案桩，不调真模型） | TODO | – |

### A2 · 发布网关域

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W1-A2-1 | 网关骨架 + `PlatformAdapter` 抽象基类（严格按契约 §4） | TODO | W0-4 |
| W1-A2-2 | `UnifiedPost` 模型与校验 | TODO | – |
| W1-A2-3 | LinkedIn Adapter（公司页，`author_urn` + visibility） | TODO | – |
| W1-A2-4 | YouTube Adapter（resumable upload + `pending_finalize`） | TODO | – |
| W1-A2-5 | Fake Adapter（供其他域与测试使用） | TODO | – |
| W1-A2-6 | 幂等：`unified_post_id` 透传 + `find_existing()` 兜底 | TODO | – |
| W1-A2-7 | 错误分级 → `ErrorClass` 映射与重试策略 | TODO | – |
| W1-A2-8 | 异步回执状态机（`pending_finalize` + 超时告警） | TODO | – |
| W1-A2-9 | **半自动导出器**（P0 能力，不得省略） | TODO | – |
| W1-A2-10 | 单元测试（全部走 Fake Adapter，不发真实请求） | TODO | – |

### A3 · 调度与账号域

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W1-A3-1 | Celery + Redis 接线（含 eager 测试模式） | TODO | W0-4 |
| W1-A3-2 | 延迟任务投递（`eta`/`countdown`，**不用 cron**） | TODO | – |
| W1-A3-3 | 令牌桶限流 + 发布冷却 | TODO | – |
| W1-A3-4 | **配额预检**（YouTube 1600 单位/次、日配额 10000 的硬约束） | TODO | – |
| W1-A3-5 | best-time 表结构与查询（按 account/platform/timezone/weekday/slot） | TODO | – |
| W1-A3-6 | 账号模型 + OAuth 流程骨架 | TODO | – |
| W1-A3-7 | Token 生命周期（access/refresh 双过期 + 提前刷新 + 吊销） | TODO | – |
| W1-A3-8 | Vault/KMS 加密接口（业务层不可见明文，永不落日志） | TODO | – |
| W1-A3-9 | 账号停用熔断（即时挂起该账号全部任务） | TODO | – |
| W1-A3-10 | 单元测试 | TODO | – |

---

## W2 · 合规、平台与验证（并行）

### A4 · 合规与治理域

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W2-A4-1 | 规则引擎骨架 + `compliance_findings` 结构化输出（rule/severity/message/position） | TODO | – |
| W2-A4-2 | 版权规则（自有素材优先，入库需 `license_status`） | TODO | – |
| W2-A4-3 | 敏感内容词库（B2B 工业向，最小集） | TODO | – |
| W2-A4-4 | 平台政策与质量规则（反机器味、字数、emoji 上限） | TODO | – |
| W2-A4-5 | **制裁与出口管制筛查**（OFAC/BIS，`sanctions_screenings` 表） | TODO | – |
| W2-A4-6 | `block` / `warn` 与豁免留痕 | TODO | – |
| W2-A4-7 | 单元测试 | TODO | – |

### A5 · 平台与前端

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W2-A5-1 | API 骨架 + 11 个端点（路径按契约 §7 冻结） | TODO | – |
| W2-A5-2 | 审批控制台（通过/驳回/改稿/整批，含强制单条复核规则） | TODO | – |
| W2-A5-3 | 发布看板（KPI 用询盘/触达口径，非点赞） | TODO | – |
| W2-A5-4 | 半自动导出界面（复制 + 唤起官方入口） | TODO | – |
| W2-A5-5 | 接口测试（后端用 fake） | TODO | – |

### A6 · 验证域

| ID | 任务 | 状态 | 依赖 |
| --- | --- | --- | --- |
| W2-A6-1 | 契约一致性校验（字段名/枚举 vs `INTERFACES.md`） | TODO | W1 交付 |
| W2-A6-2 | 集成测试（端到端，含假 Adapter） | TODO | W1 交付 |
| W2-A6-3 | 风控演练（限流/认证失效/政策拒绝/异步超时） | TODO | W1 交付 |
| W2-A6-4 | 幂等演练（重复投递不重复发布） | TODO | W1 交付 |
| W2-A6-5 | 生成质量回归框架（固定 brief 集打分） | TODO | W1-A1 |

---

## 未决问题（不阻塞开发）

| # | 问题 | 契约临时默认 | 责任人 |
| --- | --- | --- | --- |
| Q1 | VK 是否移出主清单 | ✅ **已关闭：2026-09-11 法务评审通过，VK 转正式运营（P1）**；契约 v1.1 已加 `Platform.VK` | 用户 |
| Q2 | TikTok 是否降为 P2 | 暂不投入 | 用户 |
| Q3 | 首发市场是否为印度+美国+台湾 | 按此实现 best-time 与语言 | 用户 |
| Q4 | 硬参数披露边界（材质/单重/产能/检测） | `brand_guides.payload` 预留三级清单 | 用户 |
| Q5 | 机加工工段素材补拍 | `TODO(need-real-data)` 占位 | 用户/M0 |

---

## 验收里程碑（对应需求文档 M1）

> **M1 验收标准**：单账号单平台跑通「生成 → 审核 → 发布 → 回执」闭环。
> 目标平台：**LinkedIn**（P0-A）。发布可先走半自动，但闭环状态必须完整可追溯。

---

## W1 执行记录（2026-09-10）

### 第一轮：失败（角色漂移）

| 域 | 任务名 | 结果 | 说明 |
| --- | --- | --- | --- |
| A1 内容生产 | `/root/a1_content` | **失败** | 30 分钟零代码产出 |
| A2 发布网关 | `/root/a2_publish` | **失败** | 30 分钟零代码产出 |
| A3 调度与账号 | `/root/a3_scheduler` | **失败（越界）** | 继承 root 上下文后自认 root，转写治理文档并改 `pyproject.toml`/`AGENTS.md`，产出 6 份派工单；代码零产出 |

**真因**：派生时用了 `fork_turns="all"`，子 agent 继承 root 的完整对话上下文 → 角色漂移。
已记入 `docs/多Agent协同开发方案.md` §7.1，并写入 `AGENTS.md §0.1`（身份边界）。

### 第二轮：修正后成功

| 域 | 任务名 | 派生方式 | 结果 |
| --- | --- | --- | --- |
| A2 发布网关 | `/root/a2_publish_v2` | **`fork_turns="none"`** | ✅ **80 条单测全绿** |

**A2 v2 交付物**（`pulse/services/publish/`，约 20 个文件）：
`base.py`（契约 §4 的 ABC，签名已逐字核对）、`gateway.py`、`errors.py`、`state.py`、`store.py`、
`transport.py`、`semi_auto.py`、`adapters/{fake,linkedin,youtube}.py` + 6 个测试文件

**关键验收已通过**（root 独立复核，非自述）：
- `pytest pulse/services/publish` → **80 passed**
- 全仓库 `pytest` → **100 passed**（80 + 20 契约基线）
- 最高风险规则已实现并有显式断言：平台受理 → **只进 `pending_finalize`**，绝不直接置 `published`；超时只告警，**绝不自动发布**

**对照结论**：`fork_turns="all"` → 30 分钟零产出；`fork_turns="none"` → 5 分钟产出 20 个文件、80 条测试。
后续所有工作域一律用 `fork_turns="none"`。

### 待办

- A1 内容生产、A3 调度与账号**尚未重新启动**（用 `fork_turns="none"` 重开）
- W2（A4 合规 / A5 平台前端 / A6 验证）未启动
- `W0-5` Git 提交：见下方提交记录

---

## 版本管理（V1.10.0 · 2026-09-11）

**当前版本 = V1.10.0**（`git tag` 可查）。三处版本号必须一致，由
`pulse/shared/tests/test_release_version.py` 守护（5 项）：

| 位置 | 当前取值 | 真源属性 |
| --- | --- | --- |
| `pyproject.toml` | `1.10.0` | `[project] version` |
| `pulse/__init__.py` | `1.10.0` | `pulse.__version__` |
| `CHANGELOG.md` | `## [1.10.0]` | 逐版本条目 |
| 契约版本（独立于产品版本） | `1.1` | `pulse.shared.CONTRACT_VERSION` |

> **版本漂移教训（2026-09-11）**：1.4.1–1.7.1 期间只打了 git tag、
> 没同步 `pyproject.toml` / `pulse.__version__` / `CHANGELOG.md`，导致三处停在 1.4.0
> 而 tag 已到 v1.7.1。守护测试只比对这三处、看不到 tag，所以一直没报警。
> v1.8.0 已一次性追平。**发布时三处 + tag 必须同时改。**

### 提交记录

| 提交 | 内容 |
| --- | --- |
| `879b3f8` | W0：冻结接口契约 v1.0 + 多 Agent 协同治理基线 |
| `0612960` | W1：A2 发布网关域交付（80 测试通过）+ 治理补强与 fork 策略修正 |

### 发布标签

| 标签 | 说明 |
| --- | --- |
| `v1.0.0` | 契约冻结 + 发布网关域交付；含版本元数据、变更记录与 CI |

### 远端（GitHub）

`main` 推送到 GitHub 后，`.github/workflows/ci.yml` 会在推送 / PR 时跑全量 pytest，
在推送 `v*` 标签时自动创建 Release。本机直连 GitHub 被阻断，需经代理端口
`127.0.0.1:7897`（见 `README.md` §4.4）。

### 升版口径

契约冻结或对外行为不兼容 → 升 `MAJOR`；向后兼容的新增 → 升 `MINOR`；修缺陷 → 升 `PATCH`。
每次升版同步 `pyproject.toml` / `pulse/__init__.py` / `CHANGELOG.md` 三处，再打 `vX.Y.Z` 带注释标签。

### 仓库根整理（2026-09-10）

代码仓库根定为 **`D:\agent开发\菲美得\agent`**（`.git` 随之归位），工作区 `D:\agent开发\菲美得\`
只保留不入库的业务资产。整理内容：

| 项 | 处理 | 说明 |
| --- | --- | --- |
| `.git` | 移到 `agent\` | 仓库根与代码根统一，工作树恢复干净，历史与远端不变 |
| `.github\workflows\ci.yml` | 从 `agent\workflows\` 归位 | 原路径 GitHub 不读取，会导致 CI 静默失效 |
| `__pycache__` / `.pytest_cache` | 删除 | 可再生缓存，共 8 个目录 |
| `Pulse海外社媒Agent开发文档_v0.2.docx` | 删除 | 已被 v0.2.1 取代，且不在 AGENTS.md §1 文档优先级清单内；旧版本仍可从 git 历史取回 |
| 各文档中的绝对路径 | 更新 | 解释器与仓库根路径同步为 `agent\…` |

**结论：仓库内容与 `v1.0.0` 标签逐字节一致**——本次整理只改路径与说明，未改任何代码。

### 第二轮整理（2026-09-11 · V1.8.0）

| 项 | 处理 | 说明 |
| --- | --- | --- |
| `Pulse海外社媒Agent开发文档_v0.2.1.docx` | 删除 | 已被 **v0.3** 完全覆盖（v0.3 在其基础上新增媒体资产库与召回策略、FR-15~FR-19）；AGENTS.md §1 文档优先级同步改指 v0.3。旧版仍可从 git 历史取回 |
| `__pycache__` / `.pyc`（仓库内 13 处 + 素材工具目录 1 处） | 删除 | 可再生缓存 |
| 工作区根的两张控制台预览图 | 删除 | 开发期截图，非交付物 |
| 版本漂移 | 追平 | `pyproject.toml` / `pulse.__version__` / `CHANGELOG.md` 停在 1.4.0 而 tag 已到 v1.7.1；已统一为 1.8.0 并补写 1.4.1–1.7.1 变更记录 |
| 版本引用对齐 | 更新 | README 头部版本、标签/发布示例、TASKS 版本管理节、AGENTS 文档清单、派工单与 `.gitignore` 中的素材计数 |
