"""The bridge client against a local server that behaves like an Apps Script web app:
POST /exec answers 302 to a one-off URL, and the result comes from a GET there."""

import json
from datetime import UTC, datetime

from aiogram.types import Chat, Message, User
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.services.bridge import Bridge


async def make_app_script(handler):
    received: list[dict] = []
    results: dict[str, dict] = {}

    async def exec_(request: web.Request) -> web.Response:
        body = await request.json()
        received.append(body)
        key = str(len(received))
        results[key] = handler(body)
        raise web.HTTPFound(f"/echo?user_content_key={key}")

    async def echo(request: web.Request) -> web.Response:
        return web.Response(text=json.dumps(results[request.query["user_content_key"]]), content_type="application/json")

    app = web.Application()
    app.router.add_post("/exec", exec_)
    app.router.add_get("/echo", echo)
    server = TestServer(app)
    await server.start_server()
    return server, received


async def test_bridge_follows_the_apps_script_redirect():
    def handler(body):
        if body["secret"] != "s":
            return {"ok": False, "error": "forbidden"}
        if body["action"] == "staff":
            return {"ok": True, "usernames": ["Lola_M", "@boss"], "chat_ids": ["777", "-100", "x"]}
        return {"ok": True, "client": body["message"]["text"] == "/test"}

    server, received = await make_app_script(handler)
    try:
        bridge = Bridge(str(server.make_url("/exec")), "s")
        usernames, chat_ids = await bridge.staff()
        assert usernames == {"lola_m", "boss"} and chat_ids == {777, -100}

        msg = Message(message_id=1, date=datetime.now(UTC), chat=Chat(id=777, type="private"), from_user=User(id=777, is_bot=False, first_name="L", username="lola_m"), text="/my")
        reply = await bridge.forward(msg)
        assert reply.ok and not reply.client
        assert received[-1]["message"] == {"text": "/my", "chat": {"id": 777, "type": "private", "title": None}, "from": {"id": 777, "username": "lola_m", "first_name": "L"}}

        wrong = Bridge(str(server.make_url("/exec")), "wrong")
        assert (await wrong.forward(msg)).ok is False
        assert await wrong.staff() == (set(), set())
        await bridge.close()
        await wrong.close()
    finally:
        await server.close()
