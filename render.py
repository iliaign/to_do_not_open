"""
render.py — всё, что рисуется в игре, и интерполяция снимков сервера.

Кадр (frame) — то, что получается из Interpolator.frame():
    {"o": (x, y, угол), "p": [{pid,x,gy,facing,kt,kl,kicks}, ...],
     "st": [...], "lbl": str, "w": [pid, сек, цель, имя], "latest": {pid: (x, gy)}}
Статичные данные игроков (цвет, тип, имя) берутся из roster: pid -> (color, kind, name).
"""

from __future__ import annotations

import functools
import math

import pygame

from core import (KICK_DURATION, OBJECT_SCALE, SPEED_SCALE, WALL_X, WIN_KICKS, X_MIN,
                  Y_MAX, Y_MIN, clamp)

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
C_OK = (74, 222, 128)
C_ERR = (252, 165, 165)

INTERP_DELAY = 0.1       # чужие игроки и объект рисуются на 100 мс «в прошлом» — движение плавное


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
#  ИНТЕРПОЛЯЦИЯ СНИМКОВ
# =====================================================================
class Interpolator:
    """Хранит последние снимки сервера и отдаёт кадр для момента now - delay."""

    def __init__(self, delay: float = INTERP_DELAY, keep: int = 16):
        self.delay = delay
        self.keep = keep
        self.buf: list = []          # [(время приёма, снимок, {pid: запись})]

    def push(self, snap: dict, t: float) -> None:
        pmap = {e[0]: e for e in snap["p"]}
        self.buf.append((t, snap, pmap))
        if len(self.buf) > self.keep:
            del self.buf[0]

    def frame(self, now: float):
        buf = self.buf
        if not buf:
            return None
        rt = now - self.delay
        newest = buf[-1]

        if rt <= buf[0][0]:
            lo = hi = buf[0]
            f = 0.0
        elif rt >= newest[0]:
            lo = hi = newest
            f = 0.0
        else:
            lo, hi = buf[-2], buf[-1]
            for i in range(len(buf) - 1):
                if buf[i][0] <= rt <= buf[i + 1][0]:
                    lo, hi = buf[i], buf[i + 1]
                    break
            span = hi[0] - lo[0]
            f = (rt - lo[0]) / span if span > 1e-6 else 1.0

        # --- объект
        if lo is hi:                 # снимков «после» нет — экстраполируем по скорости
            ox, oy, ang, vx, vy, av, mult = hi[1]["o"]
            el = min(max(0.0, rt - hi[0]), 0.15)
            ox = clamp(ox + vx * mult * SPEED_SCALE * el, -WALL_X - 3, WALL_X + 3)
            oy = clamp(oy + vy * mult * SPEED_SCALE * el, Y_MIN, Y_MAX)
            ang = (ang + av * SPEED_SCALE * el) % 360.0
        else:
            a, b = lo[1]["o"], hi[1]["o"]
            ox = a[0] + (b[0] - a[0]) * f
            oy = a[1] + (b[1] - a[1]) * f
            ang = (a[2] + (((b[2] - a[2] + 180.0) % 360.0) - 180.0) * f) % 360.0

        # --- люди
        pick = lo if f < 0.5 else hi          # для дискретных полей (поворот, анимация пинка)
        persons = []
        for e in hi[1]["p"]:
            pid, x1, y1 = e[0], e[1], e[2]
            e0 = lo[2].get(pid)
            if e0 is not None and lo is not hi:
                x = e0[1] + (x1 - e0[1]) * f
                gy = e0[2] + (y1 - e0[2]) * f
            else:
                x, gy = x1, y1
            src = pick[2].get(pid) or e
            elapsed = max(0.0, rt - pick[0])
            persons.append({
                "pid": pid, "x": x, "gy": gy,
                "facing": src[3],
                "kt": max(0.0, src[4] - elapsed * SPEED_SCALE),
                "kl": src[5],
                "kicks": newest[2].get(pid, e)[6],
            })

        s = newest[1]
        return {
            "o": (ox, oy, ang),
            "p": persons,
            "st": s["st"],
            "lbl": s["lbl"],
            "w": s.get("w") or [0, 0.0, WIN_KICKS, ""],
            "latest": {pid: (e[1], e[2]) for pid, e in newest[2].items()},
        }


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


# =====================================================================
#  ПАНЕЛИ
# =====================================================================
def addr_box_rect() -> pygame.Rect:
    """Рамка с адресом сервера в боковой панели (она же кнопка «копировать»)."""
    top = int(SIDE_RECT.top + (1 - 0.24) * SIDE_RECT.height)
    return pygame.Rect(SIDE_RECT.left + 14, top, SIDE_RECT.width - 28, 92)


