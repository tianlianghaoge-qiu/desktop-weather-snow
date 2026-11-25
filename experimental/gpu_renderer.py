# -*- coding: utf-8 -*-
"""
GPU 渲染后端（Direct3D 11 + DirectComposition）
==============================================

用 GPU 画雪花与卡片，替代原来「Pillow 画整屏 → 预乘转换 → GDI 拷贝」的 CPU 路径。
调用方每帧只需给出雪花四边形和卡片矩形，GPU 负责光栅化与合成。

vtable 下标来自 mingw-w64 头文件（d3d11.h 的 *Vtbl 结构含 IUnknown 三项；
dcomp.h 的 DECLARE_INTERFACE_IID_ 不含，故 DComp 接口统一 +3）。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes as wt

# --------------------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------------------
D3D11_SDK_VERSION = 7
D3D_DRIVER_TYPE_HARDWARE = 1
D3D_DRIVER_TYPE_WARP = 5
D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20

DXGI_FORMAT_B8G8R8A8_UNORM = 87
DXGI_FORMAT_R32G32_FLOAT = 6
DXGI_FORMAT_R32_FLOAT = 41

D3D11_USAGE_DYNAMIC = 2
D3D11_USAGE_DEFAULT = 0
D3D11_CPU_ACCESS_WRITE = 0x10000
D3D11_BIND_VERTEX_BUFFER = 0x1
D3D11_BIND_CONSTANT_BUFFER = 0x4
D3D11_BIND_SHADER_RESOURCE = 0x8
D3D11_MAP_WRITE_DISCARD = 4
D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST = 4
D3D11_FILTER_MIN_MAG_MIP_LINEAR = 0x15
D3D11_TEXTURE_ADDRESS_CLAMP = 3
D3D11_COMPARISON_ALWAYS = 8
D3D11_BLEND_ONE = 2
D3D11_BLEND_INV_SRC_ALPHA = 7
D3D11_BLEND_OP_ADD = 1

DXGI_ALPHA_MODE_PREMULTIPLIED = 1

WS_EX_NOREDIRECTIONBITMAP = 0x00200000
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000
SW_SHOWNOACTIVATE = 4

# ---- vtable 下标（d3d11） ----
DEV_QueryInterface = 0
DEV_CreateBuffer = 3
DEV_CreateTexture2D = 5
DEV_CreateShaderResourceView = 7
DEV_CreateRenderTargetView = 9
DEV_CreateInputLayout = 11
DEV_CreateVertexShader = 12
DEV_CreatePixelShader = 15
DEV_CreateBlendState = 20
DEV_CreateSamplerState = 23

CTX_VSSetConstantBuffers = 7
CTX_PSSetShaderResources = 8
CTX_PSSetShader = 9
CTX_PSSetSamplers = 10
CTX_VSSetShader = 11
CTX_Draw = 13
CTX_Map = 14
CTX_Unmap = 15
CTX_IASetInputLayout = 17
CTX_IASetVertexBuffers = 18
CTX_IASetPrimitiveTopology = 24
CTX_OMSetRenderTargets = 33
CTX_OMSetBlendState = 35
CTX_RSSetViewports = 44
CTX_ClearRenderTargetView = 50

# ---- vtable 下标（dcomp，含 IUnknown 3 项） ----
DC_Commit = 3
DC_CreateTargetForHwnd = 6
DC_CreateVisual = 7
DC_CreateSurface = 8
TARGET_SetRoot = 3
VISUAL_SetContent = 15
SURFACE_BeginDraw = 3
SURFACE_EndDraw = 4


# --------------------------------------------------------------------------------------
# 结构体
# --------------------------------------------------------------------------------------
class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_string(cls, text: str) -> "GUID":
        parts = text.strip("{}").split("-")
        g = cls()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        rest = bytes.fromhex(parts[3] + parts[4])
        for i, b in enumerate(rest):
            g.Data4[i] = b
        return g


class DXGI_SAMPLE_DESC(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint), ("Quality", ctypes.c_uint)]


class D3D11_BUFFER_DESC(ctypes.Structure):
    _fields_ = [("ByteWidth", ctypes.c_uint), ("Usage", ctypes.c_int),
                ("BindFlags", ctypes.c_uint), ("CPUAccessFlags", ctypes.c_uint),
                ("MiscFlags", ctypes.c_uint), ("StructureByteStride", ctypes.c_uint)]


class D3D11_SUBRESOURCE_DATA(ctypes.Structure):
    _fields_ = [("pSysMem", ctypes.c_void_p), ("SysMemPitch", ctypes.c_uint),
                ("SysMemSlicePitch", ctypes.c_uint)]


class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
    _fields_ = [("pData", ctypes.c_void_p), ("RowPitch", ctypes.c_uint),
                ("DepthPitch", ctypes.c_uint)]


class D3D11_VIEWPORT(ctypes.Structure):
    _fields_ = [("TopLeftX", ctypes.c_float), ("TopLeftY", ctypes.c_float),
                ("Width", ctypes.c_float), ("Height", ctypes.c_float),
                ("MinDepth", ctypes.c_float), ("MaxDepth", ctypes.c_float)]


class D3D11_INPUT_ELEMENT_DESC(ctypes.Structure):
    _fields_ = [("SemanticName", ctypes.c_char_p), ("SemanticIndex", ctypes.c_uint),
                ("Format", ctypes.c_int), ("InputSlot", ctypes.c_uint),
                ("AlignedByteOffset", ctypes.c_uint), ("InputSlotClass", ctypes.c_int),
                ("InstanceDataStepRate", ctypes.c_uint)]


class D3D11_RENDER_TARGET_BLEND_DESC(ctypes.Structure):
    _fields_ = [("BlendEnable", wt.BOOL), ("SrcBlend", ctypes.c_int),
                ("DestBlend", ctypes.c_int), ("BlendOp", ctypes.c_int),
                ("SrcBlendAlpha", ctypes.c_int), ("DestBlendAlpha", ctypes.c_int),
                ("BlendOpAlpha", ctypes.c_int), ("RenderTargetWriteMask", ctypes.c_ubyte)]


class D3D11_BLEND_DESC(ctypes.Structure):
    _fields_ = [("AlphaToCoverageEnable", wt.BOOL), ("IndependentBlendEnable", wt.BOOL),
                ("RenderTarget", D3D11_RENDER_TARGET_BLEND_DESC * 8)]


class D3D11_TEXTURE2D_DESC(ctypes.Structure):
    _fields_ = [("Width", ctypes.c_uint), ("Height", ctypes.c_uint), ("MipLevels", ctypes.c_uint),
                ("ArraySize", ctypes.c_uint), ("Format", ctypes.c_int),
                ("SampleDesc", DXGI_SAMPLE_DESC), ("Usage", ctypes.c_int),
                ("BindFlags", ctypes.c_uint), ("CPUAccessFlags", ctypes.c_uint),
                ("MiscFlags", ctypes.c_uint)]


class D3D11_SAMPLER_DESC(ctypes.Structure):
    _fields_ = [("Filter", ctypes.c_int), ("AddressU", ctypes.c_int),
                ("AddressV", ctypes.c_int), ("AddressW", ctypes.c_int),
                ("MipLODBias", ctypes.c_float), ("MaxAnisotropy", ctypes.c_uint),
                ("ComparisonFunc", ctypes.c_int), ("BorderColor", ctypes.c_float * 4),
                ("MinLOD", ctypes.c_float), ("MaxLOD", ctypes.c_float)]


# --------------------------------------------------------------------------------------
# COM 辅助
# --------------------------------------------------------------------------------------
HRESULT = ctypes.c_long


def _fn(obj, index, restype, argtypes):
    vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def call(obj, index, restype, argtypes, *args):
    return _fn(obj, index, restype, argtypes)(obj, *args)


def release(obj) -> None:
    if obj:
        call(obj, 2, ctypes.c_ulong, [])


class GpuError(RuntimeError):
    pass


def _check(hr, what: str) -> None:
    if hr < 0:
        raise GpuError(f"{what} 失败: 0x{hr & 0xFFFFFFFF:08x}")


IID_IDXGIDevice = GUID.from_string("54ec77fa-1377-44e6-8c32-88fd5f44c84c")
IID_ID3D11Texture2D = GUID.from_string("6f15aaf2-d208-4e89-9ab4-489535d34f9c")
IID_IDXGISurface = GUID.from_string("cafcb56c-6ac3-4889-bf47-9e23bbd260ec")
IID_IDCompositionDevice = GUID.from_string("c37ea93a-e7aa-450d-b16f-9746cb0407f3")

d3d11 = ctypes.WinDLL("d3d11")
dxgi = ctypes.WinDLL("dxgi")
dcomp = ctypes.WinDLL("dcomp")
d3dcompiler = ctypes.WinDLL("d3dcompiler_47")
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                   wt.HINSTANCE, ctypes.c_void_p]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wt.ATOM
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.ShowWindow.restype = wt.BOOL
user32.DestroyWindow.argtypes = [wt.HWND]
user32.DestroyWindow.restype = wt.BOOL
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetWindowPos.restype = wt.BOOL

d3d11.D3D11CreateDevice.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint,
    ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_int),
    ctypes.POINTER(ctypes.c_void_p)]
d3d11.D3D11CreateDevice.restype = HRESULT

dcomp.DCompositionCreateDevice.argtypes = [ctypes.c_void_p, ctypes.POINTER(GUID),
                                           ctypes.POINTER(ctypes.c_void_p)]
dcomp.DCompositionCreateDevice.restype = HRESULT

d3dcompiler.D3DCompile.argtypes = [
    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
d3dcompiler.D3DCompile.restype = HRESULT

# --------------------------------------------------------------------------------------
# 着色器
# --------------------------------------------------------------------------------------
SHADER_SRC = """
cbuffer Screen : register(b0)
{
    float2 gScreen;
    float2 gPad;
};

