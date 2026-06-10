#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
AI Judge 评估脚本：读取 query_tracker JSONL，用另一个 LLM 逐条评判 NL2SQL 正确性。

用法:
    source /root/.bashrc && python scripts/judge_queries.py
    python scripts/judge_queries.py --limit 50 --output judged.jsonl
"""

import asyncio
import json
import os
import sys
import argparse
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ============================================================
# AI Judge 核心 Prompt
# ============================================================

JUDGE_SYSTEM_PROMPT = """你是一个数据库 SQL 评审专家。你的任务是判断一个 NL2SQL 系统生成的 SQL 语句是否正确回答了用户的自然语言问题。

## 背景
这个系统服务于 CockroachDB 数据库运维场景。数据库中的表都是监控指标表，结构统一：
- greptime_timestamp: 时间戳
- greptime_value: 指标数值（这是核心查询字段）
- instance / node_id / job / store: 标签维度

表名格式为 cockroach_metrics.{指标名}，例如：
- cockroach_metrics.sys_cpu_usage (CPU使用率)
- cockroach_metrics.abortspanbytes (事务Span字节数)
- cockroach_metrics.sql_query_count_total (SQL查询总数)

## 评判标准

你需要根据以下三个维度给出综合判断：

### 1. 表选择是否正确
- NL问的是什么指标？SQL查的是不是对应的表？
- 如果使用了多选题无关的表 → 部分正确或错误

### 2. SQL逻辑是否正确
- SELECT的列是否符合问题要求？
- WHERE条件是否正确反映了筛选意图？
- 聚合函数(SUM/AVG/MAX/COUNT)是否使用正确？
- GROUP BY / ORDER BY 是否合理？
- LIMIT 是否符合要求的数量？
- 时间范围是否正确？

### 3. 是否回答了问题
- SQL执行成功，返回的数据能不能回答用户的问题？
- 执行失败的话，失败原因是什么？

## 输出格式

请严格按照以下JSON格式输出（不要输出其他内容）：

