#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
针对已完成的 batch 重跑 judge，**只**处理基础设施类失败记录（infra types）：
- timeout / connection_error / planner_error / permission_denied

目的：验证新 prompt + 新 category 生效后，57 条 infra 记录的 judge 行为变化。
输出写到独立文件（不污染原 judge output），方便对比 before/after。

用法：
    PYTHONPATH=src /usr/bin/python3 scripts/rerun_infra_judges.py \
        --input query_tracker_logs/query_tracker_2026-07-06_s2on_n20_m5_32b_1500_noise0_version3.jsonl \
        --output judge_results/judge_2026-07-06_s2on_n20_m5_32b_1500_noise0_version3.infra_rerun.jsonl \
        --concurrency 2
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for _name in (
    "xiyan_mcp_server.utils.judge",
    "xiyan_mcp_server.utils.schema_retriever",
    "xiyan_mcp_server.utils.embedding_service",
    "xiyan_mcp_server.utils.greptimedb_source",
    "xiyan_mcp_server.utils.db_source",
    "xiyan_mcp_server.db_source",
    "xiyan_mcp_server.utils.db_mschema",
    "xiyan_mcp_server.utils.knowledge_indexer",
    "xiyan_mcp_server.utils.stage2_filter",
    "httpx",
    "openai",
):
    logging.getLogger(_name).setLevel(logging.WARNING)
logger = logging.getLogger("rerun_infra_judges")


INFRA_ERROR_TYPES = {"timeout", "connection_error", "planner_error", "permission_denied"}


def parse_tracker_records(path: Path) -> List[Dict]:
    text = path.read_text(encoding="utf-8")
    dec = json.JSONDecoder(); idx = 0
    out = []
    while idx < len(text):
        while idx < len(text) and text[idx] in " \r\n\t":
            idx += 1
        if idx >= len(text):
            break
        obj, end = dec.raw_decode(text, idx)
        out.append(obj); idx = end
    return out


def slim_rerun(record: Dict, verdict, judge_error: str = None) -> Dict:
    """只输出对比必需的字段"""
    out = {
        "nl_query": record.get("nl_query", ""),
        "initial_sql": record.get("initial_sql", ""),
        "error_type": record.get("error_type"),
        "exec_error": (record.get("exec_error") or "")[:200],
        "dialect": record.get("dialect"),
        "level": record.get("level"),
    }
    if verdict is not None:
        out["verdict"] = verdict
    if judge_error is not None:
        out["judge_error"] = judge_error
    return out


async def judge_one(judge_model, record: Dict, semaphore, db_name, system_prefix, max_retries=3):
    from xiyan_mcp_server.utils.judge import JudgeError
    async with semaphore:
        nl = record.get("nl_query", "")
        sql = record.get("initial_sql", "")
        exec_summary = {
            "success": record.get("exec_success", False),
            "n_rows": record.get("result_rows", 0),
            "preview": record.get("result_preview"),
            "fields": record.get("fields"),
            "error": record.get("exec_error"),
            "error_type": record.get("error_type"),
        }
        dialect = record.get("dialect")
        last_err = None
        for attempt in range(max_retries):
            try:
                verdict = await asyncio.to_thread(
                    judge_model.judge,
                    nl_query=nl,
                    generated_sql=sql,
                    exec_summary=exec_summary,
                    database=db_name,
                    system_prefix=system_prefix,
                    dialect=dialect,
                )
                return {"ok": True, "verdict": verdict}
            except JudgeError as e:
                err_str = str(e)
                if "RateLimitError" in err_str or "429" in err_str:
                    if attempt < max_retries - 1:
                        wait = 5 * (2 ** attempt)
                        await asyncio.sleep(wait)
                        continue
                last_err = f"JudgeError: {e}"
                break
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < max_retries - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    break
        return {"ok": False, "judge_error": last_err}


async def main_async(args):
    import yaml
    from scripts.run_judge_batch import init_components
    from xiyan_mcp_server.utils.judge import JudgeModel

    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    judge_cfg = config.get("judge", {}) or {}
    if not judge_cfg.get("api_url"):
        logger.error("config.yml 中 judge.api_url 未配置")
        sys.exit(1)

    # 1. 初始化主流程组件
    retriever, db_name, system_prefix = init_components(config)

    # 2. 初始化 JudgeModel（用 config 默认 timeout）
    judge_model = JudgeModel(judge_cfg, retriever=retriever)
    logger.info(f"JudgeModel timeout={judge_model.timeout}s, db={db_name}, system={system_prefix}")

    # 3. 读取 tracker
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        logger.error(f"input 不存在: {input_path}")
        sys.exit(1)

    records = parse_tracker_records(input_path)
    infra_records = [r for r in records if (r.get("error_type") or "") in INFRA_ERROR_TYPES]
    logger.info(f"tracker 共 {len(records)} 条，其中 infra 失败 {len(infra_records)} 条")

    if not infra_records:
        logger.info("没有 infra 失败记录，退出")
        return

    if args.limit > 0:
        infra_records = infra_records[:args.limit]
        logger.info(f"--limit {args.limit}: 只跑前 {len(infra_records)} 条")

    # 4. 并发跑
    semaphore = asyncio.Semaphore(args.concurrency)
    tasks = [
        asyncio.create_task(judge_one(judge_model, r, semaphore, db_name, system_prefix))
        for r in infra_records
    ]
    results = await asyncio.gather(*tasks)

    # 5. 写盘
    ok = sum(1 for r in results if r["ok"])
    fail = sum(1 for r in results if not r["ok"])
    logger.info(f"judge 完成: 成功 {ok}, 仍失败 {fail}")

    with open(output_path, "w", encoding="utf-8") as f:
        for r, ret in zip(infra_records, results):
            if ret["ok"]:
                f.write(json.dumps(slim_rerun(r, ret["verdict"]), ensure_ascii=False, indent=2) + "\n")
            else:
                f.write(json.dumps(slim_rerun(r, None, judge_error=ret["judge_error"]),
                                   ensure_ascii=False, indent=2) + "\n")
    logger.info(f"输出: {output_path}")

    # 6. 汇总
    from collections import Counter
    cat = Counter()
    correct_count = 0
    for ret in results:
        if ret["ok"]:
            v = ret["verdict"]
            cat[v["category"]] += 1
            if v.get("correct"):
                correct_count += 1
    print(f"\n{'='*60}")
    print(f"infra rerun 汇总: 共 {len(infra_records)} 条 | judge 成功 {ok} | 失败 {fail}")
    print(f"  correct=True: {correct_count} | correct=False: {ok - correct_count}")
    print(f"\n  category 分布:")
    for k, v in cat.most_common():
        print(f"    {k:30s} {v:>3}  ({v/ok*100:>5.1f}%)")


def main():
    parser = argparse.ArgumentParser(description="重跑 infra 失败记录的 judge 评价")
    parser.add_argument("--config", default="src/xiyan_mcp_server/config.yml")
    parser.add_argument("--input", required=True, help="原 tracker jsonl")
    parser.add_argument("--output", required=True, help="infra rerun 输出文件")
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--limit", type=int, default=0,
                        help="最多跑几条（0=全量 63；smoke test 用 ~10）")
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
