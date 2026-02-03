"""简单测试脚本 - 测试基本功能"""
import requests
import json

base_url = "http://localhost:8000/mcp"
headers = {"Content-Type": "application/json"}

# 测试查询
test_queries = [
    "显示所有表",
    "查看 sys_cpu_usage 表的结构",
    "查询最近 5 条 CPU 使用率数据",
]

for query in test_queries:
    print(f"\n{'='*60}")
    print(f"测试查询: {query}")
    print('='*60)
    
    # 初始化
    init_req = {
        "jsonrpc": "2.0",
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"}
        },
        "id": 1
    }
    
    resp = requests.post(base_url, headers=headers, json=init_req, timeout=60)
    session_id = resp.headers.get("Mcp-Session-Id")
    
    if session_id:
        headers["Mcp-Session-Id"] = session_id
        
        # initialized 通知
        notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        requests.post(base_url, headers=headers, json=notif, timeout=10)
        
        # 调用工具
        call_req = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_data",
                "arguments": {"query": query, "format": "markdown"}
            },
            "id": 2
        }
        
        resp = requests.post(base_url, headers=headers, json=call_req, timeout=300)
        
        # 解析结果
        for line in resp.text.split('\n'):
            if line.startswith('data: '):
                try:
                    result = json.loads(line[6:])
                    if "result" in result:
                        for item in result["result"].get("content", []):
                            if item.get("type") == "text":
                                print(item.get("text", "")[:500])
                    elif "error" in result:
                        print(f"错误: {result['error']}")
                    break
                except:
                    pass
        
        print(f"HTTP Status: {resp.status_code}")
    
    # 等待一下避免速率限制
    import time
    time.sleep(2)
