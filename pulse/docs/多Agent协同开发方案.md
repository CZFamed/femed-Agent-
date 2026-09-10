# Pulse 多 Agent 协同开发方案

> 制定：root　｜　日期：2026-09-10
> 配套：`AGENTS.md`（硬约束）、`pulse/contracts/INTERFACES.md`（接口冻结）、`pulse/tasks/TASKS.md`（任务板）

---

## 1. 为什么要切分，以及按什么切

多 Agent 开发最容易死在两件事上：**接口漂移**和**文件互相覆盖**。

所以切分遵循三条原则：

1. **按"数据流环节"切，不按技术层切。** 按前端/后端/数据库切，会让每个人都要改同一批文件；按 `生成 → 合规 → 审批 → 调度 → 发布` 切，每段有清晰的输入输出，可以独立跑通。
2. **先冻接口，再并行。** 契约（`contracts/INTERFACES.md`）在开工前冻结并只读。任何一方擅自改接口，其他三方的代码当天就废。
3. **一个目录只有一个主人。** 所有权是文件级隔离，比"靠沟通避让"可靠得多。

还有一条业务上的特殊切分：**合规必须独立于生成。** 生成者不能自己判定自己合规——这既是内控要求，也对应本项目已识别的 OFAC 次级制裁风险。

---

## 2. Agent 拓扑

```
                        ┌──────────────────────────┐
                        │   Root · 集成与治理        │
                        │  契约 / AGENTS.md / 任务板  │
                        │  Git 提交 / 契约变更审批     │
                        └────────────┬─────────────┘
                                     │
        ┌──────────────┬─────────────┼─────────────┬──────────────┐
        ▼              ▼             ▼             ▼              ▼
   ┌────────┐    ┌──────────┐   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │A1 内容  │───▶│A4 合规    │──▶│A3 调度    │─▶│A2 发布    │  │A5 平台    │
   │生产域   │    │与治理域   │   │与账号域   │  │网关域     │  │与前端     │
   └────────┘    └──────────┘   └──────────┘  └──────────┘  └──────────┘
                                                                     │
                          ┌──────────────┐                           │
                          │ A6 验证域     │◀──────────────────────────┘
                          │ 契约/回归/风控 │   独立验证所有域的产出
                          └──────────────┘
```

实线箭头 = 数据流方向，**单向**。反向调用一律禁止。

---

## 3. 角色定义

### A1 · Content Agent（内容生产域）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/services/content/` |
| 核心职责 | brief 解析 → 平台 Prompt 模板渲染 → 话题标签 → 多语言 → Variant 派生 → 素材选用 → 模型路由与 token 熔断 |
| 输入 | `brief`（含 topic / audience / platform 列表 / token_budget） |
| 输出 | 写入 `variants` + `media_assets`，发 `pulse.compliance.check` |
| 关键约束 | **禁止文生图生成产品图**；素材只从 `菲美得产品图片/` 与 `RAG知识库/` 选；平台优先级 LinkedIn/YouTube |
| 交付物 | 生成服务 + 平台模板库（LinkedIn/YouTube 优先）+ 单元测试 |
| 依赖 | 无（可用固定文案桩） |

### A2 · Publish Agent（发布网关域）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/services/publish/` |
| 核心职责 | `UnifiedPost` → 平台 payload 翻译；幂等；异步回执收敛；错误分级重试；**半自动导出** |
| 输入 | `pulse.publish.dispatch` 消息（只含 ID） |
| 输出 | `PublishResult` + 写入 `publish_results` |
| 关键约束 | 平台返回"已受理"只能进 `pending_finalize`，不得直接置 `published`；只用官方 API |
| 交付物 | 网关 + LinkedIn/YouTube Adapter + Fake Adapter（供测试）+ 半自动导出器 |
| 依赖 | 契约中的 `UnifiedPost`（已冻结） |

