# -*- coding: utf-8 -*-
"""逐个参数组合排查 CreateSwapChainForComposition 的 INVALID_CALL。"""
import ctypes
import sys

sys.path.insert(0, r"C:\Users\Administrator\Documents\deepseek-harness\default-workspace\desktop_weather")
import gpu_renderer as g

device = ctypes.c_void_p()
context = ctypes.c_void_p()
fl = ctypes.c_int()
hr = g.d3d11.D3D11CreateDevice(None, g.D3D_DRIVER_TYPE_HARDWARE, None,
                               g.D3D11_CREATE_DEVICE_BGRA_SUPPORT, None, 0,
                               g.D3D11_SDK_VERSION, ctypes.byref(device),
                               ctypes.byref(fl), ctypes.byref(context))
print("device hr:", hex(hr & 0xFFFFFFFF), "fl:", hex(fl.value))

factory = ctypes.c_void_p()
hr = g.dxgi.CreateDXGIFactory1(ctypes.byref(g.IID_IDXGIFactory2), ctypes.byref(factory))
print("factory hr:", hex(hr & 0xFFFFFFFF), "ptr:", hex(factory.value or 0))

adapter = ctypes.c_void_p()
hr = g.call(factory, 12, g.HRESULT, [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
            0, ctypes.byref(adapter))
print("EnumAdapters1 hr:", hex(hr & 0xFFFFFFFF), "adapter:", hex(adapter.value or 0))

is_cur = g.call(factory, 13, g.wt.BOOL, [])
print("IsCurrent:", is_cur)


def try_desc(label, **kw):
    d = g.DXGI_SWAP_CHAIN_DESC1()
    d.Width = kw.get("w", 900)
    d.Height = kw.get("h", 420)
    d.Format = kw.get("fmt", g.DXGI_FORMAT_B8G8R8A8_UNORM)
    d.SampleDesc.Count = 1
    d.BufferUsage = g.DXGI_USAGE_RENDER_TARGET_OUTPUT
    d.BufferCount = kw.get("buffers", 2)
    d.Scaling = kw.get("scaling", g.DXGI_SCALING_STRETCH)
    d.SwapEffect = kw.get("effect", g.DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL)
    d.AlphaMode = kw.get("alpha", g.DXGI_ALPHA_MODE_PREMULTIPLIED)
    d.Flags = kw.get("flags", 0)
    sc = ctypes.c_void_p()
    hr = g.call(factory, 24, g.HRESULT,
                [ctypes.c_void_p, ctypes.POINTER(g.DXGI_SWAP_CHAIN_DESC1),
                 ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                device, ctypes.byref(d), None, ctypes.byref(sc))
    print(f"{label:28s} hr=0x{hr & 0xFFFFFFFF:08x}  sc={hex(sc.value or 0)}")
    if hr >= 0:
        g.release(sc)
    return hr


print("desc size:", ctypes.sizeof(g.DXGI_SWAP_CHAIN_DESC1))
try_desc("default")
try_desc("alpha=ignore", alpha=0)
try_desc("buffers=3", buffers=3)
try_desc("effect=flip_discard", effect=4)
try_desc("no usage flag", )
try_desc("format RGBA", fmt=28)