struct VSIn
{
    float2 pos : POSITION;
    float2 uv  : TEXCOORD0;
    float  a   : TEXCOORD1;
};

struct VSOut
{
    float4 pos : SV_POSITION;
    float2 uv  : TEXCOORD0;
    float  a   : TEXCOORD1;
};

VSOut VSMain(VSIn v)
{
    VSOut o;
    o.pos = float4(v.pos.x / gScreen.x * 2.0 - 1.0,
                   1.0 - v.pos.y / gScreen.y * 2.0, 0.0, 1.0);
    o.uv = v.uv;
    o.a = v.a;
    return o;
}

float4 PSSnow(VSOut i) : SV_Target
{
    float2 d = i.uv - 0.5;
    float r = saturate(1.0 - length(d) * 2.0);
    float a = pow(r, 1.7) * i.a;
    return float4(a, a, a, a);
}

Texture2D gCard : register(t0);
SamplerState gSamp : register(s0);

float4 PSCard(VSOut i) : SV_Target
{
    return gCard.Sample(gSamp, i.uv) * i.a;
}
""".encode("utf-8")

VERTEX_FLOATS = 5
VERTEX_STRIDE = VERTEX_FLOATS * 4      # pos.xy, uv.xy, alpha


class GpuOverlay:
    """D3D11 渲染 + DirectComposition 合成的透明窗口（无分层窗口、无 GDI 拷贝）。"""

    def __init__(self, width: int, height: int, x: int = 0, y: int = 0):
        self.width = int(width)
        self.height = int(height)
        self.x = int(x)
        self.y = int(y)
        self.hwnd = None
        self.device = None
        self.context = None
        self.dcomp_device = None
        self.dcomp_target = None
        self.dcomp_visual = None
        self.surface = None
        self._rtv = None
        self._rtv_tex = None
        self.vs = None
        self.ps_snow = None
        self.ps_card = None
        self.input_layout = None
        self.vbuffer = None
        self.vbuffer_capacity = 0
        self.sampler = None
        self.card_texture = None
        self.card_srv = None
        self.cbuffer = None
        self.card_key = None
        self._blend = None
        self.feature_level = 0
        self._create_window()
        self._create_device()
        self._create_pipeline()
        self._create_dcomp()

    # -- 窗口 -------------------------------------------------------------------
    def _create_window(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
        self._proc = proc_type(lambda h, m, w, l: user32.DefWindowProcW(h, m, w, l))

        class WNDCLASSEXW(ctypes.Structure):
            _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", proc_type),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
                        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON)]

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "GpuWeatherOverlay"
        user32.RegisterClassExW(ctypes.byref(wc))

        ex = WS_EX_NOREDIRECTIONBITMAP | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(ex, "GpuWeatherOverlay", "DesktopWeather",
                                           WS_POPUP, self.x, self.y, self.width, self.height,
                                           None, None, hinst, None)
        if not self.hwnd:
            raise GpuError(f"CreateWindowExW 失败: {ctypes.get_last_error()}")
        user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)

    # -- 设备 -------------------------------------------------------------------
    def _create_device(self) -> None:
        device = ctypes.c_void_p()
        context = ctypes.c_void_p()
        feature = ctypes.c_int()
        hr = -1
        for driver in (D3D_DRIVER_TYPE_HARDWARE, D3D_DRIVER_TYPE_WARP):
            hr = d3d11.D3D11CreateDevice(
                None, driver, None, D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None, 0, D3D11_SDK_VERSION,
                ctypes.byref(device), ctypes.byref(feature), ctypes.byref(context))
            if hr >= 0:
                break
        _check(hr, "D3D11CreateDevice")
        self.device = device
        self.context = context
        self.feature_level = feature.value

    # -- 管线 -------------------------------------------------------------------
    def _compile(self, entry: str, target: str) -> ctypes.c_void_p:
        code = ctypes.c_void_p()
        errors = ctypes.c_void_p()
        src = ctypes.create_string_buffer(SHADER_SRC)
        hr = d3dcompiler.D3DCompile(ctypes.cast(src, ctypes.c_void_p), len(SHADER_SRC),
                                    b"inline", None, None, entry.encode(), target.encode(),
                                    0, 0, ctypes.byref(code), ctypes.byref(errors))
        if hr < 0:
            message = ""
            if errors:
                message = ctypes.string_at(errors).decode("utf-8", "ignore")[:400]
            release(errors)
            raise GpuError(f"着色器编译失败 {entry}: {message}")
        release(errors)
        # ID3D10Blob: 3=GetBufferPointer, 4=GetBufferSize
        ptr = call(code, 3, ctypes.c_void_p, [])
        size = call(code, 4, ctypes.c_size_t, [])
        if not ptr or not size:
            raise GpuError(f"着色器字节码为空 {entry}")
        return ptr, size

    def _create_pipeline(self) -> None:
        vs_code, vs_size = self._compile("VSMain", "vs_4_0")
        ps_snow_code, ps_snow_size = self._compile("PSSnow", "ps_4_0")
        ps_card_code, ps_card_size = self._compile("PSCard", "ps_4_0")

        vs = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateVertexShader, HRESULT,
                    [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                     ctypes.POINTER(ctypes.c_void_p)],
                    vs_code, vs_size, None, ctypes.byref(vs)), "CreateVertexShader")
        self.vs = vs

        elements = (D3D11_INPUT_ELEMENT_DESC * 3)()
        elements[0] = D3D11_INPUT_ELEMENT_DESC(b"POSITION", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, 0, 0)
        elements[1] = D3D11_INPUT_ELEMENT_DESC(b"TEXCOORD", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 8, 0, 0)
        elements[2] = D3D11_INPUT_ELEMENT_DESC(b"TEXCOORD", 1, DXGI_FORMAT_R32_FLOAT, 0, 16, 0, 0)
        layout = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateInputLayout, HRESULT,
                    [ctypes.POINTER(D3D11_INPUT_ELEMENT_DESC), ctypes.c_uint,
                     ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)],
                    elements, 3, vs_code, vs_size, ctypes.byref(layout)), "CreateInputLayout")
        self.input_layout = layout

        for attr, code, size in (("ps_snow", ps_snow_code, ps_snow_size),
                                 ("ps_card", ps_card_code, ps_card_size)):
            obj = ctypes.c_void_p()
            _check(call(self.device, DEV_CreatePixelShader, HRESULT,
                        [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p)],
                        code, size, None, ctypes.byref(obj)), f"CreatePixelShader {attr}")
            setattr(self, attr, obj)



        self._ensure_vbuffer(4096)

        cbd = D3D11_BUFFER_DESC()
        cbd.ByteWidth = 16
        cbd.Usage = D3D11_USAGE_DYNAMIC
        cbd.BindFlags = D3D11_BIND_CONSTANT_BUFFER
        cbd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE
        cb = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateBuffer, HRESULT,
                    [ctypes.POINTER(D3D11_BUFFER_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA),
                     ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(cbd), None, ctypes.byref(cb)), "CreateBuffer(constant)")
        self.cbuffer = cb

        sd = D3D11_SAMPLER_DESC()
        sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR
        sd.AddressU = sd.AddressV = sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP
        sd.ComparisonFunc = D3D11_COMPARISON_ALWAYS
        sd.MinLOD = 0.0
        sd.MaxLOD = 3.402823466e38
        sampler = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateSamplerState, HRESULT,
                    [ctypes.POINTER(D3D11_SAMPLER_DESC), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(sd), ctypes.byref(sampler)), "CreateSamplerState")
        self.sampler = sampler

        bd = D3D11_BLEND_DESC()
        bd.AlphaToCoverageEnable = False
        bd.IndependentBlendEnable = False
        bd.RenderTarget[0].BlendEnable = True
        bd.RenderTarget[0].SrcBlend = D3D11_BLEND_ONE
        bd.RenderTarget[0].DestBlend = D3D11_BLEND_INV_SRC_ALPHA
        bd.RenderTarget[0].BlendOp = D3D11_BLEND_OP_ADD
        bd.RenderTarget[0].SrcBlendAlpha = D3D11_BLEND_ONE
        bd.RenderTarget[0].DestBlendAlpha = D3D11_BLEND_INV_SRC_ALPHA
        bd.RenderTarget[0].BlendOpAlpha = D3D11_BLEND_OP_ADD
        bd.RenderTarget[0].RenderTargetWriteMask = 0x0F
        blend = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateBlendState, HRESULT,
                    [ctypes.POINTER(D3D11_BLEND_DESC), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(bd), ctypes.byref(blend)), "CreateBlendState")
        self._blend = blend

    def _create_dcomp(self) -> None:
        dxgi_device = ctypes.c_void_p()
        _check(call(self.device, DEV_QueryInterface, HRESULT,
                    [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(IID_IDXGIDevice), ctypes.byref(dxgi_device)),
               "QueryInterface IDXGIDevice")

        dc = ctypes.c_void_p()
        _check(dcomp.DCompositionCreateDevice(dxgi_device,
                                              ctypes.byref(IID_IDCompositionDevice),
                                              ctypes.byref(dc)), "DCompositionCreateDevice")
        target = ctypes.c_void_p()
        _check(call(dc, DC_CreateTargetForHwnd, HRESULT,
                    [wt.HWND, wt.BOOL, ctypes.POINTER(ctypes.c_void_p)],
                    self.hwnd, True, ctypes.byref(target)), "CreateTargetForHwnd")
        visual = ctypes.c_void_p()
        _check(call(dc, DC_CreateVisual, HRESULT, [ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(visual)), "CreateVisual")
        surface = ctypes.c_void_p()
        _check(call(dc, DC_CreateSurface, HRESULT,
                    [ctypes.c_uint, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                     ctypes.POINTER(ctypes.c_void_p)],
                    self.width, self.height, DXGI_FORMAT_B8G8R8A8_UNORM,
                    DXGI_ALPHA_MODE_PREMULTIPLIED, ctypes.byref(surface)), "CreateSurface")
        _check(call(visual, VISUAL_SetContent, HRESULT, [ctypes.c_void_p], surface),
               "Visual::SetContent")
        _check(call(target, TARGET_SetRoot, HRESULT, [ctypes.c_void_p], visual), "Target::SetRoot")
        _check(call(dc, DC_Commit, HRESULT, []), "Commit")

        self.dcomp_device = dc
        self.dcomp_target = target
        self.dcomp_visual = visual
        self.surface = surface
        release(dxgi_device)

    # -- 资源 -------------------------------------------------------------------
    def _ensure_vbuffer(self, vertex_count: int) -> None:
        if self.vbuffer and vertex_count <= self.vbuffer_capacity:
            return
        if self.vbuffer:
            release(self.vbuffer)
        capacity = max(4096, int(vertex_count * 1.5))
        bd = D3D11_BUFFER_DESC()
        bd.ByteWidth = capacity * VERTEX_STRIDE
        bd.Usage = D3D11_USAGE_DYNAMIC
        bd.BindFlags = D3D11_BIND_VERTEX_BUFFER
        bd.CPUAccessFlags = D3D11_CPU_ACCESS_WRITE
        buf = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateBuffer, HRESULT,
                    [ctypes.POINTER(D3D11_BUFFER_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA),
                     ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(bd), None, ctypes.byref(buf)), "CreateBuffer(vertex)")
        self.vbuffer = buf
        self.vbuffer_capacity = capacity

    def set_card(self, image, key=None) -> None:
        """上传卡片位图（RGBA）。key 未变则跳过。"""
        if key is not None and key == self.card_key:
            return
        data = image.tobytes("raw", "BGRa")
        w, h = image.size
        desc = D3D11_TEXTURE2D_DESC()
        desc.Width = w
        desc.Height = h
        desc.MipLevels = 1
        desc.ArraySize = 1
        desc.Format = DXGI_FORMAT_B8G8R8A8_UNORM
        desc.SampleDesc.Count = 1
        desc.Usage = D3D11_USAGE_DEFAULT
        desc.BindFlags = D3D11_BIND_SHADER_RESOURCE
        buffer = ctypes.create_string_buffer(data, len(data))
        sub = D3D11_SUBRESOURCE_DATA()
        sub.pSysMem = ctypes.cast(buffer, ctypes.c_void_p)
        sub.SysMemPitch = w * 4
        texture = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateTexture2D, HRESULT,
                    [ctypes.POINTER(D3D11_TEXTURE2D_DESC), ctypes.POINTER(D3D11_SUBRESOURCE_DATA),
                     ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(desc), ctypes.byref(sub), ctypes.byref(texture)),
               "CreateTexture2D(card)")
        srv = ctypes.c_void_p()
        _check(call(self.device, DEV_CreateShaderResourceView, HRESULT,
                    [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                    texture, None, ctypes.byref(srv)), "CreateShaderResourceView")
        if self.card_srv:
            release(self.card_srv)
        if self.card_texture:
            release(self.card_texture)
        self.card_texture = texture
        self.card_srv = srv
        self.card_key = key

    # -- 渲染 -------------------------------------------------------------------
    def _bind_target(self) -> None:
        isurf = ctypes.c_void_p()
        _check(call(self.surface, SURFACE_BeginDraw, HRESULT,
                    [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                    None, ctypes.byref(IID_IDXGISurface), ctypes.byref(isurf)), "BeginDraw")
        texture = ctypes.c_void_p()
        _check(call(isurf, 0, HRESULT,
                    [ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)],
                    ctypes.byref(IID_ID3D11Texture2D), ctypes.byref(texture)), "QI ID3D11Texture2D")
        release(isurf)
        if not self._rtv or self._rtv_tex != texture.value:
            if self._rtv:
                release(self._rtv)
            rtv = ctypes.c_void_p()
            _check(call(self.device, DEV_CreateRenderTargetView, HRESULT,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                        texture, None, ctypes.byref(rtv)), "CreateRenderTargetView")
            self._rtv = rtv
            self._rtv_tex = texture.value
        release(texture)

    def render(self, snow_quads, card_rect=None, card_alpha=1.0) -> None:
        self._bind_target()
        ctx = self.context

        call(ctx, CTX_ClearRenderTargetView, None,
             [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float)],
             self._rtv, (ctypes.c_float * 4)(0.0, 0.0, 0.0, 0.0))

        vp = D3D11_VIEWPORT(0, 0, self.width, self.height, 0, 1)
        call(ctx, CTX_RSSetViewports, None, [ctypes.c_uint, ctypes.POINTER(D3D11_VIEWPORT)],
             1, ctypes.byref(vp))
        call(ctx, CTX_OMSetRenderTargets, None,
             [ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p],
             1, ctypes.byref(self._rtv), None)
        call(ctx, CTX_OMSetBlendState, None,
             [ctypes.c_void_p, ctypes.POINTER(ctypes.c_float), ctypes.c_uint],
             self._blend, (ctypes.c_float * 4)(0, 0, 0, 0), 0xFFFFFFFF)

        mapped = D3D11_MAPPED_SUBRESOURCE()
        _check(call(ctx, CTX_Map, HRESULT,
                    [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                     ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
                    self.cbuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, ctypes.byref(mapped)),
               "Map(constant)")
        const = (ctypes.c_float * 4)(float(self.width), float(self.height), 0.0, 0.0)
        ctypes.memmove(mapped.pData, const, ctypes.sizeof(const))
        call(ctx, CTX_Unmap, None, [ctypes.c_void_p, ctypes.c_uint], self.cbuffer, 0)

        verts = []
        for cx, cy, r, a in snow_quads:
            if a <= 0.004 or r <= 0.2:
                continue
            x0, y0, x1, y1 = cx - r, cy - r, cx + r, cy + r
            verts += [x0, y0, 0.0, 0.0, a, x1, y0, 1.0, 0.0, a, x0, y1, 0.0, 1.0, a,
                      x1, y0, 1.0, 0.0, a, x1, y1, 1.0, 1.0, a, x0, y1, 0.0, 1.0, a]
        snow_count = len(verts) // VERTEX_FLOATS

        card_count = 0
        if card_rect and self.card_srv:
            x, y, w, h = card_rect
            a = max(0.0, min(1.0, card_alpha))
            verts += [x, y, 0.0, 0.0, a, x + w, y, 1.0, 0.0, a, x, y + h, 0.0, 1.0, a,
                      x + w, y, 1.0, 0.0, a, x + w, y + h, 1.0, 1.0, a,
                      x, y + h, 0.0, 1.0, a]
            card_count = 6

        total = snow_count + card_count
        if total:
            self._ensure_vbuffer(total)
            mapped = D3D11_MAPPED_SUBRESOURCE()
            _check(call(ctx, CTX_Map, HRESULT,
                        [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_uint,
                         ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE)],
                        self.vbuffer, 0, D3D11_MAP_WRITE_DISCARD, 0, ctypes.byref(mapped)),
                   "Map(vertex)")
            packed = (ctypes.c_float * len(verts))(*verts)
            ctypes.memmove(mapped.pData, packed, ctypes.sizeof(packed))
            call(ctx, CTX_Unmap, None, [ctypes.c_void_p, ctypes.c_uint], self.vbuffer, 0)

            call(ctx, CTX_IASetPrimitiveTopology, None, [ctypes.c_int],
                 D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST)
            call(ctx, CTX_IASetInputLayout, None, [ctypes.c_void_p], self.input_layout)
            stride = ctypes.c_uint(VERTEX_STRIDE)
            offset = ctypes.c_uint(0)
            buffers = (ctypes.c_void_p * 1)(self.vbuffer.value)
            call(ctx, CTX_IASetVertexBuffers, None,
                 [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p),
                  ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)],
                 0, 1, buffers, ctypes.byref(stride), ctypes.byref(offset))
            call(ctx, CTX_VSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint],
                 self.vs, None, 0)
            call(ctx, CTX_VSSetConstantBuffers, None,
                 [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                 0, 1, (ctypes.c_void_p * 1)(self.cbuffer.value))

            if snow_count:
                call(ctx, CTX_PSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint],
                     self.ps_snow, None, 0)
                call(ctx, CTX_Draw, None, [ctypes.c_uint, ctypes.c_uint], snow_count, 0)
            if card_count:
                call(ctx, CTX_PSSetShader, None, [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint],
                     self.ps_card, None, 0)
                call(ctx, CTX_PSSetShaderResources, None,
                     [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                     0, 1, (ctypes.c_void_p * 1)(self.card_srv.value))
                call(ctx, CTX_PSSetSamplers, None,
                     [ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p)],
                     0, 1, (ctypes.c_void_p * 1)(self.sampler.value))
                call(ctx, CTX_Draw, None, [ctypes.c_uint, ctypes.c_uint], card_count, snow_count)

        _check(call(self.surface, SURFACE_EndDraw, HRESULT, []), "EndDraw")
        _check(call(self.dcomp_device, DC_Commit, HRESULT, []), "Commit")

    # -- 收尾 -------------------------------------------------------------------
    def close(self) -> None:
        for name in ("_rtv", "card_srv", "card_texture", "sampler", "_blend", "cbuffer",
                     "vbuffer", "input_layout", "ps_card", "ps_snow", "vs", "surface",
                     "dcomp_visual", "dcomp_target", "dcomp_device", "context", "device"):
            obj = getattr(self, name, None)
            if obj is not None and isinstance(obj, ctypes.c_void_p) and obj.value:
                release(obj)
            setattr(self, name, None)
        if self.hwnd:
            user32.DestroyWindow(self.hwnd)
            self.hwnd = None
