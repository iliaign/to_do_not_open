"""
Обучение в МФТИ — упоротая аркада с пинками (pygame + клиент/сервер).

Запуск:  python main.py        (нужен pygame >= 2.0:  pip install pygame)

Режимы:
  * ИГРА С БОТОМ         — локально, против стоячего бота
  * ОНЛАЙН / СОЗДАТЬ     — вы хост (сервер внутри процесса) + играете сами
  * ОНЛАЙН / ПОДКЛЮЧИТЬСЯ — вводите IP хоста из локальной сети

Управление: WASD / стрелки — двигаться, ПРОБЕЛ — пинок, ESC — в меню.
"""

import functools
import math
import random
from array import array

import pygame

from core import (GROUND_MAX, GROUND_MIN, KICK_DURATION, OBJECT_SCALE, PERSON_X_LIMIT,
                  PLAYER_CONTROL_SPEED, SPEED_SCALE, WALL_X, X_MAX, X_MIN,
                  Y_MAX, Y_MIN, clamp)
from net import PORT, GameServer, LocalSession, NetSession, local_ip

# =====================================================================
#  ОБЩЕЕ
# =====================================================================
WIN_W, WIN_H = 1180, 720
SCALE = 34                                   # пикселей на единицу мира
GAME_RECT = pygame.Rect(10, 20, 816, 680)    # 24x20 ед. * 34
SIDE_RECT = pygame.Rect(836, 20, 334, 680)

C_BG = (11, 16, 32)
C_PANEL = (17, 24, 39)
C_TEXT = (248, 250, 252)
C_DIM = (148, 163, 184)
C_GOLD = (251, 191, 36)
C_CYAN = (34, 211, 238)
C_YELLOW = (250, 204, 21)

QUIT = "__quit__"


@functools.lru_cache(maxsize=None)
def font(size: int, bold: bool = False) -> pygame.font.Font:
    return pygame.font.SysFont(
        "consolas,dejavusansmono,liberationmono,couriernew,menlo,monospace",
        size, bold=bold)


def draw_text(surf, text, size, color, pos, anchor="center", bold=False):
    img = font(size, bold).render(text, True, color)
    rect = img.get_rect()
    setattr(rect, anchor, (int(pos[0]), int(pos[1])))
    surf.blit(img, rect)
    return rect


def to_screen(x: float, y: float) -> tuple[int, int]:
    return (int(round(GAME_RECT.left + (x - X_MIN) * SCALE)),
            int(round(GAME_RECT.top + (Y_MAX - y) * SCALE)))


