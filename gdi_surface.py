# -*- coding: utf-8 -*-
"""
GDI 合成后端
============

把雪花和卡片直接画进分层窗口的 32 位 DIB，然后一次 UpdateLayeredWindow 提交。

相比原路径（Pillow 画整屏 → tobytes 预乘转换 → memmove 进 DIB）省掉了两块固定开销：
  * 整幅位图的预乘 alpha + BGRA 转换（2240×1400 约 11 ms）
  * 12.5 MB 的 memcpy（约 4 ms）

GDI 的 AlphaBlend 语义是 dst = src + dst * (1 - srcA)，正好是预乘 alpha 的 over 合成，
所以雪花精灵和卡片都按预乘格式准备好即可。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
msimg32 = ctypes.WinDLL("msimg32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
BLACKNESS = 0x00000042
SMOOTHING_OFF = 0

BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
ULW_ALPHA = 0x00000002


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wt.DWORD * 3)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [wt.HDC, ctypes.POINTER(BITMAPINFO), wt.UINT, ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteObject.restype = wt.BOOL
gdi32.DeleteDC.argtypes = [wt.HDC]
gdi32.DeleteDC.restype = wt.BOOL
msimg32.AlphaBlend.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             BLENDFUNCTION]
msimg32.AlphaBlend.restype = wt.BOOL
gdi32.PatBlt.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.DWORD]
gdi32.PatBlt.restype = wt.BOOL
gdi32.GdiFlush.argtypes = []
gdi32.GdiFlush.restype = wt.BOOL
user32.GetDC.argtypes = [wt.HWND]
user32.GetDC.restype = wt.HDC
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
user32.ReleaseDC.restype = ctypes.c_int
user32.UpdateLayeredWindow.argtypes = [wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT),
                                       ctypes.POINTER(SIZE), wt.HDC, ctypes.POINTER(wt.POINT),
                                       wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD]
user32.UpdateLayeredWindow.restype = wt.BOOL


class Bitmap:
    """一块 32 位预乘 BGRA 位图 + 配套内存 DC。"""

    __slots__ = ("w", "h", "dc", "bitmap", "old", "bits")

    def __init__(self, w: int, h: int):
        self.w = max(1, int(w))
        self.h = max(1, int(h))
        screen = user32.GetDC(None)
        self.dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = self.w
        info.bmiHeader.biHeight = -self.h          # 负高度 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.dc, ctypes.byref(info), DIB_RGB_COLORS,
                                             ctypes.byref(self.bits), None, 0)
        self.old = gdi32.SelectObject(self.dc, self.bitmap)

    def upload_rows(self, data: bytes, pitch: int | None = None) -> None:
        """把预乘 BGRA 数据拷进位图。"""
        size = self.w * self.h * 4
        ctypes.memmove(self.bits, data, min(size, len(data)))

    def clear(self) -> None:
        ctypes.memset(self.bits, 0, self.w * self.h * 4)

    def close(self) -> None:
        if self.dc:
            gdi32.SelectObject(self.dc, self.old)
            gdi32.DeleteObject(self.bitmap)
            gdi32.DeleteDC(self.dc)
            self.dc = None


class GdiSurface:
    """分层窗口的绘制表面：直接画在 DIB 上，最后一次性提交。"""

    def __init__(self, hwnd, x: int, y: int, w: int, h: int):
        self.hwnd = hwnd
        self.x = int(x)
        self.y = int(y)
        self.w = int(w)
        self.h = int(h)
        self.canvas = Bitmap(self.w, self.h)
        self._sprites = {}
        self._images = {}
        self._bf_cache = {}
        self._dst_point = wt.POINT(self.x, self.y)
        self._src_point = wt.POINT(0, 0)
        self._size = SIZE(self.w, self.h)
        self._blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
        self._dirty: list[tuple[int, int, int, int]] = []

    # -- 精灵 -------------------------------------------------------------------
    def sprite(self, radius: int, alpha: int, shape: str = "round") -> Bitmap:
        """半径/透明度量化后缓存的雪花精灵（预乘白色软圆点）。"""
        from PIL import Image, ImageDraw, ImageFilter

        radius = max(1, int(radius))
        alpha = max(0, min(255, int(alpha)))
        key = (radius, alpha, shape)
        hit = self._sprites.get(key)
        if hit is not None:
            return hit
        size = radius * 2
        big = size * 3
        img = Image.new("L", (big, big), 0)
        d = ImageDraw.Draw(img)
        if shape == "round":
            d.ellipse([0, 0, big - 1, big - 1], fill=255)
        else:
            d.rectangle([0, 0, big - 1, big - 1], fill=255)
        img = img.filter(ImageFilter.GaussianBlur(big * 0.06))
        img = img.resize((size, size), Image.LANCZOS)
        mask = img.point(lambda v: int(v * alpha / 255))
        premul = Image.new("RGBA", (size, size), (255, 255, 255, 0))
        premul.putalpha(mask)
        data = premul.tobytes("raw", "BGRa")
        bmp = Bitmap(size, size)
        bmp.upload_rows(data)
        self._sprites[key] = bmp
        return bmp

    # -- 绘制 -------------------------------------------------------------------
    def erase(self, x: int, y: int, w: int, h: int) -> None:
        if w <= 0 or h <= 0:
            return
        x0, y0 = max(0, int(x)), max(0, int(y))
        x1, y1 = min(self.w, int(x + w)), min(self.h, int(y + h))
        if x1 <= x0 or y1 <= y0:
            return
        gdi32.PatBlt(self.canvas.dc, x0, y0, x1 - x0, y1 - y0, BLACKNESS)

    def snow(self, cx: float, cy: float, radius: float, alpha: float) -> None:
        if alpha <= 0.01 or radius < 0.5:
            return
        r = int(round(radius))
        a = int(round(max(0.0, min(1.0, alpha)) * 255))
        if a <= 2:
            return
        a = (a // 16) * 16 + 8          # 量化到 16 级，缓存更省
        bmp = self.sprite(r, a)
        msimg32.AlphaBlend(self.canvas.dc, int(cx - r), int(cy - r), bmp.w, bmp.h,
                         bmp.dc, 0, 0, bmp.w, bmp.h,
                         BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA))

    # -- 位图缓存 ---------------------------------------------------------------
    def image(self, key, pil_image) -> Bitmap:
        """把 PIL 图（RGBA）缓存成预乘位图。调用方保证 key 与内容一一对应。"""
        entry = self._images.get(key)
        if entry is not None and entry[0] == (pil_image.width, pil_image.height):
            return entry[1]
        data = pil_image.tobytes("raw", "BGRa")
        bmp = Bitmap(pil_image.width, pil_image.height)
        bmp.upload_rows(data)
        if entry is not None:
            entry[1].close()
        elif len(self._images) >= 16:
            oldest = next(iter(self._images))
            self._images.pop(oldest)[1].close()
        self._images[key] = ((pil_image.width, pil_image.height), bmp)
        return bmp

    def _bf(self, alpha: int) -> BLENDFUNCTION:
        """按透明度缓存 BLENDFUNCTION，避免每次调用都新建结构体。"""
        alpha = max(0, min(255, int(alpha)))
        hit = self._bf_cache.get(alpha)
        if hit is None:
            hit = BLENDFUNCTION(AC_SRC_OVER, 0, alpha, AC_SRC_ALPHA)
            self._bf_cache[alpha] = hit
        return hit

    def blit(self, bmp: Bitmap, x: int, y: int, const_alpha: int = 255) -> None:
        if not bmp:
            return
        a = max(0, min(255, int(const_alpha)))
        if a <= 0:
            return
        msimg32.AlphaBlend(self.canvas.dc, int(x), int(y), bmp.w, bmp.h,
                           bmp.dc, 0, 0, bmp.w, bmp.h,
                           BLENDFUNCTION(AC_SRC_OVER, 0, a, AC_SRC_ALPHA))

    def clear(self) -> None:
        self.canvas.clear()

    def present(self, alpha: int = 255) -> None:
        """把整块表面提交给窗口。"""
        blend = BLENDFUNCTION(AC_SRC_OVER, 0, max(0, min(255, int(alpha))), AC_SRC_ALPHA)
        gdi32.GdiFlush()
        user32.UpdateLayeredWindow(self.hwnd, None, ctypes.byref(self._dst_point),
                                   ctypes.byref(self._size), self.canvas.dc,
                                   ctypes.byref(self._src_point), 0,
                                   ctypes.byref(blend), ULW_ALPHA)

    def close(self) -> None:
        for bmp in self._sprites.values():
            bmp.close()
        self._sprites.clear()
        for _size, bmp in self._images.values():
            bmp.close()
        self._images.clear()
        if self.canvas:
            self.canvas.close()
            self.canvas = None