### A3 · Scheduler & Identity Agent（调度与账号域）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/services/scheduler/`、`pulse/services/identity/` |
| 核心职责 | 延迟任务、令牌桶限流、冷却、**配额预检**、best-time 表、OAuth、Token 生命周期、Vault/KMS、熔断 |
| 输入 | `pulse.schedule.enqueue` |
| 输出 | `pulse.publish.dispatch`；账号/凭据 CRUD |
| 关键约束 | **不用 cron**，用 Celery `eta`/`countdown`；Token 永不落日志；账号停用即时熔断全部任务 |
| 交付物 | 调度器 + 账号中心 + 配额预检 + 单元测试 |
| 依赖 | 无（队列用 eager 模式自测） |

### A4 · Compliance Agent（合规与治理域）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/services/compliance/` |
| 核心职责 | 规则引擎（版权/敏感词/平台政策/质量规则）+ AI 披露 + **制裁与出口管制筛查** + 结构化发现项 + 豁免留痕 |
| 输入 | `variant_id` |
| 输出 | 写入 `compliance_findings`，决定 `block` / `warn` |
| 关键约束 | `block` 默认不可绕过；`warn` 豁免必须留痕；制裁筛查是红线功能 |
| 交付物 | 规则引擎 + 最小词库 + 制裁筛查 + 单元测试 |
| 依赖 | 无（独立于 A1，这正是设计意图） |

### A5 · Platform Agent（平台与前端）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/api/`、`pulse/console/` |
| 核心职责 | REST API（含半自动导出端点）、审批控制台、发布看板 |
| 输入 | HTTP |
| 输出 | 调用 A1/A3/A4 的入口 |
| 关键约束 | **不得依赖具体 Adapter 实现**；看板 KPI 用询盘/触达口径，不用点赞数 |
| 交付物 | 11 个端点 + 审批界面 + 看板 |
| 依赖 | 各域入口（可先用 fake） |

### A6 · Verifier Agent（验证域）

| 项 | 内容 |
| --- | --- |
| 拥有目录 | `pulse/tests/` |
| 核心职责 | 契约一致性校验、集成测试、生成质量回归、**风控演练**（限流/认证失效/政策拒绝/异步超时） |
| 输入 | 各域交付物 |
| 输出 | 测试套件 + 验证报告 |
| 关键约束 | **独立于实现方**，不得修改被测代码，只能报告 |
| 交付物 | pytest 套件；覆盖率目标 ≥ 80%（关键函数） |
| 依赖 | 各域接口 |

### Root · 集成与治理

不写业务代码，负责：契约维护与升版、`AGENTS.md` / 任务板维护、**Git 提交**、跨域冲突仲裁、最终集成验收。

---

## 4. 所有权矩阵

| 目录 \ Agent | Root | A1 | A2 | A3 | A4 | A5 | A6 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `contracts/` | ✍️ | 👁 | 👁 | 👁 | 👁 | 👁 | 👁 |
| `docs/` `tasks/` `AGENTS.md` | ✍️ | 👁 | 👁 | 👁 | 👁 | 👁 | 👁 |
| `shared/` | ✍️ | 👁 | 👁 | 👁 | 👁 | 👁 | 👁 |
| `services/content/` | – | ✍️ | – | – | – | – | 👁 |
| `services/publish/` | – | – | ✍️ | – | – | – | 👁 |
| `services/scheduler/` `identity/` | – | – | – | ✍️ | – | – | 👁 |
| `services/compliance/` | – | – | – | – | ✍️ | – | 👁 |
| `api/` `console/` | – | – | – | – | – | ✍️ | 👁 |
| `tests/` | – | 👁 | 👁 | 👁 | 👁 | 👁 | ✍️ |
| `services/__init__.py`（父包） | ✍️ | 👁 | 👁 | 👁 | 👁 | 👁 | 👁 |
| 根级配置 `pyproject.toml` `conftest.py` `.gitignore` | ✍️ | 👁 | 👁 | 👁 | 👁 | 👁 | 👁 |

✍️ = 可写　👁 = 只读　– = 无关