# =====================================================================
#  ЗВУКИ (чистый Python, без numpy)
# =====================================================================
class Sfx:
    def __init__(self):
        self.ok = False
        self.kick_up, self.kick_down = [], []
        self.boost = self.newp = None
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(44100, -16, 1, 512)
            self.rate, _size, self.channels = pygame.mixer.get_init()
            for _ in range(3):
                self.kick_up.append(self._make_kick(True))
                self.kick_down.append(self._make_kick(False))
            self.boost = self._make_boost()
            self.newp = self._make_newp()
            self.ok = True
        except Exception:
            self.ok = False

    # --- синтез
    def _tone(self, f0, f1, dur, vol=0.35, wave="sine"):
        n = int(self.rate * dur)
        ratio = f1 / f0
        phase = 0.0
        out = []
        for i in range(n):
            t = i / self.rate
            phase += 2 * math.pi * f0 * ratio ** (t / dur) / self.rate
            if wave == "square":
                s = 1.0 if math.sin(phase) >= 0 else -1.0
            elif wave == "saw":
                s = 2 * ((phase / (2 * math.pi)) % 1.0) - 1
            else:
                s = math.sin(phase)
            out.append(s * min(1.0, t / 0.01) * math.exp(-3.0 * t) * vol)
        return out

    def _noise(self, dur=0.08, vol=0.4):
        n = int(self.rate * dur)
        return [random.uniform(-1, 1) * math.exp(-18.0 * i / self.rate) * vol
                for i in range(n)]

    def _pack(self, samples):
        data = array("h")
        for s in samples:
            v = int(clamp(s, -1.0, 1.0) * 32767)
            for _ in range(self.channels):
                data.append(v)
        return pygame.mixer.Sound(buffer=data.tobytes())

    def _make_kick(self, up: bool):
        j = random.uniform(0.85, 1.25)
        hit = self._noise(0.06, 0.35)
        if up:
            a = self._tone(220 * j, 880 * j, 0.18, 0.30, "square")
            b = self._tone(300 * j, 1200 * j, 0.22, 0.25, "sine")
        else:
            a = self._tone(700 * j, 90 * j, 0.28, 0.30, "saw")
            b = self._tone(160 * j, 55 * j, 0.18, 0.25, "sine")
        return self._pack(hit + a + b)

    def _make_boost(self):
        base = 260 * random.uniform(0.9, 1.15)
        notes = []
        for m in (1.0, 1.26, 1.5, 2.0):
            notes += self._tone(base * m, base * m * 1.01, 0.07, 0.22, "square")
        return self._pack(notes)

    def _make_newp(self):
        return self._pack(self._tone(500, 1500, 0.08, 0.25, "sine")
                          + self._tone(1400, 2200, 0.10, 0.20, "square"))

    # --- воспроизведение
    def handle(self, ev) -> None:
        if not self.ok or not ev:
            return
        kind = ev[0]
        if kind == "kick":
            random.choice(self.kick_up if ev[1] else self.kick_down).play()
        elif kind == "boost":
            self.boost.play()
        elif kind == "newp":
            self.newp.play()


# =====================================================================
#  ОТРИСОВКА СЦЕНЫ
# =====================================================================
def build_object_surface() -> pygame.Surface:
    """«Объект» нарисован один раз; начало координат объекта = центр поверхности."""
    S = SCALE * OBJECT_SCALE
    R = 6.4
    size = int(2 * R * S) + 4
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    cx = cy = size // 2
    skin, shadow, outline = (232, 160, 119), (189, 109, 82), (92, 45, 37)
    lw = max(1, round(5 * OBJECT_SCALE))

    def P(x, y):
        return (int(round(cx + x * S)), int(round(cy - y * S)))

    def ellipse(x, y, w, h, fill):
        rect = pygame.Rect(0, 0, int(w * S), int(h * S))
        rect.center = P(x, y)
        pygame.draw.ellipse(surf, outline, rect)
        pygame.draw.ellipse(surf, fill, rect.inflate(-2 * lw, -2 * lw))

    def circle(x, y, r, fill):
        pygame.draw.circle(surf, outline, P(x, y), int(r * S))
        pygame.draw.circle(surf, fill, P(x, y), int(r * S) - lw)

    def shadow_ellipse(x, y, w, h, alpha):
        layer = pygame.Surface((size, size), pygame.SRCALPHA)
        rect = pygame.Rect(0, 0, int(w * S), max(1, int(h * S)))
        rect.center = P(x, y)
        pygame.draw.ellipse(layer, (*shadow, int(alpha * 255)), rect)
        surf.blit(layer, (0, 0))

    ellipse(0, -2.7, 4.9, 2.25, skin)
    circle(-1.58, -3.32, 1.22, skin)
    circle(1.58, -3.32, 1.22, skin)

    body = pygame.Rect(0, 0, int(2.9 * S), int(7.11 * S))
    body.midtop = P(0, 4.23)
    radius = int(1.33 * S)
    pygame.draw.rect(surf, outline, body, border_radius=radius)
    pygame.draw.rect(surf, skin, body.inflate(-2 * lw, -2 * lw),
                     border_radius=radius - lw)

    ellipse(0, 4.18, 2.84, 2.55, skin)
    shadow_ellipse(-0.46, 0.35, 0.34, 5.9, 0.34)
    shadow_ellipse(0, 3.25, 2.28, 0.2, 0.55)
    return surf


