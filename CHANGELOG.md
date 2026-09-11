# 变更记录

本文件记录 Pulse 的**对外可见版本**变更。格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

---

## [1.4.0] — 2026-09-11

**视觉模型接入改为 OpenCode Go + DeepSeek，并新增配置自检**。

### 变更

- **接入形态**：OpenCode Go 使用 Responses API（`https://opencode.ai/zen/go/v1/responses`），
  描述器同时支持 `responses` 与 `chat`（`/chat/completions`）两种形态，用
  `PULSE_VISION_API_STYLE` 切换，默认 `responses`。
- **默认模型**：`deepseek-v4-flash-vision-exp`。模型目录显示
  `deepseek-v4-flash` / `deepseek-v4-pro` / `deepseek-flash` **只支持文本输入**，
  拿它们识图会直接失败，因此配置成这三个模型时会在调用前拦截并给出中文提示。
- **配置自检**：新增 `python -m pulse.console.launcher --check-vision` 与
  `检查视觉模型.bat`，用一张 8×8 探针图验证"地址 / 密钥 / 模型能否识图"，
  失败时区分 config（未填 Key）、model（模型不支持图片）、request（网络或鉴权）三个阶段。
- **随仓库提供空配置**：`.env`（已 gitignore，Key 留空待填）与 `.env.example`（含说明）。

### 验证

- 未填 Key：自检返回"未配置 PULSE_VISION_API_KEY"。
- 用无效 Key 实测：接口返回 `401 Unauthorized`（`/zen/go/v1/responses`），
  证明地址与请求形态正确，只差有效 Key。
- 测试基线：**161 项全绿**（1.3.0 的 156 项 + 本次 5 项）。

---

## [1.3.0] — 2026-09-11

**素材描述自动生成：上传图片不再需要手填描述**。

### 新增

- **描述生成模块** `pulse/services/media/describe.py`：
  - 视觉模型路径：调用 OpenAI 兼容的多模态接口识别画面（配置 `PULSE_VISION_API_KEY`
    即启用），提示词强制"只描述肉眼可见内容，禁止推测材质 / 公差 / 单重 / 产能"；
  - 基础信息兜底：未配置模型时，用文件名、拍摄时间、分辨率、品类等**可见事实**
    自动拼装描述，并明确提示"画面内容待补充"；
  - 参数防编造：`find_unverified_claims` 检出模型输出的牌号 / 吨位 / 公差 / 产能等
    画面不可见参数，作为警告返回人工核对（AGENTS.md §3.7）。
- **控制台交互改造**：上传表单不再要求手填摘要 / 关键词 / 细节；选择图片或切换品类即调用
  `POST /api/describe` 自动生成并回填（仍可直接修改）；入库接口在描述为空时自动生成。
- **配置样例** `.env.example`：视觉模型地址 / 密钥 / 模型名 / 超时。
- **测试** 13 项（`pulse/services/media/tests/test_describe.py` 9 项 + 控制台 4 项）。

### 说明

- 未配置视觉模型不报错：自动降级为"基础信息描述"；模型调用失败也会降级并在页面提示原因。
- 图片尺寸解析用标准库完成（PNG / JPEG / GIF / BMP / WebP），不新增第三方依赖。
- 测试基线：**156 项全绿**（1.2.0 的 143 项 + 本次 13 项）。

---

## [1.2.0] — 2026-09-11

**一键启动：给非技术同事的素材库控制台入口**。

### 新增

- **双击启动**：仓库根目录 `一键启动_素材库控制台.bat`——自动进入仓库目录、
  调用 `.venv` 解释器、挑选空闲端口、自动打开浏览器，并用中文提示启动状态与容量预警。
- **一键建快捷方式**：`创建桌面快捷方式.bat`——在桌面生成「Pulse素材库」图标，
  以后从桌面双击即可。
- **启动器模块** `pulse/console/launcher.py`：路径解析（缺目录时给中文提示，不抛栈）、
  端口挑选（8765 被占用时自动换端口）、地址构造、浏览器自动打开；
  `python -m pulse.console.launcher` 也可直接调用。
- **页面「三步上手」引导**：控制台首页顶部说明上传、看预警、标记使用三个动作。
- **测试** 10 项（`pulse/console/tests/test_launcher.py`）。

### 说明

- 台账文件默认写到 `.pulse/media_ledger.sqlite3`，已加入 `.gitignore`，不会污染仓库。
- `.bat` / `.cmd` 在 `.gitattributes` 中固定 CRLF，避免 cmd 解析异常。
- 测试基线：**143 项全绿**（1.1.0 的 133 项 + 本次 10 项）。

