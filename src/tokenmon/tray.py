from __future__ import annotations

import ctypes
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Tokenmon"
ERROR_ALREADY_EXISTS = 183

_mutex = None  # kept referenced for the life of the process


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def already_running() -> bool:
    global _mutex
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _mutex = kernel32.CreateMutexW(None, False, "Local\\Tokenmon")
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def autostart_enabled() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_NAME)
        return True
    except FileNotFoundError:
        return False


def set_autostart(on: bool) -> None:
    """Only meaningful for the packaged exe: from source, sys.executable would be python.exe."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, f'"{sys.executable}" --minimized')
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


def start_tray(show, quit_app):
    """Start the notification-area icon on its own thread; returns the icon, or None without pystray/Pillow."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(50, 213, 131, 255))
    draw.ellipse((20, 20, 44, 44), fill=(11, 13, 16, 255))

    def toggle_autostart(icon, item):
        set_autostart(not autostart_enabled())

    from . import __version__

    menu = pystray.Menu(
        pystray.MenuItem(f"Tokenmon v{__version__}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Buka Tokenmon", lambda icon, item: show(), default=True),
        pystray.MenuItem("Mulai bersama Windows", toggle_autostart,
                         checked=lambda item: autostart_enabled(), enabled=frozen()),
        pystray.MenuItem("Keluar", lambda icon, item: quit_app()),
    )
    icon = pystray.Icon(RUN_NAME, image, "Tokenmon", menu)
    icon.run_detached()
    return icon
