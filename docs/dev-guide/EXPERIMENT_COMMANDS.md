# 实验命令手册

> 给协作同事的速查手册。从"拿到代码"到"出准确率报表"完整流程。
> 详细原理见 `docs/JUDGE_IMPLEMENTATION_2026-06-15.md` 和
> `docs/STAGE2_TUNING_LOG_2026-06-15.md`，本文只列命令。

---

## 0. 前置检查（每次开机做一遍）

```bash
cd /data/xiyan_mcp_server

# 1. Redis 起着没
redis-cli ping     # 期望: PONG

# 2. MCP server 起着没（默认 8000 端口）
curl -s http://localhost:8000/mcp -o /dev/null -w "%{http_code}\n"
# 期望: 406 或 200（406 是正常的，说明 server 在）

# 3. 如果 server 没起，前台起：
python -m xiyan_mcp_server.server streamable-http --host 0.0.0.0 --port 8000
# 或后台起：
nohup python -m xiyan_mcp_server.server streamable-http --host 0.0.0.0 --port 8000 \
    > server.log 2>&1 &
```

**改了 server 代码 / 配置必须重启 server！** 不重启的话新参数不生效。

---

## 1. 跑批量实验（产 tracker 文件）

脚本：`scripts/batch_test_queries.py`
输出：`query_tracker_logs/query_tracker_{date}_{tag}.jsonl`

### 1.1 标准三组对照实验

```bash
# A. 二级筛选开启，N=20, M=5（默认实验组）
python scripts/batch_test_queries.py \
    --input dataset/cockroach_nl2sql_1500.jsonl \
    --stage2 --stage1-n 20 --stage2-m 5

# B. 二级筛选开启，N=30, M=3（对比配比）
python scripts/batch_test_queries.py \
    --input dataset/cockroach_nl2sql_1500.jsonl \
    --stage2 --stage1-n 30 --stage2-m 3

# C. 二级筛选关闭（baseline）
python scripts/batch_test_queries.py \
    --input dataset/cockroach_nl2sql_1500.jsonl \
    --no-stage2
```

跑完文件落在：
```
query_tracker_logs/
├── query_tracker_2026-06-15_s2on_n20_m5.jsonl
├── query_tracker_2026-06-15_s2on_n30_m3.jsonl
└── query_tracker_2026-06-15_s2off.jsonl
```

### 1.2 常用参数

| 参数 | 说明 | 例 |
|---|---|---|
| `--input` | 数据集路径（必传） | `dataset/cockroach_nl2sql_1500.jsonl` |
| `--start N` | 从第 N 条开始 | `--start 100` |
| `--limit M` | 只跑 M 条（0=全部） | `--limit 20` |
| `--delay S` | 条间隔秒数 | `--delay 5` |
| `--timeout S` | 单条超时秒数 | `--timeout 180` |
| `--stage2` / `--no-stage2` | 启用/禁用二级筛选 | |
| `--stage1-n N` | stage1 召回数 | `--stage1-n 20` |
| `--stage2-m M` | stage2 精筛保留数 | `--stage2-m 5` |
| `--tag NAME` | 实验标签（不传时自动派生） | `--tag exp03` |

### 1.3 小样本试跑（建议先做）

```bash
# 先跑前 10 条看看，确认 server 没问题
python scripts/batch_test_queries.py \
    --input dataset/cockroach_nl2sql_1500.jsonl \
    --limit 10 --stage2 --stage1-n 20 --stage2-m 5
```

### 1.4 后台跑全量（1500 条 ~1-2 小时）

```bash
nohup python scripts/batch_test_queries.py \
    --input dataset/cockroach_nl2sql_1500.jsonl \
    --stage2 --stage1-n 20 --stage2-m 5 \
    > batch_s2on_n20_m5.log 2>&1 &

# 看进度
tail -f batch_s2on_n20_m5.log
```

---

## 2. 跑评价模型（产 judge 文件）

脚本：`scripts/run_judge_batch.py`
输入：tracker jsonl
输出：`judge_results/judge_*.jsonl`（同样的记录 + judge 字段）

### 2.1 标准流程

