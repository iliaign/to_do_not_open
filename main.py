"""
Обучение в МФТИ — упоротая аркада с пинками (pygame + клиент/сервер).

Запуск:  python main.py        (pip install -r requirements.txt)

Режимы:
  * ИГРА С БОТОМ  — локально, против стоячего бота
  * ОНЛАЙН / СОЗДАТЬ — выбираете сеть (Radmin VPN, Wi-Fi, ...), вы хост и играете сами;
                       адрес копируется в буфер обмена, игра видна в списке у друзей
  * ОНЛАЙН / ПОДКЛЮЧИТЬСЯ — клик по найденной игре или ввод/вставка адреса вручную

Управление: WASD / стрелки — двигаться, ПРОБЕЛ — пинок, ESC — в меню,
C (или клик по адресу) — скопировать адрес сервера (у хоста).
Победа в раунде — первым набрать 25 пинков.
"""

from __future__ import annotations

import pygame

import settings
import ui
from audio import Sfx
from game import play, run_online
from net import LocalSession
from render import C_BG, C_DIM, WIN_H, WIN_W, build_object_surface, draw_text


def open_window():
    """Окно можно растягивать: картинка масштабируется (pygame.SCALED)."""
    flags = getattr(pygame, "SCALED", 0) | pygame.RESIZABLE
    try:
        return pygame.display.set_mode((WIN_W, WIN_H), flags)
    except pygame.error:
        return pygame.display.set_mode((WIN_W, WIN_H))


def main() -> None:
    pygame.mixer.pre_init(44100, -16, 1, 512)
    pygame.init()
    screen = open_window()
    pygame.display.set_caption("Обучение в МФТИ")
    clock = pygame.time.Clock()
    cfg = settings.load()

    # при первом запуске звуки создаются ~секунду, дальше берутся из кэша
    screen.fill(C_BG)
    draw_text(screen, "Загрузка...", 28, C_DIM, (WIN_W // 2, WIN_H // 2))
    pygame.display.flip()
    pygame.event.pump()

    sfx = Sfx()
    obj_surf = build_object_surface()
    notice = ""

    while True:
        choice = ui.main_menu(screen, clock, cfg["name"], notice)
        notice = ""
        if choice in ("quit", ui.QUIT):
            break

        result = ""
        if choice == "name":
            new = ui.name_menu(screen, clock, cfg["name"])
            if new == ui.QUIT:
                break
            if new is not None:
                cfg["name"] = new
                settings.save(cfg)
            continue
        if choice == "bot":
            session = LocalSession(cfg["name"])
            result = play(screen, clock, sfx, session, ["режим: игра с ботом"], obj_surf)
            session.close()
        elif choice == "online":
            result = run_online(screen, clock, sfx, obj_surf, cfg)

        if result == ui.QUIT:
            break
        notice = result

    pygame.quit()


if __name__ == "__main__":
    main()
