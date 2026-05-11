#!/usr/bin/env python3
"""
本地测试 HDFS 上传功能

模拟 HDFS 环境，将文件保存到本地 "hdfs_test/" 目录
"""
import argparse
import sys
from pathlib import Path

try:
    from pyspark.sql import SparkSession
except ImportError:
    print("错误: 未安装 PySpark")
    print("请安装: pip install pyspark")
    sys.exit(1)


def create_local_spark_session():
    """创建本地 Spark 会话（不连接 HDFS）"""
    spark = SparkSession.builder \
        .appName("LocalParquetTest") \
        .config("spark.driver.memory", "1g") \
        .master("local[*]") \
        .getOrCreate()

    return spark


def test_read_and_write_parquet(spark, input_file: str, output_dir: str):
    """测试读取和写入 Parquet 文件"""
    print(f"\n{'='*60}")
    print(f"测试文件: {input_file}")
    print(f"{'='*60}")

    input_path = Path(input_file)
    if not input_path.exists():
        print(f"✗ 文件不存在: {input_file}")
        return False

    try:
        # 读取 Parquet 文件
        print(f"\n📖 读取 Parquet 文件...")
        df = spark.read.parquet(str(input_path))

        row_count = df.count()
        col_count = len(df.columns)

        print(f"   ✓ 读取成功")
        print(f"   行数: {row_count:,}")
        print(f"   列数: {col_count}")
        print(f"   列名: {', '.join(df.columns)}")

        # 显示前几行
        print(f"\n📊 数据预览:")
        df.show(5, truncate=False)

        # 写入到输出目录
        output_path = Path(output_dir) / input_path.stem
        print(f"\n📤 写入到: {output_path}")

        df.write.mode("overwrite").parquet(str(output_path))

        print(f"   ✓ 写入成功")

        # 验证写入
        print(f"\n🔍 验证写入...")
        df_verify = spark.read.parquet(str(output_path))
        verify_count = df_verify.count()

        if verify_count == row_count:
            print(f"   ✓ 验证成功 (行数匹配: {verify_count})")
            return True
        else:
            print(f"   ✗ 验证失败 (行数不匹配: {verify_count} != {row_count})")
            return False

    except Exception as e:
        print(f"✗ 处理失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def batch_test_parquet_files(spark, input_dir: str, output_dir: str, limit: int = 3):
    """批量测试 Parquet 文件"""
    input_path = Path(input_dir)
    parquet_files = list(input_path.glob("*.parquet"))

    if not parquet_files:
        print(f"⚠ 未找到 Parquet 文件在 {input_dir}")
        return

    print(f"\n{'='*60}")
    print(f"批量测试模式")
    print(f"{'='*60}")
    print(f"源目录: {input_dir}")
    print(f"目标目录: {output_dir}")
    print(f"文件数量: {len(parquet_files)}")
    print(f"测试限制: {limit} 个文件")

    # 创建输出目录
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    # 限制测试数量
    test_files = parquet_files[:limit]

    success_count = 0
    fail_count = 0

    for idx, file_path in enumerate(test_files, 1):
        print(f"\n[{idx}/{len(test_files)}] {file_path.name}")
        if test_read_and_write_parquet(spark, str(file_path), output_dir):
            success_count += 1
        else:
            fail_count += 1

    # 统计
    print(f"\n{'='*60}")
    print(f"测试完成!")
    print(f"{'='*60}")
    print(f"✓ 成功: {success_count}")
    print(f"✗ 失败: {fail_count}")
    print(f"总计: {len(test_files)}")

    return success_count > 0


def main():
    parser = argparse.ArgumentParser(
        description='本地测试 Parquet 文件读写（模拟 HDFS）'
    )
    parser.add_argument(
        '--file',
        type=str,
        help='单个 Parquet 文件路径'
    )
    parser.add_argument(
        '--dir',
        type=str,
        default='query_results_parquet',
        help='Parquet 文件目录（默认: query_results_parquet）'
    )
    parser.add_argument(
        '--output',
        type=str,
        default='hdfs_test_output',
        help='输出目录（默认: hdfs_test_output）'
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=3,
        help='批量测试时的文件数量限制（默认: 3）'
    )

    args = parser.parse_args()

    print("=" * 60)
    print("   本地 Parquet 文件读写测试")
    print("   （模拟 HDFS 上传流程）")
    print("=" * 60)

    # 创建 Spark 会话
    print("\n初始化 Spark...")
    spark = create_local_spark_session()
    print("✓ Spark 会话创建成功")

    try:
        if args.file:
            # 测试单个文件
            success = test_read_and_write_parquet(
                spark,
                args.file,
                args.output
            )
            sys.exit(0 if success else 1)

        else:
            # 批量测试
            success = batch_test_parquet_files(
                spark,
                args.dir,
                args.output,
                args.limit
            )
            sys.exit(0 if success else 1)

    finally:
        spark.stop()
        print("\n✓ Spark 会话已关闭")


if __name__ == '__main__':
    main()
