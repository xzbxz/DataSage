# DataSage Canary Next

这是 DataSage Profile 的源码目录；当前它也被直接用作
`datasage-canary-next` 运行目录。源码与运行实例同址是迁移期间的技术债，
不是目标发行结构。

`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据属于用户态，
不得提交，也不得由发行更新覆盖。企微和数据库权限不属于发行流程的修改范围。

当前候选版本：`0.15.0-rc8`。

## Hermes 原生专家能力

本 Profile 不再使用 `.no-bundled-skills` 退出 Hermes 内建 Skill 同步。针对精确
锁定的 Hermes `0.20.5`，`config.yaml` 用完整 `skills.disabled` denylist 只保留
经过审查的文档、表格、PDF、演示文稿、OCR、引用、会议/文档行动项和周度计划
能力。内建 Skill 由 Hermes 在下一次 `hermes update` 时同步；需要立即同步时运行
`hermes -p datasage-canary-next skills opt-in --sync`。本仓库不复制或修改这些宿主资产。

升级 Hermes 前必须先运行离线测试。测试会比较新宿主的完整 bundled Skill
名称集合和当前 denylist；任何新增、删除或重命名都会阻断升级，直到维护者完成
快照差异审查。不要只因为新 Skill 看起来相关就默认启用，也不要手工维护一份
Profile 内的内建 Skill 副本。

## 权威边界

- Git commit/tag 是源码版本身份；`distribution.yaml` 是 Hermes 安装载荷与版本声明。
- `hermes profile install/update/info` 是安装、更新和实例信息的官方入口。
- `build_release_receipt.py` 只是源码仓库中的 DataSage 质量门禁：它把真实 replay、
  compaction、交付和性能证据绑定到一个经过审查的运行载荷。它不是安装器、
  版本系统或回滚系统，也不随运行 Profile 发行。
- `distribution_owned` 只列运行所需文件。测试、E2E scorer、构建脚本和历史重构
  文档留在源码仓库，不进入干净安装实例。

## 候选质量门禁

全部实现合并后，在源码根目录为当前载荷生成一个新的、不可覆盖的候选 receipt：

```powershell
$candidate = "pending/datasage-v015-rc8-candidate-receipt-$(Get-Date -Format yyyyMMddTHHmmss).json"
python -B build_release_receipt.py --output $candidate
python -B build_release_receipt.py --verify-candidate
```

不带 `--receipt` 的 `--verify-candidate` 只扫描 `pending/` 下的 candidate receipt，
并按版本和当前 `content_sha256` 唯一发现。旧的同版本候选可以保留；它们不会被
误选。如果当前内容没有唯一匹配项，命令必须失败，而不是猜测“最新”文件。

需要锁定某份候选时可显式运行：

```powershell
python -B build_release_receipt.py --verify-candidate --receipt $candidate
```

candidate 与 final receipt 使用严格分离的目录和文件名：

- `--verify-candidate` 只接受 `pending/*-candidate-receipt*.json`；
- `--check` 只接受 `release/*-release-receipt.json`；
- `--output` 只创建新的 pending candidate，拒绝覆盖，也不能直接写入 `release/`。

退出码 `2` 表示当前载荷没有匹配的 receipt 或身份不一致；退出码 `3` 表示载荷
一致，但 DataSage live/host/performance 门禁仍阻断。离线测试通过不等于可发布。
所有质量门禁均通过并完成审查后，才把完全相同的候选 receipt 作为新的
`release/*-release-receipt.json` 提交；随后对最终提交打 tag。可用以下命令检查
final receipt 与载荷的绑定：

```powershell
python -B build_release_receipt.py --check
```

历史 receipt 保持不可变。receipt 的内容哈希只是质量证据的受测对象，不取代
Git commit/tag，也不证明 Hermes 安装来源。

## 官方 Profile Distribution 迁移阻断

Hermes `0.20.5` 要求远程发行仓库的根目录直接包含 `distribution.yaml`。当前 Git
仓库根目录是 `profiles/`，本 Profile 位于其 `datasage-canary-next/` 子目录，因此
现在的远程仓库不能直接作为 `hermes profile install <git-url>` 的来源；本次文件
整理没有假装解决这个仓库拓扑问题。

仓库级迁移必须选择一种方案：

1. 首选：把 `datasage-canary-next/` 发布为独立仓库，目录内容位于仓库根；
2. 或建立专用发行分支/仓库，把该子目录投影到发行根，且不复制第二份可编辑源。

不要为 Hermes 再实现“支持仓库子目录”的自定义安装器。迁移完成前，远程
`install/update` 不应被宣称可用。

## 干净安装验证

发行根布局完成后，先把待验证 tag/commit 检出到临时、干净的本地目录。Hermes
`0.20.5` 的远程安装跟随远端默认分支，CLI 不提供 commit 参数，因此 canary
验证应从已经 detached 到审查版本的本地 checkout 安装：

```powershell
git clone <distribution-repo-url> <clean-source-dir>
git -C <clean-source-dir> checkout --detach <reviewed-tag-or-commit>
git -C <clean-source-dir> status --porcelain
git -C <clean-source-dir> rev-parse HEAD
hermes profile install <clean-source-dir> --name datasage-rc8-clean -y
hermes profile info datasage-rc8-clean
hermes -p datasage-rc8-clean plugins doctor datasage-query --ci
```

随后只在这个隔离 Profile 上运行真实 replay、企微入站到 delivery ledger、宿主
compaction 和性能门禁。确认干净实例不含 `tests/`、`docs/history/`、
`build_release_receipt.py` 或插件 `e2e/`，且未触碰既有 Profile 的 `.env`、Memory、
sessions、企微和数据库权限。

第一次迁移必须使用新名称做干净安装，因为 Hermes 更新只覆盖新 manifest 中列出
的路径，不会自动删除旧版本已落盘、但后来移出 `distribution_owned` 的开发资产。
验证完成后再按独立发布授权切换 canary。

## 后续更新与信息检查

只有实例已经由官方 installer 安装、并记录了根布局合法的 source 后，才使用：

```powershell
hermes profile update datasage-canary-next -y
hermes profile info datasage-canary-next
```

`update` 默认保留已有 `config.yaml`；只有明确审查并授权配置替换时才可使用
`--force-config`。不要把当前源码/运行同址的 Profile 当作已经完成这一迁移。

DataSage 当前没有有 owner 的定时任务，因此不向 Hermes `cron` toolset 暴露
`datasage-query`。未来新增主动巡检时，必须同时定义任务 owner、调度身份与权限、
超时/重试和端到端测试，不能只恢复配置项。
