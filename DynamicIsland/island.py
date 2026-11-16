"""
Dynamic Island for Windows

- Black pill at the top-center of the screen with blinking eyes
- While a track plays, a small icon (red Spotify / Yandex Music) shows on the right side
- While FL Studio is running, a round bubble island with its icon appears to the right
- Hover the pill to expand it (it stays open for a moment after the mouse leaves; gear >
  "Stay expanded" keeps it open until you collapse it)
- In a browser the island glides to the bottom-left corner, in a game to the top-left corner
  (gear > Auto-move / Edit app lists)
- Only music apps (Spotify, Yandex Music) are shown - videos and social media are ignored
- A separate small island on the right switches the colour theme (custom themes: gear >
  Edit custom themes)
- Gear button > Customize island: hide pages (e.g. the calendar), change their order, ...
- Scroll the mouse wheel DOWN over the expanded island to switch pages:
      1) Music    (cover, title, artist, prev / play-pause / next, progress)
      2) Clock    (digital time with seconds + analog clock + date)
      3) Calendar (today + month grid)
      4) Weather  (current + 3-day forecast, auto-located by IP)
      5) Chat     (free AI chat: animated character on the left, messages + input on the
                   right; her mouth moves while she 'speaks' a reply)
  Scroll UP to go back.
- Reads Spotify (or any media player) through the Windows media API (SMTC)
- Weather: Open-Meteo (free, no API key)
- Adds itself to Windows startup on first run
- sounds/hello.mp3 plays when Windows starts it; sounds/goodbye.mp3 plays when the PC is
  shut down / restarted / logged off, or when you quit the island from its menu
- Gear button (next to the theme button) -> Refresh weather / Play hello sound / Play goodbye sound /
  Start with Windows (toggle) / Quit
"""
import sys
import json
import time
import math
import os
import random
import asyncio
import calendar
import ctypes
import datetime
from functools import lru_cache
import threading
import subprocess
import urllib.error
import urllib.request
import winreg
from ctypes import wintypes

from PySide6.QtCore import (Qt, QRectF, QRect, QPointF, QPoint, QTimer, Signal, QObject,
                            QVariantAnimation, QEasingCurve, QFileInfo,
                            QAbstractNativeEventFilter)
from PySide6.QtGui import (QPainter, QColor, QPainterPath, QFont, QFontMetrics,
                           QPixmap, QGuiApplication, QCursor, QPen, QPolygonF, QIcon,
                           QLinearGradient, QBrush)
from PySide6.QtWidgets import (QApplication, QWidget, QMenu, QFileIconProvider,
                               QLineEdit, QInputDialog, QToolTip, QDialog, QListWidget,
                               QListWidgetItem, QAbstractItemView, QDoubleSpinBox, QCheckBox,
                               QPushButton, QVBoxLayout, QHBoxLayout, QLabel,
                               QDialogButtonBox, QMessageBox)

from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winsdk.windows.storage.streams import Buffer, DataReader, InputStreamOptions

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
# Set a fixed location for the weather, e.g. (50.11, 8.68, "Frankfurt").
# Leave as None to detect the location automatically by IP address.
LOCATION_OVERRIDE = None
WEATHER_REFRESH_SECONDS = 15 * 60

ACCENT_RED = QColor(255, 59, 48)
WHITE = QColor(255, 255, 255)
GRAY = QColor(160, 160, 160)
# --- theme colours (rebound by apply_theme) ---
BODY = QColor(0, 0, 0)               # island body
MUTED = QColor(170, 170, 170)        # secondary text
BUBBLE_AI = QColor(48, 48, 56)
BUBBLE_USER = QColor(10, 132, 255)
CLOUD = QColor(225, 230, 236)
BODY_SOLID = QColor(0, 0, 0)         # body colour without transparency (for icons on top)
GLASS = False                        # True for 'liquid glass' style themes
SPOTIFY_RED = QColor(255, 59, 48)    # brand colour of the little Spotify icon - never themed


def fg_alpha(a):
    c = QColor(WHITE)
    c.setAlpha(int(a))
    return c


def fill_body(p, path, rect):
    """Island body. Normal themes: flat colour. Glass themes: tinted translucent glass with a
    bright top sheen, a diagonal specular streak and a glowing rim."""
    p.fillPath(path, BODY)
    if not GLASS:
        return
    p.save()
    p.setClipPath(path, Qt.IntersectClip)
    sheen = QLinearGradient(rect.topLeft(), rect.bottomLeft())
    sheen.setColorAt(0.0, QColor(255, 255, 255, 80))
    sheen.setColorAt(0.5, QColor(255, 255, 255, 12))
    sheen.setColorAt(1.0, QColor(255, 255, 255, 34))
    p.fillPath(path, QBrush(sheen))
    streak = QLinearGradient(rect.topLeft(), rect.bottomRight())
    streak.setColorAt(0.0, QColor(255, 255, 255, 0))
    streak.setColorAt(0.18, QColor(255, 255, 255, 0))
    streak.setColorAt(0.28, QColor(255, 255, 255, 46))
    streak.setColorAt(0.38, QColor(255, 255, 255, 0))
    streak.setColorAt(1.0, QColor(255, 255, 255, 0))
    p.fillPath(path, QBrush(streak))
    p.restore()
    rim = QLinearGradient(rect.topLeft(), rect.bottomRight())
    rim.setColorAt(0.0, QColor(255, 255, 255, 210))
    rim.setColorAt(0.5, QColor(255, 255, 255, 60))
    rim.setColorAt(1.0, QColor(255, 255, 255, 140))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QBrush(rim), 1.3))
    p.drawPath(path)


# ----------------------------------------------------------------------------
# Media sessions (Spotify etc.) - runs in a separate thread
# ----------------------------------------------------------------------------
class MediaWorker(QObject):
    changed = Signal(object)

    def __init__(self):
        super().__init__()
        self._last_key = None
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.create_task(self._poll())
        self.loop.run_forever()

    @staticmethod
    def _source_of(s):
        sid = s.source_app_user_model_id.lower()
        if "spotify" in sid:
            return "spotify"
        if "yandex" in sid and ("music" in sid or "muzyka" in sid):
            return "yandex"
        return "other"

    async def _pick(self):
        """Only music apps are shown: a playing Spotify / Yandex Music first, then a paused
        one. Videos and social media are never picked up."""
        mgr = await MediaManager.request_async()
        known = []
        for s in list(mgr.get_sessions()):
            src = self._source_of(s)
            if src != "other":
                known.append((src, s))
        for src, s in known:
            if s.get_playback_info().playback_status == PlaybackStatus.PLAYING:
                return src, s
        # anything else (browsers: YouTube, TikTok, Instagram, VK, Twitch...; video players)
        # is ignored on purpose
        if known:
            return known[0]
        return "other", None

    async def _get_session(self):
        return (await self._pick())[1]

    async def _poll(self):
        while True:
            try:
                await self._tick()
            except Exception as e:
                print("media error:", e)
            await asyncio.sleep(0.5)

    async def _read_thumb(self, props):
        ref = props.thumbnail
        if not ref:
            return None
        stream = await ref.open_read_async()
        size = stream.size
        if not size:
            return None
        buf = Buffer(size)
        await stream.read_async(buf, buf.capacity, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buf)
        data = bytearray(buf.length)
        reader.read_bytes(data)
        return bytes(data)

    async def _tick(self):
        src, s = await self._pick()
        if s is None:
            if self._last_key != "none":
                self._last_key = "none"
                self.changed.emit({"none": True})
            return

        props = await s.try_get_media_properties_async()
        info = s.get_playback_info()
        tl = s.get_timeline_properties()

        title = props.title or ""
        artist = props.artist or ""
        key = (src, title, artist)
        art = None
        art_changed = False
        if key != self._last_key:
            self._last_key = key
            art_changed = True
            try:
                art = await self._read_thumb(props)
            except Exception:
                art = None

        try:
            dur = (tl.end_time - tl.start_time).total_seconds()
            pos = tl.position.total_seconds()
        except Exception:
            dur, pos = 0.0, 0.0

        self.changed.emit({
            "none": False,
            "source": src,
            "title": title,
            "artist": artist,
            "playing": info.playback_status == PlaybackStatus.PLAYING,
            "pos": pos,
            "dur": dur,
            "art": art,
            "art_changed": art_changed,
        })

    def command(self, name):
        asyncio.run_coroutine_threadsafe(self._cmd(name), self.loop)

    async def _cmd(self, name):
        s = await self._get_session()
        if s is None:
            return
        if name == "toggle":
            await s.try_toggle_play_pause_async()
        elif name == "next":
            await s.try_skip_next_async()
        elif name == "prev":
            await s.try_skip_previous_async()


# ----------------------------------------------------------------------------
# Weather (Open-Meteo) - runs in a separate thread
# ----------------------------------------------------------------------------
def _get_json(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "DynamicIsland/2.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _locate():
    if LOCATION_OVERRIDE:
        return LOCATION_OVERRIDE
    try:
        d = _get_json("https://ipapi.co/json/")
        return float(d["latitude"]), float(d["longitude"]), d.get("city", "")
    except Exception:
        d = _get_json("http://ip-api.com/json/?fields=status,city,lat,lon")
        return float(d["lat"]), float(d["lon"]), d.get("city", "")


class WeatherWorker(QObject):
    updated = Signal(object)

    def __init__(self):
        super().__init__()
        self._wake = threading.Event()
        self._cache_loc = None
        threading.Thread(target=self._run, daemon=True).start()

    def refresh(self):
        self._wake.set()

    def _run(self):
        while True:
            delay = WEATHER_REFRESH_SECONDS
            try:
                self.updated.emit(self._fetch())
            except Exception as e:
                print("weather error:", e)
                self.updated.emit({"error": True})
                delay = 60
            self._wake.wait(delay)
            self._wake.clear()

    def _fetch(self):
        if self._cache_loc is None:
            self._cache_loc = _locate()
        lat, lon, city = self._cache_loc
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
               "&daily=weather_code,temperature_2m_max,temperature_2m_min"
               "&timezone=auto&forecast_days=4")
        d = _get_json(url)
        cur = d["current"]
        daily = d["daily"]
        days = []
        for i in range(len(daily["time"])):
            dt = datetime.datetime.strptime(daily["time"][i], "%Y-%m-%d")
            days.append({
                "name": dt.strftime("%a"),
                "code": daily["weather_code"][i],
                "hi": daily["temperature_2m_max"][i],
                "lo": daily["temperature_2m_min"][i],
            })
        return {
            "error": False,
            "city": city,
            "temp": cur["temperature_2m"],
            "feels": cur["apparent_temperature"],
            "code": cur["weather_code"],
            "wind": cur["wind_speed_10m"],
            "days": days,
        }


def weather_kind(code):
    if code == 0:
        return "clear"
    if code in (1, 2):
        return "partly"
    if code == 3:
        return "cloud"
    if code in (45, 48):
        return "fog"
    if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
        return "rain"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (95, 96, 99):
        return "storm"
    return "cloud"


WEATHER_TEXT = {
    "clear": "Clear sky", "partly": "Partly cloudy", "cloud": "Cloudy",
    "fog": "Fog", "rain": "Rain", "snow": "Snow", "storm": "Thunderstorm",
}


# ----------------------------------------------------------------------------
# Drawing helpers
# ----------------------------------------------------------------------------
@lru_cache(maxsize=64)
def _font(px, bold):
    f = QFont("Segoe UI")
    f.setPixelSize(px)
    f.setWeight(QFont.Bold if bold else QFont.Normal)
    return f


def draw_text(p, x, y, w, h, s, px, color, bold=False,
              align=Qt.AlignLeft | Qt.AlignVCenter):
    p.setFont(_font(px, bold))
    p.setPen(color)
    p.drawText(QRect(int(x), int(y), int(w), int(h)), align, s)


def draw_spotify_icon(p, cx, cy, d, color):
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
    pen = QPen(QColor(0, 0, 0))
    pen.setCapStyle(Qt.RoundCap)
    p.setBrush(Qt.NoBrush)
    for dy, half, bow, wd in ((-0.15, 0.29, 0.14, 0.11),
                              (0.00, 0.23, 0.11, 0.095),
                              (0.14, 0.17, 0.08, 0.08)):
        pen.setWidthF(max(1.0, d * wd))
        p.setPen(pen)
        path = QPainterPath()
        path.moveTo(cx - d * half, cy + d * dy)
        path.quadTo(cx, cy + d * (dy - bow), cx + d * half, cy + d * dy)
        p.drawPath(path)


