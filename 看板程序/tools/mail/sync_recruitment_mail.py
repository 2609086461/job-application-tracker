"""Incrementally sync recruitment emails from QQ Mail into per-company archives."""

from __future__ import annotations

import argparse
import email
import imaplib
import ssl
from datetime import date, datetime, timedelta
from typing import Callable

import keyring

from tools.mail.mail_pipeline import (
    PROCESSED_FILE,
    STATE_FILE,
    SYNC_DIR,
    analyze_message,
    archive_message,
    ensure_directories,
    load_json,
    rebuild_company_indexes,
    write_json,
)
from tools.mail.read_qq_mail import (
    CREDENTIAL_SERVICE,
    DEFAULT_ADDRESS,
    IMAP_HOST,
    IMAP_PORT,
    load_or_prompt_auth_code,
    save_auth_code,
)


DEFAULT_INITIAL_DATE = date(2026, 8, 12)


def with_rule_fallback(primary_analyzer, fallback_analyzer=analyze_message, log=print):
    """Disable the paid analyzer after its first runtime failure in a sync run."""
    primary_available = True

    def analyzer(message, raw, uid, uidvalidity):
        nonlocal primary_available
        if not primary_available:
            return fallback_analyzer(message, raw, uid, uidvalidity)
        try:
            return primary_analyzer(message, raw, uid, uidvalidity)
        except RuntimeError as exc:
            primary_available = False
            log(f"大模型分析不可用，本次后续邮件改用本地规则：{exc}")
            return fallback_analyzer(message, raw, uid, uidvalidity)

    return analyzer


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("日期格式必须为 YYYY-MM-DD") from exc


def read_uidvalidity(client: imaplib.IMAP4_SSL) -> str:
    status, values = client.response("UIDVALIDITY")
    if status == "UIDVALIDITY" and values and values[0]:
        raw = values[0]
        return raw.decode("ascii", errors="replace") if isinstance(raw, bytes) else str(raw)
    return "unknown"


def limit_uids(values: list[bytes], limit: int) -> list[bytes]:
    """Keep the earliest unprocessed UIDs so a partial run never skips a gap."""
    return values[:limit]


def search_uids_since(client: imaplib.IMAP4_SSL, since: date, limit: int) -> list[bytes]:
    status, data = client.uid("search", None, "SINCE", since.strftime("%d-%b-%Y"))
    if status != "OK":
        raise RuntimeError("无法搜索QQ邮箱收件箱。")
    return limit_uids((data[0] or b"").split(), limit)


def search_uids_after(client: imaplib.IMAP4_SSL, last_uid: int, limit: int) -> list[bytes]:
    """Return only UIDs appended after the last successful run."""
    status, data = client.uid("search", None, "UID", f"{last_uid + 1}:*")
    if status != "OK":
        raise RuntimeError("无法按邮件UID搜索QQ邮箱收件箱。")
    newer = [
        value for value in (data[0] or b"").split()
        if value.isdigit() and int(value) > last_uid
    ]
    return limit_uids(newer, limit)


def fetch_raw(client: imaplib.IMAP4_SSL, uid: bytes) -> bytes | None:
    status, payload = client.uid("fetch", uid, "(BODY.PEEK[])")
    if status != "OK" or not payload:
        return None
    return next(
        (item[1] for item in payload if isinstance(item, tuple) and isinstance(item[1], bytes)),
        None,
    )


def append_history(lines: list[str]) -> None:
    path = SYNC_DIR / "sync_history.txt"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    block = "\n".join(lines).rstrip() + "\n"
    path.write_text(existing + block, encoding="utf-8")


