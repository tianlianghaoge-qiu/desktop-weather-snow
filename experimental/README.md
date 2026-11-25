# GPU 渲染后端（实验，未启用）

这里是从 v1.2.0 的 GDI 渲染路径向 **Direct3D 11 + DirectComposition** 迁移的探索性代码。
**主程序 `desktop_weather.py` 完全没有引用本目录**，删除本目录不影响程序运行。

## 目标

现状（GDI 分层窗口）每帧要做三件 CPU 上的重活：

| 环节 | 耗时（2240×1400） |
| --- | --- |
| Pillow 光栅化 300 片雪花 | ~6 ms |
| 整幅位图预乘 alpha + BGRA 转换 | ~11 ms |
| 拷进 DIB 并 `UpdateLayeredWindow` | ~5 ms |

合计约 22 ms/帧，因此 60 帧无法达成（实测上限约 45）。目标是让 GPU 直接光栅化粒子、
由 DWM 合成，把每帧 CPU 开销压到 1 ms 级。

## 已完成（可运行、已验证）

`experimental/gpu_renderer.py` 里的 `GpuOverlay` 已经跑通以下全部环节：

1. `WS_EX_NOREDIRECTIONBITMAP` 透明窗口创建
2. `D3D11CreateDevice`（硬件设备，Feature Level 11_0）
3. 运行时 HLSL 编译（`d3dcompiler_47!D3DCompile`）：顶点着色器 + 雪花像素着色器 + 卡片纹理像素着色器
4. 顶点/常量缓冲、输入布局、采样器、预乘 alpha 混合状态
5. `DCompositionCreateDevice` → `CreateTargetForHwnd` → `CreateVisual` → `SetContent` → `SetRoot` → `Commit`
6. `CreateSurface(900×420, B8G8R8A8, PREMULTIPLIED)` 成功返回表面对象

运行 `python gpu_probe.py` 可以看到上述每一步都返回 `S_OK`。

## 阻塞点

**`IDCompositionSurface::BeginDraw` 在 dcomp.dll 内部 fail-fast**（`0xC0000409`），
拿不到可绘制的 DXGI 表面，因此无法把 D3D11 的绘制结果写进合成表面。

已排除的因素：

| 试验 | 结果 |
| --- | --- |
| `updateRect` 传 `NULL` / 传合法 `RECT` | 均崩溃 |
| 表面格式 `B8G8R8A8_UNORM` + `PREMULTIPLIED` | 崩溃 |
| 表面格式 `R8G8B8A8_UNORM` + `PREMULTIPLIED` | 崩溃 |
| 表面格式 `B8G8R8A8_UNORM` + `IGNORE` | 崩溃 |
| 表面是否已挂到 visual（SetContent + Commit 后再 BeginDraw） | 均崩溃 |
| 同接口的 `EndDraw` / `SuspendDraw` / `ResumeDraw` | 正常返回 `0x88980802`（`DCOMPOSITION_ERROR_SURFACE_NOT_BEING_RENDERED`），证明表面对象本身有效 |

另一条路（DXGI 交换链 + `IDCompositionVisual::SetContent(swapchain)`）同样受阻：
`IDXGIFactory2::CreateSwapChainForComposition` 与 `CreateSwapChainForHwnd` 都返回
`DXGI_ERROR_INVALID_CALL (0x887A0001)`，而同一设备/工厂下的旧版
`IDXGIFactory::CreateSwapChain` 与 `D3D11CreateDeviceAndSwapChain` 却能成功。

## 下一步可尝试

1. 改用 `DCompositionCreateDevice2` / `DCompositionCreateDevice3`（Win10 1803+ 的现代路径，
   WinUI 与 Terminal 走的是这条），表面实现可能与 v1 设备不同；
2. 或者用 `DCompositionCreateDevice3` + `IDCompositionDesktopDevice::CreateTargetForHwnd`；
3. 若仍不行，退回 CPU 侧优化：用 GDI `AlphaBlend` 把雪花直接画进 DIB，跳过 Pillow 的整屏
   预乘转换，预计可到 60 帧 / 单核 40% 左右（仍达不到 10%）。

## vtable 下标备忘

下标来自 mingw-w64 头文件（`d3d11.h` 的 `*Vtbl` 结构体显式包含 `IUnknown` 三项；
`dcomp.h` 的 `DECLARE_INTERFACE_IID_` **不包含**，需要自行 +3）。踩过的坑：

- `IDCompositionDevice`：`Commit=3`、`CreateTargetForHwnd=6`、`CreateVisual=7`、`CreateSurface=8`
- `IDCompositionVisual`：`SetContent=15`（头文件里 `#ifdef __cplusplus` 有重复重载，
  解析时不能把两份都算进去，否则会算成 23 而越界调用）
- `IDCompositionSurface`：`BeginDraw=3`、`EndDraw=4`
- `ID3D11DeviceContext`：`Map=14`、`Draw=13`、`IASetVertexBuffers=18`、`OMSetRenderTargets=33`
