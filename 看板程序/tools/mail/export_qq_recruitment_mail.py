"""Export recruitment-related QQ Mail messages as local text for analysis."""

from __future__ import annotations

import argparse
import email
import html
import imaplib
import re
import ssl
from collections import Counter
from datetime import date, datetime, timedelta
from email.message import Message
from pathlib import Path

import keyring
from keyring.errors import KeyringError

from tools.mail.read_qq_mail import (
    CREDENTIAL_SERVICE,
    DEFAULT_ADDRESS,
    IMAP_HOST,
    IMAP_PORT,
    decode_mime_header,
    load_or_prompt_auth_code,
    parse_mail_time,
    save_auth_code,
)


from project_paths import EXPORT_DIR

RECRUITMENT_HINTS = (
    "招聘", "校招", "校园招聘", "应聘", "候选人", "职位", "简历", "测评", "笔试", "机考", "面试",
    "offer", "录用", "mokahr", "recruit", "career", "campus", "jobs", "zhiye", "51job", "zhaopin",
)


def parse_iso_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须为 YYYY-MM-DD") from exc


def decode_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    declared = part.get_content_charset()
    for encoding in (declared, "utf-8", "gb18030", "latin-1"):
        if not encoding:
            continue
        try:
            return payload.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", errors="replace")


def html_to_text(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return html.unescape(value)


def normalize_body(value: str, limit: int = 16000) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r" *\n *", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if len(value) > limit:
        value = value[:limit].rstrip() + "\n[正文过长，已截断]"
    return value


def extract_body(message: Message) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        if disposition == "attachment":
            continue
        content_type = part.get_content_type().lower()
        if content_type == "text/plain":
            plain_parts.append(decode_part(part))
        elif content_type == "text/html":
            html_parts.append(html_to_text(decode_part(part)))
    selected = "\n".join(plain_parts).strip() or "\n".join(html_parts).strip()
    return normalize_body(selected)


def classify(subject: str, sender: str, body: str) -> str | None:
    subject_lower = subject.lower()
    sample = f"{subject}\n{sender}\n{body[:6000]}".lower()

    # 结果类必须存在明确结论，避免把招聘宣传中的“Offer发放”误判为已录用。
    rejection_patterns = (
        r"很遗憾[^。\n]{0,120}(?:不太?合适|不匹配|未通过|未能通过|无法进入|不再推进)",
        r"(?:遗憾|抱歉)[^。\n]{0,120}(?:不匹配|无法进入下一个阶段|不再推进)",
        r"(?:您的?|你(?:的)?)(?:简历|申请|面试|笔试|测评)[^。\n]{0,80}(?:未通过|不合适|无法进入)",
        r"(?:本次|此次)?(?:招聘|应聘|面试|申请)流程(?:已经|已)?(?:终止|结束)",
    )
    if any(re.search(pattern, sample) for pattern in rejection_patterns):
        return "录用/淘汰"
    if re.search(r"(?:offer\s*(?:letter|通知|函|确认)|录用(?:通知|意向书|确认)|正式录用)", subject_lower):
        return "录用/淘汰"

    # 面试后的满意度问卷不是新的面试待办。
    if re.search(r"面试(?:满意度|体验).{0,12}(?:调查|评价|问卷)|面试满意度调查", subject_lower):
        return "其他招聘通知"

    # 实际流程邀请优先看主题，正文只接受带“邀请/安排/参加”的明确行动句。
    if re.search(r"笔试|机考|在线编程|编程测试|上机考试|考试邀请", subject_lower):
        return "笔试/机考"
    if re.search(r"(?:邀|邀请).{0,20}(?:参加|进行).{0,30}考试|岗位.{0,8}考试", subject_lower):
        return "笔试/机考"
    if re.search(r"测评|人才测验|性格测试|心理测试|ai测评|在线测验", subject_lower):
        return "测评"
    if re.search(r"ai\s*面试", subject_lower):
        return "测评"
    if re.search(r"面试|面谈|初试|复试|终面|hr面", subject_lower):
        return "面试"

    promotional_subject = bool(re.search(r"宣讲会|空宣|双选会|招聘简章|岗位推荐|邀您投递", subject_lower))
    if not promotional_subject and re.search(r"(?:邀请|请于|参加)[\s\S]{0,50}(?:笔试|机考|在线编程|上机考试)", body[:3000], re.I):
        return "笔试/机考"
    if not promotional_subject and re.search(r"(?:邀请|请于|完成)[\s\S]{0,50}(?:在线)?测评", body[:3000], re.I):
        return "测评"
    if not promotional_subject and re.search(r"(?:现|诚挚|正式)?邀请(?:您|你)?[^。\n]{0,40}(?:参加)?(?:面试|面谈|初试|复试|终面)", body[:3000], re.I):
        return "面试"

    delivery_patterns = (
        r"感谢.{0,10}(?:投递|应聘)",
        r"投递成功|申请成功|简历已收到|收到.{0,10}简历",
    )
    if any(re.search(pattern, subject_lower) for pattern in delivery_patterns):
        return "投递确认"
    if any(re.search(pattern, body[:2500], re.I) for pattern in delivery_patterns):
        return "投递确认"

    if any(keyword.lower() in sample for keyword in RECRUITMENT_HINTS):
        return "其他招聘通知"
    return None


def fetch_messages(address: str, auth_code: str, start: date, end: date, limit: int) -> list[dict]:
    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)
    try:
        client.login(address, auth_code)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法以只读方式打开收件箱。")

        before = end + timedelta(days=1)
        status, data = client.uid(
            "search",
            None,
            "SINCE",
            start.strftime("%d-%b-%Y"),
            "BEFORE",
            before.strftime("%d-%b-%Y"),
        )
        if status != "OK":
            raise RuntimeError("无法搜索指定日期范围。")

        rows: list[dict] = []
        for uid in (data[0] or b"").split()[-limit:]:
            status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
            if status != "OK" or not payload:
                continue
            raw = next(
                (item[1] for item in payload if isinstance(item, tuple) and isinstance(item[1], bytes)),
                None,
            )
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            subject = decode_mime_header(message.get("Subject"))
            sender = decode_mime_header(message.get("From"))
            body = extract_body(message)
            category = classify(subject, sender, body)
            if not category:
                continue
            rows.append(
                {
                    "uid": uid.decode("ascii", errors="replace"),
                    "time": parse_mail_time(message.get("Date")),
                    "sender": sender,
                    "subject": subject,
                    "category": category,
                    "body": body,
                }
            )
        rows.sort(key=lambda row: row["time"] or datetime.min.astimezone())
        return rows
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass


