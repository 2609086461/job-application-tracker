"""飞书 API 封装 + 看板数据读取。"""
import os
import re
import time
from datetime import datetime
from collections import Counter
from urllib.parse import quote, unquote

import requests
from dotenv import load_dotenv

from app import bus  # noqa: E402
from company_aliases import canonical_company_name

ENV_PATH = os.path.abspath(os.environ.get("JOB_TRACKER_ENV_PATH", os.path.join(os.path.dirname(__file__), "..", ".env")))
CONFIG_KEYS = [
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "FEISHU_APP_TOKEN",
    "MAIN_TABLE_ID",
]
REQUIRED_CONFIG_KEYS = ["FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_APP_TOKEN", "MAIN_TABLE_ID"]

# JOB_TRACKER_ENV_PATH is the deployment's authoritative configuration.  Do
# not let stale variables inherited by a long-running service manager shadow
# credentials that were atomically refreshed on disk.
load_dotenv(ENV_PATH, override=True)

APP_ID = os.getenv("FEISHU_APP_ID")
APP_SECRET = os.getenv("FEISHU_APP_SECRET")
APP_TOKEN = os.getenv("FEISHU_APP_TOKEN")
MAIN_TABLE_ID = os.getenv("MAIN_TABLE_ID")
API = "https://open.feishu.cn/open-apis"

# 缓存 wiki 节点 token -> 多维表格 app_token 的映射，避免每次请求都解析
_APP_TOKEN_CACHE = {}
_TENANT_TOKEN_CACHE = {"value": "", "expires_at": 0.0}


# ── 飞书业务错误 ──────────────────────────────────────
class _FeishuError(RuntimeError):
    """飞书 API 业务错误，携带 code / msg，供 friendly_error() 精确诊断。"""
    def __init__(self, d: dict):
        self.code = d.get("code", 0)
        self.msg = d.get("msg", "")
        super().__init__(f"[{self.code}] {self.msg}")


def friendly_error(exc: Exception) -> str:
    """将飞书异常转成对用户友好的中文提示。

    所有地方（配置测试 / 看板数据读取）统一走这个函数，保证提示一致。
    """
    # ── 1. 飞书业务错误（优先按错误码精确提示）──────────
    if isinstance(exc, _FeishuError):
        code = exc.code
        msg = exc.msg
        # 多维表格
        if code in (99991400, 232010, 232011):
            return (
                f"应用没有「多维表格」权限："
                f"请在飞书开发者后台 → 权限管理中开通 bitable 相关权限。\n"
                f"详情：[{code}] {msg}")
        if code in (1254300, 1254003, 1254309, 1254310):
            return (
                f"应用没有该多维表格的访问权限："
                f"请把多维表格分享给应用（表格页面 → 添加协作者 → 搜索并添加你的飞书应用）。\n"
                f"详情：[{code}] {msg}")
        if code == 1254100:
            return f"多维表格不存在或已被删除。\n详情：[{code}] {msg}"
        if code == 1254002:
            return f"Table ID 不存在，或应用没有该子表的查看权限。\n详情：[{code}] {msg}"
        if code == 1254041:
            return f"Table ID「{MAIN_TABLE_ID}」在该多维表格中不存在。\n详情：[{code}] {msg}"
        # 知识库 / wiki
        if code == 99991403:
            return (
                f"知识库访问被拒：应用没有该 wiki 节点的查看权限。"
                f"请把应用加入知识库空间成员。\n"
                f"详情：[{code}] {msg}")
        if code == 91403:
            return (
                f"多维表格访问被拒：应用没有该多维表格或子表的查看权限。"
                f"请把应用添加到多维表格的协作者中。\n"
                f"详情：[{code}] {msg}")
        if code in (99991401, 230002):
            return f"wiki 节点不存在或 token 不正确。\n详情：[{code}] {msg}"
        if code in (230001, 99991408):
            return (
                f"应用没有「知识库」权限："
                f"请在飞书开发者后台 → 权限管理中开通 wiki 相关权限。\n"
                f"详情：[{code}] {msg}")
        # 鉴权
        if code == 99991661:
            return (
                f"飞书 App ID 或 App Secret 不正确："
                f"请检查飞书开发者后台 → 凭证与基础信息，复制正确的凭证。\n"
                f"详情：[{code}] {msg}")
        if code in (99991663, 99991667, 10003, 10012):
            return (
                f"飞书 App ID / App Secret 可能不正确，或应用已被禁用。"
                f"请检查飞书开发者后台。\n"
                f"详情：[{code}] {msg}")
        return f"飞书 API 错误 [{code}]：{msg}"

    # ── 2. 网络 / 代理问题 ──────────────────────────
    text = str(exc)
    if any(k in text for k in ("SSLError", "ConnectionError", "10054", "Max retries",
                                 "EOF", "timed out", "Timeout", "Connection refused")):
        return "连不上飞书 open.feishu.cn：请检查网络出口、代理或 IP 白名单。"

    # ── 3. HTTP 层面异常（raise_for_status 抛出的，没有飞书业务 code）───
    if isinstance(exc, requests.HTTPError):
        resp_text = getattr(exc.response, "text", "") or text
        try:
            body = exc.response.json() if exc.response is not None else {}
        except Exception:
            body = {}
        if isinstance(body, dict) and body.get("code"):
            # 里面有飞书业务 code，走一遍 _FeishuError
            return friendly_error(_FeishuError(body))
        return f"飞书 API 请求失败（HTTP {exc.response.status_code if exc.response is not None else '?'}）：{resp_text[:240]}"

    # ── 4. 兜底 ──────────────────────────────────
    return f"飞书连接失败：{text[:220]}"


