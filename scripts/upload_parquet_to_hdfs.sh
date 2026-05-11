#!/bin/bash
# 将 Parquet 文件上传到 HDFS 的脚本

# 配置变量
HDFS_USER="your_username"  # 修改为你的 HDFS 用户名
HDFS_BASE_PATH="/data/xiyan_mcp_server"  # HDFS 目标路径
LOCAL_DIR="query_results_parquet"  # 本地 Parquet 文件目录

# HDFS 集群地址（如果有 NameNode 高可用，使用 nameservice）
HDFS_NAMENODE="hdfs://proxy.huabase.net:8020"

# 颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

print_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查本地文件是否存在
check_local_files() {
    if [ ! -d "$LOCAL_DIR" ]; then
        print_error "本地目录不存在: $LOCAL_DIR"
        print_info "请先运行测试生成 Parquet 文件"
        exit 1
    fi

    file_count=$(ls -1 "$LOCAL_DIR"/*.parquet 2>/dev/null | wc -l)
    if [ "$file_count" -eq 0 ]; then
        print_error "没有找到 Parquet 文件在 $LOCAL_DIR"
        exit 1
    fi

    print_info "找到 $file_count 个 Parquet 文件"
}

# 创建 HDFS 目录
create_hdfs_directory() {
    print_info "创建 HDFS 目录: $HDFS_BASE_PATH"

    hdfs dfs -mkdir -p "$HDFS_BASE_PATH"

    if [ $? -eq 0 ]; then
        print_info "HDFS 目录创建成功"
    else
        print_error "HDFS 目录创建失败"
        exit 1
    fi
}

# 上传单个文件
upload_single_file() {
    local file=$1
    local filename=$(basename "$file")
    local hdfs_path="$HDFS_BASE_PATH/$filename"

    print_info "上传: $filename"

    hdfs dfs -put -f "$file" "$hdfs_path"

    if [ $? -eq 0 ]; then
        print_info "✓ $filename 上传成功"
    else
        print_error "✗ $filename 上传失败"
        return 1
    fi
}

# 批量上传文件
upload_all_files() {
    print_info "开始批量上传..."

    success_count=0
    fail_count=0

    for file in "$LOCAL_DIR"/*.parquet; do
        if [ -f "$file" ]; then
            if upload_single_file "$file"; then
                ((success_count++))
            else
                ((fail_count++))
            fi
        fi
    done

    echo ""
    print_info "上传完成!"
    print_info "成功: $success_count, 失败: $fail_count"
}

# 验证上传结果
verify_upload() {
    print_info "验证 HDFS 文件..."

    hdfs dfs -ls "$HDFS_BASE_PATH" | grep ".parquet"

    if [ $? -eq 0 ]; then
        print_info "✓ 文件验证成功"
    else
        print_warn "⚠ 未在 HDFS 中找到 Parquet 文件"
    fi
}

# 主函数
main() {
    echo "================================================"
    echo "   Parquet 文件上传到 HDFS 工具"
    echo "================================================"
    echo ""

    # 检查本地文件
    check_local_files
    echo ""

    # 创建 HDFS 目录
    create_hdfs_directory
    echo ""

    # 上传文件
    upload_all_files
    echo ""

    # 验证
    verify_upload
    echo ""

    echo "================================================"
    print_info "全部完成!"
    echo "HDFS 路径: $HDFS_NAMENODE$HDFS_BASE_PATH"
    echo "================================================"
}

# 执行主函数
main
