# 媒体资产库与召回策略契约 v1.1

> 所有者：root　｜　冻结日期：2026-09-11　｜　最近升版：2026-09-11（v1.1）　｜　实现：`pulse/services/media/`
> 关联：`AGENTS.md` §3 铁律、`pulse/contracts/INTERFACES.md`（发布契约）

本契约定义**素材入库**与**素材召回**两条链路。发布契约（INTERFACES.md）仍然有效，
两者通过素材 `license_status` 衔接：入库素材默认 `owned`，`pending` 不得进入发布。

---

## 1. 领域对象

| 对象 | 说明 | 关键字段 |
| --- | --- | --- |
| MediaAsset | 一张可召回的实拍素材 | `asset_id`(`img_` 前缀)、`file_name`、`process`、`sub_process`、`source_path`、`description_path`、`keywords`、`added_at`、`is_legacy`、`brand` |
| MediaCandidate | 召回输入 | `asset`、`similarity`（向量检索给出，0–1） |
| RecallPick | 召回输出 | `asset_id`、`similarity`、`weight`、`is_new`、`category`、`summary` |
| CapacityReport | 容量与预警 | `total`、`available`、`cooling`、`new`、`legacy`、`threshold`、`alert` |

素材 ID 规则：`img_` + `sha1(描述文件相对路径)[:12]`。描述文件相对路径不变则 ID 稳定。

---

## 2. 业务规则（冻结）

### 2.1 冷却（B1）

- 触发时点：素材**实际进入生成内容或发布**时调用 `RecallLedger.mark_used`；
  仅被检索命中不写台账、不消耗冷却。
- 窗口：同一素材在同一品牌下，最近一次 `used` 之后 **15 天**内不得被召回。
- 幂等：同一 `(asset_id, brand, event, content_id)` 重复写入只记一次，
  生成失败重试不得重复消耗冷却。
- 隔离：冷却按 `brand` 隔离，跨品牌互不影响。

### 2.2 时效优先（B2）

- 召回权重 = `similarity × freshness_boost`；新图（登记表中存在且入库 ≤ 7 天）乘 2.0，
  老图权重等于 `similarity`。
- **既有图库全部为老图**：登记表无记录 → `is_legacy = True`，永久不加权。
- 新鲜度加权只影响概率，不构成硬性置顶；相似度为 0 的候选一律不召回。

### 2.3 随机性（B3）

- 候选排序采用**无放回加权随机采样**（A-Res：`key = u ** (1 / weight)`，取前 K），
  禁止"永远返回相似度最高的同一张"。

### 2.4 容量预警（B4）

- `available = total - cooling`；`available < 112`（约一周用量）时 `alert = "red"`，
  控制台显示红色横幅；否则 `alert = "ok"`。
- 阈值来自 `RecallConfig.capacity_red_threshold`，不得在调用点硬编码。

### 2.5 素材来源（B5）

- 入库仅接受实拍图（`.jpg/.jpeg/.png/.webp/.bmp`，默认 ≤ 20 MB）；
  工业件禁止文生图产品图（AGENTS.md §3.6）。
- 同一内容哈希重复入库一律拒绝，返回 `DuplicateMediaError`。

### 2.6 入库许可（B6，v1.1 新增）

- **只有完成视觉识别的图片才允许入库**。识别成功（`source == "vision"`）时签发
  入库凭据 `vision_ticket`，入库请求必须携带；凭据绑定**图片内容哈希**，
  有效期 `VISION_TICKET_TTL_SECONDS`（默认 1 小时）。
- 无凭据 / 凭据伪造 / 凭据与图片内容不符 → 拒绝入库（HTTP **403**，
  响应体含 `vision_required: true`）。服务端同样校验，绕过页面直接调接口也进不来。
- 未提供描述时由服务端自行识别，识别不成功同样拒绝。
- 应急开关：`PULSE_MEDIA_REQUIRE_VISION=0` 可临时关闭（仅限视觉服务长时间故障时使用）。

