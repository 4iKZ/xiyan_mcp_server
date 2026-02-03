"""简单的功能测试"""
import requests
import json
import time

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

def query(q):
    """执行单个查询"""
    # 初始化
    init = {"jsonrpc": "2.0", "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                       "clientInfo": {"name": "test", "version": "1.0"}}, "id": 1}
    resp = requests.post(base_url, headers=headers, json=init, timeout=30)
    session_id = resp.headers.get("Mcp-Session-Id")
    if not session_id:
        print("无法获取 Session ID")
        return

    headers["Mcp-Session-Id"] = session_id
    # initialized 通知
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    requests.post(base_url, headers=headers, json=notif, timeout=10)

    # 调用工具
    call = {"jsonrpc": "2.0", "method": "tools/call",
            "params": {"name": "get_data", "arguments": {"query": q, "format": "markdown"}}, "id": 2}
    resp = requests.post(base_url, headers=headers, json=call, timeout=60)

    # 解析结果
    for line in resp.text.split('\n'):
        if line.startswith('data: '):
            try:
                result = json.loads(line[6:])
                if "result" in result:
                    for item in result["result"].get("content", []):
                        if item.get("type") == "text":
                            print(item.get('text', '')[:500])
                elif "error" in result:
                    print(f"错误: {result['error']}")
            except:
                pass
            break

# 测试查询
print("=" * 50)
print("测试：查询 CockroachDB 相关的表")
print("=" * 50)
query("列出 cockroach_metrics 相关的表")

print("\n" + "=" * 50)
print("测试完成")
print("=" * 50)