```json
{
  "judgment": "正确|部分正确|错误",
  "table_correct": true,
  "logic_correct": true,
  "answers_question": true,
  "reason": "一句话说明判断理由",
  "error_analysis": "如果错误，分析错误类型：表选错|列选错|聚合错误|条件错误|语法错误|多语句|其他"
}
```"""


def build_judge_user_prompt(record: dict) -> str:
    """根据追踪记录构建评判用的用户提示"""

    nl = record.get("nl_query", "")
    sql = record.get("initial_sql", "")
    fields = record.get("fields", [])
    preview = record.get("result_preview", [])
    exec_success = record.get("exec_success", False)
    exec_error = record.get("exec_error", "")
    retry_count = record.get("retry_count", 0)
    filtered_tables = record.get("filtered_table_names", [])
    relevant_schema = record.get("relevant_schema", "")

    # 构建上下文
    parts = []

    parts.append(f"## 用户自然语言问题\n{nl}")

    parts.append(f"## Schema过滤传给LLM的候选表（共{len(filtered_tables)}张）")
    if filtered_tables:
        parts.append(", ".join(filtered_tables[:10]))
    else:
        parts.append("（未启用schema过滤）")

    if relevant_schema:
        parts.append(f"## 候选表的结构定义\n{relevant_schema[:3000]}")

    parts.append(f"## LLM生成的SQL\n```sql\n{sql}\n```")

    parts.append(f"## 执行结果")
    parts.append(f"- 执行状态: {'成功' if exec_success else '失败'}")
    if exec_error:
        parts.append(f"- 错误信息: {exec_error[:500]}")
    if retry_count > 0:
        parts.append(f"- 修复重试次数: {retry_count}")

    if fields and preview:
        parts.append(f"- 返回列: {', '.join(fields)}")
        parts.append(f"- 数据预览(前3行):")
        for i, row in enumerate(preview[:3]):
            parts.append(f"  [{i+1}] " + " | ".join(f"{f}={v}" for f, v in zip(fields, row)))

    return "\n\n".join(parts)


async def judge_one(judge_model_url: str, judge_model_name: str, judge_api_key: str,
                     record: dict, timeout: int = 60) -> dict:
    """用 judge LLM 评判一条记录"""
    user_prompt = build_judge_user_prompt(record)

    # 先用 try 导入，失败则用子进程调用
    try:
        from xiyan_mcp_server.utils.llm_util import call_openai_sdk
    except Exception:
        call_openai_sdk = None

    if call_openai_sdk and judge_model_url:
        # 使用 SDK 调用
        import asyncio as _asyncio
        loop = _asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: call_openai_sdk(
                model=judge_model_name,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                key=judge_api_key,
                url=judge_model_url,
            )
        )
        raw = response.choices[0].message.content
    else:
        # Fallback: 用 openai 库直接调用
        from openai import OpenAI
        client = OpenAI(api_key=judge_api_key, base_url=judge_model_url)
        resp = client.chat.completions.create(
            model=judge_model_name,
            messages=[
                {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
            max_tokens=500,
        )
        raw = resp.choices[0].message.content

    # 解析 JSON
    try:
        # 提取 JSON 块
        json_match = re.search(r'\{[^}]+\}', raw, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = {"judgment": "未知", "reason": raw[:200]}
    except json.JSONDecodeError:
        result = {"judgment": "未知", "reason": raw[:200]}

    return result


async def main():
    parser = argparse.ArgumentParser(description="AI Judge 评估 NL2SQL 查询")
    parser.add_argument("--input", default="query_tracker_logs", help="追踪数据目录")
    parser.add_argument("--output", default="judged.jsonl", help="输出文件")
    parser.add_argument("--limit", type=int, default=0, help="最多评判条数(0=全部)")
    parser.add_argument("--delay", type=int, default=2, help="条间延迟")
    parser.add_argument("--timeout", type=int, default=60, help="单条超时")
    parser.add_argument("--judge-url", default=os.environ.get("JUDGE_API_URL", ""))
    parser.add_argument("--judge-key", default=os.environ.get("JUDGE_API_KEY", ""))
    parser.add_argument("--judge-model", default=os.environ.get("JUDGE_MODEL", "gpt-4o"))
    args = parser.parse_args()

    # 收集所有记录
    input_path = Path(args.input)
    records = []
    if input_path.is_dir():
        for f in sorted(input_path.glob("*.jsonl")):
            with open(f) as fh:
                buf, depth = "", 0
                for line in fh:
                    buf += line
                    depth += line.count('{') - line.count('}')
                    if depth == 0 and buf.strip():
                        records.append(json.loads(buf))
                        buf = ""
    else:
        with open(input_path) as fh:
            for line in fh:
                records.append(json.loads(line))

    if args.limit > 0:
        records = records[:args.limit]

    print(f"加载 {len(records)} 条记录")
    print(f"Judge模型: {args.judge_model}")

    # 如果没有配置 judge 模型，打印第一条的 prompt 预览
    if not args.judge_url:
        print("\n⚠ 未配置 --judge-url，将以预览模式运行。")
        print("设置环境变量启用AI评判:")
        print("  export JUDGE_API_URL=https://api.openai.com/v1")
        print("  export JUDGE_API_KEY=sk-xxx")
        print("  export JUDGE_MODEL=gpt-4o")
        print()

        if records:
            print("=" * 60)
            print("第一条记录的 Judge Prompt 预览:")
            print("=" * 60)
            print(f"\n## SYSTEM PROMPT ##\n{JUDGE_SYSTEM_PROMPT[:500]}...")
            print(f"\n## USER PROMPT ##\n{build_judge_user_prompt(records[0])[:2000]}")
        return

    # 执行评判
    stats = {"正确": 0, "部分正确": 0, "错误": 0, "未知": 0}
    output_path = Path(args.output)

    with open(output_path, "w", encoding="utf-8") as out:
        for i, rec in enumerate(records):
            print(f"[{i+1}/{len(records)}] {rec['nl_query'][:60]}...", end="", flush=True)

            try:
                judgment = await asyncio.wait_for(
                    judge_one(args.judge_url, args.judge_model, args.judge_key, rec, args.timeout),
                    timeout=args.timeout + 10
                )
            except asyncio.TimeoutError:
                judgment = {"judgment": "未知", "reason": "Judging timeout"}
            except Exception as e:
                judgment = {"judgment": "未知", "reason": str(e)[:200]}

            rec["judge_result"] = judgment
            j = judgment.get("judgment", "未知")
            stats[j] = stats.get(j, 0) + 1
            print(f" → {j}")

            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()

            if i < len(records) - 1:
                await asyncio.sleep(args.delay)

    print(f"\n{'='*60}")
    print(f"评判完成: {len(records)} 条")
    for k, v in stats.items():
        if v > 0:
            print(f"  {k}: {v} ({v/len(records)*100:.1f}%)")
    print(f"输出: {output_path.absolute()}")


if __name__ == "__main__":
    asyncio.run(main())
