#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""HDFS 上传工具模块"""

import datetime
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Dict, Any, Optional, Tuple


class HDFSUploader:
    """HDFS 上传器"""

    def __init__(self, config: Dict[str, Any]):
        """
        初始化 HDFS 上传器

        Args:
            config: HDFS 配置字典
        """
        self.remote_host = config.get("remote_host", "172.19.19.118")
        self.remote_user = config.get("remote_user", "root")
        self.remote_tmp_dir = config.get("remote_tmp_dir", "/parquet_data")
        self.hdfs_root_dir = config.get("hdfs_root_dir", "/user_custom_data")
        self.hadoop_home = config.get("hadoop_home", "/data/software/hadoop-3.2.4")
        self.local_output_dir = Path(config.get("local_output_dir", "query_results_hdfs"))
        self.local_output_dir.mkdir(parents=True, exist_ok=True)

    def _get_timestamp_batch_dir(self) -> str:
        """生成时间戳批次目录名"""
        batch_time = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{batch_time}_batch_001"

    def _generate_hdfs_path(self) -> str:
        """生成 HDFS 目标路径"""
        batch_name = self._get_timestamp_batch_dir()
        return f"{self.hdfs_root_dir}/{batch_name}/parquet"

    @staticmethod
    def _sanitize_path(path: str) -> str:
        """安全过滤用户提供的路径，防止路径遍历攻击

        Args:
            path: 用户提供的路径字符串

        Returns:
            过滤后的路径

        Raises:
            ValueError: 路径包含非法内容
        """
        if not path:
            return path

        if '\0' in path:
            raise ValueError("hdfs_path 包含非法字符 (null byte)")

        for component in path.split('/'):
            if component == '..':
                raise ValueError("hdfs_path 不允许包含 '..' 路径遍历")

        if not re.match(r'^[a-zA-Z0-9_\-./]*$', path):
            raise ValueError("hdfs_path 包含不允许的字符，仅支持字母、数字、_、-、.、/")

        return path

    @staticmethod
    def _ensure_parquet_extension(filename: str) -> str:
        """确保文件名以 .parquet 结尾"""
        if not filename.endswith('.parquet'):
            return f"{filename}.parquet"
        return filename

    def _validate_hdfs_path(self, full_hdfs_dir: str) -> None:
        """验证解析后的路径在 hdfs_root_dir 范围内

        Args:
            full_hdfs_dir: 完整的 HDFS 目录路径

        Raises:
            ValueError: 路径在 HDFS 根目录之外
        """
        normalized = str(PurePosixPath(full_hdfs_dir))
        root_normalized = str(PurePosixPath(self.hdfs_root_dir))
        if not normalized.startswith(root_normalized + '/') and normalized != root_normalized:
            raise ValueError(f"hdfs_path 解析后在 HDFS 根目录之外: {normalized}")

    def _resolve_hdfs_path(
        self,
        custom_path: Optional[str] = None,
        session_id: Optional[str] = None
    ) -> Tuple[str, str]:
        """解析 HDFS 目标路径和文件名

        根据用户输入智能判断是目录、文件名还是完整路径。

        Args:
            custom_path: 用户提供的自定义路径。可以是：
                - None/空: 使用时间戳自动生成
                - "myfile": 仅文件名，使用时间戳目录
                - "mydir/": 仅目录，文件名为时间戳
                - "mydir/file": 完整路径（目录+文件名）
            session_id: 可选会话 ID（仅 custom_path 为空时生效，向后兼容）

        Returns:
            (hdfs_dest_dir, filename) 元组
        """
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

        # 无自定义路径 → 保留旧行为
        if not custom_path:
            batch_dir = self._get_timestamp_batch_dir()
            hdfs_dest_dir = f"{self.hdfs_root_dir}/{batch_dir}/parquet"
            filename = self._ensure_parquet_extension(
                session_id if session_id else timestamp
            )
            return hdfs_dest_dir, filename

        # 安全过滤
        sanitized = self._sanitize_path(custom_path)

        # 去掉前导 /
        sanitized = sanitized.lstrip('/')

        # Case 1: 以 / 结尾 → 目录路径，文件名用时间戳
        if sanitized.endswith('/'):
            dir_part = sanitized.rstrip('/')
            hdfs_dest_dir = f"{self.hdfs_root_dir}/{dir_part}" if dir_part else self.hdfs_root_dir
            self._validate_hdfs_path(hdfs_dest_dir)
            filename = f"{timestamp}.parquet"
            return hdfs_dest_dir, filename

        # Case 2: 不含 / → 纯文件名，目录用时间戳批次
        if '/' not in sanitized:
            batch_dir = self._get_timestamp_batch_dir()
            hdfs_dest_dir = f"{self.hdfs_root_dir}/{batch_dir}/parquet"
            filename = self._ensure_parquet_extension(sanitized)
            return hdfs_dest_dir, filename

        # Case 3: 含 / 但不以 / 结尾 → 完整路径，最后一段是文件名
        parts = sanitized.rsplit('/', 1)
        dir_part = parts[0]
        file_part = parts[1]
        hdfs_dest_dir = f"{self.hdfs_root_dir}/{dir_part}"
        self._validate_hdfs_path(hdfs_dest_dir)
        filename = self._ensure_parquet_extension(file_part)
        return hdfs_dest_dir, filename

    def _scp_to_remote(self, local_file_path: str) -> str:
        """
        通过 SCP 上传文件到远程服务器

        Args:
            local_file_path: 本地文件路径

        Returns:
            远程文件路径

        Raises:
            subprocess.CalledProcessError: SCP 失败时抛出
        """
        filename = Path(local_file_path).name
        remote_file_path = f"{self.remote_tmp_dir}/{filename}"

        subprocess.run(
            ["scp", local_file_path, f"{self.remote_user}@{self.remote_host}:{self.remote_tmp_dir}/"],
            check=True,
            capture_output=True
        )
        return remote_file_path

    def _hdfs_put(self, remote_file_path: str, hdfs_dest: str) -> None:
        """
        通过 SSH 执行 HDFS put 命令

        Args:
            remote_file_path: 远程服务器上的文件路径
            hdfs_dest: HDFS 目标路径

        Raises:
            subprocess.CalledProcessError: HDFS 操作失败时抛出
        """
        filename = Path(remote_file_path).name
        remote_cmd = (
            f"{self.hadoop_home}/bin/hdfs dfs -mkdir -p {hdfs_dest} && "
            f"{self.hadoop_home}/bin/hdfs dfs -put {remote_file_path} {hdfs_dest}/ && "
            f"rm -f {remote_file_path}"
        )
        subprocess.run(
            ["ssh", f"{self.remote_user}@{self.remote_host}", remote_cmd],
            check=True,
            capture_output=True
        )

    def upload_to_hdfs(self, local_file_path: str, hdfs_dest_dir: Optional[str] = None) -> str:
        """
        上传文件到 HDFS

        Args:
            local_file_path: 本地文件路径
            hdfs_dest_dir: 可选 HDFS 目标目录，不传则自动生成时间戳路径

        Returns:
            HDFS 完整文件路径

        Raises:
            Exception: 上传失败时抛出
        """
        if hdfs_dest_dir is None:
            hdfs_dest_dir = self._generate_hdfs_path()

        filename = Path(local_file_path).name

        try:
            # Step 1: SCP 到远程服务器
            self._scp_to_remote(local_file_path)

            # Step 2: SSH 到远程服务器执行 HDFS 命令
            self._hdfs_put(f"{self.remote_tmp_dir}/{filename}", hdfs_dest_dir)

            # Step 3: 返回 HDFS 完整路径
            hdfs_file_path = f"{hdfs_dest_dir}/{filename}"
            return hdfs_file_path

        except subprocess.CalledProcessError as e:
            raise Exception(f"HDFS 上传失败: {e.stderr.decode('utf-8') if e.stderr else str(e)}")
        except Exception as e:
            raise Exception(f"HDFS 上传异常: {str(e)}")


