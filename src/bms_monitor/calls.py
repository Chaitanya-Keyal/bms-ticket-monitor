"""Phone call alerts through Twilio (Programmable Voice, REST API, no SDK)."""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

log = logging.getLogger(__name__)


class Caller:
    def __init__(self, account_sid: str, key_sid: str, key_secret: str, from_number: str, to_numbers: list[str]):
        if not all((account_sid, key_sid, key_secret, from_number)) or not to_numbers:
            raise ValueError("Twilio account SID, API key, caller and callee numbers are all required")
        self.url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls.json"
        self.auth = "Basic " + base64.b64encode(f"{key_sid}:{key_secret}".encode()).decode()
        self.from_number = from_number
        self.to_numbers = to_numbers

    def call(self, text: str = "") -> bool:
        """Ring every number. With text, read it twice; without, just ring and hang up when
        answered (the details are in Telegram). True if Twilio accepted at least one call."""
        if text:
            say = f'<Say language="en-IN">{escape(text)}</Say>'
            twiml = f'<Response><Pause length="1"/>{say}<Pause length="1"/>{say}</Response>'
        else:
            twiml = '<Response><Pause length="1"/><Hangup/></Response>'
        return any([self._call_one(to, twiml) for to in self.to_numbers])

    def _call_one(self, to: str, twiml: str) -> bool:
        body = urllib.parse.urlencode({"To": to, "From": self.from_number,
                                       "Twiml": twiml, "Timeout": "45"}).encode()
        req = urllib.request.Request(self.url, data=body, headers={"Authorization": self.auth})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                log.info("call to ...%s placed: %s", to[-4:], json.load(resp).get("status"))
                return True
        except urllib.error.HTTPError as e:
            log.error("twilio rejected the call to ...%s: %s %s", to[-4:], e.code, e.read()[:300])
        except Exception as e:
            log.error("twilio call failed: %s", e.__class__.__name__)
        return False
