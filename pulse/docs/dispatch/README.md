# 子 Agent 派工与协同执行手册

> 维护：**root**　｜　建立：2026-09-10　｜　契约版本：v1.0

本手册是 `多Agent协同开发方案.md` 的**执行层**。

| 文件 | 回答的问题 |
| --- | --- |
| `AGENTS.md` | 硬约束是什么（最高优先级） |
| `pulse/contracts/INTERFACES.md` | 接口长什么样（冻结） |
| `pulse/docs/多Agent协同开发方案.md` | **为什么**这么切分（原理层） |
| **本手册 + 派工单** | 每个 agent **具体干什么、怎么算干完**（执行层） |
| `pulse/tasks/TASKS.md` | 现在到哪一步了（只有 root 写） |

---

## 1. 派工单索引

| 波次 | Agent | 任务名（spawn 用） | 派工单 | 可写目录 |
| --- | --- | --- | --- | --- |
| W1 | A1 内容生产 | `/root/a1_content` | [A1_content.md](./A1_content.md) | `pulse/services/content/` |
| W1 | A2 发布网关 | `/root/a2_publish` | [A2_publish.md](./A2_publish.md) | `pulse/services/publish/` |
| W1 | A3 调度与账号 | `/root/a3_scheduler` | [A3_scheduler_identity.md](./A3_scheduler_identity.md) | `pulse/services/scheduler/`、`pulse/services/identity/` |
| W2 | A4 合规治理 | `a4_compliance` | [A4_compliance.md](./A4_compliance.md) | `pulse/services/compliance/` |
| W2 | A5 平台前端 | `a5_platform` | [A5_platform.md](./A5_platform.md) | `pulse/api/`、`pulse/console/` |
| W2 | A6 独立验证 | `a6_verifier` | [A6_verifier.md](./A6_verifier.md) | `pulse/tests/` |

## 2. 并发与波次

**并发上限 4 个 agent（含 root）**，即任何时刻最多 3 个子 agent 同时运行。

| 波次 | 并行 | 目标 | 出口条件 |
| --- | --- | --- | --- |
| W0 | root | 契约冻结 + 治理 + 目录骨架 | ✅ 已完成（commit `879b3f8`） |
| **W1** | A1 / A2 / A3 | 三大主域跑通自有链路 | 各自用桩通过单测，契约字段零偏差 |
| W2 | A4 / A5 / A6 | 合规 + 接口层 + 独立验证 | M1 闭环（假 Adapter 端到端） |

跨波次依赖一律用**桩**解耦。W1 的三个域不得等待 W2 的任何一个域。

## 3. 子 Agent 通用作业规程（开工前必读）

1. **只写自己的目录。** 见派工单 §1。需要别人改代码 → 发消息给对应 agent 或 root，**不代改**。
2. **契约只读。** 发现契约有问题 → 消息 root，说明冲突点 + 建议 + 影响面，**不得自行修改** `pulse/contracts/` 或 `pulse/shared/`。
3. **不碰 git。** 子 agent 一律不执行 `git add/commit/checkout/reset/init`。共享工作区并发提交会争抢 `index.lock` 并互相污染暂存区。
4. **不开新依赖。** 需要新增第三方库 → 先消息 root。当前环境已装：pytest / pytest-asyncio / pydantic / celery / redis / httpx。
5. **并发跑测试必须加 `-p no:cacheprovider`。** 三个 agent 同时跑 pytest 会争写同一个 `.pytest_cache`。标准命令：
   ```powershell
   $env:PYTHONIOENCODING = 'utf-8'
   & "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider
   ```
   工作目录必须是仓库根 `D:\agent开发\菲美得\agent`。**不要**用系统的 `python` / `python3`（Windows Store 占位符，不可用）。
6. **不写 `pulse/tests/`。** 那是 A6 的目录。域内单测放 `pulse/services/<你的域>/tests/`。
7. **不伪造数据。** 产能、公差、材质、检测、客户名一律来自真实来源；缺失就写 `TODO(need-real-data)` 占位。编造比缺失严重得多。
8. **不调真实外部服务。** 模型调用用固定文案桩，平台调用用 Fake Adapter。W1 阶段不得发起真实网络请求。
9. **不重排平台优先级。** P0-A = LinkedIn + YouTube；VK 冻结（合规红线）；TikTok 暂不投入。
10. **不确定就先按最简方案实现**，记进"未决问题"，**不阻塞**、不来回确认。

