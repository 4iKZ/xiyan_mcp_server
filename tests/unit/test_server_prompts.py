"""server.py 提示词 & 验证逻辑单元测试

不依赖 server.py 导入（避免 yaml 等外部依赖），
通过源码读取方式验证关键提示内容和执行逻辑。
"""

import re
from pathlib import Path

SRC_DIR = Path(__file__).parent.parent.parent / "src" / "xiyan_mcp_server"
SERVER_PY = SRC_DIR / "server.py"


def _read_server_source() -> str:
    return SERVER_PY.read_text(encoding="utf-8")


# ── 方言规则（dialect_rules）集成测试 ─────────────────────────────

class TestDialectRules:
    """验证 GreptimeDB 方言规则包含所有必需的限制说明"""

    @classmethod
    def setup_class(cls):
        cls.source = _read_server_source()
        # 提取 dialect_rules 字符串内容
        # 从 "dialect_rules = \"\"\"" 到下一个 "\"\"\""
        m = re.search(r'dialect_rules = """(.*?)"""', cls.source, re.DOTALL)
        cls.rules = m.group(1) if m else ""

    def test_rules_not_empty(self):
        assert len(self.rules) > 100, "dialect_rules 不应为空"

    def test_rules_contain_datetime_rules(self):
        assert "DATE()" in self.rules
        assert "NOW()" in self.rules or "CURRENT_DATE" in self.rules
        assert "date_trunc" in self.rules, \
            "应包含 date_trunc 作为 DATE() 的替代方案"

    def test_rules_contain_function_restrictions(self):
        """改动 1：规则应包含函数限制部分 (h)"""
        assert "approx_percentile()" in self.rules, \
            "应包含 approx_percentile 替换为 approx_percentile_cont 的说明"
        assert "approx_percentile_cont" in self.rules, \
            "应包含 approx_percentile_cont 的正确用法"
        assert "variance()" in self.rules, \
            "应包含 variance 替换为 var_samp 的说明"
        assert "var_samp()" in self.rules or "var_pop()" in self.rules, \
            "应包含 var_samp/var_pop 替代方案"
        assert "WITHIN GROUP" in self.rules, \
            "approx_percentile_cont 应使用 WITHIN GROUP (ORDER BY ...) 语法"
        assert "median" in self.rules, \
            "应包含 median(列) 简写语法说明"

    def test_rules_contain_merge_prohibition(self):
        """改动 1：规则应禁止 MERGE/DML 语句"""
        assert "MERGE" in self.rules, \
            "应包含 MERGE 语句不支持的说明"
        assert "SELECT" in self.rules, \
            "应提示只允许 SELECT"


# ── function_not_found 修复提示验证 ────────────────────────────────

class TestFunctionNotFoundFixPrompt:
    """验证 function_not_found 修复提示包含 DataFusion 函数指引"""

    @classmethod
    def setup_class(cls):
        cls.source = _read_server_source()
        cls.prompt = cls._extract_fn_prompt()

    @classmethod
    def _extract_fn_prompt(cls):
        """提取 function_not_found 对应的 PROMPT_VARIANTS 条目"""
        # 从 "function_not_found": ( ... ), 匹配到下一个 ),
        m = re.search(
            r'"function_not_found":\s*\((.*?)\n\s*\),',
            cls.source, re.DOTALL,
        )
        return m.group(1) if m else ""

    def test_fn_prompt_not_empty(self):
        assert len(self.prompt) > 50, \
            "function_not_found 修复提示不应为空"

    def test_fn_prompt_lists_supported_functions(self):
        """改动 2：修复提示应列出 GreptimeDB 支持的聚合函数"""
        assert "COUNT" in self.prompt, "应列出 COUNT"
        assert "SUM" in self.prompt, "应列出 SUM"
        assert "AVG" in self.prompt or "avg" in self.prompt, "应列出 AVG"
        assert "STDDEV" in self.prompt, "应列出 STDDEV"
        assert "var_samp" in self.prompt, "应列出 var_samp"
        assert "var_pop" in self.prompt, "应列出 var_pop"
        assert "approx_percentile_cont" in self.prompt, \
            "应列出 approx_percentile_cont"

    def test_fn_prompt_gives_concrete_alternatives(self):
        """改动 2：应包含具体替代写法指引"""
        assert "approx_percentile_cont(0.95" in self.prompt, \
            "应包含 approx_percentile_cont 的正确参数格式示例"
        assert "ORDER BY" in self.prompt or "LIMIT" in self.prompt, \
            "应包含 ORDER BY + LIMIT 近似分位数的替代方案"
        assert "WITHIN GROUP" in self.prompt, \
            "修复提示应使用 WITHIN GROUP (ORDER BY ...) 语法"


