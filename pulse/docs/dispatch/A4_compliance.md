# 派工单 · A4 Compliance（合规与治理域）

> 任务名：`a4_compliance`　｜　波次：**W2**　｜　可写目录：`pulse/services/compliance/`（含 `tests/`）
> 开工前必读：`AGENTS.md` → 本派工单 → `pulse/contracts/INTERFACES.md` §3.5/§6/§7

## 1. 边界

| | 内容 |
| --- | --- |
| 可写 | `pulse/services/compliance/` |
| 只读 | `pulse/contracts/`、`pulse/shared/`、`pulse/docs/`、`pulse/tasks/` |
| 禁止 | 改契约；写 `pulse/tests/`；碰 git；依赖 A1 的内部实现 |

## 2. 交付目标（一句话）

对每个 `variant_id` 给出**结构化、可追溯、可豁免留痕**的合规判定：`block` 还是 `warn`。

## 3. 任务包

| ID | 任务 | 完成定义（DoD） | 证据 |
| --- | --- | --- | --- |
| W2-A4-1 | 规则引擎骨架 | 输出字段与契约 §6 `compliance_findings` 一致：`rule / severity / message / position` | 单测：命中规则 → 结构化 finding |
| W2-A4-2 | 版权规则 | 素材 `license_status` 为 `pending` → 直接 `block`；`owned`/`licensed` 放行 | 单测：三种状态各一条 |
| W2-A4-3 | 敏感词库（最小集） | B2B 工业向；词库外置为数据，不硬编码在逻辑里 | 单测：命中/不命中 |
| W2-A4-4 | 平台政策与质量规则 | 反机器味、字数上下限、emoji 上限、hashtag 规范性 | 单测：每类规则至少 1 条 |
| W2-A4-5 | **制裁与出口管制筛查** | 结果枚举 `clear / hit / review_required`；记录 `lists_checked`（OFAC SDN / BIS Entity List 等）与 `evidence` | 单测：三类结果 + 记录完整性 |
| W2-A4-6 | `block` / `warn` 与豁免留痕 | `block` **默认不可绕过**；`warn` 豁免必须写入 `waived_by` | 单测：无豁免人时 `warn` 不可豁免 |
| W2-A4-7 | 单元测试 | 覆盖率 ≥ 80% | 测试命令 + 通过数 |

## 4. 为什么你不在 A1 里面

生成者不能判定自己合规——这既是内控要求，也对应本项目**已识别的真实风险**：尽调报告显示美国正重点监控"中国 → 俄罗斯 CNC/机床供应链"，铸件/机床功能件出口方与之交易可能触发**次级制裁**。所以：

- 合规域必须**独立于内容生成**，只吃 `variant_id`，不看生成过程；
- 制裁筛查是**红线功能**，不是可选装饰。

## 5. 关键约束

1. **筛查结论是"尽调记录"，不是"法律豁免"。** 结果写 `clear` 只代表"在所列清单中未命中，且已留痕"，**不得**输出"合规/合法/无风险"这类结论性表述。
2. 词库、清单、规则集**全部外置**（数据文件/配置），代码只做匹配与判定。清单会变，改数据不该改代码。
3. `position` 要能定位到字段名 + 字符偏移，否则审批人无法在控制台上看到"哪一句有问题"。
4. 不得引入需要联网的法律数据库依赖（无网络、无凭证）。清单用本地静态数据 + `TODO(need-real-data)` 标注更新日期。

## 6. 依赖与协同

- W2 启动时 W1 已完成，你可以直接对真实的 A1 产物（`variants`）跑规则。
- 需要新增第三方依赖 → 先消息 root。

## 7. 交付

按 `README.md` §4 模板回报 root。

