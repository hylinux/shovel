# Shovel CLI：当前实现

以 `uv run shovel --help` 和各子命令的 `--help` 为准。配置是 TOML，
默认位置为用户目录下的 `.shovel/config/settings.toml`，不是 JSON。

| 命令 | 当前能力 |
| --- | --- |
| `init` | 初始化本地配置与存储；模型连通性失败会明确提示 |
| `config` | `--check` 校验、`--show` 原文显示（可能含密钥，勿分享）、`--force` 备份后重新生成 |
| `version` | 显示版本；`--verbose` / `--json` 显示运行环境 |
| `run dev` | 同时监督 Next.js 与本机 Python Host |
| `run prd` | 仅运行 Python Host，提供导出的静态 Web |
| `resource` | 创建、查看、验证、暂停、恢复、撤销、更新及删除资源 |
| `scan` | 持久扫描、作业查看与索引删除队列清理 |
| `operation` | 请求回执查找、状态查看、等待、重试 |
| `profile` | 列出、通过 JSON 文件保存、确认删除扫描 Profile |
| `history prune` | 明确确认后，按保留天数和批量上限清理已结束的历史 |

资源写入、扫描、Profile 和维护操作都经过同一个 Host 队列。已有 Host 时
CLI 使用 REST；没有时启动短生命周期非 Web Host。只读查询直接访问 SQLite。
OS 锁阻止第二个写入 Runtime 同时占用同一数据库或向量路径。

```powershell
uv run shovel resource add notes --kind local_fs --set 'root_path=C:\Notes'
uv run shovel --request-key notes-scan-001 scan run notes --no-embed
uv run shovel operation requests
uv run shovel operation wait <operation-id>
uv run shovel operation retry <operation-id>
uv run shovel profile list notes
uv run shovel profile save notes text --file profile.json
uv run shovel scan run notes --profile text --mode full_sweep --no-embed
uv run shovel history prune --keep-days 30 --limit 1000
```

一个请求意图使用一个 `--request-key`；响应丢失时复用原键，不要重新生成。
不同参数复用原键会报冲突。失败、取消和部分失败不会以成功退出码返回。
`operation wait/retry` 复用既有 Operation，不会创建新的扫描身份。

Profile 保存是完整替换，字段与校验规则见根 README 及 REST `/docs`。
默认 Profile 不允许删除；非默认 Profile 删除也会删除其扫描历史及独占索引，
不会删除源文件。历史清理保留幂等请求回执和未完成工作，不自动执行。

`doctor`、`model pull/remove`、独立 `db reset`、Agent 工具、调度和 Workflow
属于后续方向，**不是当前已实现的 CLI 命令**。生产打包及开发环境要求见
根 README 与 `prepare-develop-env.md`。
