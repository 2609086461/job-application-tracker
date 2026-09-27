"""Check the latest public tracker release without installing it."""

from __future__ import annotations

import argparse
import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from project_paths import TRACKER_DIR


DEFAULT_REPOSITORY = "2609086461/job-application-tracker"


def version_parts(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.strip().lstrip("vV"))
    return tuple(int(item) for item in numbers) or (0,)


def current_version() -> str:
    path = TRACKER_DIR / "VERSION"
    return path.read_text(encoding="utf-8").strip() if path.is_file() else "0.0.0"


def github_json(url: str, timeout: float = 10):
    request = Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "job-tracker-update-check"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def latest_public_version(repository: str, timeout: float = 10) -> dict:
    """Return the newest stable release or tag.

    A tag is enough for the lightweight public update channel, while a GitHub
    Release can add a friendlier title and notes later.
    """
    candidates: list[dict[str, str]] = []
    errors: list[Exception] = []
    try:
        release = github_json(
            f"https://api.github.com/repos/{repository}/releases/latest",
            timeout,
        )
        tag = str(release.get("tag_name") or "").strip()
        if tag:
            candidates.append({
                "tag_name": tag,
                "name": str(release.get("name") or tag),
                "html_url": str(release.get("html_url") or ""),
            })
    except (HTTPError, URLError, OSError, ValueError) as exc:
        errors.append(exc)

    try:
        tags = github_json(
            f"https://api.github.com/repos/{repository}/tags?per_page=100",
            timeout,
        )
        for item in tags if isinstance(tags, list) else []:
            tag = str(item.get("name") or "").strip()
            if tag:
                candidates.append({
                    "tag_name": tag,
                    "name": tag,
                    "html_url": f"https://github.com/{repository}/tree/{tag}",
                })
    except (HTTPError, URLError, OSError, ValueError) as exc:
        errors.append(exc)

    if candidates:
        return max(candidates, key=lambda item: version_parts(item["tag_name"]))
    if errors:
        raise errors[0]
    raise ValueError("GitHub returned no releases or tags")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("JOB_TRACKER_UPDATE_REPOSITORY", DEFAULT_REPOSITORY))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    installed = current_version()
    try:
        release = latest_public_version(args.repository)
    except HTTPError as exc:
        message = "No public release is available yet." if exc.code == 404 else f"GitHub returned HTTP {exc.code}."
        payload = {"status": "unavailable", "current": installed, "message": message}
    except (URLError, OSError, ValueError) as exc:
        payload = {"status": "unavailable", "current": installed, "message": f"Update check failed: {exc}"}
    else:
        latest = str(release.get("tag_name") or "").strip()
        available = bool(latest) and version_parts(latest) > version_parts(installed)
        payload = {
            "status": "update_available" if available else "current",
            "current": installed,
            "latest": latest,
            "name": str(release.get("name") or latest),
            "url": str(release.get("html_url") or ""),
        }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif payload["status"] == "update_available":
        print(f"New version: {payload['latest']} (current: {payload['current']})")
        print(payload["url"])
        print("This command only checks. Ask the user before installing the update.")
    elif payload["status"] == "current":
        print(f"Already current: {payload['current']}")
    else:
        print(payload["message"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
