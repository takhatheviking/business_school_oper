"""Bridge to the Apps Script of the task-status spreadsheet (bot/Bridge.gs in this repo).

The spreadsheet's script sends task notifications to staff by itself. Incoming messages
can be read by only one program, and that is this server, so staff commands (/my tasks,
/team, /test) are forwarded to the script's web app. The script answers the person
directly through the Bot API with the same token.

Who is staff: everyone whose @username or chat_id is on the spreadsheet's "Команда" sheet.
The list is fetched from the script and cached for a few minutes.
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass

import aiohttp
from aiogram.types import Message
from aiogram.types import User as TgUser

log = logging.getLogger(__name__)


@dataclass
class BridgeReply:
    ok: bool
    client: bool = False  # the script did not recognise a staff member: the bot handles it as usual


class Bridge:
    def __init__(self, url: str | None, secret: str | None, *, cache_seconds: int = 300, timeout: float = 30) -> None:
        self.url, self.secret = url, secret
        self.cache_seconds, self.timeout = cache_seconds, timeout
        self._staff: tuple[set[str], set[int]] = (set(), set())
        self._staff_at = float("-inf")
        self._lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    async def _post(self, payload: dict) -> dict:
        """POST to the web app. Apps Script answers with a redirect to the result; aiohttp follows it."""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        async with self._http.post(self.url, json={"secret": self.secret, **payload}) as resp:
            text = await resp.text()
        try:
            data = json.loads(text)
        except ValueError:
            raise RuntimeError(f"bridge answered HTTP {resp.status} with non-JSON: {text[:120]!r}") from None
        if not data.get("ok"):
            raise RuntimeError(f"bridge refused: {data.get('error')}")
        return data

    async def staff(self) -> tuple[set[str], set[int]]:
        if not self.enabled:
            return set(), set()
        async with self._lock:
            if time.monotonic() - self._staff_at > self.cache_seconds:
                try:
                    data = await self._post({"action": "staff"})
                except Exception as e:  # noqa: BLE001 - keep the last known list (empty before the first success)
                    log.warning("bridge staff list failed: %s", e)
                else:
                    usernames = {str(u).lower().lstrip("@") for u in data.get("usernames", []) if u}
                    chat_ids = {int(c) for c in data.get("chat_ids", []) if str(c).lstrip("-").isdigit()}
                    self._staff = (usernames, chat_ids)
                self._staff_at = time.monotonic()
            return self._staff

    async def is_staff(self, user: TgUser, chat_id: int) -> bool:
        usernames, chat_ids = await self.staff()
        return chat_id in chat_ids or bool(user.username and user.username.lower() in usernames)

    async def is_registered(self, chat_id: int) -> bool:
        return chat_id in (await self.staff())[1]

    def forget_staff(self) -> None:
        """After a registration the list changes: fetch it again on the next message."""
        self._staff_at = float("-inf")

    async def forward(self, message: Message) -> BridgeReply:
        user = message.from_user
        payload = {
            "action": "message",
            "message": {
                "text": message.text or "",
                "chat": {"id": message.chat.id, "type": message.chat.type, "title": message.chat.title},
                "from": {"id": user.id, "username": user.username, "first_name": user.first_name} if user else {},
            },
        }
        try:
            data = await self._post(payload)
        except Exception as e:  # noqa: BLE001 - the caller tells the person the command is unavailable
            log.warning("bridge forward failed: %s", e)
            return BridgeReply(ok=False)
        return BridgeReply(ok=True, client=bool(data.get("client")))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
