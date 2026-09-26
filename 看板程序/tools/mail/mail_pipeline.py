"""Recruitment-mail classification, metadata extraction and archive helpers."""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import date, datetime, timedelta
from email.message import Message
from pathlib import Path
from urllib.parse import quote

from company_aliases import company_search_aliases
from tools.mail.export_qq_recruitment_mail import classify, decode_part, extract_body
from tools.mail.read_qq_mail import decode_mime_header, parse_mail_time


from project_paths import DATA_DIR

ROOT = DATA_DIR
ARCHIVE_DIR = ROOT / "公司投递"
SYNC_DIR = ROOT / "同步记录"
PENDING_DIR = ROOT / "待更新"
STATE_FILE = SYNC_DIR / "mail_sync_state.json"
PROCESSED_FILE = SYNC_DIR / "processed_mails.json"
DASHBOARD_FILE = SYNC_DIR / "mail_dashboard.json"

COMPANY_ALIASES = company_search_aliases()

UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
HREF_RE = re.compile(r"(?i)href\s*=\s*[\"']([^\"']+)[\"']")


def ensure_directories() -> None:
    for path in (ARCHIVE_DIR, SYNC_DIR, PENDING_DIR, SYNC_DIR / "分析历史"):
        path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def safe_name(value: str, fallback: str = "未命名") -> str:
    value = UNSAFE_FILENAME.sub("_", value).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    return (value[:80] or fallback).strip()


def normalize_message_id(value: str | None, raw: bytes) -> str:
    value = (value or "").strip().lower()
    if value:
        return value
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def identify_company(subject: str, sender: str, body: str) -> str:
    # Subject and sender identify the recruiting company more reliably than
    # browser names, partners and unrelated employers mentioned in the body.
    for sample in (subject.lower(), sender.lower(), body[:3000].lower()):
        for company, aliases in COMPANY_ALIASES:
            if any(alias.lower() in sample for alias in aliases):
                return company

    company_match = re.search(
        r"([\u4e00-\u9fffA-Za-z（）()]{2,35}?(?:股份有限公司|有限责任公司|有限公司|集团))",
        f"{subject}\n{body[:1500]}",
    )
    if company_match:
        return safe_name(company_match.group(1), "待识别")

    display = sender.split("<", 1)[0].strip().strip('"')
    display = re.sub(r"(?:校园)?招聘|人才招聘|招聘中心|人力资源|no-?reply", "", display, flags=re.I)
    display = display.strip(" -_｜|：:")
    looks_like_person = bool(re.fullmatch(r"[\u4e00-\u9fff]{2,4}", display))
    if display and "@" not in display and len(display) <= 30 and not looks_like_person:
        return safe_name(display, "待识别")
    return "待识别"


def extract_job(subject: str, body: str) -> str:
    sample = f"{subject}\n{body[:5000]}"
    patterns = (
        r"投递[^\n]{0,30}?公司的?([^\n，。]{2,60}?)(?:职位|岗位)",
        r"(?:应聘|投递|申请)(?:职位|岗位)?[：:]?\s*([^\n，。]{2,60}?(?:工程师|开发|方向))",
        r"(?:职位名称|应聘职位|申请职位|投递职位)[：:]\s*([^\n，。]{2,80})",
    )
    for pattern in patterns:
        match = re.search(pattern, sample, re.I)
        if match:
            value = re.sub(r"\s+", " ", match.group(1)).strip(" -_：:")
            if value:
                return value[:100]
    return ""


def extract_urls(message: Message) -> list[str]:
    urls: list[str] = []
    parts = message.walk() if message.is_multipart() else (message,)
    for part in parts:
        if part.is_multipart() or (part.get_content_disposition() or "").lower() == "attachment":
            continue
        if part.get_content_type().lower() not in ("text/plain", "text/html"):
            continue
        value = html.unescape(decode_part(part))
        urls.extend(HREF_RE.findall(value))
        urls.extend(URL_RE.findall(value))

    cleaned: list[str] = []
    seen: set[str] = set()
    for url in urls:
        url = html.unescape(url).rstrip(".,;，。；)]}")
        if not url.lower().startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned[:50]


def choose_action_url(urls: list[str]) -> str:
    ignored = ("unsubscribe", "privacy", "agreement", "logo", "image", "static", "beian")
    preferred = ("nowcoder", "mokahr", "feishu", "hire", "career", "campus", "exam", "interview")
    asset_suffix = re.compile(r"\.(?:png|jpe?g|gif|webp|svg|css|js)(?:[?#]|$)", re.IGNORECASE)
    candidates = [
        url for url in urls
        if not any(word in url.lower() for word in ignored) and not asset_suffix.search(url)
    ]
    return next((url for url in candidates if any(word in url.lower() for word in preferred)), candidates[0] if candidates else "")


