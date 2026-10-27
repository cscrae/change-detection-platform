# 环境安装与原生启动

Windows 原生部署使用独立的服务 Python 环境和 GPU 推理环境。Node、Go 放在项目 `.tools` 下，不修改系统或用户 PATH；模型、影像、数据库和本机配置均不进入 Git。

## 一次配置

在项目根目录运行：

```powershell
.\setup-local.cmd
.\start-all.cmd -CheckOnly
.\start-all.cmd
```

`setup-local.cmd` 需要已有 `uv.exe`、64 位 Windows 和可用的 NVIDIA 驱动，并访问 Python、PyPI、Node.js、Go 和 npm 官方源。没有 uv 时先按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/) 安装。安装器校验 Node/Go 官方 ZIP 的 SHA-256；下载缓存和工具均位于 `.tools`。

当前安装脚本固定 Python 3.13.14、Node 24.22.1、Go 1.27.3，并依次安装、检查和构建：

| 位置 | 用途 |
|---|---|
| `backend-py/.venv-local` | FastAPI、Rasterio、后处理、CPU ONNX 结构检查 |
| `model-runtime/.venv-gpu` | ONNX Runtime GPU 1.23.2；按需安装 CUDA 12/cuDNN 9 运行库 |
| `.tools/node` | 项目 Node 与 npm |
| `.tools/go` | 项目 Go 工具链 |
| `.tools/bin/cdp-server.exe` | 预编译 Go API |
| `frontend/node_modules`、`frontend/dist` | 前端依赖及构建产物 |
| `.env.local.json` | 当前主机的影像、模型和解释器路径 |

服务依赖来自 `backend-py/requirements.lock.txt`。GPU 核心依赖来自 `model-runtime/requirements.windows-gpu.txt`。若 `.env.local.json` 或进程变量 `CHANGE_CUDA_DLL_DIRS` 指向完整的 CUDA 12/cuDNN 9 DLL 目录，安装器会复用它；否则通过 ONNX Runtime 的 `cuda,cudnn` extras 安装 NVIDIA 运行库，无需再安装完整 CUDA Toolkit。不要在同一 Python 环境中同时安装 `onnxruntime` 与 `onnxruntime-gpu`。DLL 预加载方式见 [ONNX Runtime 官方说明](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html#preload-dlls)。

重复安装保留已有本机配置及业务数据；如服务正在运行，先用 `stop-all.cmd` 停止，再重新构建。启动器优先运行预编译 API；修改 Go 源码后须重新运行安装脚本或手动 `go build`。

## 本机配置与数据接入

首次安装创建 `.env.local.json`。模板见 `local-settings.example.json`：

```json
{
  "LOCAL_IMAGERY_DIR": "data/imagery",
  "TEST_UP_SOURCE_ROOT": "\\\\nas.example.invalid\\imagery\\hebei\\test_up",
  "CHANGE_ONNX_PYTHON": "model-runtime/.venv-gpu/Scripts/python.exe",
  "CHANGE_MODEL_PATH": "model-runtime/model/enforcement_2.onnx"
}
```

相对路径均按项目根目录解析，与启动时所在目录无关；绝对路径也可使用。已有进程环境变量优先于该文件。其他位置的 CUDA/cuDNN DLL 可通过 `CHANGE_CUDA_DLL_DIRS` 指定，多个目录用分号分隔。启动器还支持项目 GPU 环境、Conda `Library/bin`、PyTorch 和 NVIDIA pip 包的 DLL 发现。

`TEST_UP_SOURCE_ROOT` 是两个 test_up 工具使用的可选字段：`scripts/prepare_test_up.py` 用它确定原始数据根目录，`scripts/server_relocate_test_up.py` 用它匹配 VRT 中的原始 Windows 共享根。两者分别按 `--source-root` / `--windows-root` 参数 → 进程环境变量 `TEST_UP_SOURCE_ROOT` → 项目 `.env.local.json` 同名字段读取；不使用这两个工具时可省略该字段。示例主机不是业务服务，使用前需换成有读取权限的真实绝对路径或 UNC 地址。

真实地址只放在已被 Git 忽略的 `.env.local.json` 或当前进程环境中，不写入公共模板和文档。已有本机配置时只补充该字段，保留原有模型、解释器及 CUDA 配置，不要整份覆盖；修改这个字段也不会自动切换平台已经生效的影像来源。完整准备与接入步骤见 [test_up 接入说明](TEST_UP_IMPORT.md)。

缺少模型或影像时仍可进入管理界面。模型文件和实际遥感影像最后单独接入，不会由安装脚本下载或生成。默认影像目录是项目 `data/imagery`，不再依赖原机器的映射盘。

首次启动后在「管理」验证或导入兼容模型、登记服务端可读的影像目录并应用。真实变化检测至少需要同一区域两个不同月份的时相。持久化后，影像来源及模型设置以 SQLite 生效版本为准；修改启动环境变量或 `.env.local.json` 不会覆盖已有业务配置，须在管理页修改。详情见 [管理说明](docs/MANAGEMENT.md)。

## 启动、停止与验证

入口为 [http://localhost:5173](http://localhost:5173)，Go API 8080、Python API 8090，仅供本机使用。日志位于 `data/logs`，业务数据位于 `backend-go/data`。

后台服务由 Windows Management 独立启动，使用隐藏窗口并脱离启动工具的 Job；启动命令结束或关闭该终端不会自动停止平台。需要停止时运行 `stop-all.cmd`。电脑重启后须重新运行 `start-all.cmd`，未配置开机自启。若浏览器提示连接被拒绝，先确认三个服务已启动，并使用上述本机地址。

```powershell
.\start-all.cmd -CheckOnly
.\start-all.cmd
.\stop-all.cmd -CheckOnly
.\stop-all.cmd
```

检查命令返回 0 表示基础启动依赖具备；模型缺失或 CUDA 模型会话检查失败会明确警告，不能当作真实检测通过。启动器对模型会话检查限时 45 秒、服务启动限时 120 秒、影像索引请求限时 15 秒。已有服务会保留并提示可能使用旧配置；明显无关的端口占用会报错，脚本不会结束其他应用。

不依赖业务模型或影像的真实 GPU 运行库检查：

```powershell
backend-py/.venv-local/Scripts/python.exe scripts/check_gpu_runtime.py
```

该命令创建临时 ONNX Conv 图，在独立 GPU 环境执行并校验数值，禁止 CPU 回退。通过仅代表 CUDA/cuDNN 基础运行库可执行，实际业务模型仍须在管理页完成验证。

开发检查：

```powershell
backend-py/.venv-local/Scripts/python.exe -m pip check
model-runtime/.venv-gpu/Scripts/python.exe -m pip check
Push-Location backend-py
.venv-local/Scripts/python.exe -m unittest discover -s tests -v
Pop-Location
Push-Location backend-go
$env:GOCACHE = Join-Path (Get-Location) '.cache/go-build'
..\.tools\go\bin\go.exe test ./...
Pop-Location
Push-Location frontend
$env:PATH = (Resolve-Path ../.tools/node).Path + ';' + $env:PATH
npm.cmd run build
Pop-Location
```

更多验收说明见 [开发与测试](docs/DEVELOPMENT.md)。容器部署另见 [容器说明](deploy/README.md)，不由本机原生环境检查覆盖。
