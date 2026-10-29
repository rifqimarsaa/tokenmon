"""Drawing helpers for the dashboard: anti-aliased gauges and cards (Pillow), springs, DPI and fonts."""
from __future__ import annotations

import ctypes
import math
import tkinter.font as tkfont
from ctypes import wintypes

from PIL import Image, ImageDraw

SWEEP = 270  # the arc opens at the bottom, like a speedometer
START = 135


def dpi_aware() -> None:
    """Ask Windows not to bitmap-stretch the window on 125%/150% displays (call before creating Tk)."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def work_area() -> tuple[int, int, int, int] | None:
    """Usable screen rectangle without the taskbar as (left, top, width, height), or None if unknown."""
    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(rect), 0)  # SPI_GETWORKAREA
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except (AttributeError, OSError):
        return None


def animations_enabled() -> bool:
    """False when Windows has "Show animations" turned off (reduced motion)."""
    try:
        flag = ctypes.c_int(1)
        ctypes.windll.user32.SystemParametersInfoW(0x1042, 0, ctypes.byref(flag), 0)  # SPI_GETCLIENTAREAANIMATION
        return bool(flag.value)
    except (AttributeError, OSError):
        return True


def pick_font(candidates: tuple[str, ...], fallback: str = "Segoe UI") -> str:
    families = set(tkfont.families())
    return next((name for name in candidates if name in families), fallback)


def mix(a: str, b: str, amount: float) -> str:
    """Blend hex colour `a` towards `b` by `amount` (0..1)."""
    pa = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    pb = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * amount):02X}" for x, y in zip(pa, pb))


def _rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16), alpha


class Spring:
    """Critically damped spring (damping 1.0, `response` seconds), the model Apple uses for UI motion.

    Retargeting keeps the current position *and* velocity, so a value that changes mid-flight bends
    smoothly instead of restarting.
    """

    def __init__(self, value: float = 0.0, response: float = 0.5):
        self.value = self.target = value
        self.velocity = 0.0
        self.omega = 2 * math.pi / response

    @property
    def settled(self) -> bool:
        return abs(self.target - self.value) < 0.05 and abs(self.velocity) < 0.5

    def set_target(self, target: float) -> None:
        self.target = target

    def jump(self, value: float) -> None:
        self.value = self.target = value
        self.velocity = 0.0

    def step(self, dt: float) -> None:
        steps = max(1, int(dt / 0.004))
        h = dt / steps
        for _ in range(steps):  # semi-implicit Euler in small steps stays stable at 30 fps
            accel = self.omega ** 2 * (self.target - self.value) - 2 * self.omega * self.velocity
            self.velocity += accel * h
            self.value += self.velocity * h
        if self.settled:
            self.jump(self.target)


def render_gauge(size: int, value: float, today: float, color: str, today_color: str, track: str,
                 ss: int = 3) -> Image.Image:
    """Gauge image: `value` percent filled, the last `today` percent of it in `today_color`."""
    s = size * ss
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = round(s * 0.075)
    pad = width // 2 + ss * 2
    radius = (s - 2 * pad) / 2 - width / 2  # centre line of the stroke, where the round caps sit
    centre = s / 2

    def cap(degrees: float, fill: tuple) -> None:
        a = math.radians(START + degrees)
        x, y = centre + radius * math.cos(a), centre + radius * math.sin(a)
        draw.ellipse((x - width / 2, y - width / 2, x + width / 2, y + width / 2), fill=fill)

    outer, inner = (s - 2 * pad) / 2, (s - 2 * pad) / 2 - width

    def stroke(a0: float, a1: float, fill: tuple) -> None:
        # One filled ring segment: much cheaper than ImageDraw.arc with a thick line.
        if a1 <= a0:
            return
        steps = max(2, math.ceil((a1 - a0) / 2))
        angles = [math.radians(START + a0 + (a1 - a0) * i / steps) for i in range(steps + 1)]
        points = [(centre + outer * math.cos(a), centre + outer * math.sin(a)) for a in angles]
        points += [(centre + inner * math.cos(a), centre + inner * math.sin(a)) for a in reversed(angles)]
        draw.polygon(points, fill=fill)

    track_fill = _rgba(track)
    stroke(0, SWEEP, track_fill)
    cap(0, track_fill)
    cap(SWEEP, track_fill)

    value = max(0.0, min(100.0, value))
    today = max(0.0, min(value, today))
    if value > 0:
        end = SWEEP * value / 100
        split = SWEEP * (value - today) / 100
        base, recent = _rgba(color), _rgba(today_color)
        stroke(0, split, base)
        stroke(split, end, recent)
        cap(0, base if split > 0 else recent)
        cap(end, recent if today > 0 else base)
    return image.resize((size, size), Image.LANCZOS)


def card_image(w: int, h: int, radius: int, fill: str, border: str, highlight: str, ss: int = 3) -> Image.Image:
    """Rounded card: hairline border plus a lighter 1px edge along the top, like light on a raised surface."""
    image = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), radius * ss, fill=_rgba(border))
    draw.rounded_rectangle((ss, ss, w * ss - 1 - ss, h * ss - 1 - ss), (radius - 1) * ss, fill=_rgba(fill))
    draw.line((radius * ss, ss, (w - radius) * ss, ss), fill=_rgba(highlight), width=ss)
    return image.resize((w, h), Image.LANCZOS)


def bar_image(w: int, h: int, value: float, color: str, track: str, ss: int = 3) -> Image.Image:
    """Slim capsule progress bar."""
    image = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((0, 0, w * ss - 1, h * ss - 1), h * ss / 2, fill=_rgba(track))
    filled = round((w * ss) * max(0.0, min(100.0, value)) / 100)
    if filled > 0:
        draw.rounded_rectangle((0, 0, max(filled, h * ss) - 1, h * ss - 1), h * ss / 2, fill=_rgba(color))
    return image.resize((w, h), Image.LANCZOS)
