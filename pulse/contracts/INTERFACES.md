# Pulse 工程接口契约（冻结版 v1.1）

> 所有人：**root**　｜　状态：**冻结（FROZEN）**　｜　冻结日期：2026-09-10　｜　最近升版：2026-09-11（v1.1）
>
> **本文件是并行开发的地基。** 所有 agent 只读引用，不得修改。
> 需要变更 → 发消息给 root，说明冲突点 + 建议方案 + 影响范围，由 root 统一升版。
>
> 版本变更记录写在文末，每次升版必须写明"哪些已完成的代码需要同步调整"。

---

## 1. 设计前提

| 项 | 取值 |
| --- | --- |
| 平台优先级 | **P0-A**: LinkedIn, YouTube　**P1**: Reddit, Facebook, **VK**　**P2**: Instagram　**暂不投入**: TikTok |
| VK 状态 | **转为正式运营（2026-09-11 法务评审通过）**，见 §10 变更记录与 AGENTS.md §3.4 留痕义务 |
| 业务场景 | B2B 工业铸件出海（采购/供应链为决策人） |
| 语言 | 英语为主，中英对照供审校 |
| 发布模式 | API 自动发布为主 + **半自动兜底为 P0 能力**（非降级方案） |

---

## 2. 统一发布对象 `UnifiedPost`

发布网关对外的唯一入参。各 Adapter 负责翻译成平台私有 payload。
**这是 A2 与 A3 之间唯一的接口**，双方都不得绕过它直接传平台参数。

```jsonc
{
  "unified_post_id": "up_01J8XYZ",        // 幂等键，全局唯一，透传至平台（能传则传）
  "platform": "linkedin",                  // linkedin | youtube | reddit | facebook | instagram | vk
  "account_id": "acct_01",
  "source_id": "src_20260910_001",         // 溯源：本贴派生自哪条核心内容
  "variant_id": "var_20260910_003",

  "content_type": "text",                  // text | image | video
  "caption": {
    "text": "…",                           // 英文主文案
    "text_zh": "…",                        // 中文对照（供审校，不外发）
    "lang": "en"
  },
  "title": "…",                            // YouTube / Reddit 必填，其他平台可空
  "hashtags": ["#casting", "#foundry"],    // 已含 '#'，不含空格

  "media": [                               // 允许空数组（纯文本贴）
    {
      "kind": "image",                     // image | video
      "url": "s3://pulse-media/...",
      "mime": "image/jpeg",
      "width": 1200, "height": 1500,       // 图片必填，用于比例校验
      "duration_s": null,                  // 视频必填
      "license_status": "owned"            // owned | licensed（禁止其他取值）
    }
  ],

  "scheduled_at": "2026-09-11T03:00:00+05:30",  // ISO8601 带时区偏移，必须含 offset
  "options": {},                           // 平台专属，见下表

  "compliance": {
    "ai_generated_disclosure": false,      // 工业实拍内容通常为 false
    "checked_at": "2026-09-11T02:55:00+05:30",
    "blocked": false,                      // true 时网关必须拒绝发布
    "findings_ref": []                     // compliance_findings.id 列表
  }
}
```

### 2.1 `options` 平台字段

| 平台 | 字段 | 必填 | 说明 |
| --- | --- | --- | --- |
| linkedin | `linkedin_visibility` | 是 | `PUBLIC` \| `CONNECTIONS` \| `LOGGED_IN` |
| linkedin | `author_urn` | 是 | 公司页 URN（`urn:li:organization:…`） |
| youtube | `privacy_status` | 是 | `public` \| `unlisted` \| `private` |
| youtube | `category_id` | 是 | 默认 `28`（Science & Technology） |
| youtube | `made_for_kids` | 是 | 默认 `false` |
| facebook | `page_id` | 是 | 目标主页 |
| reddit | `subreddit` | 是 | 由**人工确认**后填入 |
| reddit | `flair` | 否 | 版规要求时必填 |
| vk | `owner_id` | 是 | 社区 ID，**必须为负数**（VK 以负值表示社区墙；正数会发到个人墙） |
| vk | `from_group` | 否 | 以社区名义发布，默认 `true` |

> 工业内容**不使用** `made_for_kids`、`ai_generated_disclosure = true`（除非确实用了 AI 生成画面）。

> **VK 语言口径（v1.1 新增，流程约束而非代码校验）**：VK 是四平台中唯一的非英语平台，
> 对外文案必须为**俄语**，以英语母版翻译派生并保留审校记录。
> `Caption` 结构目前只有 `text`（对外主文案）与 `text_zh`（中文审校），
> 因此 VK 帖的 `caption.text` 应放俄语；是否为此增加 `text_ru` 字段留待 A1 内容域提出。

