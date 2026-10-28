"""
net.py — сеть и «сессии».

Протокол: TCP, JSON-строки (по одной на сообщение, разделитель '\n').

  клиент -> сервер:  {"t":"in","mx":-1|0|1,"my":-1|0|1,"k":true|false}
  сервер -> клиент:  {"t":"welcome","id":N}
                     {"t":"full"}
                     {"t":"s", ...снимок World.snapshot()...}   ~30 раз/сек

Сервер авторитетный: симуляция идёт только на нём, клиенты шлют ввод
и рисуют снимки.

Сессии имеют одинаковый интерфейс (его использует main.py):
    my_id, alive, predict, send_input(mx,my,kick), poll() -> (snapshot|None, events), close()
  * NetSession   — подключение к серверу по сети
  * LocalSession — локальный World в том же процессе (игра с ботом)

Отдельный headless-сервер:  python net.py
"""

import json
import socket
import threading
import time

from core import MAX_HUMANS, TICK, World

PORT = 50505
SNAPSHOT_INTERVAL = 1.0 / 30.0


# Если игра показывает не тот адрес — впишите сюда свой адрес из Radmin VPN,
# например MANUAL_ADDRESS = "26.12.34.56". Пустая строка = определять автоматически.
MANUAL_ADDRESS = ""


def _route_ip(target: str):
    """IP нашего интерфейса, через который идёт маршрут к target (пакеты не отправляются)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((target, 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return None


def all_local_ips() -> list[str]:
    ips: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.append(info[4][0])
    except OSError:
        pass
    for target in ("26.255.255.255", "10.255.255.255", "192.168.255.255"):
        ip = _route_ip(target)
        if ip:
            ips.append(ip)
    seen, result = set(), []
    for ip in ips:
        if ip not in seen and not ip.startswith("127."):
            seen.add(ip)
            result.append(ip)
    return result


def local_ip() -> str:
    """Адрес для подключения друзей. Приоритет: ручной -> Radmin (26.*) -> обычный LAN."""
    if MANUAL_ADDRESS:
        return MANUAL_ADDRESS
    ips = all_local_ips()
    for ip in ips:
        if ip.startswith("26."):          # Radmin VPN
            return ip
    return ips[0] if ips else "127.0.0.1"


def _encode(msg: dict) -> bytes:
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


# =====================================================================
#  СЕРВЕР
# =====================================================================
class GameServer:
    def __init__(self, port: int = PORT):
        self.port = port
        self.world = World(spawn_extras=True)
        self.lock = threading.Lock()
        self.clients: dict[int, socket.socket] = {}
        self.running = False
        self.srv = None

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(8)
        srv.settimeout(0.5)
        self.srv = srv
        self.running = True
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._tick_loop, daemon=True).start()

    def stop(self) -> None:
        self.running = False
        with self.lock:
            conns = list(self.clients.values())
            self.clients.clear()
        for c in conns:
            try:
                c.close()
            except OSError:
                pass
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
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn.settimeout(2.0)
            with self.lock:
                person = self.world.add_human() if len(self.clients) < MAX_HUMANS else None
                pid = person.pid if person else None
            if pid is None:
                try:
                    conn.sendall(_encode({"t": "full"}))
                except OSError:
                    pass
                conn.close()
                continue
            try:
                conn.sendall(_encode({"t": "welcome", "id": pid}))
            except OSError:
                with self.lock:
                    self.world.remove(pid)
                conn.close()
                continue
            with self.lock:
                self.clients[pid] = conn
            threading.Thread(target=self._client_loop, args=(pid, conn),
                             daemon=True).start()

    def _client_loop(self, pid: int, conn: socket.socket) -> None:
        buf = b""
        while self.running:
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
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                    if msg.get("t") == "in":
                        mx = int(msg.get("mx", 0))
                        my = int(msg.get("my", 0))
                        with self.lock:
                            self.world.set_input(pid, mx, my)
                            if msg.get("k"):
                                self.world.queue_kick(pid)
                except (ValueError, TypeError, AttributeError):
                    continue
        self._drop(pid)

    def _drop(self, pid: int) -> None:
        with self.lock:
            conn = self.clients.pop(pid, None)
            self.world.remove(pid)
        if conn:
            try:
                conn.close()
            except OSError:
                pass

    # ---- основной цикл симуляции
    def _tick_loop(self) -> None:
        last = time.perf_counter()
        acc = 0.0
        last_send = 0.0
        while self.running:
            now = time.perf_counter()
            acc = min(acc + (now - last), 0.25)
            last = now

            snap = None
            targets = []
            with self.lock:
                stepped = False
                while acc >= TICK:
                    self.world.step()
                    acc -= TICK
                    stepped = True
                if stepped and now - last_send >= SNAPSHOT_INTERVAL:
                    snap = self.world.snapshot()
                    targets = list(self.clients.items())
                    last_send = now

            if snap is not None:
                data = _encode(snap)
                for pid, conn in targets:
                    try:
                        conn.sendall(data)
                    except OSError:
                        self._drop(pid)
            time.sleep(0.003)


# =====================================================================
#  СЕССИИ
# =====================================================================
class NetSession:
    predict = True   # клиент предсказывает движение своего игрока

    def __init__(self, host: str, port: int = PORT, timeout: float = 4.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock.settimeout(1.0)
        self.my_id = None
        self.error = None
        self._alive = True
        self._lock = threading.Lock()
        self._latest = None
        self._fresh = False
        self._events: list = []
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
        return self._alive

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
        t = msg.get("t")
        if t == "welcome":
            self.my_id = msg.get("id")
        elif t == "full":
            self.error = "сервер полон"
        elif t == "s":
            with self._lock:
                self._latest = msg
                self._fresh = True
                self._events.extend(msg.get("ev", []))

    def send_input(self, mx: int, my: int, kick: bool = False) -> None:
        try:
            self.sock.sendall(_encode({"t": "in", "mx": mx, "my": my, "k": bool(kick)}))
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

    def __init__(self):
        self.world = World(spawn_extras=False)
        me = self.world.add_human()
        self.world.add_bot()          # стоячий бот
        self.my_id = me.pid
        self._acc = 0.0
        self._last = time.perf_counter()

    @property
    def alive(self) -> bool:
        return True

    def send_input(self, mx: int, my: int, kick: bool = False) -> None:
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
    server = GameServer(PORT)
    server.start()
    print(f"Сервер запущен: {local_ip()}:{PORT}  (Ctrl+C — остановить)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        server.stop()