"""
ui.py — меню: главное, онлайн, выбор сети, ник, подключение (со списком найденных игр).

Все функции-меню возвращают QUIT, если закрыли окно.
"""

from __future__ import annotations

import re
import threading
import time

import pygame

from clipboard import PasteJob
from core import NAME_MAX, sanitize_name
from net import PROTOCOL, NetSession
from netutil import PORT, friendly_error, host_targets
from render import C_BG, C_DIM, C_ERR, C_GOLD, C_PANEL, C_TEXT, WIN_H, WIN_W, draw_text

QUIT = "__quit__"

GREEN, GREEN_H = (34, 197, 94), (22, 163, 74)
BLUE, BLUE_H = (96, 165, 250), (59, 130, 246)
RED, RED_H = (239, 68, 68), (220, 38, 38)
AMBER, AMBER_H = (251, 191, 36), (245, 158, 11)
GRAY, GRAY_H = (203, 213, 225), (148, 163, 184)

_ADDR_ALLOWED = set("0123456789.:abcdefghijklmnopqrstuvwxyz-")
_ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(:\d{1,5})?$")


def clean_address(raw: str) -> str:
    return "".join(c for c in raw.lower() if c in _ADDR_ALLOWED)


# =====================================================================
#  ОБЩИЕ ЭЛЕМЕНТЫ
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
            draw_text(screen, notice, 18, C_ERR, (WIN_W // 2, WIN_H - 40))
        pygame.display.flip()
        clock.tick(60)


# =====================================================================
#  МЕНЮ
# =====================================================================
def main_menu(screen, clock, name, notice=""):
    cx = WIN_W // 2
    label = f"ИМЯ: {name}" if name else "ИМЯ: (не задано)"
    return choose(screen, clock, "упоротая аркада с пинками", [
        Button("bot", "ИГРА С БОТОМ", (cx, 270), GREEN, GREEN_H),
        Button("online", "ОНЛАЙН (ЛОКАЛЬНАЯ СЕТЬ)", (cx, 345), BLUE, BLUE_H, w=520),
        Button("name", label, (cx, 420), AMBER, AMBER_H, w=520),
        Button("quit", "ВЫХОД", (cx, 495), RED, RED_H),
    ], notice)


def online_menu(screen, clock):
    cx = WIN_W // 2
    return choose(screen, clock, "онлайн по локальной сети", [
        Button("host", "СОЗДАТЬ ИГРУ (ХОСТ)", (cx, 280), GREEN, GREEN_H, w=520),
        Button("join", "ПОДКЛЮЧИТЬСЯ", (cx, 360), BLUE, BLUE_H, w=520),
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


def name_menu(screen, clock, current=""):
    """Ввод ника. Возвращает новый ник (может быть ''), None (отмена) или QUIT."""
    cx = WIN_W // 2
    text = current
    btn_ok = Button("ok", "СОХРАНИТЬ", (cx - 170, 450), GREEN, GREEN_H, w=320)
    btn_back = Button("back", "НАЗАД", (cx + 170, 450), RED, RED_H, w=320)
    box = pygame.Rect(0, 0, 560, 56)
    box.center = (cx, 340)

    pygame.key.start_text_input()
    try:
        while True:
            mouse = pygame.mouse.get_pos()
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    return QUIT
                if ev.type == pygame.TEXTINPUT:
                    text = (text + "".join(c for c in ev.text if c.isprintable()))[:NAME_MAX]
                elif ev.type == pygame.KEYDOWN:
                    if ev.key == pygame.K_ESCAPE:
                        return None
                    if ev.key == pygame.K_BACKSPACE:
                        text = text[:-1]
                    elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        return sanitize_name(text)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if btn_ok.rect.collidepoint(ev.pos):
                        return sanitize_name(text)
                    if btn_back.rect.collidepoint(ev.pos):
                        return None

            draw_title(screen, "ваше имя в игре")
            draw_text(screen, f"Введите имя (до {NAME_MAX} символов). "
                              "Пустое имя — «Игрок N».", 18, C_DIM, (cx, 275))
            pygame.draw.rect(screen, C_PANEL, box, border_radius=6)
            pygame.draw.rect(screen, (59, 130, 246), box, 2, border_radius=6)
            caret = "|" if (pygame.time.get_ticks() // 500) % 2 == 0 else " "
            draw_text(screen, text + caret, 26, C_TEXT, (box.left + 14, box.centery),
                      "midleft")
            draw_text(screen, "Счёт в раунде сохраняется, если вы вышли и зашли с тем же именем.",
                      15, C_DIM, (cx, 395))
            btn_ok.draw(screen, mouse)
            btn_back.draw(screen, mouse)
            pygame.display.flip()
            clock.tick(60)
    finally:
        pygame.key.stop_text_input()


# =====================================================================
#  ПОДКЛЮЧЕНИЕ
# =====================================================================
class Connector:
    """Подключение в фоновом потоке, чтобы окно не зависало. cancel() отменяет."""

    def __init__(self, host, port, name):
        self.session = None
        self.exc = None
        self.done = False
        self._cancelled = False
        self._lock = threading.Lock()
        threading.Thread(target=self._run, args=(host, port, name), daemon=True).start()

    def _run(self, host, port, name):
        try:
            s = NetSession(host, port, name=name)
        except Exception as e:
            with self._lock:
                self.exc = e
                self.done = True
            return
        with self._lock:
            if self._cancelled:
                s.close()
            else:
                self.session = s
            self.done = True

    def cancel(self):
        with self._lock:
            self._cancelled = True
            s, self.session = self.session, None
        if s is not None:
            s.close()


def join_menu(screen, clock, finder, name, text=""):
    """Список найденных игр + ручной ввод адреса.

    Возвращает (NetSession | None | QUIT, использованный адрес).
    """
    cx = WIN_W // 2
    error = ""
    connector = None
    conn_addr = ""
    paste_job = PasteJob() if not text else None     # пустое поле: пробуем взять адрес из буфера
    auto_paste = paste_job is not None

    btn_join = Button("join", "ПОДКЛЮЧИТЬСЯ", (cx - 170, 625), GREEN, GREEN_H, w=320, h=54)
    btn_back = Button("back", "НАЗАД", (cx + 170, 625), RED, RED_H, w=320, h=54)
    btn_paste = Button("paste", "ВСТАВИТЬ", (cx + 275, 535), BLUE, BLUE_H, w=150, h=52)
    box = pygame.Rect(0, 0, 520, 52)
    box.center = (cx - 80, 535)

    def begin(addr_text):
        nonlocal connector, conn_addr, error
        host, port = addr_text.strip(), PORT
        try:
            if ":" in host:
                host, p = host.rsplit(":", 1)
                try:
                    port = int(p)
                except ValueError:
                    raise ValueError("неверный порт")
            if not host:
                raise ValueError("введите адрес хоста")
            if not 0 < port < 65536:
                raise ValueError("неверный порт")
        except ValueError as e:
            error = f"Ошибка: {e}"
            return
        error = ""
        conn_addr = f"{host}:{port}"
        connector = Connector(host, port, name)

    def request_paste():
        nonlocal paste_job, auto_paste
        paste_job = PasteJob()
        auto_paste = False

    pygame.key.start_text_input()
    try:
        while True:
            mouse = pygame.mouse.get_pos()
            games = finder.games()[:4]
            rows = []
            for i in range(len(games)):
                r = pygame.Rect(0, 0, 660, 46)
                r.center = (cx, 255 + i * 54)
                rows.append(r)

            # результат вставки из буфера
            if paste_job is not None and paste_job.done:
                clip = clean_address(paste_job.text)
                if auto_paste:
                    if _ADDR_RE.match(clip) and not text:
                        text = clip
                elif clip:
                    text, error = clip, ""
                else:
                    error = "В буфере обмена нет адреса"
                paste_job = None
                auto_paste = False

            # результат подключения
            if connector is not None and connector.done:
                if connector.session is not None:
                    return connector.session, conn_addr
                error = f"Не удалось подключиться: {friendly_error(connector.exc)}"
                connector = None

            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    if connector is not None:
                        connector.cancel()
                    return QUIT, text
                if connector is not None:               # пока подключаемся — только отмена
                    if ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                        connector.cancel()
                        connector = None
                    continue
                if ev.type == pygame.TEXTINPUT:
                    text += clean_address(ev.text)
                elif ev.type == pygame.KEYDOWN:
                    ctrl = ev.mod & (pygame.KMOD_CTRL | pygame.KMOD_META)
                    if ev.key == pygame.K_ESCAPE:
                        return None, text
                    if ctrl and (ev.key == pygame.K_v or getattr(ev, "scancode", None) == 25):
                        request_paste()
                    elif ev.key == pygame.K_BACKSPACE:
                        text = text[:-1]
                    elif ev.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                        begin(text)
                elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                    if btn_back.rect.collidepoint(ev.pos):
                        return None, text
                    if btn_paste.rect.collidepoint(ev.pos):
                        request_paste()
                        continue
                    if btn_join.rect.collidepoint(ev.pos):
                        begin(text)
                        continue
                    for g, r in zip(games, rows):
                        if r.collidepoint(ev.pos):
                            text = f"{g['addr']}:{g['port']}"
                            begin(text)
                            break

            # ---------------- рисование
            if connector is not None:
                draw_title(screen, "подключение")
                dots = "." * (1 + int(time.monotonic() * 3) % 3)
                draw_text(screen, f"Подключаюсь к {conn_addr}{dots}", 24, C_GOLD, (cx, 340))
                draw_text(screen, "ESC — отмена", 16, C_DIM, (cx, 390))
            else:
                draw_title(screen, "подключение к игре")
                draw_text(screen, "Найденные игры в локальной сети (клик — подключиться):",
                          18, C_DIM, (cx, 215))
                if games:
                    for g, r in zip(games, rows):
                        pygame.draw.rect(screen, BLUE_H if r.collidepoint(mouse) else BLUE,
                                         r, border_radius=8)
                        draw_text(screen, g["name"] or "Игра", 20, C_BG,
                                  (r.left + 16, r.centery), "midleft", True)
                        if g["version"] != PROTOCOL:
                            draw_text(screen, "другая версия", 13, (127, 29, 29),
                                      (r.left + 270, r.centery), "midleft")
                        draw_text(screen, f"{g['addr']}:{g['port']}", 16, (30, 41, 59),
                                  (r.right - 80, r.centery), "midright")
                        draw_text(screen, f"{g['players']}/{g['max']}", 18, C_BG,
                                  (r.right - 14, r.centery), "midright", True)
                else:
                    msg = ("Автопоиск недоступен — введите адрес вручную" if finder.error
                           else "Ищу игры в сети... (хост должен создать игру в этой же сети)")
                    draw_text(screen, msg, 17, C_DIM, (cx, 255))

                draw_text(screen, "Или введите адрес хоста вручную (Ctrl+V — вставить):",
                          18, C_DIM, (cx, 490))
                pygame.draw.rect(screen, C_PANEL, box, border_radius=6)
                pygame.draw.rect(screen, (59, 130, 246), box, 2, border_radius=6)
                caret = "|" if (pygame.time.get_ticks() // 500) % 2 == 0 else " "
                draw_text(screen, text + caret, 26, C_TEXT, (box.left + 14, box.centery),
                          "midleft")
                if error:
                    draw_text(screen, error, 17, C_ERR, (cx, 580))
                btn_paste.draw(screen, mouse)
                btn_join.draw(screen, mouse)
                btn_back.draw(screen, mouse)
            pygame.display.flip()
            clock.tick(60)
    finally:
        pygame.key.stop_text_input()
