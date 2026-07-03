#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评价模型批量评估脚本

对 query_tracker 写出的 jsonl 记录逐条跑 judge，输出带 verdict 字段的新文件。

特性：
- **断点续跑**：output 文件里已有的 (nl_query, initial_sql) 组合自动跳过
- **并发**：默认 4 路并发调 judge 模型，串行 1500 条会过慢
- **失败重试**：429/超时 退避重试 3 次；解析失败标记 judge_error 继续下一条
- **完全离线**：不挂 MCP server；直接读 jsonl，写 jsonl
- **复用主流程组件**：Redis 客户端 / Embedding / SchemaRetriever 复用 server.py 单例逻辑

用法：
    # 1. 跑全量（第一次）
    PYTHONPATH=src python scripts/run_judge_batch.py \
        --input  query_tracker_logs/query_tracker_2026-06-15.jsonl \
        --output judge_results/judge_2026-06-15.jsonl

    # 2. 中断了同一条命令再来一次 → 自动跳过已评过的
    PYTHONPATH=src python scripts/run_judge_batch.py \
        --input  query_tracker_logs/query_tracker_2026-06-15.jsonl \
        --output judge_results/judge_2026-06-15.jsonl

    # 3. 小样本 smoke test（前 10 条）
    PYTHONPATH=src python scripts/run_judge_batch.py \
        --input  query_tracker_logs/query_tracker_2026-06-15.jsonl \
        --output judge_results/judge_smoke.jsonl \
        --limit 10

    # 4. 调大/调小并发
    PYTHONPATH=src python scripts/run_judge_batch.py \
        --input ... --output ... --concurrency 8

    # 5. 只评失败的记录（人工排查时）
    PYTHONPATH=src python scripts/run_judge_batch.py \
        --input ... --output ... --only-failed
