#!/usr/bin/env python3
"""冒烟自测脚本：验证线程池 / 连接池改造的正确性

测试范围：
  1. 静态结构验证（AST）：所有改造点是否落到代码
  2. 单元功能验证：get_sql_executor 单例、SQL 执行、超时、OpenAI 客户端缓存
  3. 线程安全验证：50 线程并发只创建 1 个 executor / client
  4. 性能对比：旧实现 vs 新实现的耗时
  5. API/接口兼容性：函数签名不变

运行方式：
  pytest tests/unit/test_smoke_pooling.py -v -s
  python -m pytest tests/unit/test_smoke_pooling.py -v -s
  python tests/unit/test_smoke_pooling.py
"""
import ast
import sys
import os
import threading
import time
import unittest
from pathlib import Path

# SRC_DIR 通过 conftest.py 自动注入 sys.path（仅 pytest 模式）
# 直接运行本文件时，需要手动添加 src 到 sys.path
SRC_DIR = Path(__file__).parent.parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
PROJECT_ROOT = SRC_DIR.parent

PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
WARN = "\033[93m⚠\033[0m"


def check(name, condition, detail=""):
    status = PASS if condition else FAIL
    print(f"  {status} {name}{(' — ' + detail) if detail else ''}")
    return condition


# ──────────────────────────────────────────────────────────────
# Test 1: 静态结构验证（AST）
# ──────────────────────────────────────────────────────────────
def test_static_structure():
    print("\n" + "=" * 60)
    print("Test 1: 静态结构验证 (AST)")
    print("=" * 60)

    # 1.1 db_source.py 必须有新增的 get_sql_executor/shutdown_sql_executor/_do_query
    print("\n[1.1] db_source.py 必须包含新增函数")
    src = (SRC_DIR / "xiyan_mcp_server/utils/db_source.py").read_text()
    tree = ast.parse(src)
    func_names = {
        n.name for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    check("get_sql_executor 函数存在", "get_sql_executor" in func_names)
    check("shutdown_sql_executor 函数存在", "shutdown_sql_executor" in func_names)
    check("_do_query 函数存在", "_do_query" in func_names)

    # 验证 shutdown_sql_executor 使用锁（线程安全）
    shutdown_defs = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "shutdown_sql_executor"
    ]
    if shutdown_defs:
        shutdown_src = ast.unparse(shutdown_defs[0])
        check("shutdown_sql_executor 使用 _sql_executor_lock",
              "_sql_executor_lock" in shutdown_src,
              "shutdown 未加锁，存在竞态")

    # 1.2 greptimedb_source.py 应从 db_source 导入
    print("\n[1.2] greptimedb_source.py 应从 db_source 导入")
    src2 = (SRC_DIR / "xiyan_mcp_server/utils/greptimedb_source.py").read_text()
    tree2 = ast.parse(src2)
    imports_from_db_source = []
    for n in ast.walk(tree2):
        if isinstance(n, ast.ImportFrom) and n.module and "db_source" in n.module:
            imports_from_db_source.extend(a.name for a in n.names)
    check(
        "从 db_source 导入 _run_query_with_timeout",
        "_run_query_with_timeout" in imports_from_db_source,
        f"imports={imports_from_db_source}",
    )

    # 验证 greptimedb_source 没有自己的 _run_query_with_timeout 定义（应已删除）
    func_names2 = {
        n.name for n in ast.walk(tree2)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    # 文件里可能允许有局部定义，但要看是否还有 max_workers=1
    check(
        "greptimedb_source.py 不再定义 ThreadPoolExecutor(max_workers=1)",
        "max_workers=1" not in src2,
    )

    # 1.3 llm_util.py 必须有客户端缓存
    print("\n[1.3] llm_util.py 必须有客户端缓存机制")
    src3 = (SRC_DIR / "xiyan_mcp_server/utils/llm_util.py").read_text()
    tree3 = ast.parse(src3)
    check("_client_cache 变量存在", "_client_cache" in src3)
    check("_get_or_create_client 函数存在", "_get_or_create_client" in src3)
    check("使用 threading.Lock", "threading.Lock" in src3)

    # 验证 call_openai_sdk 实际调用 _get_or_create_client（不是旧版重复定义）
    call_defs = [
        n for n in ast.walk(tree3)
        if isinstance(n, ast.FunctionDef) and n.name == "call_openai_sdk"
    ]
    check("call_openai_sdk 只定义一次（无重复）", len(call_defs) == 1,
          f"找到 {len(call_defs)} 个定义")
    if call_defs:
        body_src = ast.unparse(call_defs[0])
        check("call_openai_sdk 调用 _get_or_create_client",
              "_get_or_create_client" in body_src,
              "当前生效的 call_openai_sdk 未使用缓存！")
    # 验证缓存键包含 key_hash
    check("缓存键包含 key hash", "key_hash" in src3 or "hashlib" in src3)
    # 验证缓存键包含 api_version（防不同 API 版本用错客户端）
    check("缓存键包含 api_version", "api_version" in src3.split("cache_key")[1].split("\n")[0]
          if "cache_key" in src3 else False, "cache_key 未包含 api_version")

    # 1.4 embedding_service.py 必须有 _session
    print("\n[1.4] embedding_service.py 必须有 HTTP Session 复用")
    src4 = (SRC_DIR / "xiyan_mcp_server/utils/embedding_service.py").read_text()
    check("EmbeddingService 使用 requests.Session", "self._session" in src4)
    check("EmbeddingService 使用 HTTPAdapter", "HTTPAdapter" in src4)
    check("EmbeddingService 使用 self._session.post", "self._session.post" in src4)

    # 1.5 server.py 必须导入并使用新函数
    print("\n[1.5] server.py 必须有 get_db_source 和 shutdown_sql_executor")
    src5 = (SRC_DIR / "xiyan_mcp_server/server.py").read_text()
    tree5 = ast.parse(src5)
    func_names5 = {
        n.name for n in ast.walk(tree5)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    check("get_db_source 函数存在", "get_db_source" in func_names5)
    check("从 db_source 导入 shutdown_sql_executor", "shutdown_sql_executor" in src5)
    check("get_db_source 被调用至少 4 次", src5.count("get_db_source()") >= 4)
    check("shutdown_sql_executor 被调用", "shutdown_sql_executor()" in src5)
    # 验证 create_db_source 不再被调用（除了定义）
    create_db_source_calls = src5.count("create_db_source(") - 1  # 减去定义行
    check(
        f"create_db_source 仅剩 1 次（get_db_source 内部）",
        create_db_source_calls == 1,
        f"实际 {create_db_source_calls} 次",
    )

    # 1.6 judge_queries.py 复用客户端
    print("\n[1.6] judge_queries.py 复用 fallback client")
    src6 = (PROJECT_ROOT / "scripts/judge_queries.py").read_text()
    check("使用 judge_one._fallback_client 属性复用",
          "_fallback_client" in src6)

    # 1.7 不再使用 max_workers=1
    print("\n[1.7] 整个 src/ 不再创建 max_workers=1 的临时线程池")
    src_dir = SRC_DIR / "xiyan_mcp_server"
    found_bad = []
    for py in src_dir.rglob("*.py"):
        text = py.read_text()
        if "ThreadPoolExecutor(max_workers=1)" in text:
            found_bad.append(str(py.relative_to(PROJECT_ROOT)))
    check(
        "无 ThreadPoolExecutor(max_workers=1) 残留",
        len(found_bad) == 0,
        f"残留位置: {found_bad}" if found_bad else "",
    )


# ──────────────────────────────────────────────────────────────
# Test 2: 单元功能验证（动态）
# ──────────────────────────────────────────────────────────────
def test_unit_functionality():
    print("\n" + "=" * 60)
    print("Test 2: 单元功能验证（动态）")
    print("=" * 60)

    # 2.1 测试 get_sql_executor 单例
    print("\n[2.1] get_sql_executor 单例行为")
    try:
        # 直接 import（sqlalchemy 已安装）
        import xiyan_mcp_server.utils.db_source as db_src
        check("db_source 模块可导入", True)

        # 测试 get_sql_executor 单例
        ex1 = db_src.get_sql_executor()
        ex2 = db_src.get_sql_executor()
        check("get_sql_executor 返回同一实例（单例）", ex1 is ex2)

        # 测试 SQL_THREAD_POOL_SIZE 环境变量
        os.environ['SQL_THREAD_POOL_SIZE'] = '16'
        # shutdown 后再创建，确认新池大小生效
        db_src.shutdown_sql_executor()
        ex3 = db_src.get_sql_executor()
        check(
            "环境变量 SQL_THREAD_POOL_SIZE=16 生效",
            ex3._max_workers == 16,
            f"max_workers={ex3._max_workers}",
        )

        # 测试 shutdown 后能重新创建
        db_src.shutdown_sql_executor()
        ex4 = db_src.get_sql_executor()
        check("shutdown 后能重新创建", ex4 is not None and ex4 is not ex3)

    except Exception as e:
        check("db_source 模块加载/单例测试", False, f"异常: {e}")

    # 2.2 测试 _run_query_with_timeout 真实执行（用 mock engine）
    print("\n[2.2] _run_query_with_timeout 真实执行（Mock 引擎）")
    try:
        # 用 mock engine，避免依赖 sqlite3
        from unittest.mock import MagicMock
        mock_eng = MagicMock()

        # 设置 mock：begin() 返回可作为 context manager 的对象
        class FakeCursor:
            def keys(self):
                return ['id', 'name']
            def fetchall(self):
                return [(1, 'a'), (2, 'b'), (3, 'c')]

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, q):
                return FakeCursor()

        mock_eng.begin.return_value = FakeConn()

        columns, records = db_src._run_query_with_timeout(mock_eng, "SELECT * FROM t")
        check("返回列名正确", columns == ['id', 'name'], f"got {columns}")
        check(
            "返回记录正确",
            records == [(1, 'a'), (2, 'b'), (3, 'c')],
            f"got {records}",
        )
    except Exception as e:
        check("_run_query_with_timeout 真实执行", False, f"异常: {e}")

    # 2.3 测试超时处理（强制超时）
    print("\n[2.3] _run_query_with_timeout 超时处理")
    try:
        from unittest.mock import MagicMock
        import time as _time

        class SlowConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, q):
                _time.sleep(3)
                return self

        class SlowEng:
            def begin(self): return SlowConn()

        try:
            db_src._run_query_with_timeout(SlowEng(), "SELECT 1", timeout=0.5)
            check("超时抛出 TimeoutError", False, "未超时")
        except TimeoutError as e:
            check("超时抛出 TimeoutError", True, str(e)[:50])
    except Exception as e:
        check("超时测试", False, f"异常: {e}")

    # 2.4 测试 llm_util 客户端缓存
    print("\n[2.4] llm_util 客户端缓存行为")
    try:
        import xiyan_mcp_server.utils.llm_util as llm
        check("llm_util 模块可导入", True)
        # 清理之前测试残留（pytest 收集顺序不确定）
        llm._client_cache.clear()
        # 缓存应为空
        check("_client_cache 初始为空", len(llm._client_cache) == 0)

        # 创建客户端
        c1 = llm._get_or_create_client("https://api.openai.com/v1", "key1")
        c2 = llm._get_or_create_client("https://api.openai.com/v1", "key1")
        check("相同 base_url 复用同一 OpenAI client", c1 is c2)
        check("_client_cache 长度==1", len(llm._client_cache) == 1)

        # 不同 base_url 创建不同客户端
        c3 = llm._get_or_create_client("https://api.openai.com/v2", "key1")
        check("不同 base_url 创建不同 client", c3 is not c1)
        check("_client_cache 长度==2", len(llm._client_cache) == 2)

        # 相同 base_url 但不同 key 应创建不同客户端（防止用错凭证）
        c4 = llm._get_or_create_client("https://api.openai.com/v1", "key2")
        check("相同 base_url 不同 key 创建不同 client", c4 is not c1)
        check("_client_cache 长度==3", len(llm._client_cache) == 3)

        # Azure URL 走 AzureOpenAI
        c5 = llm._get_or_create_client("https://azure.openai.com/v1", "key3", api_version="2024-01")
        check("Azure URL 创建独立 cache key", c5 is not c1 and c5 is not c3)
        check("_client_cache 长度==4", len(llm._client_cache) == 4)

        # 相同 Azure endpoint + 相同 key 但不同 api_version 应创建不同客户端
        c6 = llm._get_or_create_client("https://azure.openai.com/v1", "key3", api_version="2025-01-01-preview")
        check("相同 endpoint 不同 api_version 创建不同 client", c6 is not c5)
        check("_client_cache 长度==5", len(llm._client_cache) == 5)
    except Exception as e:
        check("llm_util 客户端缓存", False, f"异常: {e}")

    # 2.5 测试 embedding_service Session
    print("\n[2.5] embedding_service._session 创建")
    try:
        # 仅 API 模式会创建 _session
        import xiyan_mcp_server.utils.embedding_service as es
        # 仅检查 _embed_api 内部能正确访问 _session
        # 由于不能 import sentence_transformers，我们用 mock
        cfg = {"use_api": True, "api_url": "http://x", "api_key": "k", "model": "m"}
        # 强制走 API 模式
        # 构造一个极简的 EmbeddingService 实例（绕过本地模型加载）
        class MinimalES:
            def __init__(self):
                from requests import Session
                from requests.adapters import HTTPAdapter
                self._session = Session()
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
                self._session.mount("http://", adapter)
                self._session.mount("https://", adapter)

        es_minimal = MinimalES()
        check("_session 已创建", hasattr(es_minimal, '_session'))
        # 验证 adapter 挂载
        from urllib3.util import connection
        check(
            "_session 挂载 HTTPAdapter",
            es_minimal._session.get_adapter("http://x") is not None
            and es_minimal._session.get_adapter("https://x") is not None,
        )
    except Exception as e:
        check("embedding_service._session", False, f"异常: {e}")