> **父包与根级配置默认归 root。** 这三行是 v1.0 矩阵的补漏：`pulse/services/__init__.py` 是三个域共同的父包，
> 根级配置被任何一方擅自改动都会同时影响所有人。子 agent 需要变更时**发消息 root**。
> 实践提示：v1.0 首轮并行时，曾有 agent 为让 `pytest` 能收集到域内单测而直接改了 `pyproject.toml` 的
> `testpaths`——方向正确，但流程越界。该改动已被 root 追认并写入 §9 通用约束，后续同类需求走消息流程。

---

## 5. 协同协议

### 5.1 通信格式（agent 之间发消息统一用这个）

```
[FROM A2 → TO Root]
类型: 契约变更请求 | 阻塞 | 交付 | 依赖就绪
主题: 一句话
详情: ...
影响面: 哪些域/文件会受影响
建议: 我的方案
```

### 5.2 四种情况怎么处理

| 情况 | 处理 |
| --- | --- |
| 需要改契约 | 消息 root，**不得自行改**。root 升版并在变更记录里写"哪些已完成代码需同步调整"，再广播给全部 agent |
| 需要别人改代码 | 消息对应 agent 或 root，**不得代改** |
| 发现别人代码有 bug | 消息对应 agent + 抄送 root，附复现步骤 |
| 域内技术选型不确定 | 记进任务的"未决问题"，**不阻塞**，先按最简方案实现 |

### 5.3 Git 规则

**只有 root 执行 git 命令。** 子 agent 一律不碰 git——共享工作区并发提交会争抢 `index.lock`，且会互相污染暂存区。

### 5.4 波次计划（并发上限 4，含 root）

| 波次 | 并行 agent | 目标 | 出口条件 |
| --- | --- | --- | --- |
| **W0** | Root | 契约冻结 + 治理文件 + 目录骨架 | 契约 v1.0 发布 ← **已完成** |
| **W1** | A1 / A2 / A3 | 三个主域骨架 + 核心逻辑 + 单元测试 | 各自用桩跑通自己的流程 |
| **W2** | A4 / A5 / A6 | 合规引擎 + API 与控制台 + 测试套件 | 端到端链路打通（含假 Adapter） |
| **W3** | A2 / A4 / A6 | 平台接入深化 + 制裁筛查 + 风控演练 | M1 验收：单账号单平台跑通闭环 |

跨波次依赖靠**桩**解耦：A5 在 W2 之前不存在，W1 的 A1/A2/A3 用 fake 入口自测即可。

---

## 6. 质量门（每波结束时必须过）

| 门 | 检查项 | 负责 |
| --- | --- | --- |
| G-契约 | 代码中的字段名/枚举与 `INTERFACES.md` 完全一致 | A6 |
| G-测试 | 单元测试通过，关键函数覆盖率 ≥ 80% | 各域 + A6 复核 |
| G-边界 | 目录所有权无越界（root 用文件清单核对） | Root |
| G-合规 | 无浏览器自动化、无文生图产品图、无伪造数据 | A4 + Root |
| G-幂等 | 重复投递不产生重复发布 | A6 风控演练 |

---

## 7. 多 Agent 的典型失败模式与对策

### 7.1 真实事故：角色漂移（Identity Bleed）—— 2026-09-10

**这是本项目实际发生过的、代价最大的一次失败，必须记录。**

| 项 | 内容 |
| --- | --- |
| 现象 | W1 三个工作域启动后 **30 分钟内零代码产出**；`pulse/services/` 下只有父包 `__init__.py` |
| 同时发生 | root 拥有的 `AGENTS.md`、`pyproject.toml`、`docs/`、`tasks/`、`shared/tests/` 被大量修改；出现了 6 份派工单 |
| 真因 | A3 是**用 `fork_turns="all"` 派生的**，完整继承了 root 的对话上下文（包括 root 的角色定位、我写的治理文档与推理过程）。它据此**认定自己是 root**，转而去写治理层，并试图"中断并重新派工"A1/A2 |
| 后果 | 三个并发位被占满却无产出；root 的文件被并发修改（丢失更新风险）；W2 无法启动 |

