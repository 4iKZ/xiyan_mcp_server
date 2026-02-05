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
      <img src="https://img.shields.io/badge/Python-3.11+-blue.svg" alt="Python 3.11+">
    </a>
    <a href="https://github.com/XGenerationLab/xiyan_mcp_server">
      <img src="https://img.shields.io/github/stars/XGenerationLab/xiyan_mcp_server?style=social" alt="GitHub stars">
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
- [可用工具](#可用工具)
- [项目架构](#项目架构)
- [与原项目的主要差异](#与原项目的主要差异)
- [Schema 知识库管理](#schema-知识库管理)
- [测试](#测试)
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
- 全局数据库连接池
- SQL 自动错误修复

## 安装

### 系统要求
- Python 3.11+
- Redis（可选，用于 Schema 过滤）

### 快速安装

```bash
cd /data/xiyan_mcp_server
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

### 直接启动

```bash
PYTHONPATH=/data/xiyan_mcp_server/src python -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000
```

### Systemd 服务

```bash
systemctl start xiyan-mcp-server
systemctl status xiyan-mcp-server
```

### 查看日志

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

## 开源许可

本项目基于 Apache 2.0 许可证开源。

## 致谢与原作者

本项目基于 [XGenerationLab/xiyan_mcp_server](https://github.com/XGenerationLab/xiyan_mcp_server) 进行改造。

感谢原项目的所有贡献者：

- XGenerationLab
- ahmedmustahid
- YifuLiuL
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
