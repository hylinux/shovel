# 自动生成项目快照

从项目根目录运行（需要 Windows PowerShell 5.1+ 或 PowerShell 7，以及 Git）：

```powershell
.\scripts\generate-project-snapshot.ps1
```

默认生成 `$env:USERPROFILE\shovel_snapshot.zip`，与原命令的实际输出位置一致，
不是桌面目录。也可以指定输出位置；父目录必须已存在，已有文件不会被覆盖：

```powershell
.\scripts\generate-project-snapshot.ps1 -OutputPath "$env:USERPROFILE\Desktop\shovel_snapshot.zip"
```

脚本默认根据自身位置定位项目，不依赖当前工作目录。也可以显式指定仓库：

```powershell
.\scripts\generate-project-snapshot.ps1 -ProjectRoot "C:\path\to\repo" -OutputPath "C:\path\to\snapshot.zip"
```

仓库需要至少有一个提交。快照保留目录结构，包含：

- Git 已跟踪的现存文件，以及未被忽略的新文件（去重）。
- `_gitlog.txt`：最近 100 条提交的单行图形日志。
- `_gitstatus.txt`：当前 Git 状态。
- `_uncommitted.diff`：未暂存与已暂存的差异，分别标记。

文件和差异均排除任意层级的 `.env*` 文件及目录；已跟踪的其他被忽略文件
仍按 Git 规则包含。未跟踪文件的内容保存在 ZIP 中，不出现在 Git diff 中。
符号链接会明确报错，不会跟随链接读取目标；子模块不会自动递归打包。

脚本不会在仓库中写入报告或文件列表。ZIP 先写入输出目录的临时文件，成功后
再移动到目标路径；失败会清理临时文件。若仓库内已有与三个报告同名的待打包
文件，会报错而不是覆盖或静默遗漏。

**分享前仍需人工检查快照。** `.env*` 排除不是通用密钥检测；其他配置文件、
源代码、提交信息及差异仍可能包含敏感内容。状态报告可能显示 `.env*` 路径，
但不会包含其文件内容。生成期间应避免修改文件，以免快照和 Git 报告不一致。
