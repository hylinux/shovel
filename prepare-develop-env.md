# 开发环境搭建指南


## 文档信息

| 项目 | 内容 |
|---|---|
| 文档名称 | Shovel 开发环境搭建指南 |
| 项目名称 | Shovel AI Agent Studio and Runtime Platform |
| 文档类型 | 开发环境搭建 / 依赖技术说明  |
| 作者 | HongWei Guo |
| 版本 | Draft v1.0 |
| 推荐路径 | `prepare-develop-env.md` |
| 更新时间 | 2026/7/31 |
| 目标读者 | 项目开发者、架构设计者、后续维护者 |

---

## 1. 开发工具说明

目前主要的开发语言是`Python`, `Typescript`, `CSS`, `html`，依赖如下的开发工具：

- Python > 3.14
- Node.js  Next.js 
- Tailwind CSS

需要安装的工具：

- Python > 3.14
- Node.js > V24 (Use LTS version)
- Python uv

请使用官方文档安装上述工具。

## 2. 依赖的外部数据库等工具：

本机开发建议使用Docker Desktop, 安装好docker 之后，请依次在本机启动qdrant 以及redis ，启动命令参考：

- `Qdrant`:

```bash
docker run -d --name qdrant_latest -p 6333:6333 -p 6334:6334 -v "d:/qdrantDB/:/qdrant/storage:z" qdrant:latest
```

- `Redis`:

```bash
docker run -d --name redis -p 6379:6379 -v d:/DockerData/RedisData:/data redis:latest
```

## 3. 代码准备：

分别在同级目录下clone 两个代码库：

```bash
git clone https://github.com/hylinux/py-generic-host.git
git clone https://github.com/hylinux/shovel.git
```

注意: `Shovel` 依赖 `py-generic-host`库，因此建议先`clone py-generic-host`

然后进入 `shovel` 目录，并运行：

```bash
uv sync --upgrade
```

会自动安装依赖以及编译，打包`whl`

然后尝试运行:

```bash
uv run shovel version
```

或者

```bash
uv run shovel version --verbose
```

初始化项目：

```bash
uv run shovel init
```

`Have Fun!`


