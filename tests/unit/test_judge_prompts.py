"""JudgeModel 基础设施类失败处理 — 单元测试

覆盖 4 个改动点：
1. CATEGORIES 增加 4 个 infrastructure_* 类别
2. PROMPT_TEMPLATE 新增「基础设施类失败判定规则」段 + {infra_error_instruct} 占位符
3. _normalize_verdict 允许 correct=True 与 infrastructure_* 共存
4. _build_infra_infra_instruct() 根据 error_type 生成对应指令
"""

import pytest

from xiyan_mcp_server.utils.judge import JudgeModel


# ── CATEGORIES ────────────────────────────────────────────────────

class TestCategoriesContainsInfra:
    def test_categories_contain_all_4_infra(self):
        """CATEGORIES 应包含 4 个新基础设施类别"""
        for c in [
            "infrastructure_timeout",
            "infrastructure_conn",
            "infrastructure_planner",
            "infrastructure_permission",
        ]:
            assert c in JudgeModel.CATEGORIES, f"CATEGORIES 缺 {c}"

    def test_categories_still_contain_original_7(self):
        """原有 7 类别不能丢"""
        for c in [
            "correct", "schema_wrong", "column_wrong",
            "aggregation_wrong", "filter_wrong", "syntax",
            "other", "system_failure",
        ]:
            assert c in JudgeModel.CATEGORIES, f"CATEGORIES 缺 {c}"

    def test_categories_total_11(self):
        """CATEGORIES 应该是 8 + 4 = 12 项（注意：原 7 + system_failure + 4 infra = 12）"""
        # 原有: correct, schema_wrong, column_wrong, aggregation_wrong, filter_wrong, syntax, other, system_failure = 8
        # 新增: 4 个 infrastructure_*
        # 合计: 12
        assert len(JudgeModel.CATEGORIES) == 12


# ── PROMPT_TEMPLATE ──────────────────────────────────────────────

class TestPromptTemplateInfra:
    def test_prompt_has_infra_section(self):
        """PROMPT_TEMPLATE 应包含「基础设施类失败判定规则」段 + 占位符"""
        assert "基础设施类失败判定规则" in JudgeModel.PROMPT_TEMPLATE
        assert "{infra_error_instruct}" in JudgeModel.PROMPT_TEMPLATE

    def test_prompt_mentions_all_4_infra_error_types(self):
        """PROMPT_TEMPLATE 应点名所有 4 类 error_type"""
        t = JudgeModel.PROMPT_TEMPLATE
        for kw in ["timeout", "connection_error", "planner_error", "permission_denied"]:
            assert kw in t, f"PROMPT_TEMPLATE 缺 {kw}"

    def test_prompt_category_list_expanded_to_11(self):
        """类别清单应从 7 扩展到 11（多了 4 个 infra）"""
        # 旧文案 "从以下 7 类中选一个" 应该已替换
        assert "7 类" not in JudgeModel.PROMPT_TEMPLATE
        assert "11 类" in JudgeModel.PROMPT_TEMPLATE
        # 新增的 4 个 infra 类别都应在类别清单中
        for c in [
            "infrastructure_timeout", "infrastructure_conn",
            "infrastructure_planner", "infrastructure_permission",
        ]:
            assert c in JudgeModel.PROMPT_TEMPLATE


# ── _normalize_verdict ───────────────────────────────────────────

