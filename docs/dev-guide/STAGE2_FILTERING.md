# 二级筛选（Stage 2 Schema Filtering）

> 在向量粗召回（Stage 1）之后，用本地小模型对候选表做一次精筛（Stage 2），
> 用来支撑论文 Exp 3「(N, M) 配比对 NL2SQL 准确率的影响」的实验。

## 一、为什么需要二级筛选

**旧路径（单阶段）**：用户问题 → Embedding → Redis KNN top-k → 取这 k 张表的 Schema → 拼 Prompt 给 NL2SQL 模型。

问题：
- 想测「Schema 噪声多少时模型还能答对」时，单阶段下表的数量 = 噪声量 = 信号量，无法解耦。
- top-k 大了引入无关表，小了可能漏召回真正用得到的表。

**新路径（两阶段）**：
```
query
  → Stage 1: 向量粗召回 N 张候选表（控制召回率）
  → Stage 2: 本地小模型从 N 张里精筛 M 张（控制噪声/精度）
  → 构建 Sub-Schema 给 NL2SQL 主模型
```

实验维度变成 `(N, M)` 配比：
- N 固定、M 变化 → 衡量「同等召回下，给主模型多少张表最合适」
- M 固定、N 变化 → 衡量「精筛器对召回噪声的容忍度」

## 二、改动总览

| 文件 | 变更类型 | 说明 |
|------|---------|------|
| `src/xiyan_mcp_server/config.yml` | 修改 | 在 `schema_filter` 下新增 `stage2` 子配置 |
| `src/xiyan_mcp_server/utils/stage2_filter.py` | **新增** | `Stage2Filter` 类 + `Stage2FilterError` |
| `src/xiyan_mcp_server/utils/schema_retriever.py` | 修改 | `__init__` 接收 `stage2_filter`；`retrieve_and_build` 返回三元组并支持运行时覆盖 |
| `src/xiyan_mcp_server/utils/query_tracker.py` | 修改 | `record_query` 新增 6 个 stage2 追踪字段 |
| `src/xiyan_mcp_server/server.py` | 修改 | 新增 `get_stage2_filter()` 单例；`call_xiyan` / `get_data` / `query_and_upload_to_hdfs` 全链路透传开关与 N/M |

### 2.1 Stage2Filter（新文件）

`utils/stage2_filter.py`

- 调用方式：OpenAI 兼容 SDK（`from openai import OpenAI`），可对接本地 vLLM / OpenAI-compat endpoint
- Prompt 模板：列出 N 张候选表的 `table_name + friendly_name + description`（description 截断到 200 字符控制长度），要求模型输出长度为 M 的 JSON 数组
- 解析容错：strip markdown 代码块 → 直接 `json.loads` → 正则提第一段 `[...]` 兜底
- **失败模式：抛 `Stage2FilterError` 不降级**（故意这样，避免污染配比实验归因）
- 候选数 ≤ M 时跳过模型调用，直接返回

### 2.2 SchemaRetriever（修改）

`utils/schema_retriever.py`

- `__init__` 多接收一个 `stage2_filter` 参数；从 `config["stage2"]` 读取 `enabled / stage1_top_n / stage2_top_m`
- 启动期防御：`enabled=true` 但没注入 filter → warning 并强制关闭
- `retrieve_and_build` 签名变更：
  ```python
  def retrieve_and_build(
      self, query, database=None, system_prefix=None,
      stage2_enabled=None, stage1_top_n=None, stage2_top_m=None,
  ) -> Tuple[List[str], str, Dict]:
  ```
- 返回三元组：`(final_table_names, sub_schema, meta)`
- `meta` 字段：`stage2_enabled / stage1_top_n / stage2_top_m / stage1_tables / stage2_tables / stage2_model`
- 运行时强开但没 filter → `RuntimeError`

### 2.3 QueryTracker（修改）

`utils/query_tracker.py` 的 `record_query` 新增 6 个可选参数，写入 JSONL：
```python
stage2_enabled, stage1_top_n, stage2_top_m,
stage1_tables, stage2_tables, stage2_model
```

### 2.4 server.py（修改）

- `get_stage2_filter()` 单例（threading.Lock 保护）
- `get_schema_retriever()` 仅当 `stage2.enabled=true` 时才构造并注入 Stage2Filter
- `call_xiyan(query, ..., stage2_enabled=None, stage1_n=None, stage2_m=None)` 透传到 retriever
- `get_data` / `query_and_upload_to_hdfs` MCP 工具暴露同样三个参数
- 两处 `tracker.record_query()` 调用都从 `retrieval_meta` 取字段写入

## 三、配置项

`src/xiyan_mcp_server/config.yml`：

```yaml
schema_filter:
  enabled: true
  knowledge_dir: "json"
  top_k: 5                 # 单阶段模式使用；启用 stage2 时被忽略
  score_threshold: 0.4

  stage2:
    enabled: false         # 总开关：false = 完全走旧单阶段路径
    stage1_top_n: 20       # 向量粗召回 N 张
    stage2_top_m: 5        # 模型精筛保留 M 张
    model_name: "qwen3-14B"
    api_url: "http://localhost:8002/v1/"
    api_key: "EMPTY"       # 本地服务通常不校验，SDK 需要非空字符串
    temperature: 0.0       # 筛选任务建议 0
    timeout: 60
```

