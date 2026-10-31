"""
netutil.py — поиск сетевых адаптеров и варианты «в какой сети играем».

Без pygame; работает и без psutil (тогда определение по IP-диапазонам).
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass

PORT = 50505               # TCP-порт игры
DISCOVERY_PORT = 50506     # UDP-порт, по которому хосты объявляют игры

# Нужно только для режима «Все сети сразу», если автоопределение показывает не тот адрес.
# Пример: MANUAL_ADDRESS = "26.12.34.56". Пустая строка = определять автоматически.
MANUAL_ADDRESS = ""

DEFAULT_MASK = "255.255.255.0"

# вид -> (название, подсказка, приоритет в списке)
KINDS = {
    "radmin":    ("Radmin VPN",         "виртуальная сеть через интернет", 0),
    "lan":       ("Локальная сеть",     "Wi-Fi / Ethernet — устройства на одном роутере", 1),
    "hamachi":   ("Hamachi",            "виртуальная сеть через интернет", 2),
    "zerotier":  ("ZeroTier",           "виртуальная сеть через интернет", 2),
    "tailscale": ("Tailscale",          "виртуальная сеть через интернет", 2),
    "vpn":       ("VPN",                "виртуальный адаптер VPN", 3),
    "other":     ("Другая сеть",        "прочий адаптер", 5),
    "virtual":   ("Виртуальный адаптер", "VirtualBox / WSL / Docker — обычно не нужен", 9),
}

_NAME_HINTS = (                       # подстрока имени адаптера -> вид
    ("radmin", "radmin"), ("hamachi", "hamachi"), ("zerotier", "zerotier"),
    ("tailscale", "tailscale"),
    ("virtualbox", "virtual"), ("vmware", "virtual"), ("vethernet", "virtual"),
    ("docker", "virtual"), ("wsl", "virtual"), ("hyper-v", "virtual"),
    ("vboxnet", "virtual"), ("virbr", "virtual"), ("veth", "virtual"),
    ("vpn", "vpn"), ("wireguard", "vpn"), ("tun", "vpn"),
)


@dataclass(frozen=True)
class NetIface:
    ip: str
    kind: str
    name: str = ""            # имя адаптера (если доступно psutil)
    mask: str = DEFAULT_MASK

    @property
    def title(self) -> str:
        return f"{KINDS[self.kind][0]}   {self.ip}"

    @property
    def desc(self) -> str:
        parts = [p for p in (self.name, KINDS[self.kind][1]) if p]
        return " · ".join(parts)


@dataclass(frozen=True)
class HostTarget:
    """Один вариант в меню «в какой сети играем»."""
    bind: str       # адрес для bind() сервера
    connect: str    # куда подключается сам хост
    show: str       # адрес, который показываем/копируем для друзей
    title: str
    desc: str = ""


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


def _classify(ip: str, name: str):
    """Вид сети по имени адаптера и адресу. None — адрес не годится (loopback и т.п.)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return None
    if (addr.version != 4 or addr.is_loopback or addr.is_link_local
            or addr.is_unspecified or addr.is_multicast):
        return None
    low = name.lower()
    for hint, kind in _NAME_HINTS:
        if hint in low:
            return kind
    if ip.startswith("26."):
        return "radmin"
    if ip.startswith("25."):
        return "hamachi"
    if addr in ipaddress.ip_network("100.64.0.0/10"):
        return "tailscale"
    if addr.is_private:
        return "lan"
    return "other"


def directed_broadcast(ip: str, mask: str):
    """Широковещательный адрес подсети (например 192.168.1.255) или None."""
    try:
        net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
    except ValueError:
        return None
    if net.prefixlen >= 31:
        return None
    return str(net.broadcast_address)


def list_interfaces() -> list[NetIface]:
    """Все подходящие IPv4-адреса этого компьютера, лучшие — первыми."""
    found: dict[str, tuple[str, str]] = {}      # ip -> (имя адаптера, маска)

    have_psutil = False
    try:
        import psutil                  # необязательная зависимость: даёт имена адаптеров
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                if a.family == socket.AF_INET:
                    found.setdefault(a.address, (name, a.netmask or DEFAULT_MASK))
        have_psutil = True
    except Exception:
        pass

    if not have_psutil:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                found.setdefault(info[4][0], ("", DEFAULT_MASK))
        except OSError:
            pass
        for target in ("26.255.255.255", "10.255.255.255", "192.168.255.255",
                       "172.16.255.255", "8.8.8.8"):
            ip = _route_ip(target)
            if ip:
                found.setdefault(ip, ("", DEFAULT_MASK))

    result = []
    for ip, (name, mask) in found.items():
        kind = _classify(ip, name)
        if kind:
            result.append(NetIface(ip, kind, name, mask))
    result.sort(key=lambda i: KINDS[i.kind][2])      # сортировка устойчивая
    return result


def all_local_ips() -> list[str]:
    return [i.ip for i in list_interfaces()]


def local_ip() -> str:
    """Адрес для друзей, когда сеть не выбрана вручную: ручной -> лучший из списка."""
    if MANUAL_ADDRESS:
        return MANUAL_ADDRESS
    ifaces = list_interfaces()
    return ifaces[0].ip if ifaces else "127.0.0.1"


def host_targets(limit: int = 5) -> list[HostTarget]:
    """Варианты для меню выбора сети: найденные адаптеры + «все сети» + «только я»."""
    targets = [HostTarget(i.ip, i.ip, i.ip, i.title, i.desc)
               for i in list_interfaces()[:limit]]
    best = local_ip()
    targets.append(HostTarget("0.0.0.0", "127.0.0.1", best, "Все сети сразу",
                              f"слушать на всех адаптерах · друзьям: {best}"))
    targets.append(HostTarget("127.0.0.1", "127.0.0.1", "127.0.0.1",
                              "Только этот компьютер",
                              "для проверки: два окна игры на одном ПК"))
    return targets


def friendly_error(e: Exception) -> str:
    """Понятный текст вместо сырого исключения сокета."""
    if isinstance(e, ConnectionRefusedError):
        return "хост не принимает подключения (игра создана? адрес верный?)"
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "хост не отвечает (другая сеть? файрвол?)"
    if isinstance(e, socket.gaierror):
        return "неверный адрес"
    if type(e) in (ConnectionError, ValueError):     # наши собственные сообщения
        return str(e)
    if isinstance(e, OSError):
        return f"сеть недоступна ({e})"
    return str(e) or e.__class__.__name__
