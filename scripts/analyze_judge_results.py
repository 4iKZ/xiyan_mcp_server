#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
评价结果聚合分析脚本

读取 run_judge_batch.py 产出的 judge_*.jsonl，输出准确率表、错误类别分布、
分桶（按 stage1_top_n / stage2_top_m / stage2_model）的细分指标。

设计原则：
- **一个脚本干一件事**：不和 run_judge_batch.py 混在一起。该脚本只读不写
  judge_results，纯报表。
- 输出 markdown 友好的表格，方便直接贴到 STAGE2_TUNING_LOG / 论文草稿
- 可选 `--csv` 把分桶结果输出成宽表，给 pandas / Excel 二次分析
- 容错：judge_error 记录、缺字段记录都不会让脚本崩，只是单独计数

用法：
    # 1. 默认：在屏幕上打印综合报表
    python scripts/analyze_judge_results.py \
        --input judge_results/judge_2026-06-15.jsonl

    # 2. 同时落一份 CSV 给后续分析
    python scripts/analyze_judge_results.py \
        --input judge_results/judge_2026-06-15.jsonl \
        --csv   judge_results/bucket_metrics_2026-06-15.csv

    # 3. 只看某一种模型 / 某一种 stage2 配置
    python scripts/analyze_judge_results.py \
        --input ... --filter-model "deepseek-ai/DeepSeek-V4-Flash"

    # 4. 把错误样本导出来人工排查（按类别）
    python scripts/analyze_judge_results.py \
        --input ... --dump-errors schema_wrong,filter_wrong \
        --error-output judge_results/errors_schema_filter.jsonl