```bash
# 每个 tracker 文件单独跑一次
python scripts/run_judge_batch.py \
    --input  query_tracker_logs/query_tracker_2026-06-15_s2on_n20_m5.jsonl \
    --output judge_results/judge_2026-06-15_s2on_n20_m5.jsonl \
    --concurrency 4
```

### 2.2 三组对照批跑（推荐写成循环）

```bash
mkdir -p judge_results
DATE=2026-06-15

for TAG in s2on_n20_m5 s2on_n30_m3 s2off; do
    nohup python scripts/run_judge_batch.py \
        --input  query_tracker_logs/query_tracker_${DATE}_${TAG}.jsonl \
        --output judge_results/judge_${DATE}_${TAG}.jsonl \
        --concurrency 4 \
        > judge_results/run_${TAG}.log 2>&1 &
done

# 看任意一个的进度
tail -f judge_results/run_s2on_n20_m5.log
```

### 2.3 参数说明

| 参数 | 说明 | 默认 |
|---|---|---|
| `--input` | tracker jsonl 路径（必传） | — |
| `--output` | judge jsonl 输出路径（必传） | — |
| `--concurrency` | 并发数（网关压力大就降到 2） | 4 |
| `--limit N` | 只评 N 条（smoke test 用） | 0=全部 |
| `--only-failed` | 只评 `exec_success=False` 的记录 | False |
| `--no-sort` | 跑完不按输入顺序整理输出（默认会整理） | False |

### 2.4 输出格式（重要）

每条 judge 记录是**精简过**的——只保留分析必需的 14 个字段
（`nl_query` / `initial_sql` / `tables_used` / `stage1_tables` / `stage2_tables` /
`run_tag` / `exec_success` / `result_rows` / `stage2_enabled` / `stage1_top_n` /
`stage2_top_m` / `stage2_model` / `tool` / `input_index`）+ `judge` 字段。
原 tracker 里 `exec_error` traceback、`relevant_schema`、`retries` 等冗长字段
都被砍掉，文件体积约为原 tracker 的 44%。

输出会自动按 `input_index` 排回 tracker 的原始顺序（写入阶段是按完成顺序追加，
保证崩了不丢；最后做一次原子排序重写）。

### 2.5 断点续跑

**中断了再跑同一条命令就行**——已评过的 `(nl_query, initial_sql)` 自动跳过。
不会重复算钱。

```bash
# 比如中途断了，直接重跑
python scripts/run_judge_batch.py \
    --input  query_tracker_logs/query_tracker_2026-06-15_s2on_n20_m5.jsonl \
    --output judge_results/judge_2026-06-15_s2on_n20_m5.jsonl \
    --concurrency 4
# → 屏幕会显示"已有 X 条评价结果，将跳过"
```

### 2.6 时长估算

- 单条 judge 调用：~17–110 秒（DeepSeek-V4-Pro 网关延迟）
- 并发 4 跑 1500 条 ≈ **3 小时**
- 全 3 组 4500 条 ≈ **9 小时**（建议过夜）

---

## 3. 看准确率报表

脚本：`scripts/analyze_judge_results.py`

### 3.1 单组报表

```bash
python scripts/analyze_judge_results.py \
    --input judge_results/judge_2026-06-15_s2on_n20_m5.jsonl
```

输出：整体准确率、错误类别分布、分桶细分、judge 延迟。

### 3.2 多组横向对比（合并 + 分桶）

```bash
DATE=2026-06-15

# 1. 把三组的 judge 输出合并
cat judge_results/judge_${DATE}_s2on_n20_m5.jsonl \
    judge_results/judge_${DATE}_s2on_n30_m3.jsonl \
    judge_results/judge_${DATE}_s2off.jsonl \
    > judge_results/judge_${DATE}_all.jsonl

# 2. 出报表（自动按 run_tag 分桶，每组一行准确率）
python scripts/analyze_judge_results.py \
    --input judge_results/judge_${DATE}_all.jsonl \
    --csv   judge_results/bucket_metrics_${DATE}.csv
```

CSV 可以直接拖进 Excel / pandas 出图。

### 3.3 只看某一组

```bash
python scripts/analyze_judge_results.py \
    --input judge_results/judge_2026-06-15_all.jsonl \
    --filter-tag s2on_n20_m5
```