def sync_mailbox(
    address: str,
    auth_code: str,
    since: date,
    limit: int,
    reprocess: bool,
    recovery_days: int = 3,
    analyzer: Callable = analyze_message,
    batch_analyzer: Callable[[list[dict]], dict[str, dict | None]] | None = None,
) -> dict:
    ensure_directories()
    state = load_json(STATE_FILE, {})
    processed = load_json(PROCESSED_FILE, {"schema_version": 1, "items": {}})
    processed_items = processed.setdefault("items", {})
    known_message_ids = {
        item.get("message_id")
        for item in processed_items.values()
        if isinstance(item, dict) and item.get("message_id")
    }

    context = ssl.create_default_context()
    client = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, ssl_context=context)
    archived: list[dict] = []
    ignored = 0
    skipped = 0
    scanned = 0
    pending_records: list[dict] = []
    seen_message_ids = set(known_message_ids)
    previous_uid = int(state.get("last_uid") or 0)
    max_uid = previous_uid
    started_at = datetime.now().astimezone()
    try:
        client.login(address, auth_code)
        status, _ = client.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("无法以只读方式打开QQ邮箱收件箱。")
        uidvalidity = read_uidvalidity(client)
        same_mailbox = (
            not reprocess
            and previous_uid > 0
            and state.get("uidvalidity")
            and state.get("uidvalidity") == uidvalidity
        )
        if same_mailbox:
            scan_mode = "uid_incremental"
            scan_description = f"UID {previous_uid + 1}:*"
            uids = search_uids_after(client, previous_uid, limit)
        else:
            if state.get("last_successful_sync_at") and not reprocess:
                recovery_since = datetime.fromisoformat(state["last_successful_sync_at"]).date() - timedelta(days=recovery_days)
            else:
                recovery_since = since
            scan_mode = "recovery_window"
            scan_description = f"SINCE {recovery_since.isoformat()}"
            # A UIDVALIDITY change means old UID values are no longer comparable.
            # Message-ID deduplication below keeps this bounded recovery safe.
            max_uid = 0
            uids = search_uids_since(client, recovery_since, limit)
        for uid_raw in uids:
            uid = uid_raw.decode("ascii", errors="replace")
            mail_key = f"{uidvalidity}:{uid}"
            if not reprocess and mail_key in processed_items:
                skipped += 1
                if uid.isdigit():
                    max_uid = max(max_uid, int(uid))
                continue
            raw = fetch_raw(client, uid_raw)
            if not raw:
                raise RuntimeError(f"无法读取 UID {uid} 的邮件正文；同步游标未推进。")
            scanned += 1
            message = email.message_from_bytes(raw)
            message_id = (message.get("Message-ID") or "").strip().lower()
            if not reprocess and message_id and message_id in seen_message_ids:
                processed_items[mail_key] = {
                    "message_id": message_id,
                    "relevant": False,
                    "duplicate": True,
                    "processed_at": datetime.now().astimezone().isoformat(),
                }
                skipped += 1
                if uid.isdigit():
                    max_uid = max(max_uid, int(uid))
                continue
            pending_records.append({
                "mail_key": mail_key,
                "uid": uid,
                "uidvalidity": uidvalidity,
                "message_id": message_id,
                "message": message,
                "raw": raw,
            })
            if message_id:
                seen_message_ids.add(message_id)
            if uid.isdigit():
                max_uid = max(max_uid, int(uid))
    finally:
        try:
            client.logout()
        except imaplib.IMAP4.error:
            pass

    if batch_analyzer and pending_records:
        analyzed = batch_analyzer(pending_records)
        expected_keys = {record["mail_key"] for record in pending_records}
        if set(analyzed) != expected_keys:
            raise RuntimeError("批量分析结果与待处理邮件不一致；同步游标未推进。")
    else:
        analyzed = {
            record["mail_key"]: analyzer(
                record["message"],
                record["raw"],
                record["uid"],
                record["uidvalidity"],
            )
            for record in pending_records
        }

    for record in pending_records:
        mail_key = record["mail_key"]
        message_id = record["message_id"]
        meta = analyzed[mail_key]
        if meta and meta.get("is_recruitment", True):
            archived_meta = archive_message(meta, record["raw"])
            archived.append(archived_meta)
            known_message_ids.add(archived_meta["message_id"])
            processed_items[mail_key] = {
                "message_id": archived_meta["message_id"],
                "relevant": True,
                "company": archived_meta["company"],
                "event": archived_meta["event"],
                "archive_text": archived_meta["archive_text"],
                "analysis_provider": archived_meta.get("analysis_provider", "rules"),
                "analysis_model": archived_meta.get("analysis_model", ""),
                "analysis_hash": archived_meta.get("analysis_hash", ""),
                "processed_at": datetime.now().astimezone().isoformat(),
            }
        else:
            ignored += 1
            processed_items[mail_key] = {
                "message_id": message_id,
                "relevant": False,
                "analysis_provider": (meta or {}).get("analysis_provider", "rules"),
                "analysis_model": (meta or {}).get("analysis_model", ""),
                "analysis_hash": (meta or {}).get("analysis_hash", ""),
                "processed_at": datetime.now().astimezone().isoformat(),
            }

    finished_at = datetime.now().astimezone()
    if scanned or skipped:
        processed["updated_at"] = finished_at.isoformat()
        write_json(PROCESSED_FILE, processed)
    if archived:
        rebuild_company_indexes()
    new_state = {
        "schema_version": 1,
        "mailbox": address,
        "uidvalidity": uidvalidity,
        "initial_sync_date": state.get("initial_sync_date") or since.isoformat(),
        "last_scan_since": recovery_since.isoformat() if scan_mode == "recovery_window" else "",
        "last_uid": max_uid,
        "last_successful_sync_at": finished_at.isoformat(),
        "sync_mode": scan_mode,
        "last_run": {
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "scanned": scanned,
            "archived": len(archived),
            "ignored": ignored,
            "skipped": skipped,
            "archived_mail_keys": [item.get("mail_key") for item in archived],
        },
    }
    write_json(STATE_FILE, new_state)
    append_history([
        "=" * 72,
        f"同步开始：{started_at.isoformat()}",
        f"同步完成：{finished_at.isoformat()}",
        f"扫描范围：{scan_description}",
        f"扫描 {scanned} 封；新增招聘邮件 {len(archived)} 封；忽略 {ignored} 封；去重跳过 {skipped} 封",
    ])
    return new_state


