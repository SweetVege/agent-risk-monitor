"""Read what the model said to the user before an action, from the Claude Code transcript.

The transcript is JSONL, one content block per line. It is written asynchronously, so the
tool call may not be in it yet when the hook fires; coming up empty is normal and callers
must accept an empty string. The model's reasoning is blank in the transcript, so this only
sees text it wrote for the user.
"""
from __future__ import annotations

import json
import os

TAIL_BYTES = 512 * 1024
MAX_SAID = 1200


def _entries(path: str):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in lines[1 if size > TAIL_BYTES else 0 :]:  # the first line is partial when we start mid-file
        try:
            yield json.loads(line)
        except ValueError:
            continue


def said_before(path: str, tool_use_id: str) -> str:
    """What the model said earlier in this turn, before this tool call. Empty if the call is not in the transcript yet."""
    if not path or not tool_use_id:
        return ""
    said: list[str] = []
    try:
        for entry in _entries(path):
            content = (entry.get("message") or {}).get("content")
            if entry.get("type") == "user":
                # a new user message starts a new turn; tool results are user entries too but do not count
                if isinstance(content, str) or not any(b.get("type") == "tool_result" for b in content or []):
                    said = []
                continue
            if entry.get("type") != "assistant" or not isinstance(content, list):
                continue
            for block in content:
                if block.get("type") == "text" and block.get("text", "").strip():
                    said.append(block["text"].strip())
                elif block.get("type") == "tool_use" and block.get("id") == tool_use_id:
                    return "\n".join(said)[-MAX_SAID:]
    except OSError:
        pass
    return ""
