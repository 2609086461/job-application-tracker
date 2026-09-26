"""Build one cached, source-linked public profile for each active task company.

The job is deliberately separate from the dashboard request path.  It performs
public-web retrieval and one small Codex synthesis only when a company lacks a
cached profile; normal dashboard reads only load this local JSON cache.
"""

from __future__ import annotations

import argparse
from base64 import b64decode
import json
import os
import re
import tempfile
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlparse
from urllib.request import Request, urlopen

from app import mail_store
from company_aliases import canonical_company_name, company_key
from tools.feishu import sync_recruitment_tasks
from tools.mail.codex_batch_analyzer import (
    DEFAULT_FAST_MODEL,
    CodexAnalysisError,
    _assert_chatgpt_login,
    _codex_binary,
    _codex_environment,
    resolve_model,
)
from tools.mail.mail_pipeline import SYNC_DIR, ensure_directories


PROFILE_FILE = SYNC_DIR / "company_profiles.json"
DEFAULT_TIMEOUT_SECONDS = 120
MAX_SOURCE_TEXT = 6_000
PROFILE_SCHEMA = {
    "type": "object",
    "properties": {
        "industry": {"type": "string"},
        "business": {"type": "string"},
        "company_type": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["industry", "business", "company_type", "summary"],
    "additionalProperties": False,
}

PROFILE_PROMPT = """根据标准输入的单个公司公开网页资料，生成简短、克制的求职参考。
只使用输入材料中的事实；材料没有明确说明时填“未确认”，不要猜测规模、待遇、业务或岗位要求。
输出字段要求：industry（行业，最多30字）、business（主营业务，最多70字）、
company_type（公司类型/上市情况等，最多40字）、
summary（供看板直接展示的两句公司简介，最多150字）。不要提及招聘、测评、笔试、面试、岗位或匹配度。
只输出符合 schema 的 JSON。"""

_EVENT_CONTEXT = re.compile(r"招聘|测评|笔试|机考|面试|一面|二面|三面|岗位|匹配|事项")
_BAD_SOURCE_CONTEXT = re.compile(r"输入网页资料|所给网页|输入材料|未提供.{0,20}公开信息|未提供.{0,20}公司")
_INSUFFICIENT_SUMMARY = "公开资料不足，暂未生成可靠公司简介。"
_UNAVAILABLE_SUMMARY = "暂无简介"


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = values.get("content", "").strip()
        if tag.lower() == "a":
            css = values.get("class", "")
            href = values.get("href", "")
            if "result__a" in css and href:
                self.links.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        value = " ".join(data.split())
        if not value:
            return
        if self._in_title and not self.title:
            self.title = value
        if len(" ".join(self.text_parts)) < MAX_SOURCE_TEXT:
            self.text_parts.append(value)


def _now() -> datetime:
    return datetime.now().astimezone()


def _default_store() -> dict:
    return {"schema_version": 1, "profiles": {}}


def _load_store() -> dict:
    try:
        value = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_store()
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), dict):
        return _default_store()
    version = value.get("schema_version")
    return {"schema_version": version if isinstance(version, int) and version >= 1 else 1, "profiles": dict(value["profiles"])}


def _save_store(store: dict) -> None:
    ensure_directories()
    pending = PROFILE_FILE.with_suffix(".pending")
    pending.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.replace(PROFILE_FILE)


def _profile_key(company: str) -> str:
    return company_key(company)


def _active_companies(task_items: list[dict]) -> dict[str, str]:
    companies: dict[str, str] = {}
    for item in task_items:
        if not isinstance(item, dict) or item.get("status") == "已完成":
            continue
        company = canonical_company_name(str(item.get("company") or ""))
        key = _profile_key(company)
        if key:
            companies.setdefault(key, company)
    return companies


def profiles_for_companies(companies: list[str]) -> dict[str, dict]:
    """Return public-safe profiles for the companies rendered in the dashboard."""
    profiles = _load_store()["profiles"]
    output: dict[str, dict] = {}
    keys = {_profile_key(canonical_company_name(company)) for company in companies}
    for key in keys:
        if not key:
            continue
        profile = profiles.get(key)
        if not isinstance(profile, dict):
            continue
        if profile.get("status") == "ready":
            output[key] = {
                field: str(profile.get(field) or "")
                for field in ("company", "industry", "business", "company_type", "summary", "source_url", "source_title", "updated_at")
            }
            output[key]["status"] = "ready"
        elif profile.get("status") == "error":
            output[key] = {"company": str(profile.get("company") or ""), "status": "unavailable", "summary": _UNAVAILABLE_SUMMARY}
    return output


def profiles_for_tasks(task_items: list[dict]) -> dict[str, dict]:
    """Backward-compatible helper for task-only callers."""
    return profiles_for_companies(list(_active_companies(task_items).values()))


