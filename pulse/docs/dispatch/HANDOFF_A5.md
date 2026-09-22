# 交接单 · A5 接口层与审批台（2026-09-22）

> 为什么有这个文件：2026-09-22 有 5 个子 agent 实例**只收到环境上下文、没收到任务正文**
> （A1 ×3、A4 ×2、A5 ×2），只有 A3、A6 正常。为绕开正文投递失败，任务正文改放文件里，
> spawn 时只发一句话让子 agent 读本文件。
>
> **如果你（子 agent）读到了本文件，说明投递成功，请按 §1 开工。**

## 1. 任务

按 `pulse/docs/dispatch/A5_platform.md` 把接口层做完（W2-A5-1 ~ W2-A5-5）。
那份派工单是验收标准的唯一真源；本文件只补"当前基线""接线方式"和"边界"。

## 2. 身份与边界（硬约束）

- 你是子 agent A5，不是 root。只写 `pulse/api/`（含它自己的 `tests/`）。
- **`pulse/console/` 属 root，不要改**（要接审批界面就在 `pulse/api/` 里做，或在报告里写依赖请求）。
- 不碰 git；不 spawn 子 agent；不改 `AGENTS.md` / `pyproject.toml` / `pulse/contracts/` /
  `pulse/docs/` / `pulse/tasks/` / `pulse/shared/` 以及任何其他域目录。
- 不新增第三方依赖（环境里已有 `pytest` / `pytest-asyncio` / `pydantic` / `celery` / `redis` / `httpx`）；
  不连真实数据库、不发真实网络请求——后端服务用假实现/桩。
- 不写 `pulse/tests/`（那是 A6 的目录）。

## 3. 环境与验证

```powershell
cd "D:\agent开发\菲美得\agent"
$env:PYTHONIOENCODING = 'utf-8'
& ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider pulse/api
```

- 解释器只能用 `.venv\Scripts\python.exe`；必须带 `-p no:cacheprovider`。
- 当前基线（2026-09-22）：**全仓 699 项全绿、0 xfail**，版本 `v1.17.2`、契约 v1.1。
  你的新增用例不许让这个基线变红。

## 4. 端点（契约 §7，**路径冻结**）

`POST /api/v1/briefs`｜`GET /api/v1/contents/{id}/variants`｜`PATCH /api/v1/variants/{id}/status`｜
`POST /api/v1/variants/{id}/schedule`｜`PATCH /api/v1/schedules/{id}`｜`POST /api/v1/schedules/{id}/publish`｜
`GET /api/v1/schedules/{id}/semi-auto`｜`GET /api/v1/accounts`｜`POST /api/v1/accounts/{id}/oauth`｜
`DELETE /api/v1/accounts/{id}/credential`｜`POST /api/v1/compliance/screen`

统一错误体：`{"error": {"code": ..., "message": ..., "details": {}}}`。

## 5. 审批工作流（FR-4，P0；当前**没有任何实现**，是 M1 的缺口之一）

- 动作：通过 / 驳回 / 改稿（needs_revision）/ 整批通过；**留痕**（人 / 时间 / diff）。
- 状态机见契约 §3.1：`draft → pending_review → approved | rejected | needs_revision`。
- **三个必须逐条复核、不可整批通过的触发条件**：首次接入某账号、命中合规告警、更换 Brand Guide。
- `approved` 之前的内容**不得进入发布池**（契约 §2 与需求 US-3）。

## 6. 可以直接复用的现成域（只读引用，不要改）

| 域 | 位置 | 你能拿到什么 |
| --- | --- | --- |
| A1 内容 | `pulse/services/content/` | `ContentService.submit_brief/generate_variant/self_review`、`ContentStore`、`AccountBinding`、`DerivedVariant` |
| A3 调度 | `pulse/services/scheduler/` | `Dispatcher.create_schedule/enqueue_schedule/publish_now/cancel_schedule`、`QuotaLedger`、`BestTimeTable` |
| A3 账号 | `pulse/services/identity/` | 账号 CRUD、`store_tokens`、凭据吊销与熔断联动 |
| A2 发布 | `pulse/services/publish/` | `PublishGateway`、`FakeAdapter`、`SemiAutoExporter`（半自动导出） |
| 契约类型 | `pulse/shared/models.py`、`enums.py` | `UnifiedPost` / `VariantStatus` / `ComplianceInfo` 等（跨域传递必须用这些类型） |

> 注意：A4 合规域（`pulse/services/compliance/`）**正在并行开发**。你的
> `POST /api/v1/compliance/screen` 与审批里的"命中合规告警"分支请用**桩**（或按契约语义的替身）
> 先跑通，并在报告里注明"待 A4 落地后换真实现"。

## 7. 交付

用 `pulse/docs/dispatch/README.md` §4 的**六节模板**回报。第 2 节必须是**可直接复制运行的命令 + 真实输出**。
遇到契约矛盾或需要新依赖：先按最简方案实现，把问题写进"未决问题"，不要停下等 root。
