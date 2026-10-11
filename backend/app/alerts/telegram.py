"""
Telegram alerts and remote commands over the raw Bot API (httpx; no bot framework).

    AlertService ──notify(text)──▶ TelegramBot.send_message ──POST /bot<token>/sendMessage──▶ your chat
    your chat ──"/pause OptionsEvent"──▶ getUpdates (long polling) ──▶ handle_update ──▶ injected handler
                                                                                    └──▶ reply

Commands (the handlers are injected by app/main.py, so this module knows nothing about agents):
    /status             one-paragraph desk status
    /pause <agent>      stop one agent buying          /resume <agent>   let it buy again
    /brief              today's brief now               /help             this list

Rules:
  * No `TELEGRAM_BOT_TOKEN` → the bot is disabled and every call is a silent no-op
    (alerts still appear on the dashboard).
  * Commands are executed ONLY for the chat in `TELEGRAM_CHAT_ID`. Messages from any other
    chat are ignored. With no chat id configured, the bot answers any message with the
    sender's chat id and how to configure it, and executes nothing.
  * The token is part of every request URL, so errors are logged by type/status only,
    never with the URL.
  * Messages are plain text (no Markdown parsing) and cut at Telegram's 4096-character limit.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings
from app.constants import AGENT_NAMES

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
SEND_TIMEOUT_SECONDS = 10.0
LONG_POLL_SECONDS = 25                         # Telegram holds getUpdates open this long
POLL_HTTP_MARGIN_SECONDS = 10.0                # HTTP timeout = long poll + this margin
POLL_ERROR_BACKOFF_SECONDS = 5.0
MAX_MESSAGE_CHARS = 4096
TRUNCATION_MARK = "…"

COMMAND_STATUS = "/status"
COMMAND_PAUSE = "/pause"
COMMAND_RESUME = "/resume"
COMMAND_BRIEF = "/brief"
COMMAND_HELP = "/help"
COMMAND_START = "/start"

HELP_TEXT = (
    "PocketDesk commands:\n"
    "/status — desk status\n"
    "/pause <agent> — stop one agent buying\n"
    "/resume <agent> — let it buy again\n"
    "/brief — today's brief now\n"
    f"Agents: {', '.join(AGENT_NAMES)}"
)

TextResult = str | Awaitable[str]


class TelegramError(Exception):
    """A Bot API call failed (network, HTTP status or `ok: false`). Never contains the token."""


@dataclass(frozen=True)
class CommandHandlers:
    """What each command does; each returns (or awaits to) the reply text."""

    status: Callable[[], TextResult]
    pause: Callable[[str], TextResult]
    resume: Callable[[str], TextResult]
    brief: Callable[[], TextResult]


@dataclass(frozen=True)
class ParsedCommand:
    """"/pause@PocketDeskBot OptionsEvent" → name "/pause", argument "OptionsEvent"."""

    name: str
    argument: str


def parse_command(text: str) -> ParsedCommand | None:
    """Split a message into command and argument; None when the text is not a command."""
    stripped = text.strip()
    if not stripped.startswith("/"):
        return None
    head, _, rest = stripped.partition(" ")
    name = head.split("@", 1)[0].lower()
    return ParsedCommand(name=name, argument=rest.strip())


def resolve_agent(argument: str) -> str | None:
    """Case-insensitive agent name lookup ("optionsevent" → "OptionsEvent")."""
    by_lower_name = {name.lower(): name for name in AGENT_NAMES}
    return by_lower_name.get(argument.strip().lower())


def truncate_message(text: str) -> str:
    """Cut text to Telegram's 4096-character limit, marking the cut."""
    if len(text) <= MAX_MESSAGE_CHARS:
        return text
    return text[: MAX_MESSAGE_CHARS - len(TRUNCATION_MARK)] + TRUNCATION_MARK


