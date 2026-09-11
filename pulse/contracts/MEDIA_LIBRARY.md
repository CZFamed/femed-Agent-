# 媒体资产库与召回策略契约 v1.0

> 所有者：root　｜　冻结日期：2026-09-11　｜　实现：`pulse/services/media/`
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
| GET | `/` | 素材库页面（容量横幅、上传表单、素材清单） |
| GET | `/api/state` | 素材清单 + 容量报告 + 每张图冷却状态 |
| POST | `/api/assets` | multipart 上传入库（`file`、`process`、`sub_process`、`keywords`、`summary`、`details`） |
| POST | `/api/usage` | 标记素材已用于内容 / 发布（`asset_id`、可选 `content_id`） |
| POST | `/api/recall` | 按策略召回（`query`、可选 `top_k`） |

错误语义：格式/体积不合规、重复素材、参数缺失一律 400，响应体 `{ok:false, error}`。

---

## 5. 契约变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.0 | 2026-09-11 | 首次冻结：入库、冷却、时效优先、加权随机、容量预警 |
