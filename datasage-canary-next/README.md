# DataSage Canary Next

这是 `datasage-canary-next` 的源码仓库，也是当前 Hermes Profile 安装目录。

Git 只跟踪 `distribution_owned` 发行文件、测试、文档和 release receipt。
`.env`、认证信息、状态库、会话、日志、Memory 及其他运行数据必须保持忽略，
不得使用 `git add -f` 提交这些内容。

当前版本：`0.15.0-rc3`
发行内容 SHA-256：`551382439ab3c00bb82ae36827d7f50c3e7e61df7103e6f101ecf785c675f30d`

## 推荐流程

1. 使用分支或外部 worktree 修改和运行离线测试，避免测试读取本目录的真实 `.env`。
2. 合并到 `main`，生成 release receipt 并提交。
3. 对版本提交打 tag。
4. 重启 `datasage-canary-next` gateway，并确认 receipt 与 Git 版本一致。

运行目录不再依赖 Hermes distribution installer 做日常版本管理；后续以 Git commit
和 tag 为版本基准。
