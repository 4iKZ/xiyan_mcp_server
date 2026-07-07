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

    def test_rules_prohibit_union(self):
        """改动 2 (本次)：规则应禁止 UNION/UNION ALL/多语句"""
        assert "UNION" in self.rules, \
            "应包含 UNION 关键字的禁止说明"
        assert "禁止" in self.rules, \
            "应使用'禁止'明确的否定词"
        assert "CASE WHEN" in self.rules, \
            "应推荐 CASE WHEN 并列聚合改写"

    def test_rules_have_distinct_orderby_with_example(self):
        """改动 3 (本次)：DISTINCT+ORDER BY 规则应附反例代码"""
        assert "SELECT DISTINCT" in self.rules
        assert "ORDER BY" in self.rules
        assert "must appear in select list" in self.rules, \
            "应包含完整报错信息作为反例锚点"

    def test_rules_prohibit_multi_statement(self):
        """改动 4 (本次)：规则应禁止分号分隔的多条 SQL"""
        assert "分号" in self.rules, \
            "应明确禁止用分号分隔多个 SELECT 语句"
        assert "只允许" in self.rules, \
            "应说明只允许单个 SQL 语句"

    def test_rules_contain_increment_pattern(self):
        """改动 5 (本次)：规则 k) 应覆盖增量/趋势/变化语义"""
        assert "LAG(" in self.rules, \
            "规则 k 应包含 LAG 窗口函数示例"
        assert "增量" in self.rules or "变化" in self.rules, \
            "规则 k 应针对增量/变化类 NL"
        assert "LAG(GREPTIME_VALUE)" in self.rules, \
            "规则 k 应给出 LAG 窗口函数的具体写法"

    def test_rules_contain_time_bucket_pattern(self):
        """改动 6 (本次)：规则 l) 应覆盖时间粒度聚合"""
        assert "DATE_TRUNC" in self.rules, \
            "规则 l 应包含 DATE_TRUNC"
        assert "每小时" in self.rules, \
            "规则 l 应覆盖每小时聚合场景"
        assert "date_trunc('hour'" in self.rules.lower(), \
            "规则 l 应给出 'hour' 粒度具体语法"

    def test_rules_contain_multi_table_join_hint(self):
        """改动 7 (本次)：规则 m) 应覆盖多指标联合查询 + _count/_sum 配对"""
        assert "结合" in self.rules or "对比" in self.rules, \
            "规则 m 应识别结合/对比类 NL"
        assert "JOIN" in self.rules, \
            "规则 m 应提示使用 JOIN"
        assert "_count" in self.rules and "_sum" in self.rules, \
            "规则 m 应提及配对表 _count/_sum 模式"

    def test_rules_contain_group_by_hint(self):
        """改动 8 (本次)：规则 n) 应覆盖分组聚合"""
        assert "GROUP BY" in self.rules, \
            "规则 n 应包含 GROUP BY"
        assert "每个" in self.rules or "按" in self.rules, \
            "规则 n 应识别每个/按 分组类 NL"


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


# ── 本次 prompt 优化方案验证 ───────────────────────────────────────

class TestPromptOptimizationChanges:
    """验证 prompt 优化方案中各项改动"""

    @classmethod
    def setup_class(cls):
        cls.source = _read_server_source()
        m = re.search(r'dialect_rules = """(.*?)"""', cls.source, re.DOTALL)
        cls.rules = m.group(1) if m else ""

    def test_intersect_except_prohibited(self):
        """Task 9: 规则 5.10 应禁止 INTERSECT / EXCEPT"""
        assert "INTERSECT" in self.rules, "应禁止 INTERSECT"
        assert "EXCEPT" in self.rules, "应禁止 EXCEPT"

    def test_table_name_case_sensitivity(self):
        """Task 8: 规则 1 应包含表名大小写提示"""
        assert "区分大小写" in self.source, "规则 1 应包含表名区分大小写提示"
        assert "schedules_backup" in self.source, "应给出大小写示例"

    def test_distinct_orderby_in_retry_strategy(self):
        """Task 2: RETRY_STRATEGY 应包含 distinct_orderby_error"""
        assert '"distinct_orderby_error"' in self.source, \
            "RETRY_STRATEGY 应包含 distinct_orderby_error 条目"

    def test_distinct_orderby_prompt_variant_exists(self):
        """Task 2: PROMPT_VARIANTS 应包含 distinct_orderby_error 变体"""
        m = re.search(
            r'"distinct_orderby_error":\s*\((.+?)\)\s*,',
            self.source, re.DOTALL,
        )
        prompt = m.group(1) if m else ""
        assert "GROUP BY" in prompt and "聚合函数" in prompt, \
            "distinct_orderby_error prompt 应包含 GROUP BY 替代方案"

    def test_sql_fix_signature_has_dialect_rules(self):
        """Task 3: sql_fix 函数签名应包含 dialect_rules 参数"""
        assert "dialect_rules: str" in self.source, \
            "sql_fix 签名应包含 dialect_rules 参数"
        assert "time_rules: str" in self.source, \
            "sql_fix 签名应包含 time_rules 参数"

    def test_sql_fix_injects_dialect_rules(self):
        """Task 3: sql_fix 应在 system_prompt 中注入 dialect_rules"""
        assert "if dialect_rules:" in self.source, \
            "sql_fix 应检查并注入 dialect_rules"

    def test_table_not_found_injects_candidates(self):
        """Task 4: table_not_found 应注入候选表名列表"""
        assert "difflib" in self.source, "应导入 difflib"
        assert "_FULL_TABLE_LIST" in self.source
        assert "get_close_matches" in self.source

    def test_column_not_found_injects_valid_fields(self):
        """Task 5: column_not_found 应注入 valid fields 列表"""
        assert "valid_match" in self.source, "应存在 valid_match 提取逻辑"
        assert "实际存在的字段" in self.source, "应注入字段列表提示"

    def test_lag_contradiction_fixed(self):
        """Task 6: 规则 5.11 应优先自连接而非 LAG()"""
        assert "自连接" in self.rules, "规则 5.11 应优先自连接差分"
        assert "LAG(" in self.rules, "仍保留 LAG( 字符串"
        assert "LAG(GREPTIME_VALUE)" in self.rules, "仍保留 LAG(GREPTIME_VALUE) 字符串"

    def test_function_not_found_has_lag_hint(self):
        """Task 6: function_not_found prompt 应包含 LAG 提示"""
        # 注意：不能用 \)\s*, 泛匹配，因为 prompt 内部有 (ORDER BY 列), 导致提前截断
        m = re.search(
            r'"function_not_found":\s*\((.*?)\n\s*\),',
            self.source, re.DOTALL,
        )
        prompt = m.group(1) if m else ""
        assert "LAG" in prompt, \
            "function_not_found prompt 应包含 LAG 不支持提示"

    def test_join_error_has_distinct_hint(self):
        """Task 7: join_error prompt 应包含 DISTINCT 提示"""
        m = re.search(
            r'"join_error":\s*\((.+?)\)\s*,',
            self.source, re.DOTALL,
        )
        prompt = m.group(1) if m else ""
        assert "DISTINCT" in prompt, \
            "join_error prompt 应包含 SELECT DISTINCT 提示"


# ── 集成验证：所有改动文件语法有效 ─────────────────────────────────

class TestSyntaxValid:
    def test_server_py_compiles(self):
        import py_compile
        py_compile.compile(str(SERVER_PY), doraise=True)

    def test_query_tracker_py_compiles(self):
        import py_compile
        qt = SRC_DIR / "utils" / "query_tracker.py"
        py_compile.compile(str(qt), doraise=True)
