# RustDesk 自建定制说明

本目录承载对上游 `rustdesk/rustdesk` 的全部定制内容。核心设计原则是：
**所有对上游文件的改动都必须由 `apply_custom.py` 生成，而不是手改源码。**
这样每次同步上游时，只需「以上游为准合并 → 重跑脚本」即可，永远不会陷入合并冲突。

## 定制内容

| 配置项 | 值 |
| --- | --- |
| ID / 中继服务器 | `222.128.49.178` |
| 服务器公钥 | `LQT7jYJlwj5FTwZH3xNC4enXmRbjRjNOhb4RMg8Zljw=` |
| 更新检查源 | `https://github.com/duhuilai/rustdesk` |

所有值集中在 [`custom.json`](./custom.json)，修改后重跑脚本即可生效。

## 脚本改了哪些文件

| 文件 | 改动 | 目的 |
| --- | --- | --- |
| `libs/hbb_common/src/config.rs` | `RENDEZVOUS_SERVERS`、`RS_PUB_KEY` | 内置自建服务器与公钥 |
| `libs/hbb_common/src/lib.rs` | `version_check_request` 的 URL | 版本检查改查自有仓库的 Release API |
| `libs/hbb_common/src/lib.rs` | `VersionCheckResponse.url` 重命名映射 `html_url` | 适配 GitHub 返回体 |
| `src/common.rs` | 请求由 `POST + JSON` 改为 `GET + User-Agent` | 适配 GitHub REST API 调用约定 |
| `.github/workflows/flutter-build.yml` | 14 处 `prerelease: true` → `false` | 见下方「为什么必须改 prerelease」 |
| `.github/workflows/flutter-build.yml` | 11 个非 Windows job 设为 `if: false` | 只构建 Windows，见下方「构建范围」 |
| `.github/workflows/{ci,fdroid,flutter-nightly}.yml` | 停用自动触发 | 避免空耗 Actions 配额 |
| `.github/dependabot.yml` | 删除该文件 | 见下方「为什么删除 dependabot.yml」 |

### 构建范围

只保留 Windows 构建链路必需的 3 个 job：

| 保留的 job | 作用 |
| --- | --- |
| `generate-bridge` | 生成 Flutter / Rust FFI 桥接代码 |
| `build-RustDeskTempTopMostWindow` | Windows 置顶窗口辅助组件 |
| `build-for-windows-flutter` | 主构建，产出 x86_64 与 aarch64 安装包 |

其余 11 个 job（macOS、iOS、Linux、Android、AppImage、Flatpak、Web，
以及已废弃的 Sciter UI 构建）统一设为 `if: false`。

采用 `if: false` 而非删除代码块，原因有二：删除会与上游产生大范围冲突；
而 `if: false` 可由脚本幂等还原。需注意部分 job 原本就带有 job 级 `if:`
（如 `${{ inputs.upload-artifact }}`），脚本会**替换**该行而非新增——
若新增会产生重复键，导致整个工作流 YAML 解析失败。

### 为什么自动更新只需改一处 URL

`src/updater.rs` 拿到形如 `.../releases/tag/<版本>` 的地址后，会自动把路径中的
`tag` 替换为 `download`，再拼出 `rustdesk-<版本>-<架构>.exe`（或 `.msi`）作为下载链接。

GitHub Release API 返回的 `html_url` 恰好就是这个形式，而上游 CI 产出的安装包命名
（`rustdesk-${VERSION}-${arch}.exe`）也恰好匹配。因此只要把版本检查的数据源换成
自有仓库，整条「检查更新 → 下载 → 静默升级」链路即可原样复用，无需改动 `updater.rs`。

### 为什么必须改 prerelease

上游 CI 一律以 `prerelease: true` 发布，而 GitHub 的 `/releases/latest` 端点
**不会返回预发布版本**。若保持原样，客户端将永远查不到新版本，自动更新静默失效。

### 为什么删除 dependabot.yml

