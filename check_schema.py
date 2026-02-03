"""检查数据库 Schema 和表结构"""
import requests
import json

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

# 初始化
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

# 列出所有资源（表）
list_req = {
    "jsonrpc": "2.0",
    "method": "resources/list",
    "id": 2
}
resp = requests.post(base_url, headers=headers_sess, json=list_req, timeout=60)

for line in resp.text.split('\n'):
    if line.startswith('data: '):
        try:
            result = json.loads(line[6:])
            if "result" in result:
                resources = result["result"].get("resources", [])
                print("数据库中的所有资源（表）：\n")
                for r in resources:
                    uri = r.get("uri", "")
                    name = r.get("name", "")
                    print(f"  URI: {uri}")
                    print(f"  名称: {name}")
                    print()
        except Exception as e:
            print(f"解析错误: {e}")
        break
