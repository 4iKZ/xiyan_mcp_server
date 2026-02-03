"""直接查询数据库 Schema"""
import requests
import json

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def query_nl(q):
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
        "params": {"name": "get_data", "arguments": {"query": q, "format": "markdown"}},
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
                            print(item.get('text', ''))
                elif "error" in result:
                    print(f"Error: {result['error']}")
            except:
                pass
            break

print("="*60)
print("查询 CPU 相关的表及所属 Schema")
print("="*60)

# 查询 CockroachDB 相关的 CPU 表
print("\n【查询 1】CockroachDB 系统的 CPU 使用率：")
print("-"*60)
query_nl("查询 CockroachDB_metrics.sys_cpu_sys_percent 表的前 3 条数据")

import time
time.sleep(3)

# 查询 sundb 相关的 CPU 表
print("\n【查询 2】Sundb 系统的 CPU 使用率：")
print("-"*60)
query_nl("查询 sundb_metrics.sys_cpu_usage 表的前 3 条数据")
