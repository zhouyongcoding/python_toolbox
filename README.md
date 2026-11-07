# Python 工具箱

从 Python 桌面客户端独立复制出的 PySide6 工具箱项目。原客户端中的工具箱保持不变。

## 安装依赖

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
```

## 启动

```powershell
python run_toolbox.py
```

二维码解析通过 `http://192.168.1.25:8000/api/scan_qr` 请求后端；使用前请确认后端服务可访问。

## 打包为 Windows EXE

安装 PyInstaller 后，在项目目录执行：

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm PythonToolbox.spec
```

单文件 GUI 程序生成在 `dist/PythonToolbox.exe`。将 exe 复制到目标电脑即可运行；二维码解析仍需能访问上述后端。

## 测试

```powershell
python -m unittest discover -s tests -v
```