def draw_cloud(p, cx, cy, s, color):
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawRoundedRect(QRectF(cx - 0.5 * s, cy - 0.02 * s, s, 0.34 * s), 0.17 * s, 0.17 * s)
    p.drawEllipse(QPointF(cx - 0.15 * s, cy - 0.02 * s), 0.2 * s, 0.2 * s)
    p.drawEllipse(QPointF(cx + 0.1 * s, cy - 0.1 * s), 0.26 * s, 0.26 * s)


def draw_sun(p, cx, cy, r, color):
    p.setPen(Qt.NoPen)
    p.setBrush(color)
    p.drawEllipse(QPointF(cx, cy), r, r)
    pen = QPen(color)
    pen.setWidthF(max(1.2, r * 0.2))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    for i in range(8):
        a = i * math.pi / 4
        p.drawLine(QPointF(cx + math.cos(a) * r * 1.4, cy + math.sin(a) * r * 1.4),
                   QPointF(cx + math.cos(a) * r * 1.85, cy + math.sin(a) * r * 1.85))


def draw_weather_icon(p, cx, cy, s, kind):
    sun = QColor(255, 204, 64)
    cloud = CLOUD
    p.save()
    if kind == "clear":
        draw_sun(p, cx, cy, 0.24 * s, sun)
    elif kind == "partly":
        draw_sun(p, cx - 0.17 * s, cy - 0.17 * s, 0.16 * s, sun)
        draw_cloud(p, cx + 0.07 * s, cy + 0.1 * s, 0.82 * s, cloud)
    elif kind == "cloud":
        draw_cloud(p, cx, cy, s, cloud)
    elif kind == "fog":
        pen = QPen(MUTED)
        pen.setWidthF(max(1.5, s * 0.07))
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        for i, (x0, x1) in enumerate(((-0.38, 0.38), (-0.3, 0.42), (-0.38, 0.3))):
            y = cy - 0.2 * s + i * 0.2 * s
            p.drawLine(QPointF(cx + x0 * s, y), QPointF(cx + x1 * s, y))
    elif kind in ("rain", "snow", "storm"):
        draw_cloud(p, cx, cy - 0.12 * s, 0.9 * s,
                   QColor(150, 156, 168) if kind == "storm" else cloud)
        if kind == "rain":
            pen = QPen(QColor(90, 170, 255))
            pen.setWidthF(max(1.2, s * 0.06))
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            for dx in (-0.22, 0.0, 0.22):
                p.drawLine(QPointF(cx + dx * s, cy + 0.26 * s),
                           QPointF(cx + (dx - 0.05) * s, cy + 0.42 * s))
        elif kind == "snow":
            p.setPen(Qt.NoPen)
            p.setBrush(WHITE)
            for dx, dy in ((-0.22, 0.3), (0.0, 0.4), (0.22, 0.3)):
                p.drawEllipse(QPointF(cx + dx * s, cy + dy * s), 0.045 * s, 0.045 * s)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(sun)
            bolt = QPolygonF([
                QPointF(cx + 0.04 * s, cy + 0.18 * s),
                QPointF(cx - 0.1 * s, cy + 0.34 * s),
                QPointF(cx - 0.01 * s, cy + 0.34 * s),
                QPointF(cx - 0.06 * s, cy + 0.5 * s),
                QPointF(cx + 0.1 * s, cy + 0.3 * s),
                QPointF(cx + 0.01 * s, cy + 0.3 * s),
            ])
            p.drawPolygon(bolt)
    p.restore()


def draw_yandex_icon(p, cx, cy, d):
    """Approximation of the Yandex Music icon: yellow circle with a black note."""
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(255, 204, 0))
    p.drawEllipse(QPointF(cx, cy), d / 2, d / 2)
    p.setBrush(QColor(0, 0, 0))
    p.drawEllipse(QPointF(cx - 0.1 * d, cy + 0.17 * d), 0.14 * d, 0.1 * d)
    pen = QPen(QColor(0, 0, 0))
    pen.setWidthF(max(1.2, d * 0.09))
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(cx + 0.03 * d, cy + 0.14 * d), QPointF(cx + 0.03 * d, cy - 0.26 * d))
    p.drawLine(QPointF(cx + 0.03 * d, cy - 0.26 * d), QPointF(cx + 0.22 * d, cy - 0.14 * d))


# ----------------------------------------------------------------------------
# FL Studio detection (Windows process list) - runs in a separate thread
# ----------------------------------------------------------------------------
FL_EXE_NAMES = {"fl64.exe", "fl.exe", "fl studio.exe", "fl_studio.exe"}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
_k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
_k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32FirstW.restype = wintypes.BOOL
_k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
_k32.Process32NextW.restype = wintypes.BOOL
_k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_k32.OpenProcess.restype = wintypes.HANDLE
_k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                            ctypes.POINTER(wintypes.DWORD)]
_k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def find_fl_studio():
    """Returns the exe path of a running FL Studio ('' if unknown), or None."""
    snap = _k32.CreateToolhelp32Snapshot(0x2, 0)
    if not snap or snap == _INVALID_HANDLE:
        return None
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() in FL_EXE_NAMES:
                path = ""
                h = _k32.OpenProcess(0x1000, False, entry.th32ProcessID)
                if h:
                    buf = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(1024)
                    if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                        path = buf.value
                    _k32.CloseHandle(h)
                return path
            ok = _k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        _k32.CloseHandle(snap)
    return None


class AppWatcher(QObject):
    fl_changed = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._last = "unset"
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            try:
                path = find_fl_studio()
                if path != self._last:
                    self._last = path
                    self.fl_changed.emit(path is not None, path or "")
            except Exception as e:
                print("watcher error:", e)
            time.sleep(2.0)


class Bubble(QWidget):
    """Small round island next to the main one, shown while FL Studio is open."""
    SIZE = 34
    WIN = 42

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.resize(self.WIN, self.WIN)
        self.icon = None
        self.s = 0.0
        self.target = 0
        self._geom = None
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_s)
        self.anim.finished.connect(self._on_done)

    def set_running(self, running, pixmap):
        self.icon = pixmap
        target = 1 if running else 0
        if target == self.target:
            self.update()
            return
        self.target = target
        if target:
            self.show()
        self.anim.stop()
        self.anim.setStartValue(self.s)
        self.anim.setEndValue(float(target))
        self.anim.setDuration(450 if target else 250)
        self.anim.setEasingCurve(QEasingCurve.OutBack if target else QEasingCurve.InCubic)
        self.anim.start()

    def _on_s(self, v):
        self.s = float(v)
        self.update()

    def _on_done(self):
        if self.target == 0:
            self.hide()

    def follow(self, island_geom, shift=1.0):
        """Stay vertically centered on the collapsed island, just right of it
        (and right of the theme island while that one is shown: shift 0..1)."""
        self._geom = island_geom
        cx = island_geom.x() + island_geom.width() + 8 + 42 * shift + self.SIZE / 2
        cy = island_geom.y() + H0 / 2
        self.move(int(cx - self.WIN / 2), int(cy - self.WIN / 2))

    def paintEvent(self, e):
        s = max(self.s, 0.0)
        if s <= 0.01:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        c = QPointF(self.WIN / 2, self.WIN / 2)
        d = self.SIZE * s
        circ = QPainterPath()
        circ.addEllipse(c, d / 2, d / 2)
        fill_body(p, circ, QRectF(c.x() - d / 2, c.y() - d / 2, d, d))
        p.setPen(fg_alpha(22))
        p.setBrush(Qt.NoBrush)
        p.drawPath(circ)
        p.setOpacity(min(1.0, s))
        isz = d * 0.62
        if self.icon is not None and not self.icon.isNull():
            scaled = self.icon.scaled(int(isz * 2), int(isz * 2), Qt.KeepAspectRatio,
                                      Qt.SmoothTransformation)
            target = QRectF(c.x() - isz / 2, c.y() - isz / 2, isz, isz)
            p.drawPixmap(target, scaled, QRectF(scaled.rect()))
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 150, 30))
            p.drawRoundedRect(QRectF(c.x() - isz / 2, c.y() - isz / 2, isz, isz), 5, 5)
            f = QFont("Segoe UI")
            f.setPixelSize(max(6, int(isz * 0.5)))
            f.setWeight(QFont.Bold)
            p.setFont(f)
            p.setPen(QColor(40, 20, 0))
            p.drawText(QRectF(c.x() - isz / 2, c.y() - isz / 2, isz, isz), Qt.AlignCenter, "FL")


# ----------------------------------------------------------------------------
# Startup: autostart with Windows + startup sounds
# ----------------------------------------------------------------------------
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_KEY = r"Software\DynamicIsland"
APP_NAME = "DynamicIsland"
FROZEN = bool(getattr(sys, "frozen", False))


def autostart_command():
    return f'"{sys.executable}" --startup'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    if not FROZEN:      # only the built exe can register itself
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass
    except OSError as e:
        print("autostart error:", e)


def setup_autostart():
    """First run of the exe: add it to Windows startup. Later runs: keep the
    registered path up to date, unless the user switched autostart off."""
    if not FROZEN:
        return
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, APP_KEY) as k:
            try:
                winreg.QueryValueEx(k, "FirstRunDone")
                first = False
            except FileNotFoundError:
                first = True
                winreg.SetValueEx(k, "FirstRunDone", 0, winreg.REG_DWORD, 1)
        if first or autostart_enabled():
            set_autostart(True)
    except OSError as e:
        print("autostart error:", e)


SOUND_FILES = {"hello": "hello.mp3", "goodbye": "goodbye.mp3"}


def sound_path(kind):
    """hello.mp3 / goodbye.mp3 from a 'sounds' folder next to the exe (so you can
    swap them), otherwise from the folder bundled inside the exe."""
    here = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
    bundled = getattr(sys, "_MEIPASS", here)
    for base in (here, bundled):
        p = os.path.join(base, "sounds", SOUND_FILES[kind])
        if os.path.isfile(p):
            return p
    return None


class StartupSound(QObject):
    """Plays hello.mp3 / goodbye.mp3 (Windows MCI, no extra packages)."""
    done = Signal()

    def __init__(self):
        super().__init__()
        self._active = set()
        self._winmm = ctypes.windll.winmm
        self._winmm.mciSendStringW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR,
                                               wintypes.UINT, wintypes.HANDLE]

    def play_blocking(self, kind):
        path = sound_path(kind)
        if not path:
            return
        alias = f"disland_{kind}"
        if self._winmm.mciSendStringW(f'open "{path}" type mpegvideo alias {alias}',
                                      None, 0, None) != 0:
            return
        self._winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
        self._winmm.mciSendStringW(f"close {alias}", None, 0, None)

    def play(self, kind, delay=0.0, quit_after=False):
        if kind in self._active:
            return
        self._active.add(kind)
        threading.Thread(target=self._run, args=(kind, delay, quit_after), daemon=True).start()

    def _run(self, kind, delay, quit_after):
        try:
            time.sleep(delay)
            self.play_blocking(kind)
        except Exception as e:
            print("sound error:", e)
        finally:
            self._active.discard(kind)
            if quit_after:
                self.done.emit()


# ---- Windows shutdown / log-off: play goodbye.mp3 before the PC goes down ----
WM_QUERYENDSESSION = 0x0011
_user32 = ctypes.windll.user32
_user32.ShutdownBlockReasonCreate.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
_user32.ShutdownBlockReasonCreate.restype = wintypes.BOOL
_user32.ShutdownBlockReasonDestroy.argtypes = [wintypes.HWND]
_user32.ShutdownBlockReasonDestroy.restype = wintypes.BOOL


class ShutdownFilter(QAbstractNativeEventFilter):
    """When Windows starts shutting down, ask it to wait while goodbye.mp3 plays."""

    def __init__(self, sound: StartupSound):
        super().__init__()
        self.sound = sound
        self.active = False

    def nativeEventFilter(self, event_type, message):
        try:
            if bytes(event_type) == b"windows_generic_MSG":
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_QUERYENDSESSION:
                    if not self.active:
                        self.active = True
                        _user32.ShutdownBlockReasonCreate(msg.hWnd, "Saying goodbye...")
                        threading.Thread(target=self._bye, args=(msg.hWnd,),
                                         daemon=True).start()
                    return True, 0          # 0 = "not yet": keeps the shutdown waiting
        except Exception as e:
            print("shutdown filter error:", e)
        return False, 0

    def _bye(self, hwnd):
        try:
            self.sound.play_blocking("goodbye")
        finally:
            _user32.ShutdownBlockReasonDestroy(hwnd)
            os._exit(0)                     # we are done; let Windows continue