## 四、使用方式

### 方式 A：改 config.yml（持久生效，需重启）

适合定下一组配置长期跑。改完 `stage2.enabled / stage1_top_n / stage2_top_m` 后重启：

```bash
PYTHONPATH=src python -m xiyan_mcp_server
# 或 HTTP 模式
PYTHONPATH=src python -m xiyan_mcp_server.server streamable-http --host 0.0.0.0 --port 8000
```

启动日志会打印：
```
二级筛选已启用: model=qwen3-14B, N=20, M=5
```

### 方式 B：MCP 工具参数临时覆盖（推荐用于批量配比实验）

`get_data` 与 `query_and_upload_to_hdfs` 暴露三个可选参数：

| 参数 | 类型 | None 时行为 | 传值时行为 |
|------|------|-------------|-----------|
| `stage2_enabled` | `bool` | 沿用 yml | 本次调用覆盖 |
| `stage1_n` | `int` | 沿用 yml | 本次覆盖 N |
| `stage2_m` | `int` | 沿用 yml | 本次覆盖 M |

示例：

```python
# 基线：单阶段
get_data(query="CPU 使用率最高的 5 个节点", stage2_enabled=False)

# 两阶段 N=20, M=5
get_data(query="...", stage2_enabled=True, stage1_n=20, stage2_m=5)

# 两阶段 N=30, M=3（同一条 query 跑不同配比对比）
get_data(query="...", stage2_enabled=True, stage1_n=30, stage2_m=3)
```

外层用 Python 脚本循环不同 (N, M) 批跑，不需要重启服务。

## 五、追踪记录字段

每次调用会在 `query_tracker_logs/query_tracker_YYYY-MM-DD.jsonl` 追加一条记录：

```json
{
  "timestamp": "2026-06-11T10:30:00",
  "nl_query": "CPU 使用率最高的 5 个节点",
  "initial_sql": "SELECT ...",
  "exec_success": true,
  "tables_used": ["cockroach_metrics.sys_cpu_usage"],

  "stage2_enabled": true,
  "stage1_top_n": 20,
  "stage2_top_m": 5,
  "stage1_tables": ["cockroach_metrics.a", "cockroach_metrics.b", "..."],
  "stage2_tables": ["cockroach_metrics.sys_cpu_usage", "cockroach_metrics.b"],
  "stage2_model": "qwen3-14B",

  "filtered_table_names": ["cockroach_metrics.sys_cpu_usage", "cockroach_metrics.b"],
  "relevant_schema": "# Table: ...",
  "total_latency_ms": 1234.5
}
```

聚合分析时按 `(stage1_top_n, stage2_top_m)` 分桶统计 `exec_success`、`tables_used ⊆ stage2_tables` 等指标即可得到配比对准确率的影响曲线。

## 六、失败模式说明

| 场景 | 行为 |
|------|------|
| Stage2Filter 模型调用失败 | 抛 `Stage2FilterError`，不降级 |
| 模型输出无法解析为 JSON 数组 | 抛 `Stage2FilterError` |
| 模型筛出的表名全部不在候选中 | 抛 `Stage2FilterError` |
| 部分表名不在候选 | 记 warning，保留能匹配上的 |
| `stage2.enabled=true` 但启动时没注入 Stage2Filter | 启动期 warning + 强制关闭 stage2 |
| 运行时 `stage2_enabled=True` 但 retriever 没 filter | 抛 `RuntimeError` |
| 候选数 ≤ M | 跳过模型调用，直接返回 |

**为什么不降级**：实验目的是测「这组 (N, M) 在精筛器辅助下的准确率」。一旦失败回退到 N 张全量，记录里就分不清「答对是因为配比合理」还是「答对是因为退化成了大召回」。

## 七、运行前 checklist

1. 本地 qwen3-14B（或替代模型）已部署在 `stage2.api_url` 指向的 endpoint，OpenAI 兼容格式
2. 配置中 `model_name` 与服务端实际加载的模型名一致
3. Redis 已用对应 `database.system` 重建索引（这个跟 stage2 无关，但是 retriever 工作的前提）
4. `openai` SDK 已安装（`pip install openai`，本项目 llm_util 已用，正常都是装好的）

## 八、相关文件索引

- 实验设计文档：`docs/RESEARCH_FRAMEWORK.md`
- 实验执行计划：`docs/EXPERIMENT_PLAN.md`
- Stage2 实现：`src/xiyan_mcp_server/utils/stage2_filter.py`
- 检索编排：`src/xiyan_mcp_server/utils/schema_retriever.py`（`retrieve_and_build`）
- 追踪写入：`src/xiyan_mcp_server/utils/query_tracker.py`
- 工具入口：`src/xiyan_mcp_server/server.py`（`get_data` / `query_and_upload_to_hdfs`）
