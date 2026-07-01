#!/usr/bin/env bash
set -euo pipefail

# ─────────────────────────────────────────────────────────
# 构建 Judge 模型专用的 Noise0 Redis 索引
#
# 原理：临时将 noise0 备份文件替换为活跃文件 → 调用
# index_knowledge.py 建索引 → 恢复原始活跃文件。
# 最终得到一个与主索引完全隔离的 xiyan_schema_cockroach_judge。
#
# 用法：
#     bash scripts/build_judge_index.sh
#     bash scripts/build_judge_index.sh --force  强制重建（即使索引已存在）
# ─────────────────────────────────────────────────────────

FORCE="${1:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COCKROACH_DIR="$PROJECT_ROOT/json/cockroach"
BACKUP_DIR="$COCKROACH_DIR/.noisebackup"
ACTIVE_FILE="$COCKROACH_DIR/cockroach_metrics_knowledge.json"
NOISE0_SRC="$BACKUP_DIR/cockroach_metrics_knowledge_noise0.json"
JUDGE_INDEX="xiyan_schema_cockroach_judge"
CONFIG="$PROJECT_ROOT/src/xiyan_mcp_server/config.yml"
INDEX_SCRIPT="$PROJECT_ROOT/scripts/index_knowledge.py"
PYTHONPATH="$PROJECT_ROOT/src:/data/pipspace"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERR]${NC} $*"; }

# ── 0. 前置检查 ──
if [[ ! -f "$NOISE0_SRC" ]]; then
    err "noise0 备份文件不存在: $NOISE0_SRC"
    exit 1
fi

EXISTS=$(docker exec redis-stack redis-cli FT._LIST 2>/dev/null | grep -c "$JUDGE_INDEX" || true)
if [[ "$EXISTS" -gt 0 ]]; then
    if [[ "$FORCE" == "--force" ]] || [[ "$FORCE" == "-f" ]]; then
        log "索引 $JUDGE_INDEX 已存在，强制重建"
        docker exec redis-stack redis-cli FT.DROPINDEX "$JUDGE_INDEX" DD 2>/dev/null || true
    else
        warn "索引 $JUDGE_INDEX 已存在，跳过（使用 --force 强制重建）"
        exit 0
    fi
fi

# ── 0. 生成 noise0 知识库所需的临时文件 ──
#     noise0 文件已经是 embedding_content 格式，直接可用。
#     其他两个知识文件（logs、traces）无噪声变体，直接用活跃文件即可。

log "=== 构建 Judge 专用索引: $JUDGE_INDEX ==="
log "数据来源: $NOISE0_SRC"

# ── 1. 暂存活跃文件并替换为 noise0 ──
ACTIVE_BACKUP="$BACKUP_DIR/.active_backup_for_judge_build.json"
SWAPPED=false

if [[ -f "$ACTIVE_FILE" ]]; then
    log "暂存当前活跃文件..."
    cp "$ACTIVE_FILE" "$ACTIVE_BACKUP"
    SWAPPED=true
fi

log "临时替换活跃文件为 noise0..."
cp "$NOISE0_SRC" "$ACTIVE_FILE"

# ── 2. 建索引（--index-name 覆盖默认命名） ──
log "构建 Redis 向量索引: $JUDGE_INDEX"
cd "$PROJECT_ROOT"
PYTHONPATH="$PYTHONPATH" /usr/bin/python "$INDEX_SCRIPT" \
    --config "$CONFIG" \
    --system cockroach \
    --index-name "$JUDGE_INDEX" \
    --rebuild

# ── 3. 恢复原始活跃文件 ──
if [[ "$SWAPPED" == "true" ]]; then
    log "恢复原始活跃文件..."
    mv "$ACTIVE_BACKUP" "$ACTIVE_FILE"
else
    rm -f "$ACTIVE_FILE"
fi

# ── 4. 验证 ──
DOC_COUNT=$(docker exec redis-stack redis-cli FT.INFO "$JUDGE_INDEX" 2>/dev/null \
    | grep -oP 'num_docs\s+\K\d+' || echo "0")
log "索引完成: $JUDGE_INDEX, 文档数=$DOC_COUNT"
log "验证无残留临时文件..."
rm -f "$ACTIVE_BACKUP"

echo ""
echo "============================================"
echo "  ✅ Judge 索引构建完成"
echo "  索引名称: $JUDGE_INDEX"
echo "  文档数量: $DOC_COUNT"
echo "============================================"