def _local_datetime(year: int, month: int, day: int, hour: int = 23, minute: int = 59) -> datetime | None:
    try:
        if hour == 24 and minute == 0:
            return (datetime(year, month, day).astimezone() + timedelta(days=1))
        return datetime(year, month, day, hour, minute).astimezone()
    except ValueError:
        return None


def extract_dates(body: str, received_at: datetime | None) -> list[datetime]:
    year_hint = received_at.year if received_at else datetime.now().year
    found: list[tuple[int, datetime]] = []
    patterns = (
        re.compile(r"(?P<y>20\d{2})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})日?(?:\s*[（(][^）)\n]{0,12}[）)])?\s*(?P<h>\d{1,2})[:：](?P<min>\d{2})"),
        re.compile(r"(?<!\d)(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*[（(][^）)\n]{0,12}[）)])?\s*(?P<h>\d{1,2})[:：](?P<min>\d{2})"),
        re.compile(r"(?P<y>20\d{2})[年\-/\.](?P<m>\d{1,2})[月\-/\.](?P<d>\d{1,2})日?(?:\s*(?P<h>\d{1,2})[:：](?P<min>\d{2}))?"),
        re.compile(r"(?<!\d)(?P<m>\d{1,2})月(?P<d>\d{1,2})日(?:\s*(?P<h>\d{1,2})[:：](?P<min>\d{2}))?"),
    )
    for pattern in patterns:
        for match in pattern.finditer(body):
            year = int(match.groupdict().get("y") or year_hint)
            hour = int(match.groupdict().get("h") or 23)
            minute = int(match.groupdict().get("min") or 59)
            value = _local_datetime(year, int(match.group("m")), int(match.group("d")), hour, minute)
            if value:
                found.append((match.start(), value))
    found.sort(key=lambda item: item[0])
    output: list[datetime] = []
    for _, value in found:
        same_day = next((index for index, old in enumerate(output) if old.date() == value.date()), None)
        if same_day is None:
            output.append(value)
        elif output[same_day].time().hour == 23 and output[same_day].time().minute == 59 and value.time() != output[same_day].time():
            output[same_day] = value
    return output


def extract_deadline(body: str, received_at: datetime | None) -> datetime | None:
    dates = extract_dates(body, received_at)
    date_token = r"(?:20\d{2}[年\-/\.]\d{1,2}[月\-/\.]\d{1,2}日?(?:\s*\d{1,2}[:：]\d{2})?|\d{1,2}月\d{1,2}日(?:\s*\d{1,2}[:：]\d{2})?)"
    for match in re.finditer(date_token + r"[^。\n]{0,35}(?:截止|失效|前(?:完成|提交|选择)?)", body, re.I):
        nearby = extract_dates(match.group(0), received_at)
        if nearby:
            return nearby[-1]
    for match in re.finditer(r"(?:截止|有效期|失效|请于|之前|前完成|最晚)[^\n。]{0,120}", body, re.I):
        nearby = extract_dates(match.group(0), received_at)
        if nearby:
            return nearby[0]
    valid_days = re.search(r"(?:收到|接收)[^\n。]{0,30}?(\d{1,2})\s*天(?:内|有效)", body)
    if not valid_days:
        valid_days = re.search(r"(?:有效期|有效时间)(?:为|：|:)?.{0,10}?(\d{1,2})\s*(?:个)?(?:自然)?[天日]", body)
    if valid_days and received_at:
        return received_at + timedelta(days=int(valid_days.group(1)))
    valid_hours = re.search(r"(?<!\d)(\d{1,3})\s*(?:小时|h)\s*(?:内|有效)", body, re.I)
    if not valid_hours:
        valid_hours = re.search(r"(?:有效期|有效时间)(?:为|：|:)?.{0,10}?(\d{1,3})\s*(?:个)?小时", body, re.I)
    if valid_hours and received_at:
        return received_at + timedelta(hours=int(valid_hours.group(1)))
    return dates[-1] if dates else None


