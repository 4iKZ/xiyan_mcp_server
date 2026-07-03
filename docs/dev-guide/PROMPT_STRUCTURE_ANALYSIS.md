# 三个模型的 Prompt 结构分析

## 模型 1：xiyansql-7b（SQL 生成）

### 初始生成 Prompt

**消息结构**: system + user（2 条消息）

#### System Message

```
你现在是一名{dialect}数据分析专家，你的任务是根据参考的数据库schema和用户的问题，编写正确的SQL来回答用户的问题，生成的SQL用 sql 和  包围起来。
注意：
1、表名已经包含了完整的 schema 前缀（如 sundb_metrics.table_name），请直接引用这些表名，**禁止**添加 'public.' 或其他任何额外的库名/Schema 前缀。
2、只生成一个 SQL 语句。
3、对于查询明细数据（不含 COUNT/SUM/AVG/GROUP BY 等聚合），请在 SQL 末尾加 LIMIT 500 限制返回行数。
4、GreptimeDB 底层使用 DataFusion 查询引擎，以下 PostgreSQL 语法不兼容，必须避免：
   a) 没有 DATE() 函数，日期过滤直接用字符串比较：greptime_timestamp >= '2026-06-01 00:00:00'
   b) SELECT DISTINCT 时，ORDER BY 的所有列必须出现在 SELECT 列表中
   c) 时间戳之间不能做乘除运算（timestamp/timestamp 或 timestamp*n 等不支持）
   d) 多表 JOIN 或子查询中，列引用务必带表别名（如 t.node_id），避免仅写 node_id
   e) 时间戳值写成完整的 ISO 字符串 '2026-06-01 00:00:00'，不要简写为 '2026-06-01'
   f) 子查询中避免 SELECT *（DataFusion 可能无法正确展开），尽量显式列出所需列名
   g) date_part 等函数只能用于真正的时间戳列，不能对聚合后的数值列调用

【数据库schema】
{Sub-Schema 字符串，格式如下：}

# Table: cockroach_metrics.changefeed_backfill_count, 名称: Changefeed当前回填数。描述: ...
[
(job:VARCHAR),
(instance:VARCHAR),
(node_id:VARCHAR),
(greptime_value:DOUBLE),
(greptime_timestamp:TIMESTAMP)
]
# Table: cockroach_metrics.changefeed_backfill_pending_ranges, ...
[
...
]

【问题】
{用户自然语言查询}
```

#### User Message

```
用户的问题是: {用户自然语言查询}
```

---

### 修复重试 Prompt（SQL 执行失败时）

**消息结构**: system + user（2 条消息）

#### System Message（按错误类型选不同模板）

```
现在你是一个{dialect}数据分析专家。下面的SQL执行时出现了**{错误描述}**。{针对性修复指示}
注意：
1. ...
2. ...
3. ...
4. 生成的SQL用```sql 和```包围起来。

【数据库schema】
{完整 M-Schema 字符串}
```

**8 种错误类型的修复指示：**

| 错误类型 | 修复指示 |
|----------|----------|
| syntax_error | 仅修复语法错误，不允许改变SQL的逻辑 |
| column_not_found | 尝试替换为schema中实际存在的列名；如果原列名在schema中找不到，尝试语义最接近的列名；如果确实没有匹配的列，可以适当简化查询（如用 SELECT * 替代） |
| table_not_found | 尝试替换为schema中实际存在的表名；在schema中查找语义最接近的表名 |
| function_not_found | 尝试用数据库实际支持的函数实现等价的语义 |
| type_error | 添加适当的 CAST(expr AS type) 转换不兼容的类型；对于比较操作，确保两边类型一致；对于聚合函数参数，确保类型正确 |
| join_error | 给所有有歧义的列引用加上表名前缀（如 table.column）；如果使用了别名，请使用别名前缀 |
| unsupported_statement | 确保SQL以 SELECT 开头，没有多余的前缀词；确保只有一条SQL语句；移除任何非标准的语法结构 |
| planner_error | 拆分子查询为更简单的形式；简化 WHERE 子句中的复杂表达式；如果使用了窗口函数或CTE，尝试重写为简单查询 |
| other（默认） | 只修改必要的部分，尽量保持原SQL的逻辑不变；如果错误无法修复，可以尝试生成等价但语法不同的SQL |

#### User Message

