# 派工单 · A3 Scheduler & Identity（调度与账号域）

> 任务名：`/root/a3_scheduler`　｜　波次：**W1**
> 可写目录：`pulse/services/scheduler/`、`pulse/services/identity/`（各含自己的 `tests/`）
> 开工前必读：`AGENTS.md` → 本派工单 → `pulse/contracts/INTERFACES.md` §3.2/§5/§6

## 1. 边界

| | 内容 |
| --- | --- |
| 可写 | `pulse/services/scheduler/`、`pulse/services/identity/` |
| 只读 | `pulse/contracts/`、`pulse/shared/`、`pulse/docs/`、`pulse/tasks/` |
| 禁止 | 改契约；写 `pulse/tests/`；碰 git；连真实 Redis；打印任何 Token 明文 |

> 你一人管两个域。**两个域各自独立可测**——`identity` 不得依赖 `scheduler` 的内部实现，反之亦然。

## 2. 交付目标（一句话）

决定"什么时候、用哪个账号、以什么频率"发，并在配额、限流、凭据失效三种压力下**可控降级而非静默失败**。

## 3. 任务包

| ID | 任务 | 完成定义（DoD） | 证据 |
| --- | --- | --- | --- |
| W1-A3-1 | Celery + Redis 接线 | **含 eager 测试模式**：单测不依赖真实 Redis | eager 下任务同步执行并返回结果 |
| W1-A3-2 | 延迟任务投递 | 用 `eta` / `countdown`；**禁用 cron 硬排**（要能取消与重排） | 单测：入队/取消/重排 |
| W1-A3-3 | 令牌桶限流 + 发布冷却 | 按 `accounts.quota_config` 配置；冷却期内不得重复发 | 单测：超限被拒 + 冷却生效 |
| W1-A3-4 | **配额预检** | YouTube 上传约 1600 单位/次、日配额约 10000；预检失败**不投递**并给出可读原因 | 单测：第 7 次上传被拦 |
| W1-A3-5 | best-time 表 | 键：`account / platform / timezone / weekday / slot`；查询返回 `scheduled_at`（**必须带时区偏移**） | 单测：跨时区查询不漂移 |
| W1-A3-6 | 账号模型 + OAuth 骨架 | 字段与契约 §6 `accounts` 一致（`id/platform/display_name/region/timezone/status/quota_config`） | 单测：CRUD + 状态机 |
| W1-A3-7 | Token 生命周期 | access/refresh 双过期；**提前刷新**；支持吊销 | 单测：临近过期触发刷新、吊销后拒绝 |
| W1-A3-8 | Vault/KMS 加密接口 | 业务层**只见句柄不见明文**；`encrypted_payload` 为密文；**日志中永不出现 Token** | 单测：断言日志输出不含明文 |
| W1-A3-9 | 账号停用熔断 | 账号 `paused`/`revoked` → **即时挂起该账号全部待发任务** | 单测：停用后原 pending 任务不再投递 |
| W1-A3-10 | 单元测试 | 覆盖率 ≥ 80%（关键路径） | 测试命令 + 通过数 |

## 4. 关键约束

1. **队列只传 ID，不传业务对象**（契约 §5）。你的出口消息 `pulse.publish.dispatch` 载荷是 `{job_id, unified_post_id}`，**仅此而已**。这样重试天然幂等、消息体不会版本漂移。
2. **禁用 cron。** 一律 `eta` / `countdown`，理由是取消与重排。
3. **Token 永不落日志。** 这是 A3 的头号事故源，测试里要有断言守着。
4. **`scheduled_at` 必须带时区偏移。** 契约模型会直接把无偏移的时间判为错误——多时区排期漂移是真实事故，不是理论风险。
5. 你拥有 `schedules` / `publish_jobs` / `accounts` / `credentials` 四张表的写入逻辑；`publish_jobs.unified_post_id` 上有**唯一索引**，这是幂等的第一层防线（第二层在 A2 的 `find_existing()`）。
6. **平台基线**：P0-A = LinkedIn + YouTube。VK **冻结**，不得实现其调度分支。TikTok 暂不投入。

## 5. 依赖与协同

- 你不等待任何 W1 同伴：队列用 eager 模式即可自测；上游 A5 用桩。
- 需要 A2 配合（如回执超时回调）→ 消息 `/root/a2_publish` 并抄送 root。
- 涉及"发布失败后是否重排"的策略分歧，归 A2（错误分级）+ 你（重投时机），边界写进交付报告。

## 6. 交付

按 `README.md` §4 模板回报 root。两个域**分别**列改动文件与测试结果。
