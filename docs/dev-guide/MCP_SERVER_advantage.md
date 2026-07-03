你可以了解一下部署在这个虚拟机上的xiyan
  mcp，也可以看一下文件MCP_SERVER_GUIDE.md里的介绍，你可以给我说一下为了提高xiyan
  mcp的功能精度，都是用了哪些方法

优点：
  基于对代码和文档的分析，XiYan MCP Server 在 Text-to-SQL
  的精度提升上采用了以下几个层面的方法：

  ---
  1. 专用 Text-to-SQL 微调模型

  使用 XiYanSQL-QwenCoder 系列模型（基于 QwenCoder 微调），专门针对 SQL
  生成任务优化。当前配置使用云端 32B
  版本（XGenerationLab/XiYanSQL-QwenCoder-32B-2504），相比本地 14B 版本有更强的 SQL
  生成能力。

  2. Schema 语义过滤（核心精度提升手段）

  这是提升精度最关键的方法。当数据库有 2343+ 张表时，把所有 Schema 塞进 prompt 会超出
  LLM 上下文窗口，且噪声表会严重干扰 SQL 生成。

  实现流程：
  - 离线阶段：用 Qwen3-Embedding-8B（4096 维向量）将所有表的描述信息向量化，存入 Redis
   的向量索引
  - 在线阶段：用户的自然语言查询 → Embedding 向量化 → Redis KNN 向量检索 → 返回
  top_k（5）张最相关的表 → 构建 Sub-Schema
  - 支持按 system_prefix 做数据库级别的 TAG 过滤，缩小检索范围
  - 设置 score_threshold（0.4）过滤低相似度结果

  效果：将 2000+ 张表的完整 Schema 裁剪为仅 5 张相关表的精简 Schema，大幅减少 LLM
  的上下文噪声。

  3. SQL 错误自动修复循环

  当生成的 SQL 执行失败时，进入最多 5 次的修复循环：

  初始 SQL 执行失败
    → LLM 分析错误信息 + 原始 Schema + 原始问题
    → 生成修正 SQL（仅修复语法错误，不改逻辑）
    → 重新执行
    → 成功则退出，失败则继续下一轮

  修复 prompt 明确要求"仅修复语法错误，不允许改变 SQL 的逻辑"，保持查询语义不变。

  4. 多层 Prompt 工程

  在 SQL 生成的 system prompt 中注入了精确的领域知识：
  - 表名已包含完整 schema 前缀（如 sundb_metrics.table_name），禁止添加额外前缀
  - 限定只生成一个 SQL 语句
  - 根据数据库方言（greptimedb/mysql/postgresql）定制角色描述
  - 提供完整的 M-Schema（含字段类型、主键、外键、注释、示例值）

  5. 丰富的 Schema 元数据（M-Schema）

  不仅仅是表名和列名，M-Schema 还包含：
  - 列的 distinct 示例值（最多 5 个），帮助 LLM 理解数据内容和格式
  - 主键 / 外键关系，帮助 LLM 生成正确的 JOIN
  - 表和列的注释
  - 类型映射：GreptimeDB 特有类型（如 INT64、FLOAT64）映射为标准 SQL 类型

  6. SQL 多层安全验证

  在保证安全的同时也提升了精度：
  - 禁止非 SELECT 语句（DROP、DELETE、UPDATE 等）
  - 禁止多语句执行
  - 表名白名单验证（Resource 接口）
  - 标识符格式验证（防注入），同时过滤注释和危险字符

  7. GreptimeDB 专用适配

  针对 GreptimeDB 的兼容性问题做了专门处理：
  - 自定义 SQLAlchemy 方言，绕过 pg_catalog 不兼容问题
  - 使用 information_schema 直接查询元数据
  - 支持 MySQL 协议（端口 4002）和 PostgreSQL 协议（端口 4003）两种连接方式

  8. Schema 缓存（10 分钟 TTL）

  带 TTL 的内存缓存避免频繁查询数据库元数据，同时保证 Schema 在一定时间内保持新鲜。

  9. 延迟列加载

  数据库有 2343+ 张表，初始化时只加载表名列表，列的详细信息（字段类型、示例值等）在
  Schema 过滤命中时才按需加载，避免启动时的巨大开销。

  ---
  总结：精度提升的核心思路是 "先检索再生成"——通过 Embedding 向量检索将上下文从 2000+
  张表压缩到 5 张相关表，让 LLM 在高质量、低噪声的 Schema 上下文中生成
  SQL，再配合错误自修复循环兜底。