def render_text(rows: list[dict], address: str, start: date, end: date) -> str:
    counts = Counter(row["category"] for row in rows)
    lines = [
        "QQ邮箱招聘邮件分析文本",
        f"邮箱：{address}",
        f"范围：{start.isoformat()} 至 {end.isoformat()}",
        f"招聘相关邮件：{len(rows)} 封",
        "分类：" + "；".join(f"{name} {count}" for name, count in counts.items()),
        "",
    ]
    for index, row in enumerate(rows, 1):
        time_text = row["time"].strftime("%Y-%m-%d %H:%M:%S %z") if row["time"] else "时间未知"
        lines.extend(
            [
                "=" * 80,
                f"邮件 {index:03d}",
                f"分类：{row['category']}",
                f"时间：{time_text}",
                f"发件人：{row['sender'] or '未知'}",
                f"主题：{row['subject'] or '(无主题)'}",
                "正文：",
                row["body"] or "(无可读取的文本正文)",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="只读导出QQ邮箱中的招聘相关邮件")
    parser.add_argument("--email", default=DEFAULT_ADDRESS)
    parser.add_argument("--from-date", required=True, type=parse_iso_date)
    parser.add_argument("--to-date", required=True, type=parse_iso_date)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.from_date > args.to_date:
        parser.error("--from-date 不能晚于 --to-date")
    if args.limit <= 0:
        parser.error("--limit 必须大于0")

    try:
        auth_code, auth_source = load_or_prompt_auth_code(args.email)
    except RuntimeError as exc:
        print(f"读取授权码失败：{exc}")
        return 1
    if not auth_code:
        print("未输入授权码，已取消。")
        return 1

    print(f"正在只读扫描 {args.from_date} 至 {args.to_date} 的收件箱邮件...")
    try:
        rows = fetch_messages(args.email, auth_code, args.from_date, args.to_date, args.limit)
    except imaplib.IMAP4.error as exc:
        if auth_source == "keyring":
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, args.email)
                print("已保存的授权码失效，已从Windows凭据管理器删除。")
            except KeyringError:
                pass
        print(f"QQ邮箱登录失败：{exc}")
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"读取失败：{exc}")
        return 1

    if auth_source == "prompt":
        save_auth_code(args.email, auth_code)

    EXPORT_DIR.mkdir(exist_ok=True)
    output = args.output or EXPORT_DIR / (
        f"qq_recruitment_{args.from_date.isoformat()}_{args.to_date.isoformat()}.txt"
    )
    output.write_text(render_text(rows, args.email, args.from_date, args.to_date), encoding="utf-8")
    print(f"已导出 {len(rows)} 封招聘相关邮件：{output}")
    print("未修改邮件，未写入飞书。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
