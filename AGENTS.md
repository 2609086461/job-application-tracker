# Project Rules

- Application source is in `看板程序/`; persistent user data is in `业务数据/`.
- Run Python tools from `看板程序` with `python -m ...`; do not execute package files by path.
- Prefer `.venv/Scripts/python.exe` on Windows and `.venv/bin/python` on Linux/macOS.
- Use `project_paths.py` for all data, backup, export and log paths.
- Never commit or print `.env`, mail authorization codes, Feishu/OpenAI credentials, SSH keys,
  Codex authentication, real mail, application records, interview materials or runtime state.
- Treat IMAP access as read-only. Sending mail or creating drafts requires separate user approval.
- Preview Feishu changes before applying them; do not infer authorization to mutate cloud data.
- Preserve mail UID state and deduplication records during upgrades and moves.
- Do not place real user data in fixtures. Tests must use temporary directories and fictional values.
- Run `python -m unittest discover -s tests -v` from `看板程序` after code changes.
- Updating source must not overwrite `业务数据/`, `历史备份/` or `看板程序/.env`.
