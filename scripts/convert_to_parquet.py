#!/usr/bin/env python3
"""
将 JSON、CSV、Markdown 格式的查询结果转换为 Parquet 格式

使用方法:
    python scripts/convert_to_parquet.py --input query_results/ --output query_results_parquet/
"""

import argparse
import json
import re
import csv
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def convert_csv_to_parquet(csv_path: Path, output_path: Path) -> None:
    """将 CSV 文件转换为 Parquet 格式"""
    df = pd.read_csv(csv_path)
    parquet_path = output_path / f"{csv_path.stem}.parquet"
    df.to_parquet(parquet_path, index=False)
    print(f"✓ CSV -> Parquet: {csv_path.name} -> {parquet_path.name}")


def convert_json_to_parquet(json_path: Path, output_path: Path) -> None:
    """将 JSON 文件转换为 Parquet 格式

    支持两种 JSON 格式:
    1. 错误信息格式: {"error": "...", "sql": "..."}
    2. 数据数组格式: [{"col1": "val1", ...}, ...]
    """
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 判断是错误信息还是数据
    if isinstance(data, dict):
        # 错误信息格式 - 转换单行 DataFrame
        df = pd.DataFrame([data])
    elif isinstance(data, list):
        # 数据数组格式
        df = pd.DataFrame(data)
    else:
        print(f"⚠ 跳过不支持的 JSON 格式: {json_path.name}")
        return

    parquet_path = output_path / f"{json_path.stem}.parquet"
    df.to_parquet(parquet_path, index=False)
    print(f"✓ JSON -> Parquet: {json_path.name} -> {parquet_path.name}")


def convert_markdown_to_parquet(md_path: Path, output_path: Path) -> None:
    """将 Markdown 表格文件转换为 Parquet 格式"""
    content = md_path.read_text(encoding='utf-8')

    # 解析 Markdown 表格
    lines = content.strip().split('\n')

    # 查找表头行（包含 | 的行）
    table_lines = []
    in_table = False

    for line in lines:
        if '|' in line and line.strip():
            # 跳过分隔行（包含 ---）
            if '---' not in line:
                table_lines.append(line)
        elif table_lines:
            # 表格结束
            break

    if len(table_lines) < 2:
        print(f"⚠ 跳过无效的 Markdown 表格: {md_path.name}")
        return

    # 解析表头和数据
    headers = [cell.strip() for cell in table_lines[0].split('|')[1:-1]]
    rows = []

    for line in table_lines[1:]:
        cells = [cell.strip() for cell in line.split('|')[1:-1]]
        if len(cells) == len(headers):
            rows.append(dict(zip(headers, cells)))

    if not rows:
        print(f"⚠ 跳过空表格: {md_path.name}")
        return

    df = pd.DataFrame(rows)

    # 尝试转换数值列
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors='ignore')

    parquet_path = output_path / f"{md_path.stem}.parquet"
    df.to_parquet(parquet_path, index=False)
    print(f"✓ Markdown -> Parquet: {md_path.name} -> {parquet_path.name}")


def convert_directory(input_dir: Path, output_dir: Path, extensions: List[str]) -> None:
    """转换目录下所有指定格式的文件"""
    output_dir.mkdir(parents=True, exist_ok=True)

    files = []
    for ext in extensions:
        files.extend(input_dir.glob(f"*.{ext}"))

    if not files:
        print(f"⚠ 未找到 {' 或 '.join(extensions)} 文件在 {input_dir}")
        return

    print(f"\n📁 处理目录: {input_dir}")
    print(f"   找到 {len(files)} 个文件\n")

    for file_path in files:
        try:
            if file_path.suffix == '.csv':
                convert_csv_to_parquet(file_path, output_dir)
            elif file_path.suffix == '.json':
                convert_json_to_parquet(file_path, output_dir)
            elif file_path.suffix == '.md':
                convert_markdown_to_parquet(file_path, output_dir)
        except Exception as e:
            print(f"✗ 转换失败 {file_path.name}: {e}")


def main():
    parser = argparse.ArgumentParser(
        description='将 JSON、CSV、Markdown 格式的查询结果转换为 Parquet 格式'
    )
    parser.add_argument(
        '--input',
        type=str,
        default='query_results/',
        help='输入目录路径 (默认: query_results/)'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='query_results_parquet/',
        help='输出目录路径 (默认: query_results_parquet/)'
    )
    parser.add_argument(
        '--format',
        type=str,
        choices=['json', 'csv', 'md', 'all'],
        default='all',
        help='要转换的文件格式 (默认: all)'
    )

    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)

    if not input_path.exists():
        print(f"✗ 输入目录不存在: {input_path}")
        return

    format_map = {
        'json': ['json'],
        'csv': ['csv'],
        'md': ['md'],
        'all': ['json', 'csv', 'md']
    }

    extensions = format_map[args.format]

    print("=" * 50)
    print("🔄 查询结果转 Parquet 工具")
    print("=" * 50)

    convert_directory(input_path, output_path, extensions)

    print("\n" + "=" * 50)
    print("✓ 转换完成!")
    print(f"📂 Parquet 文件保存在: {output_path.resolve()}")
    print("=" * 50)


if __name__ == '__main__':
    main()
