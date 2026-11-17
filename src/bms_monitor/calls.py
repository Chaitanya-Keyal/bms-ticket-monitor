"""Phone call alerts through Twilio (Programmable Voice, REST API, no SDK)."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)


class Caller:
    def __init__(self, account_sid: str, key_sid: str, key_secret: str, from_number: str,
                 to_numbers: list[tuple[str, str]], twiml_url: str = ""):
        if not all((account_sid, key_sid, key_secret, from_number)) or not to_numbers:
            raise ValueError("Twilio account SID, API key, caller and callee numbers are all required")
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
        self.auth = "Basic " + base64.b64encode(f"{key_sid}:{key_secret}".encode()).decode()
        self.from_number = from_number
        self.to_numbers = to_numbers
        # Trial accounts may only point calls at Twilio's own template URLs (no inline TwiML,
        # no Timeout), e.g. https://webhooks.twilio.com/v1/Voice/Template/voice_speech_recognition
        self.twiml_url = twiml_url

    def call(self, text: str = "", only: list[str] | None = None) -> bool:
        """Ring every number. With text, read it twice; without, just ring and hang up when
        answered (the details are in Telegram). With twiml_url set, that URL decides what the call
        plays instead. `only` limits the call to those labels. True if Twilio accepted at least one."""
        targets = [t for t in self.to_numbers if only is None or t[0] in only]
        unknown = sorted(set(only or []) - {label for label, _ in self.to_numbers})
        if unknown:
            log.warning("not in CALL_TO, not called: %s", ", ".join(unknown))
        if not targets:
            return False
        if self.twiml_url:
            params = {"Url": self.twiml_url}
        else:
            if text:
                say = f'<Say language="en-IN">{escape(text)}</Say>'
                twiml = f'<Response><Pause length="1"/>{say}<Pause length="1"/>{say}</Response>'
            else:
                twiml = '<Response><Pause length="1"/><Hangup/></Response>'
            params = {"Twiml": twiml, "Timeout": "45"}
        # All numbers at once, so every phone rings at the same time.
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            return any(list(pool.map(lambda t: self._call_one(*t, params), targets)))

    def _call_one(self, label: str, to: str, params: dict) -> bool:
        body = urllib.parse.urlencode({"To": to, "From": self.from_number, **params}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                log.info("call to %s: placed (%s)", label, json.load(resp).get("status"))
                return True
        except urllib.error.HTTPError as e:
            log.error("call to %s: rejected by twilio: %s %s", label, e.code, e.read()[:300])
        except Exception as e:
            log.error("call to %s: failed (%s)", label, e.__class__.__name__)
        return False
