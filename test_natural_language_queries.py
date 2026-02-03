"""
XiYan MCP Server 自然语言查询综合测试

测试各种类型的自然语言问题，验证：
1. 简单查询
2. 聚合查询
3. 时间范围查询
4. 排序和限制
5. 多条件组合
6. 不同输出格式
"""

import requests
import json
import time
from typing import List, Dict, Any

# 配置
BASE_URL = "http://localhost:8000/mcp"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream"
}


class MCPTestClient:
    """MCP 测试客户端"""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session_id = None
        self.headers = HEADERS.copy()

    def initialize(self) -> bool:
        """初始化 MCP 会话"""
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
            resp = requests.post(self.base_url, headers=self.headers, json=init_request, timeout=60)
            self.session_id = resp.headers.get("Mcp-Session-Id")

            if self.session_id:
                self.headers["Mcp-Session-Id"] = self.session_id

                # 发送 initialized 通知
                initialized_notification = {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized"
                }
                requests.post(self.base_url, headers=self.headers, json=initialized_notification, timeout=10)
                print(f"✅ 会话初始化成功: {self.session_id[:16]}...")
                return True
            else:
                print("❌ 无法获取 Session ID")
                return False
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            return False

    def parse_sse_response(self, text: str) -> Dict[str, Any]:
        """解析 SSE 响应"""
        for line in text.split('\n'):
            if line.startswith('data: '):
                try:
                    return json.loads(line[6:])
                except:
                    pass
        return None

    def query(self, natural_language: str, format_type: str = "markdown") -> Dict[str, Any]:
        """执行自然语言查询

        Args:
            natural_language: 自然语言查询
            format_type: 输出格式 (markdown, json, csv)

        Returns:
            包含结果和元数据的字典
        """
        call_request = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "get_data",
                "arguments": {
                    "query": natural_language,
                    "format": format_type
                }
            },
            "id": 2
        }

        try:
            start_time = time.time()
            resp = requests.post(self.base_url, headers=self.headers, json=call_request, timeout=300)
            elapsed_time = time.time() - start_time

            result = self.parse_sse_response(resp.text)

            return {
                "success": result is not None and "result" in result,
                "status_code": resp.status_code,
                "elapsed_time": elapsed_time,
                "response": result,
                "raw_text": resp.text[:1000] if result is None else None
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "elapsed_time": time.time() - start_time
            }


# 测试用例定义
TEST_CASES = [
    # 系统级跨库查询
    {
        "category": "系统级跨库查询",
        "query": "查询最近5分钟的平均CPU使用率，并显示最近的一条运行日志内容",
        "description": "跨库查询测试 - 同时获取指标和日志"
    },
    # 基础查询
    {
        "category": "基础查询",
        "query": "查询所有表",
        "description": "最基本的查询 - 获取所有表"
    },
    {
        "category": "基础查询",
        "query": "显示表结构",
        "description": "查看数据库表结构"
    },

    # 简单数据查询
    {
        "category": "数据查询",
        "query": "查询前10条数据",
        "description": "获取前10条记录"
    },
    {
        "category": "数据查询",
        "query": "查询所有数据",
        "description": "获取所有数据（可能很多）"
    },

    # 时间相关查询
    {
        "category": "时间查询",
        "query": "最近5分钟CPU使用率是多少",
        "description": "时间范围查询 - 最近5分钟"
    },
    {
        "category": "时间查询",
        "query": "查询今天的所有数据",
        "description": "时间范围查询 - 今天"
    },
    {
        "category": "时间查询",
        "query": "最近一小时的平均内存使用率",
        "description": "时间范围 + 聚合函数"
    },
    {
        "category": "时间查询",
        "query": "过去24小时的数据趋势",
        "description": "长时间范围查询"
    },

    # 聚合查询
    {
        "category": "聚合查询",
        "query": "统计每台服务器的平均CPU使用率",
        "description": "GROUP BY + AVG"
    },
    {
        "category": "聚合查询",
        "query": "计算总内存使用量",
        "description": "SUM 聚合"
    },
    {
        "category": "聚合查询",
        "query": "每个主机的数据条数",
        "description": "COUNT + GROUP BY"
    },
    {
        "category": "聚合查询",
        "query": "最大值和最小值",
        "description": "MAX/MIN 聚合"
    },

    # 条件查询
    {
        "category": "条件查询",
        "query": "CPU使用率大于80的数据",
        "description": "简单条件过滤"
    },
    {
        "category": "条件查询",
        "query": "内存使用率超过90%且CPU使用率超过50%的记录",
        "description": "多条件 AND 查询"
    },
    {
        "category": "条件查询",
        "query": "主机名是 server01 或 server02 的数据",
        "description": "OR 条件查询"
    },

    # 排序和限制
    {
        "category": "排序查询",
        "query": "按CPU使用率从高到低排序",
        "description": "ORDER BY DESC"
    },
    {
        "category": "排序查询",
        "query": "最新的10条记录",
        "description": "ORDER BY time DESC + LIMIT"
    },
    {
        "category": "排序查询",
        "query": "按内存使用率升序排列的前5条",
        "description": "ORDER BY ASC + LIMIT"
    },

    # 复杂查询
    {
        "category": "复杂查询",
        "query": "每台主机最近10分钟的CPU和内存平均值",
        "description": "GROUP BY + 时间范围 + 多字段聚合"
    },
    {
        "category": "复杂查询",
        "query": "找出CPU使用率最高的3台主机",
        "description": "聚合 + 排序 + LIMIT"
    },
    {
        "category": "复杂查询",
        "query": "过去一小时每5分钟的统计",
        "description": "时间分组统计"
    },

    # 边界测试
    {
        "category": "边界测试",
        "query": "",
        "description": "空查询（应该失败）"
    },
    {
        "category": "边界测试",
        "query": "   ",
        "description": "只有空格（应该失败）"
    },
    {
        "category": "边界测试",
        "query": "SELECT * FROM users; DROP TABLE users; --",
        "description": "SQL 注入攻击（应该被拦截）"
    },

    # 不同格式测试
    {
        "category": "格式测试",
        "query": "查询前5条数据",
        "description": "测试 Markdown 格式",
        "format": "markdown"
    },
    {
        "category": "格式测试",
        "query": "查询前5条数据",
        "description": "测试 JSON 格式",
        "format": "json"
    },
    {
        "category": "格式测试",
        "query": "查询前5条数据",
        "description": "测试 CSV 格式",
        "format": "csv"
    },
]