# ----------------------------------------------------------------------------
# Free AI chat (OpenAI-compatible chat APIs; no paid key needed)
# ----------------------------------------------------------------------------
AI_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DynamicIsland")
AI_CONFIG_FILE = os.path.join(AI_DIR, "ai.json")

PROVIDERS = {
    "pollinations": {"label": "Pollinations (free, no key)",
                     "base": "https://text.pollinations.ai/openai",
                     "model": "openai", "needs_key": False},
    "ollama": {"label": "Ollama (local on your PC, free)",
               "base": "http://localhost:11434/v1",
               "model": "llama3.2", "needs_key": False},
    "groq": {"label": "Groq (free key)",
             "base": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile", "needs_key": True},
    "gemini": {"label": "Google Gemini (free key)",
               "base": "https://generativelanguage.googleapis.com/v1beta/openai",
               "model": "gemini-2.5-flash", "needs_key": True},
    "openrouter": {"label": "OpenRouter (free models, key)",
                   "base": "https://openrouter.ai/api/v1",
                   "model": "meta-llama/llama-3.3-70b-instruct:free", "needs_key": True},
}
DEFAULT_SYSTEM_PROMPT = (
    "You are a friendly, helpful assistant that lives in a small Dynamic Island widget on the "
    "user's Windows PC. Keep answers short (1-3 sentences) unless the user asks for more detail. "
    "Reply in the language the user writes in."
)


def load_ai_config():
    cfg = {"provider": "pollinations", "api_keys": {}, "models": {}, "system_prompt": ""}
    try:
        with open(AI_CONFIG_FILE, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    for k in ("api_keys", "models"):
        if not isinstance(cfg.get(k), dict):
            cfg[k] = {}
    if cfg.get("provider") not in PROVIDERS:
        cfg["provider"] = "pollinations"
    return cfg


def save_ai_config(cfg):
    os.makedirs(AI_DIR, exist_ok=True)
    with open(AI_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def ask_text(title, label, password=False, default=""):
    dlg = QInputDialog()
    dlg.setWindowFlag(Qt.WindowStaysOnTopHint, True)
    dlg.setWindowTitle(title)
    dlg.setLabelText(label)
    dlg.setTextValue(default)
    if password:
        dlg.setTextEchoMode(QLineEdit.Password)
    dlg.resize(460, 120)
    return dlg.textValue().strip() if dlg.exec() else None


class ChatWorker(QObject):
    reply = Signal(str)
    busy = Signal(bool)

    def __init__(self):
        super().__init__()
        self.history = []
        self.lock = threading.Lock()

    def ask(self, text):
        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def clear(self):
        self.history = []

    def _request(self, provider, cfg):
        info = PROVIDERS[provider]
        key = cfg["api_keys"].get(provider, "")
        model = cfg["models"].get(provider) or info["model"]
        if info["needs_key"] and not key:
            raise RuntimeError(f"{info['label']}: no key set (gear > AI chat > AI provider)")
        system = cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
        body = {"model": model, "max_tokens": 400,
                "messages": [{"role": "system", "content": system}] + self.history}
        headers = {"Content-Type": "application/json", "User-Agent": "DynamicIsland/3.0"}
        if key:
            headers["Authorization"] = "Bearer " + key
        req = urllib.request.Request(info["base"].rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                err = json.loads(e.read().decode("utf-8")).get("error", "")
                detail = err.get("message", "") if isinstance(err, dict) else str(err)
            except Exception:
                pass
            raise RuntimeError(f"{info['label']}: HTTP {e.code} {detail}".strip()[:200])
        except urllib.error.URLError:
            raise RuntimeError(f"{info['label']}: cannot connect")
        content = data["choices"][0]["message"]["content"]
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = (content or "").strip()
        if not content:
            raise RuntimeError(f"{info['label']}: empty answer")
        return content

    def _complete(self, cfg):
        # the chosen provider first, then the other no-key ones as a safety net
        order = [cfg["provider"]] + [p for p in ("pollinations", "ollama") if p != cfg["provider"]]
        first_error = None
        for provider in order:
            try:
                return self._request(provider, cfg)
            except Exception as e:
                if first_error is None:
                    first_error = e
        raise first_error

    def _run(self, text):
        with self.lock:
            self.busy.emit(True)
            self.history.append({"role": "user", "content": text})
            self.history = self.history[-20:]
            try:
                answer = self._complete(load_ai_config())
                self.history.append({"role": "assistant", "content": answer})
                self.reply.emit(answer)
            except Exception as e:
                self.history.pop()          # failed turn: do not keep it
                self.reply.emit(f"({e})")
            finally:
                self.busy.emit(False)


# ----------------------------------------------------------------------------
# Animated character (idle loop: hair sway + breathing) with lip-sync mouth
# ----------------------------------------------------------------------------
def find_character_dir():
    here = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
    bundled = getattr(sys, "_MEIPASS", here)
    for base in (here, bundled):
        d = os.path.join(base, "character")
        if os.path.isfile(os.path.join(d, "meta.json")):
            return d
    return None


def viseme(ch):
    """Mouth shape for one character: 0 closed, 1 slightly open, 2 wide (e/i),
    3 round (o/u), 4 big open (a)."""
    c = ch.lower()
    if c in "bpm\u0431\u043f\u043c":
        return 0
    if c in "a\u0430\u044f":
        return 4
    if c in "ou\u043e\u0443\u0451\u044e":
        return 3
    if c in "eiy\u044d\u0435\u0438\u044b":
        return 2
    if c.isalnum():
        return 1
    return 0


class Character:
    """Pre-rendered loop frames (see tools/make_character.py) + 5 mouth shapes."""

    def __init__(self, folder, dpr):
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            meta = json.load(f)
        self.n = meta["frames"]
        self.loop = float(meta["loop_seconds"])
        k = CARD_W / meta["width"]                 # picture pixels -> logical pixels

        def load(name, w, h):
            pm = QPixmap(os.path.join(folder, name))
            if pm.width() != w or pm.height() != h:
                pm = pm.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            pm.setDevicePixelRatio(dpr)
            return pm

        fw, fh = int(round(CARD_W * dpr)), int(round(CARD_H * dpr))
        self.frames = [load(f"frame_{i:02d}.png", fw, fh) for i in range(self.n)]
        pw, ph = meta["patch_w"], meta["patch_h"]
        mw, mh = int(round(pw * k * dpr)), int(round(ph * k * dpr))
        self.mouths = [load(f"mouth_{s}.png", mw, mh) for s in range(meta["mouth_states"])]
        self.mx = (meta["mouth_x"] - pw / 2) * k
        self.my = (meta["mouth_y"] - ph / 2) * k

    def draw(self, p, x, y, now, mouth):
        idx = (now % self.loop) / self.loop * self.n
        i0 = int(idx) % self.n
        i1 = (i0 + 1) % self.n
        f = idx - int(idx)
        base = p.opacity()
        p.drawPixmap(QPointF(x, y), self.frames[i0])
        if f > 0.02:                         # cross-fade to the next frame = smooth motion
            p.setOpacity(base * f)
            p.drawPixmap(QPointF(x, y), self.frames[i1])
            p.setOpacity(base)
        p.drawPixmap(QPointF(x + self.mx, y + self.my), self.mouths[max(0, min(mouth, len(self.mouths) - 1))])


# ----------------------------------------------------------------------------
# Themes + the separate theme-switch island (right side)
# ----------------------------------------------------------------------------
THEMES = {
    "Dark":     {"body": "#000000", "fg": "#FFFFFF", "muted": "#A0A0A0", "accent": "#FF3B30",
                 "bubble_ai": "#303038", "bubble_user": "#0A84FF", "cloud": "#E1E6EC"},
    "Midnight": {"body": "#0B1026", "fg": "#EAF0FF", "muted": "#8E9AC4", "accent": "#5AA9FF",
                 "bubble_ai": "#1B2448", "bubble_user": "#3D7BFF", "cloud": "#C9D6F5"},
    "Sakura":   {"body": "#1A0E14", "fg": "#FFF0F5", "muted": "#C99AB0", "accent": "#FF6FA5",
                 "bubble_ai": "#3A1F2B", "bubble_user": "#E8548C", "cloud": "#F3DDE6"},
    "Neon":     {"body": "#0A0614", "fg": "#F4EEFF", "muted": "#9C8CC9", "accent": "#00E5FF",
                 "bubble_ai": "#20123C", "bubble_user": "#B026FF", "cloud": "#D9CCFF"},
    "Forest":   {"body": "#08140F", "fg": "#E9F7EE", "muted": "#8DB59C", "accent": "#3DDC84",
                 "bubble_ai": "#16301F", "bubble_user": "#1FA35C", "cloud": "#D3EBDD"},
    "Sunset":   {"body": "#1A0B05", "fg": "#FFF3E8", "muted": "#D1A58A", "accent": "#FF8A3D",
                 "bubble_ai": "#3A2013", "bubble_user": "#E5602B", "cloud": "#F2DCCB"},
    "Liquid Glass": {"glass": True, "body": "#80141C34", "fg": "#FFFFFF", "muted": "#D2DCF5",
                     "accent": "#8FD0FF", "bubble_ai": "#40FFFFFF", "bubble_user": "#B33D9BFF",
                     "cloud": "#EAF3FF"},
    "Light":    {"body": "#F4F4F6", "fg": "#16161A", "muted": "#6E6E78", "accent": "#FF3B30",
                 "bubble_ai": "#E2E2E8", "bubble_user": "#0A84FF", "cloud": "#B8C0CC"},
}
THEME_KEYS = ("body", "fg", "muted", "accent", "bubble_ai", "bubble_user", "cloud")
SETTINGS_FILE = os.path.join(AI_DIR, "settings.json")
CUSTOM_THEMES_FILE = os.path.join(AI_DIR, "themes.json")
COLLAPSE_DELAY_MS = 2500       # the island stays open this long after the mouse leaves
CUSTOM_THEMES_EXAMPLE = """{
  "My theme": {
    "body": "#101820",
    "fg": "#FFFFFF",
    "muted": "#9AA5B1",
    "accent": "#FEE715",
    "bubble_ai": "#1E2A36",
    "bubble_user": "#2F6FED",
    "cloud": "#DDE6EE",
    "glass": false
  }
}
"""


def load_settings():
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_setting(key, value):
    data = load_settings()
    data[key] = value
    try:
        os.makedirs(AI_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print("settings error:", e)


_theme_cache = None
_color_cache = {}


def clear_theme_cache():
    global _theme_cache
    _theme_cache = None
    _color_cache.clear()


def all_themes():
    """Built-in themes + the ones from %APPDATA%\\DynamicIsland\\themes.json (cached)."""
    global _theme_cache
    if _theme_cache is not None:
        return _theme_cache
    themes = {k: dict(v) for k, v in THEMES.items()}
    try:
        with open(CUSTOM_THEMES_FILE, encoding="utf-8") as f:
            custom = json.load(f)
        for name, pal in custom.items():
            if isinstance(pal, dict):
                merged = dict(THEMES["Dark"])
                merged.update({k: v for k, v in pal.items() if k in THEME_KEYS or k == "glass"})
                themes[str(name)] = merged
    except Exception:
        pass
    _theme_cache = themes
    return themes


def theme_colors(name):
    if name in _color_cache:
        return _color_cache[name]
    pal = all_themes().get(name) or THEMES["Dark"]
    out = {}
    for k in THEME_KEYS:
        c = QColor(str(pal.get(k, THEMES["Dark"][k])))
        out[k] = c if c.isValid() else QColor(THEMES["Dark"][k])
    _color_cache[name] = out
    return out


def theme_is_glass(name):
    return bool((all_themes().get(name) or {}).get("glass", False))


def apply_theme(name):
    """Rebinds the colour globals that all drawing code reads. Returns the real theme name."""
    global BODY, BODY_SOLID, GLASS, WHITE, GRAY, MUTED, ACCENT_RED, BUBBLE_AI, BUBBLE_USER, CLOUD
    if name not in all_themes():
        name = "Dark"
    c = theme_colors(name)
    BODY, WHITE, MUTED, GRAY = c["body"], c["fg"], c["muted"], c["muted"]
    BODY_SOLID = QColor(c["body"])
    BODY_SOLID.setAlpha(255)
    GLASS = theme_is_glass(name)
    ACCENT_RED, BUBBLE_AI, BUBBLE_USER, CLOUD = c["accent"], c["bubble_ai"], c["bubble_user"], c["cloud"]
    return name


ALL_PAGES = ("music", "clock", "calendar", "weather", "chat")
PAGE_TITLES = {"music": "Music player", "clock": "Clock", "calendar": "Calendar",
               "weather": "Weather", "chat": "AI chat"}


def load_page_order():
    """Pages that are shown, in order (setting 'pages'); default: all of them."""
    saved = load_settings().get("pages")
    if isinstance(saved, list):
        ids = [p for p in dict.fromkeys(saved) if p in ALL_PAGES]
        if ids:
            return ids
    return list(ALL_PAGES)


class CustomizeDialog(QDialog):
    """Show / hide and re-order the island pages, and a few behaviour options."""

    def __init__(self, page_ids, collapse_seconds, show_theme_button):
        super().__init__()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowTitle("Customize island")
        self.resize(400, 460)
        self.result_pages = list(page_ids)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Pages: tick = shown. Drag them, or use the buttons, to change the order\n"
                             "(the first page is the one the island opens on)."))
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        lay.addWidget(self.list)
        self._fill(page_ids)

        row = QHBoxLayout()
        up, down, reset = QPushButton("Move up"), QPushButton("Move down"), QPushButton("Reset")
        up.clicked.connect(lambda: self._move(-1))
        down.clicked.connect(lambda: self._move(1))
        reset.clicked.connect(lambda: self._fill(list(ALL_PAGES)))
        for b in (up, down, reset):
            row.addWidget(b)
        lay.addLayout(row)

        drow = QHBoxLayout()
        drow.addWidget(QLabel("Stay open after the mouse leaves:"))
        self.delay = QDoubleSpinBox()
        self.delay.setRange(0.0, 30.0)
        self.delay.setSingleStep(0.5)
        self.delay.setSuffix(" s")
        self.delay.setValue(collapse_seconds)
        drow.addWidget(self.delay)
        lay.addLayout(drow)

        self.theme_btn = QCheckBox("Show the theme button when hovering the island")
        self.theme_btn.setChecked(show_theme_button)
        lay.addWidget(self.theme_btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def _fill(self, enabled_ids):
        self.list.clear()
        order = list(enabled_ids) + [p for p in ALL_PAGES if p not in enabled_ids]
        for pid in order:
            it = QListWidgetItem(PAGE_TITLES[pid])
            it.setData(Qt.UserRole, pid)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsDragEnabled)
            it.setCheckState(Qt.Checked if pid in enabled_ids else Qt.Unchecked)
            self.list.addItem(it)

    def _move(self, d):
        r, n = self.list.currentRow(), self.list.count()
        if r < 0 or not 0 <= r + d < n:
            return
        it = self.list.takeItem(r)
        self.list.insertItem(r + d, it)
        self.list.setCurrentRow(r + d)

    def _accept(self):
        ids = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.Checked:
                ids.append(it.data(Qt.UserRole))
        if not ids:
            QMessageBox.warning(self, "Customize island", "Keep at least one page.")
            return
        self.result_pages = ids
        self.accept()


def make_gear_path():
    """Gear glyph centred on (0, 0): 8 teeth, round body, round hole."""
    body = QPainterPath()
    body.addEllipse(QPointF(0, 0), 6.3, 6.3)
    for i in range(8):
        a = i * math.pi / 4
        ca, sa = math.cos(a), math.sin(a)
        pts = [(-1.8, -9.3), (1.8, -9.3), (2.2, -5.0), (-2.2, -5.0)]
        poly = QPolygonF([QPointF(x * ca - y * sa, x * sa + y * ca) for x, y in pts])
        tooth = QPainterPath()
        tooth.addPolygon(poly)
        tooth.closeSubpath()
        body = body.united(tooth)
    hole = QPainterPath()
    hole.addEllipse(QPointF(0, 0), 2.7, 2.7)
    return body.subtracted(hole)


class GearIsland(QWidget):
    """Round settings button next to the theme island. It fades in and out together with it;
    a click opens the settings menu."""
    D = 34

    def __init__(self, on_click, on_vis=None):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.on_click = on_click
        self.on_vis = on_vis
        self.vis = 0.0
        self.vis_target = 0
        self.vis_anim = QVariantAnimation(self)
        self.vis_anim.valueChanged.connect(self._on_vis)
        self.angle = 0.0
        self.spin = QVariantAnimation(self)
        self.spin.valueChanged.connect(self._on_spin)
        self.gear = make_gear_path()
        self._anchor = (0, 0)
        self.resize(self.D, self.D)

    def set_wanted(self, want):
        want = 1 if want else 0
        if want == self.vis_target:
            return
        self.vis_target = want
        if want and not self.isVisible():
            self.show()
        self.vis_anim.stop()
        self.vis_anim.setStartValue(self.vis)
        self.vis_anim.setEndValue(float(want))
        self.vis_anim.setDuration(170 if want else 140)
        self.vis_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.vis_anim.start()

    def _on_vis(self, v):
        self.vis = float(v)
        if self.on_vis:
            self.on_vis()
        if self.vis <= 0.001 and self.vis_target == 0:
            self.hide()
        self.update()

    def follow(self, island_geom):
        self._anchor = (island_geom.x() + island_geom.width() + 8, island_geom.y())
        self.move(*self._anchor)

    def _spin_to(self, target):
        self.spin.stop()
        self.spin.setStartValue(self.angle)
        self.spin.setEndValue(float(target))
        self.spin.setDuration(320)
        self.spin.setEasingCurve(QEasingCurve.OutCubic)
        self.spin.start()

    def _on_spin(self, v):
        self.angle = float(v)
        self.update()

    def enterEvent(self, e):
        self.setCursor(Qt.PointingHandCursor)
        self._spin_to(90)

    def leaveEvent(self, e):
        self._spin_to(0)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.on_click:
            self.on_click()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.vis)
        w, h = self.width(), self.height()
        body = QPainterPath()
        body.addEllipse(QRectF(0.5, 0.5, w - 1, h - 1))
        fill_body(p, body, QRectF(0.5, 0.5, w - 1, h - 1))
        p.setPen(fg_alpha(22))
        p.setBrush(Qt.NoBrush)
        p.drawPath(body)
        p.translate(w / 2, h / 2)
        p.rotate(self.angle)
        p.setPen(Qt.NoPen)
        p.setBrush(WHITE)
        p.drawPath(self.gear)


class ThemeIsland(QWidget):
    """Separate small island to the right of the main one. Hover it to see all themes,
    click a colour to apply it. The mouse wheel cycles through the themes."""
    D = 34
    PAD = 12
    SLOT = 25

    def __init__(self, on_pick, on_vis=None):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.on_pick = on_pick
        self.on_vis = on_vis
        self.vis = 0.0                      # 0 = hidden, 1 = fully visible
        self.vis_target = 0
        self.vis_anim = QVariantAnimation(self)
        self.vis_anim.valueChanged.connect(self._on_vis)
        self.names = []
        self.current = ""
        self.t = 0.0
        self.target = 0
        self._anchor = (0, 0)
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_t)
        self.resize(self.D, self.D)

    def set_themes(self, names, current):
        self.names = list(names)
        self.current = current
        self._place()
        self.update()

    def set_wanted(self, want):
        """Visible only while the mouse is on the main island (or on this one)."""
        want = 1 if want else 0
        if want == self.vis_target:
            return
        self.vis_target = want
        if want and not self.isVisible():
            self.show()
        self.vis_anim.stop()
        self.vis_anim.setStartValue(self.vis)
        self.vis_anim.setEndValue(float(want))
        self.vis_anim.setDuration(170 if want else 140)
        self.vis_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.vis_anim.start()

    def _on_vis(self, v):
        self.vis = float(v)
        if self.on_vis:
            self.on_vis()
        if self.vis <= 0.001 and self.vis_target == 0:
            self._go(0)
            self.hide()
        self.update()

    def _width(self):
        full = self.PAD * 2 + self.SLOT * max(len(self.names), 1)
        return int(self.D + (full - self.D) * max(self.t, 0.0))

    def _place(self):
        ax, ay = self._anchor
        self.setGeometry(ax, ay, self._width(), self.D)

    def follow(self, island_geom, dx=0):
        self._anchor = (island_geom.x() + island_geom.width() + 8 + dx, island_geom.y())
        self._place()

    def _go(self, target):
        if target == self.target:
            return
        self.target = target
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(float(target))
        self.anim.setDuration(320 if target else 240)
        self.anim.setEasingCurve(QEasingCurve.OutBack if target else QEasingCurve.InOutCubic)
        self.anim.start()

    def _on_t(self, v):
        self.t = float(v)
        self._place()
        self.update()

    def _index_at(self, x):
        i = int((x - self.PAD) // self.SLOT)
        return i if 0 <= i < len(self.names) else -1

    def enterEvent(self, e):                 # opens while the mouse is over it ...
        self._go(1)
        self.raise_()

    def leaveEvent(self, e):                 # ... and closes the moment the mouse leaves
        QToolTip.hideText()
        self.unsetCursor()
        self._go(0)

    def mouseMoveEvent(self, e):
        if self.t > 0.6:
            i = self._index_at(e.position().x())
            if i >= 0:
                QToolTip.showText(e.globalPosition().toPoint(), self.names[i], self)
            self.setCursor(Qt.PointingHandCursor if i >= 0 else Qt.ArrowCursor)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.t > 0.6:
            i = self._index_at(e.position().x())
            if i >= 0:
                self.on_pick(self.names[i])

    def wheelEvent(self, e):
        if not self.names:
            return
        step = -1 if e.angleDelta().y() > 0 else 1
        try:
            i = self.names.index(self.current)
        except ValueError:
            i = 0
        self.on_pick(self.names[(i + step) % len(self.names)])

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setOpacity(self.vis)
        w, h = self.width(), self.height()
        body = QPainterPath()
        body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 17, 17)
        fill_body(p, body, QRectF(0.5, 0.5, w - 1, h - 1))
        p.setPen(fg_alpha(22))
        p.drawPath(body)
        k = min(max(self.t, 0.0), 1.0)
        if k < 0.5:                                   # collapsed: a little "palette" dot
            p.setOpacity((1 - k * 2) * self.vis)
            c = QPointF(w / 2, h / 2)
            p.setPen(QPen(fg_alpha(170), 1.6))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, 9, 9)
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT_RED)
            p.drawEllipse(c, 5.5, 5.5)
        if k > 0.5 and self.names:                    # expanded: one swatch per theme
            p.setOpacity(min(1.0, (k - 0.5) * 2) * self.vis)
            r = 9.0
            for i, name in enumerate(self.names):
                cx, cy = self.PAD + self.SLOT * i + self.SLOT / 2, h / 2
                col = theme_colors(name)
                p.setPen(QPen(fg_alpha(90), 1))
                glass = theme_is_glass(name)
                p.setBrush(QColor(190, 215, 255, 150) if glass else col["body"])
                p.drawEllipse(QPointF(cx, cy), r, r)
                circle = QPainterPath()
                circle.addEllipse(QPointF(cx, cy), r, r)
                half = QPainterPath()
                half.addRect(QRectF(cx, cy - r, r, 2 * r))
                p.fillPath(circle.intersected(half), col["accent"])
                if glass:                                  # little specular highlight
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(255, 255, 255, 190))
                    p.drawEllipse(QPointF(cx - 3.5, cy - 4), 2.6, 1.7)
                if name == self.current:
                    p.setPen(QPen(WHITE, 1.8))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(QPointF(cx, cy), r + 3.4, r + 3.4)


