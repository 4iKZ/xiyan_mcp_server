"""
MCP HTTP 自然语言查询测试 - 支持多种输出格式
"""
import requests
import json
import re

base_url = "http://localhost:8000/mcp"

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
    query = "查询最近5小时数据库集群的平均磁盘IO情况"
    
    # 测试三种格式
    print("\n" + "="*60)
    print("测试多种输出格式")
    print("="*60)
    
    for fmt in ["markdown", "json", "csv"]:
        test_query(query, format_type=fmt)