### 3.4 导出错样本人工排查

```bash
python scripts/analyze_judge_results.py \
    --input judge_results/judge_2026-06-15_all.jsonl \
    --dump-errors schema_wrong,filter_wrong \
    --error-output judge_results/errors_table_filter.jsonl
```

错误类别可选：`schema_wrong / column_wrong / aggregation_wrong / filter_wrong / syntax / other`

---

## 4. 完整工作流速查

```
┌────────────────────────────────────────────────────────────────┐
│ 1. 跑批量（每组一个 tag）                                       │
│    batch_test_queries.py --stage2 --stage1-n 20 --stage2-m 5  │
│                                                                │
│    → query_tracker_logs/query_tracker_{date}_{tag}.jsonl       │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│ 2. 跑评价                                                       │
│    run_judge_batch.py --input ... --output ... --concurrency 4 │
│                                                                │
│    → judge_results/judge_{date}_{tag}.jsonl                    │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│ 3. 看报表                                                       │
│    analyze_judge_results.py --input ... [--csv ...]            │
│                                                                │
│    → 屏幕 + judge_results/bucket_metrics_*.csv                 │
└────────────────────────────────────────────────────────────────┘
```

---

## 5. 常见问题

### Q: server 没起来 / 起不来
```bash
# 看 server 日志
tail -50 server.log
# 常见原因：8000 端口已被占
ss -lnp | grep 8000
# 杀掉占用进程或换端口
```

### Q: judge 调用失败说"context length exceeded"
看 tracker 里 `initial_sql` 是不是空字符串。空 SQL 是被测系统层的失败，
judge 脚本会自动标 `system_failure` 跳过；如果是 judge 自己 prompt 超长，
降 `judge_top_k`（在 `config.yml` 里改）。

### Q: 跑到一半被打断了怎么办
- batch_test：用 `--start N` 接着跑（tracker 文件会 append，不会丢数据）
- judge：直接重跑同一条命令，自动跳过已评过的

### Q: 想换 judge 模型
改 `src/xiyan_mcp_server/config.yml` 里 `judge:` 节的 `model_name`
和 `api_url`，**不用重启 server**（judge 是离线脚本）。

### Q: 想换 stage2 模型
改 `config.yml` 里 `schema_filter.stage2.model_name` 和 `api_url`，
**必须重启 server**。

### Q: 同一个 tag 跑两次会怎样
两次的记录都会 append 到同一个文件里 → judge 端按 `(nl_query, initial_sql)`
去重，但因为同一组 query 两次的 SQL 可能不一样，**两次都会被判**。
建议每次实验前先把旧 tracker 文件挪走或改 tag。

---

## 6. 文件位置一览

```
/data/xiyan_mcp_server/
├── dataset/cockroach_nl2sql_1500.jsonl              ← 测试集
├── src/xiyan_mcp_server/config.yml                  ← 所有配置
│
├── query_tracker_logs/                              ← 原始 tracker
│   └── query_tracker_{date}_{tag}.jsonl
│
├── judge_results/                                   ← 评价结果
│   ├── judge_{date}_{tag}.jsonl                     ← 评价主输出
│   ├── bucket_metrics_{date}.csv                    ← 分桶 CSV
│   └── errors_*.jsonl                               ← 错样本（可选）
│
├── scripts/
│   ├── batch_test_queries.py                        ← step 1: 跑批量
│   ├── run_judge_batch.py                           ← step 2: 跑评价
│   └── analyze_judge_results.py                     ← step 3: 出报表
│
└── docs/
    ├── EXPERIMENT_COMMANDS.md                       ← 本文档
    ├── JUDGE_IMPLEMENTATION_2026-06-15.md           ← 评价模型设计详解
    └── STAGE2_TUNING_LOG_2026-06-15.md              ← 二级筛选调优记录
```

---

## 7. 联系人 / 求助

- 跑不通先看：`server.log`、`batch_*.log`、`judge_results/run_*.log`
- 配置疑问看：`src/xiyan_mcp_server/config.yml` 里的注释（每节都有）
- 数据格式疑问看：`docs/JUDGE_IMPLEMENTATION_2026-06-15.md` §10.7