def determine_event(category: str, subject: str, body: str) -> tuple[str, str, str]:
    sample = f"{subject}\n{body[:5000]}".lower()
    if re.search(r"(?:知识产权和商业秘密保护)?承诺书.{0,20}签署|请.{0,20}签署.{0,20}承诺书", sample):
        return "补充材料", "", "签署知识产权和商业秘密保护承诺书"
    if re.search(r"更新.{0,10}简历|补充.{0,10}(?:简历|材料)", sample):
        return "补充材料", "", "按要求更新简历或补充材料"
    if re.search(r"(?:预约面试|面试预约)(?:成功|已确认)", subject):
        return "面试已预约", "一面", "查看预约详情并准备面试"
    if category == "录用/淘汰":
        if re.search(r"遗憾|未通过|不再推进|流程(?:终止|结束)|不太?合适|不匹配|无法进入", sample):
            return "淘汰", "已挂", "无需操作，确认飞书结果"
        return "录用", "OC", "核对录用信息并及时回复"
    if category == "面试":
        if re.search(r"二面|复试", sample):
            return "二面", "二面", "确认面试时间并准备"
        if re.search(r"三面|终面|hr面", sample):
            return "终面", "三面", "确认面试时间并准备"
        return "面试", "一面", "选择或确认面试时间"
    if category == "笔试/机考":
        return "笔试/机考", "机考", "按邮件时间完成笔试"
    if category == "测评":
        return "测评", "测评", "在截止时间前完成测评"
    if category == "投递确认":
        return "投递确认", "已投递", "无需操作"
    return "招聘通知", "", "阅读邮件并判断是否需要操作"


