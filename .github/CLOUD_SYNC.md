# 每日云端商品同步

这个工作流按用户授权执行真实商品同步，并用简体中文汇报结果。新增的协调脚本不修改原有商品导出、验证和导入逻辑。

## 执行与汇报

- 时间：北京时间／吉隆坡时间每天 **12:00**，对应 UTC `0 4 * * *`，不受夏令时影响。GitHub 的定时触发可能延迟，不保证准点；公共仓库长期无活动也可能被 GitHub 停用定时工作流。
- 执行：GitHub 托管的 Ubuntu runner 使用 Python 3.12 和 Playwright Chromium，运行原有 `scripts/sync_products.py --run`。
- 汇报：每次有效执行都写入 Actions Summary，并在同一个「美宜佳每日商品同步报告」Issue 中发表评论，提及仓库所有者。需要在 GitHub 中订阅该 Issue，并按个人设置开启网页或邮件通知。不会在本 Codex 对话里自动新增消息。
- 内容：成功或失败状态、新增／更新／跳过／成功／失败数量、拆包规格警告数和运行日志链接。未安装依赖、缺少凭据等前置失败也会尝试汇报；GitHub API 不可用时保留 Actions Summary。
- Actions Artifact 仅保留脱敏中文日报 7 天。该仓库是公开仓库，不上传商品表、后台截图、详细业务日志或原始回执；这些诊断文件仅留在本次临时 runner 中，作业结束后不保留。成功运行仍沿用原脚本的产物清理规则。
- 原同步子程序的输出仅写入临时 runner 的本地日志，不直接显示在公开 Actions 日志中，避免浏览器异常链带出填写值。公开汇报仅包含执行状态、退出码和规定的汇总数量。

## 启用条件

工作流必须发布到仓库默认分支，GitHub Actions 与 Issues 都必须启用。工作流仅申请 `contents: read` 和 `issues: write` 权限。

在 GitHub 仓库的 **Settings → Secrets and variables → Actions → Secrets** 中安全绑定这四项现有凭据：

- `POSPAL_USERNAME`
- `POSPAL_PASSWORD`
- `MYJ_ADMIN_USERNAME`
- `MYJ_ADMIN_PASSWORD`

这些是 GitHub Actions 自己的 Secrets，与 Codex 云环境的凭据绑定独立。不要将密码提交到 Git，也不要写进 Issue 或聊天。工作流的 `GITHUB_TOKEN` 由 GitHub 自动提供，无需另填个人访问令牌。

### 从本机现有配置迁移

在原电脑的项目目录，用本机现有 GitHub CLI 认证执行：

```bash
python3 .github/cloud/bind_local_secrets.py --env-file /本机实际路径/.env
```

脚本只读取上述四项，通过标准输入交给 `gh secret set`，由 GitHub CLI 加密后写入指定仓库；值不会出现在命令参数、聊天或脚本输出中。已有同名绑定会保留，不会覆盖。可以先加 `--check-only` 检查本机来源。脚本不执行 shell 格式的 `.env` 内容。

这个命令必须在能读取原电脑配置的本地任务中执行。当前云端无法读取本机文件；在云端执行来源检查已明确失败，因此尚未发生凭据迁移。这个脚本绑定 GitHub Actions Secrets，不会填写 Codex 云环境密钥，也不会自动启用定时导入。

先通过 Actions 的 **Run workflow** 分别运行 `dry-run` 和 `check-myj`，验证真实站点登录、导出文件与导入界面。两项检查不会上传商品。若失败，先修复运行条件再启用真实同步。

切换前必须停用电脑上的旧 launchd 执行器；这个云端工作流无法读取或锁定本机的同步进程。不要让两个执行器同时导入。在本机项目目录运行已有的 `python scripts/install_runtime_launchd.py --uninstall`，并检查旧任务确已停止。

切换时还要查看本机当天的完成标记。如果本机当天已经成功同步，请等到下一个北京时间日期再启用云端真实同步；云端报告 Issue 无法自动获知本机过去的完成记录。旧执行器结果不明或仍在运行时，不要启用新执行器。

满足上述条件后，在 Actions 的 **Variables** 中将 `MEIYIJIA_CLOUD_SYNC_ENABLED` 设置为 `true`。这个开关表示云端是唯一真实导入执行器。没有设置时，定时任务仅记录「未启用」，不会上传商品或每天发送无效通知。

可选变量 `MEIYIJIA_REPORT_MENTION` 指定接收提及通知的 GitHub 用户名；默认是仓库所有者。默认的三项产品数量和警告阈值与现有脚本相同，可通过同名 Actions Variables 调整。

## 重复导入与异常恢复

所有自动与手动运行共用一个 concurrency group，不会相互取消正在执行的任务。完成状态持久化在报告 Issue 的隐藏 JSON 中，不依赖临时 runner、会被驱逐的缓存或云环境后台进程。

真实执行前先把状态保存为 `running`。同一北京时间日期已经成功时，后续运行会跳过。程序失败、执行中断或结果无法完整确认时，后续自动导入会暂停，并继续报告阻塞。依赖安装失败发生在导入开始之前，不会被当成部分导入。

只有在人工检查 MYJ 导入记录后，才能在 **Run workflow** 中恢复：

1. `mode` 选 `run`，`resolve_run_key` 填日报里的待核实执行标识。
2. 确认没导入过时，`previous_result` 选 `not-imported`，允许新尝试。
3. 确认已经成功时，选 `completed`，记录上次完成；同日不会再次导入。

恢复核实会写入 Issue 评论。不要删除报告 Issue、修改机器状态来绕过核查，或在结果不明时反复重跑。

## 当前交付状态

2026-10-19 已在原电脑完成用户授权的迁移与启用：

- 四项现有凭据已通过标准输入绑定至 GitHub Actions Secrets，并核验名称齐全；凭据值未显示。
- 原有 10 项测试、协调脚本 21 项测试、凭据迁移脚本 4 项测试及本地脚本检查通过。
- 银豹登录、导出与文件校验通过：[dry-run](https://github.com/wangchaozhuanyong/meiyijia-skill/actions/runs/39199163985)。
- 美宜佳后台登录与导入入口检查通过：[check-myj](https://github.com/wangchaozhuanyong/meiyijia-skill/actions/runs/39199186753)。两项检查均未上传商品，检查的执行代码为 `5b19463843c5c6d738b144277df438719192861e`。
- 已核实本机当天无成功标记、无正在运行的同步进程；旧 launchd 执行器已卸载并停用，保留原凭据、历史记录和项目内重建模板。
- `MEIYIJIA_CLOUD_SYNC_ENABLED=true` 已写入并读回，工作流处于 active，计划为北京时间／吉隆坡时间每天 12:00。
- [中文报告 Issue](https://github.com/wangchaozhuanyong/meiyijia-skill/issues/1) 已创建，两个检查结果已成功发表评论并提及仓库所有者，Issue 订阅已确认。
- 已限制公开附件为脱敏日报，原同步子程序的输出不直接进入公开 Actions 日志。

本次启用未执行真实商品导入。首次真实同步结果须以后续定时执行的回执和日报为准；历史商品导入失败及拆包异常未在本次云端配置中修复。真实运行失败或结果不明时，按上述恢复规则暂停后续导入。