def run_tests():
    """运行所有测试"""
    print("="*80)
    print("XiYan MCP Server 自然语言查询综合测试")
    print("="*80)

    # 初始化客户端
    client = MCPTestClient(BASE_URL)
    if not client.initialize():
        print("\n❌ 客户端初始化失败，退出测试")
        return

    print(f"\n📋 共有 {len(TEST_CASES)} 个测试用例\n")

    # 统计结果
    results = {
        "total": len(TEST_CASES),
        "success": 0,
        "failed": 0,
        "errors": 0,
        "by_category": {}
    }

    # 运行测试
    for i, test_case in enumerate(TEST_CASES, 1):
        category = test_case["category"]
        query = test_case["query"]
        description = test_case["description"]
        format_type = test_case.get("format", "markdown")

        # 打印测试信息
        print(f"\n{'─'*80}")
        print(f"[{i}/{results['total']}] {category} - {description}")
        print(f"{'─'*80}")
        print(f"查询: {query}")
        print(f"格式: {format_type}")

        # 初始化类别统计
        if category not in results["by_category"]:
            results["by_category"][category] = {"total": 0, "success": 0, "failed": 0}

        results["by_category"][category]["total"] += 1

        # 执行查询
        result = client.query(query, format_type)

        # 处理结果
        if result["success"]:
            results["success"] += 1
            results["by_category"][category]["success"] += 1

            response_data = result["response"].get("result", {})
            content_list = response_data.get("content", [])

            if content_list:
                for item in content_list:
                    if item.get("type") == "text":
                        text_content = item.get("text", "")
                        # 截断长内容
                        if len(text_content) > 500:
                            text_content = text_content[:500] + "\n... (内容已截断)"
                        print(f"\n✅ 成功 ({result['elapsed_time']:.2f}秒)")
                        print(f"结果:\n{text_content}")
            else:
                print(f"✅ 成功但没有返回内容")
        else:
            if "error" in result:
                results["errors"] += 1
                print(f"\n⚠️  错误: {result['error']}")
            else:
                results["failed"] += 1
                results["by_category"][category]["failed"] += 1

                print(f"\n❌ 失败 (HTTP {result.get('status_code', 'N/A')}, {result['elapsed_time']:.2f}秒)")
                if result.get("raw_text"):
                    print(f"原始响应: {result['raw_text']}")

        # 避免请求过快
        time.sleep(0.5)

    # 打印统计报告
    print(f"\n\n{'='*80}")
    print("测试统计报告")
    print(f"{'='*80}")

    print(f"\n总览:")
    print(f"  总计:     {results['total']} 个测试")
    print(f"  成功:     {results['success']} ✅")
    print(f"  失败:     {results['failed']} ❌")
    print(f"  错误:     {results['errors']} ⚠️")
    print(f"  成功率:   {results['success']/results['total']*100:.1f}%")

    print(f"\n按类别统计:")
    for category, stats in results["by_category"].items():
        success_rate = stats["success"] / stats["total"] * 100 if stats["total"] > 0 else 0
        print(f"  {category}:")
        print(f"    成功: {stats['success']}/{stats['total']} ({success_rate:.0f}%)")

    # 结论
    print(f"\n{'='*80}")
    if results["success"] == results["total"]:
        print("🎉 所有测试通过！")
    elif results["success"] > results["total"] * 0.8:
        print("✓ 大部分测试通过，少数失败可能是正常的（如边界测试）")
    else:
        print("⚠️  多个测试失败，需要检查配置和服务状态")
    print(f"{'='*80}\n")


def run_quick_test():
    """运行快速测试（仅测试几个简单查询）"""
    print("="*80)
    print("XiYan MCP Server 快速测试")
    print("="*80)

    client = MCPTestClient(BASE_URL)
    if not client.initialize():
        print("\n❌ 客户端初始化失败")
        return

    quick_tests = [
        "查询所有表",
        "最近5分钟CPU使用率",
        "查询前5条数据",
        "按CPU使用率从高到低排序，取前3条"
    ]

    print(f"\n📋 快速测试 ({len(quick_tests)} 个查询)\n")

    for i, query in enumerate(quick_tests, 1):
        print(f"\n[{i}/{len(quick_tests)}] {query}")
        print("─"*40)

        result = client.query(query)

        if result["success"]:
            response_data = result["response"].get("result", {})
            content_list = response_data.get("content", [])

            if content_list:
                for item in content_list:
                    if item.get("type") == "text":
                        text_content = item.get("text", "")
                        if len(text_content) > 300:
                            text_content = text_content[:300] + "\n... (已截断)"
                        print(f"✅ ({result['elapsed_time']:.2f}秒)\n{text_content}")
        else:
            print(f"❌ {result.get('error', '未知错误')}")

        time.sleep(0.5)

    print(f"\n{'='*80}\n")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        # 快速测试模式
        run_quick_test()
    else:
        # 完整测试模式
        run_tests()