上游的 `.github/dependabot.yml` 仅配置了对 git 子模块的每日更新
（`package-ecosystem: gitsubmodule`）。本仓库已将 `libs/hbb_common` 内联为普通目录
并删除 `.gitmodules`，该更新源已不存在，Dependabot 会持续报错：

```
dependency_file_not_found: /.gitmodules not found
```

作为自动同步上游的 fork，本就不希望 Dependabot 开依赖 PR 与同步相互冲突，
因此直接删除该文件。该操作由 `apply_custom.py` 的 `patch_dependabot` 步骤在每次
同步时幂等执行，确保上游合并不会把上游的 `dependabot.yml` 重新带回来。

## hbb_common 的处理方式

上游把 `libs/hbb_common` 作为 git submodule 引用。若沿用子模块，就必须额外维护一个
`hbb_common` 分叉仓库，且每次上游更新子模块指针都要手工对齐。

本仓库改为**将 hbb_common 源码内联提交**到主仓库，好处是：

- 单仓库即可完成全部定制，无需维护第二个仓库；
- 上游的 `flutter-build.yml` 无需任何改动即可直接复用；
- 同步时由工作流按上游锁定的提交号重新内联，版本对齐完全确定。

## 用法

```bash
# 应用定制（幂等，可反复执行）
python custom/apply_custom.py

# 仅校验是否已全部应用，不写文件
python custom/apply_custom.py --check
```

脚本在上游重构导致匹配失败时会**以非零码退出**，从而让 CI 立刻报错，
而不是静默产出一个连不上自建服务器的安装包。

## 自动同步与发布

由 [`.github/workflows/sync-upstream.yml`](../.github/workflows/sync-upstream.yml) 驱动：

```
每 6 小时定时触发
        │
        ├─ 查询上游最新正式版 Release
        ├─ 本仓库已有同名标签？ ── 是 ──> 结束
        │                        否
        ├─ 合并上游代码（冲突一律取上游）
        ├─ 按上游锁定提交重新内联 hbb_common
        ├─ 重跑 apply_custom.py 写回定制值
        ├─ 提交并推送 master
        └─ 打同名标签并推送
                │
                └─> 触发 flutter-tag.yml
                        └─> flutter-build.yml 构建 Windows
                                └─> 发布 Release 与安装包
```

### 必需配置

在仓库 **Settings → Secrets and variables → Actions** 添加：

| Secret | 说明 |
| --- | --- |
| `RELEASE_PAT` | 具备 `repo` + `workflow` 权限的 Personal Access Token |

**这一项不可省略。** GitHub 有防递归机制：用默认的 `GITHUB_TOKEN` 推送标签
不会触发任何工作流，构建链路会断在打标签这一步。

同时在 **Settings → Actions → General** 确认：

- Actions permissions：`Allow all actions and reusable workflows`
- Workflow permissions：`Read and write permissions`

### 手动触发

在 Actions 页面选择「同步上游并发布」，可指定：

- `upstream_tag`：手动指定上游版本号（留空则取最新正式版）
- `force_rebuild`：即使已存在同名标签也强制重新同步并重新打标签

## 注意事项

- **仓库必须公开**。客户端的更新检查是匿名请求，私有仓库的
  `/releases/latest` 对匿名请求返回 404，会导致自动更新静默失效。
  同时公开仓库的 Actions 完全免费，无分钟数限制。
- **定时任务会被自动停用**：GitHub 会在仓库连续 60 天无活动后停用定时工作流。
  上游若长期未发版，需手动触发一次以恢复。
- **产出为未签名安装包**：上游 CI 的代码签名步骤以 `if: env.X != null` 保护，
  未配置签名 Secret 时自动跳过。Windows 首次运行会出现 SmartScreen 提示，
  选择「更多信息 → 仍要运行」即可。
- **产物架构**：`rustdesk-<版本>-x86_64.exe`（便携版）、同名 `.msi`（安装版），
  以及对应的 `aarch64` 版本。`updater.rs` 会按运行架构自动选择。