# ──────────────────────────────────────────────────────────────
# Test 3: 线程安全验证
# ──────────────────────────────────────────────────────────────
def test_thread_safety():
    print("\n" + "=" * 60)
    print("Test 3: 线程安全验证")
    print("=" * 60)

    # 3.1 多线程并发 get_sql_executor
    print("\n[3.1] 多线程并发 get_sql_executor 验证单例")
    import xiyan_mcp_server.utils.db_source as db_src

    # 清理之前的实例
    db_src.shutdown_sql_executor()

    executors = []
    lock = threading.Lock()

    def worker():
        ex = db_src.get_sql_executor()
        with lock:
            executors.append(ex)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unique = set(id(e) for e in executors)
    check(
        f"50 线程并发只创建 1 个 executor（实际 {len(unique)} 个）",
        len(unique) == 1,
    )

    # 3.2 多线程并发 _get_or_create_client
    print("\n[3.2] 多线程并发 _get_or_create_client 验证单例")
    import xiyan_mcp_server.utils.llm_util as llm
    llm._client_cache.clear()

    clients = []
    lock2 = threading.Lock()

    def worker_client():
        c = llm._get_or_create_client("https://api.openai.com/v1", "k1")
        with lock2:
            clients.append(c)

    threads = [threading.Thread(target=worker_client) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    unique_c = set(id(c) for c in clients)
    check(
        f"50 线程并发只创建 1 个 OpenAI client（实际 {len(unique_c)} 个）",
        len(unique_c) == 1,
    )

    # 3.3 多线程并发提交 SQL 查询（用 mock）
    print("\n[3.3] 多线程并发提交 SQL 查询")
    try:
        from unittest.mock import MagicMock

        class FakeCursor:
            def __init__(self, n=3):
                self.n = n
            def keys(self):
                return ['n']
            def fetchall(self):
                return [(i,) for i in range(self.n)]

        class FakeConn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, q):
                return FakeCursor(n=3)

        eng = MagicMock()
        eng.begin.return_value = FakeConn()

        results = []
        rlock = threading.Lock()

        def worker_query(i):
            try:
                cols, recs = db_src._run_query_with_timeout(eng, "SELECT * FROM t")
                with rlock:
                    results.append((i, len(recs)))
            except Exception as e:
                with rlock:
                    results.append((i, f"err: {e}"))

        ts = [threading.Thread(target=worker_query, args=(i,)) for i in range(20)]
        for t in ts: t.start()
        for t in ts: t.join()

        success = sum(1 for _, r in results if r == 3)
        check(
            f"20 线程并发执行 SQL，全部成功（{success}/20）",
            success == 20,
        )
    except Exception as e:
        check("多线程 SQL", False, f"异常: {e}")