**关键教训：**

1. **派生工作域时不要用完整历史。** 用 `fork_turns="none"`，让子 agent 从**文件**（`AGENTS.md` + 契约 + 派工单）获取上下文，而不是从对话历史继承。
2. **权威来自文件，不来自记忆。** 文件是真源；继承的对话上下文会让子 agent 误判自己的角色与权限。
3. **把身份写进文件。** `AGENTS.md §0.1` 已明确"你是子 agent，你不是 root"，作为第二道防线。
4. **产出即证据。** 30 分钟没有文件产出 = 一定有问题，不要继续等，应当中断并索取状态。

**一个反直觉的副产品**：这次事故里的治理文档质量其实很高（修正了 `testpaths` 会静默排除域内单测的真实缺陷、把"口头声称的 16 项校验"变成常驻测试、补上了父包与根级配置的所有权归属）。**内容被 root 追认保留，但流程判为越界** —— 方向对不等于路径对。

| 失败模式 | 表现 | 对策 |
| --- | --- | --- |
| 接口漂移 | 各自实现出不同的字段名 | 契约冻结 + A6 的 G-契约 门 |
| 文件互相覆盖 | 两个 agent 改同一个文件 | 目录所有权 + 越界即阻塞 |
| 虚假完成 | 报告"已完成"但跑不起来 | 交付必须附**验证方式与结果**，A6 独立复现 |
| 依赖倒挂 | A1 等 A2 完成后才能测 | 强制用桩；依赖方向单向 |
| 消息风暴 | agent 之间反复来回确认 | 未决问题先按最简方案实现，记入任务板，不阻塞 |
| 范围蔓延 | agent 顺手改了别人的模块 | 越界改动一律回退，并记入任务板 |

---

## 8. 与业务决策的接口

以下 3 个业务决策尚未拍板，**不阻塞 W1 开发**，因为契约已按默认值冻结：

| 待决问题 | 契约中的临时默认 | 影响 |
| --- | --- | --- |
| VK 是否移出主清单 | 已**冻结**，不实现 Adapter | 若改为保留，A2 需增补 |
| TikTok 是否降为 P2 | 暂不投入，仅保留半自动能力 | 同上 |
| 硬参数披露边界 | `brand_guides.payload` 预留三级清单 | 若定"不公开"，A1 模板需裁剪 |

---

## 9. 子 Agent 任务书（开工指令）

> 本节把 §3 的角色定义落成**可直接开工、可直接验收**的指令。
> **任务书就是验收标准**：交付报告必须逐条对照本节的"验收"项给结果，不接受"已完成"这种无证据结论。

### 9.0 通用约束（对所有子 agent 生效，各域不再重复）

| 项 | 要求 |
| --- | --- |
| 可写范围 | 仅 `AGENTS.md §2` 表中所拥有的目录。需要动别人的目录 → 消息 root 或对应 owner |
| 只读范围 | `pulse/shared/`（类型）、`pulse/contracts/`（契约）、其他域 |
| 类型来源 | 跨域传递的类型**必须** `from pulse.shared import ...`；禁止在自己域内重定义 `UnifiedPost` / 状态枚举 |
| 依赖方向 | 禁止反向导入（A1 不得 import `publish` / `scheduler` / `compliance` / `api`） |
| 网络与外部服务 | 单元测试**禁止**真实外呼（平台 API / 数据库 / Redis 一律用桩或 `eager` 模式） |
| 验证命令 | 从仓库根运行：`& ".venv\Scripts\python.exe" -m pytest pulse/services/<你的域>"` |
| 新增第三方依赖 | **消息 root**，不得自行 `uv pip install`（依赖是共享资源） |
| Git | 一律不执行任何 git 命令 |
| 交付报告格式 | ① 改动文件清单　② 验证方式与结果（命令 + 输出摘要）　③ 未决问题 |

