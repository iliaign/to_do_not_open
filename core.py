"""
core.py — правила и физика игры. Не зависит ни от pygame, ни от сокетов.

Один и тот же World используется:
  * в режиме «игра с ботом» (локально, внутри клиента);
  * на сервере в онлайн-режиме (авторитетная симуляция).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

# ---------------------------------------------------------------- настройки
SPEED_SCALE = 1.0                 # ускорение игрового времени
OBJECT_SCALE = 1.0 / 6.0          # размер объекта относительно оригинала
TICK_RATE = 60                    # тиков симуляции в секунду
TICK = 1.0 / TICK_RATE
SIM_DT = TICK * SPEED_SCALE       # игровое время за один тик

PLAYER_CONTROL_SPEED = 8.0 * 1.2  # скорость игрока
WALK_SPEED = 0.6
NEW_PLAYER_FORCED_INTERVAL = 60.0  # реальные секунды
KICK_DURATION = 0.46
EVENT_DURATION = 1.15

HALF_WIDTH = 2.3 * OBJECT_SCALE
HALF_HEIGHT = 4.6 * OBJECT_SCALE
MANUAL_KICK_RANGE = 3.2
MANUAL_KICK_COOLDOWN = 0.25       # пауза между ручными пинками одного игрока (сервер следит сам)
LAG_TOLERANCE = 8.0               # насколько «увиденный» клиентом объект может отличаться
                                  # от настоящего (компенсация задержки: 100 мс интерполяции + пинг)

WIN_KICKS = 25                    # сколько пинков нужно для победы в раунде
ROUND_RESTART_DELAY = 6.0         # пауза после победы до нового раунда (реальные секунды)
NAME_MAX = 12                     # максимальная длина ника

# Границы мира (как в оригинальном matplotlib-варианте)
X_MIN, X_MAX = -12.0, 12.0
Y_MIN, Y_MAX = -7.0, 13.0
WALL_X = 8.05
PERSON_X = 10.35
GROUND_MIN = Y_MIN + 0.6
GROUND_MAX = Y_MAX - 5.6
PERSON_X_LIMIT = X_MAX - 1.2

MAX_HUMANS = 4
MAX_EXTRAS = 8

HUMAN_COLORS = ["#60a5fa", "#f87171", "#22d3ee", "#fb923c"]
EXTRA_COLORS = ["#34d399", "#f472b6", "#fbbf24", "#a78bfa"]
BOT_COLOR = "#9ca3af"


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def sanitize_name(raw) -> str:
    """Ник: только печатные символы, без лишних пробелов, не длиннее NAME_MAX."""
    text = "".join(ch for ch in str(raw) if ch.isprintable())
    text = " ".join(text.split())
    return text[:NAME_MAX].strip()


@dataclass
class Person:
    pid: int
    kind: str            # "human" | "bot" | "npc"
    name: str
    color: str
    x: float
    ground_y: float
    facing: int
    kick_timer: float = 0.0
    kick_lift: int = 1
    walk_speed: float = 0.0
    walk_dir: int = 1
    mx: int = 0          # текущий ввод (только для human)
    my: int = 0
    kicks: int = 0       # личный счёт пинков
    kick_cd: float = 0.0  # остаток паузы между ручными пинками


class World:
    def __init__(self, spawn_extras: bool = True, seed=None):
        self.rng = random.Random(seed)
        self.spawn_extras = spawn_extras
        self.persons: dict[int, Person] = {}
        self._next_id = 1
        self.roster_version = 0     # растёт при любом добавлении/удалении/переименовании

        # «объект», который пинают
        self.x, self.y = 0.0, 3.0
        self.vx, self.vy = 12.0, 7.0
        self.angle = 0.0
        self.av = 85.0
        self.mult = 1.0
        self.boost_hits = 0

        self.time_s = 0.0
        self.last_new_time = 0.0
        self.label = ""
        self.label_timer = 0.0
        self.stats = {"total": 0, "up": 0, "down": 0, "manual": 0}

        # раунды
        self.winner = None          # pid победителя или None
        self.winner_name = ""
        self.round_timer = 0.0
        self._saved_scores: dict[str, int] = {}   # счёт вышедших игроков по нику

        self._leader = None         # pid лидера таблицы
        self.events: list = []      # звуковые события для клиентов
        self._kick_queue: list = []  # [(pid, claim | None)]

    # ------------------------------------------------------------ люди
    def _new_id(self) -> int:
        pid = self._next_id
        self._next_id += 1
        return pid

    def humans(self) -> list[Person]:
        return [p for p in self.persons.values() if p.kind == "human"]

    def _unique_name(self, base: str) -> str:
        taken = {p.name.lower() for p in self.humans()}
        if base.lower() not in taken:
            return base
        n = 2
        while True:
            suffix = f" {n}"
            cand = base[:NAME_MAX - len(suffix)] + suffix
            if cand.lower() not in taken:
                return cand
            n += 1

    def add_human(self, name: str = ""):
        """Добавляет игрока. Возвращает Person или None, если мест нет."""
        used = {p.color for p in self.humans()}
        slot = next((i for i, c in enumerate(HUMAN_COLORS) if c not in used), None)
        if slot is None or len(self.humans()) >= MAX_HUMANS:
            return None
        facing = 1 if slot % 2 == 0 else -1
        nick = self._unique_name(sanitize_name(name) or f"Игрок {slot + 1}")
        p = Person(
            pid=self._new_id(), kind="human", name=nick,
            color=HUMAN_COLORS[slot],
            x=-PERSON_X if facing == 1 else PERSON_X,
            ground_y=-5.35 + (slot // 2) * 4.0,
            facing=facing,
        )
        p.kicks = self._saved_scores.pop(nick, 0)    # вернулся — счёт сохранился
        self.persons[p.pid] = p
        self.roster_version += 1
        return p

    def add_bot(self) -> Person:
        """Стоячий бот справа. Сам не двигается, но пинает при касании стены."""
        p = Person(
            pid=self._new_id(), kind="bot", name="БОТ", color=BOT_COLOR,
            x=PERSON_X, ground_y=-0.5, facing=-1, walk_speed=0.0,
        )
        self.persons[p.pid] = p
        self.roster_version += 1
        return p

    def _add_extra(self) -> None:
        extras = sum(1 for p in self.persons.values() if p.kind == "npc")
        if extras >= MAX_EXTRAS:
            return
        facing = self.rng.choice((-1, 1))
        p = Person(
            pid=self._new_id(), kind="npc", name="NPC",
            color=self.rng.choice(EXTRA_COLORS),
            x=-PERSON_X if facing == 1 else PERSON_X,
            ground_y=self.rng.uniform(-5.85, 4.7),
            facing=facing, walk_speed=WALK_SPEED,
            walk_dir=self.rng.choice((-1, 1)),
        )
        self.persons[p.pid] = p
        self.roster_version += 1

    def remove(self, pid: int) -> None:
        p = self.persons.pop(pid, None)
        if p is None:
            return
        if p.kind == "human" and p.kicks > 0 and self.winner is None:
            self._saved_scores[p.name] = p.kicks
        self.roster_version += 1

    def roster(self) -> list:
        """Статичные данные о людях (цвет, тип, имя) — шлются клиентам только при изменениях."""
        return [[p.pid, p.color, p.kind, p.name] for p in self.persons.values()]

    # ----------------------------------------------------------- ввод
    def set_input(self, pid: int, mx: int, my: int) -> None:
        p = self.persons.get(pid)
        if p and p.kind == "human":
            p.mx, p.my = int(clamp(mx, -1, 1)), int(clamp(my, -1, 1))

    def queue_kick(self, pid: int, claim=None) -> None:
        """claim — (x, y) объекта так, как его видел игрок в момент нажатия."""
        self._kick_queue.append((pid, claim))

    # --------------------------------------------------------- события
    def _emit(self, *ev) -> None:
        self.events.append(list(ev))

    def _set_label(self, text: str) -> None:
        self.label = text
        self.label_timer = EVENT_DURATION

    def _lead_change(self, p: Person) -> str:
        """Текст, если после пинка p кто-то единолично вышел вперёд в таблице."""
        cands = [q for q in self.persons.values() if q.kind in ("human", "bot")]
        if p not in cands:
            return ""
        top = max(q.kicks for q in cands)
        leaders = [q for q in cands if q.kicks == top]
        if top >= 2 and len(leaders) == 1 and leaders[0].pid != self._leader:
            self._leader = leaders[0].pid
            return f"{leaders[0].name.upper()} ВПЕРЕДИ!"
        return ""

    def _count_kick(self, p: Person, up: bool, manual: bool) -> None:
        self.stats["total"] += 1
        self.stats["up" if up else "down"] += 1
        if manual:
            self.stats["manual"] += 1
        self._emit("kick", up)
        if self.winner is None or p.kind == "npc":     # после победы счёт людей заморожен
            p.kicks += 1
        if self.winner is None and p.kind in ("human", "bot") and p.kicks >= WIN_KICKS:
            self._finish_round(p)

    def _finish_round(self, p: Person) -> None:
        self.winner = p.pid
        self.winner_name = p.name
        self.round_timer = ROUND_RESTART_DELAY
        self._set_label(f"{p.name.upper()} ПОБЕДИЛ!")
        self._emit("win")

    def reset_round(self) -> None:
        for p in self.persons.values():
            p.kicks = 0
        self._saved_scores.clear()
        self.winner = None
        self.winner_name = ""
        self.round_timer = 0.0
        self._leader = None
        self._set_label("НОВЫЙ РАУНД")

    # ---------------------------------------------------------- физика
    def _extents(self) -> tuple[float, float]:
        r = math.radians(self.angle)
        c, s = abs(math.cos(r)), abs(math.sin(r))
        return c * HALF_WIDTH + s * HALF_HEIGHT, s * HALF_WIDTH + c * HALF_HEIGHT

    def _find_kicker(self, side: int, extent_y: float):
        cands = [p for p in self.persons.values()
                 if p.facing == side
                 and abs(self.y - (p.ground_y + 1.25)) < extent_y + 0.8]
        if not cands:
            return None
        return min(cands, key=lambda p: abs(self.y - (p.ground_y + 1.25)))

    def _apply_kick(self, p: Person) -> list[str]:
        rng = self.rng
        p.kick_timer = KICK_DURATION
        p.kick_lift = rng.choice((-1, 1))
        self.vx = p.facing * abs(self.vx)
        self.vy += p.kick_lift * rng.uniform(6.0, 9.5)
        self.av += rng.uniform(-230.0, 230.0) + p.kick_lift * 75.0
        events = ["UP KICK" if p.kick_lift > 0 else "DOWN KICK"]
        self._count_kick(p, p.kick_lift > 0, manual=False)
        lead = self._lead_change(p)
        if lead:
            events.append(lead)

        if self.boost_hits > 0:
            self.boost_hits -= 1
            if self.boost_hits == 0:
                self.mult = 1.0
                events.append("SPEED NORMAL")
        if rng.random() < 0.15:
            if self.boost_hits == 0:
                self.mult = 2.0
                events.append("SPEED x2")
            else:
                events.append("SPEED x2 REFRESH")
            self.boost_hits = 5
            self._emit("boost")
        if self.spawn_extras and rng.random() < 0.05:
            self._add_extra()
            self.last_new_time = self.time_s
            events.append("NEW PLAYER")
            self._emit("newp")
        return events

    def _manual_kick(self, p: Person, claim=None) -> None:
        if p.kick_cd > 0:                       # защита от спама пинками
            return
        cy = p.ground_y + 1.25
        reach = MANUAL_KICK_RANGE + HALF_HEIGHT
        ox, oy = self.x, self.y
        if math.hypot(self.x - p.x, self.y - cy) > reach:
            # Объект на сервере уже далеко. Но игрок мог видеть его рядом (задержка) —
            # принимаем пинок, если заявленная позиция правдоподобна.
            if claim is None:
                return
            cx, cyy = claim
            if math.hypot(cx - self.x, cyy - self.y) > LAG_TOLERANCE:
                return
            if math.hypot(cx - p.x, cyy - cy) > reach + 1.0:
                return
            ox, oy = cx, cyy
        rng = self.rng
        dx = ox - p.x
        side = 1 if dx > 0 else -1
        p.facing = side
        p.kick_timer = KICK_DURATION
        p.kick_cd = MANUAL_KICK_COOLDOWN
        p.kick_lift = rng.choice((-1, 1))
        self.vx = side * max(abs(self.vx), 10.0)
        self.vy += p.kick_lift * rng.uniform(6.0, 9.5)
        self.av += rng.uniform(-230.0, 230.0) + p.kick_lift * 75.0
        self._count_kick(p, p.kick_lift > 0, manual=True)
        text = "MANUAL " + ("UP KICK" if p.kick_lift > 0 else "DOWN KICK")
        lead = self._lead_change(p)
        self._set_label(text + ("   " + lead if lead else ""))

    def _move_human(self, p: Person, dt: float) -> None:
        if p.mx:
            p.x += p.mx * PLAYER_CONTROL_SPEED * dt
            p.facing = 1 if p.mx > 0 else -1
        if p.my:
            p.ground_y += p.my * PLAYER_CONTROL_SPEED * dt
        p.x = clamp(p.x, X_MIN + 1.2, PERSON_X_LIMIT)
        p.ground_y = clamp(p.ground_y, GROUND_MIN, GROUND_MAX)

    def _walk_npc(self, p: Person, dt: float) -> None:
        p.ground_y += p.walk_dir * p.walk_speed * dt
        if p.ground_y < GROUND_MIN:
            p.ground_y, p.walk_dir = GROUND_MIN, 1
        elif p.ground_y > GROUND_MAX:
            p.ground_y, p.walk_dir = GROUND_MAX, -1

    def step(self) -> None:
        """Один тик симуляции (SIM_DT игрового времени)."""
        dt = SIM_DT
        rng = self.rng
        self.time_s += dt

        if self.winner is not None:
            self.round_timer -= TICK
            if self.round_timer <= 0:
                self.reset_round()

        for p in list(self.persons.values()):
            p.kick_timer = max(0.0, p.kick_timer - dt)
            p.kick_cd = max(0.0, p.kick_cd - dt)
            if p.kind == "human":
                self._move_human(p, dt)
            elif p.kind == "npc" and p.walk_speed > 0:
                self._walk_npc(p, dt)

        for pid, claim in self._kick_queue:
            p = self.persons.get(pid)
            if p:
                self._manual_kick(p, claim)
        self._kick_queue.clear()

        if (self.spawn_extras and
                (self.time_s - self.last_new_time) / SPEED_SCALE >= NEW_PLAYER_FORCED_INTERVAL):
            self._add_extra()
            self.last_new_time = self.time_s
            self._set_label("NEW PLAYER (60s)")
            self._emit("newp")

        wind_x = 1.8 * math.sin(self.time_s * 0.72) + rng.uniform(-0.7, 0.7)
        wind_y = 1.2 * math.sin(self.time_s * 1.13) + rng.uniform(-0.8, 0.8)
        self.vx += (wind_x - 0.045 * self.vx) * dt
        self.vy += (-2.0 + wind_y - 0.035 * self.vy) * dt
        self.av += rng.uniform(-28.0, 28.0) * dt
        self.av *= 0.998

        self.x += self.vx * self.mult * dt
        self.y += self.vy * self.mult * dt
        self.angle = (self.angle + self.av * dt) % 360.0
        ex, ey = self._extents()

        events: list[str] = []
        if self.vx < 0 and self.x - ex <= -WALL_X:
            kicker = self._find_kicker(1, ey)
            self.x = -WALL_X + ex
            if kicker:
                events = self._apply_kick(kicker)
            else:
                self.vx = abs(self.vx)
        elif self.vx > 0 and self.x + ex >= WALL_X:
            kicker = self._find_kicker(-1, ey)
            self.x = WALL_X - ex
            if kicker:
                events = self._apply_kick(kicker)
            else:
                self.vx = -abs(self.vx)

        if self.y - ey < Y_MIN:
            self.y = Y_MIN + ey
            self.vy = abs(self.vy) * 0.96
            self.av += rng.uniform(-65.0, 65.0)
        elif self.y + ey > Y_MAX:
            self.y = Y_MAX - ey
            self.vy = -abs(self.vy) * 0.96
            self.av += rng.uniform(-65.0, 65.0)

        if events:
            self._set_label("   ".join(events))
        elif self.label_timer > 0:
            self.label_timer -= dt
            if self.label_timer <= 0:
                self.label = ""

    # -------------------------------------------------------- снимок
    def snapshot(self) -> dict:
        """Компактное состояние для клиентов (без имён и цветов — они в roster()).
        Очищает очередь событий."""
        ev, self.events = self.events, []
        return {
            "t": "s",
            "rv": self.roster_version,
            "o": [round(self.x, 3), round(self.y, 3), round(self.angle, 2),
                  round(self.vx, 3), round(self.vy, 3), round(self.av, 2), self.mult],
            # [pid, x, ground_y, facing, kick_timer, kick_lift, kicks]
            "p": [[p.pid, round(p.x, 3), round(p.ground_y, 3), p.facing,
                   round(p.kick_timer, 3), p.kick_lift, p.kicks]
                  for p in self.persons.values()],
            "st": [self.stats["total"], self.stats["up"],
                   self.stats["down"], self.stats["manual"]],
            "lbl": self.label,
            # [pid победителя (0 — нет), секунд до нового раунда, цель, имя победителя]
            "w": [self.winner or 0, round(max(0.0, self.round_timer), 1),
                  WIN_KICKS, self.winner_name],
            "ev": ev,
        }
