# Pulse — 海外社媒内容 Agent（B2B 工业铸件出海）

**当前版本：V1.11.0**（接口契约 v1.1 / 媒体库契约 v1.4）

为**菲美得**构建的多平台社媒内容生成与自动发布 Agent。业务基线是 B2B 工业铸件出海：
客户为**无自有铸造厂的海外机床整机厂**（印度为主，美国 / 台湾次之），
能力闭环为 `内容生产 → 合规校验 → 发布网关 → 半自动兜底`。

策划与验收口径见 `AGENTS.md` 与 `pulse/contracts/INTERFACES.md`（**冻结，只读**）。

---

## 1. 版本

| 项 | 取值 | 真源 |
| --- | --- | --- |
| 产品版本 | **1.11.0** | `pyproject.toml` + `pulse.__version__` |
| 接口契约版本 | **1.1**（冻结） | `pulse.shared.CONTRACT_VERSION` |
| 媒体库契约版本 | **1.4**（冻结） | `pulse/contracts/MEDIA_LIBRARY.md` |
| 版本策略 | 语义化版本 `MAJOR.MINOR.PATCH` | 见 §5 |
| 变更记录 | 逐版本追加 | `CHANGELOG.md` |

```powershell
& ".venv\Scripts\python.exe" -c "import pulse; print(pulse.__version__, pulse.__contract_version__)"
# 1.11.0 1.1
```

---

## 2. 快速开始

### 2.1 解释器

系统 `python` / `python3` 是 Windows Store 占位符，**不可用**。唯一可用解释器：

```
D:\agent开发\菲美得\agent\.venv\Scripts\python.exe
```

### 2.2 运行测试

工作目录必须是**仓库根**（`pyproject.toml` 在此，`pythonpath = ["."]`）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'      # 避免中文输出乱码
& "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider
```

`-p no:cacheprovider` 不是可选项：多个 agent 共享工作区，同时跑 pytest 会争写 `.pytest_cache`。

### 2.3 安装依赖

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'uv-cache'
uv pip install --python "D:\agent开发\菲美得\agent\.venv\Scripts\python.exe" <包名>
```

---

## 3. 目录结构

| 路径 | 内容 |
| --- | --- |
| `pulse/contracts/` | 工程接口契约（冻结，只读） |
| `pulse/shared/` | 跨域共用类型与契约基线测试 |
| `pulse/services/content/` | 内容生产域 |
| `pulse/services/publish/` | 发布网关域 |
| `pulse/services/scheduler/`、`pulse/services/identity/` | 调度域、账号与凭据域 |
| `pulse/services/compliance/` | 合规与治理域 |
| `pulse/api/`、`pulse/console/` | 接口层与控制台 |
| `pulse/tests/` | 跨域契约 / 集成 / 风控测试 |
| `pulse/docs/`、`pulse/tasks/` | 治理文档与任务板 |

### 3.1 仓库边界

**仓库根 = 本目录 `D:\agent开发\菲美得\agent`。** 上层目录 `D:\agent开发\菲美得\` 是工作区，
存放**不入版本库**的业务资产：

| 位置 | 内容 | 是否入库 |
| --- | --- | --- |
| `agent/` | 代码、契约、治理文档、CI | **是**（仓库根） |
| `agent/RAG知识库/` | 素材描述索引（**318 份素材描述** + 13 份汇总索引，共 331 个 `.md`），供内容域按语义选素材 | 否（体积大） |
| `agent/.venv/` | 本地 Python 解释器（唯一可用） | 否 |
| `../菲美得产品图片/` | 实拍素材 **342 个文件**（299 图 + 19 视频 + 说明文档），产品图唯一来源 | 否（在仓库外） |
| `../外贸公司/` | 客群调研与业务资料 | 否（在仓库外） |

> 内容域引用素材时，路径基准是**工作区**而不是仓库根：`../菲美得产品图片/…`。
> 描述文件 front-matter 里的 `source_folder` 写的是 `D:/菲美得/…`，与本机不符，
> 选用器必须做前缀重映射（见 `pulse/docs/dispatch/A1_content.md` §4）。

---

## 4. Git 与 GitHub 版本管理

### 4.1 分支约定

| 分支 | 用途 |
| --- | --- |
| `main` | 唯一主线，始终可发布；发布提交在此打 tag |
| `release/<MAJOR>.<MINOR>` | （可选）历史维护分支，仅供线上缺陷修复 |

### 4.2 标签约定

发布版本使用**带注释标签**，前缀 `v`：

标签与 `pyproject.toml` 的 `version` 保持一致，形如 `v1.8.0`。

```powershell
git tag -l --format='%(refname:short)  %(subject)'
git show v1.11.0 --stat --no-patch
```

### 4.3 提交消息约定

`<范围>: <动作> <对象>`，范围用波次或域：`W1` / `contract` / `publish` / `scheduler` / `content` / `ci`。

```
W1: A2 发布网关域交付（80 测试通过）+ 治理补强
release: V1.8.0 契约 v1.1 — VK 转正式运营
```

### 4.4 连接到 GitHub

本机 `git` 直连 GitHub 会被阻断，需经本地代理端口 `127.0.0.1:7897`（Clash Verge）：

```powershell
git config --local http.proxy http://127.0.0.1:7897
git config --local https.proxy http://127.0.0.1:7897