def draw_sidebar(screen, frame, roster, my_id, mode_lines, server_addr=None,
                 copy_state=None) -> None:
    """copy_state: None | "busy" | "ok" | "fail" — подпись в рамке адреса."""
    pygame.draw.rect(screen, C_PANEL, SIDE_RECT)
    cx = SIDE_RECT.centerx

    def sy(f):
        return SIDE_RECT.top + (1 - f) * SIDE_RECT.height

    draw_text(screen, "СПРАВКА", 26, C_TEXT, (cx, sy(0.97)), "midtop", True)

    # --- общий счётчик
    draw_text(screen, "— СЧЁТЧИК КИКОВ —", 18, C_GOLD, (cx, sy(0.91)), "midtop", True)
    total, up, down, manual = frame["st"] if frame else (0, 0, 0, 0)
    draw_text(screen, str(total), 36, C_TEXT, (cx, sy(0.87)), "midtop", True)
    for i, line in enumerate((f"UP     {up:>4}", f"DOWN   {down:>4}",
                              f"MANUAL {manual:>4}")):
        draw_text(screen, line, 16, (203, 213, 225), (cx, sy(0.79) + i * 23), "midtop")

    # --- таблица лидеров
    target = frame["w"][2] if frame else WIN_KICKS
    draw_text(screen, f"— ТАБЛИЦА (до {target}) —", 18, C_GOLD, (cx, sy(0.62)), "midtop", True)
    rows, npc = [], 0
    if frame:
        for pr in frame["p"]:
            info = roster.get(pr["pid"])
            if info is None or info[1] == "npc":
                npc += 1
            else:
                rows.append((pr["kicks"], pr["pid"], info[0], info[2]))
    rows.sort(key=lambda r: (-r[0], r[1]))
    y = sy(0.56)
    for rank, (kicks, pid, color, name) in enumerate(rows, 1):
        me = pid == my_id
        leader = rank == 1 and kicks > 0
        row = pygame.Rect(SIDE_RECT.left + 14, int(y) - 14, SIDE_RECT.width - 28, 28)
        if leader:
            pygame.draw.rect(screen, (66, 52, 16), row, border_radius=5)
        elif me:
            pygame.draw.rect(screen, (30, 41, 59), row, border_radius=5)
        draw_text(screen, f"{rank}.", 16, C_DIM, (row.left + 8, y), "midleft")
        sw = pygame.Rect(row.left + 38, int(y) - 8, 16, 16)
        pygame.draw.rect(screen, pygame.Color(color), sw)
        pygame.draw.rect(screen, C_BG, sw, 1)
        draw_text(screen, name + (" (ты)" if me else ""), 16,
                  C_YELLOW if me else (226, 232, 240), (sw.right + 8, y), "midleft")
        draw_text(screen, str(kicks), 18, C_GOLD if leader else C_TEXT,
                  (row.right - 10, y), "midright", True)
        y += 34
    if npc:
        draw_text(screen, f"+ NPC: {npc}", 14, C_DIM, (SIDE_RECT.left + 20, y), "midleft")

    # --- адрес сервера (когда мы хост); клик по рамке — копировать
    if server_addr:
        box = addr_box_rect()
        hover = box.collidepoint(pygame.mouse.get_pos())
        border = {"ok": C_OK, "fail": C_ERR}.get(copy_state, C_CYAN)
        pygame.draw.rect(screen, (22, 33, 56) if hover else (15, 23, 42), box,
                         border_radius=8)
        pygame.draw.rect(screen, border, box, 2, border_radius=8)
        draw_text(screen, "АДРЕС СЕРВЕРА", 15, C_DIM, (cx, box.top + 8), "midtop", True)
        draw_text(screen, server_addr, 22, C_CYAN, (cx, box.top + 32), "midtop", True)
        if copy_state == "ok":
            draw_text(screen, "СКОПИРОВАНО В БУФЕР!", 14, C_OK, (cx, box.top + 66),
                      "midtop", True)
        elif copy_state == "fail":
            draw_text(screen, "не удалось скопировать", 13, C_ERR, (cx, box.top + 66),
                      "midtop")
        elif copy_state == "busy":
            draw_text(screen, "копирую...", 13, C_DIM, (cx, box.top + 66), "midtop")
        else:
            draw_text(screen, "нажми сюда или C — скопировать", 12, C_DIM,
                      (cx, box.top + 66), "midtop")

    for i, line in enumerate(reversed(mode_lines)):
        draw_text(screen, line, 14, (100, 116, 139),
                  (cx, SIDE_RECT.bottom - 10 - i * 18), "midbottom")


def draw_banner(screen, frame) -> None:
    """Плашка победителя раунда поверх игрового поля."""
    w = frame.get("w") or [0, 0.0, WIN_KICKS, ""]
    if not w[0]:
        return
    shade = pygame.Surface(GAME_RECT.size, pygame.SRCALPHA)
    shade.fill((0, 0, 0, 150))
    screen.blit(shade, GAME_RECT.topleft)
    cx, cy = GAME_RECT.center
    draw_text(screen, "ПОБЕДИТЕЛЬ РАУНДА", 30, C_GOLD, (cx, cy - 50), "center", True)
    draw_text(screen, str(w[3]), 52, C_TEXT, (cx, cy + 10), "center", True)
    left = max(0, int(math.ceil(w[1])))
    draw_text(screen, f"новый раунд через {left} с", 20, C_DIM, (cx, cy + 70), "center")