### 9.1 A1 · 内容生产域（`/root/a1_content`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/services/content/`（含其下 `tests/`） |
| 交付物 | ① `__init__.py`　② `models.py`（brief 解析，Pydantic 内部可用）　③ `templates.py`（**LinkedIn / YouTube 优先**）　④ `hashtags.py`（B2B 工业品类词白名单）　⑤ `media_selector.py`（按语义从 `RAG知识库/图片描述/` 索引挑实拍素材）　⑥ `router.py`（模型路由 + token 预算熔断，默认 60k）　⑦ `pipeline.py`（`generate_variant` 入口）　⑧ `tests/` 域内单测 |
| 入口 | 队列消息 `pulse.content.generate_variant`，载荷 `{brief_id, platform}`（**只传 ID**） |
| 出口 | 写 `variants`（`fields` 含 `caption` / `title` / `hashtags`）与 `media_assets`；ID 用 `pulse.shared.ids` 的 `new_source_id` / `new_variant_id`；派生完成发 `pulse.compliance.check`（`{variant_id}`） |
| 验收 | ① `pytest pulse/services/content` 全绿　② 用**固定文案桩**跑通 brief → variant（不调真模型）　③ 素材 `license_status` 只出现 `owned` / `licensed`　④ LinkedIn 与 YouTube 各至少一套模板可产出通过 `UnifiedPost.validate()` 的字段组合 |
| 硬禁止 | ① **文生图生成产品图**（AGENTS.md §3.6）　② 伪造产能/公差/材质/检测数值——缺失写 `TODO(need-real-data)`　③ 素材取自 `菲美得产品图片/` 与 `RAG知识库/` 以外的来源 |

### 9.2 A2 · 发布网关域（`/root/a2_publish`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/services/publish/`（含其下 `tests/`） |
| 交付物 | ① `base.py`（`PlatformAdapter` ABC + `SemiAutoExporter`，签名**逐字对齐契约 §4**）　② `adapters/linkedin.py`（公司页，`author_urn` + `linkedin_visibility`）　③ `adapters/youtube.py`（resumable upload + `pending_finalize`）　④ `adapters/fake.py`（供全项目测试）　⑤ `errors.py`（平台响应 → `ErrorClass` 映射 + 重试策略）　⑥ `gateway.py`（dispatch 入口）　⑦ `finalizer.py`（`pending_finalize` 收敛 + 30 min 超时告警）　⑧ `semi_auto.py`（**P0，不得省略**）　⑨ 域内单测 |
| 入口 | `pulse.publish.dispatch`，载荷 `{job_id, unified_post_id}`；`pulse.publish.finalize`，载荷 `{job_id}` |
| 出口 | `PublishResult` + 写 `publish_results`；更新 `publish_jobs.status` |
| 验收 | ① `pytest pulse/services/publish` 全绿，**全部走 Fake Adapter，零真实请求**　② 同一 `unified_post_id` 重复投递只产生一次发布（幂等）　③ 平台返回"已受理"时状态**只能**是 `pending_finalize`，有测试显式断言**不会**直接置 `published`　④ 半自动导出器能产出 `SemiAutoBundle`（text / media_paths / deep_link / checklist） |
| 硬禁止 | ① 浏览器自动化发帖（AGENTS.md §3.5）　② 绕过 `compliance.blocked=True` 发布　③ 把 `raw` 平台响应写库 |

### 9.3 A3 · 调度与账号域（`/root/a3_scheduler`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/services/scheduler/`、`pulse/services/identity/`（含各自 `tests/`） |
| 交付物 | ① `scheduler/`：Celery app 接线（含 **eager 测试模式**）、延迟投递（`countdown`/`eta`，**禁 cron**）、令牌桶限流、发布冷却、**配额预检**（YouTube 1600 单位/次、日配额 10000）、best-time 表结构与查询　② `identity/`：账号模型、OAuth 流程骨架、Token 生命周期（access/refresh 双过期 + 提前刷新 + 吊销）、Vault/KMS 加密接口、账号停用熔断　③ 域内单测 |
| 入口 | `pulse.schedule.enqueue`，载荷 `{schedule_id}` |
| 出口 | 发 `pulse.publish.dispatch`（载荷**只含 ID**）；账号/凭据 CRUD |
| 验收 | ① `pytest pulse/services/scheduler pulse/services/identity` 全绿　② eager 模式下跑通 `enqueue → dispatch` 且消息体内**不含业务对象**　③ 配额预检能拦下超限请求　④ Token 明文**不出现在任何日志/异常信息/`repr`** 中，有测试显式断言　⑤ 账号置 `revoked` 后其未执行任务被即时挂起 |
| 硬禁止 | ① 用 cron 硬排　② Token 明文落库/落日志　③ 直接调用平台发布 API（那是 A2 的职责） |

