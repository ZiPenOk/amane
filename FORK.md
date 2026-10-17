# 本 fork 的约定

这是 [ZiPenOk/amane](https://github.com/ZiPenOk/amane), fork 自 [sqzw-x/amane](https://github.com/sqzw-x/amane).
本文规定分支布局、与上游同步、构建与部署的流程; 动代码或同步上游前先读这里, 不必逐次交代.

## 分支与远端

| 远端 | 指向 | 用途 |
|------|------|------|
| `origin` | 本 fork | 推送自己的提交, 构建自己的镜像 |
| `upstream` | `sqzw-x/amane` | 只读, 取上游提交与 tag |

| 分支 | 内容 | 说明 |
|------|------|------|
| `main` | 纯上游 | 只做 fast-forward; 对照与 diff 基准, **永远不在这里提交** |
| `dev` | 上游 + 本 fork 的改动 | **默认分支**; 日常在这里工作, 镜像也从这里构建 |

`dev` 必须是默认分支: GitHub 的手动触发 (`workflow_dispatch`) 只认默认分支上的工作流.
想确认自己改了什么:

```powershell
git diff main dev --stat
git log --oneline main..dev
```

同步以**上游的发版 tag** 为单元 (`v0.21.0` 这种), 不追 `upstream/main` 的每个提交.

## 本 fork 增加了什么

| 功能 | 落点 | 同步时的注意 |
|------|------|--------------|
| 图片压缩 (长边 / 质量) | `src/amane/media/compress.py` 与 pipeline / resource_store / handlers 的挂点, 配置 `CompressConfig` | 独立文件; 上游改图片管线时确认挂点仍在 |
| fc2cmadb 数据源 | `src/amane/crawlers/sites/fc2ppvdb.py` | 上游若同时改这个站点, 冲突必须人工判断 |
| 站点 Cookie 兼容 | `src/amane/config/manager.py` (`SiteConfig.cookie`) | 上游 `SiteConfig` 内加了 `mode="before"` 校验器直接吃 `Cookie` 头; 上游改这个类时人工合并 |
| 预览图不落盘开关 | `src/amane/api/routes/resources.py` + `ScrapingConfig.cache_preview_images` | 上游改 `ScrapingConfig` / 资源路由时人工合并 |
| 删除清单补收同名前缀产物 | `src/amane/library/cleanup/footprint.py` | 上游改 `build_footprint` / `_add_products` 时人工合并 |
| 无码官网来源 (official_uncensored) | `src/amane/crawlers/sites/official_uncensored.py` | 新增文件 + registry 注册 + 无码默认路由; 上游加同名来源时人工合并 |
| Google 免费翻译 (gtx) | `src/amane/llm/google.py` + `build_translator`, 配置 `LLMConfig.provider` / `google_endpoint` | 上游改 `llm/` 或 `build_translator` 时人工合并 |
| 全字段翻译 | `src/amane/handlers/scrape.py::_translate_metadata` + `TRANSLATABLE_METADATA_FIELDS` | 上游改翻译步骤时人工合并 |
| 中字水印按番号配对 | `src/amane/parsing/file_info.py::subtitle_matches_video` + `library/rules.py` / `cleanup/inventory.py` 的调用点 | 上游改水印判定或 `has_companion_subtitle` 时人工合并 |
| 上传海报 / 封面直接覆盖库图 | `src/amane/api/routes/metadata.py` 的上传接口 + `src/amane/media/placement.py` | 上传写 Resource 后按库模板立刻覆盖 thumb / poster / fanart; 上游改 metadata 路由或整理模板时人工合并 |
| 字幕语言统一 chs/cht | `src/amane/parsing/file_info.py::detect_subtitle_language`, 占位符 `{srt_lang}` | 上游改字幕命名或模板占位符时人工合并 |
| Emby 媒体库刷新通知 | `src/amane/emby.py` + `AsyncWorker` 结算钩子, 配置 `EmbyConfig` | 新增文件; 上游改 `scheduler/worker.py` 的结算路径时确认钩子仍在 |
| SmartStrm 远端删除通知 | `src/amane/smartstrm.py` + `handlers/delete.py` 的挂点, 配置 `SmartStrmConfig` | 新增文件; 上游改 `DeleteHandler` 时人工合并 |
| 删光文件后连带删除元数据 | `src/amane/handlers/delete.py::_delete_orphan_metadata`, 配置 `CleanupConfig` (默认关) | 上游改 `DeleteHandler` / `DeleteResult` 时人工合并 |
| 自己的镜像构建 | `.github/workflows/build-mine.yml` | 独立文件, 不与上游冲突 |
| 生成产物重跑 | `.github/workflows/regen-openapi.yml` | 本地跑不了 `pnpm` 时的兜底: Actions 里手动 Run workflow |

上游的 `ci.yaml` 与 `docker.yml` 保持**逐字节相同**, 因为布局已经避开了它们:

- `ci.yaml` 只在 push `main` / `develop` 与手动触发; 工作分支叫 `dev`, push 不触发它, 手动触发照样可用
- `docker.yml` 只在 `v*` tag 触发, 本 fork 不推 tag

## 同步流程

工作树必须干净, 然后一条命令:

```powershell
# Windows PowerShell 5.1 (本机默认, 无需装 pwsh)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\sync-upstream.ps1 -Push
# 装了 PowerShell 7 的话
pwsh scripts/sync-upstream.ps1 -Push
```

脚本依次做: 取上游 → `main` 前移到上游 tag → 切 `dev` 合并 `main` → 生成物冲突交给生成器 →
重跑 openapi 与前端客户端 → 打同步 tag → 本地检查 (ruff / format / pyright / ty / pytest / pnpm check) →
推送 `main` 与 `dev` → 触发 CI 与镜像构建.

| 选项 | 作用 |
|------|------|
| `-Ref v0.22.0` | 用指定 tag / 分支, 缺省取最新 `v*` tag |
| `-SkipChecks` | 跳过本地检查 |
| `-Push` | 检查通过后推送 `main`、`dev` 并触发 CI 与镜像构建; 不加则只停在本地 |
| `-NoTag` | 不打同步 tag |

手工等价步骤:

```powershell
git fetch upstream --tags --prune
git checkout main
git merge --ff-only v0.21.0      # main 只前移, 冲突说明上游改过历史, 停手排查
git checkout dev
git merge main                   # 冲突在这里解决
uv sync --frozen --all-extras --dev
uv run python scripts/export_openapi.py
cd web; pnpm install --frozen-lockfile; pnpm gen-client; cd ..
git commit
uv run ruff check src/amane tests scripts; uv run ruff format --check src/amane tests scripts
uv run pyright; uv run ty check
uv run pytest tests/ -q -n auto --dist worksteal
cd web; pnpm check; cd ..
git push origin main dev
```

## 冲突规则

1. **生成物** (`web/openapi.json`, `web/src/client/*`): 永远不手改. 任取一边, 然后重跑
   `uv run python scripts/export_openapi.py` 与 `cd web && pnpm gen-client`.
   校验: `git diff main dev -- web/openapi.json web/src/client` 应当只剩本 fork 自己的 schema 差异 (压缩、翻译、通知等 section 与字段).
2. **`.github/workflows/*.yml`**: 以上游版本为准. `ci.yaml` 与 `docker.yml` 现在不需要任何本地改动;
   `build-mine.yml` 与 `regen-openapi.yml` 是本 fork 自己的文件, 不会冲突.
3. **功能代码**: 人工判断; 判断不了就两边都留, 并用对应站点的用例验证.

## 本地环境

需要 `git` / `uv` / `node` / `pnpm` (Windows 上 `uv` 装在 `%USERPROFILE%\.local\bin`).

```powershell
uv sync --frozen --all-extras --dev
cd web; pnpm install --frozen-lockfile
```

## 已知情况

- Windows 上全量 `pytest` 有 1 个与本 fork 无关的失败:
  `tests/playback/test_proxy.py::test_upstream_request_errors_do_not_leak_gate_permits` (Proactor 事件循环差异), Ubuntu CI 上不失败.
  `sync-upstream.ps1` 把它列为已知失败, 只告警不阻断.
- 本 fork 的文档放仓库根目录: `docs/` 下的新页面不在 `mkdocs.yml` 的 `nav` 里, `mkdocs build --strict` 会因此报错.
- 启动日志会明文打印 API token (`api token auth enabled token=...`), 贴日志前打码.

## 构建与部署

1. Actions → **Build my image** → Run workflow (或 `sync-upstream.ps1 -Push` 自动触发);
   产出 `ghcr.io/zipenok/amane:latest` 与 `ghcr.io/zipenok/amane:sha-<commit>`.
2. 部署机 (Debian x86_64):

```bash
cd <compose 目录>
docker compose pull && docker compose up -d
```

容器启动时自动跑数据库迁移, 并留一份 `amane.db.pre-migrate-*.bak`.