# ----------------------------------------------------------------------------
# Where the island sits: top-center normally, bottom-left over browsers, top-left in games
# ----------------------------------------------------------------------------
PLACES = {"top_center": (1.0, 0.0), "bottom_left": (0.0, 1.0), "top_left": (0.0, 0.0)}
CONTEXT_PLACE = {"normal": "top_center", "browser": "bottom_left", "game": "top_left"}

BROWSER_EXES = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "opera_gx.exe", "brave.exe",
                "vivaldi.exe", "browser.exe", "yandex.exe", "arc.exe", "iexplore.exe", "waterfox.exe",
                "librewolf.exe", "chromium.exe", "maxthon.exe", "thorium.exe", "floorp.exe",
                "zen.exe", "sidekick.exe"}
GAME_EXES = {"cs2.exe", "csgo.exe", "dota2.exe", "valorant.exe", "valorant-win64-shipping.exe",
             "fortniteclient-win64-shipping.exe", "league of legends.exe", "overwatch.exe",
             "gta5.exe", "rdr2.exe", "eldenring.exe", "r5apex.exe", "rocketleague.exe",
             "tslgame.exe", "witcher3.exe", "cyberpunk2077.exe", "genshinimpact.exe",
             "yuanshen.exe", "starrail.exe", "robloxplayerbeta.exe", "terraria.exe",
             "stardewvalley.exe", "hl2.exe", "bf2042.exe", "destiny2.exe", "warframe.x64.exe"}