---

## 3. 状态机与枚举

### 3.1 `VariantStatus`

```
draft → pending_review → approved | rejected | needs_revision
```

### 3.2 `ScheduleStatus`

```
pending → scheduled → publishing → published | failed
                    ↘ cancelled
```

### 3.3 `PublishJobStatus`（含异步回执终态）

```
queued → dispatching → publishing → published
                     ↘ pending_finalize → published | failed
                     ↘ failed → retrying → publishing
                     ↘ rejected          （政策拒绝，终态）
                     ↘ cancelled
```

**关键约束**：平台返回"已受理"时**只能**进 `pending_finalize`，**绝不能**直接置 `published`。
`pending_finalize` 必须设置超时（默认 30 分钟），超时 → 告警 + 人工核对。

### 3.4 `ErrorClass`（错误分级）

| 值 | 触发 | 处置 | 是否重试 |
| --- | --- | --- | --- |
| `rate_limited` | 429 / quota 超限 | 指数退避 | 是（重试前配额预检） |
| `transient` | 5xx / 网络超时 | 有限次重试 | 是 |
| `auth_expired` | 401 / token 失效 | 刷新 Token 后重试 | 是（刷新失败转人工） |
| `media_processing` | 平台异步处理中 | 转 `pending_finalize` | 轮询 |
| `policy_rejected` | 内容/政策拒绝 | 转 `rejected` + 告警 | **否** |
| `validation_error` | payload 不合法 | 转 `failed` + 告警 | 否（属代码缺陷） |

### 3.5 `Severity`（合规发现项）

| 值 | 含义 | 处置 |
| --- | --- | --- |
| `block` | 硬拦截 | 默认不可绕过，需管理员权限豁免 |
| `warn` | 提示 | 可人工豁免，**必须留痕** |

---

## 4. Adapter 抽象接口（A2 实现，A5 不得依赖具体 Adapter）

```python
# pulse/services/publish/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass(frozen=True)
class PublishResult:
    ok: bool
    status: str                  # publishing | pending_finalize | published | failed | rejected
    platform_post_id: str | None = None
    post_url: str | None = None
    error_class: str | None = None      # ErrorClass 值
    error_message: str | None = None
    raw: dict | None = None             # 平台原始响应，仅入日志不入库

class PlatformAdapter(ABC):
    """每个平台一个实现。所有方法必须幂等。"""

    platform: str                # "linkedin" 等，与 UnifiedPost.platform 一致

    @abstractmethod
    async def validate(self, post: "UnifiedPost") -> list[str]:
        """发布前校验 payload 合法性，返回错误消息列表（空 = 通过）。不发起网络请求。"""

    @abstractmethod
    async def find_existing(self, post: "UnifiedPost") -> str | None:
        """幂等兜底：查平台是否已存在该 unified_post_id 的贴文，返回 platform_post_id。
        平台不支持透传幂等键时必须实现。"""

    @abstractmethod
    async def publish(self, post: "UnifiedPost", credential: "Credential") -> PublishResult:
        """执行发布。返回 publishing / pending_finalize / published / failed / rejected。"""

    @abstractmethod
    async def poll_finalize(self, platform_post_id: str, credential: "Credential") -> PublishResult:
        """收敛 pending_finalize 的终态。平台为同步返回时返回 ok=True, status='published'。"""

    @abstractmethod
    async def fetch_metrics(self, platform_post_id: str, credential: "Credential") -> dict:
        """FR-8 数据回捞（P1）。M1/M2 可返回 {}。"""
```

**半自动导出**（P0 能力，与 Adapter 平级，不得省略）：

```python
class SemiAutoExporter(ABC):
    """生成可复制内容 + 平台官方发布入口，供人工确认后发布。"""
    @abstractmethod
    def export(self, post: "UnifiedPost") -> "SemiAutoBundle":
        """返回 {text, media_paths, deep_link, checklist}。"""
```

---

## 5. 队列消息契约（Celery）

| 任务名 | 生产方 | 消费方 | 载荷 |
| --- | --- | --- | --- |
| `pulse.content.generate_variant` | A5/编排 | A1 | `{brief_id, platform}` |
| `pulse.compliance.check` | A1 | A4 | `{variant_id}` |
| `pulse.schedule.enqueue` | A5 | A3 | `{schedule_id}` |
| `pulse.publish.dispatch` | A3 | A2 | `{job_id, unified_post_id}` |
| `pulse.publish.finalize` | A3（延迟重投） | A2 | `{job_id}` |
| `pulse.metrics.fetch` | A3 | A2 | `{job_id}`（P1） |

