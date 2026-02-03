"""测试 CockroachDB 专属查询"""
import requests
import json
import time

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def query_db(q):
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
                            print(item.get('text', '')[:500])
                elif "error" in result:
                    print(f"Error: {result['error']}")
            except:
                pass
            break

print("="*60)
print("CockroachDB 专属功能测试")
print("="*60)

tests = [
    "查询 CockroachDB 的 CPU 系统使用率，显示前 3 条数据",
    "统计 CockroachDB 的 SQL 查询总数",
]

for q in tests:
    print(f"\n查询: {q}")
    print('-'*60)
    query_db(q)
    time.sleep(3)

print("\n" + "="*60)