# full-screen windows of these are NOT games (video players, presentations, remote desktop ...)
IGNORE_EXES = {"vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe", "mpv.exe",
               "potplayermini.exe", "potplayermini64.exe", "potplayer64.exe", "wmplayer.exe",
               "powerpnt.exe", "mstsc.exe", "vmware.exe", "vmware-vmx.exe", "virtualboxvm.exe",
               "vmconnect.exe", "zoom.exe", "teams.exe", "ms-teams.exe", "code.exe", "devenv.exe",
               "photoshop.exe", "acrobat.exe", "acrord32.exe", "winword.exe", "excel.exe",
               "obs64.exe", "anydesk.exe", "teamviewer.exe", "applicationframehost.exe",
               "explorer.exe", "searchhost.exe", "shellexperiencehost.exe",
               "startmenuexperiencehost.exe", "lockapp.exe", "textinputhost.exe"}
# game libraries (launchers themselves live elsewhere, so they do not match)
GAME_PATH_PARTS = ("\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\",
                   "\\riot games\\", "\\ubisoft game launcher\\games\\", "\\ea games\\",
                   "\\xboxgames\\")
GAME_PATH_EXCLUDE = ("\\epic games\\launcher\\", "\\riot client\\")
APPS_FILE = os.path.join(AI_DIR, "apps.json")
APPS_EXAMPLE = """{
  "browsers": ["mybrowser.exe"],
  "games": ["mygame.exe"],
  "ignore": ["not-a-game-when-fullscreen.exe"]
}
"""
_apps_cache = {"mtime": "unset", "data": None}


def app_lists():
    """Built-in app lists + your own from %APPDATA%\\\\DynamicIsland\\\\apps.json (cached by mtime)."""
    try:
        m = os.path.getmtime(APPS_FILE)
    except OSError:
        m = None
    if _apps_cache["data"] is None or _apps_cache["mtime"] != m:
        data = {"browsers": set(BROWSER_EXES), "games": set(GAME_EXES), "ignore": set(IGNORE_EXES)}
        if m is not None:
            try:
                with open(APPS_FILE, encoding="utf-8") as f:
                    custom = json.load(f)
                for key in data:
                    for name in custom.get(key, []):
                        data[key].add(str(name).lower())
            except Exception as e:
                print("apps.json error:", e)
        _apps_cache["mtime"], _apps_cache["data"] = m, data
    return _apps_cache["data"]


def classify_app(exe, low_path, lists, d3d_fullscreen=False, borderless_fullscreen=False):
    """'browser' / 'game' / 'normal' for the program in front."""
    if exe in lists["browsers"]:
        return "browser"
    if exe in lists["games"]:
        return "game"
    if (any(p in low_path for p in GAME_PATH_PARTS)
            and not any(p in low_path for p in GAME_PATH_EXCLUDE)):
        return "game"
    if exe in lists["ignore"]:
        return "normal"
    if d3d_fullscreen or borderless_fullscreen:       # a full-screen app that is none of the above
        return "game"
    return "normal"


def place_xy(u, v, w, h, cx, left_x, top, bottom_y):
    """Window position for the placement blend (u: 0 = left aligned, 1 = centred;
    v: 0 = top, 1 = bottom - the island then grows upwards)."""
    x = left_x + (cx - left_x) * u - w * u / 2.0
    y = top + v * (bottom_y - h - top)
    return int(round(x)), int(round(y))


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


_shell32 = ctypes.windll.shell32
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetWindowThreadProcessId.restype = wintypes.DWORD
_user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetClassNameW.restype = ctypes.c_int
_user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
_user32.MonitorFromWindow.restype = wintypes.HANDLE
_user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL
_shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
_shell32.SHQueryUserNotificationState.restype = ctypes.c_long