class TestNormalizeVerdict:
    """_normalize_verdict 的核心修复：correct=True 时允许 infrastructure_* 共存"""

    def _bare_instance(self):
        """绕过 __init__ 构造最小可用实例（只测纯函数行为）"""
        return JudgeModel.__new__(JudgeModel)

    def test_allows_correct_with_infra_timeout(self):
        """核心：correct=True + category=infrastructure_timeout 不应被强制覆盖"""
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True,
            "category": "infrastructure_timeout",
            "reason": "x",
        })
        assert v["correct"] is True
        assert v["category"] == "infrastructure_timeout"

    def test_allows_correct_with_infra_conn(self):
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True, "category": "infrastructure_conn", "reason": "x",
        })
        assert v["correct"] is True
        assert v["category"] == "infrastructure_conn"

    def test_allows_correct_with_infra_planner(self):
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True, "category": "infrastructure_planner", "reason": "x",
        })
        assert v["correct"] is True
        assert v["category"] == "infrastructure_planner"

    def test_allows_correct_with_infra_permission(self):
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True, "category": "infrastructure_permission", "reason": "x",
        })
        assert v["correct"] is True
        assert v["category"] == "infrastructure_permission"

    def test_still_forces_correct_for_sql_errors(self):
        """旧行为：correct=True + SQL 语义错类别仍强制归为 correct"""
        m = self._bare_instance()
        for cat in ["schema_wrong", "column_wrong", "aggregation_wrong", "filter_wrong", "syntax"]:
            v = m._normalize_verdict({"correct": True, "category": cat, "reason": "x"})
            assert v["category"] == "correct", f"category={cat} 应被强制为 correct"

    def test_unknown_infra_category_eventually_correct(self):
        """未列入 CATEGORIES 的 fake infra_* 经过两阶段归一化：
        阶段 1：'infrastructure_unknown' 不在 CATEGORIES → 归为 'other'
        阶段 2：correct=True + category='other'（非 infra_*）→ 强制为 'correct'
        这就是当前 _normalize_verdict 的兜底行为（safety net：模型乱填时信 correct=True）
        """
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True, "category": "infrastructure_unknown", "reason": "x",
        })
        # 两阶段后变成 "correct"
        assert v["category"] == "correct"

    def test_inverse_correct_false_with_correct_category(self):
        """correct=False + category=correct → 仍归为 other（兜底逻辑不变）"""
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": False, "category": "correct", "reason": "x",
        })
        assert v["category"] == "other"


# ── _normalize_verdict: conclusion unwrap ────────────────────────

class TestNormalizeVerdictConclusionUnwrap:
    """兼容模型把 verdict 包在 conclusion 对象里（CoT 输出习惯）

    现象：模型输出 {"analysis": {...}, "conclusion": {"correct": true, ...}}
    修复：_normalize_verdict 顶层缺 correct 时 unwrap 到 conclusion
    """

    def _bare_instance(self):
        return JudgeModel.__new__(JudgeModel)

    def test_unwrap_conclusion_with_correct_true(self):
        m = self._bare_instance()
        v = m._normalize_verdict({
            "analysis": {"SQL 语义": "...", "意图匹配": "..."},
            "conclusion": {
                "correct": True, "category": "correct", "reason": "SQL 正确",
            },
        })
        assert v["correct"] is True
        assert v["category"] == "correct"
        assert v["reason"] == "SQL 正确"

    def test_unwrap_conclusion_with_infra_category(self):
        """conclusion 包 infra_* 类别，unwrap 后与 correct=True 共存"""
        m = self._bare_instance()
        v = m._normalize_verdict({
            "analysis": {"...": "..."},
            "conclusion": {
                "correct": True,
                "category": "infrastructure_timeout",
                "reason": "SQL 文本正确但 exec 超时",
            },
        })
        assert v["correct"] is True
        assert v["category"] == "infrastructure_timeout"

    def test_unwrap_conclusion_with_correct_false(self):
        m = self._bare_instance()
        v = m._normalize_verdict({
            "analysis": {"...": "..."},
            "conclusion": {
                "correct": False, "category": "schema_wrong", "reason": "表选错",
            },
        })
        assert v["correct"] is False
        assert v["category"] == "schema_wrong"

    def test_top_level_correct_takes_precedence(self):
        """顶层已有 correct 时不被 conclusion 覆盖（即使 conclusion 也有 verdict）"""
        m = self._bare_instance()
        v = m._normalize_verdict({
            "correct": True,
            "category": "correct",
            "conclusion": {  # 应被忽略
                "correct": False,
                "category": "syntax",
            },
        })
        assert v["correct"] is True
        assert v["category"] == "correct"

    def test_conclusion_not_dict_falls_through_to_error(self):
        """conclusion 不是 dict（字符串/列表等）时不 unwrap，走旧逻辑抛错"""
        m = self._bare_instance()
        with pytest.raises(Exception) as exc_info:
            m._normalize_verdict({
                "analysis": {"...": "..."},
                "conclusion": "some string",  # 非 dict
            })
        assert "不是 bool" in str(exc_info.value)

    def test_no_conclusion_no_correct_still_raises(self):
        """既无 correct 也无 conclusion 时仍按旧逻辑抛错"""
        m = self._bare_instance()
        with pytest.raises(Exception) as exc_info:
            m._normalize_verdict({"analysis": {"SQL 语义": "..."}})
        assert "不是 bool" in str(exc_info.value)

    def test_conclusion_missing_correct_still_raises(self):
        """conclusion 存在但里面也没 correct → 抛错（不静默归 other）"""
        m = self._bare_instance()
        with pytest.raises(Exception) as exc_info:
            m._normalize_verdict({
                "analysis": {"...": "..."},
                "conclusion": {"category": "correct", "reason": "..."},  # 缺 correct
            })
        assert "不是 bool" in str(exc_info.value)

    def test_non_dict_parsed_raises(self):
        """parsed 不是 dict 时直接抛错（防御）"""
        m = self._bare_instance()
        with pytest.raises(Exception) as exc_info:
            m._normalize_verdict("not a dict")
        assert "不是 dict" in str(exc_info.value)

    def test_parsed_list_raises(self):
        m = self._bare_instance()
        with pytest.raises(Exception) as exc_info:
            m._normalize_verdict([{"correct": True, "category": "correct"}])
        assert "不是 dict" in str(exc_info.value)


