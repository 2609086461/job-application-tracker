"""Batch recruitment-mail analysis through the locally authenticated Codex CLI."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from tools.mail.llm_analyzer import (
    INSTRUCTIONS,
    LLM_PREFILTER_HINTS,
    OUTPUT_SCHEMA,
    build_email_input,
    decision_to_metadata,
    normalize_deadline,
)
from tools.mail.mail_pipeline import analyze_message


DEFAULT_FAST_MODEL = "auto"
DEFAULT_REVIEW_MODEL = "auto"
DEFAULT_TIMEOUT_SECONDS = 300
DEFAULT_MAX_BATCH_ITEMS = 25

DECISION_SCHEMA = copy.deepcopy(OUTPUT_SCHEMA)
DECISION_SCHEMA["properties"] = {
    "mail_key": {"type": "string"},
    **DECISION_SCHEMA["properties"],
    "needs_deep_review": {"type": "boolean"},
}
DECISION_SCHEMA["required"] = [
    "mail_key",
    *DECISION_SCHEMA["required"],
    "needs_deep_review",
]
BATCH_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "decisions": {
            "type": "array",
            "items": DECISION_SCHEMA,
        },
    },
    "required": ["decisions"],
    "additionalProperties": False,
}

FAST_PROMPT = f"""{INSTRUCTIONS}
你将一次收到多封邮件。只分析标准输入中的 JSON，不读取文件、不运行命令。
必须为每个输入 mail_key 返回且只返回一项，mail_key 原样复制。
needs_deep_review 仅在证据冲突、阶段无法区分、公司主体不确定或截止时间含糊时设为 true。
普通通知和明确的单一阶段应设为 false。"""

REVIEW_PROMPT = f"""{INSTRUCTIONS}
这是快模型标记为不确定的少量邮件。只分析标准输入中的 JSON，不读取文件、不运行命令。
必须为每个输入 mail_key 返回且只返回一项，mail_key 原样复制。
请解决证据冲突并给出最终判断；needs_deep_review 固定返回 false。"""


class CodexAnalysisError(RuntimeError):
    pass


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise CodexAnalysisError(f"{name} 必须是正整数。") from exc
    if value <= 0:
        raise CodexAnalysisError(f"{name} 必须是正整数。")
    return value


def resolve_model(name: str, default: str) -> str | None:
    model = os.environ.get(name, default).strip() or default
    if model.lower() == "auto":
        return None
    if "gpt-6" in model.lower() or "astra" in model.lower():
        raise CodexAnalysisError(
            f"{name}={model} 不适合后台邮件分类；已硬性禁止 GPT-6/Astra。"
        )
    return model


def _codex_binary() -> str:
    configured = os.environ.get("MAIL_CODEX_BIN", "codex").strip() or "codex"
    resolved = shutil.which(configured)
    if not resolved:
        raise CodexAnalysisError("未找到 Codex CLI；请安装 codex 并使用 ChatGPT 登录。")
    return resolved


def _assert_chatgpt_login(codex_bin: str) -> None:
    completed = subprocess.run(
        [codex_bin, "login", "status"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
        env=_codex_environment(),
    )
    status = f"{completed.stdout}\n{completed.stderr}".lower()
    if completed.returncode or "chatgpt" not in status:
        raise CodexAnalysisError("Codex CLI 尚未使用 ChatGPT 账号登录，已停止以避免走付费 API。")


def _codex_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("OPENAI_API_KEY", None)
    environment.pop("CODEX_API_KEY", None)
    return environment


def _chunks(items: list[dict], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _run_codex_chunk(
    items: list[dict],
    *,
    model: str | None,
    effort: str,
    prompt: str,
    codex_bin: str,
    timeout: int,
) -> dict[str, dict]:
    expected_keys = [item["mail_key"] for item in items]
    with tempfile.TemporaryDirectory(prefix="job-tracker-codex-") as folder:
        temp_dir = Path(folder)
        schema_path = temp_dir / "schema.json"
        result_path = temp_dir / "result.json"
        schema_path.write_text(
            json.dumps(BATCH_OUTPUT_SCHEMA, ensure_ascii=False),
            encoding="utf-8",
        )
        command = [
                codex_bin,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--config",
                f'model_reasoning_effort="{effort}"',
                "--cd",
                str(temp_dir),
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(result_path),
                prompt,
            ]
        if model:
            command[command.index("--config"):command.index("--config")] = ["--model", model]
        model_label = model or "account-default"
        completed = subprocess.run(
            command,
            input=json.dumps({"emails": items}, ensure_ascii=False),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_codex_environment(),
        )
        if completed.returncode:
            raise CodexAnalysisError(
                f"Codex {model_label}/{effort} 批处理失败（退出码 {completed.returncode}）。"
            )
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexAnalysisError(f"Codex {model_label} 未返回有效的结构化结果。") from exc

    decisions = payload.get("decisions") if isinstance(payload, dict) else None
    if not isinstance(decisions, list):
        raise CodexAnalysisError(f"Codex {model_label} 的结果缺少 decisions 数组。")
    by_key = {
        item.get("mail_key"): item
        for item in decisions
        if isinstance(item, dict) and isinstance(item.get("mail_key"), str)
    }
    if len(by_key) != len(decisions) or set(by_key) != set(expected_keys):
        raise CodexAnalysisError(f"Codex {model_label} 返回的 mail_key 与输入批次不一致。")
    return by_key


def _run_codex_batches(
    items: list[dict],
    *,
    model: str | None,
    effort: str,
    prompt: str,
    codex_bin: str,
    timeout: int,
    max_batch_items: int,
) -> dict[str, dict]:
    output: dict[str, dict] = {}
    for chunk in _chunks(items, max_batch_items):
        output.update(
            _run_codex_chunk(
                chunk,
                model=model,
                effort=effort,
                prompt=prompt,
                codex_bin=codex_bin,
                timeout=timeout,
            )
        )
    return output


def _needs_review(decision: dict, email_input: dict) -> bool:
    if decision.get("needs_deep_review") or decision.get("confidence") != "high":
        return True
    if decision.get("is_recruitment") and decision.get("event") != "非招聘":
        if not str(decision.get("company") or "").strip():
            return True
    deadline = str(decision.get("deadline") or "")
    if deadline and not normalize_deadline(deadline):
        return True
    action_url = str(decision.get("action_url") or "")
    return bool(action_url and action_url not in email_input.get("available_urls", []))


def analyze_records_with_codex(records: list[dict]) -> dict[str, dict | None]:
    """Analyze new messages once in a fast batch and escalate only ambiguous rows."""
    output: dict[str, dict | None] = {}
    candidate_inputs: list[dict] = []
    records_by_key: dict[str, dict] = {}
    for record in records:
        mail_key = record["mail_key"]
        email_input = build_email_input(record["message"])
        sample = "\n".join(
            (email_input["sender"], email_input["subject"], email_input["body"])
        ).lower()
        if not any(hint.lower() in sample for hint in LLM_PREFILTER_HINTS):
            output[mail_key] = {
                "is_recruitment": False,
                "analysis_provider": "local_prefilter",
                "analysis_model": "",
                "analysis_prompt_version": "recruitment-mail-v1",
                "analysis_hash": hashlib.sha256(record["raw"]).hexdigest(),
                "analysis_confidence": "high",
                "analysis_evidence": [],
                "analysis_validation_issues": [],
            }
            continue
        candidate_inputs.append({"mail_key": mail_key, **email_input})
        records_by_key[mail_key] = record

    if not candidate_inputs:
        return output

    fast_model = resolve_model("MAIL_CODEX_FAST_MODEL", DEFAULT_FAST_MODEL)
    review_model = resolve_model("MAIL_CODEX_REVIEW_MODEL", DEFAULT_REVIEW_MODEL)
    timeout = _positive_int_env("MAIL_CODEX_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    max_batch_items = _positive_int_env("MAIL_CODEX_MAX_BATCH_ITEMS", DEFAULT_MAX_BATCH_ITEMS)
    codex_bin = _codex_binary()
    _assert_chatgpt_login(codex_bin)
    decisions = _run_codex_batches(
        candidate_inputs,
        model=fast_model,
        effort="low",
        prompt=FAST_PROMPT,
        codex_bin=codex_bin,
        timeout=timeout,
        max_batch_items=max_batch_items,
    )
    ambiguous_inputs = [
        item
        for item in candidate_inputs
        if _needs_review(decisions[item["mail_key"]], item)
    ]
    if ambiguous_inputs:
        decisions.update(
            _run_codex_batches(
                ambiguous_inputs,
                model=review_model,
                effort="medium",
                prompt=REVIEW_PROMPT,
                codex_bin=codex_bin,
                timeout=timeout,
                max_batch_items=max_batch_items,
            )
        )

    reviewed_keys = {item["mail_key"] for item in ambiguous_inputs}
    for mail_key, record in records_by_key.items():
        output[mail_key] = decision_to_metadata(
            record["message"],
            record["raw"],
            record["uid"],
            record["uidvalidity"],
            decisions[mail_key],
            provider="codex_cli",
            model=(review_model if mail_key in reviewed_keys else fast_model) or "account-default",
        )
    return output


def build_codex_batch_analyzer(
    log: Callable[[str], None] = print,
    *,
    fallback_to_rules: bool = False,
) -> Callable[[list[dict]], dict[str, dict | None]]:
    """Return a batch analyzer that defers mail unless fallback is explicit."""

    def analyzer(records: list[dict]) -> dict[str, dict | None]:
        try:
            return analyze_records_with_codex(records)
        except (CodexAnalysisError, subprocess.TimeoutExpired) as exc:
            if not fallback_to_rules:
                raise CodexAnalysisError(
                    f"Codex 批处理暂不可用；本批邮件保留到下次重试，UID 游标不会推进：{exc}"
                ) from exc
            log(f"Codex 批处理不可用，本批邮件按显式配置改用本地规则：{exc}")
            output = {}
            for record in records:
                meta = analyze_message(
                    record["message"],
                    record["raw"],
                    record["uid"],
                    record["uidvalidity"],
                )
                if meta is not None:
                    meta["analysis_provider"] = "rules_fallback"
                    meta["analysis_model"] = ""
                    meta["analysis_confidence"] = "low"
                output[record["mail_key"]] = meta
            return output

    return analyzer
