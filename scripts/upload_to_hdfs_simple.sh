#!/bin/bash
#
# 使用 hdfs dfs -put 命令上传 Parquet 文件到 HDFS
# 在 cllm 机器 117 节点上执行
#

set -e

# 配置
LOCAL_DIR="/tmp/xiyan_parquet"           # 本地 parquet 文件目录
HDFS_BASE_PATH="/flatten_window_metrics"  # HDFS 目标路径
HDFS_NAMENODE="hdfs://proxy.huabase.net:8020"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

echo "================================================"
echo "   HDFS Parquet 上传工具 (hdfs dfs -put)"
echo "================================================"
echo ""

# 检查本地文件
if [ ! -d "$LOCAL_DIR" ]; then
    echo "错误: 目录不存在: $LOCAL_DIR"
    echo "请先创建目录并放入 parquet 文件:"
    echo "  mkdir -p $LOCAL_DIR"
    exit 1
fi

file_count=$(ls -1 "$LOCAL_DIR"/*.parquet 2>/dev/null | wc -l)
if [ "$file_count" -eq 0 ]; then
    echo "错误: 未找到 Parquet 文件在 $LOCAL_DIR"
    exit 1
fi

print_info "找到 $file_count 个 Parquet 文件"
echo "源目录: $LOCAL_DIR"
echo "目标路径: $HDFS_NAMENODE$HDFS_BASE_PATH"
echo ""

# 创建 HDFS 目录
print_info "创建 HDFS 目录..."
hdfs dfs -mkdir -p "$HDFS_BASE_PATH"

# 上传文件
print_info "开始上传文件..."
success=0
fail=0

for file in "$LOCAL_DIR"/*.parquet; do
    if [ -f "$file" ]; then
        filename=$(basename "$file")
        print_info "上传: $filename"

        if hdfs dfs -put -f "$file" "$HDFS_BASE_PATH/"; then
            ((success++))
        else
            ((fail++))
        fi
    fi
done

echo ""
echo "================================================"
print_info "上传完成!"
echo "================================================"
echo "成功: $success, 失败: $fail"
echo ""
echo "验证 HDFS 文件:"
echo "  hdfs dfs -ls $HDFS_BASE_PATH"
echo ""