# ── 空 SQL 保护机制验证 ───────────────────────────────────────────

class TestEmptySqlGuard:
    """验证 extract_sql_from_qwen 之后的空 SQL 检查"""

    @classmethod
    def setup_class(cls):
        cls.source = _read_server_source()

    def test_empty_sql_check_exists(self):
        """改动 3：sql_query 提取后应有空值检查"""
        assert "not sql_query or not sql_query.strip()" in self.source, \
            "应包含空 SQL 检查逻辑"
        assert "LLM 生成" in self.source and "空 SQL" in self.source, \
            "应包含 LLM 生成空 SQL 的错误提示"

    def test_empty_sql_raises_valueerror(self):
        """空 SQL 应触发 ValueError（进入 except ValueError 分支）"""
        # 检查 raise ValueError 后面是否包含空 SQL 相关消息
        assert 'raise ValueError' in self.source
        # 验证 ValueError 消息模式
        m = re.search(
            r'raise ValueError\("LLM 生成的 SQL 为空[^"]*"\)',
            self.source,
        )
        assert m is not None, "应包含 raise ValueError 调用"


# ── ValueError → fix loop 验证 ─────────────────────────────────────

class TestValueErrorRouting:
    """验证 ValueError 能进入修复循环（改动 4+5）"""

    @classmethod
    def setup_class(cls):
        cls.source = _read_server_source()

    def test_fetch_wrapped_with_valueerror_catch(self):
        """改动 5：fetch() 调用应包裹在 try-except ValueError 中"""
        fetch_calls = re.findall(
            r'try:\s+fetch_result = db_env\.database\.',
            self.source,
        )
        assert len(fetch_calls) >= 1, \
            "至少一处 fetch() 调用应包裹在 try-except ValueError 中"

    def test_valueerror_caught_in_fix_loop(self):
        """fix loop 中的 fetch() 也应包裹 ValueError"""
        # fix loop 中的 repaired_sql fetch 被 try 包裹
        fix_try = re.findall(
            r'try:\s+fetch_result = db_env\.database\.fetch\(repaired_sql\)',
            self.source,
        )
        assert len(fix_try) >= 1, \
            "fix loop 中的 fetch() 也应包裹 try-except ValueError"

    def test_except_valueerror_block_exists(self):
        """独立的 except ValueError 分支存在"""
        assert "except ValueError as e:" in self.source, \
            "应有独立的 except ValueError 分支"


# ── _get_retry_strategy 集成验证 ───────────────────────────────────

class TestRetryStrategyCoverage:
    """验证各种 error_type 的 retry 策略正确"""

    def test_retry_strategy_importable(self):
        """RETRY_STRATEGY 字典应存在且包含所有错误类型"""
        import ast
        tree = ast.parse(_read_server_source())

        # 找到 RETRY_STRATEGY 字典
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "RETRY_STRATEGY":
                        keys = [k.value for k in node.value.keys
                                if isinstance(k, ast.Constant)]
                        assert "function_not_found" in keys
                        assert "unsupported_statement" in keys
                        assert "syntax_error" in keys
                        assert "timeout" in keys
                        assert "connection_error" in keys
                        # syntax_error 应是唯一使用 prev 的
                        return
        pytest.fail("RETRY_STRATEGY 字典未找到")

    def test_syntax_error_uses_prev_baseline(self):
        """syntax_error 应使用 prev 基线（增量修复）"""
        assert '"syntax_error"' in _read_server_source()
        # 验证 base_sql: "prev"
        m = re.search(
            r'"syntax_error".*?"base_sql":\s*"prev"',
            _read_server_source(),
        )
        assert m is not None, "syntax_error 应使用 prev 基线"


# ── 集成验证：所有改动文件语法有效 ─────────────────────────────────

class TestSyntaxValid:
    def test_server_py_compiles(self):
        import py_compile
        py_compile.compile(str(SERVER_PY), doraise=True)

    def test_query_tracker_py_compiles(self):
        import py_compile
        qt = SRC_DIR / "utils" / "query_tracker.py"
        py_compile.compile(str(qt), doraise=True)