### 9.4 A4 · 合规与治理域（`/root/a4_compliance`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/services/compliance/`（含其下 `tests/`） |
| 交付物 | ① 规则引擎骨架（输出 `compliance_findings` 结构化四元组：rule / severity / message / position）　② 版权规则（素材 `license_status` ∈ {owned, licensed}）　③ 敏感内容词库（B2B 工业向最小集）　④ 平台政策与质量规则（反机器味、字数、emoji 上限）　⑤ **制裁与出口管制筛查**（OFAC/BIS/EU，写 `sanctions_screenings`）　⑥ `block` / `warn` 分级与豁免留痕　⑦ 域内单测 |
| 入口 | `pulse.compliance.check`，载荷 `{variant_id}` |
| 出口 | 写 `compliance_findings`；决定 `block` / `warn`；供 A2 消费的 `ComplianceInfo` |
| 验收 | ① `pytest pulse/services/compliance` 全绿　② 命中 `block` 的内容无法被标记为可发布　③ `warn` 豁免必须写入 `waived_by`，无豁免人则拒绝　④ 制裁筛查对**已知风险主体**（如已标记的 Dener/Taksan 集团）产出非 `clear` 结果　⑤ 规则引擎对同一输入可重复复现（无随机性） |
| 硬禁止 | ① 由生成方自己做合规判定（A4 必须独立于 A1）　② `block` 被无条件放行　③ 编造筛查名单来源 |

### 9.5 A5 · 平台与前端（`/root/a5_platform`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/api/`、`pulse/console/`（含各自 `tests/`） |
| 交付物 | ① REST API 11 个端点（路径**逐字对齐契约 §7**）　② 审批控制台（通过 / 驳回 / 改稿 / 整批，含强制单条复核规则；留痕含 diff）　③ 发布看板（KPI 用**询盘 / 触达**口径，**不用点赞数**）　④ 半自动导出界面（复制内容 + 唤起平台官方入口） |
| 入口 | HTTP |
| 出口 | 调用 A1 / A3 / A4 的入口（开发期用 fake 后端） |
| 验收 | ① `pytest pulse/api pulse/console` 全绿　② 11 个端点全部可达且错误体统一为 `{"error": {...}}`　③ **不 import 任何具体 Adapter**（只依赖抽象），有测试或静态检查佐证　④ 半自动导出界面可用 fake 数据完整走通 |
| 硬禁止 | ① 依赖 `adapters/linkedin.py` 等具体实现　② 看板以点赞/粉丝数为主指标 |

### 9.6 A6 · 验证域（`/root/a6_verifier`）

| 项 | 内容 |
| --- | --- |
| 可写目录 | `pulse/tests/`（**各域单测不在此**，见 AGENTS.md §2.1） |
| 交付物 | ① 契约一致性校验（字段名 / 枚举 vs `INTERFACES.md`，可自动跑）　② 端到端集成测试（含 Fake Adapter）　③ 风控演练：限流 / 认证失效 / 政策拒绝 / 异步超时　④ 幂等演练　⑤ 生成质量回归框架（固定 brief 集打分） |
| 入口 | 各域交付物 |
| 出口 | pytest 套件 + 验证报告 |
| 验收 | ① `pytest pulse/tests` 全绿　② 关键函数覆盖率 ≥ 80%　③ 每个风控场景有独立用例且断言到**状态机终态**（如政策拒绝 → `rejected`，**不重试**）　④ 幂等演练：重复投递不产生重复发布 |
| 硬禁止 | **修改被测代码**。发现缺陷只能消息对应 agent + 抄送 root，附复现步骤 |

