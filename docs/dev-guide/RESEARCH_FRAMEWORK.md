# XiYan MCP NL2SQL 鲁棒性研究框架

## 核心研究问题

**Schema 上下文质量如何影响 NL2SQL 精度？**

围绕 XiYan MCP Server 的 Text-to-SQL 管线，系统性地研究三种上下文质量退化/变化模式对最终 SQL 生成精度的影响。

---

## 研究动机

当前 NL2SQL 系统普遍采用"检索增强生成"范式：先通过向量检索从大规模数据库 Schema 中筛选相关表，再将精简后的 Sub-Schema 作为上下文交给 LLM 生成 SQL。然而：

- 不同 LLM 对 Schema 上下文的利用效率差异未被系统研究
- Schema 知识库的描述质量（元数据准确性）对精度的影响缺乏量化分析
- 检索阶段的 precision-recall 权衡如何影响最终 SQL 精度尚无明确指导

本研究的三个实验维度分别对应上述三个问题。

---

## 实验环境

| 项目 | 详情 |
|------|------|
| **系统** | XiYan MCP Server (v0.1.5+) |
| **数据库** | CockroachDB 监控指标（189 张表，via GreptimeDB） |
| **测试集** | 281 条自然语言查询（5 个难度 level） |
| **向量检索** | Qwen3-Embedding-8B (4096 维) + Redis KNN |
| **评估方式** | AI judge + 100 条人工标注 dual validation |

---

## 三维研究框架

```
         ┌──────────────────────────────────────────┐
         │            研究问题                        │
         │   Schema 上下文质量如何影响 NL2SQL 精度？  │
         └──────────────────────────────────────────┘
                           │
          ┌────────────────┼────────────────┐
          ▼                ▼                 ▼
     Exp 1: 模型       Exp 2: 描述级      Exp 3: 表级噪声
     维度              噪声               (两阶段筛选)

     同一干净上下文      同一模型            同一模型
     × 不同模型          × 不同噪声比例      × 不同(N,M)配比
     × 5个难度level      × 5个难度level      × 5个难度level

     回答:              回答:              回答:
     哪个模型最擅长      模型对描述质量      最优的召回/筛选
     利用Schema上下文   的容忍度边界        配比是多少
```

---

## Experiment 1: 模型维度（跨模型对比）

### 目标

同一 Schema 上下文在不同模型上的 NL2SQL 准确率差异。

### 实验设计

- 使用当前系统默认的干净 Schema 上下文（向量检索 top_k=5）
- 在 281 条查询上测试不同 LLM
- 按 5 个难度 level 分层统计准确率

### 候选模型

| 类别 | 模型 |
|------|------|
| 当前基线 | XiYanSQL-QwenCoder-32B |
| 闭源 SOTA | GPT-4o, Claude Sonnet/Opus |
| 性价比 | DeepSeek-V3/Coder |
| 开源 SQL 专用 | sqlcoder 系列, DIN-SQL |

### 输出指标

- 总体准确率（按模型 × 难度 level 的矩阵）
- 平均 SQL 修复次数
- 平均端到端延迟

---

## Experiment 2: 描述级噪声（元数据质量鲁棒性）

### 目标

表检索结果正确，但给 LLM 的表描述（comment、字段说明、示例值）被不同程度污染时，模型精度如何退化。

### 核心概念

这是模拟真实场景中**表名/字段名结构正确但文档描述不准**的情况（如自动生成的描述、不同团队维护的文档质量参差）。

### M-Schema 噪声注入目标

```
M-Schema 结构：
├── 结构信息（表名、字段名、类型）    ← 保持准确，不加噪
├── 语义信息（comment、description）  ← 噪声注入目标 ★
└── 示例值（distinct examples）       ← 噪声注入目标 ★
```

### 噪声梯度设计

| 噪声级别 | 操作 | 示例 |
|----------|------|------|
| **0%** (baseline) | 原始干净描述 | `sys_cpu_usage: CPU 使用率百分比` |
| **25%** | 随机替换 25% 字段的描述/注释 | 1/4 字段描述被替换为无关内容 |
| **50%** | 替换 50% 字段描述 + 表级描述 | 一半字段 + 表注释被污染 |
| **75%** | 替换 75% 描述 + 错误示例值 | 大部分语义信息被替换 |
| **100%** | 全部语义信息替换（结构保留） | `sys_cpu_usage: 用户登录日志，greptime_value: 登录次数` |

### 噪声生成方式

- 从数据库中随机选取其他表的描述信息来替换目标表的描述
- 保证替换后的描述仍是"合理但错误"的数据（而非乱码）
- 每种噪声级别独立生成 3 套不同的噪声配置，取平均结果以消除随机性

### 输出指标

- 各噪声级别的准确率退化曲线（折线图）
- 各难度 level 的噪声敏感度差异
- 不同错误类型的分布变化（表选错、列选错、JOIN 错误等）

---

## Experiment 3: 表级噪声（两阶段筛选配比优化）

### 目标

向量检索召回过多候选表会引入噪声，召回过少可能遗漏关键表。通过两阶段筛选（向量粗召回 → 模型精筛选），找到最优的 (N, M) 配比。

### 核心概念