def _process_path(pid):
    h = _k32.OpenProcess(0x1000, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        return buf.value if _k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
    finally:
        _k32.CloseHandle(h)


def detect_context():
    """'browser', 'game' or 'normal' for the window in front; None while one of our own
    windows is in front (then the island simply stays where it is)."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "normal"
    pid = wintypes.DWORD(0)
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == os.getpid():
        return None
    cls = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls, 256)
    if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
        return "normal"
    path = _process_path(pid.value)
    exe = os.path.basename(path).lower()
    low = path.lower()

    d3d = False
    state = ctypes.c_int(0)
    if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0:
        d3d = state.value == 3                       # exclusive full-screen DirectX app

    borderless = False
    rect = wintypes.RECT()
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        mon = _user32.MonitorFromWindow(hwnd, 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if mon and _user32.GetMonitorInfoW(mon, ctypes.byref(mi)):
            m = mi.rcMonitor
            covers = (rect.left <= m.left and rect.top <= m.top and
                      rect.right >= m.right and rect.bottom >= m.bottom)
            has_caption = _user32.GetWindowLongW(hwnd, -16) & 0x00C00000   # maximized apps have one
            borderless = covers and not has_caption
    return classify_app(exe, low, app_lists(), d3d, borderless)


# ----------------------------------------------------------------------------
# Island window
# ----------------------------------------------------------------------------
W0, H0 = 150, 34       # collapsed size
BADGE_EXTRA = 0        # collapsed pill keeps its normal height
W1, H1 = 400, 140      # expanded size
TOP_MARGIN = 8
W_CHAT, H_CHAT = 600, 320   # on the chat page the island becomes bigger
CARD_W, CARD_H = 164, 292   # character card (left side of the chat page)
INPUT_RECT = QRect(194, 276, 352, 32)   # AI text input (right side)
SEND_RECT = QRect(554, 276, 32, 32)
SPEECH_CPS = 17.0           # how fast she 'speaks' a reply (characters per second)


def fmt(sec):
    sec = max(0, int(sec))
    return f"{sec // 60}:{sec % 60:02d}"


class Island(QWidget):
    def __init__(self, worker: MediaWorker, weather: WeatherWorker, bubble: Bubble,
                 chat: ChatWorker):
        super().__init__()
        self.bubble = bubble
        self.theme_island = ThemeIsland(self.set_theme, self._theme_vis_changed)
        self.gear_island = GearIsland(self.show_settings_menu, self._theme_vis_changed)
        self.menu_open = False
        self.keep_open = bool(load_settings().get("keep_open", False))
        self.page_ids = load_page_order()
        self.collapse_delay_ms = int(float(load_settings().get("collapse_delay", COLLAPSE_DELAY_MS / 1000.0)) * 1000)
        self.show_theme_button = bool(load_settings().get("theme_button", True))
        self.chat = chat
        self.chat_msgs = []
        self.chat_busy = False
        self.sound = StartupSound()
        self.source = "other"
        self.worker = worker
        self.weather_worker = weather
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint |
                            Qt.Tool | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setMouseTracking(True)

        geo = QGuiApplication.primaryScreen().geometry()
        self.cx = geo.center().x()
        self.top = geo.top() + TOP_MARGIN
        avail = QGuiApplication.primaryScreen().availableGeometry()      # without the taskbar
        self.left_x = geo.left() + 14
        self.bottom_y = avail.y() + avail.height() - 14
        self.place_u, self.place_v = PLACES["top_center"]
        self.place_mode = "top_center"
        self.place_from = self.place_to = PLACES["top_center"]
        self.ctx, self.ctx_candidate, self.ctx_count = "normal", "normal", 0
        self.auto_move = bool(load_settings().get("auto_move", True))

        # expand / collapse animation
        self.t = 0.0
        self.target = 0
        self.pin_until = 0.0
        self.anim = QVariantAnimation(self)
        self.anim.valueChanged.connect(self._on_t)
        self.anim.finished.connect(self._on_anim_done)

        self.collapse_timer = QTimer(self)
        self.collapse_timer.setSingleShot(True)
        self.collapse_timer.setInterval(self.collapse_delay_ms)
        self.collapse_timer.timeout.connect(self._maybe_collapse)

        # page switching (0 = music, 1 = calendar, 2 = weather)
        self.page = 0
        self.page_pos = 0.0
        self.last_wheel = 0.0
        self.page_anim = QVariantAnimation(self)
        self.page_anim.valueChanged.connect(self._on_page_pos)

        # Spotify badge (grows the collapsed pill while a track is playing)
        self.badge = 0.0
        self.badge_target = 0
        self.badge_anim = QVariantAnimation(self)
        self.badge_anim.valueChanged.connect(self._on_badge)

        # track state
        self.title = "No active player"
        self.artist = "Open Spotify to play music"
        self.none = True
        self.playing = False
        self.dur = 0.0
        self.raw_pos = -1.0
        self.base_pos = 0.0
        self.base_time = time.monotonic()
        self.art = None
        self.art_serial = 0
        self.weather_ver = 0
        self._track_key = None

        # weather state
        self.weather = None

        # eyes
        self.blink = 0.0
        self.blink_anim = QVariantAnimation(self)
        self.blink_anim.setStartValue(0.0)
        self.blink_anim.setEndValue(1.0)
        self.blink_anim.setDuration(170)
        self.blink_anim.valueChanged.connect(self._on_blink)
        self.blink_anim.finished.connect(self._schedule_blink)
        self._schedule_blink()

        self._last_paint = 0.0
        self._page_cache = {}
        self.speak = None            # reply currently being 'spoken' (typewriter + lip-sync)
        self.reveal_len = 0
        self.mouth_state = 0
        cdir = find_character_dir()
        try:
            self.character = Character(cdir, self.devicePixelRatioF()) if cdir else None
        except Exception as e:
            print("character load error:", e)
            self.character = None
        self.frame_timer = QTimer(self)
        self.frame_timer.setInterval(16)
        self.frame_timer.timeout.connect(self._frame)
        self.frame_timer.start()
        self.place_anim = QVariantAnimation(self)
        self.place_anim.valueChanged.connect(self._on_place)
        self.ctx_timer = QTimer(self)                 # which program is in front? (every 0.35 s)
        self.ctx_timer.setInterval(350)
        self.ctx_timer.timeout.connect(self._check_context)
        self.ctx_timer.start()

        # button rectangles (expanded layout, music page)
        self.rect_prev = QRect(244 - 52 - 18, 82 - 18, 36, 36)
        self.rect_play = QRect(244 - 20, 82 - 20, 40, 40)
        self.rect_next = QRect(244 + 52 - 18, 82 - 18, 36, 36)

        # chat page: text box + signals
        self.chat_input = QLineEdit(self)
        self.chat_input.setPlaceholderText("Type a message...")
        self._style_chat_input()
        self.theme_island.set_themes(list(all_themes()), apply_theme(load_settings().get("theme", "Dark")))
        self.chat_input.returnPressed.connect(self._send_chat)
        self.chat_input.hide()
        QApplication.instance().focusChanged.connect(lambda *_: self.collapse_timer.start())
        self.chat.reply.connect(self.on_chat_reply)
        self.chat.busy.connect(self.on_chat_busy)
        self.chat_msgs.append(("assistant", "Hi! Ask me anything."))

        self.worker.changed.connect(self.on_state)
        self.weather_worker.updated.connect(self.on_weather)
        self._apply_geometry()

    # ---------- animations ----------
    # ---------- smart placement ----------
    def move_to(self, mode):
        if mode == self.place_mode:
            return
        self.place_mode = mode
        self.place_from = (self.place_u, self.place_v)
        self.place_to = PLACES[mode]
        self.place_anim.stop()
        self.place_anim.setStartValue(0.0)
        self.place_anim.setEndValue(1.0)
        self.place_anim.setDuration(750)
        self.place_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.place_anim.start()

    def _on_place(self, t):
        t = float(t)
        (fu, fv), (tu, tv) = self.place_from, self.place_to
        self.place_u, self.place_v = fu + (tu - fu) * t, fv + (tv - fv) * t
        self._apply_geometry()

    def _check_context(self):
        if not self.auto_move:
            return
        try:
            ctx = detect_context()
        except Exception as e:
            print("context error:", e)
            return
        if ctx is None:                      # one of our own windows is in front: stay put
            return
        if ctx == self.ctx_candidate:
            self.ctx_count += 1
        else:
            self.ctx_candidate, self.ctx_count = ctx, 1
        if self.ctx_count >= 2 and ctx != self.ctx:      # same answer twice = really switched
            self.ctx = ctx
            self.move_to(CONTEXT_PLACE[ctx])

    def set_auto_move(self, on):
        self.auto_move = bool(on)
        save_setting("auto_move", self.auto_move)
        if not on:
            self.ctx = self.ctx_candidate = "normal"
            self.move_to("top_center")

    def edit_app_lists(self):
        os.makedirs(AI_DIR, exist_ok=True)
        if not os.path.isfile(APPS_FILE):
            with open(APPS_FILE, "w", encoding="utf-8") as f:
                f.write(APPS_EXAMPLE)
        subprocess.Popen(["notepad.exe", APPS_FILE])

    def _theme_vis_changed(self):
        self.bubble.follow(getattr(self, "_row_geom", self.geometry()),
                           self.gear_island.vis + self.theme_island.vis)

    def _update_theme_visibility(self):
        """The theme island shows up only while the mouse is on the island (or on the theme
        island itself, including the small gap between them)."""
        pos = QCursor.pos()
        ti, gi = self.theme_island, self.gear_island
        stack = gi.geometry().adjusted(-8, 0, 0, 0).united(ti.geometry())
        want = self.menu_open or self.geometry().contains(pos) or stack.contains(pos)
        gi.set_wanted(want)
        ti.set_wanted(want and self.show_theme_button)

    def _update_speech(self, now):
        if not self.speak:
            return
        text = self.speak["text"]
        n = int((now - self.speak["t0"]) * SPEECH_CPS)
        if n >= len(text) + 3:               # finished (plus a short pause)
            self.speak = None
            self.mouth_state = 0
            return
        self.reveal_len = min(n, len(text))
        self.mouth_state = viseme(text[min(n, len(text) - 1)])

    def _frame(self):
        now = time.monotonic()
        self._update_theme_visibility()
        self._update_speech(now)
        if self.t > 0.5:                                 # expanded
            vis = self._visible_pids()
            if "clock" in vis or "chat" in vis:          # moving hands / living character
                need = 1 / 30
            elif "music" in vis:                         # progress bar
                need = 0.2
            else:                                        # calendar / weather: static
                need = 1.0
        else:                                            # collapsed: eyes
            need = 1 / 30 if (self.playing or self.blink > 0) else 1.0
        if now - self._last_paint >= need:
            self.update()

    def _schedule_blink(self):
        self.blink = 0.0
        QTimer.singleShot(random.randint(2500, 6000), self.blink_anim.start)

    def _on_blink(self, v):
        self.blink = v

    def _animate_to(self, target):
        self.target = target
        self.anim.stop()
        self.anim.setStartValue(self.t)
        self.anim.setEndValue(float(target))
        if target:
            self.anim.setDuration(420)
            self.anim.setEasingCurve(QEasingCurve.OutBack)
        else:
            self.anim.setDuration(300)
            self.anim.setEasingCurve(QEasingCurve.InOutCubic)
        self.anim.start()

    def _on_anim_done(self):
        if self.target == 0:
            self.page_anim.stop()
            self.page = 0
            self.page_pos = 0.0
            self.chat_input.clearFocus()
            self.chat_input.hide()

    def expand(self):
        if self.target != 1:
            self._animate_to(1)

    def collapse(self):
        if self.target != 0:
            self._animate_to(0)

    def _maybe_collapse(self):
        if self.keep_open or self.menu_open or self.chat_input.hasFocus():
            return
        if not self.underMouse() and time.time() >= self.pin_until:
            self.collapse()

    def _on_t(self, v):
        self.t = float(v)
        self._apply_geometry()
        self.update()

    def _on_page_pos(self, v):
        self.page_pos = float(v)
        self._apply_geometry()
        self.update()

    def _on_badge(self, v):
        self.badge = float(v)
        self._apply_geometry()

    def _set_badge(self, on):
        on = 1 if on else 0
        if on == self.badge_target:
            return
        self.badge_target = on
        self.badge_anim.stop()
        self.badge_anim.setStartValue(self.badge)
        self.badge_anim.setEndValue(float(on))
        self.badge_anim.setDuration(280)
        self.badge_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.badge_anim.start()

    # ---------- pages (order and visibility are settings) ----------
    def _pi(self, pid):
        return self.page_ids.index(pid) if pid in self.page_ids else -1

    @staticmethod
    def _page_size(pid):
        return (W_CHAT, H_CHAT) if pid == "chat" else (W1, H1)

    def _page_split(self):
        """(index of the page mostly in view, fraction of the way to the next one)."""
        n = len(self.page_ids)
        pp = min(max(self.page_pos, 0.0), n - 1.0)
        f = min(int(pp), n - 1)
        return f, (pp - f if f < n - 1 else 0.0)

    def _page_y(self, pid):
        """y of that page's top edge inside the window (huge numbers = not in view)."""
        i = self._pi(pid)
        if i < 0:
            return 10 ** 6
        f, frac = self._page_split()
        hf = self._page_size(self.page_ids[f])[1]
        if i == f:
            return -frac * hf
        if i == f + 1:
            return hf * (1.0 - frac)
        return -10 ** 6 if i < f else 10 ** 6

    def _visible_pids(self):
        f, frac = self._page_split()
        ids = [self.page_ids[f]]
        if frac > 0.001 and f + 1 < len(self.page_ids):
            ids.append(self.page_ids[f + 1])
        return ids

    def apply_pages(self, ids):
        cur = self.page_ids[min(self.page, len(self.page_ids) - 1)]
        self.page_ids = list(ids)
        self._page_cache = {}
        self.page = self.page_ids.index(cur) if cur in self.page_ids else 0
        self.page_anim.stop()
        self.page_pos = float(self.page)
        self._apply_geometry()
        self.update()

    def _make_menu(self):
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu{background:%s;color:%s;border:1px solid rgba(128,128,128,90);padding:4px;}"
            "QMenu::item{padding:6px 24px 6px 14px;}"
            "QMenu::item:selected{background:%s;}"
            "QMenu::separator{height:1px;background:rgba(128,128,128,80);margin:4px 8px;}"
            % (BODY_SOLID.name(), WHITE.name(), ACCENT_RED.name()))
        menu.addAction("Customize island...", self.open_customize)
        menu.addSeparator()

        behave = menu.addMenu("Behaviour")
        pin = behave.addAction("Stay expanded")
        pin.setCheckable(True)
        pin.setChecked(self.keep_open)
        pin.toggled.connect(self.set_keep_open)
        auto = behave.addAction("Auto-move (browser / game)")
        auto.setCheckable(True)
        auto.setChecked(self.auto_move)
        auto.toggled.connect(self.set_auto_move)
        behave.addAction("Edit app lists...", self.edit_app_lists)
        if FROZEN:
            start = behave.addAction("Start with Windows")
            start.setCheckable(True)
            start.setChecked(autostart_enabled())
            start.toggled.connect(set_autostart)

        themes = menu.addMenu("Themes")
        themes.addAction("Edit custom themes...", self.edit_custom_themes)
        themes.addAction("Reload themes", self.reload_themes)

        ai = menu.addMenu("AI chat")
        prov = ai.addMenu("AI provider")
        current = load_ai_config()["provider"]
        for pid, info in PROVIDERS.items():
            act = prov.addAction(info["label"])
            act.setCheckable(True)
            act.setChecked(pid == current)
            act.triggered.connect(lambda _=False, pid=pid: self.choose_provider(pid))
        ai.addAction("Set AI model...", self.set_ai_model)
        ai.addAction("Clear chat", self.clear_chat)

        menu.addAction("Refresh weather", self.weather_worker.refresh)

        sounds = menu.addMenu("Sounds")
        sounds.addAction("Play hello sound", lambda: self.sound.play("hello"))
        sounds.addAction("Play goodbye sound", lambda: self.sound.play("goodbye"))

        menu.addSeparator()
        menu.addAction("Quit (plays goodbye)", self.quit_with_goodbye)
        menu.addAction("Quit silently", lambda: self.quit_with_goodbye(silent=True))
        return menu

    def show_settings_menu(self):
        """Opens next to the gear; the gear / theme button stay visible while it is open."""
        menu = self._make_menu()
        self.menu_open = True
        try:
            menu.exec(self.gear_island.mapToGlobal(QPoint(0, self.gear_island.height() + 6)))
        finally:
            self.menu_open = False
            self.collapse_timer.start()

    def open_customize(self):
        dlg = CustomizeDialog(self.page_ids, self.collapse_delay_ms / 1000.0, self.show_theme_button)
        if not dlg.exec():
            return
        save_setting("pages", dlg.result_pages)
        self.apply_pages(dlg.result_pages)
        secs = float(dlg.delay.value())
        save_setting("collapse_delay", secs)
        self.collapse_delay_ms = int(secs * 1000)
        self.collapse_timer.setInterval(self.collapse_delay_ms)
        self.show_theme_button = dlg.theme_btn.isChecked()
        save_setting("theme_button", self.show_theme_button)

    def set_page(self, page):
        self.page = page
        self.page_anim.stop()
        self.page_anim.setStartValue(self.page_pos)
        self.page_anim.setEndValue(float(page))
        self.page_anim.setDuration(320)
        self.page_anim.setEasingCurve(QEasingCurve.OutCubic)
        self.page_anim.start()

    def _apply_geometry(self):
        k = max(self.t, 0.0)
        h0 = H0 + BADGE_EXTRA * self.badge
        f, frac = self._page_split()
        n = len(self.page_ids)
        sw_f, sh_f = self._page_size(self.page_ids[f])
        sw_n, sh_n = self._page_size(self.page_ids[min(f + 1, n - 1)])
        w1 = sw_f + (sw_n - sw_f) * frac
        h1 = sh_f + (sh_n - sh_f) * frac
        w = int(W0 + (w1 - W0) * k)
        h = int(h0 + (h1 - h0) * k)
        x, y = place_xy(self.place_u, self.place_v, w, h, self.cx, self.left_x, self.top, self.bottom_y)
        new_geom = QRect(x, y, w, h)
        if new_geom != self.geometry():
            self.setGeometry(new_geom)
        # the little side islands stay level with the pill row (top row, or bottom row in the corner)
        row_y = int(round(new_geom.y() + self.place_v * (h - H0)))
        self._row_geom = QRect(new_geom.x(), row_y, new_geom.width(), H0)
        self.bubble.follow(self._row_geom, self.gear_island.vis + self.theme_island.vis)
        self.gear_island.follow(self._row_geom)
        self.theme_island.follow(self._row_geom, 42)        # right of the gear
        self._layout_chat_input()

    # ---------- mouse ----------
    def enterEvent(self, e):
        self.collapse_timer.stop()
        self.expand()

    def leaveEvent(self, e):
        self.unsetCursor()
        self.collapse_timer.start()

    def _on_music_page(self):
        mi = self._pi("music")
        return mi >= 0 and self.page == mi and abs(self.page_pos - mi) < 0.15

    def mouseMoveEvent(self, e):
        if self.t > 0.8 and self._on_music_page():
            p = e.position().toPoint()
            over = any(r.contains(p) for r in (self.rect_prev, self.rect_play, self.rect_next))
            self.setCursor(Qt.PointingHandCursor if over else Qt.ArrowCursor)
        else:
            self.unsetCursor()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            return                      # the settings live in the gear button now
        if e.button() == Qt.LeftButton:
            if self.t < 0.8:
                self.expand()
                return
            ci = self._pi("chat")
            if ci >= 0 and self.page == ci and abs(self.page_pos - ci) < 0.1:
                pt = e.position().toPoint()
                if SEND_RECT.contains(pt.x(), pt.y() - int(self._page_y("chat"))):
                    self._send_chat()
                return
            if not self._on_music_page():
                return
            p = e.position().toPoint()
            if self.rect_prev.contains(p):
                self.worker.command("prev")
            elif self.rect_play.contains(p):
                self.worker.command("toggle")
            elif self.rect_next.contains(p):
                self.worker.command("next")

    def wheelEvent(self, e):
        if self.t < 0.8:
            return
        dy = e.angleDelta().y()
        if dy == 0:
            return
        now = time.monotonic()
        if now - self.last_wheel < 0.28:   # one page per scroll gesture
            return
        self.last_wheel = now
        new = max(0, min(len(self.page_ids) - 1, self.page + (1 if dy < 0 else -1)))
        if new != self.page:
            self.set_page(new)

    # ---------- data ----------
    def on_weather(self, data):
        self.weather = data
        self.weather_ver += 1

    def on_fl(self, running, path):
        pm = None
        if running and path:
            icon = QFileIconProvider().icon(QFileInfo(path))
            pm = icon.pixmap(64, 64)
            if pm.isNull():
                pm = None
        self.bubble.set_running(running, pm)

    def on_state(self, s):
        if s.get("none"):
            self.none = True
            self.title, self.artist, self.art = "No active player", "Open Spotify to play music", None
            self.art_serial += 1
            self.source = "other"
            self.playing = False
            self.dur = 0.0
            self._track_key = None
            self._set_badge(False)
            return

        self.none = False
        self.source = s.get("source", "other")
        key = (self.source, s["title"], s["artist"])
        track_changed = key != self._track_key
        if track_changed:
            first = self._track_key is None
            self._track_key = key
            self.title, self.artist = s["title"], s["artist"]
            if not first:
                self.pin_until = time.time() + 5.0
                if self.target == 0:
                    self.page = max(self._pi("music"), 0)
                    self.page_pos = float(self.page)
                self.expand()
                QTimer.singleShot(5200, self._maybe_collapse)
        if s["art_changed"]:
            pm = QPixmap()
            self.art = pm if (s["art"] and pm.loadFromData(s["art"])) else None
            self.art_serial += 1

        if track_changed or s["pos"] != self.raw_pos or s["playing"] != self.playing:
            self.raw_pos = s["pos"]
            self.base_pos = s["pos"]
            self.base_time = time.monotonic()
        self.playing = s["playing"]
        self.dur = s["dur"]
        self._set_badge(self.playing and self.source in ("spotify", "yandex"))

    def current_pos(self):
        pos = self.base_pos
        if self.playing:
            pos += time.monotonic() - self.base_time
        if self.dur > 0:
            pos = min(pos, self.dur)
        return max(pos, 0.0)

    # ---------- painting ----------
    def paintEvent(self, e):
        self._last_paint = time.monotonic()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.TextAntialiasing)

        w, h = self.width(), self.height()
        k = min(max(self.t, 0.0), 1.0)
        radius = min(h / 2.0, 17 + 17 * k)

        body = QPainterPath()
        body.addRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), radius, radius)
        fill_body(p, body, QRectF(0.5, 0.5, w - 1, h - 1))
        p.setPen(fg_alpha(22))
        p.drawPath(body)

        # --- collapsed: eyes + red Spotify icon ---
        eye_op = max(0.0, 1.0 - k * 1.8)
        if eye_op > 0:
            p.setOpacity(eye_op)
            p.setPen(Qt.NoPen)
            p.setBrush(WHITE)
            eh = 13 * (1 - 0.88 * math.sin(math.pi * self.blink))
            bob = math.sin(time.time() * 5) * 1.5 if self.playing else 0
            for dx in (-10, 10):
                p.drawRoundedRect(QRectF(w / 2 + dx - 3, 17 - eh / 2 + bob, 6, eh), 3, 3)
            if self.badge > 0.02:
                p.setOpacity(eye_op * min(1.0, self.badge))
                if self.source == "yandex":
                    draw_yandex_icon(p, w - 20, h / 2, 14)
                else:
                    draw_spotify_icon(p, w - 20, h / 2, 14, SPOTIFY_RED)
            p.setOpacity(1.0)

        # --- expanded: pages ---
        op = max(0.0, min(1.0, (k - 0.55) / 0.45))
        if op <= 0:
            return
        p.setClipPath(body)
        p.setOpacity(op)

        ox = max(0.0, (w - W1) / 2.0)        # keep the 400px pages centred while the island is wide
        n = len(self.page_ids)
        f, frac = self._page_split()
        slots = [f] + ([f + 1] if frac > 0.001 and f + 1 < n else [])
        for i in slots:
            pid = self.page_ids[i]
            y = self._page_y(pid)
            if y >= h or y + self._page_size(pid)[1] <= 0:
                continue
            p.save()
            p.translate(0 if pid == "chat" else ox, y)
            pm = self._page_pixmap(pid)
            if pm is not None:
                p.drawPixmap(0, 0, pm)
                if pid == "music":
                    self._draw_music_progress(p)
                elif pid == "chat":
                    self._draw_character(p)
            else:
                self._draw_clock(p)
            p.restore()

        # page dots
        if n > 1:
            p.setPen(Qt.NoPen)
            for i in range(n):
                a = max(0.0, 1.0 - abs(i - self.page_pos))
                p.setBrush(fg_alpha(int(70 + 185 * a)))
                r = 2.2 + 0.8 * a
                p.drawEllipse(QPointF(w - 11, H1 / 2 + (i - (n - 1) / 2) * 12), r, r)

    def _page_pixmap(self, pid):
        """Static pages are drawn once into a bitmap and then just blitted."""
        drawers = {"music": self._draw_music, "calendar": self._draw_calendar,
                   "weather": self._draw_weather, "chat": self._draw_chat}
        if pid not in drawers:
            return None                      # live page (clock)
        if pid == "music":
            key = (self.title, self.artist, self.art_serial, self.playing, self.none)
        elif pid == "calendar":
            key = datetime.date.today()
        elif pid == "weather":
            key = self.weather_ver
        else:
            key = (hash(tuple(self.chat_msgs)), self.chat_busy,
                   self.reveal_len if self.speak else -1)
        dpr = self.devicePixelRatioF()
        cached = self._page_cache.get(pid)
        if cached and cached[0] == key and cached[2] == dpr:
            return cached[1]
        pw, ph = self._page_size(pid)
        pm = QPixmap(int(pw * dpr), int(ph * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(QColor(0, 0, 0, 0))
        q = QPainter(pm)
        q.setRenderHint(QPainter.Antialiasing)
        q.setRenderHint(QPainter.SmoothPixmapTransform)
        q.setRenderHint(QPainter.TextAntialiasing)
        drawers[pid](q)
        q.end()
        self._page_cache[pid] = (key, pm, dpr)
        return pm

    # ---------- themes / behaviour ----------
    def _style_chat_input(self):
        f = WHITE
        self.chat_input.setStyleSheet(
            "QLineEdit{background:rgba(%d,%d,%d,40);border:none;border-radius:16px;"
            "padding:0 12px;color:%s;font:13px 'Segoe UI';selection-background-color:%s;}"
            % (f.red(), f.green(), f.blue(), f.name(), BUBBLE_USER.name()))

    def set_theme(self, name):
        name = apply_theme(name)
        save_setting("theme", name)
        self._page_cache = {}                 # static pages are cached bitmaps -> redraw them
        self._style_chat_input()
        self.theme_island.set_themes(list(all_themes()), name)
        self.bubble.update()
        self.update()

    def reload_themes(self):
        clear_theme_cache()
        self.set_theme(load_settings().get("theme", "Dark"))

    def edit_custom_themes(self):
        os.makedirs(AI_DIR, exist_ok=True)
        if not os.path.isfile(CUSTOM_THEMES_FILE):
            with open(CUSTOM_THEMES_FILE, "w", encoding="utf-8") as f:
                f.write(CUSTOM_THEMES_EXAMPLE)
        subprocess.Popen(["notepad.exe", CUSTOM_THEMES_FILE])

    def set_keep_open(self, on):
        self.keep_open = bool(on)
        save_setting("keep_open", self.keep_open)
        if not on:
            self.collapse_timer.start()

    # ---------- AI chat ----------
    def _layout_chat_input(self):
        ci = self._pi("chat")
        y = int(self._page_y("chat") + INPUT_RECT.y()) if ci >= 0 else 0
        vis = (ci >= 0 and self.t > 0.8 and abs(self.page_pos - ci) < 0.12 and
               self.width() >= W_CHAT - 6 and 0 <= y and y + INPUT_RECT.height() <= self.height())
        if vis:
            self.chat_input.setGeometry(INPUT_RECT.x(), y, INPUT_RECT.width(), INPUT_RECT.height())
        if vis != self.chat_input.isVisible():
            self.chat_input.setVisible(vis)

    def _send_chat(self):
        text = self.chat_input.text().strip()
        if not text or self.chat_busy:
            return
        self.chat_input.clear()
        self.speak = None
        self.mouth_state = 0
        self.chat_msgs.append(("user", text))
        del self.chat_msgs[:-40]
        self.chat.ask(text)

    def on_chat_busy(self, busy):
        self.chat_busy = bool(busy)

    def on_chat_reply(self, text):
        msg = ("assistant", text)
        self.chat_msgs.append(msg)
        del self.chat_msgs[:-40]
        # she "speaks" the reply: text appears gradually while the mouth follows the letters
        self.speak = {"msg": msg, "text": text, "t0": time.monotonic()}
        self.reveal_len = 0

    def clear_chat(self):
        self.chat.clear()
        self.speak = None
        self.mouth_state = 0
        self.chat_msgs = [("assistant", "Fresh start! What's up?")]

    def choose_provider(self, pid):
        info = PROVIDERS[pid]
        cfg = load_ai_config()
        if info["needs_key"] and not cfg["api_keys"].get(pid):
            key = ask_text("AI key", f"Paste your free API key for {info['label']}:", password=True)
            if not key:
                return
            cfg["api_keys"][pid] = key
        cfg["provider"] = pid
        save_ai_config(cfg)
        self.chat_msgs.append(("assistant", f"Switched to {info['label']}."))

    def set_ai_model(self):
        cfg = load_ai_config()
        pid = cfg["provider"]
        current = cfg["models"].get(pid) or PROVIDERS[pid]["model"]
        model = ask_text("AI model", f"Model name for {PROVIDERS[pid]['label']}:", default=current)
        if model:
            cfg["models"][pid] = model
            save_ai_config(cfg)
            self.chat_msgs.append(("assistant", f"Model set to {model}."))

    def _draw_chat(self, p):
        """Right side of the chat page: message bubbles and the send button (cached bitmap)."""
        msgs = list(self.chat_msgs)
        if self.chat_busy:
            msgs.append(("assistant", "..."))
        fm = QFontMetrics(_font(13, False))
        p.setFont(_font(13, False))
        speak_msg = self.speak["msg"] if self.speak else None
        x_left, x_right = INPUT_RECT.x(), SEND_RECT.right() + 1
        y = INPUT_RECT.y() - 12
        drawn = 0
        for m in reversed(msgs):
            role, text = m
            r = fm.boundingRect(QRect(0, 0, 290, 4000), Qt.TextWordWrap, text)
            bw, bh = r.width() + 22, r.height() + 14
            if y - bh < 14 and drawn > 0:
                break
            y -= bh
            x = x_right - bw if role == "user" else x_left
            path = QPainterPath()
            path.addRoundedRect(QRectF(x, y, bw, bh), 13, 13)
            p.fillPath(path, BUBBLE_USER if role == "user" else BUBBLE_AI)
            shown = text[:self.reveal_len] if m is speak_msg else text
            p.setPen(QColor(255, 255, 255) if role == "user" else WHITE)
            p.drawText(QRect(int(x) + 11, int(y) + 7, bw - 22, bh - 14), Qt.TextWordWrap, shown)
            y -= 6
            drawn += 1

        # send button (round, with an arrow)
        c = SEND_RECT.center()
        p.setPen(Qt.NoPen)
        p.setBrush(BUBBLE_USER)
        p.drawEllipse(QPointF(c.x() + 0.5, c.y() + 0.5), 16, 16)
        pen = QPen(QColor(255, 255, 255), 2.2)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(c.x() + 0.5, c.y() + 8), QPointF(c.x() + 0.5, c.y() - 7))
        p.drawLine(QPointF(c.x() - 5.5, c.y() - 1.5), QPointF(c.x() + 0.5, c.y() - 7.5))
        p.drawLine(QPointF(c.x() + 6.5, c.y() - 1.5), QPointF(c.x() + 0.5, c.y() - 7.5))

    def _draw_character(self, p):
        """Left side of the chat page: the animated character (drawn live every frame)."""
        card = QRectF(14, 14, CARD_W, CARD_H)
        path = QPainterPath()
        path.addRoundedRect(card, 20, 20)
        if self.character is None:
            p.fillPath(path, QColor(235, 235, 240))
            draw_text(p, 14, 14, CARD_W, CARD_H, "no character\nfolder", 12, QColor(90, 90, 100),
                      align=Qt.AlignCenter)
            return
        p.save()
        p.setClipPath(path, Qt.IntersectClip)
        self.character.draw(p, 14, 14, time.monotonic(), self.mouth_state)
        p.restore()
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(fg_alpha(50), 1))
        p.drawPath(path)

    def quit_with_goodbye(self, silent=False):
        if silent or sound_path("goodbye") is None:
            QApplication.quit()
            return
        self.hide()
        self.bubble.hide()
        self.theme_island.hide()
        self.gear_island.hide()
        self.sound.done.connect(QApplication.quit)
        self.sound.play("goodbye", quit_after=True)

    # ----- page 1: music -----
    def _draw_music(self, p):
        art_rect = QRectF(20, 20, 72, 72)
        art_path = QPainterPath()
        art_path.addRoundedRect(art_rect, 14, 14)
        if self.art and not self.art.isNull():
            p.save()
            p.setClipPath(art_path, Qt.IntersectClip)
            scaled = self.art.scaled(72, 72, Qt.KeepAspectRatioByExpanding,
                                     Qt.SmoothTransformation)
            p.drawPixmap(int(art_rect.x() - (scaled.width() - 72) / 2),
                         int(art_rect.y() - (scaled.height() - 72) / 2), scaled)
            p.restore()
        else:
            p.fillPath(art_path, fg_alpha(40))
            draw_text(p, 20, 20, 72, 72, "\u266a", 28, MUTED,
                      align=Qt.AlignCenter)

        f_title = QFont("Segoe UI")
        f_title.setPixelSize(16)
        f_title.setWeight(QFont.Bold)
        title = QFontMetrics(f_title).elidedText(self.title or "-", Qt.ElideRight, 266)
        draw_text(p, 108, 18, 270, 24, title, 16, WHITE, True)

        f_art = QFont("Segoe UI")
        f_art.setPixelSize(13)
        artist = QFontMetrics(f_art).elidedText(self.artist, Qt.ElideRight, 266)
        draw_text(p, 108, 42, 270, 20, artist, 13, MUTED)

        p.setPen(Qt.NoPen)
        p.setBrush(WHITE)
        self._icon_skip(p, self.rect_prev.center(), left=True)
        self._icon_skip(p, self.rect_next.center(), left=False)
        c = self.rect_play.center()
        p.drawEllipse(QPointF(c.x(), c.y()), 20, 20)
        p.setBrush(BODY_SOLID)
        if self.playing:
            p.drawRoundedRect(QRectF(c.x() - 7, c.y() - 8, 5, 16), 1.5, 1.5)
            p.drawRoundedRect(QRectF(c.x() + 2, c.y() - 8, 5, 16), 1.5, 1.5)
        else:
            tri = QPainterPath()
            tri.moveTo(c.x() - 5, c.y() - 9)
            tri.lineTo(c.x() - 5, c.y() + 9)
            tri.lineTo(c.x() + 9, c.y())
            tri.closeSubpath()
            p.drawPath(tri)

    def _draw_music_progress(self, p):
        p.setPen(Qt.NoPen)
        pos = self.current_pos()
        frac = (pos / self.dur) if self.dur > 0 else 0.0
        bar = QRectF(20, 114, 350, 4)
        p.setBrush(fg_alpha(50))
        p.drawRoundedRect(bar, 2, 2)
        p.setBrush(WHITE)
        p.drawRoundedRect(QRectF(bar.x(), bar.y(), max(4, bar.width() * frac), 4), 2, 2)
        draw_text(p, 20, 119, 80, 16, fmt(pos), 11, MUTED)
        draw_text(p, 290, 119, 80, 16, fmt(self.dur), 11, MUTED,
                  align=Qt.AlignRight | Qt.AlignVCenter)

    @staticmethod
    def _icon_skip(p, c, left):
        s = -1 if left else 1
        tri = QPainterPath()
        tri.moveTo(c.x() - 7 * s, c.y() - 8)
        tri.lineTo(c.x() - 7 * s, c.y() + 8)
        tri.lineTo(c.x() + 5 * s, c.y())
        tri.closeSubpath()
        p.drawPath(tri)
        bx = c.x() + 6 * s if not left else c.x() - 8
        p.drawRoundedRect(QRectF(bx, c.y() - 8, 3, 16), 1, 1)

    # ----- page 2: clock -----
    def _draw_clock(self, p):
        now = datetime.datetime.now()

        # digital time + seconds
        f = QFont("Segoe UI")
        f.setPixelSize(54)
        f.setWeight(QFont.Bold)
        hm = now.strftime("%H:%M")
        adv = QFontMetrics(f).horizontalAdvance(hm)
        draw_text(p, 20, 26, 220, 64, hm, 54, WHITE, True)
        draw_text(p, 20 + adv + 4, 50, 40, 30, f":{now.second:02d}", 22, ACCENT_RED, True,
                  Qt.AlignLeft | Qt.AlignBottom)
        draw_text(p, 20, 94, 190, 20, now.strftime("%A"), 14, WHITE, True)
        draw_text(p, 20, 114, 190, 18, now.strftime("%d %B %Y"), 12, GRAY)

        # analog clock
        cx, cy, r = 312, 70, 50
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(fg_alpha(45), 2))
        p.drawEllipse(QPointF(cx, cy), r, r)
        for i in range(12):
            a = i * math.pi / 6
            major = i % 3 == 0
            inner = r - (9 if major else 5)
            pen = QPen(WHITE if major else MUTED, 2.2 if major else 1.2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(cx + math.sin(a) * inner, cy - math.cos(a) * inner),
                       QPointF(cx + math.sin(a) * (r - 3), cy - math.cos(a) * (r - 3)))

        sec = now.second + now.microsecond / 1e6
        minute = now.minute + sec / 60
        hour = (now.hour % 12) + minute / 60

        def hand(angle, length, width, color):
            pen = QPen(color, width)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(cx, cy),
                       QPointF(cx + math.sin(angle) * length, cy - math.cos(angle) * length))

        hand(hour * math.pi / 6, r * 0.5, 3.4, WHITE)
        hand(minute * math.pi / 30, r * 0.74, 2.6, WHITE)
        hand(sec * math.pi / 30, r * 0.82, 1.4, ACCENT_RED)
        p.setPen(Qt.NoPen)
        p.setBrush(ACCENT_RED)
        p.drawEllipse(QPointF(cx, cy), 3.2, 3.2)

    # ----- page 3: calendar -----
    def _draw_calendar(self, p):
        today = datetime.date.today()
        draw_text(p, 20, 18, 160, 18, today.strftime("%A").upper(), 12, ACCENT_RED, True)
        draw_text(p, 20, 34, 160, 60, str(today.day), 52, WHITE, True)
        draw_text(p, 20, 98, 160, 20, today.strftime("%B %Y"), 14, WHITE)

        x0, colw = 186, 27
        for i, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            draw_text(p, x0 + i * colw, 12, colw, 16, name, 10, MUTED, True,
                      Qt.AlignCenter)
        weeks = calendar.monthcalendar(today.year, today.month)
        for r, week in enumerate(weeks):
            for c, day in enumerate(week):
                if not day:
                    continue
                cx = x0 + c * colw + colw / 2
                cy = 38 + r * 17 + 8
                if day == today.day:
                    p.setPen(Qt.NoPen)
                    p.setBrush(ACCENT_RED)
                    p.drawEllipse(QPointF(cx, cy), 10, 10)
                    col = WHITE
                else:
                    col = MUTED if c >= 5 else WHITE
                draw_text(p, cx - colw / 2, cy - 9, colw, 18, str(day), 12, col,
                          day == today.day, Qt.AlignCenter)

    # ----- page 4: weather -----
    def _draw_weather(self, p):
        wd = self.weather
        if wd is None:
            draw_text(p, 20, 0, 350, H1, "Loading weather...", 15, GRAY,
                      align=Qt.AlignCenter)
            return
        if wd.get("error"):
            draw_text(p, 20, 40, 350, 30, "Weather unavailable", 16, WHITE, True,
                      Qt.AlignCenter)
            draw_text(p, 20, 70, 350, 24, "Check your internet connection", 12, GRAY,
                      align=Qt.AlignCenter)
            return

        kind = weather_kind(wd["code"])
        draw_text(p, 20, 14, 170, 18, wd.get("city") or "Weather", 13, GRAY)
        draw_text(p, 20, 30, 110, 56, f"{round(wd['temp'])}\u00b0", 46, WHITE, True)
        draw_weather_icon(p, 150, 60, 54, kind)
        draw_text(p, 20, 88, 175, 20, WEATHER_TEXT[kind], 14, WHITE)
        today = wd["days"][0]
        draw_text(p, 20, 110, 175, 18,
                  f"H {round(today['hi'])}\u00b0  L {round(today['lo'])}\u00b0  \u00b7  "
                  f"{round(wd['wind'])} km/h", 11, GRAY)

        # 3-day forecast
        for i, d in enumerate(wd["days"][1:4]):
            cx = 238 + i * 60
            draw_text(p, cx - 30, 18, 60, 16, d["name"], 12, GRAY, True, Qt.AlignCenter)
            draw_weather_icon(p, cx, 60, 30, weather_kind(d["code"]))
            draw_text(p, cx - 30, 82, 60, 18, f"{round(d['hi'])}\u00b0", 13, WHITE, True,
                      Qt.AlignCenter)
            draw_text(p, cx - 30, 100, 60, 18, f"{round(d['lo'])}\u00b0", 12, GRAY,
                      align=Qt.AlignCenter)


