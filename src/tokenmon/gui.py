from __future__ import annotations

import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk

from PIL import ImageTk

from . import __version__, tray
from .adapters import adapters, connect_claude
from .core import Store
from .gauge import (Spring, animations_enabled, bar_image, card_image, dpi_aware, mix, pick_font,
                    render_gauge, work_area)
from .periods import period_report, when

POLL_SECONDS = 2
PROVIDERS = (("claude", "Claude Max"), ("codex", "Codex Pro"))

COLORS = {
    "bg": "#0B0D10",
    "surface": "#14181D",
    "surface_2": "#1B2128",
    "border": "#2A323B",
    "text": "#F3F6F8",
    "secondary": "#9AA5AF",
    "muted": "#66717C",
    "accent": "#32D583",
    "amber": "#F5B342",
    "track": "#232B33",
    "danger": "#FF6B6B",
}


def compact(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def countdown(resets_at) -> str | None:
    """Time until reset; "" when unknown, None when the window already ended."""
    reset = when(resets_at)
    if reset is None:
        return ""
    seconds = int((reset - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return None
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}h {hours}j" if days else f"{hours}j {rest // 60}m"


def fmt_pct(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return str(round(value))
    return f"{value:.1f}" if value < 10 else f"{value:.0f}"


def level_color(pct: float | None) -> str:
    """Status colour for the weekly gauge: calm green, then amber, then red."""
    if pct is None or pct <= 60:
        return COLORS["accent"]
    return COLORS["amber"] if pct <= 85 else COLORS["danger"]


class Style:
    """Screen scale and the font stack. Segoe UI Variable has optical sizes: Display for big numerals,
    Text for body copy, Small for captions."""

    def __init__(self, root: tk.Misc):
        self.scale = root.winfo_fpixels("1i") / 96
        self.big = pick_font(("Segoe UI Variable Display Semib", "Segoe UI Semibold"))
        self.head = pick_font(("Segoe UI Variable Text Semibold", "Segoe UI Semibold"))
        self.body = pick_font(("Segoe UI Variable Text", "Segoe UI"))
        self.small = pick_font(("Segoe UI Variable Small", "Segoe UI"))
        self._fonts: dict[tuple, tkfont.Font] = {}

    def S(self, px: float) -> int:
        return round(px * self.scale)

    def font(self, family: str, size: int) -> tkfont.Font:
        key = (family, size)
        if key not in self._fonts:
            self._fonts[key] = tkfont.Font(family=family, size=size)
        return self._fonts[key]


class GaugeView:
    """One gauge on a panel canvas: an image item plus the numeral in its middle, driven by springs."""

    def __init__(self, panel: "ProviderPanel", cx: int, top: int, size: int):
        ui = panel.ui
        self.panel, self.ui, self.cx, self.top, self.size = panel, ui, cx, top, size
        self.value, self.today = Spring(), Spring()
        self.pct: float | None = None
        self.color = self.today_color = COLORS["accent"]
        self.num_font = ui.font(ui.big, 30)
        self.pct_font = ui.font(ui.head, 13)
        self.photo = None
        self._key = None
        c = panel
        self.image_id = c.create_image(cx, top + size // 2, anchor="center")
        self.num_id = c.create_text(cx, 0, anchor="s", fill=COLORS["text"], font=self.num_font, text="")
        self.pct_id = c.create_text(cx, 0, anchor="s", fill=COLORS["secondary"], font=self.pct_font, text="")
        self.render()

    @property
    def settled(self) -> bool:
        return self.value.settled and self.today.settled

    def set(self, pct: float | None, today: float | None, color: str, jump: bool) -> None:
        key = (pct, today, color)
        if key == self._key and not jump:
            return  # polled every 2 seconds: redraw only when something actually changed
        self._key = key
        self.pct, self.color = pct, color
        self.today_color = mix(color, "#FFFFFF", 0.4)
        for spring, target in ((self.value, pct or 0.0), (self.today, today or 0.0)):
            if jump:
                spring.jump(target)
            else:
                spring.set_target(target)  # keeps position and velocity: a change mid-flight bends, never restarts
        if jump or self.settled:
            self.render()  # a moving gauge is drawn by the panel's animation loop

    def step(self, dt: float) -> None:
        self.value.step(dt)
        self.today.step(dt)
        self.render(fast=not self.settled)

    def render(self, fast: bool = False) -> None:
        image = render_gauge(self.size, self.value.value, self.today.value, self.color, self.today_color,
                             COLORS["track"], ss=2 if fast else 3)
        c, ui = self.panel, self.ui
        if self.photo is None:
            self.photo = ImageTk.PhotoImage(image, master=c)
            c.itemconfigure(self.image_id, image=self.photo)
        else:
            self.photo.paste(image)  # reuse the Tk image: no allocation per frame
        has = self.pct is not None
        text = fmt_pct(self.value.value) if has else "—"
        number, sign, gap = self.num_font.measure(text), self.pct_font.measure("%"), ui.S(2)
        x = self.cx - (number + (sign + gap if has else 0)) / 2
        bottom = self.top + self.size / 2 + self.num_font.metrics("linespace") / 2
        c.coords(self.num_id, x + number / 2, bottom)
        c.itemconfigure(self.num_id, text=text, fill=COLORS["text"] if has else COLORS["muted"])
        # Sit the "%" on the numeral's baseline: bottoms differ by the difference of the two descents.
        c.coords(self.pct_id, x + number + gap + sign / 2,
                 bottom - (self.num_font.metrics("descent") - self.pct_font.metrics("descent")))
        c.itemconfigure(self.pct_id, text="%" if has else "")


class ProviderPanel(tk.Canvas):
    """A provider card: today and this-week gauges (this device), their token counts, and the account's weekly bar."""

    HEIGHT = 400

    def __init__(self, master, ui: Style, title: str, provider: str, animate: bool):
        super().__init__(master, bg=COLORS["bg"], highlightthickness=0, height=ui.S(self.HEIGHT))
        self.ui, self.title, self.provider, self.animate = ui, title, provider, animate
        self.report: dict | None = None
        self.note = ""
        self.gauges: list[GaugeView] = []
        self._width = 0
        self._built = False
        self._anim: str | None = None
        self._resize: str | None = None
        self._bar_key = None
        self._last = 0.0
        self.bind("<Configure>", self._on_configure)

    def _on_configure(self, event) -> None:
        if event.width == self._width:
            return
        self._width = event.width
        if self._resize:
            self.after_cancel(self._resize)
        self._resize = self.after(60, self._build)

    def _build(self) -> None:
        self._resize = None
        if self._anim:
            self.after_cancel(self._anim)
            self._anim = None
        self.delete("all")
        self._bar_key = None  # the canvas items are new, so the bar image must be attached again
        ui, S, w = self.ui, self.ui.S, self._width
        h = S(self.HEIGHT)
        self.card = ImageTk.PhotoImage(card_image(w, h, S(18), COLORS["surface"], COLORS["border"],
                                                  COLORS["surface_2"]), master=self)
        self.create_image(0, 0, image=self.card, anchor="nw")
        pad = S(28)
        title_font = ui.font(ui.head, 14)
        self.create_text(pad, S(30), anchor="w", text=self.title, fill=COLORS["text"], font=title_font)
        # Which device these gauges describe, and how much of the account's usage is estimated to be its own.
        self.device_text = self.create_text(pad + title_font.measure(self.title) + S(12), S(31), anchor="w",
                                            text="", fill=COLORS["muted"], font=ui.font(ui.small, 9))
        size, top = S(164), S(54)
        self.gauges = []
        self.columns = []
        for cx in (w * 0.27, w * 0.73):
            cx = round(cx)
            self.gauges.append(GaugeView(self, cx, top, size))
            y1 = top + size + S(10)
            # Four stacked lines, each box clear of the next: label, caption, token count, exact count.
            self.columns.append({
                "label": self.create_text(cx, y1, anchor="n", fill=COLORS["text"], text="",
                                          font=ui.font(ui.head, 12)),
                "note": self.create_text(cx, y1 + S(22), anchor="n", fill=COLORS["muted"], text="",
                                         font=ui.font(ui.small, 9)),
                "tokens": self.create_text(cx, y1 + S(68), anchor="s", fill=COLORS["text"], text="",
                                           font=ui.font(ui.big, 17)),
                "unit": self.create_text(cx, y1 + S(68), anchor="s", fill=COLORS["secondary"], text="",
                                         font=ui.font(ui.body, 10)),
                "exact": self.create_text(cx, y1 + S(70), anchor="n", fill=COLORS["muted"], text="",
                                          font=ui.font(ui.small, 9)),
                "cx": cx, "y": y1 + S(68),
            })
        line = top + size + S(10) + S(98)
        self.create_line(pad, line, w - pad, line, fill=COLORS["border"])
        self.account_label = self.create_text(pad, line + S(22), anchor="w", text="Weekly · total akun",
                                              fill=COLORS["text"], font=ui.font(ui.head, 11))
        self.account_value = self.create_text(w - pad, line + S(22), anchor="e", text="",
                                              fill=COLORS["secondary"], font=ui.font(ui.body, 10))
        self.account_bar = self.create_image(pad, line + S(40), anchor="nw")
        self.account_caption = self.create_text(pad, line + S(52), anchor="nw", text="", fill=COLORS["danger"],
                                                font=ui.font(ui.small, 9))  # only used to say why data is missing
        self.bar_size = (w - 2 * pad, S(6))
        self._built = True
        self.apply(jump=True)

    def update_report(self, report: dict, note: str) -> None:
        self.report, self.note = report, note
        if self._built:
            self.apply(jump=not self.animate)

    def apply(self, jump: bool) -> None:
        if not self.report:
            return
        ui, rep = self.ui, self.report
        day, week = rep["day"], rep["week"]
        reset = countdown(week["reset"]) if week["reset"] else None
        self.gauges[0].set(day["pct"], None, COLORS["accent"], jump)
        self.gauges[1].set(week["pct"], day["pct"] if week["pct"] is not None else None,
                           level_color(week["pct"]), jump)
        by_credits = rep["device"]["basis"] == "kredit"
        base = "% dari kapasitas mingguan" if by_credits else "% dari total akun"
        for column, label, note, data in (
                (self.columns[0], "Hari ini", base, day),
                (self.columns[1], "Minggu ini", base + (f" · reset {reset}" if reset else ""), week)):
            tokens = data["tokens"]
            number = compact(tokens)
            self.itemconfigure(column["label"], text=label)
            self.itemconfigure(column["note"], text=note)
            self.itemconfigure(column["tokens"], text=number)
            self.itemconfigure(column["unit"], text="token")
            account = f" · akun {data['official']:.0f}%" if data["official"] is not None else ""
            self.itemconfigure(column["exact"], text=f"{tokens:,} token{account}")
            # Centre "12.4M token" as one unit on a shared baseline: bottoms differ by the descents.
            big, small = ui.font(ui.big, 17), ui.font(ui.body, 10)
            gap, numeral, unit = ui.S(4), big.measure(number), small.measure("token")
            start = column["cx"] - (numeral + gap + unit) / 2
            self.coords(column["tokens"], start + numeral / 2, column["y"])
            self.coords(column["unit"], start + numeral + gap + unit / 2,
                        column["y"] - (big.metrics("descent") - small.metrics("descent")))
        device = rep["device"]
        if by_credits:
            scope = f"{device['name']} · {compact(round(device['credits']))} / {compact(device['capacity'])} kredit"
        elif self.provider == "claude":
            scope = f"{device['name']} · paket tidak dikenal, persen akun"
        else:
            scope = f"{device['name']} · persen akun (Codex tanpa tarif kredit)"
        self.itemconfigure(self.device_text, text=scope)
        # Weekly usage of the whole account (official figure, all devices), unlike the gauges above.
        total = week["official"]
        value = f"{total:.0f}% terpakai" + (f" · reset {reset}" if reset else "") if total is not None else "Menunggu data"
        self.itemconfigure(self.account_value, text=value)
        bar_key = (self.bar_size, total)
        if bar_key != self._bar_key:
            self._bar_key = bar_key
            self.bar = ImageTk.PhotoImage(bar_image(*self.bar_size, total or 0.0, level_color(total),
                                                    COLORS["track"]), master=self)
            self.itemconfigure(self.account_bar, image=self.bar)
        # Say why the official figure is missing, if it is.
        self.itemconfigure(self.account_caption, text=self.note if self.note and total is None else "")
        self.kick()

    def kick(self) -> None:
        if self._anim is None and any(not g.settled for g in self.gauges):
            self._last = time.perf_counter()
            self._anim = self.after(16, self._tick)

    def _tick(self) -> None:
        now = time.perf_counter()
        dt, self._last = min(0.05, now - self._last), now
        busy = False
        for gauge in self.gauges:
            if not gauge.settled:
                gauge.step(dt)
                busy = busy or not gauge.settled
        self._anim = self.after(33, self._tick) if busy else None  # about 30 fps, only while moving


class TokenmonApp:
    def __init__(self, root: tk.Tk, minimized: bool = False):
        self.root = root
        self.ui = Style(root)
        S = self.ui.S
        self.root.title(f"Tokenmon {__version__}")
        # Fit the usable area (screen minus taskbar) and leave room for the title bar and borders.
        left, top, avail_w, avail_h = work_area() or (0, 0, root.winfo_screenwidth(), root.winfo_screenheight())
        width, height = min(S(1120), avail_w - S(40)), min(S(900), avail_h - S(64))
        x = left + (avail_w - width) // 2
        y = top + max(0, (avail_h - height - S(40)) // 2)  # 40 = title bar and borders
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(min(S(940), width), min(S(740), height))
        self.root.configure(bg=COLORS["bg"])
        self.animate = animations_enabled()
        self.wake = threading.Event()  # set to scan right now instead of waiting out the poll interval
        self.stopping = threading.Event()
        self.filled = False
        self.note = ""
        self._configure_styles()
        self._build()
        self.tray = tray.start_tray(self.show, self.quit)
        # Closing the window keeps tracking alive in the tray; without a tray it really closes.
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw if self.tray else self.quit)
        if minimized and self.tray:
            self.root.withdraw()
        threading.Thread(target=self._worker, daemon=True).start()

    def show(self) -> None:
        self.root.after(0, self._show)

    def _show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def quit(self) -> None:
        self.stopping.set()
        self.wake.set()
        if self.tray:
            self.tray.stop()
        self.root.after(0, self.root.destroy)

    def _configure_styles(self) -> None:
        ui, S = self.ui, self.ui.S
        style = ttk.Style()
        style.theme_use("clam")
        bg, surface = COLORS["bg"], COLORS["surface"]
        # clam draws light 3D edges by default; paint every edge colour the same as its background.
        style.configure("Tokenmon.TNotebook", background=bg, borderwidth=0, bordercolor=bg, lightcolor=bg,
                        darkcolor=bg, tabmargins=(0, 0, 0, S(6)))
        style.configure("Tokenmon.TNotebook.Tab", background=bg, foreground=COLORS["secondary"],
                        padding=(S(18), S(10)), font=(ui.head, 10), borderwidth=0, bordercolor=bg,
                        lightcolor=bg, darkcolor=bg, focuscolor=bg)
        style.map("Tokenmon.TNotebook.Tab", expand=[("selected", (0, 0, 0, 0))],  # selected tab must not rise
                  background=[("selected", COLORS["surface_2"])], foreground=[("selected", COLORS["text"])],
                  bordercolor=[("selected", COLORS["surface_2"])], lightcolor=[("selected", COLORS["surface_2"])],
                  darkcolor=[("selected", COLORS["surface_2"])])
        style.layout("Tokenmon.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no outer frame
        style.configure("Tokenmon.Treeview", background=surface, fieldbackground=surface,
                        foreground=COLORS["text"], rowheight=S(34), borderwidth=0, font=(ui.body, 10),
                        bordercolor=surface, lightcolor=surface, darkcolor=surface)
        style.configure("Tokenmon.Treeview.Heading", background=COLORS["surface_2"],
                        foreground=COLORS["secondary"], borderwidth=0, font=(ui.head, 9),
                        padding=(S(8), S(8)), bordercolor=COLORS["surface_2"], lightcolor=COLORS["surface_2"],
                        darkcolor=COLORS["surface_2"])
        style.map("Tokenmon.Treeview", background=[("selected", COLORS["border"])],
                  foreground=[("selected", COLORS["text"])])

    def _button(self, parent, text: str, command) -> tk.Button:
        ui = self.ui
        button = tk.Button(parent, text=text, command=command, cursor="hand2", bg=COLORS["surface_2"],
                           fg=COLORS["text"], activebackground=COLORS["border"], activeforeground=COLORS["text"],
                           relief="flat", borderwidth=0, padx=ui.S(16), pady=ui.S(8), font=(ui.head, 10))
        # Feedback on press, not on release.
        button.bind("<ButtonPress-1>", lambda _e: button.configure(bg=COLORS["border"]))
        button.bind("<ButtonRelease-1>", lambda _e: button.configure(bg=COLORS["surface_2"]))
        return button

    def _build(self) -> None:
        ui, S = self.ui, self.ui.S
        footer = tk.Label(self.root, text=f"Tokenmon v{__version__}  ·  data tersimpan lokal  ·  persen dari "
                          "provider, token dari komputer ini", bg=COLORS["bg"], fg=COLORS["muted"],
                          font=(ui.small, 9), anchor="w", padx=S(32), pady=S(8))
        footer.pack(side="bottom", fill="x")

        header = tk.Frame(self.root, bg=COLORS["bg"], padx=S(32), pady=S(18))
        header.pack(fill="x")
        title_box = tk.Frame(header, bg=COLORS["bg"])
        title_box.pack(side="left")
        title_row = tk.Frame(title_box, bg=COLORS["bg"])
        title_row.pack(anchor="w")
        tk.Label(title_row, text="Tokenmon", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(ui.big, 22)).pack(side="left")
        tk.Label(title_row, text=f"v{__version__}", bg=COLORS["surface_2"], fg=COLORS["secondary"],
                 font=(ui.small, 9), padx=S(8), pady=S(2)).pack(side="left", padx=(S(12), 0), pady=(S(6), 0))
        tk.Label(title_box, text="Claude & Codex · pemakaian lokal, realtime", bg=COLORS["bg"],
                 fg=COLORS["secondary"], font=(ui.body, 10)).pack(anchor="w", pady=(S(2), 0))
        self.refresh_button = self._button(header, "Scan sekarang", self.wake.set)
        self.refresh_button.pack(side="right")
        self.updated = tk.Label(header, text="", bg=COLORS["bg"], fg=COLORS["secondary"], font=(ui.small, 9))
        self.updated.pack(side="right", padx=(0, S(16)))

        self.notebook = ttk.Notebook(self.root, style="Tokenmon.TNotebook")
        self.notebook.pack(fill="both", expand=True, padx=S(24), pady=(0, S(8)))
        self.usage_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.devices_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.workspaces_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.sessions_tab = tk.Frame(self.notebook, bg=COLORS["bg"])
        self.notebook.add(self.usage_tab, text="Usage")
        self.notebook.add(self.devices_tab, text="Devices")
        self.notebook.add(self.workspaces_tab, text="Workspaces")
        self.notebook.add(self.sessions_tab, text="Sessions")
        self._build_usage()
        columns = ("name", "provider", "input", "output", "cache", "total", "share")
        self.device_tree = self._build_table(self.devices_tab, "Usage by device", "Synced devices appear here", columns)
        self.workspace_tree = self._build_table(self.workspaces_tab, "Usage by workspace",
                                                "Raw paths are never shown", columns)
        self.session_tree = self._build_table(self.sessions_tab, "Recent sessions", "Session IDs are shortened", columns)

    def _build_usage(self) -> None:
        ui, S = self.ui, self.ui.S
        content = tk.Frame(self.usage_tab, bg=COLORS["bg"])
        content.pack(fill="both", expand=True, padx=S(4), pady=S(14))
        content.columnconfigure((0, 1), weight=1, uniform="card")
        self.panels = {}
        for column, (provider, title) in enumerate(PROVIDERS):
            panel = ProviderPanel(content, ui, title, provider, self.animate)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, S(8)) if column == 0 else (S(8), 0))
            self.panels[provider] = panel

        self.top_heading = tk.Frame(content, bg=COLORS["bg"])
        self.top_heading.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(S(22), S(8)))
        tk.Label(self.top_heading, text="Top workspaces", bg=COLORS["bg"], fg=COLORS["text"],
                 font=(ui.head, 13)).pack(side="left")
        tk.Label(self.top_heading, text="7 hari terakhir", bg=COLORS["bg"], fg=COLORS["secondary"],
                 font=(ui.small, 9)).pack(side="right")
        self.top_tree = self._tree(content, ("name", "provider", "total", "share"), height=3)
        self.top_tree.grid(row=2, column=0, columnspan=2, sticky="nsew")
        content.rowconfigure(2, weight=1)
        self._top_shown = True
        # The panels only get their final height after the first layout, so watch both sizes.
        for widget in (content, self.panels["claude"]):
            widget.bind("<Configure>", lambda _e: self._fit_top(content), add="+")

    def _fit_top(self, content: tk.Frame) -> None:
        """The table is a bonus: show it only when it fits below the cards, never a heading with no rows."""
        panel = self.panels["claude"]
        room = content.winfo_height() - panel.winfo_height() - self.ui.S(24)
        show = room >= self.ui.S(150)
        if show == self._top_shown:
            return
        self._top_shown = show
        for widget in (self.top_heading, self.top_tree):
            widget.grid() if show else widget.grid_remove()

    def _build_table(self, parent, title: str, subtitle: str, columns):
        ui, S = self.ui, self.ui.S
        wrap = tk.Frame(parent, bg=COLORS["bg"], padx=S(4), pady=S(14))
        wrap.pack(fill="both", expand=True)
        tk.Label(wrap, text=title, bg=COLORS["bg"], fg=COLORS["text"], font=(ui.big, 15)).pack(anchor="w")
        tk.Label(wrap, text=subtitle, bg=COLORS["bg"], fg=COLORS["secondary"],
                 font=(ui.body, 9)).pack(anchor="w", pady=(S(3), S(12)))
        tree = self._tree(wrap, columns)
        tree.pack(fill="both", expand=True)
        return tree

    def _tree(self, parent, columns, height=12):
        S = self.ui.S
        tree = ttk.Treeview(parent, columns=columns, show="headings", style="Tokenmon.Treeview", height=height)
        labels = {"name": "NAME", "provider": "PROVIDER / CLIENT", "input": "INPUT",
                  "output": "OUTPUT", "cache": "CACHE", "total": "TOTAL", "share": "SHARE"}
        for column in columns:
            anchor = "w" if column in ("name", "provider") else "e"
            tree.heading(column, text=labels[column], anchor=anchor)
            width = S(220) if column == "name" else S(150) if column == "provider" else S(95)
            tree.column(column, anchor=anchor, width=width, minwidth=S(70), stretch=column in ("name", "provider"))
        return tree

    def _post(self, callback) -> None:
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):  # window already destroyed while quitting
            pass

    def _first_run(self, store: Store) -> str:
        """Everything that used to be a button: link Claude's status line, register autostart."""
        note = connect_claude()
        if tray.frozen():
            seen = store.db.execute("SELECT 1 FROM settings WHERE key='autostart_init'").fetchone()
            if not seen:
                tray.set_autostart(True)
                store.db.execute("INSERT INTO settings(key,value) VALUES('autostart_init','1')")
                store.db.commit()
            elif tray.autostart_enabled():
                tray.set_autostart(True)  # rewrite the path in case the exe was moved
        return "" if note == "Claude sudah terhubung." else note

    def _worker(self) -> None:
        store = None
        scanners: list = []
        connect_note = ""
        payload, collected = None, 0.0
        while not self.stopping.is_set():
            try:
                if store is None:
                    store = Store()
                    connect_note = self._first_run(store)
                    scanners = adapters(poll_usage=True)  # kept across rounds so `/usage` runs on its own schedule
                before = store.db.total_changes
                for adapter in scanners:
                    adapter.scan(store)
                self.note = next((a.note for a in scanners if getattr(a, "note", "")), connect_note)
                changed = store.db.total_changes != before
                # Aggregation queries only when data changed (or every 30s for the sliding time windows).
                if changed or payload is None or time.monotonic() - collected > 30:
                    payload, collected = self._collect(store), time.monotonic()
                self._post(lambda p=payload, c=changed: self._apply_data(p, c))
            except Exception as exc:
                self._post(lambda m=str(exc): self._show_error(m))
                if store is not None:  # reconnect on the next round
                    store.close()
                    store = None
            self.wake.wait(POLL_SECONDS)
            self.wake.clear()
        if store is not None:
            store.close()

    def _collect(self, store: Store) -> dict:
        return {
            "periods": {provider: period_report(store, provider) for provider, _title in PROVIDERS},
            "devices": store.usage(since(7), "device"),
            "workspaces": store.usage(since(7), "workspace"),
            "sessions": store.usage(since(7), "session"),
        }

    def _apply_data(self, data, changed: bool) -> None:
        tip = []
        for provider, title in PROVIDERS:
            report = data["periods"][provider]
            self.panels[provider].update_report(report, self.note if provider == "claude" else "")
            name = title.split()[0]
            day, week = report["day"]["pct"], report["week"]["pct"]
            if week is None:
                tip.append(f"{name} {compact(report['week']['tokens'])} minggu")
            else:
                tip.append(f"{name} " + (f"{day:.0f}% hari · " if day is not None else "") + f"{week:.0f}% minggu")
        if self.tray:
            self.tray.title = " · ".join(tip)
        # Tables are rebuilt only when rows changed, so the selection and scroll position stay put.
        if changed or not self.filled:
            self._fill_tree(self.top_tree, data["workspaces"], compact_view=True, limit=4)
            self._fill_tree(self.device_tree, data["devices"])
            self._fill_tree(self.workspace_tree, data["workspaces"])
            self._fill_tree(self.session_tree, data["sessions"])
            self.filled = True
        self.updated.configure(text="● Live · dicek " + datetime.now().strftime("%H:%M:%S"), fg=COLORS["accent"])

    def _fill_tree(self, tree, rows, compact_view=False, limit=None) -> None:
        tree.delete(*tree.get_children())
        total = sum(row["total_tokens"] for row in rows) or 1
        visible = rows[:limit] if limit else rows
        for row in visible:
            share = row["total_tokens"] / total * 100
            if compact_view:
                values = (row["label"], row["detail"], compact(row["total_tokens"]), f"{share:.1f}%")
            else:
                values = (row["label"], row["detail"], compact(row["input_tokens"]),
                          compact(row["output_tokens"]), compact(row["cache_tokens"]),
                          compact(row["total_tokens"]), f"{share:.1f}%")
            tree.insert("", "end", values=values)

    def _show_error(self, message: str) -> None:
        self.updated.configure(text="Gagal memindai (mencoba lagi): " + message, fg=COLORS["danger"])


def main() -> int:
    minimized = "--minimized" in sys.argv
    if tray.already_running():
        if not minimized:  # an autostart duplicate stays silent
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("Tokenmon", "Tokenmon sudah berjalan. Buka lewat ikon di tray dekat jam.")
            root.destroy()
        return 0
    dpi_aware()
    root = tk.Tk()
    TokenmonApp(root, minimized)
    root.mainloop()
    return 0
