#!/usr/bin/env bash
set -euo pipefail

# ── 路径定义 ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COCKROACH_DIR="$PROJECT_ROOT/json/cockroach"
BACKUP_DIR="$COCKROACH_DIR/.noisebackup"
ACTIVE_FILE="$COCKROACH_DIR/cockroach_metrics_knowledge.json"
STAGE3_DIR="$COCKROACH_DIR/.stage3"
STAGE3_FILE="$STAGE3_DIR/cockroach_metrics_knowledge.json"
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

# ── 1.5 确保主文件是 noise0（兼容旧脚本可能已替换主文件的情况）──
if ! cmp -s "$ACTIVE_FILE" "$NOISE0_BACKUP"; then
    log "主文件与 noise0 不一致，恢复为 noise0"
    cp "$NOISE0_BACKUP" "$ACTIVE_FILE"
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

# ── 4. 激活目标版本（写 sidecar，不替换主文件，不重建 Redis）──
log "激活版本: $LABEL"
if [[ "$CHOICE" == "0" ]]; then
    rm -f "$STAGE3_FILE"
    rmdir "$STAGE3_DIR" 2>/dev/null || true
    log "noise0: 已删除 sidecar，Stage3 将 fallback 到主知识库 (noise0)"
else
    mkdir -p "$STAGE3_DIR"
    cp "$TARGET" "$STAGE3_FILE"
    log "已写入 sidecar: $STAGE3_FILE"
fi
log "主文件保持不变: $ACTIVE_FILE (noise0)"

# ── 5. Redis 索引保持不变 ──
log "Redis 索引保持不变 (始终基于 noise0)"

# ── 6. 完成 ──
echo ""
echo "============================================"
echo "  ✅ 切换完成"
echo "  当前版本: $LABEL"
echo "  主文件: noise0 (Stage1/2 使用，不变)"
echo "  Stage3 sidecar: $([ -f "$STAGE3_FILE" ] && echo '存在' || echo '不存在（fallback noise0）')"
echo "  Redis 索引: $INDEX_NAME (未重建，保持 noise0)"
echo "============================================"
echo ""
echo -e "${YELLOW}下一步请手动重启服务:${NC}"
echo "  sudo systemctl restart xiyan-mcp-server.service"
echo ""
