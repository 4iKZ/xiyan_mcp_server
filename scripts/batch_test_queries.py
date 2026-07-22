#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
批量测试脚本：逐条执行 queries.jsonl 中的自然语言查询。
每条查询独立建立 MCP 连接，避免 session 超时断连。

用法:
    source /root/.bashrc && python scripts/batch_test_queries.py
    python scripts/batch_test_queries.py --start 50 --limit 20 --delay 10

二级筛选实验（覆盖 config.yml 默认值）:
    python scripts/batch_test_queries.py --input dataset/cockroach_nl2sql_1500.jsonl \
        --limit 5 --stage2

    python scripts/batch_test_queries.py --input dataset/cockroach_nl2sql_1500.jsonl \
        --limit 100 --stage2 --stage1-n 30 --stage2-m 3

    python scripts/batch_test_queries.py --input dataset/cockroach_nl2sql_1500.jsonl \
        --limit 100 --no-stage2
"""

import asyncio
import json
import logging
import os
import sys
import time
import argparse
from pathlib import Path

import httpx
from mcp.shared.exceptions import McpError

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastmcp.client import Client

SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")

logger = logging.getLogger("batch_test_queries")

# ── Reconnect supervisor helpers ────────────────────────────────────────────
SESSION_DEAD_KEYWORDS = (
    "server session was closed",
    "session terminated",
    "client failed to connect",
    "client is not connected",
    "all connection attempts failed",
    "connection refused",
    "connection reset",
    "broken pipe",
)


def is_session_dead_error(exc: BaseException) -> bool:
    """区分 session 死亡（需要 reconnect）和单 query 失败（重试即可）。

    Returns:
        True: session 死了，需要断开 client 重建（reconnect）
        False: 单 query 失败，run_one 内部 retry 即可，不需 reconnect
    """
    if isinstance(exc, httpx.HTTPStatusError):
        # 4xx 是客户端 query 错误，5xx 才是 server/session 问题
        return exc.response.status_code >= 500
    msg = str(exc).lower()
    return any(kw in msg for kw in SESSION_DEAD_KEYWORDS)


async def run_one(
    client,
    query_id: int,
    nl_query: str,
    timeout: int,
    stage2_enabled=None,
    stage1_n=None,
    stage2_m=None,
    run_tag=None,
    max_retries: int = 3,
) -> dict:
    retryable_errors = ["session terminated", "not connected", "connection refused",
                         "timed out", "server disconnected", "server shutdown"]

    tool_args = {"query": nl_query, "format": "markdown"}
    if stage2_enabled is not None:
        tool_args["stage2_enabled"] = stage2_enabled
    if stage1_n is not None:
        tool_args["stage1_n"] = stage1_n
    if stage2_m is not None:
        tool_args["stage2_m"] = stage2_m
    if run_tag:
        tool_args["run_tag"] = run_tag

    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            result = await asyncio.wait_for(
                client.call_tool("get_data", tool_args, timeout=timeout),
                timeout=timeout + 60
            )
            elapsed = time.time() - t0
            text = result.content[0].text if result.content else ""
            success = not (text.startswith("错误") or text.startswith("Error"))
            return {
                "id": query_id,
                "success": success,
                "elapsed_s": round(elapsed, 1),
                "text_preview": text[:1000],
            }
        except asyncio.TimeoutError:
            return {"id": query_id, "success": False, "elapsed_s": timeout, "text_preview": "超时"}
        except Exception as e:
            # session dead 不重试（重试也没用），直接抛给外层 reconnect
            if is_session_dead_error(e):
                raise
            err = str(e).lower()
            retryable = any(kw in err for kw in retryable_errors)
            if retryable and attempt < max_retries:
                wait = (attempt + 1) * 8
                await asyncio.sleep(wait)
                continue
            elapsed = time.time() - t0
            return {"id": query_id, "success": False, "elapsed_s": round(elapsed, 1), "text_preview": f"{str(e)[:1000]}"}


async def main():
    parser = argparse.ArgumentParser(description="批量测试 NL2SQL 查询")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--input", type=str, default="queries.jsonl")
    parser.add_argument("--config", type=str, default="src/xiyan_mcp_server/config.yml",
                        help="config.yml 路径（用于发送完成通知邮件）")
    s2_group = parser.add_mutually_exclusive_group()
    s2_group.add_argument("--stage2", dest="stage2", action="store_true",
                          help="本次跑启用二级筛选（覆盖 yml 默认）")
    s2_group.add_argument("--no-stage2", dest="stage2", action="store_false",
                          help="本次跑禁用二级筛选（baseline，覆盖 yml 默认）")
    parser.set_defaults(stage2=None)
    parser.add_argument("--stage1-n", type=int, default=None)
    parser.add_argument("--stage2-m", type=int, default=None)
    parser.add_argument("--tag", type=str, default=None,
                        help="实验运行标签；不传时按 CLI 参数自动派生。")
    args = parser.parse_args()

    if args.tag is None:
        if args.stage2 is False:
            args.tag = "s2off"
        else:
            parts = []
            parts.append("s2on" if args.stage2 else "s2default")
            if args.stage1_n is not None:
                parts.append(f"n{args.stage1_n}")
            if args.stage2_m is not None:
                parts.append(f"m{args.stage2_m}")
            args.tag = "_".join(parts)
    if args.tag == "":
        args.tag = None

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 找不到 {input_path}")
        sys.exit(1)

    queries = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            q = json.loads(line)
            if q["id"] >= args.start:
                queries.append(q)
            if args.limit > 0 and len(queries) >= args.limit:
                break

    print(f"加载 {len(queries)} 条查询 (从 #{args.start} 开始)")
    print(f"服务器: {SERVER_URL}")
    print(f"模式: 复用单连接 | 间隔 {args.delay}s | 超时 {args.timeout}s")
    print(f"stage2_enabled={args.stage2}  stage1_n={args.stage1_n}  stage2_m={args.stage2_m}  (None=沿用 yml)")
    print(f"run_tag={args.tag!r}")
    print(f"{'='*60}")

    stats = {"ok": 0, "fail": 0}
    t_start = time.time()

    consecutive_timeout = 0
    COOLDOWN_THRESHOLD = 3
    COOLDOWN_SECONDS = 120

    # ── Reconnect supervisor: 外层 while 跑 batch, 内层 try/except 检测 session dead ──
    MAX_RECONNECTS = 10

    stats = {"ok": 0, "fail": 0}
    t_start = time.time()

    consecutive_timeout = 0
    COOLDOWN_THRESHOLD = 3
    COOLDOWN_SECONDS = 120

    reconnect_count = 0
    start_idx = 0
    session_dead_exception = None  # 记录触发 reconnect 的异常

    while start_idx < len(queries):
        consecutive_timeout = 0  # 新 session 重置冷却计数
        session_dead_exception = None

        try:
            async with Client(SERVER_URL, timeout=args.timeout + 60) as client:
                for i in range(start_idx, len(queries)):
                    qid = queries[i]["id"]
                    nl = queries[i]["query"]
                    lv = queries[i].get("level", "?")

                    eta = ""
                    completed = stats["ok"] + stats["fail"]
                    if completed > 0:
                        avg = (time.time() - t_start) / completed
                        remaining = avg * (len(queries) - completed)
                        eta = f" | 剩余约 {remaining/60:.0f}min"

                    print(f"[{i+1}/{len(queries)}] #{qid} Lv{lv} | {nl[:55]}...", end="", flush=True)

                    try:
                        result = await run_one(
                            client,
                            qid, nl, args.timeout,
                            stage2_enabled=args.stage2,
                            stage1_n=args.stage1_n,
                            stage2_m=args.stage2_m,
                            run_tag=args.tag,
                        )

                        if result["success"]:
                            stats["ok"] += 1
                            consecutive_timeout = 0
                            print(f" \033[32m✓\033[0m ({result['elapsed_s']}s){eta}")
                        else:
                            preview = result.get("text_preview", "")
                            if "Client failed to connect" in preview or "connection to server" in preview:
                                print(f" \033[33m⚠\033[0m 连接错误，30s 后重试...  [{time.strftime('%H:%M:%S')}]",
                                      flush=True)
                                await asyncio.sleep(30)
                                result = await run_one(
                                    client,
                                    qid, nl, args.timeout,
                                    stage2_enabled=args.stage2,
                                    stage1_n=args.stage1_n,
                                    stage2_m=args.stage2_m,
                                    run_tag=args.tag,
                                )
                                if result["success"]:
                                    stats["ok"] += 1
                                    consecutive_timeout = 0
                                    print(f"  重试 \033[32m✓\033[0m ({result['elapsed_s']}s){eta}")
                                    if i < len(queries) - 1:
                                        await asyncio.sleep(args.delay)
                                    continue
                                preview = result.get("text_preview", "")
                            stats["fail"] += 1
                            print(f" \033[31m✗\033[0m {preview[:50]} ({result['elapsed_s']}s){eta}")

                            if "Timed out" in result.get("text_preview", ""):
                                consecutive_timeout += 1
                            else:
                                consecutive_timeout = 0

                        if consecutive_timeout >= COOLDOWN_THRESHOLD:
                            print(
                                f"\n⚠ 连续 {consecutive_timeout} 次超时，"
                                f"冷却 {COOLDOWN_SECONDS}s（{COOLDOWN_SECONDS/60:.0f}min）"
                                f"  [{time.strftime('%H:%M:%S')}]\n",
                                flush=True,
                            )
                            await asyncio.sleep(COOLDOWN_SECONDS)
                            consecutive_timeout = 0

                        if i < len(queries) - 1:
                            await asyncio.sleep(args.delay)

                    except (httpx.HTTPError, McpError, RuntimeError, ConnectionError, asyncio.CancelledError) as e:
                        # session dead → 跳出内层 for，触发外层 reconnect
                        if is_session_dead_error(e):
                            start_idx = i  # 重跑当前 query（不是下一条）
                            session_dead_exception = e
                            raise  # 跳出 async with，到外层 except
                        # 其他异常按单 query 失败处理
                        stats["fail"] += 1
                        print(f" \033[31m✗\033[0m {str(e)[:80]}{eta}")

                # for 循环正常结束 → 所有 query 完成
                break

        except (httpx.HTTPError, McpError, RuntimeError, ConnectionError, asyncio.CancelledError) as e:
            if not is_session_dead_error(e):
                # 非 session dead 的异常不应该到这里（内层已处理）
                raise
            reconnect_count += 1
            if reconnect_count > MAX_RECONNECTS:
                print(f"\n\033[31m✗\033[0m 超过最大重连次数 ({MAX_RECONNECTS})，退出。"
                      f"已完成 {stats['ok']} 条，失败 {stats['fail']} 条。")
                logger.error(f"batch abort: exceeded MAX_RECONNECTS={MAX_RECONNECTS}")
                break
            sleep_s = min(60, 2 ** reconnect_count)
            qid = queries[start_idx]["id"]
            exc_name = type(session_dead_exception or e).__name__
            exc_msg = str(session_dead_exception or e)
            print(f"\n\033[33m⚠\033[0m Session 中断（第 {reconnect_count}/{MAX_RECONNECTS} 次），"
                  f"{sleep_s}s 后从 #{qid} 续跑...  [{time.strftime('%H:%M:%S')}]")
            logger.warning(f"batch reconnect #{reconnect_count}/{MAX_RECONNECTS} "
                           f"after {sleep_s}s sleep: {exc_name}: {exc_msg[:200]}")
            await asyncio.sleep(sleep_s)
            # 外层 while 继续，start_idx 不变（保留断点）

    total = stats["ok"] + stats["fail"]
    elapsed_min = (time.time() - t_start) / 60
    print(f"\n{'='*60}")
    print(f"完成: {total} 条 | 成功 {stats['ok']} | 失败 {stats['fail']} | 耗时 {elapsed_min:.1f}min")
    if total > 0:
        print(f"成功率: {stats['ok']/total*100:.1f}%")
    print(f"重连次数: {reconnect_count}")
    print(f"追踪数据: {Path('query_tracker_logs').absolute()}/")

    # 发送完成通知邮件（失败不影响主流程）
    _status = (
        "success" if stats["fail"] == 0
        else "failed" if stats["ok"] == 0
        else "partial"
    )
    try:
        from xiyan_mcp_server.utils.mail_util import send_completion_email
        send_completion_email(
            script_name="batch_test_queries.py",
            status=_status,
            stats={
                "total": total,
                "ok": stats["ok"],
                "fail": stats["fail"],
                "success_rate": f"{stats['ok']/total*100:.1f}%" if total else "0%",
                "elapsed_min": f"{elapsed_min:.1f}",
                "input": str(input_path),
                "tag": args.tag,
            },
            config_path=args.config,
        )
    except Exception as e:
        print(f"[warn] 发送完成通知邮件失败: {e}")


if __name__ == "__main__":
    asyncio.run(main())
