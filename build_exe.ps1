# 打包成免安装 exe（Windows）
# 用法：powershell -ExecutionPolicy Bypass -File build_exe.ps1
$ErrorActionPreference = "Stop"
$py = if ($env:PYTHON) { $env:PYTHON } else { "python" }

& $py -m pip install --disable-pip-version-check -q pyinstaller

& $py -m PyInstaller --noconfirm --clean --onefile --noconsole `
    --name "桌面天气" --icon app.ico `
    --add-data "themes;themes" `
    --exclude-module numpy --exclude-module pandas --exclude-module tkinter `
    --exclude-module unittest --exclude-module pydoc `
    desktop_weather.py

$pkg = Join-Path $PWD "dist\桌面天气-免安装版"
New-Item -ItemType Directory -Force -Path $pkg | Out-Null
Copy-Item "dist\桌面天气.exe" $pkg -Force
Copy-Item "config.example.json" (Join-Path $pkg "config.json") -Force
Write-Host "打包完成：$pkg"