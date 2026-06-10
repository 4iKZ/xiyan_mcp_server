#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""SQL 查询执行追踪模块

记录每次 NL2SQL 查询的完整执行链路（生成 → 执行 → 修复），
以 JSONL 格式存储，用于准确率评估和错误模式分析。
"""

import datetime
import json
import threading
import os
from pathlib import Path
from typing import Dict, Any, Optional, List


class QueryTracker:
    """非侵入式查询追踪器，异步写入 JSONL 文件"""

    def __init__(self, output_dir: str = "/data/xiyan_mcp_server/query_tracker_logs"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._enabled = True

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = enabled

    def _write_record(self, record: Dict[str, Any]) -> None:
        """追加一条记录到当天的 JSONL 文件"""
        if not self._enabled:
            return
        try:
            date_str = datetime.date.today().isoformat()
            file_path = self.output_dir / f"query_tracker_{date_str}.jsonl"
            with self._lock:
                with open(file_path, 'a', encoding='utf-8') as f:
                    f.write(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
        except Exception:
            pass  # 追踪失败不影响主流程

    def record_query(
        self,
        nl_query: str,
        tool: str,
        database: str,
        dialect: str,
        initial_sql: str,
        exec_success: bool,
        exec_error: Optional[str],
        error_type: Optional[str],
        result_rows: int,
        result_preview: Optional[List[List]],
        retry_count: int,
        retries: List[Dict],
        total_latency_ms: float,
        tables_used: List[str],
        fields: List[str],
        schema_filtered: bool,
        available_table_count: int,
        relevant_schema: str,
        filtered_table_names: List[str],
        format_type: Optional[str] = None,
        hdfs_path: Optional[str] = None,
    ) -> None:
        record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "tool": tool,
            "nl_query": nl_query,
            "dialect": dialect,
            "database": database,
            "format": format_type,
            "hdfs_path": hdfs_path,
            # ── 问答核心 ──
            "initial_sql": initial_sql,
            "exec_success": exec_success,
            "exec_error": exec_error,
            "error_type": error_type,
            "result_rows": result_rows,
            "result_preview": result_preview[:5] if result_preview else None,
            "fields": fields,
            # ── 修复过程 ──
            "retry_count": retry_count,
            "retries": retries,
            # ── Schema 上下文（精简版）──
            "schema_filtered": schema_filtered,
            "available_table_count": available_table_count,   # 可见表数量
            "relevant_schema": relevant_schema,               # 仅 SQL 用到的那几张表的列定义
            "filtered_table_names": filtered_table_names,      # Schema 过滤传给 LLM 的候选表名列表
            # ── 性能 ──
            "tables_used": tables_used,
            "total_latency_ms": round(total_latency_ms, 2),
        }
        self._write_record(record)


def classify_error(error_message: str) -> str:
    """根据数据库错误信息自动分类错误类型"""
    if not error_message:
        return "unknown"
    msg = error_message.lower()
    if any(k in msg for k in ['column', '列', 'does not exist', 'unknown column']):
        return "column_not_found"
    if any(k in msg for k in ['table', '表', 'does not exist', 'relation']):
        return "table_not_found"
    if any(k in msg for k in ['syntax', '语法', 'parse', 'unexpected']):
        return "syntax_error"
    if any(k in msg for k in ['join', 'ambiguous']):
        return "join_error"
    if any(k in msg for k in ['type', '类型', 'cast', 'cannot']):
        return "type_error"
    if any(k in msg for k in ['function', '函数', 'does not exist']):
        return "function_not_found"
    if any(k in msg for k in ['timeout', '超时', 'timed out']):
        return "timeout"
    if any(k in msg for k in ['permission', '权限', 'denied', 'access']):
        return "permission_denied"
    if any(k in msg for k in ['connection', '连接', 'refused']):
        return "connection_error"
    return "other"


def extract_tables_from_sql(sql: str) -> List[str]:
    """从 SQL 中自动提取引用的表名"""
    import re
    if not sql:
        return []
    tables = set()
    patterns = [
        r'(?:FROM|JOIN|INTO|UPDATE)\s+([a-zA-Z_][a-zA-Z0-9_.]*)',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, sql, re.IGNORECASE)
        for m in matches:
            if m.upper() not in ('SELECT', 'WHERE', 'SET', 'VALUES', 'GROUP', 'ORDER', 'HAVING', 'LIMIT', 'ON', 'AS', 'AND', 'OR', 'NULL', 'NOT', 'IN', 'IS', 'LIKE', 'BETWEEN', 'EXISTS'):
                tables.add(m)
    return sorted(tables)


def extract_relevant_schema(full_schema: str, tables_used: List[str]) -> str:
    """从完整 schema 文本中只提取 SQL 实际引用的表的列定义

    Args:
        full_schema: 完整的 schema 文本（如 "# Table: xxx\n[...]\n"）
        tables_used: SQL 中引用的表名列表

    Returns:
        只包含相关表的 schema 片段
    """
    import re
    if not full_schema or not tables_used:
        return ""

    # 将表名标准化为短名（仅最后一段）用于模糊匹配
    short_names = set()
    for t in tables_used:
        short_names.add(t.split('.')[-1].lower())

    # 按 "# Table:" 分割，提取每个表块
    blocks = re.split(r'\n(?=# Table:)', full_schema)
    relevant_blocks = []

    for block in blocks:
        # 检查这个 block 是否匹配任意 used table
        block_lower = block.lower()
        for short in short_names:
            if short in block_lower:
                relevant_blocks.append(block)
                break

    return '\n'.join(relevant_blocks)


def count_available_tables(full_schema: str) -> int:
    """从完整 schema 文本中统计可见表数量"""
    import re
    if not full_schema:
        return 0
    matches = re.findall(r'^# Table:\s+(\S+)', full_schema, re.MULTILINE)
    return len(matches)


# 全局单例
_tracker: Optional[QueryTracker] = None
_tracker_lock = threading.Lock()


def get_query_tracker() -> QueryTracker:
    global _tracker
    if _tracker is None:
        with _tracker_lock:
            if _tracker is None:
                _tracker = QueryTracker()
    return _tracker