# ── 配置管理 ────────────────────────────────────────
def parse_app_token(value: str) -> str:
    """从用户输入里提取 app_token / wiki 节点 token。

    支持：
    - 独立多维表格链接 https://xxx.feishu.cn/base/<token>?...
    - 知识库链接        https://xxx.feishu.cn/wiki/<token>?...
    - 直接粘贴的纯 token
    """
    value = (value or "").strip()
    if not value:
        return ""
    m = re.search(r"/(?:base|wiki)/([^/?#]+)", value)
    if m:
        return m.group(1)
    return value


def parse_table_id(value: str) -> str:
    """从用户输入里提取多维表格 table_id（tbl 开头）。

    支持从链接的 ?table=<id> 参数解析，或直接粘贴的纯 table_id。
    """
    value = (value or "").strip()
    if not value:
        return ""
    m = re.search(r"[?&]table=([^&#]+)", value)
    if m:
        return unquote(m.group(1))
    # 看起来是链接但没带 table 参数：无法解析出 table_id
    if "://" in value or "/" in value:
        return ""
    return value


def _apply_config(cfg: dict) -> None:
    global APP_ID, APP_SECRET, APP_TOKEN, MAIN_TABLE_ID
    APP_ID = cfg.get("FEISHU_APP_ID") or ""
    APP_SECRET = cfg.get("FEISHU_APP_SECRET") or ""
    APP_TOKEN = cfg.get("FEISHU_APP_TOKEN") or ""
    MAIN_TABLE_ID = cfg.get("MAIN_TABLE_ID") or ""
    _APP_TOKEN_CACHE.clear()


def get_config() -> dict:
    names = {
        "FEISHU_APP_ID": "APP_ID",
        "FEISHU_APP_SECRET": "APP_SECRET",
        "FEISHU_APP_TOKEN": "APP_TOKEN",
        "MAIN_TABLE_ID": "MAIN_TABLE_ID",
    }
    return {k: (globals().get(names[k]) if k in names else None) or os.getenv(k) or "" for k in CONFIG_KEYS}


