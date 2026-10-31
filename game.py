"""
game.py — игровой цикл и онлайн-режимы (хост / клиент).
"""

from __future__ import annotations

import pygame

import settings
from clipboard import CopyJob
from core import (GROUND_MAX, GROUND_MIN, PERSON_X_LIMIT, PLAYER_CONTROL_SPEED,
                  SPEED_SCALE, X_MIN, clamp)
from discovery import GameFinder
from net import PORT, GameServer, NetSession
from netutil import friendly_error
from render import (C_BG, C_CYAN, C_DIM, C_PANEL, C_YELLOW, GAME_RECT, INTERP_DELAY,
                    Interpolator, addr_box_rect, draw_banner, draw_person, draw_sidebar,
                    draw_text, to_screen)
from ui import QUIT, join_menu, network_menu, online_menu

# Физические коды клавиш (SDL scancode) — работают в любой раскладке (в т.ч. русской).
# Отрицательные значения — запасной вариант по keycode.
KEY_ACTIONS = {
    26: "up", 82: "up", 22: "down", 81: "down",
    4: "left", 80: "left", 7: "right", 79: "right", 44: "kick",
    -pygame.K_w: "up", -pygame.K_UP: "up", -pygame.K_s: "down", -pygame.K_DOWN: "down",
    -pygame.K_a: "left", -pygame.K_LEFT: "left", -pygame.K_d: "right",
    -pygame.K_RIGHT: "right", -pygame.K_SPACE: "kick",
}
SC_C = 6                              # физический код клавиши C


def key_id(ev) -> int:
    sc = getattr(ev, "scancode", None)
    if sc in KEY_ACTIONS:
        return sc
    return -ev.key


def play(screen, clock, sfx, session, mode_lines, obj_surf, server_addr=None) -> str:
    """Возвращает "" (выход в меню), QUIT или текст уведомления."""
    held: set[int] = set()
    last_input = None
    interp = Interpolator(INTERP_DELAY if session.predict else 0.0)
    last_obj = None                   # где объект был нарисован в прошлом кадре (для claim)
    anim_t = 0.0
    local = None                      # предсказанная позиция своего игрока
    focus_lost = getattr(pygame, "WINDOWFOCUSLOST", None)

    copy_job = None
    copy_state = None                 # None | "busy" | "ok" | "fail"
    copy_until = 0.0

    def start_copy():
        nonlocal copy_job, copy_state
        copy_job = CopyJob(server_addr)
        copy_state = "busy"

    if server_addr:
        start_copy()                  # хост: адрес сразу попадает в буфер обмена

    while True:
        dt = min(clock.tick(60) / 1000.0, 0.1)
        anim_t += dt * SPEED_SCALE
        now = pygame.time.get_ticks() / 1000.0
        kick = False

        if copy_job is not None and copy_job.done:
            copy_state = "ok" if copy_job.ok else "fail"
            copy_until = now + 3.0
            copy_job = None
        elif copy_job is None and copy_state in ("ok", "fail") and now > copy_until:
            copy_state = None

        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return QUIT
            if ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    return ""
                if server_addr and (ev.key == pygame.K_c
                                    or getattr(ev, "scancode", None) == SC_C):
                    if copy_job is None:
                        start_copy()
                    continue
                kid = key_id(ev)
                held.add(kid)
                if KEY_ACTIONS.get(kid) == "kick":
                    kick = True
            elif ev.type == pygame.KEYUP:
                held.discard(key_id(ev))
            elif (ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1
                  and server_addr and addr_box_rect().collidepoint(ev.pos)):
                if copy_job is None:
                    start_copy()
            elif focus_lost is not None and ev.type == focus_lost:
                held.clear()

        acts = {KEY_ACTIONS[k] for k in held if k in KEY_ACTIONS}
        mx = int("right" in acts) - int("left" in acts)
        my = int("up" in acts) - int("down" in acts)
        if (mx, my) != last_input or kick:
            # при пинке сообщаем серверу, где мы видели объект (компенсация задержки)
            claim = last_obj if (kick and session.predict) else None
            session.send_input(mx, my, kick, claim)
            last_input = (mx, my)

        if not session.alive:
            return "Соединение с сервером потеряно"

        snap, events = session.poll()
        if snap is not None:
            interp.push(snap, now)
        for ev in events:
            sfx.handle(ev)
        frame = interp.frame(now)
        roster = session.roster

        # ---------------- рисование
        screen.fill(C_BG)
        pygame.draw.rect(screen, C_PANEL, GAME_RECT)

        if frame is None:
            draw_text(screen, "Ожидание данных от сервера...", 22, C_DIM, GAME_RECT.center)
            draw_sidebar(screen, None, roster, session.my_id, mode_lines, server_addr,
                         copy_state)
            pygame.display.flip()
            continue

        ox, oy, oang = frame["o"]
        last_obj = (ox, oy)

        screen.set_clip(GAME_RECT)
        rot = pygame.transform.rotozoom(obj_surf, oang, 1.0)
        screen.blit(rot, rot.get_rect(center=to_screen(ox, oy)))

        persons = []
        for pr in frame["p"]:
            pid = pr["pid"]
            x, gy, facing = pr["x"], pr["gy"], pr["facing"]
            me = pid == session.my_id
            if me and session.predict:
                sx, sy = frame["latest"].get(pid, (x, gy))     # свежая позиция с сервера
                if local is None or abs(local[0] - sx) + abs(local[1] - sy) > 4.0:
                    local = [sx, sy]
                if mx or my:
                    local[0] = clamp(local[0] + mx * PLAYER_CONTROL_SPEED * SPEED_SCALE * dt,
                                     X_MIN + 1.2, PERSON_X_LIMIT)
                    local[1] = clamp(local[1] + my * PLAYER_CONTROL_SPEED * SPEED_SCALE * dt,
                                     GROUND_MIN, GROUND_MAX)
                else:
                    k = min(1.0, dt * 8.0)
                    local[0] += (sx - local[0]) * k
                    local[1] += (sy - local[1]) * k
                x, gy = local
                if mx:
                    facing = 1 if mx > 0 else -1
            info = roster.get(pid)
            color, kind, pname = info if info else ("#9ca3af", "npc", "")
            persons.append({
                "x": x, "gy": gy, "facing": facing, "color": color,
                "kt": pr["kt"], "kl": pr["kl"], "kind": kind,
                "name": pname, "me": me, "phase": pid * 1.7,
                "tag_color": C_YELLOW if me else (203, 213, 225),
            })
        for pr in sorted(persons, key=lambda p: -p["gy"]):
            draw_person(screen, pr, anim_t)
        screen.set_clip(None)

        draw_banner(screen, frame)

        # HUD
        draw_text(screen, frame["lbl"], 28, (248, 250, 252),
                  (GAME_RECT.centerx, to_screen(0, 12.15)[1]), "center", True)
        draw_text(screen, f"FPS: {clock.get_fps():5.1f}   x{SPEED_SCALE:.1f}", 18, C_CYAN,
                  (GAME_RECT.left + 12, to_screen(0, 12.15)[1]), "midleft", True)
        draw_text(screen, f"PLAYERS: {len(frame['p'])}", 18, C_YELLOW,
                  (GAME_RECT.right - 12, to_screen(0, 12.15)[1]), "midright", True)
        draw_text(screen, "WASD / стрелки — двигаться     ПРОБЕЛ — пинок     ESC — меню",
                  15, C_DIM, (GAME_RECT.centerx, to_screen(0, -6.7)[1]), "center")

        draw_sidebar(screen, frame, roster, session.my_id, mode_lines, server_addr,
                     copy_state)
        pygame.display.flip()