# ── _build_infra_instruct ────────────────────────────────────────

class TestBuildInfraInstruct:
    """_build_infra_instruct 根据 exec_summary.error_type 生成对应指令"""

    @pytest.mark.parametrize("err_type", [
        "timeout", "connection_error", "planner_error", "permission_denied",
    ])
    def test_returns_non_empty_for_infra_types(self, err_type):
        s = JudgeModel._build_infra_instruct({
            "success": False, "error_type": err_type, "error": "...",
        })
        assert s != "", f"error_type={err_type} 应生成非空指令"
        assert "基础设施类错误" in s
        assert f"error_type={err_type}" in s
        assert "只评估 SQL 文本逻辑" in s

    @pytest.mark.parametrize("err_type", [
        "schema_wrong", "column_wrong", "aggregation_wrong",
        "filter_wrong", "syntax", "table_not_found", "function_not_found",
    ])
    def test_returns_empty_for_non_infra_types(self, err_type):
        """非基础设施类错误 → 空指令（不污染 prompt）"""
        s = JudgeModel._build_infra_instruct({
            "success": False, "error_type": err_type, "error": "...",
        })
        assert s == "", f"error_type={err_type} 应返回空"

    def test_returns_empty_when_no_error_type(self):
        s = JudgeModel._build_infra_instruct({"success": True})
        assert s == ""

    def test_returns_empty_when_error_type_is_none(self):
        s = JudgeModel._build_infra_instruct({"success": False, "error_type": None})
        assert s == ""

    def test_returns_empty_when_error_type_is_empty_string(self):
        s = JudgeModel._build_infra_instruct({"success": False, "error_type": ""})
        assert s == ""

    def test_infra_types_constant_contains_four(self):
        """INFRA_ERROR_TYPES 应正好 4 项"""
        assert len(JudgeModel.INFRA_ERROR_TYPES) == 4
        assert JudgeModel.INFRA_ERROR_TYPES == frozenset({
            "timeout", "connection_error", "planner_error", "permission_denied",
        })
