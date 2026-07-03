# XiYan MCP Server API 文档

## 概述

XiYan MCP Server 是一个基于 Model Context Protocol (MCP) 的自然语言查询服务，支持通过自然语言查询 GreptimeDB、CockroachDB、MySQL 等时序数据库和关系型数据库。

| 项目 | 值 |
| --- | --- |
| **协议** | MCP over HTTP (streamable-http) |
| **基础 URL** | `http://localhost:8000/mcp` |
| **数据格式** | JSON-RPC 2.0 + SSE |

---

## 配置文件

服务配置文件位于 `src/xiyan_mcp_server/config.yml`：

```yaml
model:
  name: "XGenerationLab/XiYanSQL-QwenCoder-32B-2504"
  key: "your-api-key"
  url: "https://api-inference.modelscope.cn/v1/"

database:
  system: "sundb"              # 数据库系统标识 (sundb, cockroach 等)
  dialect: "greptimedb"        # 数据库方言
  host: "10.0.0.8"
  port: 4003
  user: "root"
  password: ""
  database: "public"

# Schema 过滤配置
schema_filter:
  enabled: true
  knowledge_dir: "json"
  top_k: 5
  score_threshold: 0.4

# Embedding 模型配置
embedding:
  model: "Qwen/Qwen3-Embedding-8B"
  use_api: true
  api_key: "your-api-key"
  vector_dim: 4096

# Redis 配置
redis:
  host: "localhost"
  port: 6379
  password: ""
  index_name: "xiyan_schema"
```

---

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `YML` | 配置文件路径（可选，默认为 `src/xiyan_mcp_server/config.yml`） |
| `PYTHONPATH` | Python 模块搜索路径，需包含 `src` 目录 |

---

## 服务启动

### 方式一：直接启动

```bash
cd /data/xiyan_mcp_server
PYTHONPATH=/data/xiyan_mcp_server/src python -m xiyan_mcp_server streamable-http --host 0.0.0.0 --port 8000
```

### 方式二：systemd 服务

```bash
# 启动服务
systemctl start xiyan-mcp-server

# 停止服务
systemctl stop xiyan-mcp-server

# 重启服务
systemctl restart xiyan-mcp-server

# 查看服务状态
systemctl status xiyan-mcp-server

# 查看日志
tail -f /tmp/xiyan_server.log
```

---

## API 使用流程

### 1. 初始化会话

首先需要初始化会话以获取 Session ID：

```python
import requests
import json

base_url = "http://localhost:8000/mcp"
headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

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

### 2. 发送 initialized 通知

初始化后需要发送 `initialized` 通知：

```python
headers_with_session = {
    **headers,
    "Mcp-Session-Id": session_id
}

initialized_notification = {
    "jsonrpc": "2.0",
    "method": "notifications/initialized"
}

requests.post(base_url, headers=headers_with_session, json=initialized_notification, timeout=10)
```

### 3. 调用查询工具

使用 `get_data` 工具进行自然语言查询：

```python
call_request = {
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "get_data",
        "arguments": {
            "query": "查询最近五分钟CPU平均使用率",
            "format": "markdown"  # 可选: markdown, json, csv
        }
    },
    "id": 2
}

resp = requests.post(base_url, headers=headers_with_session, json=call_request, timeout=300)
```

### 4. 解析 SSE 响应

响应采用 Server-Sent Events (SSE) 格式：

```python
def parse_sse_response(text):
    """解析 SSE 响应，提取 JSON 数据"""
    for line in text.split('\n'):
        if line.startswith('data: '):
            try:
                return json.loads(line[6:])
            except:
                pass
    return None

result = parse_sse_response(resp.text)
if result and "result" in result:
    content = result["result"].get("content", [])
    for item in content:
        if item.get("type") == "text":
            print(item.get('text', ''))