```
【问题】
{用户问题}

【待检查SQL】
{执行失败的 SQL}

【错误信息】
{数据库返回的错误信息}
```

---

## 模型 2：qwen3-32b（Stage 2 Schema 精筛）

**消息结构**: 仅 user（1 条消息，无 system message）

**调用参数**: `temperature=0.0`, `enable_thinking=false`

#### User Message（唯一消息）

```
你是数据库 Schema 筛选专家。下面是 {N} 张候选数据表的信息，请从中选出最有可能用于回答用户问题的 {M} 张表。

# 用户问题
{用户自然语言查询}

# 候选表（共 {N} 张）
1. {表名1}
   {embedding_content 全文 — 格式为"表名: xxx。名称: xxx。描述: xxx。业务含义: xxx。"}

2. {表名2}
   {embedding_content 全文}

...
（共 N 张）

# 输出要求
1. **必须返回恰好 {M} 个表名**，按相关性从高到低排列。即使你认为只有少数几张表强相关，也要把剩下相对最相关的表补齐到 {M} 个，**不允许少于 {M} 个**。这是为了下游配比实验的控制变量需要，不是让你判断"够不够用"。
2. 表名必须从上面候选表中精确选择，不要修改、不要添加前缀
3. 不要解释、不要 markdown 包裹、不要 ```json``` 标记
4. 直接输出形如 ["table_name_a", "table_name_b", ...] 的 JSON 数组，**长度严格等于 {M}**

# 你的输出
```

**期望输出**: `["table_a", "table_b", "table_c", "table_d", "table_e"]`（恰好 M 个）

---

## 两种实验路径的 Prompt 流水线对比

### 路径 A：Stage2 开启（s2on_n20_m5）

```
用户 Query
    │
    ▼  [Qwen3-Embedding-8B 向量检索]
    │   从 Redis 召回 20 张候选表
    │
    ▼  [qwen3-32b — Stage2 精筛]
    │   输入: 上述 user prompt（N=20, M=5）
    │   输出: ["t1","t2","t3","t4","t5"]
    │
    ▼  [build_sub_schema]
    │   用 5 张表构建 Sub-Schema（含完整列定义）
    │
    ▼  [xiyansql-7b — SQL 生成]
    │   输入: 上述 system + user prompt（Schema 部分仅 5 张表）
    │   输出: SQL
    │
    ▼
   执行 → 失败则 sql_fix（xiyansql-7b 重试）
```

### 路径 B：Stage2 关闭（s2off_n5）

```
用户 Query
    │
    ▼  [Qwen3-Embedding-8B 向量检索]
    │   从 Redis 召回 5 张（有 score_threshold 过滤）
    │
    ▼  [build_sub_schema]
    │   用 ≤5 张表构建 Sub-Schema
    │
    ▼  [xiyansql-7b — SQL 生成]
    │   输入: 与路径 A 相同的 prompt 结构（仅 Schema 表数/内容不同）
    │   输出: SQL
    │
    ▼
   执行 → 失败则 sql_fix（xiyansql-7b 重试）
```

---

## 关键差异一览

| | xiyansql-7b | qwen3-32b |
|---|---|---|
| 消息数 | 2（system + user） | 1（仅 user） |
| 核心任务 | NL → SQL | 表筛选（N选M） |
| 输入中 Schema 形式 | 完整列定义（表名、列名、类型、注释） | 表名 + KB 描述段落（无列级信息） |
| 输出格式 | SQL 代码块 | JSON 数组 |
| 失败处理 | 按错误类型分类重试 | 解析失败抛异常；不足 M 张时向量排序补足 |
| temperature | 默认 | 0.0 |
| thinking 模式 | 不涉及 | 显式禁用（enable_thinking=false） |

---

## 模型 3：Judge 模型（NL2SQL 正确性评判）

Judge 有两套实现，分别用于离线批量评估和生产环境逐条评判。

---

### 版本 A：批量评估（`scripts/judge_queries.py`）

**消息结构**: system + user（2 条消息）

**调用参数**: `temperature=0.0`, `max_tokens=500`

#### System Message

