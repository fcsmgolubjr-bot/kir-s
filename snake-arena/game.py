"""Pure authoritative game rules for a 2–4 player Snake match.

No Telegram token, payment gateway, or external dependency is needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import Optional

WIDTH = 20
HEIGHT = 20
TICK_SECONDS = 0.18
MAX_TICKS = 500  # 90-second match at 180 ms per tick
FOOD_COUNT = 2
DIRECTIONS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0)}
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}
COLORS = ["#a5bd38", "#d78551", "#8da6d9", "#b99dca"]
# Separated corners; opposite tails stay safely away from each other.
SPAWNS = [((4, 4), "right"), ((WIDTH - 5, HEIGHT - 5), "left"),
          ((WIDTH - 5, 4), "left"), ((4, HEIGHT - 5), "right")]


@dataclass
class Snake:
    pid: str
    name: str
    color: str
    body: list[tuple[int, int]]
    direction: str
    wanted: str
    alive: bool = True
    score: int = 0

    def queue(self, direction: str) -> None:
        if direction in DIRECTIONS and direction != OPPOSITE[self.direction]:
            self.wanted = direction


@dataclass
class Match:
    entrants: list[tuple[str, str]]
    rng: random.Random = field(default_factory=random.Random)
    snakes: list[Snake] = field(init=False)
    food: list[tuple[int, int]] = field(default_factory=list)
    bonus: Optional[tuple[int, int]] = None
    bonus_expires: int = 0
    tick: int = 0
    done: bool = False
    winner: Optional[str] = None
    reason: str = ""

    def __post_init__(self) -> None:
        if not (2 <= len(self.entrants) <= 4):
            raise ValueError("A match needs 2–4 players")
        self.snakes = []
        for index, (pid, name) in enumerate(self.entrants):
            head, direction = SPAWNS[index]
            dx, dy = DIRECTIONS[direction]
            body = [(head[0] - dx * t, head[1] - dy * t) for t in range(3)]
            self.snakes.append(Snake(pid, name, COLORS[index], body, direction, direction))
        for _ in range(FOOD_COUNT):
            self.spawn_food()

    def spawn_food(self) -> None:
        occupied = {pos for snake in self.snakes for pos in snake.body}
        occupied.update(self.food)
        free = [(x, y) for y in range(HEIGHT) for x in range(WIDTH) if (x, y) not in occupied]
        if free:
            self.food.append(self.rng.choice(free))

    def spawn_bonus(self) -> None:
        occupied = {pos for snake in self.snakes for pos in snake.body}
        occupied.update(self.food)
        free = [(x, y) for y in range(2, HEIGHT - 2) for x in range(2, WIDTH - 2)
                if (x, y) not in occupied]
        if free:
            self.bonus = self.rng.choice(free)
            self.bonus_expires = self.tick + 28

    def queue_turn(self, pid: str, direction: str) -> None:
        if self.done:
            return
        for snake in self.snakes:
            if snake.pid == pid and snake.alive:
                snake.queue(direction)
                return

    def eliminate(self, pid: str) -> None:
        for snake in self.snakes:
            if snake.pid == pid:
                snake.alive = False
        self.check_finished()

    def check_finished(self) -> None:
        live = [p for p in self.snakes if p.alive]
        if len(live) <= 1:
            self.done = True
            self.winner = live[0].pid if live else None
            self.reason = "last_snake" if live else "draw"

    def step(self) -> None:
        if self.done:
            return
        self.tick += 1
        if self.bonus is not None and self.tick >= self.bonus_expires:
            self.bonus = None
        if self.bonus is None and self.tick % 52 == 0:
            self.spawn_bonus()
        heads = {}
        eating = {}
        for snake in self.snakes:
            if not snake.alive:
                continue
            if snake.wanted != OPPOSITE[snake.direction]:
                snake.direction = snake.wanted
            dx, dy = DIRECTIONS[snake.direction]
            old = snake.body[0]
            heads[snake.pid] = (old[0] + dx, old[1] + dy)
            eating[snake.pid] = heads[snake.pid] in self.food

        # A vacating tail is safe unless its owner is growing this tick.
        occupied: set[tuple[int, int]] = set()
        for snake in self.snakes:
            if snake.alive:
                occupied.update(snake.body if eating[snake.pid] else snake.body[:-1])

        eliminated = set()
        for snake in self.snakes:
            if not snake.alive:
                continue
            x, y = heads[snake.pid]
            if x < 0 or x >= WIDTH or y < 0 or y >= HEIGHT or (x, y) in occupied:
                eliminated.add(snake.pid)

        # Two heads in the same square, or passing through each other, both lose.
        live = [s for s in self.snakes if s.alive]
        for i, p in enumerate(live):
            for q in live[i + 1:]:
                if heads[p.pid] == heads[q.pid] or (
                    heads[p.pid] == q.body[0] and heads[q.pid] == p.body[0]
                ):
                    eliminated.update((p.pid, q.pid))

        consumed = set()
        bonus_taken = False
        for snake in live:
            if snake.pid in eliminated:
                snake.alive = False
                continue
            head = heads[snake.pid]
            snake.body.insert(0, head)
            if eating[snake.pid]:
                snake.score += 1
                consumed.add(head)
            else:
                snake.body.pop()
            if self.bonus is not None and head == self.bonus:
                snake.score += 3
                bonus_taken = True

        if bonus_taken:
            self.bonus = None
        if consumed:
            self.food = [pos for pos in self.food if pos not in consumed]
            for _ in consumed:
                self.spawn_food()

        self.check_finished()
        if not self.done and self.tick >= MAX_TICKS:
            high = max(s.score for s in self.snakes)
            leaders = [s for s in self.snakes if s.score == high]
            self.winner = leaders[0].pid if len(leaders) == 1 else None
            self.reason = "time" if self.winner else "draw"
            self.done = True

    def snapshot(self) -> dict:
        return {
            "type": "state", "width": WIDTH, "height": HEIGHT,
            "tick": self.tick, "remaining": max(0, round((MAX_TICKS - self.tick) * TICK_SECONDS)),
            "food": [list(p) for p in self.food],
            "bonus": list(self.bonus) if self.bonus is not None else None,
            "snakes": [
                {"id": s.pid, "name": s.name, "color": s.color, "body": [list(p) for p in s.body],
                 "alive": s.alive, "score": s.score} for s in self.snakes
            ],
        }

    def result(self) -> dict:
        return {
            "type": "result", "winnerId": self.winner, "reason": self.reason,
            "players": [
                {"id": s.pid, "name": s.name, "score": s.score, "alive": s.alive, "color": s.color}
                for s in self.snakes
            ],
        }