git remote add origin https://github.com/<owner>/<repo>.git
git push -u origin main
git push origin --tags
```

TLS 若报 `schannel: AcquireCredentialsHandle failed`，改用 OpenSSL 后端：
`git config --local http.sslBackend openssl`。

### 4.5 自动化

`.github/workflows/ci.yml` 在推送 / PR 时跑全量 pytest，在推送 `v*` 标签时自动创建 GitHub Release。

---

## 5. 发布流程

1. 确认工作树干净、全量测试通过：

   ```powershell
   git status --short
   & ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider
   ```

2. 同步版本号：`pyproject.toml` → `pulse/__init__.py` → `CHANGELOG.md`，
   **并确保 git tag 与之一致**。

   > 这三处由 `test_release_version.py` 守护，但该测试**看不到 git tag**。
   > 1.4.1–1.7.1 期间正是"只打 tag、没改这三处"造成版本漂移（三处停在 1.4.0，
   > tag 已到 v1.7.1）。发布时四处必须同时改。

   ```powershell
   & ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider pulse/shared/tests/test_release_version.py
   ```

3. 提交、打标签、推送：

   ```powershell
   git commit -am "release: V1.11.0"
   git tag -a v1.11.0 -m "V1.11.0 — 实拍视频识别与入库"
   git push origin main --follow-tags
```

版本号升级口径：**契约冻结或对外行为不兼容** → 升 `MAJOR`；**向后兼容的新增** → 升 `MINOR`；
**修缺陷** → 升 `PATCH`。

---

## 6. 铁律（摘自 `AGENTS.md`，完整版见该文件）

1. 不越界写：只写自己拥有的目录。
2. 不改契约：`pulse/contracts/` 冻结，变更须由 root 统一升版。
3. 只用平台官方 API，不做浏览器自动化发帖。
4. 工业件禁止用文生图生成产品图，产品图只用实拍素材。
5. 不伪造数据：缺失标注 `TODO(need-real-data)`。
6. 英文优先：客户文案以英语为主，中英对照供审校。

---

## 7. 一键启动素材库控制台（给非技术同事）

不需要懂命令行，双击仓库根目录的两个批处理文件即可：

| 文件 | 作用 |
| --- | --- |
| `一键启动_素材库控制台.bat` | 启动素材库控制台并自动打开浏览器 |
| `创建桌面快捷方式.bat` | 在桌面生成「Pulse素材库」图标（只需运行一次） |

启动后浏览器会自动打开素材库页面，可以：上传产品实拍图入库；查看容量是否出现红色预警；
把已用于内容 / 发布的图片标记为已使用（该图 15 天内不再被召回）。关闭黑色窗口即退出。

**上传图片不需要手填描述**：选好图片就会自动生成摘要、关键词与细节说明（可直接修改），
入库时写入 RAG 描述库。想要"看懂画面内容"的描述，把 `.env.example` 复制成 `.env`，
填入 `PULSE_VISION_API_KEY` 即可启用视觉模型；不配置也能用，只是描述里只含文件名、
拍摄时间与分辨率等信息，画面内容会标注"待补充"。

视觉模型默认对接 OpenCode Go 的 `deepseek-v4-flash-vision-exp`（仓库里已放好空 `.env`，
填 Key 即可）。填完双击 `检查视觉模型.bat` 自检：

| 自检结果 | 含义 |
| --- | --- |
| 结果：可用 | 地址、密钥、模型都正确，可以开始上传图片 |
| 阶段：config | `.env` 里还没填 `PULSE_VISION_API_KEY` |
| 阶段：model | 模型不支持图片输入，请改用 `deepseek-v4-flash-vision-exp` |
| 阶段：request | 网络或鉴权失败（401 表示 Key 无效） |

如果提示"没有找到运行环境"或"没有找到文件夹"，说明拷贝不完整：需要连同
`.venv`（运行环境）和上一层的 `菲美得产品图片`（实拍素材）一起拷贝。