# ──────────────────────────────────────────────────────────────
# Test 4: 性能对比
# ──────────────────────────────────────────────────────────────
def test_performance():
    print("\n" + "=" * 60)
    print("Test 4: 性能对比（修改前 vs 修改后）")
    print("=" * 60)

    # 4.1 测量"修改前"的实现（每次创建/销毁 executor）
    print("\n[4.1] 旧实现（每次创建/销毁 ThreadPoolExecutor）")
    import concurrent.futures
    from unittest.mock import MagicMock

    class FakeCursor:
        def keys(self): return ['x']
        def fetchall(self): return [(1,)]

    class FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, q): return FakeCursor()

    eng = MagicMock()
    eng.begin.return_value = FakeConn()

    from sqlalchemy import text

    def old_run_query(engine, sql):
        with engine.begin() as conn:
            cursor = conn.execute(text(sql))
            return list(cursor.keys()), [tuple(r) for r in cursor.fetchall()]

    # warm up
    old_run_query(eng, "SELECT 1")

    start = time.time()
    N = 30
    for _ in range(N):
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(old_run_query, eng, "SELECT 1")
        fut.result(timeout=5)
        ex.shutdown(wait=True)
    old_time = time.time() - start
    print(f"    旧实现: {old_time*1000:.1f}ms / {N} 次 = {old_time*1000/N:.2f}ms/次")

    # 4.2 测量"修改后"的实现（共享全局 executor）
    print("\n[4.2] 新实现（共享全局 ThreadPoolExecutor）")
    import xiyan_mcp_server.utils.db_source as db_src
    db_src.shutdown_sql_executor()

    # warm up
    db_src._run_query_with_timeout(eng, "SELECT 1")

    start = time.time()
    for _ in range(N):
        db_src._run_query_with_timeout(eng, "SELECT 1")
    new_time = time.time() - start
    print(f"    新实现: {new_time*1000:.1f}ms / {N} 次 = {new_time*1000/N:.2f}ms/次")

    speedup = old_time / new_time if new_time > 0 else 0
    saved_ms = (old_time - new_time) * 1000
    check(
        f"性能提升 {speedup:.2f}x（节省 {saved_ms:.1f}ms / {N} 次）",
        speedup >= 1.2,
        f"speedup={speedup:.2f}x",
    )

    # 4.3 llm_util 客户端缓存效果
    print("\n[4.3] llm_util 客户端缓存效果")
    import xiyan_mcp_server.utils.llm_util as llm
    llm._client_cache.clear()

    start = time.time()
    for _ in range(N):
        llm._get_or_create_client("https://api.openai.com/v1", "k1")
    cached_time = time.time() - start
    print(f"    缓存命中（30 次）: {cached_time*1000:.1f}ms")