def _download(url: str) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; JobTrackerProfile/1.0)"})
    with urlopen(request, timeout=15) as response:  # noqa: S310 -- fixed public-web allowlist protocol below
        final_url = response.geturl()
        if urlparse(final_url).scheme not in {"http", "https"}:
            raise ValueError("公开网页返回了不支持的协议")
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            raise ValueError("公开网页不是 HTML")
        charset = response.headers.get_content_charset() or "utf-8"
        return final_url, response.read(350_000).decode(charset, errors="replace")


def _search_urls(company: str) -> list[str]:
    urls: list[str] = []
    try:
        _, page = _download("https://html.duckduckgo.com/html/?q=" + quote_plus(f"{company} 官网 公司简介"))
        parser = _PageParser()
        parser.feed(page)
        for href in parser.links:
            parsed = urlparse(href)
            target = parse_qs(parsed.query).get("uddg", [""])[0] if parsed.netloc.endswith("duckduckgo.com") else href
            target = unquote(target)
            if urlparse(target).scheme not in {"http", "https"} or target in urls:
                continue
            urls.append(target)
    except OSError:
        pass
    if not urls:
        urls = _bing_search_urls(company)
    return sorted(urls, key=_source_priority)[:3]


def _source_priority(url: str) -> tuple[int, str]:
    """Prefer stable encyclopedic company entries before corporate sites."""
    host = urlparse(url).netloc.lower()
    if host.endswith("baike.baidu.com"):
        return (0, url)
    if host.endswith("wikipedia.org"):
        return (1, url)
    return (2, url)


def _bing_result_url(href: str) -> str:
    """Decode Bing's public result redirect, retaining only an ordinary web URL."""
    href = unescape(href)
    parsed = urlparse(href)
    if not parsed.netloc.endswith("bing.com"):
        return href
    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded.startswith("a1"):
        return ""
    try:
        return b64decode(encoded[2:] + "=" * (-len(encoded[2:]) % 4)).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return ""


def _bing_search_urls(company: str) -> list[str]:
    """Fallback when DuckDuckGo presents an anti-bot challenge to the server."""
    _, page = _download("https://www.bing.com/search?q=" + quote_plus(f"{company} 官网 公司简介"))
    urls: list[str] = []
    for href in re.findall(r'<h2\b[^>]*>\s*<a\b[^>]*href="([^"]+)"', page, flags=re.IGNORECASE):
        target = _bing_result_url(href)
        if urlparse(target).scheme not in {"http", "https"} or target in urls:
            continue
        urls.append(target)
    return urls[:3]


def fetch_public_source(company: str) -> dict:
    """Fetch a short text excerpt from a public search result, without credentials."""
    failures: list[str] = []
    for url in _search_urls(company):
        try:
            final_url, page = _download(url)
            parser = _PageParser()
            parser.feed(page)
            text = " ".join(parser.text_parts)
            if len(text) < 120:
                raise ValueError("网页正文不足")
            return {
                "url": final_url,
                "title": parser.title[:160],
                "description": parser.description[:500],
                "text": text[:MAX_SOURCE_TEXT],
            }
        except (OSError, ValueError) as exc:
            failures.append(str(exc))
    # Corporate sites and encyclopedia pages sometimes reject server-side reads.
    # The public Bing result page remains a factual, source-linked fallback.
    try:
        search_url = "https://www.bing.com/search?q=" + quote_plus(f"{company} 公司简介")
        final_url, page = _download(search_url)
        parser = _PageParser()
        parser.feed(page)
        text = " ".join(parser.text_parts)
        if len(text) >= 120:
            return {
                "url": final_url,
                "title": f"{company} 公开搜索结果",
                "description": parser.description[:500],
                "text": text[:MAX_SOURCE_TEXT],
            }
    except (OSError, ValueError) as exc:
        failures.append(str(exc))
    detail = failures[-1] if failures else "搜索没有返回可读取的公开网页"
    raise RuntimeError(f"未找到可用公开资料：{detail}")


