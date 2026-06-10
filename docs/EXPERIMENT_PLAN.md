# XiYan MCP NL2SQL 实验规划

## 一、实验目标

评估 XiYan MCP 在 CockroachDB 运维场景的 NL2SQL 准确率，并验证知识图谱增强方法的效果。

**核心对比**：基线准确率 vs 知识图谱增强后准确率

## 二、实验阶段

```
Phase 1: 数据收集        Phase 2: 基线评估      Phase 3: KG构建       Phase 4: 增强评估
    │                       │                      │                      │
    ▼                       ▼                      ▼                      ▼
 跑281条查询batch       AI judge + 人工标注     构建CockroachDB      重新跑batch + 
 → query_tracker        → 基线准确率             运维知识图谱         → 增强后准确率
                          → 错误分类              → 集成到Schema过滤    → 对比分析
```

---

## 三、Phase 1：数据收集（1-2天）

### 3.1 执行全量查询

```bash
source /root/.bashrc && python scripts/batch_test_queries.py --delay 5
```

输出：`query_tracker_logs/query_tracker_2026-05-XX.jsonl`，每查询一条记录

### 3.2 数据完整性检查

```bash
# 统计
python -c "
import json
records = [json.loads(l) for l in open('query_tracker_logs/query_tracker_2026-05-XX.jsonl')]
success = sum(1 for r in records if r['exec_success'])
fail = sum(1 for r in records if not r['exec_success'])
print(f'总计: {len(records)}, 成功: {success}, 失败: {fail}')
print(f'成功率: {success/len(records)*100:.1f}%')

# 按level统计
for lv in range(1,6):
    subset = [r for r in records if r.get('level')==lv]
    s = sum(1 for r in subset if r['exec_success'])
    print(f'Level {lv}: {len(subset)}条, 成功 {s}, 成功率 {s/len(subset)*100:.1f}%' if subset else '')
"
```

---

## 四、Phase 2：基线评估（3-5天）

### 4.1 人工标注 ground truth（100条）

从 281 条中**分层抽样**100 条（每 level 按比例），人工逐条判断：

| 判定 | 标准 | 示例 |
|------|------|------|
| 正确 | SQL 语义完全匹配 NL | NL问"查前10条"，SQL 确实SELECT LIMIT 10 |
| 部分正确 | SQL 方向对但细节有误 | NL问"每小时平均值"，SQL 用了SUM而非AVG |
| 错误 | SQL 答非所问 | NL问CPU，SQL查了内存表 |

建议你 + 一个同事双盲标注，计算 Cohen's Kappa 系数（论文里标注一致性的证据）。

### 4.2 AI Judge 评估

用另一个 LLM 对全部 281 条做自动判断。Prompt 模板：

```
你是 SQL 评审专家。给定数据库 schema、自然语言问题和生成的 SQL，
判断 SQL 是否正确回答了问题。

Schema: {relevant_schema}
问题: {nl_query}
SQL: {initial_sql}
执行结果: {exec_success}
返回行数: {result_rows}
数据预览: {fields}: {result_preview}

请输出:
1. 判断: [正确] / [部分正确] / [错误]
2. 理由: 一句话说明
```

### 4.3 AI Judge 可信度验证

用人工标注的 100 条作为 ground truth，计算 AI judge 的准确率、召回率、F1。

**论文关键数据**：AI judge 与人工标注的一致性 >85% 才有资格做大规模评估。

### 4.4 基线结果表

| Level | 查询数 | 执行成功 | 语义正确(AI) | 语义正确(人工) | 主要错误类型 |
|-------|--------|---------|-------------|---------------|-------------|
| 1 简单单表 | 60 | | | | |
| 2 聚合统计 | 80 | | | | |
| 3 时间范围 | 60 | | | | |
| 4 多表复杂 | 57 | | | | |
| 5 歧义模糊 | 24 | | | | |
| **总计** | **281** | | | | |

---

## 五、Phase 3：知识图谱构建（5-7天）

### 5.1 知识图谱设计

围绕 CockroachDB 监控指标构建三层 KG：

