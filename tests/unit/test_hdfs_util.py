"""hdfs_util.py 单元测试（P0-GAP-06）"""

import pytest
from xiyan_mcp_server.utils.hdfs_util import HDFSUploader


@pytest.fixture
def uploader(tmp_path):
    config = {
        "remote_host": "localhost",
        "remote_user": "user",
        "remote_tmp_dir": "/tmp",
        "hdfs_root_dir": "/user_custom_data",
        "hadoop_home": "/opt/hadoop",
        "local_output_dir": str(tmp_path),
    }
    return HDFSUploader(config)


class TestSanitizePath:
    """路径安全过滤"""

    def test_empty_returns_empty(self):
        assert HDFSUploader._sanitize_path("") == ""

    def test_null_byte_raises(self):
        with pytest.raises(ValueError, match="非法字符"):
            HDFSUploader._sanitize_path("/path/with\0/null")

    def test_parent_directory_raises(self):
        with pytest.raises(ValueError, match="路径遍历"):
            HDFSUploader._sanitize_path("/path/../etc/passwd")

    def test_illegal_chars_raises(self):
        with pytest.raises(ValueError, match="不允许的字符"):
            HDFSUploader._sanitize_path("/path/with spaces!")

    def test_valid_path_unchanged(self):
        assert HDFSUploader._sanitize_path("my_dir/file.txt") == "my_dir/file.txt"

    def test_dots_in_filename_allowed(self):
        assert HDFSUploader._sanitize_path("file.name.txt") == "file.name.txt"

    def test_hyphens_underscores_allowed(self):
        assert (
            HDFSUploader._sanitize_path("my_dir/sub_dir/file-1_v2.txt")
            == "my_dir/sub_dir/file-1_v2.txt"
        )


class TestEnsureParquetExtension:
    """Parquet 后缀补全"""

    def test_adds_extension(self):
        assert HDFSUploader._ensure_parquet_extension("report") == "report.parquet"

    def test_preserves_existing(self):
        assert (
            HDFSUploader._ensure_parquet_extension("report.parquet") == "report.parquet"
        )

    def test_empty_returns_parquet(self):
        assert HDFSUploader._ensure_parquet_extension("") == ".parquet"


class TestValidateHdfsPath:
    """路径范围校验"""

    def test_path_inside_root_ok(self, uploader):
        uploader._validate_hdfs_path("/user_custom_data/batch/parquet")

    def test_path_equals_root_ok(self, uploader):
        uploader._validate_hdfs_path("/user_custom_data")

    def test_path_outside_root_raises(self, uploader):
        with pytest.raises(ValueError, match="根目录之外"):
            uploader._validate_hdfs_path("/etc/passwd")


class TestResolveHdfsPath:
    """HDFS 路径解析（5 种模式）"""

    def test_none_path_generates_timestamp(self, uploader):
        dest, filename = uploader._resolve_hdfs_path()
        assert dest.startswith("/user_custom_data/")
        assert filename.endswith(".parquet")

    def test_pure_filename(self, uploader):
        dest, filename = uploader._resolve_hdfs_path(custom_path="report.csv")
        assert dest.startswith("/user_custom_data/")
        assert filename == "report.csv.parquet"

    def test_directory_path(self, uploader):
        dest, filename = uploader._resolve_hdfs_path(custom_path="project_a/")
        assert dest == "/user_custom_data/project_a"
        assert filename.endswith(".parquet")

    def test_full_path(self, uploader):
        dest, filename = uploader._resolve_hdfs_path(custom_path="project/report")
        assert dest == "/user_custom_data/project"
        assert filename == "report.parquet"

    def test_session_id_used_when_no_custom_path(self, uploader):
        dest, filename = uploader._resolve_hdfs_path(session_id="sess123")
        assert "sess123" in filename

    def test_session_id_ignored_when_custom_path(self, uploader):
        dest, filename = uploader._resolve_hdfs_path(
            custom_path="report.csv", session_id="sess123"
        )
        assert filename == "report.csv.parquet"

    def test_path_outside_root_raises(self, uploader):
        with pytest.raises(ValueError, match="根目录之外"):
            uploader._validate_hdfs_path("/etc/passwd")


class TestHDFSUploaderInit:
    """初始化默认值"""

    def test_default_values(self, tmp_path):
        config = {}
        u = HDFSUploader(config)
        assert u.remote_host == "172.19.19.118"
        assert u.hdfs_root_dir == "/user_custom_data"
        assert u.local_output_dir.exists()