def _clip(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _company_only_summary(summary: object) -> str:
    """Drop historic recruitment-context sentences from a cached company profile."""
    sentences = re.split(r"(?<=[。！？])", _clip(summary, 300))
    kept = [sentence.strip() for sentence in sentences if sentence.strip()
            and not _EVENT_CONTEXT.search(sentence) and not _BAD_SOURCE_CONTEXT.search(sentence)]
    return _clip("".join(kept), 150) or _INSUFFICIENT_SUMMARY


def clean_existing_profiles() -> dict:
    """One-time, lossless-in-scope migration to company-only profile records."""
    store = _load_store()
    changed = 0
    for profile in store["profiles"].values():
        if not isinstance(profile, dict):
            continue
        before = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        profile.pop("job_fit", None)
        if profile.get("status") == "ready":
            profile["summary"] = _company_only_summary(profile.get("summary"))
        after = json.dumps(profile, ensure_ascii=False, sort_keys=True)
        if before != after:
            changed += 1
    if changed:
        store["schema_version"] = 2
        _save_store(store)
    return {"profiles": len(store["profiles"]), "updated": changed}


def summarize_public_source(company: str, source: dict) -> dict:
    model = resolve_model("COMPANY_PROFILE_MODEL", DEFAULT_FAST_MODEL)
    codex_bin = _codex_binary()
    _assert_chatgpt_login(codex_bin)
    with tempfile.TemporaryDirectory(prefix="job-tracker-profile-") as folder:
        root = Path(folder)
        schema_path = root / "schema.json"
        result_path = root / "result.json"
        schema_path.write_text(json.dumps(PROFILE_SCHEMA, ensure_ascii=False), encoding="utf-8")
        import subprocess

        completed = subprocess.run(
            [
                codex_bin, "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--sandbox", "read-only", "--model", model,
                "--config", 'model_reasoning_effort="low"', "--cd", str(root),
                "--output-schema", str(schema_path), "--output-last-message", str(result_path), PROFILE_PROMPT,
            ],
            input=json.dumps({"company": company, "source": source}, ensure_ascii=False),
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SECONDS,
            env=_codex_environment(),
            check=False,
        )
        if completed.returncode:
            raise CodexAnalysisError(f"Codex 公司简介生成失败（退出码 {completed.returncode}）。")
        try:
            value = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAnalysisError("Codex 未返回有效的公司简介 JSON。") from exc
    if not isinstance(value, dict):
        raise CodexAnalysisError("Codex 公司简介格式无效。")
    return {
        "industry": _clip(value.get("industry"), 30) or "未确认",
        "business": _clip(value.get("business"), 70) or "未确认",
        "company_type": _clip(value.get("company_type"), 40) or "未确认",
        "summary": _company_only_summary(value.get("summary")),
    }


def process_companies(companies: list[str], *, limit: int, retry_errors: bool = False) -> dict:
    """Generate selected profiles; error entries retry only when explicitly requested."""
    if limit <= 0:
        raise ValueError("limit 必须大于 0")
    selected = {_profile_key(canonical_company_name(company)): canonical_company_name(company) for company in companies}
    selected = {key: company for key, company in selected.items() if key}
    store = _load_store()
    profiles = store["profiles"]
    now = _now()
    attempted = ready = failed = 0
    for key, company in selected.items():
        existing = profiles.get(key) if isinstance(profiles.get(key), dict) else {}
        if existing.get("status") == "ready":
            continue
        if existing.get("status") == "error" and not retry_errors:
            continue
        attempted += 1
        try:
            source = fetch_public_source(company)
            profile = summarize_public_source(company, source)
            profiles[key] = {
                "status": "ready", "company": company, **profile,
                "source_url": source["url"], "source_title": source.get("title", ""),
                "updated_at": now.isoformat(),
            }
            ready += 1
        except (OSError, RuntimeError, ValueError, CodexAnalysisError) as exc:
            profiles[key] = {
                "status": "error", "company": company, "error": _clip(exc, 240),
                "updated_at": now.isoformat(),
            }
            failed += 1
        if attempted >= limit:
            break
    if attempted:
        _save_store(store)
    return {"companies": len(selected), "attempted": attempted, "ready": ready, "failed": failed}


def process_pending(*, limit: int = 1) -> dict:
    """Generate profiles for unhandled mail to-dos and active tasks, once each."""
    tasks = sync_recruitment_tasks.dashboard_data().get("items") or []
    active = _active_companies(tasks)
    mail = mail_store.get_dashboard_data(
        handled_references=mail_store.completed_mail_references(tasks),
        task_records=mail_store.mail_task_records(tasks),
    )
    mail_companies = [str(item.get("company") or "") for item in (mail.get("items") or []) if isinstance(item, dict)]
    result = process_companies([*active.values(), *mail_companies], limit=limit)
    return {"active": len(active), **{key: value for key, value in result.items() if key != "companies"}}


def main() -> int:
    parser = argparse.ArgumentParser(description="异步补全求职待办公司的公开简介缓存")
    parser.add_argument("--limit", type=int, default=1, help="本次最多生成的公司数（默认 1）")
    parser.add_argument("--clean-cache", action="store_true", help="移除历史招聘事项关联，保留公司简介")
    parser.add_argument("--company", action="append", default=[], help="明确指定一家公司重试；可重复传入")
    args = parser.parse_args()
    try:
        result = clean_existing_profiles() if args.clean_cache else (
            process_companies(args.company, limit=args.limit, retry_errors=True)
            if args.company else process_pending(limit=args.limit)
        )
    except (OSError, RuntimeError, ValueError, CodexAnalysisError) as exc:
        print(f"公司简介同步失败：{exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