```
Layer 1: 表-列关系
  cockroach_metrics.sys_cpu_usage
  ├── greptime_timestamp (Timestamp)
  ├── greptime_value (Float64)  
  ├── instance (String)
  ├── job (String)
  └── node_id (String)

Layer 2: 指标语义
  sys_cpu_usage ──belongs_to──▶ CPU类别
  abortspanbytes ──belongs_to──▶ 事务类别
  sql_query_count ──belongs_to──▶ SQL类别
  keybytes ──belongs_to──▶ 存储类别
  
Layer 3: 指标间关系
  sys_cpu_usage ──correlates_with──▶ sys_cpu_utilization (同义)
  sql_query_count ──related_to──▶ sql_query_latency (关联)
  abortspanbytes ──indicates──▶ 事务异常 (诊断)
```

### 5.2 构建方式

- Layer 1：从现有 schema 自动提取
- Layer 2：手动标注指标类别（CPU/内存/存储/SQL/事务/网络/...）
- Layer 3：基于 CockroachDB 文档 + 运维经验手动标注

**预计标注量**：189 张表 × 分 8-10 个类别 ≈ 2-3 小时

### 5.3 KG 增强 Schema 过滤

将 KG 信息编码进 prompt：

```
原prompt: "表名: cockroach_metrics.sys_cpu_usage, 列: ..."
增强prompt: "表名: cockroach_metrics.sys_cpu_usage (CPU指标, 单位: %)
           关联表: sys_cpu_utilization (同义指标)
           列: greptime_timestamp, greptime_value(CPU使用率), instance, node_id"
```

---

## 六、Phase 4：增强评估（3-5天）

### 6.1 重新跑 batch

用 KG 增强后的 schema 过滤，重新执行全部 281 条查询。

### 6.2 对比分析

| 指标 | 基线 | KG增强 | 提升 |
|------|------|--------|------|
| 总体准确率 | | | |
| Level 1 准确率 | | | |
| Level 2 准确率 | | | |
| Level 3 准确率 | | | |
| Level 4 准确率 | | | |
| Level 5 准确率 | | | |
| 平均修复次数 | | | |
| 平均延迟 | | | |

### 6.3 错误模式消融实验

| 消融条件 | 准确率 | 说明 |
|----------|--------|------|
| 无 KG 增强 | | 基线 |
| 仅 Layer1（表列关系） | | 只有schema信息 |
| Layer1+2（+指标语义） | | 知道指标类别 |
| Layer1+2+3（+指标关联） | | 完整的KG |
| 完整 KG | | 最终方案 |

### 6.4 错误分布变化

KG 增强后哪些错误类型减少了？

| 错误类型 | 基线次数 | 增强后次数 | 减少 |
|----------|---------|-----------|------|
| table_not_found | | | |
| column_not_found | | | |
| syntax_error | | | |
| join_error | | | |
| type_error | | | |

---

## 七、论文产出物

### 7.1 数据集

- `queries.jsonl`：281 条 NL-SQL 查询对
- `query_tracker_*.jsonl`：执行追踪数据
- `ground_truth_100.jsonl`：人工标注的 100 条

### 7.2 图表

1. 5 级难度准确率对比柱状图（基线 vs KG）
2. 错误类型分布饼图
3. 消融实验折线图
4. 执行成功率 vs 语义正确率对比
5. AI judge vs 人工标注散点图

### 7.3 实验环境

- 数据库：CockroachDB (via GreptimeDB), 189 张监控指标表
- LLM：XiYanSQL-QwenCoder-32B
- 评估方式：LLM-as-judge + 100条人工标注验证

---

## 八、时间线

| 阶段 | 内容 | 预估 |
|------|------|------|
| Phase 1 | 跑全量batch + 数据检查 | 1天 |
| Phase 2.1 | 人工标注100条 | 1天 |
| Phase 2.2 | AI judge评估 + 可信度验证 | 1天 |
| Phase 2.3 | 基线结果分析 | 1天 |
| Phase 3 | KG构建 + 集成 | 1周 |
| Phase 4 | 增强后跑batch + 对比 | 2天 |
| 写作 | 论文撰写 | 2-3周 |

**总计：约5-6周可完成实验 + 初稿**
