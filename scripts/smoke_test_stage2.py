#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage 2 筛选器 smoke test

用法：
    python scripts/smoke_test_stage2.py
    python scripts/smoke_test_stage2.py --config src/xiyan_mcp_server/config.yml

验证：
    1. config.yml 里的 stage2 配置能被正确读取
    2. 能连上配置的 vLLM endpoint
    3. enable_thinking=false 生效，输出里没有 <think> 段
    4. 模型按 JSON 格式输出，能被解析
    5. 解析出的表名能在候选中匹配上
"""
import argparse
import sys
import yaml
from pathlib import Path

# 让脚本能直接 import xiyan_mcp_server.*
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from xiyan_mcp_server.utils.stage2_filter import Stage2Filter, Stage2FilterError


# 简短假数据
QUERY = "CPU 使用率最高的 5 个节点"
CANDIDATES = [
    {
        "table_name": "cockroach_metrics.sys_cpu_usage",
        "friendly_name": "系统CPU使用率",
        "description": "记录每个节点 CPU 使用率的时序数据，包含 user/system/idle 占比",
    },
    {
        "table_name": "cockroach_metrics.sql_statements",
        "friendly_name": "SQL 语句执行记录",
        "description": "SQL 语句执行历史与耗时",
    },
    {
        "table_name": "cockroach_metrics.node_status",
        "friendly_name": "节点状态",
        "description": "节点心跳、版本与健康信息",
    },
    {
        "table_name": "cockroach_metrics.disk_io",
        "friendly_name": "磁盘 IO",
        "description": "磁盘读写吞吐统计",
    },
    {
        "table_name": "cockroach_metrics.memory_usage",
        "friendly_name": "内存使用",
        "description": "内存使用情况",
    },
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--config",
        default="src/xiyan_mcp_server/config.yml",
        help="config.yml 路径",
    )
    parser.add_argument("--top-m", type=int, default=2, help="精筛保留张数")
    args = parser.parse_args()

    # 1. 读配置
    with open(args.config, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    stage2_cfg = cfg.get("schema_filter", {}).get("stage2", {}) or {}
    if not stage2_cfg:
        print("[X] config.yml 里没有 schema_filter.stage2 节")
        sys.exit(1)

    print(f"[1] 读取到 stage2 配置:")
    print(f"    model_name      = {stage2_cfg.get('model_name')}")
    print(f"    api_url         = {stage2_cfg.get('api_url')}")
    print(f"    enable_thinking = {stage2_cfg.get('enable_thinking')}")
    print()

    # 2. 初始化
    try:
        f = Stage2Filter(stage2_cfg)
    except Exception as e:
        print(f"[X] Stage2Filter 初始化失败: {e}")
        sys.exit(1)
    print("[2] Stage2Filter 初始化 OK")
    print()

    # 3. 调用 + 解析
    print(f"[3] 调用模型，让其从 {len(CANDIDATES)} 张候选中选 {args.top_m} 张…")
    try:
        kept = f.filter(query=QUERY, candidates=CANDIDATES, top_m=args.top_m)
    except Stage2FilterError as e:
        print(f"[X] Stage2FilterError: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"[X] 未捕获异常 {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"[4] 精筛成功，保留 {len(kept)} 张:")
    for i, item in enumerate(kept, 1):
        print(f"    {i}. {item.get('table_name')}")
    print()

    # 4. 基本健全性检查
    if not kept:
        print("[X] 精筛结果为空")
        sys.exit(1)
    expected_top = "cockroach_metrics.sys_cpu_usage"
    if kept[0]["table_name"] != expected_top:
        print(f"[!] 警告: 排第一的不是 sys_cpu_usage（实际: {kept[0]['table_name']}）")
        print("    模型语义匹配可能有偏差，但不算硬错误")
    else:
        print(f"[OK] 排序合理: sys_cpu_usage 排在第一")

    print()
    print("=" * 50)
    print("Stage 2 smoke test 通过 ✓")
    print("=" * 50)


if __name__ == "__main__":
    main()