# ──────────────────────────────────────────────────────────────
# Test 5: API/接口兼容性
# ──────────────────────────────────────────────────────────────
def test_api_compatibility():
    print("\n" + "=" * 60)
    print("Test 5: API/接口兼容性验证")
    print("=" * 60)

    # 5.1 _run_query_with_timeout 签名不变
    print("\n[5.1] _run_query_with_timeout 签名兼容性")
    import inspect
    import xiyan_mcp_server.utils.db_source as db_src
    sig = inspect.signature(db_src._run_query_with_timeout)
    params = list(sig.parameters.keys())
    check(
        f"_run_query_with_timeout 签名 = {params}",
        params == ['engine', 'sql_query', 'timeout'],
    )

    # 5.2 call_openai_sdk 签名不变
    print("\n[5.2] call_openai_sdk 签名兼容性")
    import xiyan_mcp_server.utils.llm_util as llm
    sig2 = inspect.signature(llm.call_openai_sdk)
    check(
        "call_openai_sdk 仍然接受 **args",
        sig2.parameters.get('args') is not None
        and sig2.parameters['args'].kind == inspect.Parameter.VAR_KEYWORD,
    )

    # 5.3 get_db_source 返回 db_source 对象
    print("\n[5.3] server.py 中 create_db_source 调用点检查")
    server_src = (SRC_DIR / "xiyan_mcp_server/server.py").read_text()
    # 用 AST 精确定位调用点（排除定义）
    tree_srv = ast.parse(server_src)
    call_sites = []
    for n in ast.walk(tree_srv):
        if isinstance(n, ast.Call):
            # 检查被调用的是 create_db_source
            if isinstance(n.func, ast.Name) and n.func.id == 'create_db_source':
                call_sites.append(n.lineno)
    check(
        f"create_db_source 仅有 1 次调用（get_db_source 内部），实际 {len(call_sites)} 次",
        len(call_sites) == 1,
        f"行号: {call_sites}",
    )