async def _resolve_text(result: TextResult) -> str:
    """Await a handler result when it is awaitable."""
    if inspect.isawaitable(result):
        return await result
    return result


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop running in this thread, or None."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class TelegramBot:
    """Sends alerts to one chat and answers that chat's commands."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        handlers: CommandHandlers | None = None,
        http: httpx.AsyncClient | None = None,
        api_base: str = TELEGRAM_API_BASE,
    ):
        self._token = token
        self._chat_id = str(chat_id).strip()
        self._handlers = handlers
        self._http = http                       # injected (tests); otherwise one client per call
        self._api_base = api_base.rstrip("/")
        self._offset: int | None = None         # next getUpdates offset (last update_id + 1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._background: set[asyncio.Future[Any]] = set()

    @classmethod
    def from_settings(cls, settings: Settings | None = None, handlers: CommandHandlers | None = None) -> TelegramBot:
        """Build from TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID."""
        settings = settings or get_settings()
        return cls(settings.telegram_bot_token, settings.telegram_chat_id, handlers)

    # ─── state ────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """True when a bot token is configured."""
        return bool(self._token)

    @property
    def can_send(self) -> bool:
        """True when both the token and the destination chat id are configured."""
        return self.enabled and bool(self._chat_id)

    def set_handlers(self, handlers: CommandHandlers) -> None:
        """Attach the command handlers (main.py does this once the agents exist)."""
        self._handlers = handlers

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the app's event loop so `notify` works from scheduler threads."""
        self._loop = loop

    # ─── Bot API ──────────────────────────────────────────────────────────
    async def _post(self, method: str, payload: dict[str, Any], timeout: float) -> Any:
        """POST one Bot API method and return its `result`; raises TelegramError (token-free)."""
        url = f"{self._api_base}/bot{self._token}/{method}"
        try:
            if self._http is not None:
                response = await self._http.post(url, json=payload, timeout=timeout)
            else:
                async with httpx.AsyncClient() as http:
                    response = await http.post(url, json=payload, timeout=timeout)
        except httpx.HTTPError as error:
            raise TelegramError(f"{method}: {type(error).__name__}") from None
        try:
            body = response.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {response.status_code}, body is not JSON") from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description", "") if isinstance(body, dict) else ""
            raise TelegramError(f"{method}: HTTP {response.status_code} {description}".strip())
        return body.get("result")

    async def send_message(self, text: str, chat_id: str | None = None) -> bool:
        """Send plain text to `chat_id` (default: the configured chat). Returns True when delivered."""
        destination = chat_id or self._chat_id
        if not self.enabled or not destination:
            return False
        payload = {"chat_id": destination, "text": truncate_message(text), "disable_web_page_preview": True}
        try:
            await self._post("sendMessage", payload, SEND_TIMEOUT_SECONDS)
        except TelegramError as error:
            logger.warning("telegram send failed: %s", error)
            return False
        return True

    def notify(self, text: str) -> None:
        """
        Fire-and-forget send, safe from any thread: on the running loop → background task;
        from a scheduler thread → the bound app loop; with no loop at all → run it to completion.
        """
        if not self.can_send:
            return
        running = _running_loop()
        if running is not None:
            task = running.create_task(self.send_message(text))
            self._background.add(task)
            task.add_done_callback(self._background.discard)
        elif self._loop is not None and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.send_message(text), self._loop)
        else:
            asyncio.run(self.send_message(text))

    async def wait_for_background(self) -> None:
        """Wait until every `notify` task started on this loop has finished (tests, shutdown)."""
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    async def get_updates(self, long_poll_seconds: int = LONG_POLL_SECONDS) -> list[dict[str, Any]]:
        """New updates since the last call (long polling); advances the offset."""
        payload: dict[str, Any] = {"timeout": long_poll_seconds, "allowed_updates": ["message"]}
        if self._offset is not None:
            payload["offset"] = self._offset
        updates = await self._post("getUpdates", payload, long_poll_seconds + POLL_HTTP_MARGIN_SECONDS)
        for update in updates or []:
            self._offset = max(self._offset or 0, int(update["update_id"]) + 1)
        return list(updates or [])

    # ─── commands ─────────────────────────────────────────────────────────
    async def _run_command(self, command: ParsedCommand) -> str:
        """Execute one authorised command and return the reply text."""
        if command.name in (COMMAND_HELP, COMMAND_START) or self._handlers is None:
            return HELP_TEXT
        if command.name == COMMAND_STATUS:
            return await _resolve_text(self._handlers.status())
        if command.name == COMMAND_BRIEF:
            return await _resolve_text(self._handlers.brief())
        if command.name in (COMMAND_PAUSE, COMMAND_RESUME):
            agent = resolve_agent(command.argument)
            if agent is None:
                return f"Unknown agent {command.argument!r}. Agents: {', '.join(AGENT_NAMES)}"
            handler = self._handlers.pause if command.name == COMMAND_PAUSE else self._handlers.resume
            return await _resolve_text(handler(agent))
        return f"Unknown command {command.name}.\n\n{HELP_TEXT}"

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        """
        React to one update. Returns the reply that was sent, or None when the update was
        ignored (no text, not a command, or a chat other than TELEGRAM_CHAT_ID).
        """
        message = update.get("message") or {}
        sender_chat = str((message.get("chat") or {}).get("id", ""))
        text = message.get("text") or ""
        if not sender_chat or not text:
            return None
        if not self._chat_id:
            reply = f"PocketDesk: set TELEGRAM_CHAT_ID={sender_chat} in .env and restart to enable alerts here."
            await self.send_message(reply, chat_id=sender_chat)
            return reply
        if sender_chat != self._chat_id:
            return None
        command = parse_command(text)
        if command is None:
            return None
        try:
            reply = await self._run_command(command)
        except Exception as error:  # noqa: BLE001 — a failing handler must not stop the poll loop
            logger.warning("telegram command %s failed: %s", command.name, type(error).__name__)
            reply = f"{command.name} failed ({type(error).__name__})."
        await self.send_message(reply)
        return reply

    async def poll_once(self, long_poll_seconds: int = LONG_POLL_SECONDS) -> int:
        """Fetch and handle one batch of updates; returns how many were received."""
        updates = await self.get_updates(long_poll_seconds)
        for update in updates:
            await self.handle_update(update)
        return len(updates)

    async def run_polling(self, stop: asyncio.Event) -> None:
        """Poll until `stop` is set; on errors wait 5 s and retry. Returns at once when disabled."""
        if not self.enabled:
            return
        while not stop.is_set():
            try:
                await self.poll_once()
            except TelegramError as error:
                logger.warning("telegram polling failed: %s", error)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=POLL_ERROR_BACKOFF_SECONDS)
                except TimeoutError:
                    pass