```
你是一个数据库 SQL 评审专家。你的任务是判断一个 NL2SQL 系统生成的 SQL 语句是否正确回答了用户的自然语言问题。

## 背景
这个系统服务于 CockroachDB 数据库运维场景。数据库中的表都是监控指标表，结构统一：
- greptime_timestamp: 时间戳
- greptime_value: 指标数值（这是核心查询字段）
- instance / node_id / job / store: 标签维度

表名格式为 cockroach_metrics.{指标名}，例如：
- cockroach_metrics.sys_cpu_usage (CPU使用率)
- cockroach_metrics.abortspanbytes (事务Span字节数)
- cockroach_metrics.sql_query_count_total (SQL查询总数)

## 评判标准

你需要根据以下三个维度给出综合判断：

### 1. 表选择是否正确
- NL问的是什么指标？SQL查的是不是对应的表？
- 如果使用了多选题无关的表 → 部分正确或错误

### 2. SQL逻辑是否正确
- SELECT的列是否符合问题要求？
- WHERE条件是否正确反映了筛选意图？
- 聚合函数(SUM/AVG/MAX/COUNT)是否使用正确？
- GROUP BY / ORDER BY 是否合理？
- LIMIT 是否符合要求的数量？
- 时间范围是否正确？

### 3. 是否回答了问题
- SQL执行成功，返回的数据能不能回答用户的问题？
- 执行失败的话，失败原因是什么？

## 输出格式

请严格按照以下JSON格式输出（不要输出其他内容）：

{
  "judgment": "正确|部分正确|错误",
  "table_correct": true,
  "logic_correct": true,
  "answers_question": true,
  "reason": "一句话说明判断理由",
  "error_analysis": "如果错误，分析错误类型：表选错|列选错|聚合错误|条件错误|语法错误|多语句|其他"
}
```

#### User Message

```
## 用户自然语言问题
{用户自然语言查询}

## Schema过滤传给LLM的候选表（共{N}张）
{候选表名列表，逗号分隔，最多显示10张}

## 候选表的结构定义
{relevant_schema — 来自 query_tracker 记录中 xiyansql-7b 实际收到的 Sub-Schema，截断到 3000 字符}
格式同 xiyansql-7b 的 schema 块：
# Table: cockroach_metrics.xxx, 名称: ...描述: ...
[
(column:TYPE),
...
]

## LLM生成的SQL
```sql
{被测系统生成的 SQL}
```

## 执行结果
- 执行状态: 成功 / 失败
- 错误信息: {如执行失败，附错误信息，截断到500字符}
- 修复重试次数: {N}
- 返回列: col1, col2, ...
- 数据预览(前3行):
  [1] col1=val1 | col2=val2 | ...
  [2] col1=val1 | col2=val2 | ...
  [3] col1=val1 | col2=val2 | ...
```

**关键点**：
- `relevant_schema` 来自实验过程中写入 `query_tracker` 的实际 Sub-Schema，即 **xiyansql-7b 看到什么，Judge 就看到什么**（含同样的噪声）
- 候选表名列表也是从 tracker 记录的 `filtered_table_names` 读取，Judge 能知道 xiyansql-7b 被限定了哪些表
- 如果执行成功且有数据预览，Judge 可以用实际返回数据辅助判断

---

### 版本 B：生产环境（`utils/judge.py` — `JudgeModel` 类）

**消息结构**: 仅 user（1 条消息，无 system message）

**调用参数**: `temperature=0.0`

**Schema 来源**：Judge **独立跑一次单阶段 KNN 检索**（judge_top_k 默认 30，比主流程的 N 大），不走 stage2，目的是让 Judge 能看到"应该选但被测系统漏掉的表"。检索使用专属 Redis 索引 `xiyan_schema_cockroach_judge`（固定 noise0），表描述从 `.noisebackup/cockroach_metrics_knowledge_noise0.json` 强制加载，**与主流程的噪声实验完全隔离**。

#### User Message（唯一消息）