def save_config(cfg: dict) -> None:
    """保存飞书配置到 .env，并立即更新当前进程内配置。"""
    merged = get_config()
    incoming = {k: (cfg.get(k) or "").strip() for k in CONFIG_KEYS}
    # 兜底：即使前端未解析，服务端也把 URL 归一化成纯 token / table_id。
    if incoming.get("FEISHU_APP_TOKEN"):
        incoming["FEISHU_APP_TOKEN"] = parse_app_token(incoming["FEISHU_APP_TOKEN"])
    raw_token = (cfg.get("FEISHU_APP_TOKEN") or "").strip()
    if not incoming.get("MAIN_TABLE_ID") and raw_token:
        incoming["MAIN_TABLE_ID"] = parse_table_id(raw_token)
    if incoming.get("MAIN_TABLE_ID"):
        incoming["MAIN_TABLE_ID"] = parse_table_id(incoming["MAIN_TABLE_ID"])
    merged.update(incoming)
    old_lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, "r", encoding="utf-8") as f:
            old_lines = f.read().splitlines()
    seen, out = set(), []
    for line in old_lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in CONFIG_KEYS:
            out.append(f"{key}={merged[key]}")
            seen.add(key)
        else:
            out.append(line)
    for key in CONFIG_KEYS:
        if key not in seen:
            out.append(f"{key}={merged[key]}")
    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out).strip() + "\n")
    os.environ.update(merged)
    _apply_config(merged)


def test_config(cfg: dict) -> bool:
    old = get_config()
    _apply_config({k: (cfg.get(k) or "").strip() for k in CONFIG_KEYS})
    try:
        cfg_now = get_config()
        missing = [k for k in REQUIRED_CONFIG_KEYS if not cfg_now.get(k)]
        if missing:
            raise RuntimeError("缺少配置：" + ", ".join(missing))
        _feishu(f"/bitable/v1/apps/{_bitable_app_token()}/tables/{MAIN_TABLE_ID}/records",
                payload={"page_size": 1})
        return True
    finally:
        _apply_config(old)


# ── 飞书 API 封装 ────────────────────────────────────
def _token():
    if _TENANT_TOKEN_CACHE["value"] and time.monotonic() < _TENANT_TOKEN_CACHE["expires_at"]:
        return _TENANT_TOKEN_CACHE["value"]
    r = requests.post(f"{API}/auth/v3/tenant_access_token/internal",
                      json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=20)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise _FeishuError(d)
    value = d["tenant_access_token"]
    # Feishu currently returns a two-hour token. Keep a five-minute safety
    # margin and avoid requesting a new token for every task lookup.
    expires_in = max(int(d.get("expire") or 7200) - 300, 60)
    _TENANT_TOKEN_CACHE.update(value=value, expires_at=time.monotonic() + expires_in)
    return value


def _feishu(path, method="GET", payload=None):
    h = {"Authorization": f"Bearer {_token()}"}
    method = method.upper()
    request_kwargs = {"headers": h, "timeout": 30}
    if method == "GET":
        request_kwargs["params"] = payload or {}
    elif payload is not None:
        request_kwargs["json"] = payload
    r = requests.request(method, f"{API}{path}", **request_kwargs)
    # 先解析飞书业务 code（比 HTTP 状态码更有诊断价值）
    d = {}
    try:
        d = r.json()
    except Exception:
        pass
    if d.get("code") != 0:
        raise _FeishuError(d)
    # 飞书没给业务 code 但 HTTP 状态异常 → 让 requests 抛出，由 friendly_error 兜底
    r.raise_for_status()
    return d.get("data", {})


def _bitable_app_token():
    """把配置里的 APP_TOKEN 解析成真正的多维表格 app_token。

    支持两种来源：
    - 独立多维表格链接 /base/xxx：xxx 本身就是 app_token，直接用。
    - 知识库链接 /wiki/xxx：xxx 是 wiki 节点 token，需要用 wiki 接口换成
      节点挂载的多维表格 obj_token 才能调 bitable API。
    """
    token = APP_TOKEN or ""
    if not token:
        return token
    if token in _APP_TOKEN_CACHE:
        return _APP_TOKEN_CACHE[token]
    # 尝试用 wiki 接口解析
    try:
        node = _feishu("/wiki/v2/spaces/get_node", payload={"token": token}).get("node") or {}
        if node.get("obj_type") == "bitable" and node.get("obj_token"):
            _APP_TOKEN_CACHE[token] = node["obj_token"]
            return node["obj_token"]
    except Exception:
        # wiki 解析失败 → 不是 wiki 节点 token → 回退：原样当普通 app_token 用
        pass
    _APP_TOKEN_CACHE[token] = token
    return token


