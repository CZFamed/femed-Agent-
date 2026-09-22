# 交接单 · A6 门禁复跑（2026-09-22）

> 交付方式说明：2026-09-22 有多个子 agent 实例**只收到环境上下文、没收到任务正文**
> （A1 ×3、A4 ×3、A5 ×3），只有 A3、A6 正常。为绕开正文投递失败，任务正文放在本文件，
> spawn 时只发一句话让子 agent 读本文件。读到本文件即为投递成功。

## 1. 背景

你（A6）在 `v1.17.1` 交付过 65 项独立验证，并钉住 4 个缺陷（F-1/F-2/F-3/F-4，另有 F-5）。
随后 root 做了两批修复与交付：

| 版本 | 内容 |
| --- | --- |
| `v1.17.2` | 修 F-1（同步平台收敛）、F-2（幂等键透传）、F-3（收敛期保持 pending_finalize）、F-5（排期迁移） |
| `v1.18.0` | 交付 A4 合规与治理域（`pulse/services/compliance/`，34 项单测） |

## 2. 你的任务（有界，别扩散）

1. **验证 3 个钉桩用例已转为正式断言，且断言没有被削弱**：
   - `pulse/tests/test_integration_e2e.py::test_sync_platform_published_result_converges`（原 F-1）
   - `pulse/tests/test_integration_e2e.py::test_a1_and_a3_agree_on_the_idempotency_key`（原 F-2）
   - `pulse/tests/test_risk_drills.py::test_drill_poll_returning_retryable_error_keeps_waiting`（原 F-3）
   逐条说明：断言是否仍然检查"真实行为"（而不是被改成必然通过的空断言）。
2. **复跑门禁并给判定**（G-契约 / G-测试 / G-边界 / G-合规 / G-幂等 / G-集成）：
   ```powershell
   cd "D:\agent开发\菲美得\agent"
   $env:PYTHONIOENCODING = 'utf-8'
   & ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider
   ```
   （基线：root 实测 **733 项全绿、0 xfail**；版本 `v1.18.0`、契约 v1.1。）
3. **对新增的 A4 合规域做一次独立抽查**（不必重写它的测试）：
   - `compliance_findings` 的字段是否与契约 §6 一致；
   - `block` 是否真的不可被普通用户豁免、`warn` 豁免是否留下 `waived_by`；
   - `ComplianceInfo(blocked=True)` 是否一定带 `findings_ref`（契约 §2）；
   - 制裁筛查是否**没有**输出法律结论性表述（只允许"尽调记录 + 免责"口径）。
4. **明确指出仍未闭合的缺口**：`pulse/api/`（A5：11 个 REST 端点 + 审批台）**仍未开工**，
   端到端里的"审批"目前是替身。请照实写，不要替它打勾。

## 3. 输出要求（重要）

**报告控制在 40 行以内**：一张"门禁逐项判定"表 + 一张"仍未闭合缺口"表 + 必要的复现命令。
不要贴大段测试清单（上一轮的 61 项明细已经入档，这次不需要重复）。
发现问题只报告、不代改（A6 的既定纪律）。
