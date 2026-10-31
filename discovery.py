"""
discovery.py — автопоиск игр в локальной сети (UDP broadcast).

  * Announcer  — хост раз в секунду объявляет игру во все выбранные сети;
  * GameFinder — клиент слушает объявления и показывает список найденных игр.

Объявление: {"g":"mfti-kick","v":<версия протокола>,"port":50505,"name":"...","n":2,"max":4}
Адрес хоста берётся из адреса отправителя пакета.
"""

from __future__ import annotations

import json
import socket
import threading
import time

from netutil import DISCOVERY_PORT, NetIface, directed_broadcast, list_interfaces

GAME_TAG = "mfti-kick"
ANNOUNCE_INTERVAL = 1.0
ROUTES_REFRESH = 10.0       # как часто перечитывать список сетей
STALE_AFTER = 4.0           # игра пропадает из списка, если тишина дольше


def _announce_routes(host: str) -> list[tuple[str, str]]:
    """Пары (наш IP, широковещательный адрес), через которые объявляем игру."""
    ifaces = list_interfaces()
    if host not in ("0.0.0.0", ""):
        if host.startswith("127."):
            return []                                   # игра «только на этом ПК»
        ifaces = [i for i in ifaces if i.ip == host] or [NetIface(host, "other")]
    routes: list[tuple[str, str]] = []
    for i in ifaces:
        routes.append((i.ip, "255.255.255.255"))
        d = directed_broadcast(i.ip, i.mask)
        if d and d != "255.255.255.255":
            routes.append((i.ip, d))
    return routes


class Announcer:
    def __init__(self, host: str, port: int, name: str, get_players, version: int):
        self.host, self.port, self.name = host, port, name
        self.get_players = get_players          # -> (сейчас игроков, максимум)
        self.version = version
        self._stop = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _open(local_ip: str):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((local_ip, 0))
        except OSError:
            s.close()
            return None
        return s

    def _run(self) -> None:
        socks: dict[str, socket.socket] = {}
        routes = _announce_routes(self.host)
        last_refresh = time.monotonic()
        while not self._stop.is_set():
            if time.monotonic() - last_refresh > ROUTES_REFRESH:
                routes = _announce_routes(self.host)
                last_refresh = time.monotonic()
            players, maxp = self.get_players()
            data = json.dumps({"g": GAME_TAG, "v": self.version, "port": self.port,
                               "name": self.name, "n": players, "max": maxp},
                              separators=(",", ":")).encode("utf-8")
            for local, bcast in routes:
                s = socks.get(local)
                if s is None:
                    s = self._open(local)
                    if s is None:
                        continue
                    socks[local] = s
                try:
                    s.sendto(data, (bcast, DISCOVERY_PORT))
                except OSError:
                    pass
            self._stop.wait(ANNOUNCE_INTERVAL)
        for s in socks.values():
            s.close()


class GameFinder:
    def __init__(self):
        self.error = ""
        self._games: dict[tuple[str, int], dict] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._sock = None

    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("", DISCOVERY_PORT))
            s.settimeout(0.5)
        except OSError as e:
            s.close()
            self.error = str(e)
            return
        self._sock = s
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        s = self._sock
        while not self._stop.is_set():
            try:
                data, addr = s.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
                if msg.get("g") != GAME_TAG:
                    continue
                port = int(msg["port"])
                info = {
                    "addr": addr[0], "port": port,
                    "name": str(msg.get("name", ""))[:20],
                    "players": int(msg.get("n", 0)),
                    "max": int(msg.get("max", 0)),
                    "version": int(msg.get("v", 0)),
                    "seen": time.monotonic(),
                }
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            with self._lock:
                self._games[(addr[0], port)] = info

    def games(self) -> list[dict]:
        now = time.monotonic()
        with self._lock:
            for key in [k for k, g in self._games.items() if now - g["seen"] > STALE_AFTER]:
                del self._games[key]
            return sorted(self._games.values(), key=lambda g: (g["name"].lower(), g["addr"]))
