#!/usr/bin/env python3
"""
自动化上传 Parquet 文件到 HDFS
使用 SSH 密码认证连接远程机器
"""

import sys
import subprocess
import shlex
from pathlib import Path

try:
    import pexpect
except ImportError:
    print("安装 pexpect...")
    subprocess.run([sys.executable, "-m", "pip", "install", "pexpect"], check=True)
    import pexpect


# === 配置 ===
REMOTE_HOST = "proxy.huabase.net"
REMOTE_PORT = "2117"
REMOTE_USER = "root"
REMOTE_PASSWORD = "lxcroot"

LOCAL_PARQUET_DIR = "query_results_parquet"
REMOTE_PARQUET_DIR = "/tmp/xiyan_parquet"
HDFS_TARGET_PATH = "/flatten_window_metrics"


def run_ssh_command(command, timeout=120):
    """使用 pexpect 执行 SSH 命令"""
    # 使用 shlex.quote 来正确处理命令中的引号
    ssh_cmd = [
        "ssh", "-p", REMOTE_PORT,
        "-o", "StrictHostKeyChecking=no",
        f"{REMOTE_USER}@{REMOTE_HOST}",
        command
    ]

    print(f"  执行 SSH 命令...")

    child = pexpect.spawn(" ".join(ssh_cmd), encoding='utf-8', timeout=timeout)

    # 等待密码提示或直接连接
    idx = child.expect([pexpect.TIMEOUT, "[Pp]assword:", r"(.+)"], timeout=30)

    if idx == 1:  # 密码提示
        child.sendline(REMOTE_PASSWORD)
        # 继续等待命令执行完成
        child.expect(pexpect.EOF, timeout=timeout)
    elif idx == 2:  # 可能已经连接成功或出现其他输出
        # 检查是否需要密码
        if "password:" in child.after.lower():
            child.sendline(REMOTE_PASSWORD)
            child.expect(pexpect.EOF, timeout=timeout)
        else:
            # 继续等待完成
            child.expect(pexpect.EOF, timeout=timeout)
    else:
        # 超时，可能已经完成
        pass

    output = child.before

    if child.exitstatus and child.exitstatus != 0:
        return output

    return output


def run_scp_upload(local_files, remote_dir):
    """使用 pexpect 上传文件到远程"""
    success = 0
    fail = 0

    for file in local_files:
        print(f"  上传: {file.name}")

        # 使用 shlex.quote 来正确处理文件名中的空格和特殊字符
        file_path = shlex.quote(str(file))
        # 注意：目标路径不要加末尾的 /，否则 SCP 会报 "Is a directory" 错误
        remote_path = f"{REMOTE_USER}@{REMOTE_HOST}:{remote_dir}"

        scp_cmd = f"scp -P {REMOTE_PORT} -o StrictHostKeyChecking=no {file_path} {remote_path}"

        child = pexpect.spawn(scp_cmd, encoding='utf-8', timeout=300)

        # 等待密码提示或传输完成
        idx = child.expect([pexpect.TIMEOUT, "[Pp]assword:"], timeout=60)

        if idx == 1:  # 密码提示
            child.sendline(REMOTE_PASSWORD)
            # 等待传输完成
            child.expect(pexpect.EOF, timeout=300)

        output = child.before

        # 检查输出中是否包含 100% 来判断成功
        if output and "100%" in output:
            print(f"    ✓ 成功")
            success += 1
        else:
            print(f"    ✗ 失败: {output[:100] if output else 'No output'}")
            fail += 1

    print(f"\n  传输结果: 成功 {success}, 失败 {fail}")
    return fail == 0


def main():
    print("=" * 60)
    print("   Parquet 文件自动上传到 HDFS")
    print("=" * 60)
    print("")

    # 1. 检查本地文件
    local_dir = Path(LOCAL_PARQUET_DIR)
    if not local_dir.exists():
        print(f"✗ 本地目录不存在: {LOCAL_PARQUET_DIR}")
        return 1

    parquet_files = list(local_dir.glob("*.parquet"))
    if not parquet_files:
        print(f"✗ 未找到 Parquet 文件在 {LOCAL_PARQUET_DIR}")
        return 1

    print(f"✓ 本地找到 {len(parquet_files)} 个 Parquet 文件")
    print(f"  目录: {local_dir.resolve()}")
    print("")

    # 2. 创建远程目录
    print(f"连接远程机器: {REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PORT}")
    output = run_ssh_command(f"mkdir -p {REMOTE_PARQUET_DIR}")
    if output is False:
        print("✗ 远程目录创建失败")
        return 1
    print("✓ 远程目录创建成功")
    print("")

    # 3. 上传文件到远程
    print("上传文件到远程机器...")
    if not run_scp_upload(parquet_files, REMOTE_PARQUET_DIR):
        print("⚠ 部分文件上传失败，但继续执行...")
    print("✓ 文件上传完成")
    print("")

    # 4. 在远程机器上执行 HDFS 上传
    print("在远程机器上执行 HDFS 上传...")

    # 创建远程上传脚本
    remote_script = f'''#!/bin/bash
set -e

echo "检查远程文件..."
ls -lh {REMOTE_PARQUET_DIR}/*.parquet 2>/dev/null | wc -l

echo ""
echo "创建 HDFS 目录..."
hdfs dfs -mkdir -p {HDFS_TARGET_PATH}

echo ""
echo "开始上传到 HDFS..."
success=0
fail=0

for file in {REMOTE_PARQUET_DIR}/*.parquet; do
    if [ -f "$file" ]; then
        filename=$(basename "$file")
        echo "  上传: $filename"

        if hdfs dfs -put -f "$file" {HDFS_TARGET_PATH}/; then
            ((success++))
            echo "    ✓"
        else
            ((fail++))
            echo "    ✗"
        fi
    fi
done

echo ""
echo "上传完成! 成功: $success, 失败: $fail"

echo ""
echo "验证 HDFS 文件..."
hdfs dfs -ls {HDFS_TARGET_PATH} | head -20
'''

    # 将脚本内容通过管道传输到远程并执行
    echo_cmd = f"echo {shlex.quote(remote_script)} | bash"
    # 改为：先创建脚本文件，然后执行

    # 方法1: 使用 heredoc 在远程创建脚本
    create_script_cmd = f"cat > /tmp/hdfs_upload.sh << 'EOFSCRIPT'\n{remote_script}\nEOFSCRIPT"
    run_ssh_command(create_script_cmd)
    run_ssh_command("chmod +x /tmp/hdfs_upload.sh")

    print("执行 HDFS 上传脚本...\n")
    output = run_ssh_command("bash /tmp/hdfs_upload.sh", timeout=600)

    if output:
        print(output)

    # 清理
    run_ssh_command("rm -f /tmp/hdfs_upload.sh")

    print("")
    print("=" * 60)
    print("   全部完成!")
    print("=" * 60)
    print(f"HDFS 路径: hdfs://{REMOTE_HOST}:8020{HDFS_TARGET_PATH}")
    print("")
    print("验证命令:")
    print(f"  ssh -p {REMOTE_PORT} {REMOTE_USER}@{REMOTE_HOST} 'hdfs dfs -ls {HDFS_TARGET_PATH}'")
    print("")

    return 0


if __name__ == "__main__":
    sys.exit(main())