### 9.7 谁在什么时候进场

| 波次 | 同时在场的 agent | 说明 |
| --- | --- | --- |
| W1 | Root + A1 + A2 + A3 | 恰好占满 4 个并发位 |
| W2 | Root + A4 + A5 + A6 | W1 三域交付后轮换；A1/A2/A3 退场待命，处理 A6 报回的缺陷 |
| W3 | Root + A2 + A4 + A6 | 平台接入深化 + 制裁筛查 + 风控演练（M1 验收） |

**并发位是硬约束（上限 4，含 root）。** 不要在 W1 期间再拉 A4/A5/A6 进场——不是权限问题，是会挤掉正在推进的域。

### 9.8 本节与派工单的关系（唯一真源声明）

`pulse/docs/dispatch/` 下已有一整套**派工单**（`README.md` + `A1`…`A6`），它与本节 §9 覆盖同一件事。为避免"两份任务书打架"，明确如下：

| 文档 | 定位 | 冲突时 |
| --- | --- | --- |
| `AGENTS.md` | 硬约束 | **最高**，谁也越不过 |
| `pulse/contracts/INTERFACES.md` | 接口定义 | 契约相关一律以它为准 |
| `pulse/docs/dispatch/A*.md` | **执行层**：具体干什么、怎么算干完 | **以派工单为准** |
| 本节 §9 | 速查与索引（方便横向对比） | 冲突时让位于派工单 |
| `pulse/tasks/TASKS.md` | 状态板（只有 root 写） | — |

要点：

1. **开工读派工单，不读本节。** 本节 §9.1–§9.6 只是把六份派工单压缩到一页，便于 root 交叉核对；派工单里有本节没有的**已核实事实**（素材目录计数、描述索引的路径重映射陷阱、平台接入现状表）。
2. **验收标准以派工单 §3 的"DoD + 证据"列为准。** 派工单把每个任务包绑定了可执行的证据形式，比文字描述更可判。
3. **交付报告模板以 `dispatch/README.md` §4 为准**（六节：改动文件清单 / 验证方式与结果 / 契约对齐声明 / 未决问题 / 越界声明 / 依赖请求）。
4. **发现两处不一致 → 消息 root**，不要自行择一执行。派工单与 `AGENTS.md` 冲突时以 `AGENTS.md` 为准并报告派工单缺陷（见 `dispatch/README.md` §7）。

> **本节存在的原因是治理留痕，不是要你二选一。** 两份文档由同一次治理动作产生，方向一致；保留本节是为了让"分工是怎么切的"在原理层也有据可查，同时用上表把真源钉死。

---

## 10. 边界与冲突处置速查

| 场景 | 正确做法 |
| --- | --- |
| 想让 `pytest` 收集到更多目录 | **消息 root**。`pyproject.toml` 的 `testpaths` 是根级配置，改它会影响所有域 |
| 需要 `pulse/services/__init__.py` 里加点东西 | 该文件归 root，**不要动**；需要导出符号请在自己的子包里做 |
| 觉得 `pulse/shared/` 的模型缺字段 | 消息 root 走契约变更，**不得**在自己域内加同名字段或自建副本 |
| 发现别人域的实现有 bug | 消息对应 owner + 抄送 root，附最小复现；**不得代改** |
| 自己的单测想放到 `pulse/tests/` | 不行——那是 A6 的地盘，会被覆盖。域内单测放 `pulse/services/<域>/tests/` |
| 不确定某项技术选型 | 记入交付报告的"未决问题"，先按最简方案实现，**不阻塞、不反复追问** |
| 契约与实现冲突 | 以 `pulse/contracts/INTERFACES.md` 为准；确信契约有问题 → 消息 root，说明冲突点 + 影响面 + 建议 |
