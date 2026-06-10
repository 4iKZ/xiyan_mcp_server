#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量测试脚本：逐条执行 queries.jsonl 中的自然语言查询。
每条查询独立建立 MCP 连接，避免 session 超时断连。

用法:
    source /root/.bashrc && python scripts/batch_test_queries.py
    python scripts/batch_test_queries.py --start 50 --limit 20 --delay 10
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


async def run_one(query_id: int, nl_query: str, timeout: int, max_retries: int = 3) -> dict:
    """执行单条查询，独立连接，失败自动退避重试"""
    retryable_errors = ["session terminated", "not connected", "connection refused",
                         "timed out", "server disconnected", "server shutdown"]

    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            async with Client(SERVER_URL, timeout=timeout + 30) as client:
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
            if attempt < max_retries:
                wait = (attempt + 1) * 10
                await asyncio.sleep(wait)
                continue
            return {"id": query_id, "success": False, "elapsed_s": timeout, "text_preview": "超时"}
        except Exception as e:
            err = str(e).lower()
            retryable = any(kw in err for kw in retryable_errors)
            if retryable and attempt < max_retries:
                wait = (attempt + 1) * 8  # 8s, 16s, 24s 退避
                await asyncio.sleep(wait)
                continue
            elapsed = time.time() - t0
            return {"id": query_id, "success": False, "elapsed_s": round(elapsed, 1), "text_preview": f"{str(e)[:80]}"}


async def main():
    parser = argparse.ArgumentParser(description="批量测试 NL2SQL 查询")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--input", type=str, default="queries.jsonl")
    args = parser.parse_args()

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
    print(f"模式: 每查询独立连接 | 间隔 {args.delay}s | 超时 {args.timeout}s")
    print(f"{'='*60}")

    stats = {"ok": 0, "fail": 0}
    t_start = time.time()

    for i, q in enumerate(queries):
        qid = q["id"]
        nl = q["query"]
        lv = q.get("level", "?")

        eta = ""
        completed = stats["ok"] + stats["fail"]
        if completed > 0:
            avg = (time.time() - t_start) / completed
            remaining = avg * (len(queries) - completed)
            eta = f" | 剩余约 {remaining/60:.0f}min"

        print(f"[{i+1}/{len(queries)}] #{qid} Lv{lv} | {nl[:55]}...", end="", flush=True)

        result = await run_one(qid, nl, args.timeout)

        if result["success"]:
            stats["ok"] += 1
            print(f" \033[32m✓\033[0m ({result['elapsed_s']}s){eta}")
        else:
            stats["fail"] += 1
            print(f" \033[31m✗\033[0m {result['text_preview'][:50]} ({result['elapsed_s']}s){eta}")

        # 条间延迟
        if i < len(queries) - 1:
            await asyncio.sleep(args.delay)

    total = stats["ok"] + stats["fail"]
    elapsed_min = (time.time() - t_start) / 60
    print(f"\n{'='*60}")
    print(f"完成: {total} 条 | 成功 {stats['ok']} | 失败 {stats['fail']} | 耗时 {elapsed_min:.1f}min")
    if total > 0:
        print(f"成功率: {stats['ok']/total*100:.1f}%")
    print(f"追踪数据: {Path('query_tracker_logs').absolute()}/")


if __name__ == "__main__":
    asyncio.run(main())