### 2.7 品类判定（B7，v1.1 新增）

- 品类（`process` / `sub_process`）**由识别结果决定**，不写死默认值。
  优先级：模型判定（必须落在既有品类清单内）→ 关键词兜底 → 调用方已选品类 → 留空待人工。
- 可选品类清单由目录扫描得出（`load_categories`），不硬编码；目录变了清单自动跟着变。
- 品类的 `sub_process` **可为空**：库里"人员"这类品类本来就没有子目录。

### 2.8 索引文件不算素材（B8，v1.1 新增）

- 汇总索引文件不是素材、不计入容量、不参与召回。判定方式：
  文件名以 `00_` 开头，**或**描述头部 `content_type: "汇总索引"`。
- 只比对固定文件名 `00_汇总索引.md` 是不够的——库里实际存在
  `00_发泡工段汇总索引.md` 这类异名索引。

### 2.9 分平台召回（B9，v1.1 新增）

- 召回可指定 `platform`，按该平台在《四平台推荐风格与方式报告》中要求的**画面位次**
  逐格挑图（`pulse/services/media/platforms.py`）。
- 位次匹配顺序：顶层品类过滤 → 关键词命中（命中关键词字段强于命中摘要段落）
  → 首选形态硬筛选（视频 / 图集）→ 只在**最高分档**（≥ 最高分 95%）内随机。
- 位次挑不到素材时返回空 `picks`，页面提示补拍；若"有素材但全在冷却期"，
  响应中 `blocked_by_cooldown = true`，提示等待冷却而非补拍。
- 平台配置**不是平台优先级的事实来源**：优先级以 `pulse/contracts/INTERFACES.md` 与
  `pulse/shared/enums.py` 为准。

---

## 3. 入库产物约定

一次 `add_image` 必须同时产出：

1. 素材文件：`<media_root>/<process>/<sub_process>/<file_name>`
2. 描述文件：`<rag_root>/<process>/<sub_process>/<stem>.md`，头部字段
   `source_file / source_path / process / sub_process / content_type / keywords / added_at / brand`
3. 汇总索引：重建同目录 `00_汇总索引.md`（含总数、图片数、视频数、明细表）
4. 登记记录：`_媒体登记表.json`（决定该素材算新图还是老图）

写入失败必须保持原状：先落素材与描述，最后写登记表；登记表采用原子替换。

---

## 4. 控制台接口

| 方法 | 路径 | 语义 |
| --- | --- | --- |
| GET | `/` | 素材库页面（容量横幅、上传表单、按平台召回、素材清单） |
| GET | `/api/state` | 素材清单 + 容量报告 + 每张图冷却状态 + 可选品类 `categories` + 平台清单 `platforms` |
| POST | `/api/describe` | multipart（`file`、`process`、`sub_process`）→ 自动描述 + 品类判定 + 入库凭据 `vision_ticket` |
| POST | `/api/assets` | multipart 上传入库（`file`、`process`、`sub_process`、`keywords`、`summary`、`details`、`vision_ticket`） |
| POST | `/api/usage` | 标记素材已用于内容 / 发布（`asset_id`、可选 `content_id`） |
| POST | `/api/recall` | 召回（可选 `platform`、`query`、`top_k`）；带 `platform` 时返回按位次分组的 `slots` |

错误语义：格式/体积不合规、重复素材、参数缺失一律 **400**；入库许可未满足一律 **403**
（响应体含 `vision_required: true`）。响应体统一为 `{ok:false, error}`。

---

## 5. 契约变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.1 | 2026-09-11 | 补 B6 入库许可、B7 品类判定、B8 索引文件不计入素材、B9 分平台召回；控制台接口表补 `/api/describe`、`vision_ticket` 与 `categories`/`platforms` 字段，并订正错误码（许可不足为 403） |
| 1.0 | 2026-09-11 | 首次冻结：入库、冷却、时效优先、加权随机、容量预警 |
