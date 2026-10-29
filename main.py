"""
Обучение в МФТИ — упоротая аркада с пинками (pygame + клиент/сервер).

Запуск:  python main.py        (нужен pygame >= 2.0:  pip install pygame)
Необязательно: pip install psutil — тогда в меню видны имена сетевых адаптеров.

Режимы:
  * ИГРА С БОТОМ         — локально, против стоячего бота
  * ОНЛАЙН / СОЗДАТЬ     — выбираете сеть (Radmin VPN, Wi-Fi, ...), вы хост
                           (сервер внутри процесса) + играете сами;
                           адрес копируется в буфер обмена автоматически
  * ОНЛАЙН / ПОДКЛЮЧИТЬСЯ — вводите (или вставляете Ctrl+V) адрес хоста

Управление: WASD / стрелки — двигаться, ПРОБЕЛ — пинок, ESC — в меню,
C (или клик по адресу) — скопировать адрес сервера (у хоста).
"""

import functools
import math
import random
import re
import subprocess
import sys
from array import array

import pygame

from core import (GROUND_MAX, GROUND_MIN, KICK_DURATION, OBJECT_SCALE, PERSON_X_LIMIT,
                  PLAYER_CONTROL_SPEED, SPEED_SCALE, WALL_X, X_MAX, X_MIN,
                  Y_MAX, Y_MIN, clamp)