def main() -> int:
    parser = argparse.ArgumentParser(description="增量同步QQ邮箱招聘邮件并按公司归档")
    parser.add_argument("--email", default=DEFAULT_ADDRESS)
    parser.add_argument("--since", type=parse_date, help="首次同步起点，格式 YYYY-MM-DD")
    parser.add_argument("--recovery-days", type=int, default=3, help="UID体系变化时的恢复扫描回看天数，默认3天")
    parser.add_argument("--limit", type=int, default=2000, help="单次最多扫描邮件数")
    parser.add_argument("--reprocess", action="store_true", help="忽略去重记录，重新处理扫描范围")
    analyzer_group = parser.add_mutually_exclusive_group()
    analyzer_group.add_argument("--llm", action="store_true", help="使用 OpenAI API 分析新增邮件（兼容模式）")
    analyzer_group.add_argument(
        "--codex",
        action="store_true",
        help="使用已登录 ChatGPT 的 Codex CLI 批量分析新增邮件，不读取 API Key",
    )
    parser.add_argument(
        "--codex-fallback-rules",
        action="store_true",
        help="Codex 不可用时仍按本地规则推进；默认保留 UID 到下次重试",
    )
    args = parser.parse_args()
    if args.limit <= 0 or args.recovery_days < 0:
        parser.error("--limit 必须大于0，--recovery-days 不能小于0")
    if args.codex_fallback_rules and not args.codex:
        parser.error("--codex-fallback-rules 必须与 --codex 一起使用")

    ensure_directories()
    state = load_json(STATE_FILE, {})
    since = args.since or date.fromisoformat(state.get("initial_sync_date") or DEFAULT_INITIAL_DATE.isoformat())

    print(f"邮箱：{args.email}")
    print(f"首次/恢复扫描起点：{since.isoformat()}（日常运行按UID增量读取；只读，不改变邮件已读状态）")
    try:
        auth_code, auth_source = load_or_prompt_auth_code(args.email)
    except RuntimeError as exc:
        print(f"读取授权码失败：{exc}")
        return 1
    if not auth_code:
        print("未输入授权码，已取消。")
        return 1
    try:
        analyzer = analyze_message
        batch_analyzer = None
        if args.llm:
            from tools.mail.llm_analyzer import LlmAnalysisError, build_llm_analyzer

            try:
                analyzer = with_rule_fallback(build_llm_analyzer())
            except LlmAnalysisError as exc:
                print(f"大模型分析未启用，本次改用本地规则：{exc}")
        elif args.codex:
            from tools.mail.codex_batch_analyzer import build_codex_batch_analyzer

            batch_analyzer = build_codex_batch_analyzer(
                fallback_to_rules=args.codex_fallback_rules
            )
        result = sync_mailbox(
            args.email,
            auth_code,
            since,
            args.limit,
            args.reprocess,
            args.recovery_days,
            analyzer,
            batch_analyzer,
        )
    except imaplib.IMAP4.error as exc:
        if auth_source == "keyring":
            try:
                keyring.delete_password(CREDENTIAL_SERVICE, args.email)
                print("已保存授权码失效，已删除。")
            except Exception:
                pass
        print(f"QQ邮箱登录或读取失败：{exc}")
        return 1
    except (OSError, RuntimeError) as exc:
        print(f"同步失败：{exc}")
        return 1

    if auth_source == "prompt":
        save_auth_code(args.email, auth_code)

    run = result["last_run"]
    print(
        f"同步完成：新增招聘邮件 {run['archived']} 封，"
        f"忽略 {run['ignored']} 封，去重跳过 {run['skipped']} 封。"
    )
    print(f"同步时间已记录：{result['last_successful_sync_at']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
