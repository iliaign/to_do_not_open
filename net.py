"""
net.py — сеть и «сессии».

Протокол v2: TCP, JSON-строки (по одной на сообщение, разделитель '\n').

  клиент -> сервер:  {"t":"hello","v":2,"name":"Илья"}                    первым сообщением
                     {"t":"in","mx":-1|0|1,"my":-1|0|1,"k":bool,"ox":x,"oy":y}
                         ox/oy — где игрок видел объект в момент пинка (компенсация задержки)
  сервер -> клиент:  {"t":"welcome","v":2,"id":N,"name":"..."}
                     {"t":"bad_version","v":2}   |   {"t":"full"}
                     {"t":"roster","rv":N,"p":[[pid,color,kind,name],...]}  при изменениях
                     {"t":"s", ...снимок World.snapshot()...}               ~30 раз/сек

Сервер авторитетный: симуляция идёт только на нём, клиенты шлют ввод
и рисуют снимки. У каждого клиента своя очередь и поток отправки: медленное
соединение одного игрока не тормозит остальных (старые снимки просто отбрасываются).

Сессии имеют одинаковый интерфейс (его использует game.py):
    my_id, alive, predict, roster, send_input(mx,my,kick,claim), poll() -> (snapshot|None, events), close()
  * NetSession   — подключение к серверу по сети
  * LocalSession — локальный World в том же процессе (игра с ботом)

Отдельный headless-сервер:
    python net.py                  — слушать на всех сетях
    python net.py --ip 26.12.34.56 — слушать только в выбранной сети
    python net.py --list           — показать доступные сети
"""

from __future__ import annotations

import errno
import json
import math
import socket
import threading
import time

from core import MAX_HUMANS, TICK, World, sanitize_name
from discovery import Announcer
from netutil import (DISCOVERY_PORT, PORT, HostTarget, NetIface,  # noqa: F401  (реэкспорт)
                     friendly_error, host_targets, list_interfaces, local_ip)

PROTOCOL = 2
SNAPSHOT_STEPS = 2           # снимок каждые 2 тика = 30 раз/сек при 60 тиках
HELLO_TIMEOUT = 3.0          # сколько ждём приветствие от нового клиента
SILENCE_TIMEOUT = 8.0        # клиент считает соединение потерянным после такой тишины


def _encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


# =====================================================================
#  СЕРВЕР
# =====================================================================
class _Client:
    """Один подключённый игрок со своим потоком отправки."""

    def __init__(self, server: "GameServer", pid: int, conn: socket.socket):
        self.server, self.pid, self.conn = server, pid, conn
        self.cv = threading.Condition()
        self._ctrl: list[bytes] = []
        self._snap = None
        self.alive = True
        threading.Thread(target=self._run, daemon=True).start()

    def send_ctrl(self, data: bytes) -> None:
        with self.cv:
            self._ctrl.append(data)
            self.cv.notify()

    def send_snapshot(self, snap: dict) -> None:
        with self.cv:
            if self._snap is not None:               # прошлый снимок так и не ушёл:
                old_ev = self._snap["ev"]            # выкидываем его, но события (звуки) сохраняем
                if old_ev:
                    snap = dict(snap)
                    snap["ev"] = old_ev + snap["ev"]
            self._snap = snap
            self.cv.notify()

    def close(self) -> None:
        with self.cv:
            self.alive = False
            self.cv.notify()
        try:
            self.conn.close()
        except OSError:
            pass

    def _run(self) -> None:
        while True:
            with self.cv:
                while self.alive and not self._ctrl and self._snap is None:
                    self.cv.wait(0.5)
                if not self.alive:
                    return
                ctrl, self._ctrl = self._ctrl, []
                snap, self._snap = self._snap, None
            try:
                for data in ctrl:
                    self.conn.sendall(data)
                if snap is not None:
                    self.conn.sendall(_encode(snap))
            except OSError:
                break
        self.server._drop(self.pid)


