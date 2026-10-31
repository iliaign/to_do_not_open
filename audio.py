"""
audio.py — звуки. Синтезируются один раз и кэшируются в .wav, дальше грузятся мгновенно.

Кэш лежит в <папка игры>/cache/sfx_v2 (если папка недоступна для записи — во временной).
Файлы всегда mono 44100 Hz, 16 бит; pygame сам приводит их к формату микшера.
"""

from __future__ import annotations

import math
import os
import random
import sys
import tempfile
import wave
from array import array

import pygame

import settings
from core import clamp

RATE = 44100
CACHE_VERSION = "v2"


# ------------------------------------------------------------ синтез
def _tone(f0, f1, dur, vol=0.35, shape="sine"):
    n = int(RATE * dur)
    ratio = f1 / f0
    phase = 0.0
    out = []
    for i in range(n):
        t = i / RATE
        phase += 2 * math.pi * f0 * ratio ** (t / dur) / RATE
        if shape == "square":
            s = 1.0 if math.sin(phase) >= 0 else -1.0
        elif shape == "saw":
            s = 2 * ((phase / (2 * math.pi)) % 1.0) - 1
        else:
            s = math.sin(phase)
        out.append(s * min(1.0, t / 0.01) * math.exp(-3.0 * t) * vol)
    return out


def _noise(dur=0.08, vol=0.4):
    n = int(RATE * dur)
    return [random.uniform(-1, 1) * math.exp(-18.0 * i / RATE) * vol for i in range(n)]


def _kick(up: bool):
    j = random.uniform(0.85, 1.25)
    hit = _noise(0.06, 0.35)
    if up:
        a = _tone(220 * j, 880 * j, 0.18, 0.30, "square")
        b = _tone(300 * j, 1200 * j, 0.22, 0.25, "sine")
    else:
        a = _tone(700 * j, 90 * j, 0.28, 0.30, "saw")
        b = _tone(160 * j, 55 * j, 0.18, 0.25, "sine")
    return hit + a + b


def _boost():
    base = 260 * random.uniform(0.9, 1.15)
    notes = []
    for m in (1.0, 1.26, 1.5, 2.0):
        notes += _tone(base * m, base * m * 1.01, 0.07, 0.22, "square")
    return notes


def _newp():
    return _tone(500, 1500, 0.08, 0.25, "sine") + _tone(1400, 2200, 0.10, 0.20, "square")


def _win():
    notes = []
    for f in (523, 659, 784):
        notes += _tone(f, f * 1.003, 0.13, 0.28, "square")
    notes += _tone(1046, 1050, 0.45, 0.30, "square")
    return notes


# ------------------------------------------------------------ кэш
def _cache_dirs():
    yield os.path.join(settings.app_dir(), "cache", "sfx_" + CACHE_VERSION)
    yield os.path.join(tempfile.gettempdir(), "mfti_kick_sfx_" + CACHE_VERSION)


def _write_wav(path: str, samples) -> None:
    data = array("h", (int(clamp(s, -1.0, 1.0) * 32767) for s in samples))
    if sys.byteorder == "big":
        data.byteswap()
    tmp = path + ".tmp"
    with wave.open(tmp, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(data.tobytes())
    os.replace(tmp, path)               # файл появляется целиком или не появляется


class Sfx:
    def __init__(self):
        self.ok = False
        self.kick_up, self.kick_down = [], []
        self.boost = self.newp = self.win = None
        try:
            if pygame.mixer.get_init() is None:
                pygame.mixer.init(44100, -16, 1, 512)
            self._load()
            self.ok = True
        except Exception:
            self.ok = False

    def _load(self) -> None:
        last = None
        for folder in _cache_dirs():
            try:
                self._load_from(folder)
                return
            except Exception as e:
                last = e
        raise last or RuntimeError("нет папки для кэша звуков")

    def _load_from(self, folder: str) -> None:
        os.makedirs(folder, exist_ok=True)

        def get(name, make):
            path = os.path.join(folder, name + ".wav")
            for _ in range(2):
                if not os.path.exists(path):
                    _write_wav(path, make())
                try:
                    return pygame.mixer.Sound(path)
                except pygame.error:
                    os.remove(path)                 # битый файл — пересоздадим
            raise RuntimeError("не удалось загрузить звук " + name)

        self.kick_up = [get(f"kick_up_{i}", lambda: _kick(True)) for i in range(3)]
        self.kick_down = [get(f"kick_down_{i}", lambda: _kick(False)) for i in range(3)]
        self.boost = get("boost", _boost)
        self.newp = get("newp", _newp)
        self.win = get("win", _win)

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
        elif kind == "win":
            self.win.play()