# ── 数据接口 ────────────────────────────────────────
def list_records(table_id):
    recs, pt = [], None
    while True:
        p = {"page_size": 500}
        if pt:
            p["page_token"] = pt
        data = _feishu(f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/records",
                       payload=p)
        recs.extend(data.get("items") or [])
        if not data.get("has_more"):
            return recs
        pt = data.get("page_token")


def list_fields(table_id):
    data = _feishu(f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/fields",
                   payload={"page_size": 200})
    out = {}
    for item in data.get("items", []):
        name = item.get("field_name")
        if name:
            out[name] = item
    return out


def list_tables() -> list[dict]:
    """List all tables in the configured Bitable app."""
    data = _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables",
        payload={"page_size": 100},
    )
    return data.get("items") or []


def create_field(table_id: str, field: dict) -> dict:
    data = _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/fields",
        method="POST",
        payload=field,
    )
    return data.get("field") or data


def create_table(name: str, fields: list[dict], *, default_view_name: str = "表格") -> dict:
    """Create a Bitable subtable with an explicit initial schema."""
    return _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables",
        method="POST",
        payload={
            "table": {
                "name": name,
                "default_view_name": default_view_name,
                "fields": fields,
            }
        },
    )


def create_record(fields: dict, table_id=None):
    """在飞书主表新增一条记录。"""
    table_id = table_id or MAIN_TABLE_ID
    data = _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/records",
        method="POST",
        payload={"fields": fields},
    )
    return data.get("record") or data


def update_record(record_id: str, fields: dict, table_id=None):
    """更新一条记录；使用批量接口可避免依赖单条更新方法差异。"""
    table_id = table_id or MAIN_TABLE_ID
    data = _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/records/batch_update",
        method="POST",
        payload={"records": [{"record_id": record_id, "fields": fields}]},
    )
    records = data.get("records") or []
    return records[0] if records else data


def list_tasklists() -> list[dict]:
    data = _feishu("/task/v2/tasklists?page_size=100&user_id_type=union_id")
    return data.get("items") or []


def create_tasklist(payload: dict) -> dict:
    data = _feishu(
        "/task/v2/tasklists?user_id_type=union_id",
        method="POST",
        payload=payload,
    )
    return data.get("tasklist") or data


def create_task(payload: dict) -> dict:
    data = _feishu(
        "/task/v2/tasks?user_id_type=union_id",
        method="POST",
        payload=payload,
    )
    return data.get("task") or data


def get_task(task_guid: str) -> dict:
    data = _feishu(
        f"/task/v2/tasks/{quote(task_guid, safe='')}?user_id_type=union_id",
    )
    return data.get("task") or data


def update_task(task_guid: str, task: dict, update_fields: list[str]) -> dict:
    """更新飞书任务的指定字段。"""
    data = _feishu(
        f"/task/v2/tasks/{quote(task_guid, safe='')}?user_id_type=union_id",
        method="PATCH",
        payload={"task": task, "update_fields": update_fields},
    )
    return data.get("task") or data


def create_calendar_event(calendar_id: str, event: dict, *, idempotency_key: str = "") -> dict:
    """Create a calendar event with an optional idempotency key."""
    path = f"/calendar/v4/calendars/{quote(calendar_id, safe='')}/events"
    if idempotency_key:
        path += f"?idempotency_key={quote(idempotency_key, safe='')}"
    data = _feishu(path, method="POST", payload=event)
    return data.get("event") or data


def get_primary_calendar() -> dict:
    """Return the configured app's primary calendar."""
    data = _feishu(
        "/calendar/v4/calendars/primary?user_id_type=union_id",
        method="POST",
    )
    calendars = data.get("calendars") or []
    if not calendars:
        raise RuntimeError("飞书未返回应用主日历")
    return calendars[0].get("calendar") or calendars[0]


def add_calendar_event_attendees(
    calendar_id: str,
    event_id: str,
    attendees: list[dict],
    *,
    user_id_type: str = "union_id",
    need_notification: bool = False,
) -> dict:
    """Add user or chat attendees to an event created by the app."""
    path = (
        f"/calendar/v4/calendars/{quote(calendar_id, safe='')}/events/"
        f"{quote(event_id, safe='')}/attendees?user_id_type={quote(user_id_type, safe='')}"
    )
    return _feishu(
        path,
        method="POST",
        payload={"attendees": attendees, "need_notification": need_notification},
    )