## 4. 交付报告模板（强制）

每个 agent 完成一批任务后，回报 root 必须使用以下格式。缺任何一节都视为**未交付**。

```
[交付] <Agent 名> · <波次/任务包 ID>

1. 改动文件清单
   - <路径>（新增/修改），一句话说明

2. 验证方式与结果
   - 命令：<完整可复制的命令>
   - 结果：<通过/失败 + 关键输出，如 "42 passed" / "覆盖率 87%">
   - 未跑的部分及原因（如有）

3. 契约对齐声明
   - 代码中的字段名/枚举与 INTERFACES.md §N 一致：是/否
   - 若"否"，列出偏差项

4. 未决问题
   - <问题> → 建议方案（可留空，写"无"）

5. 越界声明
   - 是否触碰了自己目录以外的文件：否 / 是（列出并说明原因）

6. 依赖请求
   - 需要 root 或其他 agent 做什么（可写"无"）
```

> **"已完成"不等于"能跑"。** 没有第 2 节的可复现命令与输出的交付，root 一律退回。

## 5. 阻塞上报

只有满足以下之一才算 `BLOCKED`，其余情况**先按最简方案推进**：

- 契约缺失或自相矛盾，无法在不改契约的前提下实现；
- 需要新增第三方依赖；
- 需要写入非自己拥有的目录，且对方未响应；
- 业务事实缺失且无法用 `TODO(need-real-data)` 安全占位。

上报格式：`[阻塞] <Agent> · <一句话> · 影响任务ID · 需要谁做什么 · 我建议的方案`。

## 6. 波次门禁（每波出口必过）

| 门 | 检查项 | 负责 | 不通过怎么办 |
| --- | --- | --- | --- |
| G-契约 | 字段名/枚举与 `INTERFACES.md` 零偏差 | A6 | 退回实现方 |
| G-测试 | 域内单测通过，关键函数覆盖率 ≥ 80% | 各域 + A6 复核 | 补齐测试 |
| G-边界 | 无越界写入（root 用文件清单核对） | root | 回退越界改动 |
| G-合规 | 无浏览器自动化、无文生图产品图、无伪造数据 | A4 + root | 回退并记入任务板 |
| G-幂等 | 重复投递不产生重复发布 | A6 | 退回 A2/A3 |
| G-集成 | 假 Adapter 端到端闭环可追溯 | A6 | 阻断里程碑 |

## 7. 冲突与升级

| 情况 | 处理 |
| --- | --- |
| 两个 agent 想改同一个文件 | 停手 → 消息 root 仲裁。**目录所有权是唯一裁决依据。** |
| 发现别人代码有 bug | 消息该域 agent + 抄送 root，附最小复现步骤。**不代改。** |
| 契约与业务基线冲突 | 消息 root。root 更新契约 → 升版 → 广播"哪些已完成代码需同步调整"。 |
| 派工单与 AGENTS.md 冲突 | **以 AGENTS.md 为准**，并向 root 报告派工单错误。 |

## 8. 多 Agent 失败模式速查

| 失败模式 | 早期信号 | 对策 |
| --- | --- | --- |
| 接口漂移 | 各自实现出不同字段名 | 契约冻结 + G-契约 门 |
| 虚假完成 | 报告"已完成"但跑不起来 | 交付报告第 2 节强制可复现命令 |
| 依赖倒挂 | A1 等 A2 完成才能自测 | 强制用桩，依赖方向单向 |
| 并发踩踏 | `.pytest_cache` / `index.lock` 报错 | `-p no:cacheprovider` + 子 agent 不碰 git |
| 消息风暴 | agent 反复来回确认 | 未决问题先按最简方案实现，记入任务板 |
| 范围蔓延 | agent 顺手改了别人的模块 | 越界即回退，记入任务板 |
| 占位数据混入产物 | 文案里出现编造的产能/公差 | `TODO(need-real-data)` + G-合规 门 |
