"""异步 HDFS 上传模块

改造要点：
- SSH/SCP/HDFS 命令改用 asyncio.create_subprocess_exec()
- 禁止 shell 拼接用户输入
- 使用独立进程组
- 超时或取消时先 terminate，2 秒后仍未退出则 kill
- Parquet 转换通过 asyncio.to_thread() 执行
- HDFS 并发上限为 2
- 临时目录包含 request_id + UUID，避免并发冲突
- 自动生成路径包含微秒时间和随机后缀
- 用户指定目标路径已存在时返回明确错误，不静默覆盖
- 无论成功、失败或取消，都必须清理本地临时文件
"""

import asyncio
import datetime
import logging
import os
import signal
import tempfile
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from ..runtime import Deadline, DeadlineExceeded, HdfsTimeoutConfig

logger = logging.getLogger("xiyan_mcp_server.hdfs_async")


class AsyncHDFSUploader:
    """异步 HDFS 上传器

    使用 asyncio 子进程执行 SSH/SCP/HDFS 命令，
    支持并发控制、超时取消和安全清理。
    """

    def __init__(
        self,
        config: Dict[str, Any],
        timeouts: Optional[HdfsTimeoutConfig] = None,
        max_concurrency: int = 2,
    ):
        self.remote_host = config.get("remote_host", "172.19.19.118")
        self.remote_user = config.get("remote_user", "root")
        self.remote_tmp_dir = config.get("remote_tmp_dir", "/parquet_data")
        self.hdfs_root_dir = config.get("hdfs_root_dir", "/user_custom_data")
        self.hadoop_home = config.get("hadoop_home", "/data/software/hadoop-3.2.4")
        self.local_output_dir = Path(config.get("local_output_dir", "query_results_hdfs"))
        self.local_output_dir.mkdir(parents=True, exist_ok=True)

        self._timeouts = timeouts or HdfsTimeoutConfig()
        self._semaphore: Optional[asyncio.Semaphore] = None
        self._max_concurrency = max_concurrency
        self._active_processes: list[asyncio.subprocess.Process] = []
        self._proc_lock = asyncio.Lock()

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._max_concurrency)
        return self._semaphore

    def _generate_unique_path(self, request_id: Optional[str] = None) -> Tuple[str, str]:
        """生成唯一的 HDFS 目标路径和临时目录名

        包含微秒时间和随机后缀，避免并发冲突。
        """
        now = datetime.datetime.now()
        timestamp = now.strftime("%Y%m%d_%H%M%S_%f")  # 含微秒
        random_suffix = uuid.uuid4().hex[:8]
        req_part = f"{request_id}_" if request_id else ""
        batch_name = f"{timestamp}_{req_part}{random_suffix}"
        hdfs_dest_dir = f"{self.hdfs_root_dir}/{batch_name}/parquet"
        return hdfs_dest_dir, batch_name

    def _make_temp_dir(self, request_id: Optional[str] = None) -> Path:
        """创建包含 request_id + UUID 的临时目录"""
        req_part = f"{request_id}_" if request_id else ""
        dir_name = f"{req_part}{uuid.uuid4().hex[:12]}"
        temp_dir = self.local_output_dir / f".tmp_{dir_name}"
        temp_dir.mkdir(parents=True, exist_ok=True)
        return temp_dir

    async def _run_subprocess(
        self,
        args: list[str],
        *,
        timeout: float,
        deadline: Deadline,
    ) -> Tuple[int, str, str]:
        """运行子进程，支持超时取消

        超时或取消时先 terminate，kill_grace_seconds 后仍未退出则 kill。
        使用独立进程组。
        """
        actual_timeout = min(timeout, deadline.remaining())
        if actual_timeout <= 0:
            raise DeadlineExceeded("请求处理超时")

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # 独立进程组
        )

        async with self._proc_lock:
            self._active_processes.append(proc)

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=actual_timeout
            )
            return proc.returncode, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            # 先 terminate
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

            # 等待 kill_grace_seconds
            try:
                await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._timeouts.kill_grace_seconds,
                )
            except asyncio.TimeoutError:
                # 强制 kill
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
                try:
                    await proc.communicate()
                except Exception:
                    pass

            raise DeadlineExceeded("请求处理超时")
        finally:
            async with self._proc_lock:
                if proc in self._active_processes:
                    self._active_processes.remove(proc)

    async def _scp_to_remote(
        self,
        local_file_path: str,
        *,
        deadline: Deadline,
    ) -> str:
        """通过 SCP 上传文件到远程服务器"""
        filename = Path(local_file_path).name
        remote_file_path = f"{self.remote_tmp_dir}/{filename}"

        returncode, stdout, stderr = await self._run_subprocess(
            [
                "scp",
                "-o", f"ConnectTimeout={int(self._timeouts.connect_timeout_seconds)}",
                local_file_path,
                f"{self.remote_user}@{self.remote_host}:{self.remote_tmp_dir}/",
            ],
            timeout=self._timeouts.scp_timeout_seconds,
            deadline=deadline,
        )

        if returncode != 0:
            raise Exception(f"SCP 上传失败: {stderr}")
        return remote_file_path

    async def _hdfs_put(
        self,
        remote_file_path: str,
        hdfs_dest: str,
        *,
        deadline: Deadline,
        check_exists: bool = False,
    ) -> None:
        """通过 SSH 执行 HDFS put 命令"""
        # 检查目标路径是否已存在（用户指定路径时不静默覆盖）
        if check_exists:
            check_cmd = f"{self.hadoop_home}/bin/hdfs dfs -test -e {hdfs_dest}"
            returncode, _, _ = await self._run_subprocess(
                [
                    "ssh",
                    "-o", f"ConnectTimeout={int(self._timeouts.connect_timeout_seconds)}",
                    f"{self.remote_user}@{self.remote_host}",
                    check_cmd,
                ],
                timeout=self._timeouts.connect_timeout_seconds + 5,
                deadline=deadline,
            )
            if returncode == 0:
                raise FileExistsError(f"HDFS 目标路径已存在: {hdfs_dest}")

        # 执行 mkdir + put + 清理远程临时文件
        remote_cmd = (
            f"{self.hadoop_home}/bin/hdfs dfs -mkdir -p {hdfs_dest} && "
            f"{self.hadoop_home}/bin/hdfs dfs -put {remote_file_path} {hdfs_dest}/ && "
            f"rm -f {remote_file_path}"
        )

        returncode, stdout, stderr = await self._run_subprocess(
            [
                "ssh",
                "-o", f"ConnectTimeout={int(self._timeouts.connect_timeout_seconds)}",
                f"{self.remote_user}@{self.remote_host}",
                remote_cmd,
            ],
            timeout=self._timeouts.put_timeout_seconds,
            deadline=deadline,
        )

        if returncode != 0:
            raise Exception(f"HDFS put 失败: {stderr}")

    async def upload_to_hdfs_async(
        self,
        local_file_path: str,
        *,
        deadline: Deadline,
        hdfs_dest_dir: Optional[str] = None,
        request_id: Optional[str] = None,
        check_exists: bool = False,
    ) -> str:
        """异步上传文件到 HDFS

        Args:
            local_file_path: 本地文件路径
            deadline: 绝对截止时间
            hdfs_dest_dir: 可选 HDFS 目标目录
            request_id: 请求 ID（用于临时目录命名）
            check_exists: 是否检查目标路径已存在

        Returns:
            HDFS 完整文件路径
        """
        sem = self._get_semaphore()
        remaining = deadline.remaining()
        if remaining <= 0:
            raise DeadlineExceeded("请求处理超时")

        try:
            await asyncio.wait_for(sem.acquire(), timeout=remaining)
        except asyncio.TimeoutError:
            raise DeadlineExceeded("请求处理超时")

        try:
            if hdfs_dest_dir is None:
                hdfs_dest_dir, _ = self._generate_unique_path(request_id)

            filename = Path(local_file_path).name

            # Step 1: SCP 到远程服务器
            await self._scp_to_remote(local_file_path, deadline=deadline)

            # Step 2: SSH 执行 HDFS 命令
            await self._hdfs_put(
                f"{self.remote_tmp_dir}/{filename}",
                hdfs_dest_dir,
                deadline=deadline,
                check_exists=check_exists,
            )

            # Step 3: 返回 HDFS 完整路径
            return f"{hdfs_dest_dir}/{filename}"

        finally:
            sem.release()

    @staticmethod
    def _validate_hdfs_path(path: str) -> None:
        """校验用户提供的 HDFS 路径，防止命令注入和路径遍历"""
        import re
        if not path:
            return
        if '\0' in path:
            raise ValueError("hdfs_path 包含非法字符 (null byte)")
        for component in path.split('/'):
            if component == '..':
                raise ValueError("hdfs_path 不允许包含 '..' 路径遍历")
        if not re.match(r'^[a-zA-Z0-9_\-./]*$', path):
            raise ValueError("hdfs_path 包含不允许的字符，仅支持字母、数字、_、-、.、/")

    async def convert_and_upload_async(
        self,
        result: Dict[str, Any],
        *,
        deadline: Deadline,
        hdfs_path: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> str:
        """将查询结果转换为 Parquet 并异步上传到 HDFS

        Parquet 转换通过 asyncio.to_thread() 执行。
        无论成功、失败或取消，都清理本地临时文件。
        """
        # 安全校验用户提供的路径
        if hdfs_path:
            self._validate_hdfs_path(hdfs_path)

        temp_dir = self._make_temp_dir(request_id)
        try:
            # Parquet 转换在线程池中执行
            local_file = await asyncio.wait_for(
                asyncio.to_thread(
                    self._convert_to_parquet, result, temp_dir
                ),
                timeout=deadline.remaining(),
            )

            # 确定 HDFS 目标路径
            hdfs_dest_dir = None
            check_exists = False
            if hdfs_path:
                hdfs_dest_dir = f"{self.hdfs_root_dir}/{hdfs_path.lstrip('/')}"
                check_exists = True  # 用户指定路径时检查是否已存在

            # 异步上传
            hdfs_result = await self.upload_to_hdfs_async(
                str(local_file),
                deadline=deadline,
                hdfs_dest_dir=hdfs_dest_dir,
                request_id=request_id,
                check_exists=check_exists,
            )
            return hdfs_result

        finally:
            # 无论成功、失败或取消，都清理本地临时文件
            self._cleanup_temp_dir(temp_dir)

    def _convert_to_parquet(self, result: Dict[str, Any], temp_dir: Path) -> Path:
        """将查询结果转换为 Parquet 文件（同步，在线程池中执行）"""
        import pandas as pd

        fields = result.get("fields", [])
        rows = result.get("truncated_results", [])

        if isinstance(rows, str):
            raise ValueError(f"查询错误: {rows}")
        if not isinstance(rows, list):
            raise ValueError(f"无效的行数据格式: {type(rows)}")
        if not fields:
            raise ValueError("没有可用的数据")

        df_dict = {}
        for i, field in enumerate(fields):
            df_dict[field] = [row[i] if i < len(row) else None for row in rows]

        df = pd.DataFrame(df_dict)
        df = df.rename(columns={
            "greptime_timestamp": "timestamp",
            "greptime_value": "value",
        })
        if "metric_name" not in df.columns:
            df["metric_name"] = "custom_query"
        if "timestamp" in df.columns:
            df["timestamp"] = df["timestamp"].astype(str)

        # 使用唯一文件名
        filename = f"{uuid.uuid4().hex[:12]}.parquet"
        local_file = temp_dir / filename
        df.to_parquet(local_file, index=False)
        return local_file

    @staticmethod
    def _cleanup_temp_dir(temp_dir: Path) -> None:
        """清理临时目录及其内容"""
        try:
            if temp_dir.exists():
                for f in temp_dir.iterdir():
                    f.unlink(missing_ok=True)
                temp_dir.rmdir()
        except Exception as e:
            logger.warning(f"清理临时目录失败 {temp_dir}: {e}")

    async def terminate_all_processes(self) -> None:
        """终止所有活跃子进程（用于 shutdown）"""
        async with self._proc_lock:
            for proc in self._active_processes:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, OSError):
                    pass

        # 等待 2 秒
        await asyncio.sleep(self._timeouts.kill_grace_seconds)

        async with self._proc_lock:
            for proc in self._active_processes:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            self._active_processes.clear()

        logger.info("所有 HDFS 子进程已终止")
