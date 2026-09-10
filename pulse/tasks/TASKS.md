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

W1 全部交付后 → 进入 W2（A4 合规 / A5 平台前端 / A6 验证）。

---

## W0 · 契约与治理（Root）

| ID | 任务 | 负责 | 状态 | 交付物 |
| --- | --- | --- | --- | --- |
| W0-1 | 接口契约冻结 v1.0 | Root | **DONE** | `pulse/contracts/INTERFACES.md` |
| W0-2 | 治理文件与所有权约束 | Root | **DONE** | `AGENTS.md` |
| W0-3 | 协同方案与波次计划 | Root | **DONE** | `pulse/docs/多Agent协同开发方案.md` |
| W0-4 | 目录骨架与 shared 类型 | Root | **IN_PROGRESS** | `pulse/shared/` |
| W0-5 | Git 仓库初始化与首次提交 | Root | **TODO** | 待 W1 交付后一并提交 |

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
| Q1 | VK 是否移出主清单 | 冻结，不实现 | 用户 |
| Q2 | TikTok 是否降为 P2 | 暂不投入 | 用户 |
| Q3 | 首发市场是否为印度+美国+台湾 | 按此实现 best-time 与语言 | 用户 |
| Q4 | 硬参数披露边界（材质/单重/产能/检测） | `brand_guides.payload` 预留三级清单 | 用户 |
| Q5 | 机加工工段素材补拍 | `TODO(need-real-data)` 占位 | 用户/M0 |

---

## 验收里程碑（对应需求文档 M1）

> **M1 验收标准**：单账号单平台跑通「生成 → 审核 → 发布 → 回执」闭环。
> 目标平台：**LinkedIn**（P0-A）。发布可先走半自动，但闭环状态必须完整可追溯。
