<div align="center">
  <img src="https://raw.githubusercontent.com/XGenerationLab/XiYan-SQL/main/xiyanGBI.png" height="80" alt="XiYan Logo">
  <h1>XiYan MCP Server</h1>
  <p>
    <b>基于阿里 XiYanSQL 框架改造的 Model Context Protocol (MCP) 服务器</b>
  </p>
  <p>
    支持通过自然语言查询 GreptimeDB、CockroachDB、MySQL、PostgreSQL 等数据库
  </p>

  <p>
    <a href="https://opensource.org/licenses/Apache-2.0">
      <img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache 2.0">
    </a>
    <a href="https://www.python.org/">
      <img src="https://img.shields.io/badge/Python-3.13+-blue.svg" alt="Python 3.13+">
    </a>
    <a href="https://github.com/4iKZ/xiyan_mcp_server">
      <img src="https://img.shields.io/github/stars/4iKZ/xiyan_mcp_server?style=social" alt="GitHub stars">
    </a>
    <a href="https://github.com/4iKZ/xiyan_mcp_server/releases">
      <img src="https://img.shields.io/github/v/release/4iKZ/xiyan_mcp_server" alt="Release">
    </a>
  </p>
</div>

---

## 目录

- [项目概述](#项目概述)
- [核心特性](#核心特性)
- [安装](#安装)
- [配置](#配置)
- [启动服务](#启动服务)
- [API 使用](#api-使用)
- [Docker 部署](#docker-部署)
- [可用工具](#可用工具)
- [项目架构](#项目架构)
- [与原项目的主要差异](#与原项目的主要差异)
- [Schema 知识库管理](#schema-知识库管理)
- [测试](#测试)
- [常见问题](#常见问题)
- [更新日志](#更新日志)
- [开源许可](#开源许可)
- [引用](#引用)

---

## 项目概述

本项目基于 [XGenerationLab/xiyan_mcp_server](https://github.com/XGenerationLab/xiyan_mcp_server) 进行深度改造，增强了以下功能：

- 支持 GreptimeDB 时序数据库
- 支持 CockroachDB 分布式数据库
- Schema 语义过滤与延迟加载优化
- 灵活的 Embedding 模型配置（云端 API / 本地 vLLM）
- 本地 vLLM 模型支持
- 多格式输出（Markdown、JSON、CSV）
- SQL 自动修复机制（最多 5 次重试）

## 核心特性

### 数据库支持
- **GreptimeDB**（时序数据库）
- **CockroachDB**（分布式关系型数据库）
- **MySQL**
- **PostgreSQL**
- **SQLite**

### 模型支持
- **通用 LLMs**（GPT、Qwen-Max 等）
- **XiYanSQL-QwenCoder** 系列模型
- **本地 vLLM** 部署模型

### 性能优化
- Redis 语义 Schema 检索
- 列信息延迟加载
- 全局数据库连接池（支持高并发查询）
  - 基础连接池大小：10
  - 最大额外连接数：20
  - 总共支持最多 30 个并发数据库连接
  - 自动连接回收与健康检查
- SQL 自动错误修复
- 多线程安全设计（支持 SQLite 并发访问）

## 安装

### 系统要求
- Python 3.13+
- Redis（可选，用于 Schema 过滤）

### 方式 1：从源码安装

```bash
# 克隆仓库
git clone https://github.com/4iKZ/xiyan_mcp_server.git
cd xiyan_mcp_server

# 安装依赖
pip install -e .
```

### 方式 2：从 Release 安装

```bash
# 下载特定版本的源码
wget https://github.com/4iKZ/xiyan_mcp_server/archive/refs/tags/v0.1.5.tar.gz
tar -xzf v0.1.5.tar.gz
cd xiyan_mcp_server-0.1.5

# 安装依赖
pip install -e .
```

## 配置

编辑 `src/xiyan_mcp_server/config.yml`：

### 基础配置

```yaml
model:
  name: "/share/modelscope/XiYanSQL-QwenCoder-14B-2504"
  key: "not-needed"
  url: "http://10.0.0.8:10000/v1/"

database:
  system: "cockroach"        # 可选：greptimedb, mysql, postgresql, sqlite
  dialect: "greptimedb"      # 数据库方言
  host: "10.0.0.8"
  port: 4003
  user: "root"
  password: ""
  database: "public"

schema_filter:
  enabled: true
  knowledge_dir: "json"
  top_k: 5
  score_threshold: 0.4

redis:
  host: "localhost"
  port: 6379
  password: ""
  index_name: "xiyan_schema"
```

### 并发配置（可选）

数据库连接池配置在 `src/xiyan_mcp_server/utils/db_util.py` 中修改：

```python
POOL_CONFIG = {
    "pool_size": 10,          # 基础连接数
    "max_overflow": 20,       # 最大额外连接数
    "pool_timeout": 30,       # 获取连接超时（秒）
    "pool_recycle": 3600,     # 连接回收时间（秒）
    "pool_pre_ping": True,    # 连接健康检查
}
```

**说明**：
- 默认支持最多 30 个并发数据库连接
- 适用于高并发场景
- 自动回收空闲连接，避免连接泄露

### Embedding 配置（重要）

**注意：切换 embedding 模型后，必须重新生成 Redis 索引：**

```bash
python scripts/index_knowledge.py --config src/xiyan_mcp_server/config.yml --rebuild
```

#### 选项 1：云端 ModelScope API（推荐）

全精度模型，效果最佳：

```yaml
embedding:
  model: "Qwen/Qwen3-Embedding-8B"
  use_api: true
  api_key: "your-modelscope-api-key"
  vector_dim: 4096
```

#### 选项 2：本地 vLLM API

需自行部署 embedding 模型：

```yaml
embedding:
  model: "Qwen/Qwen3-Embedding-8B"
  use_api: true
  use_vllm_format: true
  api_url: "http://localhost:10001/v1/"
  api_key: "not-needed"
  vector_dim: 4096
```

**注意事项：**
- 使用 4bit 量化可能导致向量区分度下降，建议使用 FP16/BF16 量化
- 云端 API 会自动添加 `encoding_format="float"` 参数获取全精度向量

## 启动服务

### 方式 1：直接启动

```bash
# stdio 模式（默认）
python -m xiyan_mcp_server

# HTTP 模式
python -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000

# SSE 模式
python -m xiyan_mcp_server sse --host 0.0.0.0 --port 8000
```

### 方式 2：Systemd 服务

```bash
systemctl start xiyan-mcp-server
systemctl status xiyan-mcp-server
```

### 方式 3：查看日志

```bash
tail -f /tmp/xiyan_server.log
```

## API 使用

### 初始化会话

```python
import requests

base_url = "http://localhost:8000/mcp"
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

# 初始化
init_request = {
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0"}
    },
    "id": 1
}

resp = requests.post(base_url, headers=headers, json=init_request, timeout=60)
session_id = resp.headers.get("Mcp-Session-Id")
```

### 查询数据

```python
headers["Mcp-Session-Id"] = session_id

call_request = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "get_data",
        "arguments": {
            "query": "查询最近5小时数据库集群的平均内存使用率",
            "format": "markdown"  # markdown, json, csv
        }
    },
    "id": 2
}

resp = requests.post(base_url, headers=headers, json=call_request, timeout=300)
```

## Docker 部署

### 方式 1：使用 Docker 命令

```bash
# 构建镜像
docker build -t xiyan-mcp-server .

# 运行容器（stdio 模式）
docker run -v $(pwd)/src/xiyan_mcp_server/config.yml:/app/src/xiyan_mcp_server/config.yml \
           xiyan-mcp-server

# 运行容器（HTTP 模式）
docker run -p 8000:8000 \
           -v $(pwd)/src/xiyan_mcp_server/config.yml:/app/src/xiyan_mcp_server/config.yml \
           xiyan-mcp-server \
           python -m xiyan_mcp_server streamable-http --host 0.0.0.0
```

### 方式 2：使用 Docker Compose（推荐）

```bash
# 启动服务
docker-compose up -d

# 查看日志
docker-compose logs -f xiyan-mcp-server

# 停止服务
docker-compose down
```

**提示**：编辑 `docker-compose.yml` 可以：
- 切换传输模式（stdio/HTTP/SSE）
- 添加 Redis 和 GreptimeDB 依赖服务
- 配置环境变量

## 可用工具

### get_data

通过自然语言查询数据库并返回结果。

参数：
- `query`（必填）：自然语言查询问题
- `format`（可选）：输出格式
  - `markdown`：表格格式（默认）
  - `json`：JSON 格式
  - `csv`：CSV 格式

## 项目架构

```
src/xiyan_mcp_server/
├── server.py           # MCP 服务入口
├── database_env.py     # 数据库环境封装
├── config.yml          # 配置文件
├── config.example.yml  # 配置文件模板
└── utils/
    ├── db_util.py              # 数据库连接池
    ├── llm_util.py             # LLM API 调用
    ├── schema_retriever.py     # Schema 检索
    ├── embedding_service.py    # Embedding 服务（支持云端/本地）
    ├── db_source.py            # 数据库元数据
    └── greptimedb_source.py    # GreptimeDB 支持

scripts/
└── index_knowledge.py  # Schema 知识库索引工具

json/                   # Schema 知识库目录
```

## 与原项目的主要差异

1. **GreptimeDB 支持**：新增 GreptimeDB 方言和专用数据源
2. **CockroachDB 支持**：完整支持 CockroachDB 分布式数据库
3. **Schema 优化**：基于 Redis 的语义检索 + 延迟加载
4. **本地模型**：完整支持本地 vLLM 部署
5. **输出格式**：支持 Markdown/JSON/CSV 三种格式
6. **错误处理**：增强 SQL 修复逻辑（5 次重试）
7. **配置分离**：Embedding 和主模型配置分离
8. **Embedding 灵活性**：支持云端 API 和本地 vLLM 两种部署方式

## Schema 知识库管理

### 首次索引

将 `json/` 目录下的数据库 Schema 信息索引到 Redis：

```bash
python scripts/index_knowledge.py --config src/xiyan_mcp_server/config.yml
```

### 重建索引

**注意：以下情况需要重建索引：**

- 切换 Embedding 模型（云端 API ↔ 本地 vLLM）
- 更换 Embedding 模型版本
- 修改数据库 Schema 结构
- 调整向量维度配置

```bash
python scripts/index_knowledge.py --config src/xiyan_mcp_server/config.yml --rebuild
```

### 检查 Schema

```bash
# 检查 Schema 知识库
python check_schema.py

# 检查表结构
python check_tables.py
```

## 测试

```bash
# 通用测试
python test.py

# 自然语言查询测试
python test_natural_language_queries.py

# CockroachDB 测试
python test_cockroachdb.py
```

## 常见问题

### 1. Python 版本要求

本项目需要 Python 3.13 或更高版本。

### 2. Redis 连接失败

确保 Redis 服务正在运行：

```bash
# 检查 Redis 状态
systemctl status redis

# 启动 Redis
systemctl start redis
```

### 3. Embedding 模型切换

切换 embedding 模型后，必须重新生成 Redis 索引，否则会影响检索效果。

### 4. Docker 容器无法访问数据库

确保容器网络可以访问数据库服务，可以使用 `docker network` 或 `--network host` 模式。

### 5. 并发连接数限制

默认数据库连接池支持最多 30 个并发连接。如果需要更高的并发，请修改 `src/xiyan_mcp_server/utils/db_util.py` 中的 `POOL_CONFIG`：

```python
"pool_size": 20,          # 增加基础连接数
"max_overflow": 30,       # 增加最大额外连接数
```

**注意**：增加连接数需要确保数据库服务器能够处理相应数量的并发连接。

### 6. 连接池耗尽

如果出现连接池耗尽的错误，可能的原因：
- 并发请求过多，超过了最大连接数
- 连接没有正确释放（代码 bug）
- 数据库查询时间过长

**解决方案**：
- 增加连接池大小
- 优化慢查询
- 检查代码是否有连接泄露

## 更新日志

### v0.1.5 (2025-02-05) - Beta

**新增功能**
- 支持 GreptimeDB 时序数据库
- 支持 CockroachDB 分布式数据库
- Schema 语义过滤与延迟加载优化
- 数据脱敏处理

**优化改进**
- 修复 Async 函数阻塞问题
- 完善 .gitignore 配置
- 更新 Python 版本要求到 3.13
- 优化依赖版本管理

**Docker 支持**
- 重写 Dockerfile，支持 Python 3.13
- 添加 .dockerignore 文件
- 新增 docker-compose.yml 配置

## 开源许可

本项目基于 Apache 2.0 许可证开源。

## 致谢与原作者

本项目基于 [XGenerationLab/xiyan_mcp_server](https://github.com/XGenerationLab/xiyan_mcp_server) 进行改造。

感谢原项目的所有贡献者：

- XGenerationLab
- ahmedmustahid
- YifuLiu-L
- eltociear
- lwsinclair
- Matvey-Kuk
- willyomg
- ZhuangbilityY

原项目技术支持来自 [XiYan-SQL](https://github.com/XGenerationLab/XiYan-SQL) 框架。

## 引用

如果您在研究中使用了本项目，欢迎引用：

```bibtex
@article{XiYanSQL,
      title={XiYan-SQL: A Novel Multi-Generator Framework For Text-to-SQL},
      author={Yifu Liu and Yin Zhu and Yingqi Gao and Zhiling Luo and Xiaoxia Li and Xiaorong Shi and Yuntao Hong and Jinyang Gao and Yu Li and Bolin Ding and Jingren Zhou},
      year={2025},
      eprint={2507.04701},
      archivePrefix={arXiv},
      primaryClass={cs.CL},
      url={https://arxiv.org/abs/2507.04701},
}
```
