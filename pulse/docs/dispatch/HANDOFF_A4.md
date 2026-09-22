# 交接单 · A4 合规与治理域（2026-09-22）

> 为什么有这个文件：2026-09-22 有 5 个子 agent 实例**只收到环境上下文、没收到任务正文**
> （A1 ×3、A4 ×2、A5 ×2），只有 A3、A6 正常。为绕开正文投递失败，任务正文改放文件里，
> spawn 时只发一句话让子 agent 读本文件。
>
> **如果你（子 agent）读到了本文件，说明投递成功，请按 §1 开工。**

## 1. 任务

按 `pulse/docs/dispatch/A4_compliance.md` 把合规与治理域做完（W2-A4-1 ~ W2-A4-7）。
那份派工单是验收标准的唯一真源；本文件只补"当前基线"和"边界"。

## 2. 身份与边界（硬约束）

- 你是子 agent A4，不是 root。只写 `pulse/services/compliance/`（含它自己的 `tests/`）。
- 不碰 git；不 spawn 子 agent；不改 `AGENTS.md` / `pyproject.toml` / `pulse/contracts/` /
  `pulse/docs/` / `pulse/tasks/` / `pulse/shared/` 以及任何其他域目录。
- 不新增第三方依赖；不调真实外部服务（制裁名单筛查用本地最小词表 + 结构化结果，不抓网）。
- 不写 `pulse/tests/`（那是 A6 的目录）。

## 3. 环境与验证

```powershell
cd "D:\agent开发\菲美得\agent"
$env:PYTHONIOENCODING = 'utf-8'
& ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider pulse/services/compliance
```

- 解释器只能用 `.venv\Scripts\python.exe`（系统 python 是 Windows Store 占位符）。
- 必须带 `-p no:cacheprovider`（多 agent 共享工作区）。
- 当前基线（2026-09-22）：**全仓 699 项全绿、0 xfail**，版本 `v1.17.2`、契约 v1.1。
  你的新增用例不许让这个基线变红。

## 4. 契约要点（`pulse/contracts/INTERFACES.md`）

| 项 | 位置 | 要点 |
| --- | --- | --- |
| 合规发现项 | §6 `compliance_findings` | 字段 `rule` / `severity` / `message` / `position` / `waived_by` |
| 严重度 | §3.5 + `pulse/shared/enums.py` 的 `ComplianceSeverity` | `block` 硬拦截（豁免需管理员权限）；`warn` 可人工豁免但**必须留痕** |
| 制裁与出口管制 | §6 `sanctions_screenings` + `ScreeningResult` | `subject` / `result`(clear/hit/review_required) / `lists_checked` / `evidence` / `checked_by` |
| 拦截语义 | §2 `ComplianceInfo` | `blocked=True` 时发布网关**必须拒绝发布**；`blocked=True` 必须带 `findings_ref` |
| 队列入口 | §5 | 消费 `pulse.compliance.check`，载荷 `{variant_id}`（只传 ID） |

## 5. 业务口径（AGENTS.md）

- 产品图**只用实拍**，不得用文生图（铁律 6）；素材授权只允许 `owned` / `licensed`（`pending` 禁止发布）。
- 不编造产能、公差、材质、检测数据、客户名；缺失写 `TODO(need-real-data)`。
- 禁用无法证实的形容词与承诺性表述（world-class / leading / No.1 / best price / 零封号 / 保证交期）。
- 平台基线：P0-A = LinkedIn + YouTube；P1 = Reddit + Facebook + VK；TikTok 暂不投入。
  VK 是唯一非英语平台（俄语正文 + 英语母版），且**每季度制裁名单筛查**是它的留痕义务。

## 6. 交付

用 `pulse/docs/dispatch/README.md` §4 的**六节模板**回报（改动文件清单 / 验证方式与结果 /
契约对齐声明 / 未决问题 / 越界声明 / 依赖请求）。第 2 节必须是**可直接复制运行的命令 + 真实输出**。
遇到契约矛盾或需要新依赖：先按最简方案实现，把问题写进"未决问题"，不要停下等 root。
