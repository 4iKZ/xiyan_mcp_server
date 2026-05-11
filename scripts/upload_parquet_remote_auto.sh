#!/bin/bash
#
# 自动化上传 Parquet 文件到 HDFS
# 从本地传输文件到远程机器，然后执行上传
#

set -e

# === 配置区域 ===
# 远程机器配置
REMOTE_HOST="proxy.huabase.net"
REMOTE_PORT="2117"
REMOTE_USER="root"  # 根据需要修改
REMOTE_DIR="/tmp/xiyan_parquet"  # 远程临时目录
HDFS_PATH="/flatten_window_metrics"  # HDFS 目标路径

# 本地 parquet 文件目录
LOCAL_PARQUET_DIR="$(pwd)/query_results_parquet"

# === 颜色 ===
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

print_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
print_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

echo "================================================"
echo "   Parquet 文件自动上传到 HDFS"
echo "================================================"
echo ""

# 1. 检查本地文件
if [ ! -d "$LOCAL_PARQUET_DIR" ]; then
    print_error "本地目录不存在: $LOCAL_PARQUET_DIR"
    exit 1
fi

file_count=$(ls -1 "$LOCAL_PARQUET_DIR"/*.parquet 2>/dev/null | wc -l)
if [ "$file_count" -eq 0 ]; then
    print_error "未找到 Parquet 文件"
    exit 1
fi

print_info "本地找到 $file_count 个 Parquet 文件"
echo ""

# 2. 创建远程目录并传输文件
print_info "连接远程机器: $REMOTE_HOST:$REMOTE_PORT"
print_info "创建远程目录..."

ssh -p "$REMOTE_PORT" "$REMOTE_HOST" "mkdir -p $REMOTE_DIR"

print_info "传输文件到远程..."
rsync -avz -e "ssh -p $REMOTE_PORT" \
    "$LOCAL_PARQUET_DIR"/*.parquet \
    "$REMOTE_HOST:$REMOTE_DIR/"

echo ""
print_info "文件传输完成"
echo ""

# 3. 在远程机器上执行上传
print_info "在远程机器上执行 HDFS 上传..."

ssh -p "$REMOTE_PORT" "$REMOTE_HOST" bash << 'REMOTE_SCRIPT'
set -e

LOCAL_DIR="/tmp/xiyan_parquet"
HDFS_BASE_PATH="/flatten_window_metrics"

echo "================================================"
echo "   远程机器: HDFS 上传"
echo "================================================"
echo ""

# 检查文件
file_count=$(ls -1 "$LOCAL_DIR"/*.parquet 2>/dev/null | wc -l)
echo "收到 $file_count 个 Parquet 文件"
echo ""

# 创建 HDFS 目录
echo "创建 HDFS 目录: $HDFS_BASE_PATH"
hdfs dfs -mkdir -p "$HDFS_BASE_PATH"

# 上传文件
echo "开始上传..."
success=0
fail=0

for file in "$LOCAL_DIR"/*.parquet; do
    if [ -f "$file" ]; then
        filename=$(basename "$file")
        echo "  上传: $filename"

        if hdfs dfs -put -f "$file" "$HDFS_BASE_PATH/"; then
            ((success++))
            echo "    ✓ 成功"
        else
            ((fail++))
            echo "    ✗ 失败"
        fi
    fi
done

echo ""
echo "================================================"
echo "上传完成! 成功: $success, 失败: $fail"
echo "================================================"
REMOTE_SCRIPT

echo ""
print_info "全部完成!"
echo "HDFS 路径: hdfs://proxy.huabase.net:8020$HDFS_PATH"
echo ""
print_info "验证 HDFS 文件:"
echo "  ssh -p $REMOTE_PORT $REMOTE_HOST 'hdfs dfs -ls $HDFS_PATH'"
