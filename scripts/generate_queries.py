#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
查询构造脚本 v2：聚焦复杂查询能力测试

跳过了简单单表查询（MCP 在 Spider/BIRD 上已验证），
重点生成多表关联、诊断推理、歧义NL等复杂度高的查询。

用法:
    source /root/.bashrc && python scripts/generate_queries.py
"""

import asyncio
import json
import random
import re
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from fastmcp.client import Client

SERVER_URL = os.environ.get("MCP_SERVER_URL", "http://localhost:8000/mcp")
OUTPUT_FILE = "queries.jsonl"

# 常用过滤值
TAG_VALUES = {
    "instance": ["172.19.19.128:8080", "172.19.19.129:8080", "172.19.19.130:8080"],
    "node_id": ["1", "2", "3", "4"],
}

TIME_RANGES = ["1小时", "6小时", "12小时", "24小时", "3天", "7天"]
RANGES = [5, 10, 20, 50, 100]


def pick(choices, n=1):
    if n == 1:
        return random.choice(choices)
    return random.sample(choices, min(n, len(choices)))


# ─────────────────────────────────────────
# Level 2: 复杂聚合（聚合统计 + 统计函数 + 条件聚合）
# ─────────────────────────────────────────

def gen_level2(tables: list) -> list:
    """20条：统计函数、条件聚合、分布分析"""
    queries = []
    templates = []

    # P50/P95/P99 分位数查询（LLM 难点）
    for _ in range(8):
        t = pick(tables)
        templates.append(f"查询表 {t} 中指标值的 P50、P95、P99 分位数")
        templates.append(f"统计表 {t} 中指标值的百分位分布")

    # 条件聚合（多条件组合）
    for _ in range(6):
        t = pick(tables)
        tag = pick(["instance", "node_id"])
        val = pick(TAG_VALUES[tag])
        interval = pick(["小时", "30分钟"])
        agg = pick(["平均值", "最大值", "总和"])
        templates.append(f"查询表 {t} 中 {tag}={val} 的记录，按每 {interval} 统计指标的 {agg}")

    # 标准差/方差
    for _ in range(6):
        t = pick(tables)
        templates.append(f"查询表 {t} 中指标值的标准差和方差，按 instance 分组")
        templates.append(f"分析表 {t} 中指标值的离散程度")

    for q in templates:
        queries.append({"query": q, "level": 2, "category": "complex_aggregation"})
    return queries[:20]


# ─────────────────────────────────────────
# Level 3: 复杂时序分析（趋势对比、异常检测、窗口函数）
# ─────────────────────────────────────────

def gen_level3(tables: list) -> list:
    """40条：时序对比、异常检测、增长率分析"""
    queries = []
    templates = []

    # 时段对比（今天vs昨天、本周vs上周）
    for _ in range(12):
        t = pick(tables)
        templates.append(f"对比表 {t} 中今天和昨天同一时段的指标值，找出变化超过20%的时间点")
        templates.append(f"分析表 {t} 中本周与上周的指标趋势差异")
        templates.append(f"查询表 {t} 中最近7天每天的最大值和最小值，对比波动幅度")

    # 异常检测
    for _ in range(10):
        t = pick(tables)
        templates.append(f"在表 {t} 中找出最近24小时内指标值超过均值2倍标准差的时间点")
        templates.append(f"检测表 {t} 中最近1小时的突刺，找出尖峰出现的时间")
        templates.append(f"分析表 {t} 最近6小时的数据，标记异常点（值超过P99阈值的记录）")

    # 增长率/变化率
    for _ in range(10):
        t = pick(tables)
        templates.append(f"查询表 {t} 中指标值按小时的增长速率")
        templates.append(f"计算表 {t} 中指标值相对于前一小时的环比变化率")
        templates.append(f"分析表 {t} 中最近12小时的增长趋势，判断是否有持续上升的倾向")

    # 按时间窗口聚合+排序
    for _ in range(8):
        t = pick(tables)
        templates.append(f"查询表 {t} 中按小时统计的平均值，找出平均值最高的3个小时段")
        templates.append(f"在表 {t} 中按天汇总，找出指标峰值最高的那一天")

    for q in templates:
        queries.append({"query": q, "level": 3, "category": "complex_time_series"})
    return queries[:40]


# ─────────────────────────────────────────
# Level 4: 多表关联 + 诊断推理（核心难度）
# ─────────────────────────────────────────

def gen_level4(tables: list) -> list:
    """120条：多表JOIN、跨指标关联分析、诊断场景"""
    queries = []
    templates = []

    # 按语义分组（从表名中提取关键词做分组）
    cpu_tables = [t for t in tables if any(k in t.lower() for k in ["cpu", "processor", "_sys_"])]
    mem_tables = [t for t in tables if any(k in t.lower() for k in ["mem", "memory", "alloc", "sys_bytes", "rss"])]
    latency_tables = [t for t in tables if any(k in t.lower() for k in ["latency", "lat", "delay", "duration"])]
    error_tables = [t for t in tables if any(k in t.lower() for k in ["error", "fail", "abort", "restart", "txn"])]
    sql_tables = [t for t in tables if any(k in t.lower() for k in ["sql", "query", "ddl", "dml"])]
    disk_tables = [t for t in tables if any(k in t.lower() for k in ["disk", "storage", "write", "read", "fsync", "wal", "compaction"])]
    raft_tables = [t for t in tables if any(k in t.lower() for k in ["raft", "replica", "rebalance"])]
    queue_tables = [t for t in tables if any(k in t.lower() for k in ["queue", "wait"])]

    groups = {
        "CPU": cpu_tables,
        "内存": mem_tables,
        "延迟": latency_tables,
        "错误": error_tables,
        "SQL": sql_tables,
        "磁盘": disk_tables,
        "Raft": raft_tables,
        "队列": queue_tables,
    }

    # 4a: 跨表对比（同节点多指标）—— 30条
    pairs_config = [
        ("CPU", "内存"), ("CPU", "延迟"), ("内存", "磁盘"),
        ("SQL", "延迟"), ("错误", "延迟"), ("Raft", "队列"),
        ("CPU", "磁盘"), ("内存", "SQL"), ("磁盘", "延迟"),
        ("错误", "SQL"),
    ]
    for g1_name, g2_name in pairs_config:
        g1 = groups.get(g1_name, [])
        g2 = groups.get(g2_name, [])
        if g1 and g2:
            for _ in range(3):
                t1, t2 = pick(g1), pick(g2)
                node = pick(TAG_VALUES["node_id"])
                templates.append(
                    f"在同一节点 node_id={node} 上，对比表 {t1} 和表 {t2} "
                    f"最近1小时的指标变化趋势，分析 {g1_name} 和 {g2_name} 之间是否存在关联"
                )
                templates.append(
                    f"查询节点 {node} 最近6小时的 {g1_name} 指标 ({t1}) 和 "
                    f"{g2_name} 指标 ({t2})，找出 {g2_name} 峰值时对应的 {g1_name} 值"
                )

    # 4b: 多表JOIN查询 —— 30条
    for _ in range(15):
        t1, t2 = pick(tables, 2)
        templates.append(f"将表 {t1} 和表 {t2} 按时间戳关联，查询同一时间段两个指标的对应关系")
        templates.append(f"JOIN 表 {t1} 和表 {t2}，比较相同 instance 下的指标差异")

    for _ in range(15):
        t1 = pick(tables)
        node = pick(TAG_VALUES["node_id"])
        # 找同节点的另一张表
        t2 = pick([t for t in tables if t != t1])
        templates.append(
            f"在节点 {node} 上同时查询表 {t1} 和表 {t2} 最近1小时的数据，"
            f"按时间对齐后对比两者的变化趋势"
        )

    # 4c: 诊断推理场景 —— 30条
    for _ in range(10):
        node = pick(TAG_VALUES["node_id"])
        if cpu_tables and mem_tables and latency_tables:
            tc = pick(cpu_tables)
            tm = pick(mem_tables)
            tl = pick(latency_tables)
            templates.append(
                f"诊断节点 {node} 的性能问题：同时查询 CPU指标({tc})、"
                f"内存指标({tm})、延迟指标({tl}) 最近1小时的数据，分析是否存在资源瓶颈"
            )

    for _ in range(10):
        node = pick(TAG_VALUES["node_id"])
        if error_tables and latency_tables:
            te = pick(error_tables)
            tl = pick(latency_tables)
            templates.append(
                f"节点 {node} 上错误指标 {te} 最近24小时有没有突然增多？"
                f"如果有，同时段延迟指标 {tl} 是否也同步恶化"
            )

    for _ in range(10):
        node = pick(TAG_VALUES["node_id"])
        tables_sample = pick(tables, 4)
        templates.append(
            f"综合分析节点 {node} 的运行状态，同时查看以下指标最近6小时的趋势："
            f"{'、'.join(tables_sample)}"
        )

    # 4d: 跨节点对比 —— 20条
    for _ in range(20):
        t = pick(tables)
        n1, n2 = pick(TAG_VALUES["node_id"], 2)
        templates.append(
            f"对比表 {t} 在节点 {n1} 和节点 {n2} 上最近1小时的指标差异，"
            f"判断哪个节点负载更高"
        )
        templates.append(
            f"查询表 {t} 在所有节点上最近6小时的平均值，排序找出负载最高的节点"
        )

    # 4e: 子查询/嵌套 —— 10条
    for _ in range(10):
        t = pick(tables)
        templates.append(f"在表 {t} 中找出指标值超过平均值的所有记录，按时间排序")
        templates.append(f"查询表 {t} 中指标值排名前5的 instance 分别是什么，以及它们对应的平均值")

    for q in templates:
        queries.append({"query": q, "level": 4, "category": "multi_table_diagnostic"})
    return queries[:120]


# ─────────────────────────────────────────
# Level 5: 自然语言 + 歧义 + 隐含意图
# ─────────────────────────────────────────

def gen_level5(tables: list) -> list:
    """80条：模糊NL、诊断推理、隐含意图"""
    queries = []
    templates = []

    # 按语义分组（复用 Level 4 的分组逻辑）
    cpu_tables = [t for t in tables if any(k in t.lower() for k in ["cpu", "processor", "_sys_"])]
    mem_tables = [t for t in tables if any(k in t.lower() for k in ["mem", "memory", "alloc"])]
    latency_tables = [t for t in tables if any(k in t.lower() for k in ["latency", "lat", "delay"])]
    error_tables = [t for t in tables if any(k in t.lower() for k in ["error", "fail", "abort", "restart"])]

    # 5a: 纯自然语言诊断 —— 25条
    diagnostic_queries = [
        "系统最近1小时运行正常吗？有哪些异常指标",
        "哪些节点目前负载最高？按CPU、内存、延迟分别排序",
        "最近6小时内有没有出现过性能抖动？从哪些指标能看出来",
        "帮我找出集群中可能存在问题的节点，需要看哪些指标",
        "分析一下当前系统的健康状态，包括CPU、内存、延迟、错误率",
        "最近一天内哪个时间段系统压力最大？为什么",
        "有没有节点出现了资源瓶颈？从CPU和内存的角度分析",
        "当前集群中有没有异常节点？描述一下判断依据",
        "最近24小时系统的整体趋势如何？有没有恶化的迹象",
        "对比各个节点，哪个节点的错误率最高？可能是什么原因",
        "查看系统最近有无突发负载，哪些指标能反映",
        "分析集群的读写延迟是否有恶化趋势",
        "节点之间的负载是否均衡？如果不均衡哪个节点最忙",
        "查看是否有正在排队的请求积压",
        "最近的Raft共识有没有异常，从哪些指标看",
        "分析磁盘I/O是否是当前系统的瓶颈",
        "最近有没有SQL执行超时的情况",
        "检查事务失败率是否在正常范围",
        "查看连接数是否接近上限",
        "分析内存使用是否在持续增长，是否存在泄漏风险",
        "最近有没有发生过OOM或者内存压力事件",
        "查看副本同步是否有延迟",
        "分析GC暂停是否影响了查询延迟",
        "检查租约续约是否有超时",
        "有没有热点的Range导致某个节点压力过大",
    ]
    for q in diagnostic_queries:
        templates.append(q)

    # 5b: 歧义查询 —— 15条
    if cpu_tables:
        templates.append(f"查询CPU使用率最高的5个节点")
        templates.append(f"CPU使用情况最近有什么变化")
    if mem_tables:
        templates.append(f"查看内存使用趋势")
        templates.append(f"哪些节点的内存使用接近上限")
    if latency_tables:
        templates.append(f"查询延迟情况")
        templates.append(f"延迟最近有没有变慢")
        templates.append(f"P99延迟是否在可接受范围")
    if error_tables:
        templates.append(f"查看错误日志")
        templates.append(f"最近有没有报错")
        templates.append(f"系统错误率是多少")
    templates.append("查看集群的整体性能指标")
    templates.append("分析最近一次性能下降的原因")
    templates.append("查看数据库的并发压力")
    templates.append("检查存储层的读写性能")
    templates.append("网络延迟是否正常")

    # 5c: 运维场景 —— 20条
    ops_queries = [
        "如果要扩容一个节点，从当前指标看应该扩哪个节点？依据是什么",
        "分析最近1小时，有没有慢查询拖累了整体性能",
        "查看是否有大事务阻塞了其他操作",
        "当前配置的缓存命中率是否合理",
        "有没有schema变更正在影响查询性能",
        "分析日志写入量是否突然增加导致磁盘压力",
        "检查副本数据是否一致",
        "有没有Range分裂/合并操作正在进行",
        "查看Compaction是否在影响前台读写",
        "分析是否有热点写入导致某个Store负载过高",
        "当前集群的QPS是否接近历史峰值",
        "有没有租约过期的Range",
        "查看MVCC垃圾回收是否及时",
        "分析事务重试率是否偏高",
        "有没有慢Raft提案导致提交延迟",
        "检查是否有Intent堆积",
        "查看SQL语句的计划缓存命中率",
        "有没有Range在频繁Rebalance",
        "分析是否有节点间网络分区导致的问题",
        "查看Changefeed是否有滞后",
    ]
    for q in ops_queries:
        templates.append(q)

    # 5d: 多步骤推理 —— 10条
    templates.append("先找出CPU使用率最高的3个节点，然后分别查询这些节点的内存和延迟指标，分析是否有资源瓶颈")
    templates.append("查询过去1小时错误率最高的节点，然后检查该节点的系统资源使用情况，最后判断是否需要告警")
    templates.append("对比所有节点的磁盘I/O，找出I/O最繁忙的节点，然后查看该节点的写放大是否偏高")
    templates.append("先查看整体的QPS趋势，然后分析QPS高峰时段各节点的延迟分布，找出性能瓶颈节点")
    templates.append("检查哪些SQL类型的延迟最高，然后查看对应时间段的系统资源，判断是资源不足还是SQL本身的问题")
    templates.append("先找出最近1小时出现异常的指标，然后追溯到对应的节点和时段，分析异常的可能原因")
    templates.append("查看Raft共识的相关指标，如果有异常，进一步检查网络延迟和磁盘写入，定位根因")
    templates.append("分析当前各节点的负载分布，如果不均衡，查看Rebalance相关的指标，判断是否需要干预")
    templates.append("查询最近24小时的事务失败率，按错误类型分类，然后分析失败率最高的时段对应的系统状态")
    templates.append("从集群级别、节点级别、Store级别逐层查看存储指标，找出最可能的存储瓶颈点")

    # 5e: 中英混合 —— 10条
    templates.append("show me which nodes have the highest P99 latency in the last hour")
    templates.append("找出过去6小时 error count 最多的3个 instance")
    templates.append("what's the current QPS trend compared to yesterday same time")
    templates.append("查询最近24小时 the ratio of failed transactions to total transactions")
    templates.append("find nodes where CPU usage exceeded 80% at any point in the last hour")
    templates.append("列出所有 instance 的 average query latency，按从高到低排序")
    templates.append("show me the top 5 hot ranges by QPS in the last 30 minutes")
    templates.append("当前有多少 active connections，是否接近 max_connections 限制")
    templates.append("compare the disk throughput between node-1 and node-2 in the last 6 hours")
    templates.append("查询哪些 SQL statements 占用了最多的 memory")

    for q in templates:
        queries.append({"query": q, "level": 5, "category": "natural_language_diagnostic"})
    return queries[:80]


# ─────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────

async def main():
    print(f"连接 MCP Server: {SERVER_URL}")
    async with Client(SERVER_URL, timeout=120) as client:
        print("获取 Schema ...")
        result = await client.read_resource("greptimedb:///public")
        text = result[0].text
        tables = re.findall(r'^# Table:\s+(\S+)', text, re.MULTILINE)
        short_tables = sorted(set(t.split('.')[-1] for t in tables))
        # 过滤超长表名（>35字符 LLM 易混淆）
        short_tables = [t for t in short_tables if len(t) <= 35]
        print(f"获取到 {len(short_tables)} 张表（已过滤超长表名）")

    random.seed(42)

    print("\n生成查询 ...")
    all_queries = []
    all_queries.extend(gen_level2(short_tables))
    all_queries.extend(gen_level3(short_tables))
    all_queries.extend(gen_level4(short_tables))
    all_queries.extend(gen_level5(short_tables))

    # 去重
    seen = set()
    unique = []
    for q in all_queries:
        if q["query"] not in seen:
            seen.add(q["query"])
            unique.append(q)

    # 写文件
    output_path = Path(OUTPUT_FILE)
    with open(output_path, "w", encoding="utf-8") as f:
        for i, q in enumerate(unique):
            rec = {"id": i + 1, "query": q["query"], "level": q["level"], "category": q["category"]}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 统计
    labels = {2: "复杂聚合", 3: "复杂时序", 4: "多表诊断", 5: "自然语言歧义"}
    print(f"\n写入 {len(unique)} 条查询到 {output_path.absolute()}")
    for lv in [2, 3, 4, 5]:
        count = sum(1 for q in unique if q["level"] == lv)
        print(f"  Level {lv} ({labels[lv]}): {count} 条")


if __name__ == "__main__":
    asyncio.run(main())