_crash_dialog_shown = False


def _log_crash(exc_type, exc, tb):
    """Write every unexpected error to %APPDATA%\\DynamicIsland\\error.log and show the
    first one in a message box (so a silent failure is no longer silent)."""
    global _crash_dialog_shown
    import traceback
    text = "".join(traceback.format_exception(exc_type, exc, tb))
    try:
        os.makedirs(AI_DIR, exist_ok=True)
        with open(os.path.join(AI_DIR, "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "\n" + text + "\n")
    except Exception:
        pass
    if not _crash_dialog_shown:
        _crash_dialog_shown = True
        try:
            ctypes.windll.user32.MessageBoxW(
                0, text[-1400:] + "\n(saved to %APPDATA%\\DynamicIsland\\error.log)",
                "Dynamic Island - error", 0x10)
        except Exception:
            pass


def main():
    sys.excepthook = _log_crash
    app = QApplication(sys.argv)
    apply_theme(load_settings().get("theme", "Dark"))
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)   # 1 ms timer resolution for smooth animation
    except Exception:
        pass
    app.setQuitOnLastWindowClosed(False)   # we quit explicitly (after the goodbye sound)
    icon_file = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
                             "icon.ico")
    if os.path.isfile(icon_file):
        app.setWindowIcon(QIcon(icon_file))
    worker = MediaWorker()
    weather = WeatherWorker()
    bubble = Bubble()
    chat = ChatWorker()
    island = Island(worker, weather, bubble, chat)
    watcher = AppWatcher()
    watcher.fl_changed.connect(island.on_fl)
    island.show()

    setup_autostart()
    if "--startup" in sys.argv:          # launched by Windows at logon
        island.sound.play("hello", delay=3.0)   # give audio devices a moment to come up
    shutdown_filter = ShutdownFilter(island.sound)
    app.installNativeEventFilter(shutdown_filter)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
