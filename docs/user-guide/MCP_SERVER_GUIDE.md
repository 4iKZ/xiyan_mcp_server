# XiYan MCP Server — 服务介绍与远程接入指南

## 概述

XiYan MCP Server 是一个基于 **Model Context Protocol (MCP)** 的自然语言数据库查询服务。它将 LLM 驱动的 Text-to-SQL 能力封装为标准 MCP 接口，让任意支持 MCP 的客户端（如 Claude Desktop、Cursor、FastMCP Client 等）都能通过自然语言直接查询时序数据库。

**核心能力**：自然语言 → SQL 生成 → 数据库执行 → 结果输出（含 HDFS 持久化）。

---

## 服务信息

| 项目 | 详情 |
|------|------|
| **服务名称** | `xiyan` |
| **MCP 协议版本** | `2024-11-05` |
| **传输协议** | streamable-http / stdio / SSE |
| **默认端口** | `8000` |
| **HTTP 端点** | `http://<host>:8000/mcp` |

---

工具清单：

  如果 LangGraph 框架还需要注册具体的 Tool 列表，两个工具的完整定义如下：

  工具 1: get_data
  输入: query (自然语言), format (markdown/json/csv)
  输出: 格式化的数据表格
  示例: "查询sundb_metrics库的sys_cpu_usage表前10条数据"

  工具 2: query_and_upload_to_hdfs
  输入: query (自然语言), session_id (可选)
  输出: HDFS Parquet 文件路径
  示例: "查询sys_cpu_usage表前100条数据" →
  hdfs://user_custom_data/20250428_batch_001/parquet/result.parquet

## 可用工具 (Tools)

### 1. get_data — 自然语言查询数据库

输入自然语言，自动翻译为 SQL 并执行，返回格式化数据表。

**参数：**

| 参数 | 类型 | 默认值 | 必需 | 说明 |
|------|------|--------|------|------|
| `query` | `string` | - | 是 | 自然语言查询，支持中英文。例如：`查询sundb_metrics库的sys_cpu_usage表，HOST_IP为'172.19.19.111'的近期5条数据` |
| `format` | `string` | `"markdown"` | 否 | 输出格式：`markdown`（表格）、`json`、`csv` |

**返回示例 (markdown)：**
```
| greptime_timestamp | greptime_value |
| --- | --- |
| 2025-04-28 10:00:00 | 45.2 |
| 2025-04-28 10:01:00 | 48.1 |
```

### 2. query_and_upload_to_hdfs — 查询并上传至 HDFS

在 `get_data` 的基础上，将查询结果转换为 Parquet 文件并上传到 HDFS 存储，返回 HDFS 文件路径。

**参数：**

| 参数 | 类型 | 默认值 | 必需 | 说明 |
|------|------|--------|------|------|
| `query` | `string` | - | 是 | 自然语言查询 |
| `session_id` | `string` | `""` | 否 | 会话 ID，用于 Parquet 文件命名（不传则使用时间戳） |

**返回示例：**
```
数据已成功上传到 HDFS: hdfs://user_custom_data/20250428_153022_batch_001/parquet/test_session.parquet
```

### 处理流程

```
用户自然语言 → LLM (XiYanSQL-QwenCoder) → SQL
    → Schema 语义过滤（可选，Redis + Embedding）
    → 数据库执行 (GreptimeDB/CockroachDB/MySQL/...)
    → SQL 错误自动修复（最多5次重试）
    → 结果格式化 (Markdown/JSON/CSV) 或 Parquet → HDFS
```

---

## 可用资源 (Resources)

| 资源 URI | 说明 | 返回内容 |
|----------|------|----------|
| `greptimedb://{database}` | 获取数据库完整 Schema | Markdown 格式的表结构文档（带 10 分钟 TTL 缓存） |
| `greptimedb://{table_name}` | 获取指定表内容 | CSV 格式的表数据（含白名单校验防注入） |

---

## 远程接入指南

### 前置条件

1. **网络可达**：确保客户端能访问 MCP Server 所在主机的 `8000` 端口
2. **Python 客户端**：安装 `fastmcp` 包
   ```bash
   pip install fastmcp
   ```

### 服务端启动

```bash
# HTTP 模式（推荐用于远程接入）
python -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000

# SSE 模式
python -m xiyan_mcp_server sse --host 0.0.0.0 --port 8000
```

### 客户端接入 (Python / FastMCP)

```python
import asyncio
from fastmcp.client import Client

SERVER_URL = "http://<MCP服务器IP>:8000/mcp"

async def main():
    async with Client(SERVER_URL) as client:
        # 查看可用工具
        tools = await client.list_tools()
        print("可用工具:", [t.name for t in tools])

        # 1. 使用 get_data 查询数据
        result = await client.call_tool(
            "get_data",
            {
                "query": "查询sundb_metrics库的sys_cpu_usage表的前10条数据",
                "format": "json"
            }
        )
        for item in result.content:
            print(item.text)

        # 2. 使用 query_and_upload_to_hdfs 查询并上传
        result = await client.call_tool(
            "query_and_upload_to_hdfs",
            {
                "query": "查询sundb_metrics库的sys_cpu_usage表的前100条数据",
                "session_id": "my_session_001"
            }
        )
        for item in result.content:
            print(item.text)

asyncio.run(main())
```