**规则**：队列只传 **ID**，不传业务对象。消费方自行从库里取。
这样避免消息体版本漂移，也让重试天然幂等。

**延迟任务**一律使用 Celery 的 `countdown` / `eta`，**禁止使用 cron 硬排**（便于取消与重排）。

---

## 6. 数据模型（PostgreSQL DDL 摘要）

字段名为**契约**，实现必须一致。类型可合理细化。全部表含 `created_at` `updated_at`。

```sql
-- 账号矩阵
CREATE TABLE accounts (
    id            TEXT PRIMARY KEY,              -- acct_xxx
    platform      TEXT NOT NULL,
    display_name  TEXT NOT NULL,
    region        TEXT,
    timezone      TEXT NOT NULL,                 -- IANA，如 Asia/Kolkata
    status        TEXT NOT NULL DEFAULT 'active',-- active | paused | revoked
    quota_config  JSONB NOT NULL DEFAULT '{}'    -- 令牌桶/冷却/滚动窗口
);

-- 凭据（加密存储，业务层不可见明文）
CREATE TABLE credentials (
    account_id          TEXT PRIMARY KEY REFERENCES accounts(id),
    provider            TEXT NOT NULL,
    encrypted_payload   BYTEA NOT NULL,
    expires_at          TIMESTAMPTZ,
    refresh_expires_at  TIMESTAMPTZ,
    status              TEXT NOT NULL DEFAULT 'valid'  -- valid | expired | revoked
);

-- 品牌规范（原文档缺失，本契约补齐）
CREATE TABLE brand_guides (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    version     INT NOT NULL DEFAULT 1,
    payload     JSONB NOT NULL,   -- 语气/禁用词/参数披露级别/视觉规范
    is_active   BOOLEAN NOT NULL DEFAULT true
);

-- 核心内容与平台派生
CREATE TABLE contents (
    id              TEXT PRIMARY KEY,            -- src_xxx
    brief_id        TEXT NOT NULL,
    title           TEXT NOT NULL,
    body_seed       TEXT,
    brand_guide_id  TEXT REFERENCES brand_guides(id)
);

CREATE TABLE variants (
    id          TEXT PRIMARY KEY,                -- var_xxx
    source_id   TEXT NOT NULL REFERENCES contents(id),
    platform    TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    fields      JSONB NOT NULL DEFAULT '{}',     -- caption/title/hashtags/...
    ai_score    REAL
);

-- 素材（source + license_status 支撑版权审计）
CREATE TABLE media_assets (
    id              TEXT PRIMARY KEY,
    variant_id      TEXT REFERENCES variants(id),
    kind            TEXT NOT NULL,               -- image | video
    oss_key         TEXT NOT NULL,
    source          TEXT NOT NULL,               -- owned | licensed
    license_status  TEXT NOT NULL,               -- owned | licensed | pending
    meta            JSONB NOT NULL DEFAULT '{}'  -- 尺寸/时长/拍摄日期/内容标签
);

-- 审批留痕
CREATE TABLE approvals (
    id          BIGSERIAL PRIMARY KEY,
    variant_id  TEXT NOT NULL REFERENCES variants(id),
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,                   -- approve | reject | needs_revision | batch_approve
    diff        JSONB,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 合规发现项（结构化输出）
CREATE TABLE compliance_findings (
    id          BIGSERIAL PRIMARY KEY,
    variant_id  TEXT NOT NULL REFERENCES variants(id),
    rule        TEXT NOT NULL,                   -- 规则 ID
    severity    TEXT NOT NULL,                   -- block | warn
    message     TEXT NOT NULL,
    position    JSONB,                           -- 定位：字段名 + 字符偏移
    waived_by   TEXT,                            -- 豁免人（warn 才可填）
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 制裁与出口管制筛查（原文档缺失，本项目必须新增）
CREATE TABLE sanctions_screenings (
    id            BIGSERIAL PRIMARY KEY,
    subject       TEXT NOT NULL,                 -- 客户名/主体名
    result        TEXT NOT NULL,                 -- clear | hit | review_required
    lists_checked TEXT[] NOT NULL,               -- OFAC SDN / BIS Entity List / EU ...
    evidence      JSONB,
    checked_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 排期（显式存 timezone，便于多时区聚合）
CREATE TABLE schedules (
    id            TEXT PRIMARY KEY,              -- sched_xxx
    variant_id    TEXT NOT NULL REFERENCES variants(id),
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    timezone      TEXT NOT NULL,
    scheduled_at  TIMESTAMPTZ NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending'
);

-- 发布任务
CREATE TABLE publish_jobs (
    id              TEXT PRIMARY KEY,            -- job_xxx
    schedule_id     TEXT REFERENCES schedules(id),
    unified_post_id TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'queued',
    attempts        INT NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ,
    error_class     TEXT,
    error_message   TEXT
);
CREATE UNIQUE INDEX ux_jobs_unified ON publish_jobs(unified_post_id);

-- 发布结果与回捞
CREATE TABLE publish_results (
    id                BIGSERIAL PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES publish_jobs(id),
    platform          TEXT NOT NULL,
    platform_post_id  TEXT,
    post_url          TEXT,
    metrics           JSONB NOT NULL DEFAULT '{}',
    fetched_at        TIMESTAMPTZ
);
```

