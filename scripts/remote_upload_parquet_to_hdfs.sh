#!/bin/bash
#
# 在 cllm 机器 (117节点) 上将 Parquet 文件上传到 HDFS
#
# 使用方法:
#   1. 将此脚本和 parquet 文件复制到远程机器
#   2. 在远程机器上执行此脚本
#

# 配置
LOCAL_PARQUET_DIR="/tmp/xiyan_parquet"  # 本地 parquet 文件存放目录
HDFS_TARGET_PATH="/flatten_window_metrics"  # HDFS 目标路径
SPARK_SCRIPT_PATH="/opt/otel/scripts/upload_parquet_to_hdfs_spark.py"  # Spark 脚本路径

# 颜色
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo "================================================"
echo "   Parquet 文件上传到 HDFS (远程执行)"
echo "================================================"
echo ""

# 检查 Spark 是否可用
if [ ! -d "/opt/spark" ]; then
    echo -e "${RED}错误: 未找到 Spark 安装${NC}"
    echo "请确保在正确的机器上执行此脚本 (117 节点)"
    exit 1
fi

# 检查 parquet 文件
if [ ! -d "$LOCAL_PARQUET_DIR" ]; then
    echo -e "${RED}错误: Parquet 目录不存在: $LOCAL_PARQUET_DIR${NC}"
    echo "请先创建目录并放入 parquet 文件:"
    echo "  mkdir -p $LOCAL_PARQUET_DIR"
    echo "  # 将 parquet 文件复制到此目录"
    exit 1
fi

FILE_COUNT=$(ls -1 "$LOCAL_PARQUET_DIR"/*.parquet 2>/dev/null | wc -l)
if [ "$FILE_COUNT" -eq 0 ]; then
    echo -e "${RED}错误: 未找到 Parquet 文件${NC}"
    exit 1
fi

echo -e "${GREEN}找到 $FILE_COUNT 个 Parquet 文件${NC}"
echo "源目录: $LOCAL_PARQUET_DIR"
echo "目标: hdfs://proxy.huabase.net:8020$HDFS_TARGET_PATH"
echo ""

# 创建 Spark 上传脚本 (如果需要)
SPARK_PYTHON="/opt/otel/scripts/upload_parquet_to_hdfs_spark.py"

# 创建临时 Spark 脚本
cat > /tmp/upload_parquet_$$.py << 'EOF'
#!/usr/bin/env python3
import sys
from pathlib import Path
from pyspark.sql import SparkSession

def main():
    local_dir = sys.argv[1]
    hdfs_path = sys.argv[2]

    print(f"初始化 Spark...")
    spark = SparkSession.builder \
        .appName("UploadParquetToHDFS") \
        .config("spark.driver.memory", "1g") \
        .config("spark.hadoop.fs.defaultFS", "hdfs://proxy.huabase.net:8020") \
        .getOrCreate()

    print(f"Spark 会话创建成功")

    parquet_dir = Path(local_dir)
    parquet_files = list(parquet_dir.glob("*.parquet"))

    print(f"找到 {len(parquet_files)} 个 Parquet 文件")

    success = 0
    fail = 0

    for idx, parquet_file in enumerate(parquet_files, 1):
        print(f"\n[{idx}/{len(parquet_files)}] 上传: {parquet_file.name}")
        try:
            df = spark.read.parquet(str(parquet_file))
            row_count = df.count()
            print(f"  行数: {row_count:,}")

            target = f"{hdfs_path}/{parquet_file.stem}"
            df.write.mode("overwrite").parquet(target)
            print(f"  ✓ 上传成功")
            success += 1
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            fail += 1

    spark.stop()

    print(f"\n{'='*60}")
    print(f"上传完成! 成功: {success}, 失败: {fail}")
    print(f"{'='*60}")

if __name__ == "__main__":
    main()
EOF

# 执行 Spark 上传
echo "开始上传..."
echo ""

/opt/spark/bin/spark-submit \
  --master local[*] \
  --deploy-mode client \
  --conf spark.driver.memory=1g \
  /tmp/upload_parquet_$$.py "$LOCAL_PARQUET_DIR" "$HDFS_TARGET_PATH"

# 清理临时脚本
rm -f /tmp/upload_parquet_$$.py

echo ""
echo "================================================"
echo "   上传完成!"
echo "================================================"
echo "HDFS 路径: hdfs://proxy.huabase.net:8020$HDFS_TARGET_PATH"
echo ""
echo "验证 HDFS 文件:"
echo "  hdfs dfs -ls $HDFS_TARGET_PATH"
echo ""