def update_calendar_event(calendar_id: str, event_id: str, event: dict) -> dict:
    """Update an event previously created by the configured app."""
    data = _feishu(
        f"/calendar/v4/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        method="PATCH",
        payload=event,
    )
    return data.get("event") or data


def delete_calendar_event(calendar_id: str, event_id: str) -> dict:
    """删除当前应用创建的日程。"""
    return _feishu(
        f"/calendar/v4/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}",
        method="DELETE",
    )


def update_field(field_id: str, field: dict, table_id=None):
    """全量更新一个字段，调用方需保留原有 property。"""
    table_id = table_id or MAIN_TABLE_ID
    return _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/fields/{field_id}",
        method="PUT",
        payload=field,
    )


def delete_field(field_id: str, table_id=None):
    """删除一个非主字段。"""
    table_id = table_id or MAIN_TABLE_ID
    return _feishu(
        f"/bitable/v1/apps/{_bitable_app_token()}/tables/{table_id}/fields/{field_id}",
        method="DELETE",
    )


# ── 看板数据组装 ─────────────────────────────────────
def _unwrap_url(val):
    """飞书链接字段返回 {'link':'...','text':'...'}，这里提取纯 URL 字符串。"""
    if isinstance(val, dict):
        return val.get("link") or val.get("text") or str(val)
    return val


def get_main_stats():
    recs = list_records(MAIN_TABLE_ID)
    rows = [r["fields"] for r in recs if r.get("fields", {}).get("公司名称")]
    rows = [
        {**row, "公司名称": canonical_company_name(row.get("公司名称"))}
        for row in rows
    ]
    progress, directions = Counter(), Counter()
    exam_counter, interview_counter, offer_counter = Counter(), Counter(), Counter()
    for f in rows:
        progress_values = f.get("进展", []) or []
        for p in progress_values:
            progress[p] += 1
        has_exam = "机考" in progress_values
        has_interview = bool(f.get("一面") or f.get("二面") or f.get("三面")) or any(
            stage in progress_values for stage in ("一面", "二面", "三面", "hr面")
        )
        has_offer = "OC" in progress_values
        if has_exam:
            exam_counter["机考"] += 1
        if has_interview:
            interview_counter["面试中"] += 1
        if has_offer:
            offer_counter["Offer"] += 1
        for d in f.get("岗位方向", []) or []:
            directions[d] += 1
    recent = [r for r in rows if r.get("投递时间") and r.get("投递时间") != 0]
    recent.sort(key=lambda f: f.get("投递时间", 0) or 0, reverse=True)
    # 截止时间独立于投递状态：只要填了截止时间的都列出
    deadlines_raw = [r for r in rows if r.get("投递截止时间") and r.get("投递截止时间") != 0]
    deadlines_raw.sort(key=lambda f: f.get("投递截止时间", 0) or 0)
    deadlines = [
        {"company": f.get("公司名称", ""),
         "job": f.get("秋招岗位", ""),
         "deadline": f.get("投递截止时间", 0),
         "progress": f.get("进展", [])}
        for f in deadlines_raw
    ]
    return {
        "total_companies": len(rows),
        "exam_count": sum(exam_counter.values()),
        "interview_count": sum(interview_counter.values()),
        "offer_count": sum(offer_counter.values()),
        "directions": directions.most_common(15),
        "deadlines": deadlines,
        "recent": [
            {"company": f.get("公司名称", ""),
             "dir": f.get("岗位方向", []),
             "progress": f.get("进展", []),
             "job": f.get("秋招岗位", ""),
             "url": _unwrap_url(f.get("投递链接")),
             "deadline": f.get("投递截止时间", 0),
             "apply_date": f.get("投递时间", ""),
             "exam_date": f.get("机考时间", ""),
             "interview1": f.get("一面", ""),
             "interview2": f.get("二面", ""),
             "interview3": f.get("三面", ""),
             "warm": f.get("保温", ""),
             "result": f.get("结果", "")}
            for f in recent
        ]
    }


def get_dashboard_data():
    return {
        "main": get_main_stats(),
        "now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