```

---

## 可用工具

### get_data

通过自然语言查询数据库并返回结果。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `query` | string | 是 | 自然语言查询问题 |
| `format` | string | 否 | 输出格式：`markdown`（默认）、`json`、`csv` |

**返回：**

根据 `format` 参数返回不同格式的数据：

- **markdown**：表格格式的 Markdown 文本
- **json**：JSON 格式的数据和字段信息
- **csv**：CSV 格式的文本

---

## 查询示例

### 示例 1：查询 CPU 使用率（Markdown 格式）

```python
query = "查询最近五分钟CPU平均使用率"
format_type = "markdown"

# 响应示例：
# | avg_cpu_usage |
# | --- |
# | 39.20142782682185 |
```

### 示例 2：查询 CPU 使用率（JSON 格式）

```python
query = "查询最近五分钟CPU平均使用率"
format_type = "json"

# 响应示例：
# {
#   "data": [
#     {
#       "average_cpu_usage": "39.04800731873074"
#     }
#   ],
#   "fields": ["average_cpu_usage"]
# }
```

### 示例 3：查询 CPU 使用率（CSV 格式）

```python
query = "查询最近五分钟CPU平均使用率"
format_type = "csv"

# 响应示例：
# average_cpu_usage
# 39.423158487113824
```

### 示例 4：CockroachDB 专属查询

```python
query = "查询 CockroachDB 的 CPU 系统使用率，显示前 3 条数据"

# 响应示例：
# | greptime_timestamp | greptime_value | instance | job | node_id |
# | --- | --- | --- | --- | --- |
# | 2026-02-04 06:25:06.590000 | 0.01599930712920567 | 172.19.19.128:8080 | cockroachdb | 3 |
# | 2026-02-04 06:25:05.444000 | 0.014997762521303854 | 172.19.19.126:8080 | cockroachdb | 4 |
# | 2026-02-04 06:25:03.802000 | 0.018996885635371427 | 172.19.19.127:8080 | cockroachdb | 2 |
```

---

## 支持的数据库

| 数据库 | dialect | 说明 |
| --- | --- | --- |
| GreptimeDB | `greptimedb` | 时序数据库，支持 PostgreSQL 协议 |
| MySQL | `mysql` | 关系型数据库 |
| SQLite | `sqlite` | 轻量级关系型数据库 |

---

## Schema 过滤

服务支持基于 Redis 的语义 Schema 过滤，可以大幅提升查询性能：

1. **初始化阶段**：只加载表名，不加载列信息（秒级完成）
2. **查询阶段**：通过 Redis 语义检索筛选出相关表（默认 5 个）
3. **延迟加载**：只加载筛选出的表的列信息

此功能在 `config.yml` 中通过 `schema_filter.enabled` 配置启用。

---

## 错误处理

### 常见错误

| 错误 | 说明 | 解决方案 |
| --- | --- | --- |
| `406 Not Acceptable` | Accept 头缺失或错误 | 添加 `Accept: application/json, text/event-stream` |
| `400 Bad Request` | Session ID 缺失 | 确保先调用 `initialize` 并在后续请求中携带 Session ID |
| `Database error` | 数据库连接或查询错误 | 检查数据库配置和连接状态 |

---

## 测试

项目采用 pytest 三层测试架构：

```bash
# 仅运行单元测试（默认，无需外部服务）
pytest

# 运行集成测试（需先启动 MCP Server）
pytest -m integration

# 运行端到端批量查询测试
pytest -m e2e

# 运行全部测试
pytest -m ""
```

测试文件位于 `tests/` 目录下：
- `tests/unit/` — 单元测试（纯函数，无外部依赖）
- `tests/integration/` — 集成测试（需 MCP Server 运行）
- `tests/e2e/` — 端到端测试（批量查询，数据源: `queries.jsonl`）

---

## 日志

服务日志位置：`/tmp/xiyan_server.log`

查看实时日志：

```bash
tail -f /tmp/xiyan_server.log
```

---

## 性能优化

1. **全局数据库引擎单例**：避免连接池泄漏
2. **Schema 延迟加载**：只加载必要的列信息
3. **Redis 语义检索**：快速筛选相关表
4. **SQL 自动修复**：失败后自动重试 3 次

---

## 许可证

参见 [LICENSE.txt](LICENSE.txt)
