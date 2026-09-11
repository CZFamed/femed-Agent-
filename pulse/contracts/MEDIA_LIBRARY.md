# 媒体资产库与召回策略契约 v1.4

> 所有者：root　｜　冻结日期：2026-09-11　｜　最近升版：2026-09-11（v1.4）　｜　实现：`pulse/services/media/`
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

- 入库仅接受**实拍**素材：图片 `.jpg/.jpeg/.png/.webp/.bmp`（≤ 20 MB，`MAX_IMAGE_BYTES`）；
  视频 `.mp4/.mov/.avi/.mkv/.webm`（≤ 200 MB，`MAX_VIDEO_BYTES`）。
  工业件禁止文生图产品图（AGENTS.md §3.6）。
- 同一内容哈希重复入库一律拒绝，返回 `DuplicateMediaError`。

### 2.5.1 视频识别（B5.1，v1.4 新增）

- **视觉接口不支持视频输入**：实测 `input_video` 会被拒绝
  （`unknown variant input_video, expected one of input_text, o…`），
  把视频字节塞进 `input_image` 也报 "unsupported image"。
  因此视频一律**先用 ffmpeg 抽静帧、再当"一段连续画面的多张图"**送模型。
- 抽帧参数：`PULSE_VIDEO_FRAMES`（默认 4）、`PULSE_VIDEO_FRAME_WIDTH`（默认 768）、
  时间点取 10%–90% 均匀分布（避开常见黑场）。
- ffmpeg 来源优先级：`PULSE_FFMPEG` → `imageio-ffmpeg` 自带 → PATH。
  **依赖已随项目提供**（`imageio-ffmpeg`，自带静态 ffmpeg 二进制）。
- **token 实测**（同一段实拍视频）：1 帧 @1280px = 791；
  4 帧 @1280px = 3,008；**4 帧 @768px = 1,184**；6 帧 @768px = 1,750
  （服务端按像素量折算，故缩小尺寸比减少帧数更省）。默认取 4 帧 @768px：
  成本与一张原图（约 1,170）持平。
- 视频描述的落盘命名沿用库里既有约定：**`<文件名带扩展>.md`**（如 `1.mp4.md`），
  头部 `content_type: "视频描述"`；图片仍是 `<文件名去扩展>.md`。
- 视频**不计入图片容量**（0），单独统计在 `CapacityReport.videos`。

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

### 2.10 素材预览（B10，v1.2 新增）

- 控制台可预览召回的素材。定位素材文件**不得使用描述头部的 `source_path`**——
  那里写的是别的机器上的绝对路径（`D:/菲美得/…`）；可靠做法是从**描述文件的相对路径**
  推出素材目录，再按文件名查找。
- 安全约束：文件名只取 basename（防路径穿越），解析结果必须落在 `media_root` 内。
- 服务端支持 `Range` 请求（回 206），否则视频无法拖动进度。

### 2.11 按平台短文生成（B11，v1.2 新增）

- 短文按《四平台推荐风格与方式报告》§2.1/§3 的**文案结构**生成，规格固化在
  `pulse/services/media/captions.py` 的 `CaptionSpec`：
  LinkedIn `钩子行→痛点→能力证据→产品范围→CTA→标签`（600–1,200 字符、3–5 标签、0–2 emoji）；
  Facebook `场景描述→一句结论→提问收尾→标签`（150–400、1–2 标签、2–4 emoji）；
  TikTok `钩子→一句结论→标签`（≤150、4–5 标签）；VK `企业介绍→工艺→产品→设备→合作方式`
  （500–1,500、2–5 标签，**俄语**，另存英语母版）。
- **事实来源仅限给定素材的描述**。严禁编造材质牌号、公差、单重、月产能、检测结果、认证、
  客户名称；模型输出会被 `find_unverified_claims` 与禁用语表（报告 §8.3）复核，
  命中即回传警告。
- 外链口径：LinkedIn / Facebook / TikTok **正文不得出现链接**（VK 允许）。
  校验结果为一条独立条目回显，不阻断生成——让用户自己判断。
- 生成**不做静默降级**：模型不可用或输出不完整时直接报错，不返回模板拼凑的文本。

### 2.12 素材包导出（B12，v1.3 新增）

- 把召回结果导出成人能直接拿去用的文件夹：
  按**位次顺序**复制素材原图（`01_…`、`02_…`）、`说明.txt`（每位次要什么画面、实际用了哪张）、
  有短文时另写 `文案.txt`（正文 + 标签 + 第一条评论 + 英语母版 + 发布前自查）。
- **零修饰**：只做字节级复制（`shutil.copy2`），不裁不压不调色——报告 §0.1 取消了全部后期加工。
- **不覆盖已有目录**：重名自动加序号，绝不静默盖掉上一次导出。
  目录名 = `<平台名>_<YYYYMMDD-HHMM>`，落在 `PULSE_EXPORT_DIR`；
  未配置时默认 `<media_root>/../Pulse导出/`。
- 导出的位次与素材由页面**原样回传**，保证"所见即所得"——
  导出时重跑召回会因加权随机挑出别的图。
- 同一条素材被多个位次选中时只复制一份，`说明.txt` 注明与哪个位次同图。
- 素材文件缺失或复制失败时**如实计入 `missing` 并在 `说明.txt` 中标注**，
  界面显示"有 N 条素材没找到"，不静默少导。

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
| GET | `/api/asset-image` | 素材预览（`asset_id`）；回传图片/视频本体，支持 `Range`（206） |
| POST | `/api/caption` | 按平台模式生成短文（`platform`、可选 `asset_ids`、`extra_note`） |
| POST | `/api/export` | 导出素材包（`platform`、`slots`[{order,role,asset_id}]、可选 `caption`）；返回落盘目录 |

错误语义：格式/体积不合规、重复素材、参数缺失一律 **400**；入库许可未满足一律 **403**
（响应体含 `vision_required: true`）。响应体统一为 `{ok:false, error}`。

---

## 5. 契约变更记录

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| 1.4 | 2026-09-11 | B5 扩到实拍视频（新增 `MAX_VIDEO_BYTES`）；新增 B5.1 视频识别（接口不支持视频输入 → ffmpeg 抽帧 + 多图，含 token 实测、命令来源、`<文件名>.mp4.md` 命名与容量口径） |
| 1.3 | 2026-09-11 | 新增 B12 素材包导出（位次顺序、零修饰字节复制、不覆盖、所见即所得、缺失如实报告）；控制台接口表补 `/api/export` |
| 1.2 | 2026-09-11 | 新增 B10 素材预览（禁用 `source_path`、需防穿越、支持 Range）与 B11 按平台短文生成（规格、事实边界、外链口径、禁止静默降级）；控制台接口表补 `/api/asset-image` 与 `/api/caption` |
| 1.1 | 2026-09-11 | 补 B6 入库许可、B7 品类判定、B8 索引文件不计入素材、B9 分平台召回；控制台接口表补 `/api/describe`、`vision_ticket` 与 `categories`/`platforms` 字段，并订正错误码（许可不足为 403） |
| 1.0 | 2026-09-11 | 首次冻结：入库、冷却、时效优先、加权随机、容量预警 |