from net import PORT, GameServer, HostTarget, LocalSession, NetSession, host_targets

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
#  БУФЕР ОБМЕНА
# =====================================================================
def _run(cmd, data=None, want_output=False, timeout=3.0):
    flags = 0x08000000 if sys.platform == "win32" else 0     # CREATE_NO_WINDOW
    return subprocess.run(
        cmd, input=data, timeout=timeout, creationflags=flags,
        stdout=subprocess.PIPE if want_output else subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def _clip_cmds(write: bool):
    if sys.platform == "win32":
        return [["clip"]] if write else [["powershell", "-NoProfile", "-Command",
                                          "Get-Clipboard"]]
    if sys.platform == "darwin":
        return [["pbcopy"]] if write else [["pbpaste"]]
    if write:
        return [["wl-copy"], ["xclip", "-selection", "clipboard"],
                ["xsel", "--clipboard", "--input"]]
    return [["wl-paste", "-n"], ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"]]


def copy_text(text: str) -> bool:
    """Кладёт текст в буфер обмена. True — получилось."""
    try:                                              # 1) pygame (SDL)
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        pygame.scrap.put_text(text)
        return True
    except Exception:
        pass
    for cmd in _clip_cmds(write=True):                # 2) системная утилита
        try:
            if _run(cmd, data=text.encode("utf-8")).returncode == 0:
                return True
        except Exception:
            continue
    try:                                              # 3) tkinter
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
        return True
    except Exception:
        return False


def paste_text() -> str:
    """Первая строка из буфера обмена ('' если пусто или недоступно)."""
    raw = ""
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        raw = pygame.scrap.get_text()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "ignore")
    except Exception:
        raw = ""
    if not raw:
        for cmd in _clip_cmds(write=False):
            try:
                res = _run(cmd, want_output=True)
                if res.returncode == 0 and res.stdout:
                    raw = res.stdout.decode("utf-8", "ignore")
                    break
            except Exception:
                continue
    if not raw:
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            raw = root.clipboard_get()
            root.destroy()
        except Exception:
            raw = ""
    lines = raw.replace("\x00", "").strip().splitlines()
    return lines[0].strip() if lines else ""


ADDR_ALLOWED = set("0123456789.:abcdefghijklmnopqrstuvwxyz-")
_ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?$")


def clean_address(raw: str) -> str:
    return "".join(c for c in raw.lower() if c in ADDR_ALLOWED)


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


def addr_box_rect() -> pygame.Rect:
    """Рамка с адресом сервера в боковой панели (она же кнопка «копировать»)."""
    top = int(SIDE_RECT.top + (1 - 0.24) * SIDE_RECT.height)
    return pygame.Rect(SIDE_RECT.left + 14, top, SIDE_RECT.width - 28, 92)


def draw_sidebar(screen, base, my_id, mode_lines, server_addr=None, copy_state=None) -> None:
    pygame.draw.rect(screen, C_PANEL, SIDE_RECT)
    cx = SIDE_RECT.centerx

    def sy(f):
        return SIDE_RECT.top + (1 - f) * SIDE_RECT.height

    draw_text(screen, "СПРАВКА", 26, C_TEXT, (cx, sy(0.97)), "midtop", True)

    # --- общий счётчик
    draw_text(screen, "— СЧЁТЧИК КИКОВ —", 18, C_GOLD, (cx, sy(0.91)), "midtop", True)
    total, up, down, manual = base["st"] if base else (0, 0, 0, 0)
    draw_text(screen, str(total), 36, C_TEXT, (cx, sy(0.87)), "midtop", True)
    for i, line in enumerate((f"UP     {up:>4}", f"DOWN   {down:>4}",
                              f"MANUAL {manual:>4}")):
        draw_text(screen, line, 16, (203, 213, 225), (cx, sy(0.79) + i * 23), "midtop")

    # --- таблица лидеров
    draw_text(screen, "— ТАБЛИЦА —", 18, C_GOLD, (cx, sy(0.62)), "midtop", True)
    rows, npc = [], 0
    if base:
        for pid, _x, _y, _f, color, _kt, _kl, kind, name, kicks in base["p"]:
            if kind == "npc":
                npc += 1
            else:
                rows.append((kicks, pid, color, name))
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
        else:
            draw_text(screen, "нажми сюда или C — скопировать", 12, C_DIM,
                      (cx, box.top + 66), "midtop")

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
SC_C, SC_V = 6, 25                    # физические коды клавиш C и V


def key_id(ev) -> int:
    sc = getattr(ev, "scancode", None)
    if sc in KEY_ACTIONS:
        return sc
    return -ev.key


def play(screen, clock, sfx, session, mode_lines, obj_surf, server_addr=None) -> str:
    """Возвращает "" (выход в меню), QUIT или текст уведомления."""
    held: set[int] = set()
    last_input = None
    base = None
    recv_t = 0.0
    anim_t = 0.0
    local = None                      # предсказанная позиция своего игрока
    focus_lost = getattr(pygame, "WINDOWFOCUSLOST", None)

    copy_state = None                 # None | "ok" | "fail" — подпись в рамке адреса
    copy_until = 0.0

    def do_copy():
        nonlocal copy_state, copy_until
        copy_state = "ok" if copy_text(server_addr) else "fail"
        copy_until = pygame.time.get_ticks() / 1000.0 + 3.0

    if server_addr:
        do_copy()                     # хост: адрес сразу попадает в буфер обмена

    while True:
        dt = min(clock.tick(60) / 1000.0, 0.1)
        anim_t += dt * SPEED_SCALE
        kick = False

        if copy_state and pygame.time.get_ticks() / 1000.0 > copy_until:
            copy_state = None

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return QUIT
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return ""
                if server_addr and (ev.key == pygame.K_c
                                    or getattr(ev, "scancode", None) == SC_C):
                    do_copy()
                    continue
                kid = key_id(ev)
                held.add(kid)
                if KEY_ACTIONS.get(kid) == "kick":
                    kick = True
            elif ev.type == pygame.KEYUP:
                held.discard(key_id(ev))
            elif (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1
                  and server_addr and addr_box_rect().collidepoint(ev.pos)):
                do_copy()
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
            draw_sidebar(screen, None, session.my_id, mode_lines, server_addr, copy_state)
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
        for pid, x, gy, facing, color, kt, kl, kind, name, _kicks in base["p"]:
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

        draw_sidebar(screen, base, session.my_id, mode_lines, server_addr, copy_state)
        pygame.display.flip()


# =====================================================================
#  МЕНЮ
# =====================================================================
class Button:
    def __init__(self, key, text, center, color, hover, w=420, h=60, sub=""):
        self.key, self.text, self.color, self.hover = key, text, color, hover
        self.sub = sub
        self.rect = pygame.Rect(0, 0, w, h)
        self.rect.center = center

    def draw(self, screen, mouse):
        col = self.hover if self.rect.collidepoint(mouse) else self.color
        pygame.draw.rect(screen, col, self.rect, border_radius=8)
        if self.sub:
            sub = self.sub if len(self.sub) <= 72 else self.sub[:69] + "..."
            draw_text(screen, self.text, 20, C_BG,
                      (self.rect.centerx, self.rect.top + 17), "center", True)
            draw_text(screen, sub, 13, (30, 41, 59),
                      (self.rect.centerx, self.rect.top + 38), "center")
        else:
            draw_text(screen, self.text, 22, C_BG, self.rect.center, "center", True)


GREEN, GREEN_H = (34, 197, 94), (22, 163, 74)
BLUE, BLUE_H = (96, 165, 250), (59, 130, 246)
RED, RED_H = (239, 68, 68), (220, 38, 38)
AMBER, AMBER_H = (251, 191, 36), (245, 158, 11)
GRAY, GRAY_H = (203, 213, 225), (148, 163, 184)


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
            draw_text(screen, notice, 18, C_ERR, (WIN_W // 2, WIN_H - 60))
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


def network_menu(screen, clock):
    """Выбор сети, в которой хост создаёт игру.

    Возвращает HostTarget, None (назад) или QUIT.
    """
    cx = WIN_W // 2
    while True:
        targets = host_targets()
        real = len(targets) - 2                    # без «Все сети» и «Только этот компьютер»
        buttons = []
        y = 225
        for i, t in enumerate(targets):
            if t.bind == "0.0.0.0":
                col, hov = AMBER, AMBER_H
            elif t.bind == "127.0.0.1":
                col, hov = GRAY, GRAY_H
            elif i == 0:
                col, hov = GREEN, GREEN_H
            else:
                col, hov = BLUE, BLUE_H
            buttons.append(Button(i, t.title, (cx, y), col, hov, w=660, h=50, sub=t.desc))
            y += 58
        buttons.append(Button("refresh", "ОБНОВИТЬ", (cx - 165, 655), GRAY, GRAY_H,
                              w=320, h=50))
        buttons.append(Button("back", "НАЗАД", (cx + 165, 655), RED, RED_H, w=320, h=50))

        subtitle = ("выберите сеть, в которой будете играть" if real > 0 else
                    "сети не найдены — подключитесь к Wi-Fi / Radmin и нажмите ОБНОВИТЬ")
        res = choose(screen, clock, subtitle, buttons, esc_key="back")
        if res == QUIT:
            return QUIT
        if res == "back":
            return None
        if res == "refresh":
            continue
        return targets[res]


def join_menu(screen, clock, text=""):
    """Ввод IP. Возвращает NetSession, None (назад) или QUIT."""
    cx = WIN_W // 2
    error = ""
    btn_join = Button("join", "ПОДКЛЮЧИТЬСЯ", (cx, 430), GREEN, GREEN_H)
    btn_back = Button("back", "НАЗАД", (cx, 510), RED, RED_H)
    btn_paste = Button("paste", "ВСТАВИТЬ", (cx + 380, 330), BLUE, BLUE_H, w=170, h=56)

    # если в буфере уже лежит адрес (хост прислал) — подставляем сразу
    if not text:
        clip = paste_text()
        if _ADDR_RE.match(clip):
            text = clip

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

    def paste():
        nonlocal text, error
        clip = clean_address(paste_text())
        if clip:
            text, error = clip, ""
        else:
            error = "В буфере обмена нет адреса"

    pygame.key.start_text_input()
    try:
        while True:
            mouse = pygame.mouse.get_pos()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return QUIT
                if ev.type == pygame.TEXTINPUT:
                    text += clean_address(ev.text)
                elif ev.type == pygame.KEYDOWN:
                    ctrl = ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)
                    if ev.key == pygame.K_ESCAPE:
                        return None
                    if ctrl and (ev.key == pygame.K_v
                                 or getattr(ev, "scancode", None) == SC_V):
                        paste()
                    elif ev.key == pygame.K_BACKSPACE:
                        text = text[:-1]
                    elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        s = attempt()
                        if s:
                            return s
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if btn_back.rect.collidepoint(ev.pos):
                        return None
                    if btn_paste.rect.collidepoint(ev.pos):
                        paste()
                    elif btn_join.rect.collidepoint(ev.pos):
                        s = attempt()
                        if s:
                            return s

            draw_title(screen, "подключение по IP")
            draw_text(screen, f"Введите адрес хоста или вставьте Ctrl+V "
                              f"(порт по умолчанию {PORT})", 18, C_DIM, (cx, 270))
            box = pygame.Rect(0, 0, 560, 56)
            box.center = (cx, 330)
            pygame.draw.rect(screen, C_PANEL, box, border_radius=6)
            pygame.draw.rect(screen, (59, 130, 246), box, 2, border_radius=6)
            caret = "|" if (pygame.time.get_ticks() // 500) % 2 == 0 else " "
            draw_text(screen, text + caret, 26, C_TEXT, (box.left + 14, box.centery),
                      "midleft")
            if error:
                draw_text(screen, error, 17, C_ERR, (cx, 385))
            btn_paste.draw(screen, mouse)
            btn_join.draw(screen, mouse)
            btn_back.draw(screen, mouse)
            pygame.display.flip()
            clock.tick(60)
    finally:
        pygame.key.stop_text_input()


# =====================================================================
#  ОНЛАЙН
# =====================================================================
def host_game(screen, clock, sfx, obj_surf, target: HostTarget) -> str:
    """Создаёт сервер в выбранной сети и играет. Возвращает "", QUIT или уведомление."""
    server = GameServer(PORT, host=target.bind)
    try:
        server.start()
        session = NetSession(target.connect, PORT)
    except Exception as e:
        server.stop()
        return f"Не удалось создать игру в сети {target.show}: {e}"
    addr = f"{target.show}:{PORT}"
    net_name = target.title.split("   ")[0]
    print(f"Сервер запущен ({net_name}). Адрес для подключения: {addr}")
    try:
        return play(screen, clock, sfx, session,
                    ["режим: онлайн (хост)", f"сеть: {net_name}"],
                    obj_surf, server_addr=addr)
    finally:
        session.close()
        server.stop()


def run_online(screen, clock, sfx, obj_surf) -> str:
    """Весь онлайн-раздел: меню -> выбор сети / ввод адреса -> игра."""
    while True:
        sub = online_menu(screen, clock)
        if sub == QUIT:
            return QUIT
        if sub == "back":
            return ""

        if sub == "host":
            target = network_menu(screen, clock)
            if target == QUIT:
                return QUIT
            if target is None:
                continue
            return host_game(screen, clock, sfx, obj_surf, target)

        if sub == "join":
            session = join_menu(screen, clock)
            if session == QUIT:
                return QUIT
            if session is None:
                continue
            try:
                return play(screen, clock, sfx, session,
                            ["режим: онлайн (клиент)"], obj_surf)
            finally:
                session.close()


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
            result = run_online(screen, clock, sfx, obj_surf)

        if result == QUIT:
            break
        notice = result

    pygame.quit()


if __name__ == "__main__":
    main()