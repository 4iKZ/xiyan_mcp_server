#!/usr/bin/env bash
set -euo pipefail

# ── 路径定义 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COCKROACH_DIR="$PROJECT_ROOT/json/cockroach"
BACKUP_DIR="$COCKROACH_DIR/.noisebackup"
ACTIVE_FILE="$COCKROACH_DIR/cockroach_metrics_knowledge.json"
INDEX_NAME="xiyan_schema_cockroach"
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

# ── 1. 初始化：确保 .noisebackup 存在，noise0 有备份 ──
mkdir -p "$BACKUP_DIR"

NOISE0_BACKUP="$BACKUP_DIR/cockroach_metrics_knowledge_noise0.json"
NOISE50_BACKUP="$BACKUP_DIR/cockroach_metrics_knowledge_noise50.json"
NOISE100_BACKUP="$BACKUP_DIR/cockroach_metrics_knowledge_noise100.json"

# 如果 noise0 备份还不存在，从当前活动文件（就是 noise0）复制一份
if [[ ! -f "$NOISE0_BACKUP" ]]; then
    if [[ -f "$ACTIVE_FILE" ]]; then
        log "首次初始化：备份 noise0 → .noisebackup/"
        cp "$ACTIVE_FILE" "$NOISE0_BACKUP"
    else
        err "活动文件 $ACTIVE_FILE 不存在，且无 noise0 备份"
        exit 1
    fi
fi

# ── 2. 清场：将当前目录下的噪声文件移入 .noisebackup/ ──
MOVED=0
for f in "$COCKROACH_DIR"/*_noise*.json; do
    if [[ -f "$f" ]]; then
        mv "$f" "$BACKUP_DIR/"
        log "移入 .noisebackup/: $(basename "$f")"
        MOVED=$((MOVED + 1))
    fi
done
if [[ $MOVED -eq 0 ]]; then
    log "当前目录无残留噪声文件"
fi

# ── 3. 交互式菜单 ──
echo ""
echo "============================================"
echo "  Cockroach 噪声版本切换"
echo "============================================"
echo "  0  - noise0   (干净，无噪声)"
echo "  50 - noise50  (50% 噪声)"
echo "  100- noise100 (100% 噪声)"
echo "============================================"
read -r -p "请选择 [0/50/100]: " CHOICE

case "$CHOICE" in
    0)  TARGET="$NOISE0_BACKUP"
        LABEL="noise0 (干净)"
        ;;
    50) TARGET="$NOISE50_BACKUP"
        LABEL="noise50"
        ;;
    100) TARGET="$NOISE100_BACKUP"
        LABEL="noise100"
        ;;
    *)
        err "无效选择: $CHOICE"
        exit 1
        ;;
esac

if [[ ! -f "$TARGET" ]]; then
    err "目标备份不存在: $TARGET"
    exit 1
fi

# ── 4. 激活目标版本 ──
log "激活版本: $LABEL"
cp "$TARGET" "$ACTIVE_FILE"

# 验证：目录下只剩 noise0 主文件，无其他噪声文件
if ls "$COCKROACH_DIR"/*_noise*.json >/dev/null 2>&1; then
    err "目录中仍有噪声文件残留！"
    ls "$COCKROACH_DIR"/*_noise*.json
    exit 1
fi
log "json/cockroach/ 目录干净，仅含目标版本"

# ── 5. 删除旧索引 ──
log "删除 Redis 索引: $INDEX_NAME"
docker exec redis-stack redis-cli FT.DROPINDEX "$INDEX_NAME" DD 2>/dev/null || warn "索引可能不存在，继续重建"

# ── 6. 重建索引 ──
log "重建索引..."
cd "$PROJECT_ROOT"
PYTHONPATH="$PYTHONPATH" /usr/bin/python "$INDEX_SCRIPT" \
    --config "$CONFIG" \
    --system cockroach \
    --rebuild

# ── 7. 完成 ──
echo ""
echo "============================================"
echo "  ✅ 切换完成"
echo "  当前版本: $LABEL"
echo "  Redis 索引: $INDEX_NAME"
echo "  kb_table_descriptions 来源: $(basename "$ACTIVE_FILE")"
echo "============================================"
echo ""
echo -e "${YELLOW}下一步请手动重启服务:${NC}"
echo "  sudo systemctl restart xiyan-mcp-server.service"
echo ""
