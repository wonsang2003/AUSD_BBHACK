"""
Tests for app/alerts/telegram.py — no network.

The Telegram Bot API is served by an in-process httpx.MockTransport that answers with the
documented response shapes (`{"ok": true, "result": ...}`, getUpdates update objects), so the
real request/response code runs. Live delivery is verified on the user's machine with a
real TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import asyncio
import json

import httpx

from app.alerts.telegram import (
    HELP_TEXT,
    MAX_MESSAGE_CHARS,
    CommandHandlers,
    TelegramBot,
    parse_command,
    resolve_agent,
    truncate_message,
)

TOKEN = "123456:TEST-TOKEN"
CHAT_ID = "4242"
OTHER_CHAT = "9999"


class FakeTelegramApi:
    """In-process Bot API: records sendMessage calls and serves queued getUpdates results."""

    def __init__(self, updates: list[dict] | None = None, fail_with_status: int | None = None):
        self.sent: list[dict] = []
        self.paths: list[str] = []
        self.updates = list(updates or [])
        self.fail_with_status = fail_with_status

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        if self.fail_with_status is not None:
            return httpx.Response(self.fail_with_status, json={"ok": False, "description": "Unauthorized"})
        payload = json.loads(request.content or b"{}")
        if request.url.path.endswith("/sendMessage"):
            self.sent.append(payload)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        if request.url.path.endswith("/getUpdates"):
            offset = payload.get("offset", 0)
            fresh = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": fresh})
        return httpx.Response(404, json={"ok": False, "description": "Not Found"})


def _message_update(update_id: int, text: str, chat_id: str = CHAT_ID) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "date": 1_760_200_000,
            "chat": {"id": int(chat_id), "type": "private"},
            "from": {"id": int(chat_id), "is_bot": False, "first_name": "Trader"},
            "text": text,
        },
    }


def _bot(api: FakeTelegramApi, chat_id: str = CHAT_ID, token: str = TOKEN, calls: list | None = None) -> TelegramBot:
    recorded = calls if calls is not None else []
    handlers = CommandHandlers(
        status=lambda: "desk OK",
        pause=lambda agent: recorded.append(("pause", agent)) or f"{agent} paused",
        resume=lambda agent: recorded.append(("resume", agent)) or f"{agent} resumed",
        brief=_async_brief,
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(api.handler))
    return TelegramBot(token, chat_id, handlers, http=http)


async def _async_brief() -> str:
    return "brief text"


# ─── pure helpers ────────────────────────────────────────────────────────────

def test_parse_command_strips_bot_mention_and_splits_argument():
    # Act
    command = parse_command("/pause@PocketDeskBot OptionsEvent")

    # Assert
    assert (command.name, command.argument) == ("/pause", "OptionsEvent")
    assert parse_command("hello") is None


def test_resolve_agent_is_case_insensitive_and_rejects_unknown_names():
    # Act / Assert
    assert resolve_agent("optionsevent") == "OptionsEvent"
    assert resolve_agent("HedgeFund") is None


def test_truncate_message_respects_the_telegram_limit():
    # Act
    text = truncate_message("x" * (MAX_MESSAGE_CHARS + 50))

    # Assert
    assert len(text) == MAX_MESSAGE_CHARS
    assert text.endswith("…")


# ─── sending ─────────────────────────────────────────────────────────────────

async def test_send_message_posts_plain_text_to_the_configured_chat():
    # Arrange
    api = FakeTelegramApi()
    bot = _bot(api)

    # Act
    delivered = await bot.send_message("TOKEN PAUSED — agents halted")

    # Assert
    assert delivered
    assert api.sent == [{"chat_id": CHAT_ID, "text": "TOKEN PAUSED — agents halted", "disable_web_page_preview": True}]
    assert api.paths == [f"/bot{TOKEN}/sendMessage"]


async def test_bot_without_token_is_a_silent_no_op():
    # Arrange
    api = FakeTelegramApi()
    bot = _bot(api, token="")

    # Act
    delivered = await bot.send_message("hello")
    bot.notify("hello")
    await bot.run_polling(asyncio.Event())

    # Assert
    assert not delivered
    assert api.paths == []


async def test_send_failure_returns_false_and_error_text_never_contains_the_token(caplog):
    # Arrange
    api = FakeTelegramApi(fail_with_status=401)
    bot = _bot(api)

    # Act
    delivered = await bot.send_message("hello")

    # Assert
    assert not delivered
    assert TOKEN not in caplog.text
    assert "401" in caplog.text


async def test_notify_on_the_running_loop_sends_in_the_background():
    # Arrange
    api = FakeTelegramApi()
    bot = _bot(api)

    # Act
    bot.notify("runway below 3 days")
    await bot.wait_for_background()

    # Assert
    assert [message["text"] for message in api.sent] == ["runway below 3 days"]


# ─── commands ────────────────────────────────────────────────────────────────

async def test_pause_command_from_the_configured_chat_calls_the_handler_and_replies():
    # Arrange
    api = FakeTelegramApi()
    calls: list = []
    bot = _bot(api, calls=calls)

    # Act
    reply = await bot.handle_update(_message_update(1, "/pause optionsevent"))

    # Assert
    assert calls == [("pause", "OptionsEvent")]
    assert reply == "OptionsEvent paused"
    assert api.sent[-1]["text"] == "OptionsEvent paused"


async def test_commands_from_another_chat_are_ignored():
    # Arrange
    api = FakeTelegramApi()
    calls: list = []
    bot = _bot(api, calls=calls)

    # Act
    reply = await bot.handle_update(_message_update(1, "/pause OptionsEvent", chat_id=OTHER_CHAT))

    # Assert
    assert reply is None
    assert calls == []
    assert api.sent == []


async def test_without_chat_id_the_bot_only_tells_the_sender_how_to_configure_it():
    # Arrange
    api = FakeTelegramApi()
    calls: list = []
    bot = _bot(api, chat_id="", calls=calls)

    # Act
    reply = await bot.handle_update(_message_update(1, "/pause OptionsEvent", chat_id=OTHER_CHAT))

    # Assert
    assert f"TELEGRAM_CHAT_ID={OTHER_CHAT}" in reply
    assert calls == []
    assert api.sent[0]["chat_id"] == OTHER_CHAT


async def test_unknown_agent_and_unknown_command_get_helpful_replies():
    # Arrange
    bot = _bot(FakeTelegramApi())

    # Act
    unknown_agent = await bot.handle_update(_message_update(1, "/resume Nobody"))
    unknown_command = await bot.handle_update(_message_update(2, "/sell everything"))
    help_reply = await bot.handle_update(_message_update(3, "/help"))

    # Assert
    assert unknown_agent.startswith("Unknown agent 'Nobody'")
    assert unknown_command.startswith("Unknown command /sell")
    assert help_reply == HELP_TEXT


async def test_async_handlers_are_awaited():
    # Arrange
    bot = _bot(FakeTelegramApi())

    # Act
    reply = await bot.handle_update(_message_update(1, "/brief"))

    # Assert
    assert reply == "brief text"


async def test_poll_once_handles_updates_and_advances_the_offset():
    # Arrange
    api = FakeTelegramApi(updates=[_message_update(10, "/status"), _message_update(11, "/resume NewsSentiment")])
    calls: list = []
    bot = _bot(api, calls=calls)

    # Act
    first_batch = await bot.poll_once(long_poll_seconds=0)
    second_batch = await bot.poll_once(long_poll_seconds=0)

    # Assert
    assert (first_batch, second_batch) == (2, 0)
    assert [message["text"] for message in api.sent] == ["desk OK", "NewsSentiment resumed"]
    assert calls == [("resume", "NewsSentiment")]