class GameServer:
    def __init__(self, port: int = PORT, host: str = "0.0.0.0", name: str = ""):
        self.port = port
        self.host = host                       # адрес адаптера, на котором слушаем
        self.name = sanitize_name(name) or "Игра"
        self.world = World(spawn_extras=True)
        self.lock = threading.Lock()
        self.clients: dict[int, _Client] = {}
        self.running = False
        self.srv = None
        self._announcer = None

    def player_info(self) -> tuple[int, int]:
        return len(self.clients), MAX_HUMANS

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                # Windows: SO_REUSEADDR разрешил бы двум хостам занять один порт
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            for attempt in range(6):
                try:
                    srv.bind((self.host, self.port))
                    break
                except OSError as e:
                    busy = e.errno in (errno.EADDRINUSE, 10048)
                    if busy and attempt < 5:
                        time.sleep(0.25)             # порт мог ещё не освободиться после прошлой игры
                        continue
                    if busy:
                        raise OSError(f"порт {self.port} занят — игра уже запущена?") from e
                    raise
            srv.listen(8)
            srv.settimeout(0.5)
        except OSError:
            srv.close()
            raise
        self.port = srv.getsockname()[1]
        self.srv = srv
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._tick_loop, daemon=True).start()
        self._announcer = Announcer(self.host, self.port, self.name, self.player_info, PROTOCOL)
        self._announcer.start()

    def stop(self) -> None:
        self.running = False
        if self._announcer:
            self._announcer.stop()
        with self.lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for c in clients:
            c.close()
        if self.srv:
            try:
                self.srv.close()
            except OSError:
                pass

    # ---- подключения
    def _accept_loop(self) -> None:
        while self.running:
            try:
                conn, _addr = self.srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handshake, args=(conn,), daemon=True).start()

    @staticmethod
    def _reject(conn: socket.socket, msg: dict) -> None:
        try:
            conn.sendall(_encode(msg))
        except OSError:
            pass
        conn.close()

    def _read_hello(self, conn: socket.socket):
        buf = b""
        while b"\n" not in buf:
            data = conn.recv(1024)
            if not data:
                raise ValueError("соединение закрыто")
            buf += data
            if len(buf) > 4096:
                raise ValueError("слишком длинное приветствие")
        line, rest = buf.split(b"\n", 1)
        msg = json.loads(line)
        if not isinstance(msg, dict) or msg.get("t") != "hello":
            raise ValueError("ожидалось hello")
        return msg, rest

    def _roster_msg(self) -> dict:                  # вызывать под self.lock
        return {"t": "roster", "rv": self.world.roster_version, "p": self.world.roster()}

    def _handshake(self, conn: socket.socket) -> None:
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(HELLO_TIMEOUT)
            msg, rest = self._read_hello(conn)
        except (OSError, ValueError):
            conn.close()
            return
        if msg.get("v") != PROTOCOL:
            self._reject(conn, {"t": "bad_version", "v": PROTOCOL})
            return
        with self.lock:
            person = self.world.add_human(msg.get("name", "")) if self.running else None
            pid = person.pid if person else None
            pname = person.name if person else ""
        if pid is None:
            self._reject(conn, {"t": "full"})
            return
        try:
            conn.sendall(_encode({"t": "welcome", "v": PROTOCOL, "id": pid, "name": pname}))
        except OSError:
            with self.lock:
                self.world.remove(pid)
            conn.close()
            return
        conn.settimeout(2.0)
        client = _Client(self, pid, conn)
        with self.lock:
            self.clients[pid] = client
            client.send_ctrl(_encode(self._roster_msg()))   # новичок сразу получает состав
        threading.Thread(target=self._client_loop, args=(pid, conn, rest),
                         daemon=True).start()

    def _handle_msg(self, pid: int, line: bytes) -> None:
        try:
            msg = json.loads(line)
            if msg.get("t") != "in":
                return
            mx = int(msg.get("mx", 0))
            my = int(msg.get("my", 0))
            kick = bool(msg.get("k"))
            claim = None
            if kick:
                try:
                    cx, cy = float(msg["ox"]), float(msg["oy"])
                    if math.isfinite(cx) and math.isfinite(cy):
                        claim = (cx, cy)
                except (KeyError, TypeError, ValueError):
                    claim = None
            with self.lock:
                self.world.set_input(pid, mx, my)
                if kick:
                    self.world.queue_kick(pid, claim)
        except (ValueError, TypeError, AttributeError):
            return

    def _client_loop(self, pid: int, conn: socket.socket, buf: bytes) -> None:
        while self.running:
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_msg(pid, line)
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            if len(buf) > 65536:       # мусор/флуд
                break
        self._drop(pid)

    def _drop(self, pid: int) -> None:
        with self.lock:
            client = self.clients.pop(pid, None)
            self.world.remove(pid)
        if client:
            client.close()

    # ---- основной цикл симуляции
    def _tick_loop(self) -> None:
        last = time.perf_counter()
        acc = 0.0
        steps_since_send = 0
        rv_sent = -1
        while self.running:
            now = time.perf_counter()
            acc = min(acc + (now - last), 0.25)
            last = now

            snap = None
            roster = None
            targets: list[_Client] = []
            with self.lock:
                while acc >= TICK:
                    self.world.step()
                    acc -= TICK
                    steps_since_send += 1
                if self.world.roster_version != rv_sent:
                    rv_sent = self.world.roster_version
                    roster = _encode(self._roster_msg())
                if steps_since_send >= SNAPSHOT_STEPS:
                    snap = self.world.snapshot()
                    steps_since_send = 0
                if roster is not None or snap is not None:
                    targets = list(self.clients.values())

            for c in targets:
                if roster is not None:
                    c.send_ctrl(roster)
                if snap is not None:
                    c.send_snapshot(snap)
            time.sleep(0.003)


