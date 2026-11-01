"""Telegram Snake Rooms — minimal real-time server.

Run: uvicorn server:app --host 0.0.0.0 --port 8000
The same process serves both the HTML Mini App and its WebSocket endpoint.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from game import COLORS, Match, TICK_SECONDS

logger = logging.getLogger("snake")
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
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


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok", "game": "snake", "rooms": len(rooms)})


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