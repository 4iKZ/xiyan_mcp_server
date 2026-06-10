#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量测试脚本：逐条执行 queries.jsonl 中的自然语言查询，
通过 MCP Server 调用 get_data 工具，结果自动进入 query_tracker。

用法:
    source /root/.bashrc && python scripts/batch_test_queries.py

参数:
    --start N      从第 N 条开始（默认 1）
    --limit N      最多执行 N 条（默认 0=全部）
    --delay N      每条间隔 N 秒（默认 5，避免 LLM 限流）
    --timeout N    单条超时 N 秒（默认 180）
    --input FILE   输入文件（默认 queries.jsonl）
"""

import asyncio
import json
import os
import sys
import time
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastmcp.client import Client

SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")


async def run_one(client: Client, query_id: int, nl_query: str, timeout: int) -> dict:
    """执行单条查询，返回结果摘要"""
    t0 = time.time()
    try:
        result = await asyncio.wait_for(
            client.call_tool("get_data", {
                "query": nl_query,
                "format": "markdown"
            }),
            timeout=timeout
        )
        elapsed = time.time() - t0
        text = result.content[0].text if result.content else ""
        success = not (text.startswith("错误") or text.startswith("Error"))
        return {
            "id": query_id,
            "success": success,
            "elapsed_s": round(elapsed, 1),
            "text_preview": text[:120],
        }
    except asyncio.TimeoutError:
        return {"id": query_id, "success": False, "elapsed_s": timeout, "text_preview": "超时"}
    except Exception as e:
        elapsed = time.time() - t0
        return {"id": query_id, "success": False, "elapsed_s": round(elapsed, 1), "text_preview": f"异常: {e}"}


async def main():
    parser = argparse.ArgumentParser(description="批量测试 NL2SQL 查询")
    parser.add_argument("--start", type=int, default=1, help="起始 ID")
    parser.add_argument("--limit", type=int, default=0, help="最大条数（0=全部）")
    parser.add_argument("--delay", type=int, default=5, help="条间延迟秒数")
    parser.add_argument("--timeout", type=int, default=180, help="单条超时秒数")
    parser.add_argument("--input", type=str, default="queries.jsonl", help="输入文件")
    args = parser.parse_args()

    # 读取查询
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 找不到 {input_path}")
        sys.exit(1)

    queries = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            q = json.loads(line)
            if q["id"] >= args.start:
                queries.append(q)
            if args.limit > 0 and len(queries) >= args.limit:
                break

    print(f"加载 {len(queries)} 条查询 (从 #{args.start} 开始)")
    print(f"服务器: {SERVER_URL}")
    print(f"条间延迟: {args.delay}s | 单条超时: {args.timeout}s")
    print(f"{'='*60}")

    stats = {"ok": 0, "fail": 0}
    t_start = time.time()
    last_ok_id = None

    async with Client(SERVER_URL, timeout=args.timeout + 30) as client:
        for i, q in enumerate(queries):
            qid = q["id"]
            nl = q["query"]
            lv = q.get("level", "?")

            # 进度
            eta = ""
            if i > 0 and stats["ok"] + stats["fail"] > 0:
                avg = (time.time() - t_start) / (i + 1)
                remaining = avg * (len(queries) - i - 1)
                eta = f" | 预计剩余 {remaining/60:.0f}min"

            print(f"[{i+1}/{len(queries)}] #{qid} Lv{lv} | {nl[:60]}...", end="", flush=True)

            result = await run_one(client, qid, nl, args.timeout)

            if result["success"]:
                stats["ok"] += 1
                last_ok_id = qid
                print(f" ✓ ({result['elapsed_s']}s){eta}")
            else:
                stats["fail"] += 1
                print(f" ✗ {result['text_preview'][:60]} ({result['elapsed_s']}s){eta}")

            # 条间延迟
            if i < len(queries) - 1:
                await asyncio.sleep(args.delay)

    total = stats["ok"] + stats["fail"]
    elapsed = (time.time() - t_start) / 60
    print(f"\n{'='*60}")
    print(f"完成: {total} 条 | 成功 {stats['ok']} | 失败 {stats['fail']} | 耗时 {elapsed:.1f}min")
    if total > 0:
        print(f"成功率: {stats['ok']/total*100:.1f}%")
    print(f"追踪数据: {Path('query_tracker_logs').absolute()}/")


if __name__ == "__main__":
    asyncio.run(main())
