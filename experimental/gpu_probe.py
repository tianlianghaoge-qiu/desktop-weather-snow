# -*- coding: utf-8 -*-
"""GPU 渲染后端探针：创建一个 D3D11 + DComp 透明窗口，画几片雪花并截图。"""
import ctypes
import math
import sys
import time

sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\desktop_weather")

from PIL import ImageGrab

import gpu_renderer as g


def main() -> int:
    try:
        ov = g.GpuOverlay(900, 420, 200, 200)
    except Exception as exc:
        print("初始化失败:", exc)
        import traceback
        traceback.print_exc()
        return 1
    print("设备/交换链/DComp 初始化成功，hwnd =", hex(ov.hwnd))

    quads = []
    for i in range(160):
        cx = 60 + (i * 37) % 780
        cy = 40 + (i * 53) % 340
        quads.append((cx, cy, 3 + (i % 7), 0.35 + 0.5 * ((i % 5) / 4)))

    t0 = time.time()
    frames = 0
    try:
        while time.time() - t0 < 2.5:
            ov.render(quads, None, 1.0)
            frames += 1
            time.sleep(0.01)
    except Exception as exc:
        print("渲染失败:", exc)
        import traceback
        traceback.print_exc()
        return 1
    print(f"渲染 {frames} 帧，{frames / (time.time() - t0):.0f} fps")
    time.sleep(0.5)
    ImageGrab.grab(all_screens=True).save(
        r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\gpu_probe.png")
    print("已截图")
    time.sleep(0.3)
    ov.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