"""

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

import yaml

# 让脚本能 import xiyan_mcp_server.*
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


# ─────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# judge 运行期间屏幕上只留进度行 + 最终汇总；底层组件 INFO 日志统统压成 WARNING，
# 避免每条 judge 都打一堆 embedding / Redis / schema 拼装日志。
# 仍能看到真正的报错（WARNING/ERROR），所以排查问题不受影响。
for _name in (
    "xiyan_mcp_server.utils.judge",
    "xiyan_mcp_server.utils.schema_retriever",
    "xiyan_mcp_server.utils.embedding_service",
    "xiyan_mcp_server.utils.greptimedb_source",   # 「延迟加载表列信息 / 完成」
    "xiyan_mcp_server.utils.db_source",
    "xiyan_mcp_server.db_source",
    "xiyan_mcp_server.utils.db_mschema",
    "xiyan_mcp_server.utils.knowledge_indexer",
    "xiyan_mcp_server.utils.stage2_filter",
    "httpx",   # openai SDK 底层走 httpx，每个请求会打一行 INFO
    "openai",
):
    logging.getLogger(_name).setLevel(logging.WARNING)
logger = logging.getLogger("run_judge_batch")


# ─────────────────────────────────────────────
# tracker.jsonl 解析（pretty-printed JSON，不是一行一条）
# ─────────────────────────────────────────────
def parse_tracker_records(path: Path) -> Iterator[Dict]:
    """逐条解析 tracker 的 pretty-printed JSON 文件"""
    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        # 跳过空白
        while idx < n and text[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            yield obj
            idx = end
        except json.JSONDecodeError as e:
            logger.error(f"tracker 解析失败 at offset {idx}: {e}")
            break


def parse_judge_output_records(path: Path) -> Iterator[Dict]:
    """解析 judge output（pretty-printed 多对象 JSON，每条带换行排版）"""
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return
    decoder = json.JSONDecoder()
    idx = 0
    n = len(text)
    while idx < n:
        while idx < n and text[idx] in " \r\n\t":
            idx += 1
        if idx >= n:
            break
        try:
            obj, end = decoder.raw_decode(text, idx)
            yield obj
            idx = end
        except json.JSONDecodeError as e:
            logger.warning(f"judge output 解析失败 at offset {idx}: {e}")
            break


def record_key(record: Dict) -> Tuple[str, str]:
    """用 (nl_query, initial_sql) 作为唯一键标识一条 tracker 记录"""
    return (record.get("nl_query", ""), record.get("initial_sql", ""))


# ─────────────────────────────────────────────
# 精简记录：只保留分析必需的字段
# ─────────────────────────────────────────────
# 砍掉 tracker 里冗长/无关字段（exec_error traceback / relevant_schema /
# retries / fields / result_preview 等），让 judge 输出文件小一个数量级，
# 也方便人工逐条 review。
SLIM_FIELDS = (
    "nl_query",
    "initial_sql",
    "tool",
    "run_tag",
    "dialect",
    "exec_success",
    "result_rows",
    "fields",
    "tables_used",
    "stage2_enabled",
    "stage1_top_n",
    "stage2_top_m",
    "stage2_model",
    "stage1_tables",
    "stage2_tables",
)


def slim_record(
    record: Dict,
    verdict: Optional[Dict],
    input_index: int,
    judge_error: Optional[str] = None,
) -> Dict:
    """从 tracker 记录里挑出分析必需的字段 + 附加 judge 结果。

    输出字段固定顺序，便于人眼扫读和后续 diff。
    """
    out = {"input_index": input_index}
    for k in SLIM_FIELDS:
        if k in record:
            out[k] = record[k]
    out["judge"] = verdict
    if judge_error is not None:
        out["judge_error"] = judge_error
    return out


# ─────────────────────────────────────────────
# 初始化主流程组件（Redis / Embedding / SchemaRetriever）
# ─────────────────────────────────────────────
def init_components(config: dict):
    """初始化 judge 端 RAG 所需的组件"""
    db_cfg = config["database"]
    system_prefix = db_cfg.get("system", "")
    redis_cfg = config.get("redis", {})
    embedding_cfg = config.get("embedding", {})
    schema_filter_cfg = config.get("schema_filter", {})

    # Redis
    import redis
    redis_client = redis.Redis(
        host=redis_cfg.get("host", "localhost"),
        port=redis_cfg.get("port", 6379),
        password=redis_cfg.get("password") or None,
        decode_responses=False,
    )
    redis_client.ping()
    logger.info("Redis 客户端就绪")

    # Embedding
    from xiyan_mcp_server.utils.embedding_service import EmbeddingService
    embedding_service = EmbeddingService(embedding_cfg)
    logger.info(f"Embedding 模型就绪: {embedding_cfg.get('model')}")

    # 数据库 → mschema（judge 端 build_sub_schema 时要用）
    from xiyan_mcp_server.utils.db_util import init_db_conn
    from xiyan_mcp_server.utils.db_config import DBConfig

    dialect = db_cfg.get("dialect", "mysql")
    if dialect.lower() == "sqlite":
        xiyan_db_cfg = DBConfig(dialect=dialect, db_path=db_cfg.get("db_path"))
    else:
        xiyan_db_cfg = DBConfig(
            dialect=dialect,
            db_name=db_cfg["database"],
            user_name=db_cfg["user"],
            db_pwd=db_cfg["password"],
            db_host=db_cfg["host"],
            port=db_cfg["port"],
        )
    db_engine = init_db_conn(xiyan_db_cfg)

    # 复用 server.py 里的 create_db_source 逻辑
    from xiyan_mcp_server.server import create_db_source
    db_source = create_db_source(
        db_engine, dialect, db_cfg.get("database", ""), system_prefix=system_prefix,
    )
    logger.info(f"db_source 就绪，可见表 {len(db_source.mschema.tables)} 张")

    # SchemaRetriever（单阶段配置，不挂 stage2_filter）
    base_idx = redis_cfg.get("index_name", "xiyan_schema")
    judge_suffix = "_judge"
    workspace_idx = (
        f"{base_idx}_{system_prefix.lower()}{judge_suffix}" if system_prefix else f"{base_idx}{judge_suffix}"
    )
    judge_top_k = config.get("judge", {}).get("judge_top_k", 30)
    retriever_cfg = {
        "index_name": workspace_idx,
        "top_k": judge_top_k,                      # 单阶段 K 直接走 judge_top_k
        "score_threshold": 0.0,                    # judge 端不要被阈值卡掉
        "stage2": {"enabled": False},              # 强制单阶段
    }
    from xiyan_mcp_server.utils.schema_retriever import SchemaRetriever
    retriever = SchemaRetriever(
        redis_client, embedding_service, db_source.mschema,
        retriever_cfg, db_source=db_source, stage2_filter=None,
    )
    logger.info(
        f"judge 端 SchemaRetriever 就绪: index={workspace_idx}, top_k={judge_top_k}"
    )

    # 加载知识库表描述，挂到 retriever 上供 judge 端 build_sub_schema(skip_lazy_load=True) 使用
    # ★ judge 必须用不变质的 noise0 版 KB，不受实验噪声切换影响。
    #    策略：logs / traces 知识文件无噪声变体，直接读活跃文件；
    #    metrics 知识文件会被 switch_cockroach_noise.sh 替换，故从 .noisebackup 固定读取 noise0。
    kb_dir = Path(config.get("schema_filter", {}).get("knowledge_dir", "json"))
    kb_descriptions: Dict[str, str] = {}
    kb_path = kb_dir / system_prefix.lower() if system_prefix else kb_dir

    def _load_kb_from_file(file_path: Path) -> int:
        """加载一个 JSON 知识文件到 kb_descriptions，返回加载的表数"""
        n = 0
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return 0  # 非知识库文件（如 schema_cache.json）
            for t in data:
                ec = t.get("embedding_content", "")
                if ec:
                    kb_descriptions[t["table_name"].lower()] = ec
                    n += 1
        except Exception as e:
            logger.warning(f"加载知识库文件失败 {file_path}: {e}")
        return n

    if kb_path.is_dir():
        metrics_loaded = 0
        other_loaded = 0
        for kf in sorted(kb_path.glob("*.json")):
            if "noise" in kf.name.lower():
                continue
            if kf.name == "cockroach_metrics_knowledge.json":
                continue  # 跳过活跃 metrics 文件（可能已被噪声污染）
            other_loaded += _load_kb_from_file(kf)

        # 从 noise0 备份强制加载 metrics 知识
        noise0_metrics = kb_path / ".noisebackup" / "cockroach_metrics_knowledge_noise0.json"
        if noise0_metrics.is_file():
            metrics_loaded = _load_kb_from_file(noise0_metrics)
        else:
            logger.warning(f"noise0 备份不存在: {noise0_metrics}，judge 将缺少 metrics 表描述")

        logger.info(
            f"知识库表描述已加载: {len(kb_descriptions)} 张表 "
            f"(metrics noise0: {metrics_loaded}, 其他: {other_loaded})"
        )
    retriever.kb_table_descriptions = kb_descriptions
    retriever.stage3_table_descriptions = dict(kb_descriptions)  # judge 端 stage3 也强制 noise0
    logger.info(f"知识库表描述已加载: {len(kb_descriptions)} 张表（已跳过噪声文件）")

    return retriever, db_cfg.get("database"), system_prefix


# ─────────────────────────────────────────────
# 单条评价 + 重试
# ─────────────────────────────────────────────
async def judge_one(
    record: Dict,
    input_index: int,
    judge_model,
    database: Optional[str],
    system_prefix: Optional[str],
    semaphore: asyncio.Semaphore,
    delay: float = 0.0,
    max_retries: int = 3,
) -> Dict:
    """
    评价单条记录，返回精简记录 + 'judge' 字段（成功）或 'judge_error' 字段（失败）。
    `input_index` 是该记录在输入 tracker 文件中的位置，用于最终按原顺序整理输出。
    """
    from xiyan_mcp_server.utils.judge import JudgeError

    async with semaphore:
        if delay > 0:
            await asyncio.sleep(delay)
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
                # 同步调用，但放进 to_thread 跑，让 asyncio 能并发多路
                verdict = await asyncio.to_thread(
                    judge_model.judge,
                    nl_query=nl,
                    generated_sql=sql,
                    exec_summary=exec_summary,
                    database=database,
                    system_prefix=system_prefix,
                    dialect=dialect,
                )
                return slim_record(record, verdict, input_index)
            except JudgeError as e:
                # 429 限流应退避重试；解析错误一般不重试
                err_str = str(e)
                if "RateLimitError" in err_str or "429" in err_str:
                    last_err = err_str
                    if attempt < max_retries - 1:
                        wait = 5 * (2 ** attempt)  # 5s → 10s → 20s
                        logger.warning(
                            f"judge 限流 (attempt {attempt+1}/{max_retries})，"
                            f"{wait}s 后重试: {nl[:40]}..."
                        )
                        await asyncio.sleep(wait)
                        continue
                    logger.error(
                        f"judge 限流重试耗尽: {nl[:40]}... err={err_str[:120]}"
                    )
                    break
                # 其他 JudgeError（JSON 解析等）直接停
                last_err = f"JudgeError: {e}"
                logger.warning(
                    f"judge 解析失败（不重试）: {nl[:40]}... err={e}"
                )
                break
            except Exception as e:
                # 网关/超时这类，退避重试
                last_err = f"{type(e).__name__}: {e}"
                if attempt < max_retries - 1:
                    wait = 2 ** attempt
                    logger.warning(
                        f"judge 调用失败 (attempt {attempt+1}/{max_retries})，"
                        f"{wait}s 后重试: {last_err[:80]}"
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"judge 调用最终失败: {nl[:40]}... err={last_err[:120]}"
                    )

        return slim_record(record, None, input_index, judge_error=last_err)


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
async def main_async(args):
    # 1. 加载配置
    with open(args.config, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    judge_cfg = config.get("judge", {}) or {}
    if not judge_cfg.get("api_url"):
        logger.error("config.yml 中 judge 节缺失或 api_url 未填，无法继续")
        sys.exit(1)

    # 2. 初始化主流程组件
    retriever, db_name, system_prefix = init_components(config)

    # 3. 初始化 JudgeModel
    from xiyan_mcp_server.utils.judge import JudgeModel
    judge_model = JudgeModel(judge_cfg, retriever=retriever)

    # 4. 读输入
    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        logger.error(f"输入文件不存在: {input_path}")
        sys.exit(1)

    # 5. 加载已完成的 key（断点续跑）
    done: Set[Tuple[str, str]] = set()
    for r in parse_judge_output_records(output_path):
        done.add(record_key(r))
    logger.info(f"已有 {len(done)} 条评价结果，将跳过")

    # 6. 收集待评记录（带过滤）
    #    pending 里每项是 (input_index, record)；input_index 是该记录在原始
    #    tracker 文件中的位置（1-based），用于最终按原顺序整理输出。
    pending: List[Tuple[int, Dict]] = []
    skipped_done = 0
    skipped_filter = 0
    skipped_empty_sql = 0
    for idx, r in enumerate(parse_tracker_records(input_path), start=1):
        if record_key(r) in done:
            skipped_done += 1
            continue
        if args.only_failed and r.get("exec_success", False):
            skipped_filter += 1
            continue
        # 跳过被测系统未产出 SQL 的记录（系统级失败，不需要 judge 模型评价）
        if not r.get("initial_sql", "").strip():
            skipped_empty_sql += 1
            # 直接落盘为 system_failure 精简记录
            sysfail_verdict = {
                "correct": False,
                "category": "system_failure",
                "reason": "被测系统未生成 SQL（initial_sql 为空），跳过 judge",
                "judge_model": None,
                "judge_top_k": None,
                "judge_latency_ms": 0,
            }
            r_out = slim_record(r, sysfail_verdict, idx)
            with open(output_path, "a", encoding="utf-8") as fout:
                fout.write(json.dumps(r_out, ensure_ascii=False) + "\n")
            continue
        pending.append((idx, r))
        if args.limit > 0 and len(pending) >= args.limit:
            break

    logger.info(
        f"准备评价 {len(pending)} 条（跳过已完成 {skipped_done}，"
        f"过滤掉 {skipped_filter}，系统级失败 {skipped_empty_sql}）"
    )
    if not pending:
        logger.info("没有需要评价的记录，退出")
        return

    # 7. 并发跑 judge，边完成边写盘
    semaphore = asyncio.Semaphore(args.concurrency)
    t_start = time.time()
    stats = {"ok": 0, "fail": 0, "correct": 0, "incorrect": 0}

    # 用 append 模式 + 标准 JSONL（一行一条）
    with open(output_path, "a", encoding="utf-8") as fout:
        tasks = [
            asyncio.create_task(
                judge_one(r, idx, judge_model, db_name, system_prefix, semaphore, delay=args.delay)
            )
            for idx, r in pending
        ]

        completed = 0
        for fut in asyncio.as_completed(tasks):
            result = await fut
            completed += 1

            if result.get("judge") is not None:
                stats["ok"] += 1
                v = result["judge"]
                if v.get("correct"):
                    stats["correct"] += 1
                else:
                    stats["incorrect"] += 1
                short = (
                    f"{'✓' if v['correct'] else '✗'} {v['category']:18s}"
                )
            else:
                stats["fail"] += 1
                short = f"! ERROR: {result.get('judge_error','')[:60]}"

            fout.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            fout.flush()

            elapsed = time.time() - t_start
            avg = elapsed / completed
            eta_min = avg * (len(pending) - completed) / 60
            nl_preview = result.get("nl_query", "")[:40]
            print(
                f"[{completed}/{len(pending)}] {short}  "
                f"({nl_preview}...)  ETA {eta_min:.1f}min",
                flush=True,
            )

    total = stats["ok"] + stats["fail"]
    elapsed_min = (time.time() - t_start) / 60
    acc = (stats["correct"] / stats["ok"] * 100) if stats["ok"] else 0.0
    print(f"\n{'='*60}")
    print(
        f"完成: 评价 {total} 条 | 成功 {stats['ok']} | 失败 {stats['fail']} "
        f"| 耗时 {elapsed_min:.1f}min"
    )
    print(
        f"  其中 correct=True: {stats['correct']} | correct=False: {stats['incorrect']} "
        f"| 准确率 {acc:.1f}% (分母=成功评价数)"
    )

    # 8. 最终整理：按 input_index 排序重写，让输出顺序和输入 tracker 一致
    #    增量阶段是 as_completed 乱序写（保证崩了不丢），这里做一次最终整理。
    if not args.no_sort:
        _sort_output_file(output_path)
        print("已按输入顺序整理输出文件")

    print(f"输出: {output_path.absolute()}")


def _sort_output_file(path: Path) -> None:
    """读全部记录、按 input_index 排序、重写。无 input_index 的记录排到末尾。"""
    records = list(parse_judge_output_records(path))
    if not records:
        return
    # 缺字段的兼容老文件，排到末尾
    records.sort(key=lambda r: r.get("input_index", 10**12))
    tmp = path.with_suffix(path.suffix + ".sorted.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)


def main():
    parser = argparse.ArgumentParser(description="评价模型批量评估")
    parser.add_argument(
        "--config",
        default="src/xiyan_mcp_server/config.yml",
        help="config.yml 路径",
    )
    parser.add_argument(
        "--input", required=True,
        help="tracker jsonl 文件（pretty-printed 多对象格式）",
    )
    parser.add_argument(
        "--output", required=True,
        help="评价结果输出（标准 JSONL，一行一条；自动断点续跑）",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4,
        help="并发数（默认 4；网关压力大时调低）",
    )
    parser.add_argument(
        "--limit", type=int, default=0,
        help="最多评几条（默认 0=全量；smoke test 时设 10）",
    )
    parser.add_argument(
        "--only-failed", action="store_true",
        help="只评 exec_success=False 的记录",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="请求间隔秒数（GLM 免费账户限流严，建议 10-15s；0=不间隔）",
    )
    parser.add_argument(
        "--no-sort", action="store_true",
        help="跑完不按 input_index 整理输出（默认会做最终排序，让输出顺序和 tracker 一致）",
    )
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
