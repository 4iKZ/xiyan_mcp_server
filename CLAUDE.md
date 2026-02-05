# XiYan MCP Server 开发指南

## 项目概述
XiYan MCP Server 是一个基于 Model Context Protocol (MCP) 的自然语言查询服务，支持将自然语言转换为 SQL 并执行于 GreptimeDB 等时序数据库。

## 构建与运行命令
- **安装依赖**: `pip install -e .` 或 `pip install -r requirements.txt`
- **运行服务器 (Stdio)**: `python -m xiyan_mcp_server`
- **运行服务器 (HTTP)**: `python -m xiyan_mcp_server.server streamable-http --host <host> --port <port>`
- **环境变量配置**:
  - `PYTHONPATH`: 需包含 `src` 目录
  - `YML`: 配置文件路径，默认为 `src/xiyan_mcp_server/config.yml`
  - `HF_ENDPOINT`: 可选，HuggingFace 镜像地址

## 测试命令
- **执行通用测试**: `python test.py`
- **执行自然语言查询测试**: `python test_natural_language_queries.py`
- **执行 CockroachDB 测试**: `python test_cockroachdb.py`
- **执行简单查询测试**: `python test_simple.py`

## 核心脚本
- **知识库索引**: `python scripts/index_knowledge.py` (将 `json/` 下的模式信息索引至 Redis)
- **Schema 检查**: `python check_schema.py`
- **表结构检查**: `python check_tables.py`

## 代码规范
- **命名规范**: 使用 `snake_case` 命名变量和函数，`PascalCase` 命名类。
- **日志记录**: 使用 `logger_util.py` 提供的日志工具，关键路径需记录 INFO 或 DEBUG 日志。
- **错误处理**: 数据库操作需包含 try-except 块，并在失败时尝试 SQL 修复逻辑。
- **类型注解**: 鼓励在函数定义中使用类型注解。

## Git 提交规范（⚠️ 极其重要）
- **绝对禁止**在 git commit 中添加 `Co-Authored-By: Claude Sonnet 4.5` 或任何 AI 作者标记
- **所有提交必须只归属于用户本人的 GitHub 账号 (4iKZ <syhaox@outlook.com>)**
- **原因**: GitHub 会永久记录 Contributors 统计，一旦添加 AI 作者就无法删除
- **正确的提交格式**:
  ```bash
  git commit -m "feat: 描述"
  # 错误：git commit -m "feat: 描述" -m "Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>"
  ```

## 项目架构
- `src/xiyan_mcp_server/server.py`: MCP 服务入口，定义 Resource 和 Tool。
- `src/xiyan_mcp_server/utils/`:
  - `db_util.py`: 数据库连接池管理。
  - `llm_util.py`: LLM API 调用封装。
  - `schema_retriever.py`: 基于向量检索的 Schema 过滤逻辑。
  - `db_source.py`: 数据库元数据提取与查询执行。
- `json/`: 存放数据库 Schema 知识库。
- `scripts/`: 存放数据维护和索引脚本。