### 客户端接入 (Claude Desktop)

在 Claude Desktop 的配置文件中添加：

```json
{
  "mcpServers": {
    "xiyan": {
      "url": "http://<MCP服务器IP>:8000/mcp"
    }
  }
}
```

### 客户端接入 (Cursor / 其他 MCP 兼容客户端)

在对应 MCP 配置中添加 Streamable HTTP 传输：

```json
{
  "mcpServers": {
    "xiyan": {
      "type": "streamable-http",
      "url": "http://<MCP服务器IP>:8000/mcp"
    }
  }
}
```

### 客户端接入 (原始 HTTP / SSE)

如果不使用 FastMCP 客户端库，也可以直接通过 MCP JSON-RPC 协议访问：

```python
import requests

base_url = "http://<MCP服务器IP>:8000/mcp"
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

# 1. 初始化会话
resp = requests.post(base_url, headers=headers, json={
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "my-client", "version": "1.0"}
    },
    "id": 1
})
session_id = resp.headers.get("Mcp-Session-Id")

# 2. 列出工具
headers["Mcp-Session-Id"] = session_id
resp = requests.post(base_url, headers=headers, json={
    "jsonrpc": "2.0",
    "method": "tools/list",
    "params": {},
    "id": 2
})
print(resp.json())

# 3. 调用工具
resp = requests.post(base_url, headers=headers, json={
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "get_data",
        "arguments": {
            "query": "查询sundb_metrics库的sys_cpu_usage表的前10条数据",
            "format": "markdown"
        }
    },
    "id": 3
}, timeout=300)
print(resp.json())
```

### 请求/响应超时注意事项

- `get_data` 涉及 LLM 推理 + 数据库查询，建议超时设为 **120-300 秒**
- `query_and_upload_to_hdfs` 额外包含 Parquet 转换和 SCP/HDFS 上传，建议超时设为 **300+ 秒**

---

## 架构说明

```
┌─────────────────────────────────────────────────────┐
│                    MCP Client                        │
│  (Claude Desktop / Cursor / FastMCP / LangGraph)    │
└───────────────────────┬─────────────────────────────┘
                        │ HTTP (streamable-http)
                        ▼
┌─────────────────────────────────────────────────────┐
│               XiYan MCP Server                       │
│                                                      │
│  Tool: get_data          Tool: query_and_upload_to_hdfs │
│  Resource: Schema        Resource: Table Data         │
│                                                      │
│  ┌──────────────────────────────────────────────┐   │
│  │           Text-to-SQL Pipeline                │   │
│  │  1. Schema 语义过滤 (Redis + Embedding)      │   │
│  │  2. LLM SQL 生成 (XiYanSQL-QwenCoder)        │   │
│  │  3. SQL 安全执行 (白名单防注入)               │   │
│  │  4. 错误自动修复 (最多5次迭代)               │   │
│  │  5. 结果格式化 (Markdown/JSON/CSV/Parquet)    │   │
│  └──────────────────────────────────────────────┘   │
└───────────────────────┬─────────────────────────────┘
                        │
          ┌─────────────┼─────────────┐
          ▼              ▼              ▼
   ┌──────────┐  ┌──────────┐  ┌──────────┐
   │ GreptimeDB│  │  Redis    │  │   LLM    │
   │ (时序DB)  │  │ (Schema) │  │ (ModelScope│
   │          │  │ (向量检索)│  │ / vLLM)  │
   └──────────┘  └──────────┘  └──────────┘
                        │
                        ▼
                 ┌──────────┐
                 │   HDFS    │
                 │ (Parquet) │
                 └──────────┘
```

---

## 配置一览

服务端完整配置文件位于 `src/xiyan_mcp_server/config.yml`，关键配置项：

| 配置组 | 关键字段 | 说明 |
|--------|----------|------|
| `model` | `name`, `key`, `url` | LLM 模型配置，支持 ModelScope API 或本地 vLLM |
| `database` | `dialect`, `host`, `port`, `database` | 数据库连接（支持 greptimedb, mysql, postgresql, sqlite, cockroachdb） |
| `schema_filter` | `enabled`, `top_k`, `score_threshold` | Schema 语义过滤开关，基于 Redis + Embedding 检索相关表 |
| `embedding` | `model`, `use_api`, `vector_dim` | Embedding 模型（云端 ModelScope API 或本地 vLLM） |
| `redis` | `host`, `port`, `index_name` | Redis 连接（存储 Schema 向量索引） |
| `hdfs` | `enabled`, `remote_host`, `hdfs_root_dir` | HDFS 上传配置（SSH+SCP 方式） |

---

## 安全说明

- **SQL 注入防护**：Resource 接口使用表名白名单验证，禁止任意 SQL 入参
- **无内置认证**：当前版本无 API Key / OAuth 认证，建议部署在内网环境或配合反向代理（Nginx）添加认证
- **网络安全**：如需公网暴露，建议使用 TLS 终止（Nginx + Let's Encrypt）+ IP 白名单

---

## 版本信息

- **当前版本**: v0.1.5+
- **许可证**: Apache 2.0
- **代码仓库**: https://github.com/4iKZ/xiyan_mcp_server
- **上游项目**: [XGenerationLab/XiYan-SQL](https://github.com/XGenerationLab/XiYan-SQL)
