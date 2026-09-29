# `Shovel Cli` 设计目标

当前根据`cpolit`规划设计的命令行子命令：

shovel: 

Environment
├── init
├── doctor
├── config
│   ├── show
│   ├── edit
│   └── reset

Development
├── dev
├── build-ui
├── build

Runtime
├── start
└── version

Models
├── model
│   ├── list
│   ├── pull
│   └── remove

Database
└── db
    ├── init
    └── reset

## 各个子命令概述

1. `shovel init`: 初始化 `Shovel Agent` 运行的必要环境，目前先期规划要在用户目录下创建目录:

- `$USERPROFILE\.shovel`  主目录
- `$userprofile\.shovel\config` 保存配置的目录, 所有的配置都以`json`文件配置
- `$USERPROFILE\.shovel\logs`  保存配置的日志, 所有的日志文件写入到这个目录
- `$USERPROFILE\.shovel\workspace` 创建多个工作区间，每个工作区间里会保存各种信息，包括`Agent`, `Skill`, `Workflow` 等等
- `$USERPROFILE\.shovel\db` 用于保存自带的数据库，默认是使用`sqlite`, 现在还不考虑设计支持其他的数据库，这个以后再说。

除了建目录，`shovel init` 还会：生成默认配置文件、建 SQLite 表、建知识库
(Zvec) 与记忆库 (mem0 + Qdrant) 的 collection，最后检查默认大模型的连通性。

连通性检查永远不会让 `init` 失败：用户刚装完还没填 API Key 时必然连不上，
把"没配"当成错误等于要求先手写配置文件才能初始化。未配置报 info，配了但
连不上报 warning。

2. `shovel config`: 管理 `$USERPROFILE\.shovel\config\settings.toml`。

- 无参数：文件不存在就生成默认配置，已存在则读一遍做校验
- `--check`：只校验，绝不创建或修改文件
- `--force`：用默认值重新生成（先自动备份旧文件）
- `--show`：打印当前配置文件内容

生成的配置文件包含**全部**配置项。所有"可空"的配置项一律用空串表示未设置，
而不是 `None` —— TOML 没有 null，`None` 的键只能被省略，结果就是用户拿到
一堆空的 `[model]` / `[document]` 表，看不出有哪些键可以填。

我们今天先来设计个简单的命令 `Version` 显示`Shovel`的版本以及`Log` 以及说明。

3. `shovel version`: 实现了显示版本的命令，默认输出：

```powershell
❯ uv run shovel version


███████╗██╗  ██╗ ██████╗ ██╗   ██╗███████╗██╗
██╔════╝██║  ██║██╔═══██╗██║   ██║██╔════╝██║
███████╗███████║██║   ██║██║   ██║█████╗  ██║
╚════██║██╔══██║██║   ██║╚██╗ ██╔╝██╔══╝  ██║
███████║██║  ██║╚██████╔╝ ╚████╔╝ ███████╗███████╗
╚══════╝╚═╝  ╚═╝ ╚═════╝   ╚═══╝  ╚══════╝╚══════╝


DIG • BUILD • DISCOVER

🚀 Personal AI Agent Platform

╭─────────────────────────────────── About ────────────────────────────────────╮
│                                                                              │
│  Shovel is a personal AI Agent platform that helps users build, run,and      │
│  manage local or cloud-based AI Agents through a unified Web experience.     │
│                                                                              │
╰──────────────────────────────────────────────────────────────────────────────╯

╭───────────────────────────────────────────────────────────────╮
│                                                               │
│                      Product Information                      │
│                                                               │
│      📦 Name                Shovel                            │
│      🏷 Version              0.1.0                             │
│      👨 Author              HongWei Guo                       │
│                                                               │
│                                                               │
╰───────────────────────────────────────────────────────────────╯

```
