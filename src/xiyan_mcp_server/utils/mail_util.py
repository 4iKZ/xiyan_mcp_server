"""脚本完成通知邮件工具

约定：
- 邮件配置统一放在 config.yml 的 `mail` 节点
- 所有错误一律 try/except 转 logger.warning，对外不抛异常（调用方无需防御性包裹）
- 默认走 SSL 465 端口；如确需 STARTTLS，可改用 smtplib.SMTP(host, port) + starttls()
"""

import logging
import smtplib
import ssl
from email.mime.text import MIMEText
from email.utils import formatdate
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


def _load_mail_cfg(config_path: str) -> Optional[Dict[str, Any]]:
    """从 yml 读 mail 节点；enabled=false 或缺失时返回 None。"""
    try:
        with open(config_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.warning(f"config 文件不存在，跳过邮件通知: {config_path}")
        return None
    except Exception as e:
        logger.warning(f"读取 config 失败，跳过邮件通知: {e}")
        return None

    mail = cfg.get("mail")
    if not mail or not mail.get("enabled", False):
        return None
    if not mail.get("to"):
        logger.warning("mail.to 为空，跳过邮件通知")
        return None
    return mail


def _render_subject(script_name: str, status: str) -> str:
    label = {
        "success": "完成",
        "partial": "完成（含失败）",
        "failed": "失败",
    }.get(status, "完成")
    return f"[XiYan] {script_name} {label}"


def _render_body(script_name: str, status: str, stats: Dict[str, Any]) -> str:
    lines = [
        f"脚本: {script_name}",
        f"状态: {status}",
        f"时间: {formatdate(localtime=True)}",
        "",
        "统计:",
    ]
    for k, v in stats.items():
        lines.append(f"  - {k}: {v}")
    return "\n".join(lines)


def send_completion_email(
    script_name: str,
    status: str,
    stats: Dict[str, Any],
    config_path: str = "src/xiyan_mcp_server/config.yml",
) -> bool:
    """发送脚本完成通知邮件。

    Args:
        script_name: 触发邮件的脚本名（会出现在主题中）
        status: "success" | "partial" | "failed"
        stats: 自定义键值对，会拼到正文
        config_path: config.yml 路径

    Returns:
        True=发送成功；False=未发送/失败（**绝不抛异常**）
    """
    mail = _load_mail_cfg(config_path)
    if mail is None:
        return False

    host = mail["host"]
    port = int(mail.get("port", 465))
    use_ssl = bool(mail.get("use_ssl", port == 465))
    username = mail["username"]
    password = mail["password"]
    from_addr = mail["from"]
    to_addrs = list(mail["to"])
    encoding = mail.get("default-encoding", "utf-8")

    subject = _render_subject(script_name, status)
    body = _render_body(script_name, status, stats)

    msg = MIMEText(body, "plain", encoding)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    msg["Date"] = formatdate(localtime=True)

    try:
        ctx = ssl.create_default_context()
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as smtp:
                smtp.login(username, password)
                smtp.sendmail(from_addr, to_addrs, msg.as_string())
        else:
            with smtplib.SMTP(host, port, timeout=30) as smtp:
                smtp.ehlo()
                smtp.starttls(context=ctx)
                smtp.login(username, password)
                smtp.sendmail(from_addr, to_addrs, msg.as_string())
        logger.info(f"完成通知邮件已发送 → {to_addrs}（主题: {subject}）")
        return True
    except Exception as e:
        logger.warning(f"发送完成通知邮件失败 ({script_name}): {type(e).__name__}: {e}")
        return False
