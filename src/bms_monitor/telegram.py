"""Send-only Telegram client (Bot API sendMessage)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger(__name__)
LIMIT = 4000  # Telegram allows 4096 characters per message


class Telegram:
    def __init__(self, token: str, chat_id: str, thread_id: str | None = None):
        if not token or not chat_id:
            raise ValueError("Telegram token and chat id are required")
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.thread_id = thread_id

    def send(self, html: str) -> bool:
        """Send an HTML message, split into chunks if needed. True if all parts were accepted."""
        return all(self._send_one(part) for part in split(html))

    def _send_one(self, html: str) -> bool:
        body = {"chat_id": self.chat_id, "text": html, "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True}}
        if self.thread_id:
            body["message_thread_id"] = int(self.thread_id)
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return bool(json.load(resp).get("ok"))
        except urllib.error.HTTPError as e:
            log.error("telegram rejected message: %s %s", e.code, e.read()[:300])
        except Exception as e:
            log.error("telegram send failed: %s", e.__class__.__name__)
        return False


def split(text: str, limit: int = LIMIT) -> list[str]:
    """Split on blank lines so no chunk exceeds the limit (and no HTML tag is cut)."""
    parts, current = [], ""
    for block in text.split("\n\n"):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
        while len(block) > limit:
            cut = block.rfind("\n", 0, limit)
            cut = cut if cut > 0 else limit
            parts.append(block[:cut])
            block = block[cut:].lstrip("\n")
        current = block
    if current:
        parts.append(current)
    return parts