def draw_limb(surf, pts, color, width):
    sp = [to_screen(x, y) for x, y in pts]
    pygame.draw.lines(surf, color, False, sp, width)
    for pt in sp:
        pygame.draw.circle(surf, color, pt, width // 2)


def draw_person(surf, pr: dict, t: float) -> None:
    x, y, d = pr["x"], pr["gy"], pr["facing"]
    kt, kl, phase = pr["kt"], pr["kl"], pr["phase"]
    limb = pygame.Color("#dbeafe")
    face = pygame.Color("#f2c49b")
    shirt = pygame.Color(pr["color"])

    bob = 0.06 * math.sin(t * 4.0 + phase)
    step = 0.22 * math.sin(t * 5.4 + phase)
    hip_y = y + 1.82 + bob
    shoulder_y = y + 3.38 + bob
    head_y = y + 4.55 + bob
    arm_raise = 0.42 if kt > 0 else 0.0

    back_arm = [(x - d * 0.48, shoulder_y), (x - d * 0.92, y + 2.52 - step),
                (x - d * 0.76, y + 1.9 - step)]
    front_arm = [(x + d * 0.48, shoulder_y),
                 (x + d * 0.95, y + 2.48 + step + arm_raise),
                 (x + d * 0.82, y + 2.04 + step + arm_raise)]
    back_leg = [(x - d * 0.2, hip_y), (x - d * 0.52, y + 0.9),
                (x - d * (0.68 + step), y + 0.05)]
    if kt > 0:
        prog = math.sin(math.pi * clamp(1.0 - kt / KICK_DURATION, 0.0, 1.0))
        extension = 0.32 + 1.55 * prog
        lift = kl * (0.18 + 1.25 * prog)
    else:
        extension = 0.52 - step
        lift = 0.10 * math.sin(t * 5.4 + phase)
    front_leg = [(x + d * 0.2, hip_y),
                 (x + d * (0.4 + extension * 0.38), y + 0.86 + lift * 0.38),
                 (x + d * (0.56 + extension), y + 0.06 + lift)]

    draw_limb(surf, back_arm, limb, 7)
    draw_limb(surf, back_leg, limb, 10)

    # шея
    neck = pygame.Rect(0, 0, int(0.32 * SCALE), int(0.38 * SCALE))
    neck.midtop = to_screen(x, y + bob + 3.94)
    pygame.draw.rect(surf, face, neck)

    # торс
    torso = pygame.Rect(0, 0, int(1.46 * SCALE), int(2.0 * SCALE))
    torso.midtop = to_screen(x, y + bob + 3.74)
    rad = int(0.28 * SCALE)
    pygame.draw.rect(surf, shirt, torso, border_radius=rad)
    pygame.draw.rect(surf, limb, torso, width=2, border_radius=rad)

    # голова
    hc = to_screen(x, head_y)
    hr = int(0.63 * SCALE)
    pygame.draw.circle(surf, face, hc, hr)
    pygame.draw.circle(surf, limb, hc, hr, 2)

    draw_limb(surf, front_leg, limb, 10)
    draw_limb(surf, front_arm, limb, 7)

    # волосы (верхний сектор 5°..175°)
    cxw, cyw = x, head_y + 0.23
    pts = [to_screen(cxw, cyw)]
    for a in range(5, 176, 10):
        r = math.radians(a)
        pts.append(to_screen(cxw + 0.66 * math.cos(r), cyw + 0.66 * math.sin(r)))
    pygame.draw.polygon(surf, pygame.Color("#312e81"), pts)

    # глаза и рот
    gaze = x + 0.2 * d
    eye_col = pygame.Color("#172033")
    for ex in (gaze - 0.12, gaze + 0.12):
        pygame.draw.circle(surf, eye_col, to_screen(ex, head_y + 0.06),
                           max(2, int(0.075 * SCALE)))
    mouth = []
    for a in range(190, 351, 20):
        r = math.radians(a)
        mouth.append(to_screen(gaze + 0.16 * math.cos(r),
                               head_y - 0.21 + 0.09 * math.sin(r)))
    pygame.draw.lines(surf, pygame.Color("#7c2d12"), False, mouth, 2)

    # имя и маркер «это ты»
    if pr["kind"] in ("human", "bot"):
        bx, by = to_screen(x, head_y + 1.0)
        draw_text(surf, pr["name"], 14, pr["tag_color"], (bx, by), "midbottom", True)
        if pr["me"]:
            pygame.draw.polygon(surf, C_YELLOW,
                                [(bx - 6, by - 26), (bx + 6, by - 26), (bx, by - 15)])


def draw_sidebar(screen, base, my_id, mode_lines) -> None:
    pygame.draw.rect(screen, C_PANEL, SIDE_RECT)
    cx = SIDE_RECT.centerx

    def sy(f):
        return SIDE_RECT.top + (1 - f) * SIDE_RECT.height

    draw_text(screen, "СПРАВКА", 26, C_TEXT, (cx, sy(0.97)), "midtop", True)
    draw_text(screen, "— СЧЁТЧИК КИКОВ —", 18, C_GOLD, (cx, sy(0.88)), "midtop", True)
    total, up, down, manual = base["st"] if base else (0, 0, 0, 0)
    draw_text(screen, str(total), 40, C_TEXT, (cx, sy(0.82)), "midtop", True)
    for i, line in enumerate((f"UP     {up:>4}", f"DOWN   {down:>4}",
                              f"MANUAL {manual:>4}")):
        draw_text(screen, line, 17, (203, 213, 225), (cx, sy(0.72) + i * 26), "midtop")

    draw_text(screen, "— УПРАВЛЕНИЕ —", 18, C_GOLD, (cx, sy(0.60)), "midtop", True)
    for i, line in enumerate(("W / ВВЕРХ", "S / ВНИЗ", "A / ВЛЕВО", "D / ВПРАВО",
                              "ПРОБЕЛ  пинок", "ESC     меню")):
        draw_text(screen, line, 17, (226, 232, 240), (cx, sy(0.53) + i * 27), "midtop")

    draw_text(screen, "— ИГРОКИ —", 18, C_GOLD, (cx, sy(0.31)), "midtop", True)
    y = sy(0.25)
    npc = 0
    if base:
        for pid, _x, _y, _f, color, _kt, _kl, kind, name in base["p"]:
            if kind == "npc":
                npc += 1
                continue
            r = pygame.Rect(SIDE_RECT.left + 40, int(y) - 8, 18, 16)
            pygame.draw.rect(screen, pygame.Color(color), r)
            pygame.draw.rect(screen, C_BG, r, 1)
            label = name + (" (ты)" if pid == my_id else "")
            draw_text(screen, label, 16, (226, 232, 240), (r.right + 10, y), "midleft")
            y += 30
        if npc:
            draw_text(screen, f"+ NPC: {npc}", 16, C_DIM,
                      (SIDE_RECT.left + 40, y), "midleft")

    for i, line in enumerate(reversed(mode_lines)):
        draw_text(screen, line, 14, (100, 116, 139),
                  (cx, SIDE_RECT.bottom - 10 - i * 18), "midbottom")


# =====================================================================
#  ИГРОВОЙ ЦИКЛ
# =====================================================================
# Физические коды клавиш (SDL scancode) — работают в любой раскладке (в т.ч. русской).
# Отрицательные значения — запасной вариант по keycode.
KEY_ACTIONS = {
    26: "up", 82: "up", 22: "down", 81: "down",
    4: "left", 80: "left", 7: "right", 79: "right", 44: "kick",
    -pygame.K_w: "up", -pygame.K_UP: "up", -pygame.K_s: "down", -pygame.K_DOWN: "down",
    -pygame.K_a: "left", -pygame.K_LEFT: "left", -pygame.K_d: "right",
    -pygame.K_RIGHT: "right", -pygame.K_SPACE: "kick",
}


def key_id(ev) -> int:
    sc = getattr(ev, "scancode", None)
    if sc in KEY_ACTIONS:
        return sc
    return -ev.key


def play(screen, clock, sfx, session, mode_lines, obj_surf) -> str:
    """Возвращает "" (выход в меню), QUIT или текст уведомления."""
    held: set[int] = set()
    last_input = None
    base = None
    recv_t = 0.0
    anim_t = 0.0
    local = None                      # предсказанная позиция своего игрока
    focus_lost = getattr(pygame, "WINDOWFOCUSLOST", None)

    while True:
        dt = min(clock.tick(60) / 1000.0, 0.1)
        anim_t += dt * SPEED_SCALE
        kick = False

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return QUIT
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return ""
                kid = key_id(ev)
                held.add(kid)
                if KEY_ACTIONS.get(kid) == "kick":
                    kick = True
            elif ev.type == pygame.KEYUP:
                held.discard(key_id(ev))
            elif focus_lost is not None and ev.type == focus_lost:
                held.clear()

        acts = {KEY_ACTIONS[k] for k in held if k in KEY_ACTIONS}
        mx = int("right" in acts) - int("left" in acts)
        my = int("up" in acts) - int("down" in acts)
        if (mx, my) != last_input or kick:
            session.send_input(mx, my, kick)
            last_input = (mx, my)

        if not session.alive:
            return "Соединение с сервером потеряно"

        snap, events = session.poll()
        now = pygame.time.get_ticks() / 1000.0
        if snap is not None:
            base, recv_t = snap, now
        for ev in events:
            sfx.handle(ev)

        # ---------------- рисование
        screen.fill(C_BG)
        pygame.draw.rect(screen, C_PANEL, GAME_RECT)

        if base is None:
            draw_text(screen, "Ожидание данных от сервера...", 22, C_DIM,
                      GAME_RECT.center)
            draw_sidebar(screen, None, session.my_id, mode_lines)
            pygame.display.flip()
            continue

        el = min(now - recv_t, 0.1)
        ox, oy, oang, ovx, ovy, oav, omult = base["o"]
        ox = clamp(ox + ovx * omult * SPEED_SCALE * el, -WALL_X - 3, WALL_X + 3)
        oy = clamp(oy + ovy * omult * SPEED_SCALE * el, Y_MIN, Y_MAX)
        oang = (oang + oav * SPEED_SCALE * el) % 360.0

        screen.set_clip(GAME_RECT)
        rot = pygame.transform.rotozoom(obj_surf, oang, 1.0)
        screen.blit(rot, rot.get_rect(center=to_screen(ox, oy)))

        persons = []
        for pid, x, gy, facing, color, kt, kl, kind, name in base["p"]:
            me = pid == session.my_id
            if me and session.predict:
                if local is None or abs(local[0] - x) + abs(local[1] - gy) > 4.0:
                    local = [x, gy]
                if mx or my:
                    local[0] = clamp(local[0] + mx * PLAYER_CONTROL_SPEED * SPEED_SCALE * dt,
                                     X_MIN + 1.2, PERSON_X_LIMIT)
                    local[1] = clamp(local[1] + my * PLAYER_CONTROL_SPEED * SPEED_SCALE * dt,
                                     GROUND_MIN, GROUND_MAX)
                else:
                    k = min(1.0, dt * 8.0)
                    local[0] += (x - local[0]) * k
                    local[1] += (gy - local[1]) * k
                x, gy = local
                if mx:
                    facing = 1 if mx > 0 else -1
            persons.append({
                "x": x, "gy": gy, "facing": facing, "color": color,
                "kt": max(0.0, kt - el * SPEED_SCALE), "kl": kl, "kind": kind,
                "name": name, "me": me, "phase": pid * 1.7,
                "tag_color": C_YELLOW if me else (203, 213, 225),
            })
        for pr in sorted(persons, key=lambda p: -p["gy"]):
            draw_person(screen, pr, anim_t)
        screen.set_clip(None)

        # HUD
        draw_text(screen, base["lbl"], 28, C_TEXT,
                  (GAME_RECT.centerx, to_screen(0, 12.15)[1]), "center", True)
        draw_text(screen, f"FPS: {clock.get_fps():5.1f}   x{SPEED_SCALE:.1f}", 18, C_CYAN,
                  (GAME_RECT.left + 12, to_screen(0, 12.15)[1]), "midleft", True)
        draw_text(screen, f"PLAYERS: {len(base['p'])}", 18, C_YELLOW,
                  (GAME_RECT.right - 12, to_screen(0, 12.15)[1]), "midright", True)
        draw_text(screen, "WASD / стрелки — двигаться     ПРОБЕЛ — пинок     ESC — меню",
                  15, C_DIM, (GAME_RECT.centerx, to_screen(0, -6.7)[1]), "center")

        draw_sidebar(screen, base, session.my_id, mode_lines)
        pygame.display.flip()


# =====================================================================
#  МЕНЮ
# =====================================================================
class Button:
    def __init__(self, key, text, center, color, hover, w=420, h=60):
        self.key, self.text, self.color, self.hover = key, text, color, hover
        self.rect = pygame.Rect(0, 0, w, h)
        self.rect.center = center

    def draw(self, screen, mouse):
        col = self.hover if self.rect.collidepoint(mouse) else self.color
        pygame.draw.rect(screen, col, self.rect, border_radius=8)
        draw_text(screen, self.text, 22, C_BG, self.rect.center, "center", True)


GREEN, GREEN_H = (34, 197, 94), (22, 163, 74)
BLUE, BLUE_H = (96, 165, 250), (59, 130, 246)
RED, RED_H = (239, 68, 68), (220, 38, 38)


def draw_title(screen, subtitle):
    screen.fill(C_BG)
    draw_text(screen, "ОБУЧЕНИЕ В МФТИ", 54, C_TEXT, (WIN_W // 2, 110), "center", True)
    draw_text(screen, subtitle, 20, C_DIM, (WIN_W // 2, 165), "center")


def choose(screen, clock, subtitle, buttons, notice="", esc_key=None):
    while True:
        mouse = pygame.mouse.get_pos()
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return QUIT
            if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                return esc_key if esc_key else QUIT
            if ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                for b in buttons:
                    if b.rect.collidepoint(ev.pos):
                        return b.key
        draw_title(screen, subtitle)
        for b in buttons:
            b.draw(screen, mouse)
        if notice:
            draw_text(screen, notice, 18, (252, 165, 165), (WIN_W // 2, WIN_H - 60))
        pygame.display.flip()
        clock.tick(60)


def main_menu(screen, clock, notice=""):
    cx = WIN_W // 2
    return choose(screen, clock, "упоротая аркада с пинками", [
        Button("bot", "ИГРА С БОТОМ", (cx, 280), GREEN, GREEN_H),
        Button("online", "ОНЛАЙН (ЛОКАЛЬНАЯ СЕТЬ)", (cx, 360), BLUE, BLUE_H, w=520),
        Button("quit", "ВЫХОД", (cx, 440), RED, RED_H),
    ], notice)


def online_menu(screen, clock):
    cx = WIN_W // 2
    return choose(screen, clock, "онлайн по локальной сети", [
        Button("host", "СОЗДАТЬ ИГРУ (ХОСТ)", (cx, 280), GREEN, GREEN_H, w=520),
        Button("join", "ПОДКЛЮЧИТЬСЯ ПО IP", (cx, 360), BLUE, BLUE_H, w=520),
        Button("back", "НАЗАД", (cx, 440), RED, RED_H),
    ], esc_key="back")


def join_menu(screen, clock, text=""):
    """Ввод IP. Возвращает NetSession, None (назад) или QUIT."""
    cx = WIN_W // 2
    error = ""
    btn_join = Button("join", "ПОДКЛЮЧИТЬСЯ", (cx, 430), GREEN, GREEN_H)
    btn_back = Button("back", "НАЗАД", (cx, 510), RED, RED_H)
    allowed = set("0123456789.:abcdefghijklmnopqrstuvwxyz-")

    def attempt():
        nonlocal error
        draw_title(screen, "подключение по IP")
        draw_text(screen, "Подключаюсь...", 22, C_GOLD, (cx, 340))
        pygame.display.flip()
        try:
            host, port = text.strip(), PORT
            if ":" in host:
                host, p = host.rsplit(":", 1)
                port = int(p)
            if not host:
                raise ValueError("введите адрес хоста")
            return NetSession(host, port)
        except Exception as e:
            error = f"Не удалось подключиться: {e}"
            return None

    pygame.key.start_text_input()
    try:
        while True:
            mouse = pygame.mouse.get_pos()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return QUIT
                if ev.type == pygame.TEXTINPUT:
                    text += "".join(c for c in ev.text.lower() if c in allowed)
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        return None
                    if ev.key == pygame.K_BACKSPACE:
                        text = text[:-1]
                    elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        s = attempt()
                        if s:
                            return s
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if btn_back.rect.collidepoint(ev.pos):
                        return None
                    if btn_join.rect.collidepoint(ev.pos):
                        s = attempt()
                        if s:
                            return s

            draw_title(screen, "подключение по IP")
            draw_text(screen, f"Введите адрес хоста (порт по умолчанию {PORT})", 18,
                      C_DIM, (cx, 270))
            box = pygame.Rect(0, 0, 560, 56)
            box.center = (cx, 330)
            pygame.draw.rect(screen, C_PANEL, box, border_radius=6)
            pygame.draw.rect(screen, (59, 130, 246), box, 2, border_radius=6)
            caret = "|" if (pygame.time.get_ticks() // 500) % 2 == 0 else " "
            draw_text(screen, text + caret, 26, C_TEXT, (box.left + 14, box.centery),
                      "midleft")
            if error:
                draw_text(screen, error, 17, (252, 165, 165), (cx, 385))
            btn_join.draw(screen, mouse)
            btn_back.draw(screen, mouse)
            pygame.display.flip()
            clock.tick(60)
    finally:
        pygame.key.stop_text_input()


# =====================================================================
#  ТОЧКА ВХОДА
# =====================================================================
def main() -> None:
    pygame.mixer.pre_init(44100, -16, 1, 512)
    pygame.init()
    screen = pygame.display.set_mode((WIN_W, WIN_H))
    pygame.display.set_caption("Обучение в МФТИ")
    clock = pygame.time.Clock()
    sfx = Sfx()
    obj_surf = build_object_surface()
    notice = ""

    while True:
        choice = main_menu(screen, clock, notice)
        notice = ""
        if choice in ("quit", QUIT):
            break

        result = ""
        if choice == "bot":
            session = LocalSession()
            result = play(screen, clock, sfx, session,
                          ["режим: игра с ботом"], obj_surf)
            session.close()

        elif choice == "online":
            sub = online_menu(screen, clock)
            if sub == QUIT:
                break
            if sub == "host":
                server = GameServer(PORT)
                try:
                    server.start()
                    session = NetSession("127.0.0.1", PORT)
                except Exception as e:
                    server.stop()
                    notice = f"Не удалось создать игру: {e}"
                    continue
                lines = ["режим: онлайн (хост)", f"адрес: {local_ip()}:{PORT}"]
                result = play(screen, clock, sfx, session, lines, obj_surf)
                session.close()
                server.stop()
            elif sub == "join":
                session = join_menu(screen, clock)
                if session == QUIT:
                    break
                if session is not None:
                    result = play(screen, clock, sfx, session,
                                  ["режим: онлайн (клиент)"], obj_surf)
                    session.close()

        if result == QUIT:
            break
        notice = result

    pygame.quit()


if __name__ == "__main__":
    main()