# =====================================================================
#  СЕССИИ
# =====================================================================
class NetSession:
    predict = True   # клиент предсказывает движение своего игрока

    def __init__(self, host: str, port: int = PORT, timeout: float = 4.0, name: str = ""):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(1.0)
        self.my_id = None
        self.my_name = ""
        self.error = None
        self.roster: dict = {}                 # pid -> (color, kind, name)
        self._alive = True
        self._last_rx = time.monotonic()
        self._lock = threading.Lock()
        self._latest = None
        self._fresh = False
        self._events: list = []
        try:
            self.sock.sendall(_encode({"t": "hello", "v": PROTOCOL, "name": name}))
        except OSError:
            self.close()
            raise
        threading.Thread(target=self._reader, daemon=True).start()

        deadline = time.perf_counter() + timeout
        while (self.my_id is None and self.error is None and self._alive
               and time.perf_counter() < deadline):
            time.sleep(0.02)
        if self.my_id is None:
            err = self.error or "сервер не ответил"
            self.close()
            raise ConnectionError(err)

    @property
    def alive(self) -> bool:
        return self._alive and (time.monotonic() - self._last_rx) < SILENCE_TIMEOUT

    def _reader(self) -> None:
        buf = b""
        while self._alive:
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                try:
                    self._handle(json.loads(line))
                except ValueError:
                    continue
        self._alive = False

    def _handle(self, msg: dict) -> None:
        self._last_rx = time.monotonic()
        t = msg.get("t")
        if t == "welcome":
            if msg.get("v") != PROTOCOL:
                self.error = ("у хоста другая версия игры — обновите файлы игры "
                              "на всех компьютерах")
                return
            self.my_name = str(msg.get("name", ""))
            self.my_id = msg.get("id")
        elif t == "bad_version":
            self.error = (f"у хоста другая версия игры (у него v{msg.get('v')}, "
                          f"у вас v{PROTOCOL}) — обновите файлы игры")
        elif t == "full":
            self.error = "сервер полон"
        elif t == "roster":
            try:
                self.roster = {pid: (color, kind, name)
                               for pid, color, kind, name in msg.get("p", [])}
            except (TypeError, ValueError):
                pass
        elif t == "s":
            with self._lock:
                self._latest = msg
                self._fresh = True
                self._events.extend(msg.get("ev", []))

    def send_input(self, mx: int, my: int, kick: bool = False, claim=None) -> None:
        msg = {"t": "in", "mx": mx, "my": my, "k": bool(kick)}
        if kick and claim is not None:
            msg["ox"] = round(claim[0], 2)
            msg["oy"] = round(claim[1], 2)
        try:
            self.sock.sendall(_encode(msg))
        except OSError:
            self._alive = False

    def poll(self):
        with self._lock:
            snap = self._latest if self._fresh else None
            self._fresh = False
            events, self._events = self._events, []
        return snap, events

    def close(self) -> None:
        self._alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class LocalSession:
    """Игра с ботом: World крутится прямо в клиенте, без сети."""
    predict = False

    def __init__(self, name: str = ""):
        self.world = World(spawn_extras=False)
        me = self.world.add_human(name)
        self.world.add_bot()          # стоячий бот
        self.my_id = me.pid
        self._acc = 0.0
        self._last = time.perf_counter()
        self._rv = -1
        self._roster: dict = {}

    @property
    def alive(self) -> bool:
        return True

    @property
    def roster(self) -> dict:
        if self._rv != self.world.roster_version:
            self._rv = self.world.roster_version
            self._roster = {pid: (color, kind, name)
                            for pid, color, kind, name in self.world.roster()}
        return self._roster

    def send_input(self, mx: int, my: int, kick: bool = False, claim=None) -> None:
        self.world.set_input(self.my_id, mx, my)
        if kick:
            self.world.queue_kick(self.my_id)

    def poll(self):
        now = time.perf_counter()
        self._acc = min(self._acc + (now - self._last), 0.25)
        self._last = now
        while self._acc >= TICK:
            self.world.step()
            self._acc -= TICK
        snap = self.world.snapshot()
        return snap, snap["ev"]

    def close(self) -> None:
        pass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Headless-сервер игры")
    ap.add_argument("--ip", default="0.0.0.0",
                    help="адрес сети, в которой слушать (по умолчанию все)")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--name", default="Сервер", help="название игры в списке найденных")
    ap.add_argument("--list", action="store_true", help="показать доступные сети и выйти")
    args = ap.parse_args()

    if args.list:
        for i in list_interfaces():
            print(f"{i.ip:<16} {i.title.split('   ')[0]:<22} {i.name}")
        raise SystemExit(0)

    server = GameServer(args.port, host=args.ip, name=args.name)
    server.start()
    shown = local_ip() if args.ip == "0.0.0.0" else args.ip
    print(f"Сервер запущен: {shown}:{server.port}  (Ctrl+C — остановить)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.stop()
