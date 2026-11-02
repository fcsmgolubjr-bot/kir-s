"""Telegram Snake Rooms — minimal real-time server.

Run: uvicorn server:app --host 0.0.0.0 --port 8000
The same process serves both the HTML Mini App and its WebSocket endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import hmac
import hashlib
from urllib.parse import parse_qsl
import os
import urllib.request
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from game import COLORS, Match, TICK_SECONDS

logger = logging.getLogger("snake")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(CORSMiddleware, allow_origins=["https://fcsmgolubjr-bot.github.io"], allow_methods=["POST"], allow_headers=["Content-Type"])
ROOT = Path(__file__).parent / "static"
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ROOM_TTL = 30 * 60
MAX_ROOMS = 300


@dataclass
class Participant:
    pid: str
    ws: WebSocket
    name: str
    ready: bool = False


@dataclass
class Room:
    code: str
    slots: int
    owner: str
    players: dict[str, Participant] = field(default_factory=dict)
    phase: str = "lobby"
    match: Optional[Match] = None
    task: Optional[asyncio.Task] = None
    touched: float = field(default_factory=time.monotonic)


rooms: dict[str, Room] = {}


@app.get("/")
async def index():
    return FileResponse(ROOT / "index.html")



# The bot token is set in Render's environment, never committed to GitHub.
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "").strip() or secrets.token_hex(24)
PUBLIC_URL = "https://snake-arena-telegram.onrender.com"
STATIC_URL = "https://fcsmgolubjr-bot.github.io/kir-s/snake-arena/static/"
SOLO_URL = STATIC_URL + "?mode=solo"
MULTI_URL = STATIC_URL + "?mode=multi"

def telegram_api(method: str, payload: dict) -> dict:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API {method} rejected the request")
    return data

def game_buttons() -> dict:
    return {"inline_keyboard": [
        [{"text": "🐍 Одиночная игра", "web_app": {"url": SOLO_URL}}],
        [{"text": "⚔️ Мультиплеер (2–4)", "web_app": {"url": MULTI_URL}}],
    ]}

async def register_telegram_bot() -> None:
    """Configure the Telegram bot in the background, never delaying the HTTP port."""
    for attempt in range(3):
        try:
            await asyncio.wait_for(
                asyncio.to_thread(telegram_api, "setWebhook", {
                    "url": PUBLIC_URL + "/telegram/webhook",
                    "secret_token": BOT_WEBHOOK_SECRET,
                    "allowed_updates": ["message"],
                }),
                timeout=15,
            )
            await asyncio.wait_for(
                asyncio.to_thread(telegram_api, "setChatMenuButton", {
                    "menu_button": {
                        "type": "web_app", "text": "🎮 Режимы",
                        "web_app": {"url": STATIC_URL},
                    }
                }),
                timeout=15,
            )
            app.state.telegram_ready = True
            logger.info("Telegram webhook and bot menu configured")
            return
        except Exception as exc:
            # Do not log the API URL, because it contains the secret bot token.
            logger.warning(
                "Telegram setup attempt %d failed (%s); web server remains available",
                attempt + 1, type(exc).__name__,
            )
            if attempt < 2:
                await asyncio.sleep(20 * (attempt + 1))
    logger.error("Telegram setup is unavailable; games are still accessible by URL")


@app.on_event("startup")
async def connect_telegram_bot() -> None:
    app.state.telegram_ready = False
    if not BOT_TOKEN:
        logger.info("BOT_TOKEN is absent; /start buttons are not yet enabled")
        return
    # Waiting for external Telegram API during startup blocked Render's port scan.
    app.state.telegram_setup_task = asyncio.create_task(register_telegram_bot())

@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    if not BOT_TOKEN or not hmac.compare_digest(
        request.headers.get("X-Telegram-Bot-Api-Secret-Token", ""),
        BOT_WEBHOOK_SECRET,
    ):
        return JSONResponse({"ok": False}, status_code=403)

    body = await request.body()
    if len(body) > 65536:
        return JSONResponse({"ok": False}, status_code=413)
    try:
        update = json.loads(body)
    except (ValueError, TypeError):
        return JSONResponse({"ok": False}, status_code=400)

    if isinstance(update, dict):
        message = update.get("message") or {}
        chat = message.get("chat") or {}
        command = (message.get("text") or "").split(maxsplit=1)
        if (
            isinstance(message, dict)
            and isinstance(chat, dict)
            and chat.get("type") == "private"
            and command and command[0].split("@", 1)[0] in ("/start", "/menu", "/help")
        ):
            try:
                # Short invite: t.me/snake_arcade_play_bot?start=join_ABCDEF
                arg = command[1].strip() if len(command) > 1 else ""
                invite_code = clean_code(arg[5:]) if arg.lower().startswith("join_") else ""
                if invite_code and len(invite_code) == 6:
                    message_text = f"🐍 Приглашение в комнату {invite_code}"
                    reply_buttons = {"inline_keyboard": [[{
                        "text": "Войти в комнату",
                        "web_app": {"url": STATIC_URL + "?room=" + invite_code},
                    }]]}
                else:
                    message_text = "🐍 Выбери режим игры:"
                    reply_buttons = game_buttons()
                await asyncio.to_thread(
                    telegram_api, "sendMessage", {
                        "chat_id": chat["id"],
                        "text": message_text,
                        "reply_markup": reply_buttons,
                    }
                )
            except Exception:
                logger.exception("Could not send Telegram game selection buttons")
    return {"ok": True}



# The invitation is a Telegram message with a button, not a visible long URL.
# Telegram only accepts prepared messages for the authenticated Mini App user.
def validate_webapp_init_data(init_data: str) -> Optional[int]:
    if not BOT_TOKEN or not isinstance(init_data, str) or len(init_data) > 8192:
        return None
    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        if len(pairs) != len({key for key, _ in pairs}):
            return None
        values = dict(pairs)
        provided = values.get("hash", "")
        if len(provided) != 64:
            return None
        check = "\n".join(
            f"{key}={value}" for key, value in sorted(pairs)
            if key not in ("hash", "signature")
        )
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(provided, expected):
            return None
        auth_date = int(values.get("auth_date", "0"))
        if auth_date > time.time() + 60 or time.time() - auth_date > 86400:
            return None
        user_id = int(json.loads(values["user"])["id"])
        return user_id if user_id > 0 else None
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


# Simple per-user throttle: protect the Telegram API from repeated taps.
share_attempts: dict[int, list[float]] = {}


@app.post("/api/prepared-invite")
async def prepared_invite(request: Request):
    if not BOT_TOKEN:
        return JSONResponse({"error": "Telegram bot is not connected"}, status_code=503)
    raw = await request.body()
    if len(raw) > 9000:
        return JSONResponse({"error": "Invalid request"}, status_code=413)
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        init_data = payload.get("initData", "")
        code = str(payload.get("code", "")).upper()
    except (ValueError, TypeError):
        return JSONResponse({"error": "Invalid request"}, status_code=400)
    user_id = validate_webapp_init_data(init_data)
    if user_id is None:
        return JSONResponse({"error": "Open the game inside Telegram"}, status_code=401)
    if not (len(code) == 6 and all(ch in LETTERS for ch in code)):
        return JSONResponse({"error": "Invalid room code"}, status_code=400)
    room = rooms.get(code)
    if room is None or room.phase != "lobby" or len(room.players) >= room.slots:
        return JSONResponse({"error": "This room is no longer accepting players"}, status_code=404)
    now = time.monotonic()
    attempts = [t for t in share_attempts.get(user_id, []) if now - t < 60]
    if len(attempts) >= 8:
        return JSONResponse({"error": "Too many invitations"}, status_code=429)
    attempts.append(now)
    share_attempts[user_id] = attempts
    if len(share_attempts) > 1000:
        share_attempts.clear()
    direct_url = "https://t.me/snake_arcade_play_bot?start=join_" + code
    result = {
        "type": "article",
        "id": secrets.token_hex(8),
        "title": "Приглашение в Snake Arena",
        "input_message_content": {
            "message_text": "🐍 Snake Arena\nЗаходи сыграть со мной!\nКомната: " + code,
        },
        "reply_markup": {
            "inline_keyboard": [[{"text": "🎮 Войти в комнату", "url": direct_url}]]
        },
    }
    try:
        response = await asyncio.to_thread(
            telegram_api, "savePreparedInlineMessage", {
                "user_id": user_id, "result": result,
                "allow_user_chats": True, "allow_group_chats": True,
            }
        )
        prepared_id = response.get("result", {}).get("id")
        if not isinstance(prepared_id, str) or not prepared_id:
            raise ValueError("Missing prepared message id")
    except Exception as exc:
        logger.warning("Could not prepare an invite (%s)", type(exc).__name__)
        return JSONResponse({"error": "Unable to prepare Telegram invitation"}, status_code=502)
    return JSONResponse({"id": prepared_id})


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "game": "snake", "rooms": len(rooms), "telegram_ready": getattr(app.state, "telegram_ready", False)})


def clean_name(raw) -> str:
    if not isinstance(raw, str):
        return "Игрок"
    name = re.sub(r"[\x00-\x1f\x7f]+", "", raw).strip()[:18]
    return name or "Игрок"


def clean_code(raw) -> str:
    return re.sub(r"[^A-Z2-9]", "", str(raw or "").upper())[:6]


def generate_code() -> str:
    for _ in range(100):
        code = "".join(secrets.choice(LETTERS) for _ in range(6))
        if code not in rooms:
            return code
    raise RuntimeError("Could not allocate a unique room code")


def cleanup_rooms() -> None:
    # Empty expired rooms have no connected sockets, so safe to remove.
    now = time.monotonic()
    for code, room in list(rooms.items()):
        if not room.players and now - room.touched > ROOM_TTL:
            if room.task:
                room.task.cancel()
            rooms.pop(code, None)


def lobby_data(room: Room) -> dict:
    return {
        "type": "lobby", "code": room.code, "slots": room.slots,
        "phase": room.phase, "owner": room.owner,
        "players": [
            {"id": p.pid, "name": p.name, "ready": p.ready, "color": COLORS[i]}
            for i, p in enumerate(room.players.values())
        ],
    }


async def send_one(ws: WebSocket, payload: dict) -> None:
    try:
        await ws.send_json(payload)
    except Exception:
        # The receive-loop will handle the disconnect.
        pass


async def send_all(room: Room, payload: dict) -> None:
    for p in list(room.players.values()):
        await send_one(p.ws, payload)


async def broadcast_lobby(room: Room) -> None:
    room.touched = time.monotonic()
    await send_all(room, lobby_data(room))


def all_ready(room: Room) -> bool:
    return (
        room.phase == "lobby" and len(room.players) == room.slots
        and all(p.ready for p in room.players.values())
    )


async def run_match(room: Room) -> None:
    try:
        for sec in (3, 2, 1):
            if room.phase != "countdown" or len(room.players) != room.slots:
                return
            await send_all(room, {"type": "countdown", "seconds": sec})
            await asyncio.sleep(1)

        if room.phase != "countdown" or len(room.players) != room.slots:
            return
        room.match = Match([(p.pid, p.name) for p in room.players.values()])
        room.phase = "playing"
        await send_all(room, {"type": "match_start"})
        await send_all(room, room.match.snapshot())
        while room.phase == "playing" and room.match and not room.match.done:
            await asyncio.sleep(TICK_SECONDS)
            room.match.step()
            await send_all(room, room.match.snapshot())
        if room.phase == "playing" and room.match:
            room.phase = "ended"
            await send_all(room, room.match.result())
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled game loop failure in room %s", room.code)
        room.phase = "ended"
        await send_all(room, {"type": "error", "message": "Ошибка матча. Создайте новую комнату."})
    finally:
        room.task = None


async def detach(room: Room, pid: str) -> None:
    if pid not in room.players:
        return
    room.players.pop(pid)
    room.touched = time.monotonic()
    if not room.players:
        if room.task:
            room.task.cancel()
        rooms.pop(room.code, None)
        return
    if room.owner == pid:
        room.owner = next(iter(room.players))
    if room.phase == "countdown":
        room.phase = "lobby"
        if room.task:
            room.task.cancel()
    if room.phase == "playing" and room.match:
        room.match.eliminate(pid)
        await send_all(room, room.match.snapshot())
        if room.match.done:
            room.phase = "ended"
            if room.task:
                room.task.cancel()
            await send_all(room, room.match.result())
    await broadcast_lobby(room)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    pid = secrets.token_urlsafe(12)
    room: Optional[Room] = None
    try:
        # First message must select a room; no hidden Telegram token needed.
        while True:
            raw = await ws.receive_text()
            if len(raw) > 2048:
                await send_one(ws, {"type": "error", "message": "Слишком длинный запрос."})
                continue
            try:
                packet = json.loads(raw)
            except (ValueError, TypeError):
                await send_one(ws, {"type": "error", "message": "Некорректный запрос."})
                continue
            if not isinstance(packet, dict):
                continue
            command = packet.get("type")
            if room is None:
                if command not in ("create", "join"):
                    await send_one(ws, {"type": "error", "message": "Сначала создайте комнату или войдите в неё."})
                    continue
                cleanup_rooms()
                name = clean_name(packet.get("name"))
                if command == "create":
                    if len(rooms) >= MAX_ROOMS:
                        await send_one(ws, {"type": "error", "message": "Сервер занят. Повторите попытку позже."})
                        continue
                    try:
                        slots = int(packet.get("slots", 2))
                    except (ValueError, TypeError):
                        slots = 0
                    if slots not in (2, 3, 4):
                        await send_one(ws, {"type": "error", "message": "Выберите 2, 3 или 4 игрока."})
                        continue
                    code = generate_code()
                    room = Room(code, slots, pid)
                    rooms[code] = room
                else:
                    code = clean_code(packet.get("code"))
                    target = rooms.get(code)
                    if target is None:
                        await send_one(ws, {"type": "error", "message": "Комната не найдена. Проверьте код."})
                        continue
                    if target.phase != "lobby" or len(target.players) >= target.slots:
                        await send_one(ws, {"type": "error", "message": "Комната уже заполнена или матч начался."})
                        continue
                    room = target
                room.players[pid] = Participant(pid, ws, name)
                await send_one(ws, {"type": "welcome", "id": pid, "code": room.code, "slots": room.slots})
                await broadcast_lobby(room)
                continue

            room.touched = time.monotonic()
            if command == "ready" and room.phase == "lobby":
                room.players[pid].ready = bool(packet.get("value", False))
                await broadcast_lobby(room)
                if all_ready(room):
                    room.phase = "countdown"
                    await broadcast_lobby(room)
                    room.task = asyncio.create_task(run_match(room))
            elif command == "turn" and room.phase == "playing" and room.match:
                direction = packet.get("direction")
                if isinstance(direction, str):
                    room.match.queue_turn(pid, direction)
            elif command == "again" and room.phase == "ended":
                room.phase = "lobby"
                room.match = None
                for participant in room.players.values():
                    participant.ready = False
                await broadcast_lobby(room)
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("Unexpected client socket error")
    finally:
        if room:
            await detach(room, pid)