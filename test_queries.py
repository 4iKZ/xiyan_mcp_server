"""综合功能测试"""
import requests
import json
import time

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def query_db(q, fmt="markdown"):
    """执行查询"""
    init_req = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1.0"}},
        "id": 1
    }
    resp = requests.post(base_url, headers=headers, json=init_req, timeout=60)
    session_id = resp.headers.get("Mcp-Session-Id")
    
    headers_sess = {**headers, "Mcp-Session-Id": session_id}
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    requests.post(base_url, headers=headers_sess, json=notif, timeout=10)
    
    call_req = {
        "jsonrpc": "2.0",
        "method": "tools/call",
        "params": {"name": "get_data", "arguments": {"query": q, "format": fmt}},
        "id": 2
    }
    resp = requests.post(base_url, headers=headers_sess, json=call_req, timeout=300)
    
    for line in resp.text.split('\n'):
        if line.startswith('data: '):
            try:
                result = json.loads(line[6:])
                if "result" in result:
                    for item in result["result"].get("content", []):
                        if item.get("type") == "text":
                            return item.get('text', '')
                elif "error" in result:
                    return f"Error: {result['error']}"
            except:
                pass
    return "No result"

print("="*60)
print("XiYan MCP Server 功能测试")
print("="*60)

tests = [
    ("列出所有表的名称", "markdown"),
    ("查询 sys_cpu_usage 表的前 2 条记录", "markdown"),
    ("统计 sys_cpu_usage 表中每个节点的平均 CPU 使用率", "markdown"),
]

for desc, fmt in tests:
    print(f"\n{'─'*60}")
    print(f"测试: {desc}")
    print('─'*60)
    result = query_db(desc, fmt)
    print(result[:600] if len(result) > 600 else result)
    time.sleep(3)  # 避免速率限制

print("\n" + "="*60)
print("测试完成！")
print("="*60)
