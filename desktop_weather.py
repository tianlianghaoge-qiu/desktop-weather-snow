#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌面天气 · 低温飘雪
==================

一个纯 Python 的桌面小部件：

  * 透明、无边框、鼠标点击穿透的浮层，始终贴合桌面
  * 显示实时气温、体感温度、天气状况、最高/最低温、风力风向
  * 气温低于阈值（默认 3°C）时，整个桌面开始飘雪，雪量随温度降低而增大
  * 无外部依赖（除 Pillow 外），天气数据来自 Open-Meteo，无需 API Key

快捷键
------
  Ctrl + Alt + Q   退出
  Ctrl + Alt + S   切换飘雪：自动 → 强制开启 → 关闭
  Ctrl + Alt + R   立即刷新天气
  Ctrl + Alt + H   显示 / 隐藏天气卡片

命令行
------
  python desktop_weather.py                 正常运行（后台常驻）
  python desktop_weather.py --snow          强制下雪（预览效果）
  python desktop_weather.py --snapshot a.png 渲染一帧图片后退出（调试用）
  python desktop_weather.py --quit          关闭正在运行的实例
"""

from __future__ import annotations

import argparse
import ctypes
import json
import logging
import math
import os
import random
import sys
import threading
import time
import urllib.parse
import urllib.request
from ctypes import wintypes as wt
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# --------------------------------------------------------------------------------------
# 常量与路径
# --------------------------------------------------------------------------------------

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
LOCATION_CACHE = APP_DIR / "location.json"
LOG_PATH = APP_DIR / "weather.log"

MUTEX_NAME = "Local\\DSH_DesktopWeather_SingleInstance"
QUIT_EVENT_NAME = "Local\\DSH_DesktopWeather_Quit"

WINDIR = Path(os.environ.get("WINDIR", r"C:\Windows"))
FONTS = WINDIR / "Fonts"
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", str(APP_DIR))

DEFAULTS = {
    "_说明": "修改后重启程序生效。city 留空则自动定位；snow_threshold 为飘雪温度阈值（摄氏度）；snow_mode: auto/on/off",
    "city": "",
    "snow_threshold": 3.0,
    "snow_mode": "auto",
    "snow_density": 1.0,
    "position": "bottom-right",
    "margin": 28,
    "opacity": 0.92,
    "fps": 20,
    "auto_hide": False,
    "show_card": True,
    "refresh_minutes": 10,
}

SNOW_CODES = {71, 73, 75, 77, 85, 86}
WM_DESTROY = 0x0002
WM_ERASEBKGND = 0x0014
WM_HOTKEY = 0x0312
WM_DISPLAYCHANGE = 0x007E

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_NOREPEAT = 0x4000

HOTKEY_QUIT = 1
HOTKEY_SNOW = 2
HOTKEY_REFRESH = 3
HOTKEY_CARD = 4
VK = {"Q": 0x51, "S": 0x53, "R": 0x52, "H": 0x48}

HWND_BROADCAST = 0xFFFF
CMD_QUIT, CMD_SNOW_ON, CMD_SNOW_AUTO = 1, 2, 3


# --------------------------------------------------------------------------------------
# 日志 / 配置
# --------------------------------------------------------------------------------------

def setup_logging() -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 512 * 1024:
            LOG_PATH.unlink()
    except OSError:
        pass
    try:
        logging.basicConfig(
            filename=str(LOG_PATH),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s",
            encoding="utf-8",
        )
    except Exception:
        logging.basicConfig(level=logging.INFO)


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text("utf-8"))
            if isinstance(data, dict):
                for key, value in data.items():
                    if key in DEFAULTS:
                        cfg[key] = value
        except Exception:
            logging.warning("config.json 解析失败，已使用默认配置")
    else:
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except OSError:
        logging.warning("无法写入 config.json")


# --------------------------------------------------------------------------------------
# Win32
# --------------------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wt.LONG), ("y", wt.LONG)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", wt.LONG), ("cy", wt.LONG)]


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG), ("right", wt.LONG), ("bottom", wt.LONG)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
                ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM),
                ("lParam", wt.LPARAM), ("time", wt.DWORD), ("pt", POINT)]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wt.ATOM
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(POINT),
                                       ctypes.POINTER(SIZE), wt.HDC, ctypes.POINTER(POINT),
                                       wt.DWORD, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL
user32.PeekMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, wt.UINT, wt.UINT, wt.UINT]
user32.PeekMessageW.restype = wt.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, wt.UINT, wt.UINT]
user32.RegisterHotKey.restype = wt.BOOL
user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetForegroundWindow.restype = wt.HWND
user32.IsIconic.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(RECT)]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, ctypes.c_void_p, wt.UINT]
user32.SystemParametersInfoW.restype = wt.BOOL
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL
user32.RegisterWindowMessageW.argtypes = [wt.LPCWSTR]
user32.RegisterWindowMessageW.restype = wt.UINT
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL

gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT,
                                   ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ

kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
kernel32.CreateEventW.restype = wt.HANDLE
kernel32.OpenEventW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
kernel32.OpenEventW.restype = wt.HANDLE
kernel32.SetEvent.argtypes = [wt.HANDLE]
kernel32.SetEvent.restype = wt.BOOL
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CloseHandle.restype = wt.BOOL
kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
kernel32.WaitForSingleObject.restype = wt.DWORD

user32.GetDpiForSystem.restype = wt.UINT
user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetProcessDpiAwarenessContext.restype = wt.BOOL
user32.SetProcessDPIAware.restype = wt.BOOL

WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
WS_VISIBLE = 0x10000000
SW_SHOWNOACTIVATE = 4
ULW_ALPHA = 2
AC_SRC_OVER = 0
AC_SRC_ALPHA = 1
BI_RGB = 0
DIB_RGB_COLORS = 0
HWND_TOPMOST = -1
SPI_GETWORKAREA = 0x0030
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

WM_CMD = user32.RegisterWindowMessageW("DSH_DesktopWeather_Cmd")


def enable_dpi_awareness() -> None:
    """让进程按物理像素工作，避免 Windows 位图拉伸导致发虚。"""
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


def dpi_scale() -> float:
    try:
        dpi = user32.GetDpiForSystem()
    except Exception:
        dpi = 96
    if not dpi or dpi < 48:
        dpi = 96
    return dpi / 96.0


def virtual_screen() -> tuple[int, int, int, int]:
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if w <= 0 or h <= 0:
        w = user32.GetSystemMetrics(0) or 1920
        h = user32.GetSystemMetrics(1) or 1080
        x = y = 0
    return x, y, w, h


def work_area() -> RECT:
    rect = RECT()
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(rect), 0):
        rect.left, rect.top, rect.right, rect.bottom = 0, 0, 1920, 1080
    return rect


class Overlay:
    """一个 WS_EX_LAYERED 的全屏浮层：逐像素透明、鼠标穿透、永不被激活。"""

    _class_name = "DesktopWeatherOverlay"
    _class_registered = False

    def __init__(self, x: int, y: int, w: int, h: int, hotkey_cb, display_cb, command_cb=None):
        self.hotkey_cb = hotkey_cb
        self.display_cb = display_cb
        self.command_cb = command_cb
        self.hwnd = None
        self.screen_dc = None
        self.mem_dc = None
        self.hbmp = None
        self.nbytes = w * h * 4

        hinst = kernel32.GetModuleHandleW(None)
        self._proc = WNDPROC(self._wndproc)
        if not Overlay._class_registered:
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self._class_name
            atom = user32.RegisterClassExW(ctypes.byref(wc))
            if not atom:
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    raise ctypes.WinError(err)
            Overlay._class_registered = True

        ex_style = (WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST |
                    WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
        self.hwnd = user32.CreateWindowExW(ex_style, self._class_name, "DesktopWeather",
                                           WS_POPUP, x, y, w, h, None, None, hinst, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h          # 负数 = 自上而下
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB
        bits = ctypes.c_void_p()
        self.hbmp = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), DIB_RGB_COLORS,
                                           ctypes.byref(bits), None, 0)
        if not self.hbmp:
            raise ctypes.WinError(ctypes.get_last_error())
        gdi32.SelectObject(self.mem_dc, self.hbmp)
        self.bits = bits

        self.pt_dst = POINT(x, y)
        self.pt_src = POINT(0, 0)
        self.size = SIZE(w, h)
        self.blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self.msg = MSG()

    # -- 窗口过程 ---------------------------------------------------------------
    def _wndproc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_HOTKEY:
                if self.hotkey_cb:
                    self.hotkey_cb(int(wparam))
                return 0
            if msg == WM_DISPLAYCHANGE:
                if self.display_cb:
                    self.display_cb()
                return 0
            if msg == WM_CMD:
                if self.command_cb:
                    self.command_cb(int(wparam))
                return 0
            if msg == WM_ERASEBKGND:
                return 1
        except Exception:
            logging.exception("窗口过程异常")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- 绘制 -------------------------------------------------------------------
    def blit(self, image: Image.Image, alpha: int = 255) -> None:
        data = image.tobytes("raw", "BGRa")       # 预乘 alpha 的 BGRA
        ctypes.memmove(self.bits, data, self.nbytes)
        self.blend.SourceConstantAlpha = max(0, min(255, int(alpha)))
        ok = user32.UpdateLayeredWindow(self.hwnd, self.screen_dc, ctypes.byref(self.pt_dst),
                                        ctypes.byref(self.size), self.mem_dc,
                                        ctypes.byref(self.pt_src), 0,
                                        ctypes.byref(self.blend), ULW_ALPHA)
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())

    def show(self) -> None:
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)

    def pump(self) -> None:
        while user32.PeekMessageW(ctypes.byref(self.msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(self.msg))
            user32.DispatchMessageW(ctypes.byref(self.msg))

    def register_hotkeys(self) -> None:
        combos = [(HOTKEY_QUIT, VK["Q"]), (HOTKEY_SNOW, VK["S"]),
                  (HOTKEY_REFRESH, VK["R"]), (HOTKEY_CARD, VK["H"])]
        for hid, vk in combos:
            if not user32.RegisterHotKey(self.hwnd, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                logging.warning("热键注册失败 id=%s err=%s", hid, ctypes.get_last_error())

    def destroy(self) -> None:
        if self.hwnd:
            for hid in (HOTKEY_QUIT, HOTKEY_SNOW, HOTKEY_REFRESH, HOTKEY_CARD):
                user32.UnregisterHotKey(self.hwnd, hid)
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
        if self.hbmp:
            gdi32.DeleteObject(self.hbmp)
            self.hbmp = None
        if self.mem_dc:
            gdi32.DeleteDC(self.mem_dc)
            self.mem_dc = None
        if self.screen_dc:
            user32.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None


# --------------------------------------------------------------------------------------
# 天气数据
# --------------------------------------------------------------------------------------

WMO_ZH = {
    0: ("晴", "clear"), 1: ("晴间多云", "clear"), 2: ("局部多云", "partly"), 3: ("阴", "cloudy"),
    45: ("有雾", "fog"), 48: ("雾凇", "fog"),
    51: ("小毛毛雨", "drizzle"), 53: ("毛毛雨", "drizzle"), 55: ("大毛毛雨", "drizzle"),
    56: ("冻毛毛雨", "sleet"), 57: ("冻毛毛雨", "sleet"),
    61: ("小雨", "rain"), 63: ("中雨", "rain"), 65: ("大雨", "rain"),
    66: ("冻雨", "sleet"), 67: ("冻雨", "sleet"),
    71: ("小雪", "snow"), 73: ("中雪", "snow"), 75: ("大雪", "snow"), 77: ("米雪", "snow"),
    80: ("阵雨", "rain"), 81: ("强阵雨", "rain"), 82: ("暴雨", "rain"),
    85: ("阵雪", "snow"), 86: ("强阵雪", "snow"),
    95: ("雷阵雨", "thunder"), 96: ("雷阵雨夹冰雹", "thunder"), 99: ("雷暴冰雹", "thunder"),
}

WIND_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


def weather_text(code: int, is_day: int = 1) -> tuple[str, str]:
    text, kind = WMO_ZH.get(code, ("未知", "cloudy"))
    if kind == "clear" and not is_day:
        return ("晴", "clear-night")
    if kind == "partly" and not is_day:
        return ("局部多云", "partly-night")
    return text, kind


def wind_text(deg: float, speed: float) -> str:
    if speed is None:
        return ""
    idx = int(((deg or 0) + 22.5) % 360 / 45) % 8
    if speed < 0.5:
        return "无风"
    return f"{WIND_DIRS[idx]}风 {speed:.0f}km/h"


def http_json(url: str, timeout: float = 15.0):
    req = urllib.request.Request(url, headers={
        "User-Agent": "DesktopWeather/1.0 (Windows; Open-Meteo)",
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def guess_location() -> dict:
    """通过 IP 粗略定位（无需 Key）。"""
    try:
        js = http_json("http://ip-api.com/json/?fields=status,country,regionName,city,lat,lon,timezone", 8)
        if js.get("status") == "success" and js.get("lat") is not None:
            return {"lat": float(js["lat"]), "lon": float(js["lon"]),
                    "city": js.get("city") or "", "region": js.get("regionName") or ""}
    except Exception as exc:
        logging.info("ip-api 定位失败: %s", exc)
    try:
        js = http_json("https://ipapi.co/json/", 8)
        if js.get("latitude") is not None:
            return {"lat": float(js["latitude"]), "lon": float(js["longitude"]),
                    "city": js.get("city") or "", "region": js.get("region") or ""}
    except Exception as exc:
        logging.info("ipapi.co 定位失败: %s", exc)
    raise RuntimeError("无法获取当前位置")


def geocode(name: str) -> dict:
    query = urllib.parse.urlencode({"name": name, "count": 5, "language": "zh", "format": "json"})
    js = http_json("https://geocoding-api.open-meteo.com/v1/search?" + query, 12)
    results = js.get("results") or []
    if not results:
        raise RuntimeError(f"找不到城市：{name}")
    item = results[0]
    return {
        "lat": float(item["latitude"]),
        "lon": float(item["longitude"]),
        "city": item.get("name") or name,
        "region": item.get("admin1") or "",
    }


def clean_city(name: str) -> str:
    name = (name or "").strip()
    for suffix in ("市", "地区", "自治州"):
        if name.endswith(suffix) and len(name) > 2:
            return name[: -len(suffix)]
    return name


class WeatherService(threading.Thread):
    """后台线程：定位 → 拉取 Open-Meteo → 更新共享数据。"""

    def __init__(self, cfg: dict):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.lock = threading.Lock()
        self.data = None
        self.error = None
        self.version = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._location = None

    # -- 位置 -------------------------------------------------------------------
    def location(self) -> dict:
        city = (self.cfg.get("city") or "").strip()
        if self._location and self._location.get("source") == city:
            return self._location
        if not city:
            cached = self._read_cache()
            if cached and cached.get("lat") is not None:
                self._location = cached
                return cached
            guess = guess_location()
            # 用中文地名再反查一次，顺便拿到更精确的坐标
            try:
                if guess.get("city"):
                    precise = geocode(guess["city"])
                    precise["source"] = ""
                    self._write_cache(precise)
                    self._location = precise
                    return precise
            except Exception:
                pass
            guess["source"] = ""
            self._write_cache(guess)
            self._location = guess
            return guess
        loc = geocode(city)
        loc["source"] = city
        self._location = loc
        return loc

    @staticmethod
    def _read_cache():
        try:
            return json.loads(LOCATION_CACHE.read_text("utf-8"))
        except Exception:
            return None

    @staticmethod
    def _write_cache(data: dict) -> None:
        try:
            LOCATION_CACHE.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
        except OSError:
            pass

    # -- 数据 -------------------------------------------------------------------
    def fetch(self) -> dict:
        loc = self.location()
        params = {
            "latitude": f"{loc['lat']:.4f}",
            "longitude": f"{loc['lon']:.4f}",
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,"
                       "weather_code,wind_speed_10m,wind_direction_10m,is_day",
            "daily": "temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "forecast_days": "1",
        }
        js = http_json("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), 20)
        cur = js["current"]
        daily = js["daily"]
        return {
            "ok": True,
            "city": clean_city(loc.get("city") or "当前位置"),
            "region": loc.get("region") or "",
            "temp": float(cur["temperature_2m"]),
            "feels": float(cur["apparent_temperature"]),
            "humidity": int(cur.get("relative_humidity_2m") or 0),
            "code": int(cur["weather_code"]),
            "is_day": int(cur.get("is_day", 1)),
            "wind": float(cur.get("wind_speed_10m") or 0.0),
            "wind_dir": float(cur.get("wind_direction_10m") or 0.0),
            "tmax": float(daily["temperature_2m_max"][0]),
            "tmin": float(daily["temperature_2m_min"][0]),
            "clock": datetime.now().strftime("%H:%M"),
            "stamp": time.time(),
        }

    def run(self) -> None:
        delay = 0.0
        while not self._stop.is_set():
            try:
                data = self.fetch()
                with self.lock:
                    self.data = data
                    self.error = None
                    self.version += 1
                delay = max(2.0, float(self.cfg.get("refresh_minutes", 10)) * 60.0)
            except Exception as exc:
                logging.warning("天气更新失败: %s", exc)
                with self.lock:
                    self.error = str(exc)
                delay = 120.0
            self._wake.wait(delay)
            self._wake.clear()

    def refresh_now(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


# --------------------------------------------------------------------------------------
# 绘图素材
# --------------------------------------------------------------------------------------

SUN_COLOR = (255, 199, 94, 255)
MOON_COLOR = (208, 223, 255, 255)
CLOUD_LIGHT = (238, 243, 252, 255)
CLOUD_TINTED = (188, 202, 222, 255)
RAIN_COLOR = (136, 194, 255, 255)
SNOW_COLOR = (203, 232, 255, 255)
BOLT_COLOR = (255, 212, 104, 255)
ICE_COLOR = (170, 218, 255)


def _blend(dst: Image.Image, src: Image.Image, x: float, y: float) -> None:
    """把 src 以正确 alpha 合成到 dst 的 (x, y)，自动裁剪越界部分。"""
    x, y = int(round(x)), int(round(y))
    sw, sh = src.size
    dw, dh = dst.size
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(dw, x + sw), min(dh, y + sh)
    if x1 <= x0 or y1 <= y0:
        return
    if (x0, y0, x1, y1) != (x, y, x + sw, y + sh):
        src = src.crop((x0 - x, y0 - y, x1 - x, y1 - y))
    dst.alpha_composite(src, dest=(x0, y0))


def _cloud(d: ImageDraw.ImageDraw, k: float, cx: float, cy: float, w: float, color) -> None:
    """在 100×100 坐标系里画一朵云（底部基线在 cy）。"""
    r = w * 0.185
    fill = color[:3] + (color[3] if len(color) > 3 else 255,)

    def circle(x, y, rr):
        d.ellipse([(x - rr) * k, (y - rr) * k, (x + rr) * k, (y + rr) * k], fill=fill)

    d.rounded_rectangle([(cx - w * 0.40) * k, (cy - r * 1.05) * k,
                         (cx + w * 0.40) * k, (cy + r * 0.95) * k],
                        radius=r * 0.95 * k, fill=fill)
    circle(cx - w * 0.24, cy - r * 0.55, r * 1.02)
    circle(cx + w * 0.20, cy - r * 0.35, r * 0.88)
    circle(cx - w * 0.01, cy - r * 1.12, r * 1.22)


def _sun(d: ImageDraw.ImageDraw, k: float, cx: float, cy: float, r: float, color) -> None:
    for i in range(8):
        a = math.radians(i * 45)
        x1, y1 = cx + math.cos(a) * r * 1.45, cy + math.sin(a) * r * 1.45
        x2, y2 = cx + math.cos(a) * r * 1.90, cy + math.sin(a) * r * 1.90
        d.line([x1 * k, y1 * k, x2 * k, y2 * k], fill=color[:3] + (235,),
               width=max(1, int(r * 0.26 * k)))
    d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], fill=color)


def _snowflake(d: ImageDraw.ImageDraw, k: float, cx: float, cy: float, r: float,
               color, width: int) -> None:
    for i in range(3):
        a = math.radians(i * 60)
        dx, dy = math.cos(a) * r, math.sin(a) * r
        d.line([(cx - dx) * k, (cy - dy) * k, (cx + dx) * k, (cy + dy) * k],
               fill=color, width=width)
        for side in (-1, 1):
            b = a + side * math.radians(52)
            for t in (0.52, 0.78):
                bx, by = cx + dx * t, cy + dy * t
                d.line([bx * k, by * k, (bx + math.cos(b) * r * 0.30) * k,
                        (by + math.sin(b) * r * 0.30) * k], fill=color, width=width)


class Art:
    """字体、天气图标、雪花贴图缓存。"""

    def __init__(self, scale: float):
        self.s = scale
        self._fonts = {}
        self._icons = {}
        self._sprites = {}
        self._stars = {}

    def font(self, kind: str, size: float) -> ImageFont.FreeTypeFont:
        size = max(6, int(round(size)))
        key = (kind, size)
        if key not in self._fonts:
            files = {
                "light": FONTS / "segoeuil.ttf",
                "regular": FONTS / "segoeui.ttf",
                "semibold": FONTS / "seguisb.ttf",
                "cjk": FONTS / "msyh.ttc",
                "cjk_bold": FONTS / "msyhbd.ttc",
            }
            path = files.get(kind, files["cjk"])
            try:
                self._fonts[key] = ImageFont.truetype(str(path), size)
            except OSError:
                try:
                    self._fonts[key] = ImageFont.truetype(str(FONTS / "msyh.ttc"), size)
                except OSError:
                    self._fonts[key] = ImageFont.load_default(size)
        return self._fonts[key]

    # -- 天气图标 ---------------------------------------------------------------
    def icon(self, kind: str, size_px: int) -> Image.Image:
        key = (kind, size_px)
        if key in self._icons:
            return self._icons[key]
        ss = 4
        n = max(8, size_px * ss)
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        k = n / 100.0

        if kind == "clear":
            _sun(d, k, 50, 50, 21, SUN_COLOR)
        elif kind == "clear-night":
            mask = Image.new("L", (n, n), 0)
            md = ImageDraw.Draw(mask)
            md.ellipse([30 * k, 26 * k, 78 * k, 74 * k], fill=255)
            md.ellipse([46 * k, 18 * k, 92 * k, 64 * k], fill=0)
            img.paste(MOON_COLOR, (0, 0), mask)
        elif kind in ("partly", "partly-night"):
            if kind == "partly":
                _sun(d, k, 34, 34, 15, SUN_COLOR)
            else:
                mask = Image.new("L", (n, n), 0)
                md = ImageDraw.Draw(mask)
                md.ellipse([16 * k, 14 * k, 52 * k, 50 * k], fill=255)
                md.ellipse([28 * k, 8 * k, 62 * k, 42 * k], fill=0)
                img.paste(MOON_COLOR, (0, 0), mask)
            _cloud(d, k, 52, 66, 66, CLOUD_LIGHT)
        elif kind == "cloudy":
            _cloud(d, k, 42, 46, 56, CLOUD_LIGHT)
            _cloud(d, k, 58, 66, 62, (228, 236, 248, 255))
        elif kind == "fog":
            _cloud(d, k, 50, 50, 62, CLOUD_LIGHT)
            for i, y in enumerate((72, 82, 92)):
                wd = 46 - i * 6
                d.line([(50 - wd / 2) * k, y * k, (50 + wd / 2) * k, y * k],
                       fill=(210, 222, 238, 190), width=max(1, int(4 * k)))
        elif kind in ("rain", "drizzle", "sleet"):
            _cloud(d, k, 50, 46, 64, CLOUD_LIGHT)
            if kind == "drizzle":
                drops = [(36, 78), (50, 84), (64, 78)]
                ln = 7
            elif kind == "sleet":
                drops = [(36, 78), (64, 78)]
                ln = 9
            else:
                drops = [(34, 76), (50, 82), (66, 76)]
                ln = 11
            for x, y in drops:
                d.line([x * k, y * k, (x - 3) * k, (y + ln) * k],
                       fill=RAIN_COLOR, width=max(1, int(4.2 * k)))
            if kind == "sleet":
                _snowflake(d, k, 52, 84, 8, SNOW_COLOR, max(1, int(2.4 * k)))
        elif kind == "snow":
            _cloud(d, k, 50, 44, 64, CLOUD_LIGHT)
            for x, y, r in ((34, 76, 8), (56, 84, 7), (72, 72, 6)):
                _snowflake(d, k, x, y, r, SNOW_COLOR, max(1, int(2.6 * k)))
        elif kind == "thunder":
            _cloud(d, k, 50, 42, 64, CLOUD_TINTED)
            d.polygon([(52 * k, 58 * k), (38 * k, 82 * k), (50 * k, 82 * k),
                       (44 * k, 98 * k), (64 * k, 72 * k), (52 * k, 72 * k)], fill=BOLT_COLOR)
        else:
            _cloud(d, k, 50, 50, 64, CLOUD_LIGHT)

        small = img.resize((size_px, size_px), Image.LANCZOS)
        self._icons[key] = small
        return small

    # -- 雪花贴图 ---------------------------------------------------------------
    def flake(self, radius: int, alpha: int, soft: int = 0) -> Image.Image:
        radius = max(1, int(radius))
        bucket = max(1, min(15, int(round(alpha / 17.0))))
        key = (radius, bucket, soft)
        if key in self._sprites:
            return self._sprites[key]
        a = bucket * 17
        blur = max(0.4, radius * (0.08 + 0.10 * soft))
        pad = int(math.ceil(blur * 2.6)) + 1
        d = radius * 2 + pad * 2 + 1
        mask = Image.new("L", (d, d), 0)
        md = ImageDraw.Draw(mask)
        c = d / 2 - 0.5
        peak = 1.06 * (1.0 - 0.16 * soft)
        for i in range(4):
            rr = radius * (1.0 - i * 0.19)
            val = int(a * peak * (0.72 + 0.28 * i / 3.0))
            md.ellipse([c - rr, c - rr, c + rr, c + rr], fill=val)
        mask = mask.filter(ImageFilter.GaussianBlur(blur))
        img = Image.new("RGBA", (d, d), (255, 255, 255, 0))
        img.putalpha(mask)
        img = self._with_shadow(img, mask, radius)
        self._sprites[key] = img
        return img

    @staticmethod
    def _with_shadow(img: Image.Image, mask: Image.Image, radius: int) -> Image.Image:
        """给雪花加一层很淡的投影，让它在浅色桌面/窗口上也能看清。"""
        w, h = img.size
        big = Image.new("RGBA", (w + 2, h + 2), (0, 0, 0, 0))
        shadow = Image.new("RGBA", (w, h), (30, 48, 76, 0))
        shadow.putalpha(mask.point(lambda v: int(v * 0.32)))
        big.alpha_composite(shadow, dest=(1, 1 + (2 if radius >= 5 else 1)))
        big.alpha_composite(img, dest=(1, 1))
        return big

    def star(self, radius: int, alpha: int) -> Image.Image:
        radius = max(2, int(radius))
        bucket = max(1, min(15, int(round(alpha / 17.0))))
        key = (radius, bucket)
        if key in self._stars:
            return self._stars[key]
        a = bucket * 17
        n = radius * 4 + 2
        mask = Image.new("L", (n, n), 0)
        md = ImageDraw.Draw(mask)
        c = n / 2
        for i in range(3):
            ang = math.radians(i * 60)
            dx, dy = math.cos(ang) * radius * 1.7, math.sin(ang) * radius * 1.7
            md.line([c - dx, c - dy, c + dx, c + dy], fill=a, width=max(1, int(radius * 0.42)))
        mask = mask.filter(ImageFilter.GaussianBlur(0.7))
        img = Image.new("RGBA", (n, n), (255, 255, 255, 0))
        img.putalpha(mask)
        img = self._with_shadow(img, mask, radius)
        self._stars[key] = img
        return img


# --------------------------------------------------------------------------------------
# 天气卡片
# --------------------------------------------------------------------------------------

class CardPainter:
    """把天气数据画成一张带圆角、半透明玻璃质感的 RGBA 卡片。"""

    W = 412.0
    H = 142.0

    def __init__(self, art: Art, cfg: dict):
        self.art = art
        self.cfg = cfg
        self.s = art.s
        self._panel_cache = {}
        self._shadow_cache = {}

    # -- 面板底 ----------------------------------------------------------------
    def _panel(self, snowy: bool) -> Image.Image:
        key = snowy
        if key in self._panel_cache:
            return self._panel_cache[key]
        w, h = int(round(self.W * self.s)), int(round(self.H * self.s))
        r = 21 * self.s
        base_alpha = int(max(0.2, min(1.0, float(self.cfg.get("opacity", 0.92)))) * 238)
        border = (150, 208, 255, 78) if snowy else (255, 255, 255, 34)
        panel = rounded_panel((w, h), r, fill=(12, 17, 27, base_alpha),
                              border=border, border_width=max(1.0, 1.1 * self.s))
        self._panel_cache[key] = panel
        return panel

    def _shadow(self, snowy: bool) -> tuple[Image.Image, int]:
        key = snowy
        if key in self._shadow_cache:
            return self._shadow_cache[key]
        w, h = int(round(self.W * self.s)), int(round(self.H * self.s))
        r = 21 * self.s
        pad = int(26 * self.s)
        mask = Image.new("L", (w + pad * 2, h + pad * 2), 0)
        md = ImageDraw.Draw(mask)
        md.rounded_rectangle([pad, pad, pad + w, pad + h], r, fill=255)
        blur = mask.filter(ImageFilter.GaussianBlur(11 * self.s))
        shadow = Image.new("RGBA", blur.size, (3, 6, 12, 0))
        shadow.putalpha(blur.point(lambda v: min(190, int(v * 0.60))))
        if snowy:
            glow = mask.filter(ImageFilter.GaussianBlur(18 * self.s))
            gimg = Image.new("RGBA", blur.size, (120, 190, 255, 0))
            gimg.putalpha(glow.point(lambda v: int(v * 0.55)))
            shadow = Image.alpha_composite(shadow, gimg)
        self._shadow_cache[key] = (shadow, pad)
        return self._shadow_cache[key]

    # -- 主绘制 ----------------------------------------------------------------
    def draw(self, data: dict | None, snowy: bool) -> tuple[Image.Image, Image.Image, int]:
        s = self.s
        panel = self._panel(snowy).copy()
        shadow, pad = self._shadow(snowy)
        layer = Image.new("RGBA", panel.size, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)

        def put(xy, text, font, color, alpha=255, anchor="lt"):
            d.text((xy[0] * s, xy[1] * s), text, font=font, fill=color + (alpha,), anchor=anchor)

        f_temp = self.art.font("light", 60 * s)
        f_deg = self.art.font("light", 25 * s)
        f_cond = self.art.font("cjk", 15.5 * s)
        f_city = self.art.font("cjk", 10.5 * s)
        f_feels = self.art.font("cjk", 10 * s)
        f_tiny = self.art.font("cjk", 9.5 * s)

        placeholder = data is None
        humidity = None
        if placeholder:
            temp_s, feels_s, cond, kind = "—", "", "获取中", "cloudy"
            city, hi, lo, wind, clock = "", "", "", "", ""
        else:
            temp_s = f"{data['temp']:.0f}"
            feels_s = f"体感 {data['feels']:.1f}°"
            cond, kind = weather_text(data["code"], data["is_day"])
            city = data["city"]
            hi = f"最高 {data['tmax']:.1f}°"
            lo = f"最低 {data['tmin']:.1f}°"
            wind = wind_text(data["wind_dir"], data["wind"])
            clock = f"{data['clock']} 更新"
            humidity = data.get("humidity")

        # --- 温度 -----------------------------------------------------------------
        num_w = d.textlength(temp_s, font=f_temp) / s
        temp_color = (224, 244, 255) if snowy else (250, 252, 255)
        put((26, 18), temp_s, f_temp, temp_color)
        if not placeholder:
            put((28 + num_w, 22), "°", f_deg, temp_color, 205)
        if feels_s:
            put((30, 84), feels_s, f_feels, (255, 255, 255), 145)

        # --- 竖向分隔线 -----------------------------------------------------------
        d.line([(178 * s, 22 * s), (178 * s, 92 * s)], fill=(255, 255, 255, 24),
               width=max(1, int(1 * s)))

        # --- 图标 + 天气 ----------------------------------------------------------
        icon = self.art.icon(kind, int(round(38 * s)))
        layer.alpha_composite(icon, (int(196 * s), int(20 * s)))
        put((244, 24), cond, f_cond, (255, 255, 255), 244)
        if city:
            put((245, 52), city, f_city, (255, 255, 255), 138)

        # --- 最高 / 最低 ----------------------------------------------------------
        if hi:
            put((380, 26), hi, f_city, (255, 255, 255), 142, anchor="rt")
            put((380, 50), lo, f_city, (255, 255, 255), 112, anchor="rt")

        # --- 底部状态栏 -----------------------------------------------------------
        d.line([(26 * s, 106 * s), ((self.W - 26) * s, 106 * s)], fill=(255, 255, 255, 16),
               width=max(1, int(1 * s)))
        bx, by = 26.0, 114.0
        if snowy:
            _snowflake(d, 1.0, (bx + 5) * s, (by + 5.5) * s, 5.4 * s,
                       SNOW_COLOR[:3] + (245,), max(1, int(1.6 * s)))
            bx += 16
            label = "飘雪"
            put((bx, by), label, f_tiny, ICE_COLOR, 245)
            bx += d.textlength(label, font=f_tiny) / s + 11
            if humidity is not None:
                put((bx, by), "·", f_tiny, (255, 255, 255), 80)
                bx += 11
        if humidity is not None:
            put((bx, by), f"湿度 {humidity}%", f_tiny, (255, 255, 255), 125)
        elif placeholder:
            put((bx, by), "正在连接…", f_tiny, (255, 255, 255), 90)

        if wind:
            put((206, by), wind, f_tiny, (255, 255, 255), 112)
        if clock:
            put((386, by), clock, f_tiny, (255, 255, 255), 105, anchor="rt")

        panel.alpha_composite(layer)
        return panel, shadow, pad


def rounded_panel(size: tuple[int, int], radius: float, fill, border, border_width: float,
                  ss: int = 3) -> Image.Image:
    """在 3 倍尺寸下画圆角矩形再缩小，得到平滑边缘。"""
    w, h = size
    big = Image.new("RGBA", (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle([0, 0, w * ss - 1, h * ss - 1], radius * ss, fill=fill,
                        outline=border, width=max(1, int(border_width * ss)))
    return big.resize((w, h), Image.LANCZOS)


def make_pill(art: Art, text: str, alpha: int = 255) -> Image.Image:
    s = art.s
    font = art.font("cjk", 10.5 * s)
    probe = ImageDraw.Draw(Image.new("RGBA", (4, 4)))
    box = probe.textbbox((0, 0), text, font=font)
    tw, th = box[2] - box[0], box[3] - box[1]
    padx, pady = 14 * s, 8.5 * s
    w = int(tw + padx * 2)
    h = int(th + pady * 2)
    pill = rounded_panel((w, h), h / 2, fill=(10, 15, 24, 214),
                         border=(255, 255, 255, 30), border_width=max(1.0, 1.0 * s))
    d = ImageDraw.Draw(pill)
    d.text((w / 2, h / 2), text, font=font, fill=(255, 255, 255, 235), anchor="mm")
    if alpha < 255:
        a = pill.getchannel("A").point(lambda v: int(v * alpha / 255))
        pill.putalpha(a)
    return pill


# --------------------------------------------------------------------------------------
# 雪花
# --------------------------------------------------------------------------------------

class Flake:
    __slots__ = ("x", "y", "r", "vy", "phase", "sway", "sway_speed", "alpha",
                 "depth", "dying", "fade", "star")

    def __init__(self, x, y, r, vy, phase, sway, sway_speed, alpha, depth, star=False):
        self.x = x
        self.y = y
        self.r = r
        self.vy = vy
        self.phase = phase
        self.sway = sway
        self.sway_speed = sway_speed
        self.alpha = alpha
        self.depth = depth
        self.dying = False
        self.fade = 1.0
        self.star = star


class Snow:
    """全屏雪花场。"""

    def __init__(self, width: int, height: int, art: Art, density: float = 1.0):
        self.w = width
        self.h = height
        self.art = art
        self.s = art.s
        self.density = max(0.1, float(density))
        self.flakes: list[Flake] = []
        self.target = 0.0
        self.wind = 0.0
        self.time = 0.0
        self._base = (width * height) / 8000.0 * self.density

    def count_for(self, intensity: float) -> int:
        return int(self._base * max(0.0, min(1.0, intensity)))

    def prefill(self) -> None:
        """直接把雪花铺满屏幕，避免刚启动时从屏幕上方慢慢飘入。"""
        want = self.count_for(self.target)
        while len(self.flakes) < want:
            self.flakes.append(self._spawn(above=False))
        for f in self.flakes:
            f.y = random.uniform(0, self.h)

    def _spawn(self, above: bool = True) -> Flake:
        s = self.s
        depth = random.uniform(0.0, 1.0)
        r = max(1.2, (1.0 + 4.9 * depth ** 1.55) * s * 0.9)
        x = random.uniform(-20, self.w + 20)
        y = random.uniform(-self.h * 0.25, -10) if above else random.uniform(0, self.h)
        return Flake(
            x=x, y=y, r=r,
            vy=(22 + 70 * depth ** 0.85) * s,
            phase=random.uniform(0, math.tau),
            sway=random.uniform(6, 24) * s,
            sway_speed=random.uniform(0.35, 1.1),
            alpha=95 + 150 * depth ** 0.8,
            depth=depth,
            star=(depth > 0.92 and random.random() < 0.35),
        )

    def update(self, dt: float) -> None:
        self.time += dt
        want = self.count_for(self.target)
        alive = [f for f in self.flakes if not f.dying]
        if len(alive) < want:
            for _ in range(min(12, want - len(alive))):
                self.flakes.append(self._spawn(above=True))
        elif len(alive) > want:
            for _ in range(min(8, len(alive) - want)):
                victim = random.choice(alive)
                victim.dying = True
                alive.remove(victim)

        gust = 1.0 + 0.45 * math.sin(self.time * 0.23)
        dead = []
        for f in self.flakes:
            if f.dying:
                f.fade -= dt / 0.9
                if f.fade <= 0:
                    dead.append(f)
                    continue
            f.y += f.vy * dt
            f.x += (self.wind * gust * (0.35 + 0.65 * f.depth)
                    + math.sin(self.time * f.sway_speed + f.phase) * f.sway) * dt
            if f.y - f.r > self.h + 4:
                if f.dying:
                    dead.append(f)
                    continue
                f.y = random.uniform(-self.h * 0.15, -6)
                f.x = random.uniform(-20, self.w + 20)
            if f.x < -30:
                f.x = self.w + 20
            elif f.x > self.w + 30:
                f.x = -20
        if dead:
            dead_ids = {id(f) for f in dead}
            self.flakes = [f for f in self.flakes if id(f) not in dead_ids]

    def draw(self, frame: Image.Image) -> None:
        for f in self.flakes:
            a = f.alpha * max(0.0, min(1.0, f.fade))
            if a < 8:
                continue
            if f.star:
                sprite = self.art.star(int(f.r), int(a))
            else:
                soft = 0 if f.depth < 0.55 else (1 if f.depth < 0.85 else 2)
                sprite = self.art.flake(int(round(f.r)), int(a), soft)
            _blend(frame, sprite, f.x - sprite.width / 2, f.y - sprite.height / 2)


# --------------------------------------------------------------------------------------
# 主程序
# --------------------------------------------------------------------------------------

class App:
    def __init__(self, cfg: dict, force_snow: bool = False, headless: bool = False):
        self.cfg = cfg
        self.headless = headless
        self.running = True
        self.relayout_pending = False
        self.snow_mode = "on" if force_snow else str(cfg.get("snow_mode", "auto"))
        if self.snow_mode not in ("auto", "on", "off"):
            self.snow_mode = "auto"
        self.show_card = bool(cfg.get("show_card", True))
        self.weather_version = -1
        self.data = None
        self.card_img = None
        self.card_shadow = None
        self.card_pad = 0
        self.card_key = None
        self.toast = None            # (image, expire_ts)
        self.toast_text = ""
        self.snow_started = False
        self.alpha = 1.0
        self.alpha_target = 1.0
        self.overlay = None
        self.fps = max(5, min(60, int(cfg.get("fps", 20) or 20)))

        self.service = WeatherService(cfg)
        self.service.start()

        self.scale = dpi_scale()
        self.vx, self.vy, self.vw, self.vh = virtual_screen()
        self.art = Art(self.scale)
        self.painter = CardPainter(self.art, cfg)
        self.snow = Snow(self.vw, self.vh, self.art, cfg.get("snow_density", 1.0))
        self.frame = Image.new("RGBA", (self.vw, self.vh), (0, 0, 0, 0))
        self.card_pos = (0, 0)

        if not headless:
            self._create_overlay()
        self._request_card()

    # -- 初始化 ----------------------------------------------------------------
    def _create_overlay(self) -> None:
        if self.overlay:
            self.overlay.destroy()
        self.overlay = Overlay(self.vx, self.vy, self.vw, self.vh,
                               self.on_hotkey, self.on_display_change, self.on_command)
        self.overlay.register_hotkeys()
        self.overlay.show()

    def _compute_card_pos(self) -> None:
        s = self.scale
        rect = work_area()
        w, h = int(round(CardPainter.W * s)), int(round(CardPainter.H * s))
        margin = int(round(float(self.cfg.get("margin", 28)) * s))
        pos = str(self.cfg.get("position", "bottom-right")).lower()
        left = rect.left - self.vx + margin
        top = rect.top - self.vy + margin
        right = rect.right - self.vx - margin - w
        bottom = rect.bottom - self.vy - margin - h
        x = right if "right" in pos else left
        y = bottom if "bottom" in pos else top
        self.card_pos = (int(x), int(y))

    def _request_card(self) -> None:
        self.card_key = None

    def _rebuild_card(self) -> None:
        data = self.data
        snowy = self.snow.target > 0.02
        key = (id(data), data.get("stamp") if data else None, snowy)
        if key == self.card_key and self.card_img is not None:
            return
        self.card_key = key
        self.card_img, self.card_shadow, self.card_pad = self.painter.draw(data, snowy)
        self._compute_card_pos()

    # -- 状态 ------------------------------------------------------------------
    def snow_intensity(self) -> float:
        if self.snow_mode == "off":
            return 0.0
        if self.snow_mode == "on":
            return 0.62
        data = self.data
        if not data:
            return 0.0
        threshold = float(self.cfg.get("snow_threshold", 3.0))
        temp = float(data["temp"])
        base = 0.55 if int(data["code"]) in SNOW_CODES else 0.0
        if temp <= threshold:
            k = min(1.0, (threshold - temp) / 10.0)
            base = max(base, 0.30 + 0.70 * k)
        return base

    def _set_snow_mode(self, mode: str) -> None:
        self.snow_mode = mode
        label = {"auto": "自动（按气温）", "on": "强制开启", "off": "关闭"}[mode]
        self.toast_text = f"飘雪模式：{label}"
        self.toast = None
        self._request_card()

    def on_command(self, cmd: int) -> None:
        if cmd == CMD_QUIT:
            self.running = False
        elif cmd == CMD_SNOW_ON:
            self._set_snow_mode("on")
        elif cmd == CMD_SNOW_AUTO:
            self._set_snow_mode("auto")

    def on_hotkey(self, hid: int) -> None:
        if hid == HOTKEY_QUIT:
            self.running = False
        elif hid == HOTKEY_SNOW:
            order = ["auto", "on", "off"]
            self._set_snow_mode(order[(order.index(self.snow_mode) + 1) % 3])
        elif hid == HOTKEY_REFRESH:
            self.service.refresh_now()
            self.toast_text = "正在刷新天气…"
            self.toast = None
        elif hid == HOTKEY_CARD:
            self.show_card = not self.show_card
            self._request_card()

    def on_display_change(self) -> None:
        self.relayout_pending = True

    # -- 循环 ------------------------------------------------------------------
    def update(self, dt: float) -> None:
        with self.service.lock:
            data = self.service.data
            version = self.service.version
        if version != self.weather_version:
            self.weather_version = version
            self.data = data
            self._request_card()

        intensity = self.snow_intensity()
        self.snow.target = intensity
        if intensity > 0.02 and not self.snow_started:
            self.snow_started = True
            self.snow.prefill()
        elif intensity <= 0.02:
            self.snow_started = False
        if self.data:
            px = float(self.data.get("wind", 0.0)) * 0.62 * self.scale
            to_deg = math.radians((float(self.data.get("wind_dir", 0.0)) + 180.0) % 360)
            self.snow.wind = px * math.sin(to_deg)
        else:
            self.snow.wind = 0.0
        self.snow.update(dt)

        # 淡入淡出（auto_hide）
        if self.cfg.get("auto_hide") and not self.headless:
            hidden = self._foreground_covers_screen()
            self.alpha_target = 0.0 if hidden else 1.0
        else:
            self.alpha_target = 1.0
        if abs(self.alpha - self.alpha_target) > 0.005:
            step = dt * 3.5
            self.alpha += max(-step, min(step, self.alpha_target - self.alpha))
        else:
            self.alpha = self.alpha_target

        if self.toast and time.time() > self.toast[1]:
            self.toast = None
            self.toast_text = ""

        self._rebuild_card()

    def _foreground_covers_screen(self) -> bool:
        try:
            hwnd = user32.GetForegroundWindow()
            if not hwnd or user32.IsIconic(hwnd):
                return False
            rect = RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return False
            area = (rect.right - rect.left) * (rect.bottom - rect.top)
            return area >= self.vw * self.vh * 0.90
        except Exception:
            return False

    @property
    def animating(self) -> bool:
        if self.alpha <= 0.01 and self.alpha_target <= 0.01:
            return False
        return (bool(self.snow.flakes) or self.snow.target > 0
                or self.toast is not None
                or abs(self.alpha - self.alpha_target) > 0.01)

    def render(self) -> Image.Image:
        frame = self.frame
        frame.paste((0, 0, 0, 0), (0, 0, self.vw, self.vh))
        if self.snow.flakes:
            self.snow.draw(frame)
        if self.show_card and self.card_img is not None:
            x, y = self.card_pos
            pad = self.card_pad
            _blend(frame, self.card_shadow, x - pad, y - pad)
            _blend(frame, self.card_img, x, y)
        if self.toast_text:
            if self.toast is None:
                self.toast = (make_pill(self.art, self.toast_text), time.time() + 3.2)
            img, expire = self.toast
            remain = expire - time.time()
            if remain > 0:
                fade = min(1.0, remain / 0.6)
                if fade < 0.999:
                    tmp = img.copy()
                    tmp.putalpha(img.getchannel("A").point(lambda v: int(v * fade)))
                    img = tmp
                x, y = self.card_pos
                if self.card_img is not None:
                    x += (self.card_img.width - img.width) // 2
                    y -= img.height + int(10 * self.scale)
                _blend(frame, img, x, y)
        return frame

    def run(self) -> None:
        target = 1.0 / self.fps
        last = time.perf_counter()
        next_frame = last
        while self.running:
            self.overlay.pump()
            if self.relayout_pending:
                self._relayout()
            now = time.perf_counter()
            if now >= next_frame:
                dt = min(0.15, max(0.001, now - last))
                last = now
                self.update(dt)
                if self.alpha > 0.01:
                    frame = self.render()
                    self.overlay.blit(frame, int(255 * self.alpha))
                next_frame = now + (target if self.animating else 1.0)
            else:
                time.sleep(min(0.008, max(0.001, next_frame - now)))
        self.shutdown()

    def _relayout(self) -> None:
        logging.info("屏幕参数变化，重新布局")
        self.relayout_pending = False
        self.scale = dpi_scale()
        self.vx, self.vy, self.vw, self.vh = virtual_screen()
        self.art = Art(self.scale)
        self.painter = CardPainter(self.art, self.cfg)
        self.snow = Snow(self.vw, self.vh, self.art, self.cfg.get("snow_density", 1.0))
        self.snow_started = False
        self.frame = Image.new("RGBA", (self.vw, self.vh), (0, 0, 0, 0))
        self._create_overlay()
        self._request_card()

    def shutdown(self) -> None:
        self.service.stop()
        if self.overlay:
            self.overlay.destroy()
            self.overlay = None


def _acquire_mutex():
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if ctypes.get_last_error() == 183:      # ERROR_ALREADY_EXISTS
        return None
    return handle


def _quit_event_handle():
    return kernel32.OpenEventW(0x0002, False, QUIT_EVENT_NAME)


def _create_quit_event():
    return kernel32.CreateEventW(None, True, False, QUIT_EVENT_NAME)


def signal_quit() -> bool:
    handle = _quit_event_handle()
    if not handle:
        return False
    kernel32.SetEvent(handle)
    kernel32.CloseHandle(handle)
    return True


def broadcast_command(cmd: int) -> bool:
    """向正在运行的实例广播命令（HWND_BROADCAST 只送达顶层窗口）。"""
    try:
        return bool(user32.PostMessageW(HWND_BROADCAST, WM_CMD, cmd, 0))
    except Exception:
        return False


def snapshot(path: str, force_snow: bool, backdrop: str | None, wait: float) -> None:
    cfg = load_config()
    app = App(cfg, force_snow=force_snow, headless=True)
    deadline = time.time() + wait
    while time.time() < deadline and app.data is None:
        with app.service.lock:
            app.data = app.service.data
        time.sleep(0.1)
    with app.service.lock:
        app.data = app.service.data
    app.weather_version = app.service.version
    app.snow.target = app.snow_intensity() or (0.62 if force_snow else 0.0)
    app.snow.prefill()
    for _ in range(40):
        app.snow.update(1 / 30.0)
    app._request_card()
    app._rebuild_card()
    frame = app.render()

    if backdrop:
        if backdrop == "screen":
            from PIL import ImageGrab
            bg = ImageGrab.grab(all_screens=True).convert("RGBA")
        else:
            bg = Image.open(backdrop).convert("RGBA")
        if bg.size != frame.size:
            bg = bg.resize(frame.size, Image.LANCZOS)
        frame = Image.alpha_composite(bg, frame)
    frame.save(path)
    print(f"已保存 {path} ({frame.width}x{frame.height})")


def main(argv=None) -> int:
    setup_logging()
    enable_dpi_awareness()

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--snapshot", metavar="PATH", help="渲染一帧 PNG 后退出")
    parser.add_argument("--snow", action="store_true", help="强制开启飘雪（预览）")
    parser.add_argument("--backdrop", metavar="PATH", help="快照时叠加的背景图（screen 表示截屏）")
    parser.add_argument("--quit", action="store_true", help="关闭正在运行的实例")
    parser.add_argument("--wait", type=float, default=12.0, help="快照前等待天气数据的最长秒数")
    args = parser.parse_args(argv)

    if args.quit:
        broadcast_command(CMD_QUIT)
        signal_quit()
        print("已通知桌面天气退出")
        return 0

    if args.snapshot:
        snapshot(args.snapshot, args.snow, args.backdrop, args.wait)
        return 0

    mutex = _acquire_mutex()
    if mutex is None:
        if args.snow:
            broadcast_command(CMD_SNOW_ON)
            print("桌面天气已在运行，已切换为飘雪预览")
        else:
            print("桌面天气已经在运行了（Ctrl+Alt+S 可切换飘雪）")
        return 0

    cfg = load_config()
    app = App(cfg, force_snow=args.snow)
    quit_event = _create_quit_event()

    threading.Thread(target=_watch_quit_event, args=(app, quit_event), daemon=True).start()

    try:
        app.run()
    except KeyboardInterrupt:
        app.shutdown()
    finally:
        if quit_event:
            kernel32.CloseHandle(quit_event)
        if mutex:
            kernel32.CloseHandle(mutex)
    return 0


def _watch_quit_event(app: App, event_handle) -> None:
    while app.running:
        if kernel32.WaitForSingleObject(event_handle, 500) == 0:
            app.running = False
            break


if __name__ == "__main__":
    sys.exit(main())
