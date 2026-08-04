# Repository Guidelines

XiYan MCP Server converts natural-language questions into SQL and executes them against GreptimeDB/CockroachDB (Python 3.13).

## Project Structure & Module Organization

- `src/xiyan_mcp_server/` — package source. `server.py` is the MCP entry point, `runtime.py` holds the runtime contract, and `utils/` contains database, LLM, embedding, schema, HDFS, and logging modules.
- `tests/` — pytest suite split into `unit/`, `integration/`, and `e2e/` layers.
- `scripts/` — maintenance and experiment tools (knowledge indexing, judge/batch queries, parquet/HDFS uploads).
- `json/` — schema knowledge base; `docs/` — user and developer guides.
- `dataset/`, `logs/`, and `query_*` output are gitignored runtime artifacts.

## Build, Test, and Development Commands

```bash
pip install -e ".[dev]"                            # package + dev dependencies
python -m xiyan_mcp_server                         # run in stdio mode
python -m xiyan_mcp_server.server streamable-http --host 0.0.0.0 --port 8000
pytest                                             # unit tests only (default)
pytest -m integration                              # requires MCP server on :8000
pytest -m e2e                                      # end-to-end batch queries
pytest --cov=xiyan_mcp_server --cov-report=term-missing
docker build -t xiyan-mcp-server .                 # container build
docker compose up -d                               # local service stack
```

Include `src/` in `PYTHONPATH`. Copy `src/xiyan_mcp_server/config.example.yml` to `config.yml` (or set `YML`) before running.

## Coding Style & Naming Conventions

- Follow PEP 8; lint with `ruff`, format with `black`.
- Use `snake_case` for functions and variables, `PascalCase` for classes; add type annotations.
- Log through `utils/logger_util.py`; wrap database operations in try/except with repair logic.
- Keep SQL-generation prompts in dedicated prompt modules rather than scattered through handlers.

## Testing Guidelines

- Framework: pytest + pytest-asyncio. Unit tests run by default; integration/e2e are opt-in via markers.
- Name test files `test_<module>.py` under the matching `tests/` layer; put shared fixtures in that layer's `conftest.py`.
- Use `tmp_path` for temp files instead of hardcoded paths. No coverage threshold is enforced; add tests for every new module and review `pytest --cov` output.

## Commit & Pull Request Guidelines

- Git history follows Conventional Commits with Chinese descriptions: `feat:`, `fix:`, `docs:`, `chore:`, `refactor:`, `perf:`, `style:`, `test:`.
- Never add `Co-Authored-By` or any AI attribution to commits.
- PRs should have a concise description, link related issues, note behavioral or breaking changes, and include screenshots for prompt or UI changes.

## Security & Configuration Tips

- `config.yml` is gitignored — never commit real credentials or API keys; use env vars such as `MODELSCOPE_API_KEY`.
- SQL passed to the server is validated with `sqlparse`; preserve that guard when modifying the executor.