**幂等保证**：`publish_jobs.unified_post_id` 唯一索引 + Adapter 的 `find_existing()` 双层兜底。

---

## 7. REST API（A5 实现，路径冻结）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/briefs` | 提交生成 brief |
| GET | `/api/v1/contents/{id}/variants` | 查看派生草稿 |
| PATCH | `/api/v1/variants/{id}/status` | 审批（approve/reject/needs_revision） |
| POST | `/api/v1/variants/{id}/schedule` | 入排期（指定时间或 best-time） |
| PATCH | `/api/v1/schedules/{id}` | 暂停/取消/修改排期 |
| POST | `/api/v1/schedules/{id}/publish` | 立即发布 |
| GET | `/api/v1/schedules/{id}/semi-auto` | **半自动导出**（P0，原文档缺失） |
| GET | `/api/v1/accounts` | 账号列表/额度/健康度 |
| POST | `/api/v1/accounts/{id}/oauth` | 发起 OAuth 接入 |
| DELETE | `/api/v1/accounts/{id}/credential` | 吊销凭据并熔断（管理权限） |
| POST | `/api/v1/compliance/screen` | 制裁与出口管制筛查 |

统一错误体：`{"error": {"code": "...", "message": "...", "details": {}}}`

---

## 8. 跨域数据流（谁调用谁）

```
A5 API ──→ A1 content.generate_variant ──→ A4 compliance.check
                                              │
                     ┌────────────────────────┘
                     ▼
              人工审批（A5 控制台）
                     │ approved
                     ▼
              A3 schedule.enqueue ──→ A2 publish.dispatch ──→ 平台 API
                     ▲                        │
                     └── pending_finalize ────┘
                     │
                A2 publish.finalize ──→ publish_results
```

**依赖方向是单向的**：A5 → A1 → A4 → A3 → A2。反向依赖（如 A1 直接调 A2）**禁止**，否则无法并行开发。

---

## 9. 并行开发的分工接口

每个域只需实现自己的入口与出口，**中间用桩（stub）代替**：

| 域 | 入口（别人给你的） | 出口（你给别人的） | 桩 |
| --- | --- | --- | --- |
| A1 Content | `contents`, `briefs` | `variants` + `media_assets` 写入 | 生成结果先用固定文案，不接真模型 |
| A2 Publish | `publish.dispatch` 消息 + UnifiedPost | `PublishResult` + `publish_results` | 平台 HTTP 调用用 fake adapter |
| A3 Scheduler | `schedule.enqueue` | `publish.dispatch` 消息 | 队列用 eager 模式（同步执行） |
| A4 Compliance | `variant_id` | `compliance_findings` + 是否 block | 词库先用最小集 |
| A5 Platform | HTTP 请求 | 上面全部 | 后端服务用 fake |

**验收基线**：任一方在**不启动其他域**的前提下，用自己的桩跑通全流程。

---

## 10. 版本变更记录

| 版本 | 日期 | 变更 | 需同步调整的代码 |
| --- | --- | --- | --- |
| v1.1 | 2026-09-11 | **VK 由"冻结"转为正式运营（P1）**。依据：VK 法务评审通过（《菲美得_四平台推荐风格与方式报告_v1》§8.1）。`Platform` 枚举新增 `vk`；新增 VK 必填项 `owner_id`（负数社区 ID）与可选 `from_group`；补 VK 俄语口径说明。TikTok 维持"暂不投入"，仍不入枚举 | `pulse/shared/enums.py` 加 `VK`；`pulse/shared/models.py` 加 `_REQUIRED_OPTIONS[Platform.VK]` 与 `owner_id` 校验；`pulse/shared/tests/test_contract_baseline.py` 的 `test_02` 由"排除 VK"改为"包含 VK、排除 TikTok"；契约版本升 `1.1`（`CONTRACT_VERSION` 与两个守护测试同步） |
| v1.0 | 2026-09-10 | 首次冻结。补齐 `brand_guides`、`compliance_findings`、`sanctions_screenings` 三张表；新增半自动导出接口与 API；平台优先级按 B2B 基线重排 | 无（尚无代码） |