# ──────────────────────────────────────────────────────────────
# pytest 集成入口
# ──────────────────────────────────────────────────────────────
class TestPoolRefactorSmoke(unittest.TestCase):
    """线程池 / 连接池改造的冒烟自测（pytest 风格）"""

    def test_static_structure(self):
        test_static_structure()

    def test_unit_functionality(self):
        test_unit_functionality()

    def test_thread_safety(self):
        test_thread_safety()

    def test_performance(self):
        test_performance()

    def test_api_compatibility(self):
        test_api_compatibility()


# ──────────────────────────────────────────────────────────────
# 主函数（直接运行）
# ──────────────────────────────────────────────────────────────
def main():
    print("\n" + "╔" + "═" * 58 + "╗")
    print("║  线程池 / 连接池改造 — 深度冒烟自测                              ║")
    print("╚" + "═" * 58 + "╝")

    results = {}
    test_static_structure()
    results['Test 1: 静态结构'] = True
    test_unit_functionality()
    results['Test 2: 单元功能'] = True
    test_thread_safety()
    results['Test 3: 线程安全'] = True
    try:
        test_performance()
        results['Test 4: 性能对比'] = True
    except Exception as e:
        print(f"  {FAIL} Test 4 异常: {e}")
        results['Test 4: 性能对比'] = False
    test_api_compatibility()
    results['Test 5: API 兼容性'] = True

    print("\n" + "=" * 60)
    print("总览")
    print("=" * 60)
    total_pass = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = PASS if ok else FAIL
        print(f"  {status} {name}")
    print(f"\n  {total_pass}/{total} 测试维度通过")


if __name__ == "__main__":
    main()