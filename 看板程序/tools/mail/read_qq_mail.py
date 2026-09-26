"""Read recent QQ Mail headers through IMAP without modifying the mailbox."""

from __future__ import annotations

import argparse
import email
import getpass
import imaplib
import os
import ssl
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Literal

import keyring
from keyring.errors import KeyringError, PasswordDeleteError


IMAP_HOST = "imap.qq.com"
IMAP_PORT = 993
DEFAULT_ADDRESS = os.environ.get("QQ_MAIL_ADDRESS", "").strip()
CREDENTIAL_SERVICE = "EmbeddedJobTracker.QQMail.IMAP"
AUTH_CODE_FILE_ENV = "QQ_MAIL_AUTH_CODE_FILE"
AuthSource = Literal["file", "keyring", "prompt"]


def decode_mime_header(value: str | None) -> str:
    if not value:
        return ""
    parts: list[str] = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, bytes):
            for encoding in (charset, "utf-8", "gb18030", "latin-1"):
                if not encoding:
                    continue
                try:
                    parts.append(chunk.decode(encoding))
                    break
                except (LookupError, UnicodeDecodeError):
                    continue
            else:
                parts.append(chunk.decode("utf-8", errors="replace"))
        else:
            parts.append(chunk)
    return "".join(parts).strip()


def parse_mail_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.astimezone() if parsed.tzinfo else parsed
    except (TypeError, ValueError, OverflowError):
        return None


def read_recent_headers(address: str, auth_code: str, days: int, limit: int) -> list[dict]:
    since = datetime.now() - timedelta(days=days)
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)
    try:
        client.login(address, auth_code)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法以只读方式打开收件箱。")

        status, data = client.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
        if status != "OK":
            raise RuntimeError("无法搜索收件箱。")

        uids = (data[0] or b"").split()[-limit:]
        rows: list[dict] = []
        for uid in reversed(uids):
            status, payload = client.uid(
                "fetch",
                uid,
                "(BODY.PEEK[HEADER.FIELDS (DATE FROM SUBJECT MESSAGE-ID)])",
            )
            if status != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            message = email.message_from_bytes(payload[0][1])
            rows.append(
                {
                    "uid": uid.decode("ascii", errors="replace"),
                    "time": parse_mail_time(message.get("Date")),
                    "sender": decode_mime_header(message.get("From")),
                    "subject": decode_mime_header(message.get("Subject")),
                }
            )
        return rows
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def load_auth_code_file() -> str | None:
    configured_path = os.environ.get(AUTH_CODE_FILE_ENV, "").strip()
    if not configured_path:
        return None

    path = Path(configured_path)
    if not path.is_file():
        raise RuntimeError("QQ邮箱授权码文件不存在或不是普通文件。")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise RuntimeError("QQ邮箱授权码文件权限过宽，必须仅允许文件所有者读写。")

    auth_code = path.read_text(encoding="utf-8").strip()
    if not auth_code:
        raise RuntimeError("QQ邮箱授权码文件为空。")
    return auth_code


def load_or_prompt_auth_code(address: str) -> tuple[str, AuthSource]:
    file_auth_code = load_auth_code_file()
    if file_auth_code:
        print("已从受限密钥文件读取授权码。")
        return file_auth_code, "file"

    try:
        saved = keyring.get_password(CREDENTIAL_SERVICE, address)
    except Exception as exc:
        print(f"无法读取Windows凭据管理器：{exc}")
        saved = None
    if saved:
        print("已从Windows凭据管理器读取授权码。")
        return saved, "keyring"

    print("请输入QQ邮箱IMAP授权码（输入内容不会显示）：")
    auth_code = getpass.getpass("授权码：").strip()
    return auth_code, "prompt"


def save_auth_code(address: str, auth_code: str) -> None:
    try:
        keyring.set_password(CREDENTIAL_SERVICE, address, auth_code)
        print("授权码已安全保存到Windows凭据管理器。")
    except Exception as exc:
        print(f"授权码未保存，但本次读取已成功：{exc}")


def forget_auth_code(address: str) -> int:
    try:
        keyring.delete_password(CREDENTIAL_SERVICE, address)
    except PasswordDeleteError:
        print("Windows凭据管理器中没有该邮箱的授权码。")
        return 0
    except Exception as exc:
        print(f"删除失败：{exc}")
        return 1
    print("已从Windows凭据管理器删除QQ邮箱授权码。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="只读查看QQ邮箱最近邮件")
    parser.add_argument("--email", default=DEFAULT_ADDRESS, help="QQ邮箱地址")
    parser.add_argument("--days", type=int, default=7, help="读取最近多少天，默认7天")
    parser.add_argument("--limit", type=int, default=100, help="最多显示多少封，默认100封")
    parser.add_argument("--save-auth", action="store_true", help="首次输入后保存到Windows凭据管理器")
    parser.add_argument("--forget-auth", action="store_true", help="删除Windows凭据管理器中的授权码")
    args = parser.parse_args()

    if args.days <= 0 or args.limit <= 0:
        parser.error("--days 和 --limit 必须大于0")
    if not args.email:
        parser.error("请通过 QQ_MAIL_ADDRESS 或 --email 配置邮箱地址")

    if args.forget_auth:
        return forget_auth_code(args.email)

    print(f"邮箱：{args.email}")
    try:
        auth_code, auth_source = load_or_prompt_auth_code(args.email)
    except RuntimeError as exc:
        print(f"读取授权码失败：{exc}")
        return 1
    if not auth_code:
        print("未输入授权码，已取消。")
        return 1

    try:
        rows = read_recent_headers(args.email, auth_code, args.days, args.limit)
    except imaplib.IMAP4.error as exc:
        print(f"QQ邮箱登录失败：{exc}")
        if auth_source == "keyring":
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, args.email)
                print("失效的已保存授权码已删除，下次运行时会重新询问。")
            except KeyringError:
                pass
        print("请确认已开启IMAP服务，并使用邮箱生成的授权码而不是QQ密码。")
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"读取失败：{exc}")
        return 1

    if args.save_auth and auth_source == "prompt":
        save_auth_code(args.email, auth_code)

    print(f"\n最近{args.days}天共读取到 {len(rows)} 封邮件：\n")
    for index, row in enumerate(rows, 1):
        time_text = row["time"].strftime("%Y-%m-%d %H:%M") if row["time"] else "时间未知"
        print(f"[{index:03d}] {time_text}")
        print(f"      发件人：{row['sender'] or '未知'}")
        print(f"      主题：{row['subject'] or '(无主题)'}")

    print("\n只读检查完成：未修改邮件，也未写入飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
