"""Create a message in the QQ Mail Drafts folder without sending it."""

from __future__ import annotations

import argparse
import imaplib
import re
import ssl
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from tools.mail.read_qq_mail import (
    CREDENTIAL_SERVICE,
    DEFAULT_ADDRESS,
    IMAP_HOST,
    IMAP_PORT,
    load_or_prompt_auth_code,
    save_auth_code,
)


DEFAULT_DRAFTS_FOLDER = "Drafts"


def split_addresses(value: str) -> list[str]:
    """Split comma/semicolon-delimited addresses and reject malformed entries."""
    addresses = [item.strip() for item in re.split(r"[,;，；]", value) if item.strip()]
    if not addresses:
        raise ValueError("至少需要填写一个收件人。")

    invalid = [item for item in addresses if "@" not in parseaddr(item)[1]]
    if invalid:
        raise ValueError("邮箱地址格式不正确：" + "、".join(invalid))
    return addresses


def read_multiline_body() -> str:
    print("请输入正文；单独输入一行 . 后按回车结束：")
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line == ".":
            break
        lines.append(line)
    return "\n".join(lines).strip()


def collect_message_fields(args: argparse.Namespace) -> tuple[list[str], str, str]:
    recipient_text = args.recipients or input("收件人（多个邮箱用逗号分隔）：").strip()
    recipients = split_addresses(recipient_text)

    subject = (args.subject or input("主题：").strip()).strip()
    if not subject:
        raise ValueError("主题不能为空。")

    if args.body_file:
        try:
            body = args.body_file.read_text(encoding="utf-8-sig").strip()
        except OSError as exc:
            raise ValueError(f"无法读取正文文件：{exc}") from exc
    elif args.body is not None:
        body = args.body.strip()
    else:
        body = read_multiline_body()

    if not body:
        raise ValueError("正文不能为空。")
    return recipients, subject, body


def build_message(address: str, recipients: list[str], subject: str, body: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = address
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["X-Unsent"] = "1"
    message.set_content(body, charset="utf-8")
    return message


def save_draft(address: str, auth_code: str, message: EmailMessage, folder: str) -> None:
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)
    try:
        client.login(address, auth_code)
        status, response = client.append(folder, "(\\Draft \\Seen)", None, message.as_bytes())
        if status != "OK":
            detail = response[0].decode("utf-8", errors="replace") if response else "未知错误"
            raise RuntimeError(f"QQ邮箱未接受草稿：{detail}")
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="创建QQ邮件草稿（只保存，不发送）")
    parser.add_argument("--email", default=DEFAULT_ADDRESS, help="发件QQ邮箱")
    parser.add_argument("--to", dest="recipients", help="收件人，多个邮箱用逗号分隔")
    parser.add_argument("--subject", help="邮件主题")
    body_group = parser.add_mutually_exclusive_group()
    body_group.add_argument("--body", help="邮件正文")
    body_group.add_argument("--body-file", type=Path, help="UTF-8文本正文文件")
    parser.add_argument("--folder", default=DEFAULT_DRAFTS_FOLDER, help="IMAP草稿箱名称")
    parser.add_argument("--preview", action="store_true", help="仅检查输入，不连接邮箱")
    args = parser.parse_args()

    try:
        recipients, subject, body = collect_message_fields(args)
    except ValueError as exc:
        print(f"输入有误：{exc}")
        return 2

    print("\n草稿信息")
    print("- 收件人：" + ", ".join(recipients))
    print("- 主题：" + subject)
    print(f"- 正文：{len(body)} 个字符")

    if args.preview:
        print("预览完成：未连接邮箱，未创建草稿。")
        return 0

    try:
        auth_code, auth_source = load_or_prompt_auth_code(args.email)
    except RuntimeError as exc:
        print(f"读取授权码失败：{exc}")
        return 1
    if not auth_code:
        print("未输入授权码，已取消。")
        return 1

    message = build_message(args.email, recipients, subject, body)
    try:
        save_draft(args.email, auth_code, message, args.folder)
    except imaplib.IMAP4.error as exc:
        if auth_source == "keyring":
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, args.email)
                print("已保存的授权码失效，已从Windows凭据管理器删除。")
            except KeyringError:
                pass
        print(f"QQ邮箱登录或写入失败：{exc}")
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"创建草稿失败：{exc}")
        return 1

    if auth_source == "prompt":
        save_auth_code(args.email, auth_code)

    print("草稿已保存到QQ邮箱草稿箱，邮件尚未发送。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
