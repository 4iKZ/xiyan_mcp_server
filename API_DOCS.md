# XiYan MCP Server API 文档

## 概述

XiYan MCP Server 是一个基于 Model Context Protocol (MCP) 的自然语言查询服务，支持通过自然语言查询 GreptimeDB 时序数据库。

| 项目 | 值 |
| --- | --- |
| **协议** | MCP over HTTP (streamable-http) |
| **基础 URL** | `http://172.19.19.148:8000/mcp` |
| **数据格式** | JSON-RPC 2.0 + SSE |

---

## 启动服务

```powershell
$env:PYTHONPATH="d:\Trae\code\xiyan_mcp_server-main\src"
$env:YML="d:\Trae\code\xiyan_mcp_server-main\src\xiyan_mcp_server\config.yml"
$env:HF_ENDPOINT="https://hf-mirror.com"
python -m xiyan_mcp_server.server streamable-http --host 172.19.19.148 --port 8000
```

---

## 接口调用流程

### Step 1: 初始化会话

**Request:**

```http
POST /mcp HTTP/1.1
Host: 172.19.19.148:8000
Content-Type: application/json
Accept: application/json, text/event-stream

{
    "jsonrpc": "2.0",
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "your-client", "version": "1.0"}
    },
    "id": 1
}
```

**Response Headers:**
```
Mcp-Session-Id: 7ec6f1aa2c4b43a7...
Content-Type: text/event-stream
```

**Response Body (SSE):**
```
event: message
data: {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"xiyan","version":"1.0.0"}}}
```

>  **重要**: 保存 `Mcp-Session-Id` 用于后续请求

---

### Step 2: 发送 Initialized 通知

**Request:**

```http
POST /mcp HTTP/1.1
Host: 172.19.19.148:8000
Content-Type: application/json
Mcp-Session-Id: {session_id}

{
    "jsonrpc": "2.0",
    "method": "notifications/initialized"
}
```

**Response:** `202 Accepted` (无内容)

---

### Step 3: 调用工具 (自然语言查询)

**Request:**
```http
POST /mcp HTTP/1.1
Host: 172.19.19.148:8000
Content-Type: application/json
Accept: application/json, text/event-stream
Mcp-Session-Id: {session_id}

{
    "jsonrpc": "2.0",
    "method": "tools/call",
    "params": {
        "name": "get_data",
        "arguments": {
            "query": "最近5分钟CPU使用率是多少",
            "format": "markdown"  // 可选: markdown, json, csv
        }
    },
    "id": 2
}
```

**Response (SSE):**
```
event: message
data: {"jsonrpc":"2.0","id":2,"result":{"content":[{"type":"text","text":"| greptime_value |\n| --- |\n| 63.40852130325815 |"}]}}
```

---

## 可用工具

| 工具名 | 描述 | 参数 |
| --- | --- | --- |
| `get_data` | 自然语言查询数据库 | `query`: string (必填) - 自然语言问题<br>`format`: string (可选) - 输出格式 |

### format 参数说明

| 值 | 描述 | 示例输出 |
| --- | --- | --- |
| `markdown` (默认) | Markdown 表格 | `\| field \| value \|` |
| `json` | JSON 格式 | `{"data": [{...}], "fields": [...]}` |
| `csv` | CSV 格式 | `field1,field2\nvalue1,value2` |

---

## 响应格式

### 成功响应

完整的 JSON-RPC 响应结构：

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "content": [{
            "type": "text",
            "text": "..."  // 根据 format 参数返回不同格式的内容
        }]
    }
}
```

#### format=markdown (默认)

`text` 字段内容：

```
| average_cpu_usage |
| --- |
| 44.05855966287442 |
```

#### format=json

`text` 字段内容：

```json
{"data": [{"average_cpu_usage": "43.64409214893834"}], "fields": ["average_cpu_usage"]}
```

#### format=csv

`text` 字段内容：

```
average_cpu_usage
43.43204274302167
```

#### 多行结果示例 (format=json)

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "result": {
        "content": [{
            "type": "text",
            "text": "{\"data\": [{\"timestamp\": \"2026-02-02 17:00:00\", \"cpu_usage\": 45.2}, {\"timestamp\": \"2026-02-02 17:01:00\", \"cpu_usage\": 52.8}], \"fields\": [\"timestamp\", \"cpu_usage\"]}"
        }]
    }
}
```

### 错误响应

```json
{
    "jsonrpc": "2.0",
    "id": 2,
    "error": {
        "code": -32602,
        "message": "Invalid request parameters"
    }
}
```

---

## Python 调用示例

**test_nl_http.py**

