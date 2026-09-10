# Pulse — 海外社媒内容 Agent（B2B 工业铸件出海）

**当前版本：V1.0.0**

为**菲美得**构建的多平台社媒内容生成与自动发布 Agent。业务基线是 B2B 工业铸件出海：
客户为**无自有铸造厂的海外机床整机厂**（印度为主，美国 / 台湾次之），
能力闭环为 `内容生产 → 合规校验 → 发布网关 → 半自动兜底`。

策划与验收口径见 `AGENTS.md` 与 `pulse/contracts/INTERFACES.md`（**冻结，只读**）。

---

## 1. 版本

| 项 | 取值 | 真源 |
| --- | --- | --- |
| 产品版本 | **1.0.0** | `pyproject.toml` + `pulse.__version__` |
| 接口契约版本 | **1.0**（冻结） | `pulse.shared.CONTRACT_VERSION` |
| 版本策略 | 语义化版本 `MAJOR.MINOR.PATCH` | 见 §5 |
| 变更记录 | 逐版本追加 | `CHANGELOG.md` |

```powershell
& ".venv\Scripts\python.exe" -c "import pulse; print(pulse.__version__, pulse.__contract_version__)"
# 1.0.0 1.0
```

---

## 2. 快速开始

### 2.1 解释器

系统 `python` / `python3` 是 Windows Store 占位符，**不可用**。唯一可用解释器：

```
D:\agent开发\菲美得\.venv\Scripts\python.exe
```

### 2.2 运行测试

工作目录必须是**仓库根**（`pyproject.toml` 在此，`pythonpath = ["."]`）：

```powershell
$env:PYTHONIOENCODING = 'utf-8'      # 避免中文输出乱码
& "D:\agent开发\菲美得\.venv\Scripts\python.exe" -m pytest -p no:cacheprovider
```

`-p no:cacheprovider` 不是可选项：多个 agent 共享工作区，同时跑 pytest 会争写 `.pytest_cache`。

### 2.3 安装依赖

```powershell
$env:UV_CACHE_DIR = Join-Path $env:TEMP 'uv-cache'
uv pip install --python "D:\agent开发\菲美得\.venv\Scripts\python.exe" <包名>
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

---

## 4. Git 与 GitHub 版本管理

### 4.1 分支约定

| 分支 | 用途 |
| --- | --- |
| `main` | 唯一主线，始终可发布；发布提交在此打 tag |
| `release/1.0` | （可选）1.0 维护分支，仅供线上缺陷修复 |

### 4.2 标签约定

发布版本使用**带注释标签**，前缀 `v`：

```
v1.0.0        # 当前版本
```

```powershell
git tag -l --format='%(refname:short)  %(subject)'
git show v1.0.0 --stat --no-patch
```

### 4.3 提交消息约定

`<范围>: <动作> <对象>`，范围用波次或域：`W1` / `contract` / `publish` / `scheduler` / `content` / `ci`。

```
W1: A2 发布网关域交付（80 测试通过）+ 治理补强
release: 冻结 V1.0.0 版本元数据与变更记录
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

2. 同步三处版本号：`pyproject.toml` → `pulse/__init__.py` → `CHANGELOG.md`。

   ```powershell
   & ".venv\Scripts\python.exe" -m pytest -p no:cacheprovider pulse/shared/tests/test_release_version.py
   ```

3. 提交、打标签、推送：

   ```powershell
   git commit -am "release: V1.0.0"
   git tag -a v1.0.0 -m "V1.0.0 — 契约冻结 + 发布网关域交付"
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

