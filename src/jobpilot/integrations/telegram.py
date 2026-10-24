"""Telegram Bot API client (free; outbound HTTPS only, long polling, no webhook or public address needed).

Security:
- The bot token lives only in settings (a ``SecretStr``) and in the request URL. Every error raised here is built
  from the HTTP status and Telegram's ``description`` field, never from ``requests`` exceptions or URLs (which
  contain the token), and ``redact()`` scrubs any text that might.
- Messages go only to the configured chat id. Inbound updates from any other chat are rejected by the bot
  service (``services/telegram_bot.py``).
- Plain text only (no parse mode), so text from job postings can't inject formatting or links.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import requests

from jobpilot.config import Settings

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
TIMEOUT = 20
MAX_TEXT = 4096  # Telegram's limit per message
_TOKEN_RE = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{30,}")


class TelegramError(RuntimeError):
    """``retryable``: a later attempt may succeed (network, 429, 5xx). ``retry_after``: seconds Telegram asked for."""

    def __init__(self, message: str, *, retryable: bool = False, retry_after: float | None = None,
                 status: int | None = None):
        super().__init__(redact(message))
        self.retryable, self.retry_after, self.status = retryable, retry_after, status


def redact(text: str, settings: Settings | None = None) -> str:
    """Remove anything that looks like a bot token (and the configured token itself) from ``text``."""
    text = str(text)
    if settings is not None and settings.telegram_bot_token is not None:
        tok = settings.telegram_bot_token.get_secret_value()
        if tok:
            text = text.replace(tok, "<token>")
    return _TOKEN_RE.sub("<token>", text)


def config_status(settings: Settings) -> dict[str, Any]:
    """What is configured, without revealing the token."""
    tok = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
    problems = []
    if not settings.telegram_enabled:
        problems.append("JOBPILOT_TELEGRAM_ENABLED is not true")
    if not tok:
        problems.append("JOBPILOT_TELEGRAM_BOT_TOKEN is not set")
    elif not re.fullmatch(r"\d{6,12}:[A-Za-z0-9_-]{30,}", tok):
        problems.append("JOBPILOT_TELEGRAM_BOT_TOKEN does not look like a bot token (expected <digits>:<secret>)")
    if not settings.telegram_chat_id:
        problems.append("JOBPILOT_TELEGRAM_CHAT_ID is not set — run `jobpilot telegram find-chat`")
    return {"enabled": settings.telegram_enabled, "token_set": bool(tok), "chat_id_set": bool(settings.telegram_chat_id),
            "commands_enabled": settings.telegram_commands_enabled, "ready": not problems, "problems": problems}


def is_ready(settings: Settings) -> bool:
    return config_status(settings)["ready"]


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    """Split on line boundaries into chunks Telegram accepts; very long lines are hard-split."""
    text = text.strip() or "(empty)"
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


class TelegramClient:
    def __init__(self, settings: Settings, http: requests.Session | None = None, max_retries: int = 2,
                 sleep=time.sleep):
        tok = settings.telegram_bot_token.get_secret_value() if settings.telegram_bot_token else ""
        if not tok:
            raise TelegramError("JOBPILOT_TELEGRAM_BOT_TOKEN is not set")
        self._token = tok
        self.settings = settings
        self.http = http or requests.Session()
        self.max_retries = max_retries
        self.sleep = sleep

    def __repr__(self) -> str:  # never show the token
        return "TelegramClient(token=<hidden>)"

    def call(self, method: str, payload: dict | None = None, timeout: float = TIMEOUT) -> Any:
        """POST a Bot API method; retries network errors, 429 (honouring retry_after) and 5xx."""
        attempt = 0
        while True:
            attempt += 1
            try:
                err = self._once(method, payload or {}, timeout)
            except TelegramError as exc:
                err = exc
            if isinstance(err, TelegramError):
                if not err.retryable or attempt > self.max_retries:
                    raise err
                wait = min(err.retry_after if err.retry_after is not None else 2 ** attempt, 60)
                log.warning("telegram call retrying", extra={"method": method, "attempt": attempt, "wait": wait})
                self.sleep(wait)
                continue
            return err

    def _once(self, method: str, payload: dict, timeout: float) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        try:
            r = self.http.post(url, json=payload, timeout=timeout)
        except requests.Timeout:
            return TelegramError(f"Telegram {method}: timed out", retryable=True)
        except requests.RequestException as exc:  # message would contain the URL: report only the type
            return TelegramError(f"Telegram {method}: network error ({type(exc).__name__})", retryable=True)
        try:
            body = r.json()
        except ValueError:
            body = {}
        if r.status_code == 200 and body.get("ok"):
            return body.get("result")
        desc = str(body.get("description") or f"HTTP {r.status_code}")[:300]
        if r.status_code == 429:
            retry_after = float((body.get("parameters") or {}).get("retry_after") or 5)
            return TelegramError(f"Telegram {method}: rate limited ({desc})", retryable=True, retry_after=retry_after,
                                 status=429)
        if r.status_code >= 500:
            return TelegramError(f"Telegram {method}: server error ({desc})", retryable=True, status=r.status_code)
        hint = ""
        if r.status_code == 401:
            hint = " — the bot token is wrong or was revoked (check JOBPILOT_TELEGRAM_BOT_TOKEN)"
        elif r.status_code == 403:
            hint = " — the bot can't message this chat (open the bot in Telegram and press Start)"
        elif r.status_code == 400 and "chat not found" in desc.lower():
            hint = " — check JOBPILOT_TELEGRAM_CHAT_ID, and press Start in the bot chat"
        elif r.status_code == 409:
            hint = " — another program is reading this bot's updates (only one may poll at a time)"
        return TelegramError(f"Telegram {method}: {desc}{hint}", status=r.status_code)

    def get_me(self) -> dict:
        return self.call("getMe")

    def send_text(self, text: str, chat_id: str | None = None, reply_markup: dict | None = None,
                  html: bool = False) -> list[int]:
        """Send ``text`` (split as needed) to the configured chat. Returns Telegram's message ids.

        ``reply_markup`` (buttons) is attached to the last chunk. ``html``: formatted (see ``telegram_ui.to_html``;
        all content is escaped); if Telegram rejects the formatting, that chunk is sent as plain text instead.
        """
        chat = chat_id or self.settings.telegram_chat_id
        if not chat:
            raise TelegramError("JOBPILOT_TELEGRAM_CHAT_ID is not set")
        ids = []
        chunks = split_text(text, limit=3500 if html else MAX_TEXT)  # room for the markup added by formatting
        for n, chunk in enumerate(chunks):
            payload = {"chat_id": chat, "text": chunk, "disable_web_page_preview": True}
            if reply_markup and n == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            if html:
                from jobpilot.services.telegram_ui import to_html

                try:
                    res = self.call("sendMessage", {**payload, "text": to_html(chunk, heading=n == 0),
                                                    "parse_mode": "HTML"})
                except TelegramError as exc:
                    if "parse" not in str(exc).lower() and "entit" not in str(exc).lower():
                        raise
                    res = self.call("sendMessage", payload)  # formatting refused: the same text, plain
            else:
                res = self.call("sendMessage", payload)
            if not isinstance(res, dict) or "message_id" not in res:
                raise TelegramError("Telegram sendMessage: no message id in the response")
            ids.append(res["message_id"])
        return ids

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        """Acknowledge a button press (stops the spinner in Telegram)."""
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    def get_updates(self, offset: int | None, poll_timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {"timeout": poll_timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=poll_timeout + 10) or []