```python
"""
MCP HTTP 自然语言查询测试 - 支持多种输出格式
"""
import requests
import json
import re

base_url = "http://172.19.19.148:8000/mcp"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

def parse_sse_response(text):
    """解析 SSE 响应，提取 JSON 数据"""
    for line in text.split('\n'):
        if line.startswith('data: '):
            try:
                return json.loads(line[6:])
            except:
                pass
    return None

def test_query(query: str, format_type: str = "markdown"):
    """测试查询并指定输出格式
    
    Args:
        query: 自然语言查询
        format_type: 输出格式 (markdown, json, csv)
    """
    print(f"\n{'='*60}")
    print(f"查询: {query}")
    print(f"格式: {format_type}")
    print('='*60)
    
    # Step 1: 初始化会话
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
    
    try:
        resp = requests.post(base_url, headers=headers, json=init_request, timeout=60)
        session_id = resp.headers.get("Mcp-Session-Id")
        
        if not session_id:
            print("无法获取 Session ID")
            return
        
        print(f"Session ID: {session_id[:16]}...")
        
        # Step 2: 发送 initialized 通知
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        
        headers_with_session = {
            **headers,
            "Mcp-Session-Id": session_id
        }
        
        requests.post(base_url, headers=headers_with_session, json=initialized_notification, timeout=10)
        print("Initialized notification sent")
        
        # Step 3: 调用 get_data 工具（带格式参数）
        call_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_data",
                "arguments": {
                    "query": query,
                    "format": format_type  # 新增：指定输出格式
                }
            },
            "id": 2
        }
        
        resp = requests.post(base_url, headers=headers_with_session, json=call_request, timeout=300)
        print(f"HTTP Status: {resp.status_code}")
        
        # 解析 SSE 响应
        result = parse_sse_response(resp.text)
        if result:
            if "result" in result:
                content = result["result"].get("content", [])
                for item in content:
                    if item.get("type") == "text":
                        print(f"\n查询结果 ({format_type}):\n{item.get('text', '')}")
            elif "error" in result:
                print(f"错误: {result['error']}")
        else:
            print(f"原始响应:\n{resp.text[:500]}")
            
    except Exception as e:
        print(f"异常: {e}")

if __name__ == "__main__":
    query = "最近5分钟CPU使用率是多少"
    
    # 测试三种格式
    print("\n" + "="*60)
    print("测试多种输出格式")
    print("="*60)
    
    for fmt in ["markdown", "json", "csv"]:
        test_query(query, format_type=fmt)

```

---

## 注意事项

1. **超时设置**: 由于涉及 LLM 调用，建议设置较长的超时时间（300秒）
2. **会话管理**: 每个会话需要先 `initialize` 再 `notifications/initialized`
3. **并发限制**: 同一 Session 内请求需串行执行





# 别的测试方法

## 方法 1：终端 (PowerShell)

由于 **MCP 协议需要 3 步调用**，PowerShell 操作较繁琐，建议使用我们已有的测试脚本：

```bash
python test_nl_http.py
```

如果要手动逐步测试，可以这样：

```powershell
# Step 1: 初始化并获取 Session ID

$resp = Invoke-WebRequest -Uri "http://172.19.19.148:8000/mcp" -Method POST `

    -ContentType "application/json" `

    -Headers @{"Accept"="application/json, text/event-stream"} `

    -Body '{"jsonrpc":"2.0","method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}},"id":1}'



$sessionId = $resp.Headers["Mcp-Session-Id"]

Write-Host "Session ID: $sessionId"



# Step 2: 发送 initialized 通知

Invoke-WebRequest -Uri "http://172.19.19.148:8000/mcp" -Method POST `

    -ContentType "application/json" `

    -Headers @{"Mcp-Session-Id"=$sessionId} `

    -Body '{"jsonrpc":"2.0","method":"notifications/initialized"}'



# Step 3: 调用工具

Invoke-WebRequest -Uri "http://172.19.19.148:8000/mcp" -Method POST `

    -ContentType "application/json" `

    -Headers @{"Accept"="application/json, text/event-stream"; "Mcp-Session-Id"=$sessionId} `

    -Body '{"jsonrpc":"2.0","method":"tools/call","params":{"name":"get_data","arguments":{"query":"CPU使用率"}},"id":2}' `

    -TimeoutSec 300
```

------

## 方法 2：Postman

### 请求 1：初始化

| 项目            | 值                                                           |
| :-------------- | :----------------------------------------------------------- |
| Method          | `POST`                                                       |
| URL             | `http://172.19.19.148:8000/mcp`                                  |
| Headers         | `Content-Type: application/json``Accept: application/json, text/event-stream` |
| Body (raw JSON) | 见下方                                                       |

```json
{

    "jsonrpc": "2.0",

    "method": "initialize",

    "params": {

        "protocolVersion": "2024-11-05",

        "capabilities": {},

        "clientInfo": {"name": "postman", "version": "1.0"}

    },

    "id": 1

}
```

**发送后**：从 Response Headers 复制 

```json
Mcp-Session-Id
```

 的值。



------

### 请求 2：Initialized 通知

| 项目    | 值                                    |
| :------ | :------------------------------------ |
| Headers | 添加 `Mcp-Session-Id: {刚才复制的值}` |
| Body    | 见下方                                |

```json
{

    "jsonrpc": "2.0",
    "method": "notifications/initialized"

}
```

------

### 请求 3：查询

| 项目    | 值                                |
| :------ | :-------------------------------- |
| Headers | 保持 `Mcp-Session-Id` 和 `Accept` |
| Timeout | 建议设置 300 秒                   |
| Body    | 见下方                            |

```json
{

    "jsonrpc": "2.0",

    "method": "tools/call",

    "params": {

        "name": "get_data",

        "arguments": {"query": "最近5分钟CPU使用率是多少"}

    },

    "id": 2

}
```

## 建议

由于 MCP 协议需要维护 Session，**手动测试比较繁琐**。推荐：

1. 直接使用

   ```bash
   python test_nl_http.py
   ```

---
**版本信息**
- 当前版本: v1.0.1
- 修改内容: 统一更新内网 IP 为 172.19.19.148
- 修改日期: 2026-02-03