```
你是数据库 SQL 审查员。你的任务是判断给定 SQL 是否正确回答了用户问题。

# 用户问题
{用户自然语言查询}

# 相关数据表 Schema（仅供你参考，可能包含与问题无关的表）
{Judge 独立检索的 Schema，格式：}
【通用列说明】cockroach_metrics 下的表为 Prometheus 指标格式，列结构高度统一：
- 所有表都有：greptime_timestamp (TIMESTAMP, 采集时间), greptime_value (FLOAT, 指标值),
  instance (STRING, 采集实例地址，形如 IP:port), job (STRING, 采集任务名)
- 99.7% 的表有：node_id (STRING, CockroachDB 节点 ID)
- 约 30% 的表额外有：store (STRING, 存储 ID)
- 约 6% 的表额外有：le (FLOAT, 直方图桶上界)

1. cockroach_metrics.sys_cpu_usage
   {KB embedding_content 全文}
2. cockroach_metrics.sql_query_count_total
   {KB embedding_content 全文}
...
（共 judge_top_k 张，默认 30 张）

# 被审查的 SQL
```sql
{被测系统生成的 SQL}
```

# SQL 执行结果摘要
- 执行状态：成功 / 失败
- 返回行数：{N}
- 前几行预览：
{数据预览文本，最多3行}

# 判断要求
请仔细分析这条 SQL 是否正确回答了用户问题，并按以下规则输出：

1. **correct**（bool）：True 表示 SQL 正确回答了问题；False 表示存在错误
2. **category**（必须从下面 7 类中选一个）：
   - correct：SQL 正确
   - schema_wrong：用错了表（FROM/JOIN 选错了主体表）
   - column_wrong：表对，但列引用错误
   - aggregation_wrong：聚合维度/GROUP BY/HAVING 错误
   - filter_wrong：WHERE 条件错误（漏限定、错限定、范围错）
   - syntax：SQL 语法层错误（导致跑不通）
   - other：其它
3. **reason**（中文，1~2 句）：简要说明判断依据

# 输出格式
**严格输出 JSON 对象**，**不要 markdown 包裹、不要解释、不要 ```json``` 标记**。
形如：{"correct": true, "category": "correct", "reason": "..."}

# 你的输出
```

**期望输出**: `{"correct": true, "category": "correct", "reason": "SQL正确查询了..."}`

---

### 两个 Judge 版本的关键差异

| | 批量评估（judge_queries.py） | 生产环境（judge.py） |
|---|---|---|
| 消息数 | 2（system + user） | 1（仅 user） |
| Schema 来源 | 复用 xiyansql-7b 实际收到的 Sub-Schema（从 tracker 读取） | Judge 独立 RAG（K=30，单阶段） |
| Schema 格式 | 完整列定义（表名、列名、类型、注释） | 通用列说明 + KB 描述段落（skip_lazy_load 路径，无逐表列定义） |
| 噪声影响 | ✅ 受影响（直接读 tracker 中 xiyansql-7b 的 schema） | ❌ 不受影响（独立 Redis 索引 + noise0 备份，已隔离） |
| 评判粒度 | 3级（正确/部分正确/错误）+ 3维度 bool | 2级（correct bool）+ 7类错误分类 |
| 输出字段 | judgment, table_correct, logic_correct, answers_question, reason, error_analysis | correct, category, reason |
| 模型配置 | 通过环境变量 `JUDGE_MODEL`（默认 gpt-4o） | 通过 config.yml 的 judge 段配置 |

---

## 三个模型的 Schema 数据流汇总

```
json/cockroach/cockroach_metrics_knowledge.json   ← switch_cockroach_noise.sh 替换（含噪声）
         │
         ▼
SchemaRetriever.kb_table_descriptions (MCP Server 启动时一次性加载)
         │
         ├──▶ xiyansql-7b 的 table comment
         │    build_sub_schema() → comment 为空 → 回退到 kb_table_descriptions
         │
         ├──▶ qwen3-32b 的候选表描述
         │    retrieve_and_build() → 注入 embedding_content 到 stage1 results
         │
         └──▶ Judge 批量评估 的 schema（直接读 tracker 中的 relevant_schema）
              即间接来自 kb_table_descriptions

═══════════════════════════════════════════════════════
Judge 生产环境 — 已隔离（不受噪声切换影响）
═══════════════════════════════════════════════════════

json/cockroach/.noisebackup/cockroach_metrics_knowledge_noise0.json  ← 永不修改
         │
         ├──▶ Judge 专属 Redis 索引: xiyan_schema_cockroach_judge
         │    build_judge_index.sh 构建，switch 脚本不触碰
         │
         └──▶ Judge 专属 kb_table_descriptions
              run_judge_batch.py 强制从 noise0 备份加载，跳过活跃文件
```

**结论：切换噪声后，主流程（xiyansql-7b、qwen3-32b）的描述会随之改变；Judge 生产环境（`run_judge_batch.py`）不受影响，始终使用 noise0 数据。**
