#!/usr/bin/env python3
"""
将本地 Parquet 文件上传到 HDFS

使用方法:
    # 上传单个文件
    python scripts/upload_parquet_to_hdfs.py --file query_results_parquet/result.parquet

    # 上传整个目录
    python scripts/upload_parquet_to_hdfs.py --dir query_results_parquet/

    # 指定 HDFS 路径
    python scripts/upload_parquet_to_hdfs.py --dir query_results_parquet/ --hdfs-path /data/xiyan_mcp_server
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


def create_spark_session():
    """创建 Spark 会话"""
    spark = SparkSession.builder \
        .appName("UploadParquetToHDFS") \
        .config("spark.driver.memory", "1g") \
        .config("spark.hadoop.fs.defaultFS", "hdfs://proxy.huabase.net:8020") \
        .getOrCreate()

    return spark


def upload_single_file(spark, local_path: Path, hdfs_path: str):
    """上传单个 Parquet 文件到 HDFS"""
    print(f"\n{'='*60}")
    print(f"上传文件: {local_path.name}")
    print(f"{'='*60}")

    try:
        # 读取本地 Parquet 文件
        print(f"📖 读取本地文件...")
        df = spark.read.parquet(str(local_path))
        row_count = df.count()
        print(f"   行数: {row_count:,}")
        print(f"   列数: {len(df.columns)}")
        print(f"   列名: {', '.join(df.columns[:5])}{'...' if len(df.columns) > 5 else ''}")

        # 写入 HDFS
        print(f"\n📤 上传到 HDFS: {hdfs_path}")
        df.write.mode("overwrite").parquet(hdfs_path)

        print(f"✓ 上传成功!")
        return True

    except Exception as e:
        print(f"✗ 上传失败: {e}")
        return False


def upload_directory(spark, local_dir: Path, hdfs_base_path: str, pattern: str = "*.parquet"):
    """批量上传目录下的 Parquet 文件"""
    parquet_files = list(local_dir.glob(pattern))

    if not parquet_files:
        print(f"⚠ 未找到 Parquet 文件在 {local_dir}")
        return

    print(f"\n{'='*60}")
    print(f"批量上传模式")
    print(f"{'='*60}")
    print(f"源目录: {local_dir}")
    print(f"目标路径: {hdfs_base_path}")
    print(f"文件数量: {len(parquet_files)}")

    success_count = 0
    fail_count = 0

    for idx, local_file in enumerate(parquet_files, 1):
        print(f"\n[{idx}/{len(parquet_files)}]", end=" ")
        hdfs_path = f"{hdfs_base_path}/{local_file.stem}"

        if upload_single_file(spark, local_file, hdfs_path):
            success_count += 1
        else:
            fail_count += 1

    # 统计
    print(f"\n{'='*60}")
    print(f"上传完成!")
    print(f"{'='*60}")
    print(f"✓ 成功: {success_count}")
    print(f"✗ 失败: {fail_count}")
    print(f"总计: {len(parquet_files)}")


def verify_upload(spark, hdfs_path: str):
    """验证 HDFS 中的文件"""
    print(f"\n{'='*60}")
    print(f"验证 HDFS 文件: {hdfs_path}")
    print(f"{'='*60}")

    try:
        # 列出 HDFS 目录
        from pyspark.sql import DataFrame

        # 尝试读取验证
        df = spark.read.parquet(hdfs_path)
        count = df.count()
        print(f"✓ 验证成功")
        print(f"  行数: {count:,}")
        print(f"  列数: {len(df.columns)}")
        return True

    except Exception as e:
        print(f"✗ 验证失败: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description='将本地 Parquet 文件上传到 HDFS',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 上传单个文件
  python %(prog)s --file query_results_parquet/result.parquet

  # 上传整个目录
  python %(prog)s --dir query_results_parquet/

  # 指定 HDFS 路径
  python %(prog)s --dir query_results_parquet/ --hdfs-path /data/xiyan_mcp_server

  # 使用 Spark-submit 运行（推荐用于生产环境）
  /opt/spark/bin/spark-submit \\
    --master local[*] \\
    --deploy-mode client \\
    --conf spark.driver.memory=1g \\
    %(prog)s --dir query_results_parquet/
        """
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
        '--hdfs-path',
        type=str,
        default='/data/xiyan_mcp_server',
        help='HDFS 目标路径（默认: /data/xiyan_mcp_server）'
    )
    parser.add_argument(
        '--verify',
        action='store_true',
        help='上传后验证文件'
    )
    parser.add_argument(
        '--namenode',
        type=str,
        default='hdfs://proxy.huabase.net:8020',
        help='HDFS NameNode 地址'
    )

    args = parser.parse_args()

    # 参数验证
    if not args.file and not args.dir:
        parser.error("必须指定 --file 或 --dir 参数")

    if args.file and args.dir:
        parser.error("--file 和 --dir 不能同时使用")

    print("=" * 60)
    print("   Parquet 文件上传到 HDFS 工具")
    print("=" * 60)
    print(f"NameNode: {args.namenode}")
    print(f"HDFS 路径: {args.hdfs_path}")

    # 创建 Spark 会话
    print("\n初始化 Spark...")
    spark = create_spark_session()
    print("✓ Spark 会话创建成功")

    try:
        # 上传单个文件
        if args.file:
            local_path = Path(args.file)
            if not local_path.exists():
                print(f"✗ 文件不存在: {args.file}")
                sys.exit(1)

            hdfs_path = f"{args.hdfs_path}/{local_path.stem}"
            upload_single_file(spark, local_path, hdfs_path)

            # 验证
            if args.verify:
                verify_upload(spark, hdfs_path)

        # 批量上传目录
        else:
            local_dir = Path(args.dir)
            if not local_dir.exists():
                print(f"✗ 目录不存在: {args.dir}")
                sys.exit(1)

            upload_directory(spark, local_dir, args.hdfs_path)

    finally:
        spark.stop()
        print("\n✓ Spark 会话已关闭")


if __name__ == '__main__':
    main()
