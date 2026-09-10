# 派工单 · A2 Publish（发布网关域）

> 任务名：`/root/a2_publish`　｜　波次：**W1**　｜　可写目录：`pulse/services/publish/`（整棵子树）
> 开工前必读：`AGENTS.md` → 本派工单 → `pulse/contracts/INTERFACES.md` §2/§3.3/§3.4/§4/§5

## 1. 边界

| | 内容 |
| --- | --- |
| 可写 | `pulse/services/publish/`（含 `pulse/services/publish/tests/`） |
| 只读 | `pulse/contracts/`、`pulse/shared/`、`pulse/docs/`、`pulse/tasks/` |
| 禁止 | 改契约；写 `pulse/tests/`；碰 git；发真实平台请求；浏览器自动化 |

## 2. 交付目标（一句话）

把 `UnifiedPost` 可靠地送到平台，并且**在任意重复投递下都不产生重复发布**；平台"已受理"绝不等于"已发布"。

## 3. 任务包

| ID | 任务 | 完成定义（DoD） | 证据 |
| --- | --- | --- | --- |
| W1-A2-1 | 网关骨架 + `PlatformAdapter` ABC | 放在 `pulse/services/publish/base.py`，方法签名与契约 §4 **逐字一致**（`validate` / `find_existing` / `publish` / `poll_finalize` / `fetch_metrics`） | ABC 不可实例化；子类缺方法报错 |
| W1-A2-2 | `UnifiedPost` 接入 | **直接复用 `pulse.shared.models.UnifiedPost`，不得另建同名类型** | 单测用契约模型构造 |
| W1-A2-3 | LinkedIn Adapter | 公司页；校验必填 `author_urn`（`urn:li:` 前缀）+ `linkedin_visibility` 三值 | 单测覆盖合法/非法 options |
| W1-A2-4 | YouTube Adapter | resumable upload；`privacy_status` / `category_id` / `made_for_kids` 必填；返回**只能**进 `pending_finalize` | 单测断言不会直接置 `published` |
| W1-A2-5 | Fake Adapter | 供其他域与测试使用；可注入"延迟完成/政策拒绝/限流/401"四种行为 | 四种行为各有单测 |
| W1-A2-6 | 幂等 | `unified_post_id` 透传 + `find_existing()` 兜底**双层**；重复 dispatch 只产生一条 `publish_results` | 重复投递单测：调 2 次 → 1 次实际发布 |
| W1-A2-7 | 错误分级 | 平台错误 → `ErrorClass`（契约 §3.4）映射；重试策略与 `RETRYABLE_ERRORS` / `NON_RETRYABLE_ERRORS` 一致 | 每个 `ErrorClass` 至少 1 条映射单测 |
| W1-A2-8 | 异步回执状态机 | `pending_finalize` 必须带超时（默认 30 分钟）；超时 → 告警态；不复用 `published` | 状态迁移表单测，含超时分支 |
| W1-A2-9 | **半自动导出器** | 实现契约 §4 的 `SemiAutoExporter`；产出 `{text, media_paths, deep_link, checklist}` | 单测断言 4 个字段齐备且文案可复制 |
| W1-A2-10 | 单元测试 | **全部走 Fake Adapter**，不发真实请求 | 测试命令 + 通过数 |

## 4. 关键约束（契约里写死的，别自由发挥）

1. **受理 ≠ 发布成功。** 平台返回"已受理"时**只能**进 `pending_finalize`，**绝不能**直接置 `published`（契约 §3.3）。这是本项目最容易出事故的一条。
2. **`PublishResult` / `SemiAutoBundle` 已在 `pulse/shared/models.py` 实现**（含 `__post_init__` 断言：`ok=True` 且 `status="publishing"` 时必须给 `platform_post_id`）。**直接 import，不要重新定义。**
3. **`raw` 平台原始响应只进日志，不入库**（契约 §4 注释）。
4. `compliance.blocked=True` 时网关**必须拒绝发布**（契约 §2）。
5. 幂等有**两层**：`publish_jobs.unified_post_id` 唯一索引（A3/DB 侧）+ Adapter `find_existing()`（A2 侧）。
6. **半自动导出是与 Adapter 平级的 P0 能力**，不是降级方案——MVP 阶段 Facebook 群组、以及所有未过审的平台，全靠它出货。**不得省略。**

## 5. 平台接入现状（决定了你写多少"真"代码）

| 平台 | 自动化程度 | W1 要求 |
| --- | --- | --- |
| LinkedIn | 全自动（公司页） | Adapter 按契约实现，测试走 Fake |
| YouTube | 全自动（resumable upload） | 同上；注意配额（见下） |
| Reddit | 半自动（`subreddit` 需人工确认） | 仅保证 `options.subreddit` 校验 |
| Facebook / Instagram | 半自动 | 不实现 Adapter |
| **VK** | **冻结** | 不实现（合规红线） |
| TikTok | 暂不投入 | 不实现；半自动能力已覆盖 |

> YouTube 配额是硬约束：单次上传约 1600 单位，日配额约 10000 → **每天最多约 6 次上传**。`find_existing()` 若走 search API 还要约 100 单位，属于"兜底"而非"常规路径"——不要把它放进 happy path。

## 6. 依赖与协同

- 你的入口消息是 `pulse.publish.dispatch`，载荷**只含 ID**（契约 §5）。业务对象自己从库里取——**不要**假设别人会把 `UnifiedPost` 塞进消息体。
- A3（调度）负责**何时发**，你负责**怎么发**。不要在你的域里写排期/限流逻辑。
- 越界需求（需要 A3/A4 配合）→ 消息对应 agent + 抄送 root。

## 7. 交付

按 `README.md` §4 模板回报 root。第 2 节必须可复现。