- **Stage 1 (向量粗召回)**: 向量检索返回 N 张候选表（高召回率，可能含噪声）
- **Stage 2 (模型精筛选)**: LLM 从 N 张中筛选出 M 张最相关的表
- **Stage 3 (SQL 生成)**: 用 M 张表的 sub-schema 生成最终 SQL

```
Stage 1: 向量检索 → N 张候选表
  ┌─────────────────────────────────────┐
  │ 相关表 (约5张)  │  噪声表 (N-5张)   │
  └─────────────────────────────────────┘
                      ↓
Stage 2: 模型精筛 → M 张最终表
  ┌───────────────────┐
  │ 希望全是相关表     │
  └───────────────────┘
                      ↓
Stage 3: SQL 生成
```

### 实验网格

```
N (向量粗召回)  ∈ {200, 100, 50, 20, 10}
M (模型精筛选)  ∈ {50, 20, 10, 5, 3}

约束: M < N

典型测试点（9个）：
  N=200, M=50   │  N=100, M=20   │  N=50, M=10
  N=200, M=20   │  N=100, M=10   │  N=50, M=5
  N=200, M=10   │  N=100, M=5    │  N=20, M=5
```

### Stage 2 精筛 Prompt 设计

```
以下是 {N} 张候选表的描述。用户问题是："{query}"
请从中选出最相关的 {M} 张表，以 JSON 列表返回表名。
只选择与问题直接相关的表，忽略无关表。
```

### 输出指标

- 各 (N, M) 配比的最终 SQL 准确率（热力图）
- Stage 2 筛选的准确率（筛选出的表是否真的是相关表）
- 端到端延迟与 (N, M) 的关系
- 在准确率/延迟/成本三维空间的 Pareto 前沿

---

## 三个实验的协同逻辑

1. **Exp 1** 先确定"哪个模型最好"作为后续实验的 baseline 模型
2. **Exp 2** 用 baseline 模型测试"如果知识库描述质量差，系统会退化多少"
3. **Exp 3** 用 baseline 模型找"两阶段筛选的最优配比"——本质是研究检索精度不够导致的表级噪声如何影响最终结果

---

## 与 KG 增强实验的关系

现有 EXPERIMENT_PLAN.md 中的 KG 增强方案与本研究是**互补关系**：

| | KG 增强实验 | 鲁棒性实验 |
|---|---|---|
| **方向** | 差上下文 → 好上下文 | 好上下文 → 差上下文 |
| **问题** | 精度提升多少？ | 精度退化多少？ |
| **贡献** | 提出改进方法 | 量化敏感度边界 |

两者结合可完整刻画 **"上下文质量 ↔ NL2SQL 精度"** 的映射关系。

---

## 评估指标体系

### 主要指标

| 指标 | 定义 |
|------|------|
| **Execution Accuracy (EX)** | SQL 能否正确执行 |
| **Exact Set Match (ESM)** | 结果集是否与 ground truth 一致 |
| **Semantic Correctness** | AI judge 判断语义是否正确（正确/部分正确/错误） |

### 辅助指标

- 平均 SQL 修复次数
- 端到端延迟分布
- 错误类型分布（table_not_found, column_not_found, syntax_error, join_error, type_error）

### 评估验证

- AI judge (LLM-as-judge) 对全部 281 条做自动判断
- 100 条人工标注作为 ground truth
- 计算 AI judge 与人工标注的 Cohen's Kappa 系数

---

## 建议的论文结构

```
Title: On the Robustness of Schema-Augmented Text-to-SQL:
       Model Sensitivity, Metadata Quality, and Retrieval Precision

1. Introduction
2. Related Work
   2.1 NL2SQL Systems
   2.2 Schema Retrieval & Filtering
   2.3 RAG Robustness & Noise Tolerance
3. System Overview (XiYan MCP pipeline)
4. Experiment 1: Cross-Model Comparison
   4.1 Setup
   4.2 Results & Analysis
5. Experiment 2: Robustness to Noisy Schema Descriptions
   5.1 Noise Injection Methodology
   5.2 Results & Degradation Patterns
6. Experiment 3: Optimal Two-Stage Table Filtering
   6.1 Recall-Precision Tradeoff
   6.2 Optimal (N, M) Configuration
7. Discussion
   7.1 Practical Implications
   7.2 Limitations & Future Work
8. Conclusion
```

---

## 实现优先级

| 优先级 | 维度 | 理由 |
|--------|------|------|
| **P0** | Exp 2: 描述级噪声 | 学术贡献最大，实现成本最低（只需改 sub-schema 构造逻辑） |
| **P1** | Exp 1: 模型对比 | 实现成本最低，作为必要基线 |
| **P2** | Exp 3: 两阶段配比 | 需要额外实现 Stage 2 筛选组件，偏工程优化 |

---

## 数据集

- `queries.jsonl`: 281 条 NL-SQL 查询对（5 个难度 level）
- `query_tracker_logs/`: 执行追踪日志（JSONL 格式）
- `json/cockroach_metrics_knowledge.json`: CockroachDB 189 张表的 Schema 知识库

## 实验环境

- 数据库：CockroachDB 监控指标（via GreptimeDB），189 张表
- 向量模型：Qwen3-Embedding-8B (4096 维)
- 向量存储：Redis KNN 索引
- 当前 LLM：XiYanSQL-QwenCoder-32B
- 评估：AI judge + 100 条人工标注验证
