---
name: job-application-tracker
description: Maintain the autumn recruitment tracker, recruitment-mail data pipeline, Feishu synchronization, and deployment workflow in this repository. Use for changes involving 看板程序, 业务数据, mail classification, tracker records, Feishu updates, or tracker deployment.
---

# Job Application Tracker

Treat the repository root as the project boundary. Read the nearest `AGENTS.md` before making changes.

## Work In The Repository

- Application source, tests, and deployment tools are in `看板程序/`; persistent recruitment records are in `业务数据/`.
- Run commands from `看板程序`. Use `.venv/Scripts/python.exe` on Windows. In the cloud Feishu bot, use `/workspace/.venvs/autumn-recruitment/bin/python`; elsewhere on Linux, use `.venv/bin/python` when available.
- Invoke tools as modules, such as `python -m tools.mail`, `python -m tools.feishu`, or `python -m tools.dashboard`; do not execute package files by path.
- Use `project_paths.py` instead of introducing machine-specific absolute paths.
- Treat Linux systemd files under `看板程序/deploy/` as examples until the target host has been confirmed.

## Preserve Data And Authorization Boundaries

- Preserve mail paths relative to `业务数据` and retain synchronization state and deduplication records across moves and deployments.
- Never print or commit `.env`, account credentials, access tokens, SSH keys, Codex authentication, or private deployment credentials.
- Treat QQ IMAP synchronization as read-only unless the current user explicitly authorizes a broader operation. Sending mail, creating drafts, mutating Feishu records, deploying services, or changing remote infrastructure requires explicit authorization immediately before the action.
- On cloud Linux, read the QQ authorization code only through `QQ_MAIL_AUTH_CODE_FILE`; keep that owner-only secret file outside the repository.
- Treat recruitment records as private business data. The source repository may be public, but
  `业务数据/`, `历史备份/`, `.env` and runtime state must remain untracked and private.

## Synchronize Safely

- Check `git status` before changing or synchronizing the repository.
- Pull only with fast-forward when the working tree is clean. Never discard, reset, or overwrite local or cloud changes to resolve divergence.
- Do not force-push. Keep shared rules in `AGENTS.md` and shared workflow skills under `.agents/skills/`; keep machine-specific Codex configuration outside the repository.

## Verify Changes

Run the offline suite from `看板程序`:

```text
python -m unittest discover -s tests -v
```

Use the platform-specific Python executable described above. Report skipped tests and any checks that require live mail, Feishu, or cloud access rather than silently exercising those services.
