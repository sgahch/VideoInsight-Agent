from __future__ import annotations

import hashlib
import hmac
import re
import smtplib
import ssl
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from functools import lru_cache

from app.config import Settings


def normalize_email(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or any(char.isspace() or ord(char) < 32 for char in value):
        raise ValueError("请输入有效的邮箱地址。")
    try:
        address = Address(addr_spec=value)
        local, domain = address.username, address.domain.encode("idna").decode().lower()
    except (ValueError, TypeError, UnicodeError, HeaderParseError):
        raise ValueError("请输入有效的邮箱地址。") from None
    if (
        not local or len(local) > 64 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
        or local.startswith(".") or local.endswith(".") or ".." in local or "." not in domain
        or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in domain.split("."))
    ):
        raise ValueError("请输入有效的邮箱地址。")
    return f"{local.lower()}@{domain}"


def registration_available(config: Settings) -> bool:
    return bool(config.registration_enabled and config.auth_password_hash and config.smtp_username and config.smtp_password)


def registration_digest(config: Settings, value: str) -> str:
    return hmac.new(config.auth_session_secret.encode(), value.encode(), hashlib.sha256).hexdigest()


@lru_cache(maxsize=1)
def smtp_tls_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def send_verification_email(config: Settings, recipient: str, code: str) -> None:
    sender = normalize_email(config.smtp_username or "")
    message = EmailMessage()
    message["From"] = Address("VideoInsight", addr_spec=sender)
    message["To"] = Address(addr_spec=recipient)
    message["Subject"] = "VideoInsight 邮箱注册验证码"
    message.set_content(
        f"你的 VideoInsight 注册验证码是：{code}\n\n"
        "验证码 10 分钟内有效，仅能使用一次。请勿向任何人透露。\n"
        "如果不是你本人发起的注册，请忽略此邮件。\n"
    )
    with smtplib.SMTP_SSL(config.smtp_host, config.smtp_port, timeout=12, context=smtp_tls_context()) as client:
        client.login(config.smtp_username, config.smtp_password)
        client.send_message(message)