"""

import argparse
import csv
import json
import logging
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("analyze_judge")


# ─────────────────────────────────────────────
# I/O
# ─────────────────────────────────────────────
def iter_records(path: Path) -> Iterator[Dict]:
    """读 judge 输出文件（pretty-printed 多对象 JSON，每条带换行排版）。

    兼容旧的"一行一条 JSONL"格式：raw_decode 对单行紧凑 JSON 也能正常工作。
    """
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
            logger.warning(f"judge 文件解析失败 at offset {idx}: {e}")
            break


# ─────────────────────────────────────────────
# 桶键
# ─────────────────────────────────────────────
def bucket_key(record: Dict) -> Tuple:
    """
    决定一条记录归到哪个实验桶。
    维度：run_tag / stage2_enabled / stage1_top_n / stage2_top_m / stage2_model

    - run_tag 放在最前面，保证 batch_test 同一 tag 的记录聚到同一桶
    - None 实际上是 "system_failure"（被测系统未生成 SQL）的子桶，单独保留以便统计。
    """
    return (
        record.get("run_tag"),
        record.get("stage2_enabled"),
        record.get("stage1_top_n"),
        record.get("stage2_top_m"),
        record.get("stage2_model"),
    )


def bucket_label(key: Tuple) -> str:
    tag, s2_en, n, m, model = key
    tag_part = f"[{tag}] " if tag else ""
    if s2_en is None and n is None:
        return f"{tag_part}(system_failure / 未跑 stage2 标签)"
    if s2_en is False:
        return f"{tag_part}stage2=OFF, K={n}"
    model_short = (model or "?").split("/")[-1]
    return f"{tag_part}stage2=ON, N={n}, M={m}, model={model_short}"


# ─────────────────────────────────────────────
# 聚合
# ─────────────────────────────────────────────
def aggregate(records: List[Dict]) -> Dict:
    """返回整体 + 分桶的统计"""
    overall = {
        "total": 0,
        "correct": 0,
        "judge_error": 0,
        "system_failure": 0,
        "category_counts": Counter(),
        "exec_success_correct": 0,    # 执行成功 ∩ judge 判 correct
        "exec_success_total": 0,
        "latency_ms": [],
    }
    by_bucket: Dict[Tuple, Dict] = defaultdict(lambda: {
        "total": 0,
        "correct": 0,
        "judge_error": 0,
        "system_failure": 0,
        "category_counts": Counter(),
    })

    for r in records:
        overall["total"] += 1
        bk = bucket_key(r)
        bucket = by_bucket[bk]
        bucket["total"] += 1

        if r.get("exec_success"):
            overall["exec_success_total"] += 1

        verdict = r.get("judge")
        if verdict is None:
            # judge 调用失败
            overall["judge_error"] += 1
            bucket["judge_error"] += 1
            continue

        category = verdict.get("category", "other")
        correct = bool(verdict.get("correct", False))

        overall["category_counts"][category] += 1
        bucket["category_counts"][category] += 1

        if category == "system_failure":
            overall["system_failure"] += 1
            bucket["system_failure"] += 1

        if correct:
            overall["correct"] += 1
            bucket["correct"] += 1
            if r.get("exec_success"):
                overall["exec_success_correct"] += 1

        lat = verdict.get("judge_latency_ms")
        if isinstance(lat, (int, float)) and lat > 0:
            overall["latency_ms"].append(lat)

    return {"overall": overall, "by_bucket": dict(by_bucket)}


# ─────────────────────────────────────────────
# 报表打印
# ─────────────────────────────────────────────
def pct(n: int, d: int) -> str:
    return f"{n/d*100:.1f}%" if d else "—"


def fmt_overall(o: Dict) -> str:
    lines = []
    total = o["total"]
    lines.append("=" * 72)
    lines.append("整体统计")
    lines.append("=" * 72)
    lines.append(f"总记录数:          {total}")
    lines.append(f"judge 调用失败:    {o['judge_error']}  ({pct(o['judge_error'], total)})")
    lines.append(f"system_failure:    {o['system_failure']}  ({pct(o['system_failure'], total)})")
    lines.append(f"被测系统判 correct:{o['correct']}  ({pct(o['correct'], total)})")
    judged = total - o["judge_error"]
    lines.append(
        f"准确率 (judged):   {pct(o['correct'], judged)}  "
        f"(分母去掉 judge_error；含 system_failure)"
    )
    real_judged = judged - o["system_failure"]
    lines.append(
        f"准确率 (产出了SQL):{pct(o['correct'], real_judged)}  "
        f"(分母去掉 judge_error + system_failure)"
    )
    exec_t = o["exec_success_total"]
    lines.append(
        f"执行成功率:        {pct(exec_t, total)}  ({exec_t}/{total})"
    )
    lines.append(
        f"  其中被判 correct:{pct(o['exec_success_correct'], exec_t)}  "
        f"(执行成功但答非所问 = {exec_t - o['exec_success_correct']} 条)"
    )

    if o["latency_ms"]:
        lat = sorted(o["latency_ms"])
        n = len(lat)
        avg = sum(lat) / n
        p50 = lat[n // 2]
        p95 = lat[min(n - 1, int(n * 0.95))]
        lines.append("")
        lines.append("judge 延迟 (ms):")
        lines.append(
            f"  avg={avg:.0f}  p50={p50:.0f}  p95={p95:.0f}  "
            f"max={lat[-1]:.0f}  n={n}"
        )

    return "\n".join(lines)


# 类别在报表中的固定顺序（看起来稳定）
CATEGORY_ORDER = [
    "correct",
    "schema_wrong",
    "column_wrong",
    "aggregation_wrong",
    "filter_wrong",
    "syntax",
    "other",
    "system_failure",
]


def fmt_category_distribution(o: Dict) -> str:
    lines = []
    lines.append("")
    lines.append("=" * 72)
    lines.append("错误类别分布（整体）")
    lines.append("=" * 72)
    cat_counts = o["category_counts"]
    judged = sum(cat_counts.values())
    if not judged:
        return "\n".join(lines + ["（无可统计数据）"])

    lines.append(f"{'category':22s} {'count':>8s} {'pct':>8s}")
    lines.append("-" * 42)
    for cat in CATEGORY_ORDER:
        c = cat_counts.get(cat, 0)
        if c == 0 and cat not in ("correct",):
            continue
        lines.append(f"{cat:22s} {c:>8d} {pct(c, judged):>8s}")
    # 漏网的类别（不在枚举里但出现了）
    for cat, c in cat_counts.items():
        if cat not in CATEGORY_ORDER:
            lines.append(f"{cat:22s} {c:>8d} {pct(c, judged):>8s}")
    return "\n".join(lines)


def fmt_bucket_table(buckets: Dict[Tuple, Dict]) -> str:
    """每个 (N,M,model) 桶一行：total / correct / acc / 主要错类"""
    lines = []
    lines.append("")
    lines.append("=" * 100)
    lines.append("分桶统计（按 stage1_top_n / stage2_top_m / model）")
    lines.append("=" * 100)
    header = (
        f"{'bucket':52s} {'total':>6s} {'correct':>8s} {'acc':>6s} "
        f"{'sysfail':>8s} {'judge_err':>10s}"
    )
    lines.append(header)
    lines.append("-" * len(header))

    # 排序：先稳定按 tag、N、M 升序，None 桶塞最后
    def sortkey(item):
        k = item[0]
        tag, s2_en, n, m, _ = k
        return (tag or "", s2_en is None, n is None, n or 0, m or 0)

    for key, b in sorted(buckets.items(), key=sortkey):
        label = bucket_label(key)
        total = b["total"]
        correct = b["correct"]
        judge_err = b["judge_error"]
        sysfail = b["system_failure"]
        real_total = total - judge_err
        acc = pct(correct, real_total)
        lines.append(
            f"{label[:52]:52s} {total:>6d} {correct:>8d} {acc:>6s} "
            f"{sysfail:>8d} {judge_err:>10d}"
        )

    # 每个桶下面再展开错误类别分布
    lines.append("")
    lines.append("─" * 100)
    lines.append("每桶错误类别明细")
    lines.append("─" * 100)
    for key, b in sorted(buckets.items(), key=sortkey):
        if b["total"] == 0:
            continue
        lines.append(f"\n▸ {bucket_label(key)}  (total={b['total']})")
        cat_counts = b["category_counts"]
        judged = sum(cat_counts.values())
        if not judged:
            lines.append("    （无 verdict 记录）")
            continue
        for cat in CATEGORY_ORDER:
            c = cat_counts.get(cat, 0)
            if c == 0:
                continue
            lines.append(f"    {cat:22s} {c:>5d}  {pct(c, judged):>6s}")
        for cat, c in cat_counts.items():
            if cat not in CATEGORY_ORDER and c > 0:
                lines.append(f"    {cat:22s} {c:>5d}  {pct(c, judged):>6s}")
    return "\n".join(lines)


# ─────────────────────────────────────────────
# CSV 导出（宽表，方便论文图表用）
# ─────────────────────────────────────────────
def write_bucket_csv(buckets: Dict[Tuple, Dict], path: Path) -> None:
    fieldnames = [
        "run_tag",
        "stage2_enabled", "stage1_top_n", "stage2_top_m", "stage2_model",
        "total", "correct", "judge_error", "system_failure",
        "accuracy",
    ] + [f"cat_{c}" for c in CATEGORY_ORDER]

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for key, b in buckets.items():
            tag, s2_en, n, m, model = key
            total = b["total"]
            judge_err = b["judge_error"]
            real_total = total - judge_err
            row = {
                "run_tag": tag,
                "stage2_enabled": s2_en,
                "stage1_top_n": n,
                "stage2_top_m": m,
                "stage2_model": model,
                "total": total,
                "correct": b["correct"],
                "judge_error": judge_err,
                "system_failure": b["system_failure"],
                "accuracy": (
                    round(b["correct"] / real_total, 4) if real_total else ""
                ),
            }
            for c in CATEGORY_ORDER:
                row[f"cat_{c}"] = b["category_counts"].get(c, 0)
            w.writerow(row)
    logger.info(f"分桶 CSV 已写: {path}")


# ─────────────────────────────────────────────
# 错误样本导出
# ─────────────────────────────────────────────
def dump_error_samples(records: List[Dict], categories: List[str], path: Path) -> None:
    """把指定类别的错误记录导出成 pretty-printed JSON（每条带换行），供人工排查"""
    cats = set(c.strip() for c in categories if c.strip())
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            v = r.get("judge")
            if v is None:
                continue
            if v.get("category") in cats:
                f.write(json.dumps(r, ensure_ascii=False, indent=2) + "\n")
                n += 1
    logger.info(f"已导出 {n} 条错误样本（类别 {sorted(cats)}）到 {path}")


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="评价结果聚合分析")
    ap.add_argument("--input", required=True, help="judge_*.jsonl 路径")
    ap.add_argument(
        "--csv", default="",
        help="可选：分桶指标 CSV 输出路径",
    )
    ap.add_argument(
        "--filter-model", default="",
        help="可选：只看某个 stage2_model（如 deepseek-ai/DeepSeek-V4-Flash）",
    )
    ap.add_argument(
        "--filter-tool", default="",
        help="可选：只看某个 tool（如 get_data）",
    )
    ap.add_argument(
        "--filter-tag", default="",
        help="可选：只看某个 run_tag（如 's2on_n20m5'）；"
             "传 'none' 字面值则只看没有 run_tag 的旧记录。",
    )
    ap.add_argument(
        "--dump-errors", default="",
        help="可选：逗号分隔的错误类别（如 schema_wrong,filter_wrong），"
             "把这些类别的记录导出到 --error-output",
    )
    ap.add_argument(
        "--error-output", default="",
        help="dump-errors 的目标文件路径（默认在 input 同目录加 .errors.jsonl）",
    )
    args = ap.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error(f"输入不存在: {input_path}")
        sys.exit(1)

    # 读 + 过滤
    records = []
    for r in iter_records(input_path):
        if args.filter_model and r.get("stage2_model") != args.filter_model:
            continue
        if args.filter_tool and r.get("tool") != args.filter_tool:
            continue
        if args.filter_tag:
            want = args.filter_tag.strip().lower()
            actual = r.get("run_tag")
            if want == "none":
                if actual:
                    continue
            else:
                if actual != args.filter_tag:
                    continue
        records.append(r)

    if not records:
        logger.error("没有可分析的记录")
        sys.exit(1)
    logger.info(f"读入 {len(records)} 条记录（{input_path}）")

    # 聚合
    result = aggregate(records)
    print(fmt_overall(result["overall"]))
    print(fmt_category_distribution(result["overall"]))
    print(fmt_bucket_table(result["by_bucket"]))

    # CSV
    if args.csv:
        csv_path = Path(args.csv)
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        write_bucket_csv(result["by_bucket"], csv_path)

    # 错误样本导出
    if args.dump_errors:
        cats = args.dump_errors.split(",")
        if args.error_output:
            err_path = Path(args.error_output)
        else:
            err_path = input_path.with_suffix(".errors.jsonl")
        err_path.parent.mkdir(parents=True, exist_ok=True)
        dump_error_samples(records, cats, err_path)


if __name__ == "__main__":
    main()