def convert_to_parquet_and_upload(
    result: Dict[str, Any],
    uploader: HDFSUploader,
    session_id: Optional[str] = None,
    hdfs_path: Optional[str] = None
) -> str:
    """
    将查询结果转换为 Parquet 格式并上传到 HDFS

    Args:
        result: 查询结果字典，包含 truncated_results 和 fields
        uploader: HDFS 上传器实例
        session_id: 可选的会话 ID，用于文件命名（hdfs_path 为空时生效）
        hdfs_path: 可选的自定义 HDFS 路径，支持：
            - 空: 使用时间戳自动生成路径和文件名
            - "filename": 纯文件名，使用时间戳目录
            - "dir/": 以 / 结尾表示目录路径，文件名为时间戳.parquet
            - "dir/filename": 完整路径，最后一段为文件名

    Returns:
        HDFS 文件路径

    Raises:
        ValueError: 结果格式无效时抛出
        Exception: 上传失败时抛出
    """
    import pandas as pd

    # 检查结果格式
    if not isinstance(result, dict):
        raise ValueError(f"无效的结果类型: {type(result)}")

    fields = result.get("fields", [])
    rows = result.get("truncated_results", [])

    # 检查是否为错误结果
    if isinstance(rows, str):
        raise ValueError(f"查询错误: {rows}")

    if not isinstance(rows, list):
        raise ValueError(f"无效的行数据格式: {type(rows)}")

    if not fields:
        raise ValueError("没有可用的数据")

    # 转换为 DataFrame
    df_dict = {}
    for i, field in enumerate(fields):
        df_dict[field] = [row[i] if i < len(row) else None for row in rows]

    df = pd.DataFrame(df_dict)

    # 重命名列以兼容 HDFS 脚本
    df = df.rename(columns={
        "greptime_timestamp": "timestamp",
        "greptime_value": "value"
    })

    # 确保有 metric_name 列
    if "metric_name" not in df.columns:
        df["metric_name"] = "custom_query"

    # 转换 timestamp 为字符串
    if "timestamp" in df.columns:
        df["timestamp"] = df["timestamp"].astype(str)

    # 解析 HDFS 目标路径和文件名
    hdfs_dest_dir, filename = uploader._resolve_hdfs_path(
        custom_path=hdfs_path,
        session_id=session_id
    )

    # 保存为 Parquet（使用解析出的文件名）
    local_file_path = uploader.local_output_dir / filename
    df.to_parquet(local_file_path, index=False)

    # 上传到 HDFS（使用解析出的目录）
    hdfs_result_path = uploader.upload_to_hdfs(
        str(local_file_path),
        hdfs_dest_dir=hdfs_dest_dir
    )

    return hdfs_result_path
