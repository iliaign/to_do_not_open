"""
clipboard.py — буфер обмена без зависаний окна.

Быстрый путь (pygame.scrap) выполняется сразу в главном потоке. Если он не сработал,
медленные запасные способы (системные утилиты, tkinter) идут в фоновом потоке,
а интерфейс просто проверяет job.done каждый кадр.
"""

from __future__ import annotations

import subprocess
import sys
import threading

import pygame


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


def first_line(raw) -> str:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "ignore")
    lines = (raw or "").replace("\x00", "").strip().splitlines()
    return lines[0].strip() if lines else ""


def _scrap_ready() -> bool:
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        return True
    except Exception:
        return False


class _Job:
    """Быстрая попытка сразу, иначе медленная в потоке. Результат читать после job.done."""

    def __init__(self):
        self.done = False
        if self._fast():
            self.done = True
        else:
            threading.Thread(target=self._thread, daemon=True).start()

    def _thread(self) -> None:
        try:
            self._slow()
        except Exception:
            pass
        self.done = True

    def _fast(self) -> bool:
        raise NotImplementedError

    def _slow(self) -> None:
        raise NotImplementedError


class CopyJob(_Job):
    def __init__(self, text: str):
        self.text = text
        self.ok = False
        super().__init__()

    def _fast(self) -> bool:
        try:
            if _scrap_ready():
                pygame.scrap.put_text(self.text)
                if first_line(pygame.scrap.get_text()) == self.text:   # проверяем, что легло
                    self.ok = True
                    return True
        except Exception:
            pass
        return False

    def _slow(self) -> None:
        for cmd in _clip_cmds(write=True):
            try:
                if _run(cmd, data=self.text.encode("utf-8")).returncode == 0:
                    self.ok = True
                    return
            except Exception:
                continue
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(self.text)
        root.update()
        root.destroy()
        self.ok = True


class PasteJob(_Job):
    def __init__(self):
        self.text = ""
        super().__init__()

    def _fast(self) -> bool:
        try:
            if _scrap_ready():
                text = first_line(pygame.scrap.get_text())
                if text:
                    self.text = text
                    return True
        except Exception:
            pass
        return False

    def _slow(self) -> None:
        for cmd in _clip_cmds(write=False):
            try:
                res = _run(cmd, want_output=True)
                if res.returncode == 0 and res.stdout:
                    self.text = first_line(res.stdout)
                    if self.text:
                        return
            except Exception:
                continue
        import tkinter
        root = tkinter.Tk()
        root.withdraw()
        try:
            self.text = first_line(root.clipboard_get())
        finally:
            root.destroy()
