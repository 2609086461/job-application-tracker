#!/usr/bin/env python3
"""Transcribe a local audio/video attachment with Doubao recording ASR 2.0."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SUBMIT_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit"
QUERY_URL = "https://openspeech.bytedance.com/api/v3/auc/bigmodel/query"
DEFAULT_RESOURCE_ID = "volc.seedasr.auc"
PENDING_CODES = {"20000001", "20000002"}
SUCCESS_CODE = "20000000"


class TranscriptionError(RuntimeError):
    pass


def default_secret_candidates() -> list[Path]:
    candidates = [
        Path("/run/secrets/codex-speech/doubao-asr.env"),
        Path("/workspace/.secrets/doubao-asr.env"),
        Path.home() / ".config" / "codex-feishu-bot" / "doubao-asr.env",
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.insert(0, Path(local_app_data) / "CodexFeishuLocal" / "doubao-asr.env")
    return candidates


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config() -> dict[str, str]:
    configured = os.environ.get("DOUBAO_SPEECH_CREDENTIALS_FILE")
    if configured:
        secret_file = Path(configured).expanduser()
    else:
        secret_file = next((path for path in default_secret_candidates() if path.is_file()), Path(""))
    file_values = parse_env_file(secret_file) if secret_file.is_file() else {}

    def get(name: str, default: str = "") -> str:
        return os.environ.get(name) or file_values.get(name) or default

    config = {
        "app_id": get("DOUBAO_SPEECH_APP_ID"),
        "access_token": get("DOUBAO_SPEECH_ACCESS_TOKEN"),
        "resource_id": get("DOUBAO_SPEECH_RESOURCE_ID", DEFAULT_RESOURCE_ID),
    }
    missing = [name for name, value in config.items() if not value]
    if missing:
        raise TranscriptionError(
            "Missing Doubao configuration: " + ", ".join(missing) +
            ". Configure DOUBAO_SPEECH_CREDENTIALS_FILE; do not paste secrets into chat."
        )
    return config


def post_json(url: str, headers: dict[str, str], payload: dict, timeout: float):
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
            body = json.loads(raw.decode("utf-8")) if raw else {}
            return response.headers, body
    except HTTPError as exc:
        raise TranscriptionError(f"Doubao HTTP error: {exc.code}") from exc
    except URLError as exc:
        raise TranscriptionError(f"Doubao network error: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise TranscriptionError("Doubao returned invalid JSON") from exc


def api_status(headers) -> tuple[str, str, str]:
    return (
        headers.get("X-Api-Status-Code", ""),
        headers.get("X-Api-Message", ""),
        headers.get("X-Tt-Logid", ""),
    )


def format_srt_time(milliseconds: int | float) -> str:
    value = max(0, int(milliseconds))
    hours, remainder = divmod(value, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(result: dict) -> str:
    blocks: list[str] = []
    for index, item in enumerate(result.get("utterances") or [], 1):
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        start = format_srt_time(item.get("start_time") or 0)
        end = format_srt_time(item.get("end_time") or 0)
        blocks.append(f"{index}\n{start} --> {end}\n{text}")
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def output_dir_for(input_path: Path | None, explicit: str | None) -> Path:
    if explicit:
        output_dir = Path(explicit).expanduser().resolve()
    else:
        artifacts = Path(os.environ.get("CODEX_ARTIFACTS_DIR", "/workspace/artifacts"))
        stem = input_path.stem if input_path else "remote-audio"
        output_dir = artifacts / f"{stem}-{uuid.uuid4().hex[:8]}"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def transcribe(args: argparse.Namespace) -> dict[str, object]:
    config = load_config()
    input_path: Path | None = None
    if args.input:
        input_path = Path(args.input).expanduser().resolve()
        if not input_path.is_file():
            raise TranscriptionError(f"Input file does not exist: {input_path}")
        size = input_path.stat().st_size
        if size <= 0:
            raise TranscriptionError("Input file is empty")
        if size > args.max_mib * 1024 * 1024:
            raise TranscriptionError(
                f"Input is {size / 1024 / 1024:.1f} MiB; limit is {args.max_mib} MiB"
            )
        audio = {"data": base64.b64encode(input_path.read_bytes()).decode("ascii")}
    else:
        audio = {"url": args.url}

    task_id = str(uuid.uuid4())
    common_headers = {
        "X-Api-App-Key": config["app_id"],
        "X-Api-Access-Key": config["access_token"],
        "X-Api-Resource-Id": config["resource_id"],
        "X-Api-Request-Id": task_id,
        "Content-Type": "application/json",
    }
    request_body = {
        "user": {"uid": "feishu-codex"},
        "audio": audio,
        "request": {"model_name": "bigmodel", "enable_itn": True, "enable_punc": True, "enable_ddc": True},
    }
    submit_headers, _ = post_json(
        SUBMIT_URL,
        {**common_headers, "X-Api-Sequence": "-1"},
        request_body,
        args.http_timeout,
    )
    code, message, log_id = api_status(submit_headers)
    if code != SUCCESS_CODE:
        raise TranscriptionError(
            f"Doubao submit failed: status={code or 'missing'}, message={message or 'missing'}"
        )

    query_headers = dict(common_headers)
    if log_id:
        query_headers["X-Tt-Logid"] = log_id
    deadline = time.monotonic() + args.wait_timeout
    response_body: dict = {}
    while time.monotonic() < deadline:
        query_response_headers, response_body = post_json(
            QUERY_URL, query_headers, {}, args.http_timeout
        )
        code, message, _ = api_status(query_response_headers)
        if code == SUCCESS_CODE:
            break
        if code not in PENDING_CODES:
            raise TranscriptionError(
                f"Doubao query failed: status={code or 'missing'}, message={message or 'missing'}"
            )
        time.sleep(args.poll_interval)
    else:
        raise TranscriptionError(
            f"Doubao transcription timed out after {args.wait_timeout:.0f} seconds"
        )

    result = response_body.get("result") or {}
    transcript = str(result.get("text") or "").strip()
    if not transcript:
        raise TranscriptionError("Doubao completed the task but returned no speech text")

    output_dir = output_dir_for(input_path, args.output_dir)
    text_path = output_dir / "transcript.txt"
    srt_path = output_dir / "transcript.srt"
    json_path = output_dir / "result.json"
    text_path.write_text(transcript + "\n", encoding="utf-8")
    srt_path.write_text(build_srt(result), encoding="utf-8")
    json_path.write_text(json.dumps(response_body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "status": "ok",
        "characters": len(transcript),
        "output_dir": str(output_dir),
        "transcript": str(text_path),
        "subtitles": str(srt_path),
        "result": str(json_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", help="Local audio/video attachment path")
    source.add_argument("--url", help="Public or signed audio/video URL")
    parser.add_argument("--output-dir", help="Artifact output directory")
    parser.add_argument("--max-mib", type=int, default=64)
    parser.add_argument("--wait-timeout", type=float, default=600)
    parser.add_argument("--http-timeout", type=float, default=60)
    parser.add_argument("--poll-interval", type=float, default=2)
    return parser.parse_args()


def main() -> int:
    try:
        summary = transcribe(parse_args())
    except (OSError, TranscriptionError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