---

## [1.1.0] — 2026-09-11

**素材库入库与召回策略（新增媒体资产域 + 图形化控制台）**。

### 新增

- **媒体资产库域** `pulse/services/media/`：
  - 素材目录扫描与描述解析：`catalog.py`
  - 使用台账（sqlite，幂等）：`ledger.py`
  - 召回策略（15 天冷却 + 新图加权 + 加权随机采样 + 容量预警）：`policy.py`
  - 实拍图入库（写描述 MD、重建汇总索引、内容哈希查重）：`ingest.py`
  - 新增素材登记表：`registry.py`、策略配置：`config.py`
- **图形化素材库控制台** `pulse/console/`：纯标准库 HTTP 服务 + 内嵌页面，
  支持上传入库、容量红色预警、冷却状态查看、模拟召回与"标记已用于内容"。
- **素材库契约** `pulse/contracts/MEDIA_LIBRARY.md`（媒体域接口与业务规则）。
- **测试** 32 项（`pulse/services/media/tests/` 26 项 + `pulse/console/tests/` 6 项）。

### 业务决策（2026-09-11 确认）

- **冷却触发口径**：只有素材**实际进入生成内容 / 发布**才计入冷却；检索命中不计。
- **品牌级素材池**：素材池按品牌隔离，当前品牌 = `沧州菲美得`。
- **红色预警阈值**：可召回容量低于 **112 张**（约一周用量）即红色预警。
- **存量口径**：既有图库全部视为**老图**，不论此前是否被使用过，不享受新图加权。
- 既有图库无入库时间，故新增素材登记表 `_媒体登记表.json` 区分新图 / 老图。

### 说明

- 测试基线：**132 项全绿**（1.0.0 的 100 项 + 本次 32 项）。
- 工业件铁律（AGENTS.md §3.6）在入库层强制执行：仅接受实拍图，不接受文生图产品图。

---

## [1.0.0] — 2026-09-10

首个冻结版本：**接口契约冻结 + 发布网关域交付**。

### 新增

- **工程接口契约 v1.0（冻结）**：`pulse/contracts/INTERFACES.md` 定义统一发布对象 `UnifiedPost`、
  平台白名单（VK / TikTok 不得进入枚举）、素材 `license_status` 规则、`ErrorClass` 三分法
  （可重试 / 不可重试 / 轮询）、ID 前缀与时间前缀规范、`PublishResult` "受理 ≠ 发布" 语义。
- **跨域共用类型**：`pulse/shared/`（`models`、`enums`、`ids`、`__init__`）与 20 项契约基线测试。
- **发布网关域（A2）**：`pulse/services/publish/`
  - 网关主流程与幂等键透传：`gateway.py`
  - 状态机与本地存储：`state.py`、`store.py`
  - 平台适配器：LinkedIn、YouTube，以及测试用 `fake.py`；统一基类与错误模型：
    `base.py`、`errors.py`
  - **半自动兜底**（P0 能力，非降级方案）：`semi_auto.py`
  - 传输层：`transport.py`
  - 域内单元测试：适配器、契约形状、网关、待定终结、半自动（共 80 项）
- **多 Agent 协同治理基线**：`AGENTS.md`（目录所有权与铁律）、`pulse/docs/多Agent协同开发方案.md`
  （分工原理与波次设计）、`pulse/docs/dispatch/`（A1–A6 派工单）、`pulse/tasks/TASKS.md`（任务板）。
- **版本管理与持续集成**：`.gitattributes`（统一 LF）、`.github/workflows/ci.yml`
  （`main` 推送与 PR 跑全量 pytest，`v*` 标签自动创建 Release）。

### 说明

- 测试基线：**100 项全绿**（`pulse/shared/tests` 20 项 + 发布网关域 80 项），
  运行方式 `python -m pytest -p no:cacheprovider`。
- 平台优先级：P0-A = LinkedIn + YouTube；P1 = Reddit + Facebook；P2 = Instagram；
  **VK 冻结**（合规红线）；TikTok 暂不投入。
- 业务数据（`菲美得产品图片/`、`外贸公司/`、`RAG知识库/`）属项目数据，不入代码库。

### 待办（不在 1.0.0 范围内）

- A1 内容生产域、A3 调度与账号域仍在并行开发中，交付后进入 1.1.0。
- A4 合规 / A5 平台前端 / A6 独立验证于 W2 波次启动。