# =====================================================================
#  ОНЛАЙН
# =====================================================================
def host_game(screen, clock, sfx, obj_surf, target, name) -> str:
    """Создаёт сервер в выбранной сети и играет. Возвращает "", QUIT или уведомление."""
    server = GameServer(PORT, host=target.bind, name=name or "Игра")
    try:
        server.start()
        session = NetSession(target.connect, PORT, name=name)
    except Exception as e:
        server.stop()
        return f"Не удалось создать игру в сети {target.show}: {friendly_error(e)}"
    addr = f"{target.show}:{server.port}"
    net_name = target.title.split("   ")[0]
    print(f"Сервер запущен ({net_name}). Адрес для подключения: {addr}")
    try:
        return play(screen, clock, sfx, session,
                    ["режим: онлайн (хост)", f"сеть: {net_name}"],
                    obj_surf, server_addr=addr)
    finally:
        session.close()
        server.stop()


def run_online(screen, clock, sfx, obj_surf, cfg) -> str:
    """Весь онлайн-раздел: меню -> выбор сети / поиск и ввод адреса -> игра."""
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
            return host_game(screen, clock, sfx, obj_surf, target, cfg["name"])

        if sub == "join":
            finder = GameFinder()
            finder.start()
            try:
                session, addr = join_menu(screen, clock, finder, cfg["name"],
                                          cfg.get("last_addr", ""))
            finally:
                finder.stop()
            if session == QUIT:
                return QUIT
            if session is None:
                continue
            cfg["last_addr"] = addr
            settings.save(cfg)
            try:
                return play(screen, clock, sfx, session,
                            ["режим: онлайн (клиент)"], obj_surf)
            finally:
                session.close()