def urgency_for(event: str, deadline: datetime | None, now: datetime | None = None) -> tuple[str, int, str]:
    now = now or datetime.now().astimezone()
    if event == "淘汰":
        return "已结束", 90, "流程已结束"
    if deadline:
        seconds = (deadline - now).total_seconds()
        days = int(seconds // 86400)
        if seconds < 0:
            return "已过期", 80, f"已超过时间 {abs(days)} 天"
        if seconds <= 86400:
            return "紧急", 0, "24 小时内"
        if seconds <= 3 * 86400:
            return "优先", 10, f"剩余约 {days + 1} 天"
        if seconds <= 7 * 86400:
            return "注意", 20, f"剩余约 {days + 1} 天"
    if event in ("面试", "二面", "终面", "笔试/机考", "测评", "补充材料"):
        return "待处理", 30, "需要确认完成情况"
    return "普通", 60, "无需立即处理"


def analyze_message(message: Message, raw: bytes, uid: str, uidvalidity: str) -> dict | None:
    subject = decode_mime_header(message.get("Subject"))
    sender = decode_mime_header(message.get("From"))
    body = extract_body(message)
    category = classify(subject, sender, body)
    if not category:
        return None

    received_at = parse_mail_time(message.get("Date"))
    if received_at and received_at.tzinfo is None:
        received_at = received_at.astimezone()
    company = identify_company(subject, sender, body)
    urls = extract_urls(message)
    event, suggested_progress, action = determine_event(category, subject, body)
    deadline = extract_deadline(f"{subject}\n{body}", received_at) if event not in ("投递确认", "淘汰", "面试已预约") else None
    urgency, urgency_rank, urgency_note = urgency_for(event, deadline)
    return {
        "schema_version": 1,
        "mail_key": f"{uidvalidity}:{uid}",
        "uid": uid,
        "uidvalidity": uidvalidity,
        "message_id": normalize_message_id(message.get("Message-ID"), raw),
        "received_at": received_at.isoformat() if received_at else "",
        "sender": sender,
        "subject": subject,
        "company": company,
        "job": extract_job(subject, body),
        "category": category,
        "event": event,
        "suggested_progress": suggested_progress,
        "action": action,
        "deadline": deadline.isoformat() if deadline else "",
        "urgency": urgency,
        "urgency_rank": urgency_rank,
        "urgency_note": urgency_note,
        "urls": urls,
        "action_url": choose_action_url(urls),
        "body": body,
    }


def render_archive_text(meta: dict) -> str:
    lines = [
        f"公司：{meta['company']}",
        f"岗位：{meta.get('job') or '待匹配'}",
        f"邮件类型：{meta['event']}",
        f"建议进展：{meta.get('suggested_progress') or '不变'}",
        f"紧急程度：{meta['urgency']}（{meta['urgency_note']}）",
        f"邮件时间：{meta.get('received_at') or '未知'}",
        f"截止时间：{meta.get('deadline') or '未识别'}",
        f"下一步：{meta['action']}",
        f"发件人：{meta['sender']}",
        f"主题：{meta['subject']}",
        f"操作链接：{meta.get('action_url') or '未识别'}",
        "",
        "邮件正文",
        "=" * 72,
        meta.get("body") or "(无可读取正文)",
        "",
        "邮件内链接",
        "=" * 72,
    ]
    lines.extend(meta.get("urls") or ["(无)"])
    return "\n".join(lines).rstrip() + "\n"


def archive_message(meta: dict, raw: bytes) -> dict:
    company_dir = ARCHIVE_DIR / safe_name(meta["company"], "待识别")
    company_dir.mkdir(parents=True, exist_ok=True)
    received = datetime.fromisoformat(meta["received_at"]) if meta.get("received_at") else datetime.now().astimezone()
    stem = safe_name(f"{received:%Y-%m-%d_%H%M}_{meta['event']}_{meta['uid']}")
    eml_path = company_dir / f"{stem}.eml"
    txt_path = company_dir / f"{stem}.txt"
    json_path = company_dir / f"{stem}.json"

    for previous_path in ARCHIVE_DIR.glob("*/*.json"):
        if previous_path.name == "company.json" or previous_path.resolve() == json_path.resolve():
            continue
        previous = load_json(previous_path, None)
        if not isinstance(previous, dict) or previous.get("message_id") != meta["message_id"]:
            continue
        previous["superseded"] = True
        previous["superseded_at"] = datetime.now().astimezone().isoformat()
        previous["superseded_by"] = json_path.relative_to(ROOT).as_posix()
        write_json(previous_path, previous)
    eml_path.write_bytes(raw)
    txt_path.write_text(render_archive_text(meta), encoding="utf-8")

    relative_txt = txt_path.relative_to(ARCHIVE_DIR)
    relative_eml = eml_path.relative_to(ARCHIVE_DIR)
    meta = dict(meta)
    meta.pop("body", None)
    meta["archive_text"] = txt_path.relative_to(ROOT).as_posix()
    meta["archive_eml"] = eml_path.relative_to(ROOT).as_posix()
    meta["archive_url"] = "/mail-archive/" + "/".join(quote(part) for part in relative_txt.parts)
    meta["raw_url"] = "/mail-archive/" + "/".join(quote(part) for part in relative_eml.parts)
    write_json(json_path, meta)
    company_index_path = company_dir / "company.json"
    company_index = load_json(company_index_path, {"company": meta["company"], "messages": []})
    messages = [item for item in company_index.get("messages", []) if item.get("message_id") != meta["message_id"]]
    messages.append({
        "message_id": meta["message_id"],
        "received_at": meta.get("received_at"),
        "event": meta.get("event"),
        "subject": meta.get("subject"),
        "archive_text": meta.get("archive_text"),
    })
    messages.sort(key=lambda item: item.get("received_at") or "")
    company_index.update({
        "company": meta["company"],
        "updated_at": datetime.now().astimezone().isoformat(),
        "last_mail_at": messages[-1].get("received_at") if messages else "",
        "mail_count": len(messages),
        "messages": messages,
    })
    write_json(company_index_path, company_index)
    return meta


def iter_archived_metadata() -> list[dict]:
    if not ARCHIVE_DIR.exists():
        return []
    rows: list[dict] = []
    for path in ARCHIVE_DIR.glob("*/*.json"):
        value = load_json(path, None)
        if isinstance(value, dict) and value.get("message_id") and not value.get("superseded"):
            rows.append(value)
    rows.sort(key=lambda row: row.get("received_at") or "")
    return rows


def rebuild_company_indexes() -> None:
    if not ARCHIVE_DIR.exists():
        return
    grouped: dict[str, list[dict]] = {}
    for row in iter_archived_metadata():
        grouped.setdefault(row.get("company") or "待识别", []).append(row)
    now = datetime.now().astimezone().isoformat()
    for company_dir in (path for path in ARCHIVE_DIR.iterdir() if path.is_dir()):
        company = company_dir.name
        rows = sorted(grouped.get(company, []), key=lambda row: row.get("received_at") or "")
        write_json(company_dir / "company.json", {
            "company": company,
            "updated_at": now,
            "last_mail_at": rows[-1].get("received_at") if rows else "",
            "mail_count": len(rows),
            "active": bool(rows),
            "messages": [
                {
                    "message_id": row.get("message_id"),
                    "received_at": row.get("received_at"),
                    "event": row.get("event"),
                    "subject": row.get("subject"),
                    "archive_text": row.get("archive_text"),
                }
                for row in rows
            ],
        })


def iso_or_none(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.astimezone()
    except ValueError:
        return None


def refresh_urgency(meta: dict, now: datetime | None = None) -> dict:
    copy = dict(meta)
    urgency, rank, note = urgency_for(copy.get("event", ""), iso_or_none(copy.get("deadline", "")), now)
    copy.update(urgency=urgency, urgency_rank=rank, urgency_note=note)
    return copy
