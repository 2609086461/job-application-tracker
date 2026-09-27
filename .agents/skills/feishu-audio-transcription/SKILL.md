---
name: feishu-audio-transcription
description: Transcribe audio or video attachments received through a Feishu Codex bot, then return a transcript or use it as input for an interview review. Use when the user sends a recording or asks for transcription.
---

# Feishu Audio Transcription

Use the bundled deterministic client instead of installing a heavyweight local speech model.

## Workflow

1. Resolve the attachment path supplied by the Feishu bridge. Do not expose private inbound paths.
2. Run `scripts/transcribe.py` with the available Python interpreter and an output directory under the bot's artifacts directory.
3. Read `transcript.txt`. Read `result.json` only when timestamps or diagnostics are needed.
4. Return the requested transcript, or pass it to the interview-review workflow when the user asks for a复盘.
5. Publish any requested files through the Feishu bridge; workspace paths are not user-visible deliverables.

Example:

```text
python .agents/skills/feishu-audio-transcription/scripts/transcribe.py \
  --input <attachment> \
  --output-dir <artifacts>/<message-id>-transcription
```

## Configuration And Limits

- Set `DOUBAO_SPEECH_CREDENTIALS_FILE` to an owner-only file containing `DOUBAO_SPEECH_APP_ID` and `DOUBAO_SPEECH_ACCESS_TOKEN`. The client also checks the documented Windows and Docker secret locations.
- The default resource is `volc.seedasr.auc`.
- Local attachments are sent to the configured Doubao recording-file recognition service as base64. Tell the user this when privacy matters.
- Files above 64 MiB are refused by default. Ask the user to compress, split, or provide a signed URL.
- Stop after one failed retry cycle. Repeated submissions may be billable.
- Never print, attach, or commit speech credentials. Do not reuse a recording for unrelated work.
