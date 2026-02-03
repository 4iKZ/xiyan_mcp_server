"""修复版测试脚本"""
import requests
import json
import time

base_url = "http://localhost:8000/mcp"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}

def test_query(query: str):
    print(f"\n{'='*60}")
    print(f"查询: {query}")
    print('='*60)
    
    # Step 1: 初始化
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
            print(f"无法获取 Session ID - Status: {resp.status_code}")
            return
        
        # Step 2: 发送 initialized 通知
        headers_with_session = {**headers, "Mcp-Session-Id": session_id}
        initialized_notification = {
            "jsonrpc": "2.0",
            "method": "notifications/initialized"
        }
        requests.post(base_url, headers=headers_with_session, json=initialized_notification, timeout=10)
        
        # Step 3: 调用 get_data 工具
        call_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_data",
                "arguments": {"query": query, "format": "markdown"}
            },
            "id": 2
        }
        
        resp = requests.post(base_url, headers=headers_with_session, json=call_request, timeout=300)
        print(f"HTTP Status: {resp.status_code}")
        
        # 解析 SSE 响应
        for line in resp.text.split('\n'):
            if line.startswith('data: '):
                try:
                    result = json.loads(line[6:])
                    if "result" in result:
                        content = result["result"].get("content", [])
                        for item in content:
                            if item.get("type") == "text":
                                print(f"\n结果:\n{item.get('text', '')[:800]}")
                    elif "error" in result:
                        print(f"错误: {result['error']}")
                except:
                    pass
                break
                
    except Exception as e:
        print(f"异常: {e}")

if __name__ == "__main__":
    # 测试列表
    queries = [
        "显示所有表",
        "查询 sys_cpu_usage 表的前 3 条数据",
    ]
    
    for q in queries:
        test_query(q)
        time.sleep(3)  # 避免速率限制
