# 依赖来源与许可记录：内置 PyMySQL（R29 离线部分）

日期：2026-09-23｜依据：综合审查 V2.0 的 R29（记录 vendor 来源/许可/hash/更新责任）
性质：维护记录。它证明内置依赖可追溯，不构成供应链安全认证，也不声称已完成 CVE 清点。

## 1. 来源与用途

| 项目 | 记录 |
| --- | --- |
| 组件 | PyMySQL（纯 Python MySQL 驱动） |
| 版本 | 1.2.0（wheel 元数据 `Name: PyMySQL` / `Version: 1.2.0`） |
| 来源 | PyPI 官方 wheel，随 Profile 作为只读 vendor 副本，避免依赖宿主解释器环境 |
| 位置 | `plugins/datasage-query/vendor/pymysql/`、`plugins/datasage-query/vendor/pymysql-1.2.0.dist-info/` |
| 许可 | `pymysql-1.2.0.dist-info/licenses/LICENSE`（1070 字节，MIT），随包保留 |
| 边界 | 不复制宿主组件、不修改上游代码、不额外打补丁；仅由插件的只读执行层导入 |

## 2. 完整性指纹

- 文件数：25（`vendor/` 下全部文件，排除 `__pycache__`）
- 算法：对每个文件取 `relative_path + "\0" + sha256(file_bytes)`，按相对路径排序后用 `\n` 连接，
  再取 sha256。
- 聚合 sha256：`d46d7da87c801799854de0aaea178718088910b03f23f790f304466221b376e7`
- 校验：`tests/test_integration_boundaries.py::test_vendored_pymysql_matches_the_recorded_provenance`
  会重新计算该指纹并与本文比对；不一致即失败。

## 3. 更新责任与流程

| 事项 | 负责角色 | 要求 |
| --- | --- | --- |
| 版本升级 | Profile 维护负责人 | 独立兼容性任务；记录新版本、来源、许可与聚合指纹；跑完整离线回归与数据库只读核验 |
| 安全评估 | 安全/运行 owner | 版本变更前评估已知漏洞与传输影响；本文不替代该评估 |
| 许可合规 | 维护负责人 | 保留上游 LICENSE 与 dist-info；缺失即视为打包失败 |
| 分享物 | 维护负责人 | 分享/审查包按 `docs/source-export.md` 白名单导出；vendor 只读副本可随包，运行态与凭据不得入包 |

## 4. 结论边界

- 本轮只记录来源、许可、指纹与责任；未做全量漏洞扫描，也不把「指纹一致」解释为「依赖安全」。
- 宿主侧依赖（Hermes 0.21.1 及其 Python 环境）不在此记录范围：宿主按官方基底 `2237be3` 使用，
  不由 Profile 复制或替换。
