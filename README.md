# 遥感变化检测平台

面向河北区域的本地遥感影像变化检测平台，支持影像管理、同月影像索引与拼接、相邻时相检测、地图对比以及 TIFF、GeoJSON、CSV、Shapefile ZIP 导出。

## 功能与架构

- Vue 3、TypeScript、Leaflet 前端：概览、对比、报告、管理四个页面。
- Go/Gin API：配置版本、模型登记、任务队列、SQLite 元数据和结果持久化。
- FastAPI、Rasterio、Shapely：影像索引、瓦片、裁切、后处理和导出。
- ONNX Runtime：平台检测使用经过验证的 CUDA 推理模型。
- 参考底图与地图上下文：可配置标准 XYZ 底图、自适应经纬网、鼠标/中心经纬度，以及本地 GeoJSON POI 查询与弹窗。配置和坐标要求见 [地图说明](docs/MAP_CONTEXT.md)。

平台面向单机或内网使用，不包含多用户权限、公共互联网服务和自动业务备份。边界 GeoJSON 已纳入源码；原始影像、模型和运行数据库属于部署制品，需要单独准备。

## Windows GPU 部署

### 1. 安装前置软件

确认以下工具可执行：Windows 10/11、Python 3.13、Node.js 24、Go 1.22+、NVIDIA 驱动，以及兼容 ONNX Runtime 1.23 的 CUDA 12/cuDNN 9 运行库。

### 2. 获取源码和模型

    git clone <你的 GitHub 仓库地址>
    Set-Location change-detection-platform

本机权重文件保留在 model-runtime/model/enforcement_2.onnx。当前仓库已将该文件从 Git 索引移除，但不会删除本地文件。它被 .gitignore 忽略，源码克隆不会包含权重；发布时请通过独立制品、对象存储或内部文件分发系统提供，并记录 SHA-256 和许可证。

### 3. 安装依赖

本机推荐使用安装脚本，自动准备隔离的 Python 环境、项目 Node/Go、GPU 运行库，并构建前后端：

    .\setup-local.cmd

需要已有 uv 和 NVIDIA 驱动。脚本创建被 Git 忽略的 `.env.local.json`，路径示例见 `local-settings.example.json`；模型和影像可以稍后在管理页接入。完整说明见 [环境安装](ENVIRONMENT_SETUP.md)。以下命令保留为手动安装参考：

    python -m venv backend-py/.venv-local
    backend-py/.venv-local/Scripts/python.exe -m pip install -r backend-py/requirements.lock.txt --index-url https://pypi.org/simple
    Set-Location frontend
    npm ci --registry=https://registry.npmjs.org
    Set-Location ..
    Set-Location backend-go
    go mod download
    Set-Location ..
    python -m venv model-runtime/.venv-gpu
    model-runtime/.venv-gpu/Scripts/python.exe -m pip install -r model-runtime/requirements.windows-gpu.txt --index-url https://pypi.org/simple

不要在同一个 Python 环境中同时安装 onnxruntime 和 onnxruntime-gpu。安装脚本会复用已配置的完整 CUDA/cuDNN DLL 目录，或自动安装运行库；手动安装核心依赖时需自行提供 DLL。解释器选择和 CUDA DLL 查找规则见 ENVIRONMENT_SETUP.md。

### 4. 配置影像并启动

    $env:LOCAL_IMAGERY_DIR = 'D:\遥感影像'
    $env:CHANGE_ONNX_PYTHON = (Resolve-Path model-runtime/.venv-gpu/Scripts/python.exe).Path
    $env:CHANGE_CUDA_DLL_DIRS = 'D:\CUDA12\bin;D:\cuDNN9\bin'
    .\start-all.cmd -CheckOnly
    .\start-all.cmd

打开 http://localhost:5173。默认端口为前端 5173、Go API 8080、Python API 8090。首次进入管理页，验证模型、登记服务端可读的影像目录并应用配置。真实检测至少需要同一区域两个不同月份的时相；单个日期只能验证浏览、瓦片和裁切。

服务日志运行时写入 data/logs。启动器会创建 backend-go/data、data/cache 等运行目录，它们不进入 Git。

## Docker GPU 部署

计划迁移到 Linux GPU 服务器并测试较大区域时，使用新增的[服务器部署与分块测试说明](deploy/SERVER_SCALE.md)：提供独立 Compose、只读模型/影像/POI 挂载、Windows VRT 引用迁移、服务器预检和分阶段基准工具。尚未确定服务器时可以离线准备索引及分块计划；这不代表 Linux CUDA 或大范围压测已经通过。

目标主机需要 Docker、NVIDIA Container Toolkit 和可用 NVIDIA 驱动。从项目根目录执行：

    docker compose -f deploy/docker-compose.yml config
    docker compose -f deploy/docker-compose.yml up --build -d
    docker compose -f deploy/docker-compose.yml ps

默认入口是 http://localhost。原始影像挂载到 Python 容器可读目录（默认 /opt/assets/imagery），边界挂载到 /opt/assets/boundaries；容器内配置必须使用容器路径，不能填写 Windows 盘符。Go 与 Python 共用 Docker 卷 /data。普通升级不要执行 docker compose down -v，否则会删除业务卷。完整约束见 deploy/README.md。

## 目录与数据生命周期

| 路径 | 作用 | 是否提交 Git |
|---|---|---|
| data/boundaries/ | 行政边界和来源说明 | 是 |
| data/imagery/ | 可选样例或原始影像 | 否 |
| data/logs/ | 服务日志 | 否 |
| data/cache/ | 影像元数据和瓦片缓存 | 否 |
| data/validation/ | 测试和验收产物 | 否 |
| backend-go/data/ | SQLite、模型导入、裁切和结果 | 否 |
| model-runtime/model/ | 本机 ONNX 权重 | 否 |

停止服务后，可以删除 data/cache、data/logs、data/validation 和 backend-go/data 来清除本机运行状态；不会删除源码、边界数据或本机 ONNX 权重。

## 开发验证

    $env:PATH = (Resolve-Path .tools/node).Path + ';' + $env:PATH
    Set-Location frontend
    npm run build
    npm run format:check
    Set-Location ..
    Set-Location backend-py
    .venv-local\Scripts\python.exe -m unittest discover -s tests -v
    Set-Location ..
    Set-Location backend-go
    $env:GOCACHE = (Join-Path (Get-Location) '.cache/go-build')
    ..\.tools\go\bin\go.exe test ./...
    Set-Location ..

浏览器验收脚本位于 scripts/。真实 CUDA 检测、Docker/Linux、UNC 断网恢复和大范围长时负载属于不同验证范围，不能由前端构建或健康检查替代。详见 docs/DEVELOPMENT.md 和 docs/CURRENT_STATUS.md。

## GitHub 发布前检查

1. git status --short 只包含预期修改。
2. git check-ignore -v model-runtime/model/enforcement_2.onnx 命中忽略规则，且模型文件仍在本机。
3. git ls-files 不包含日志、缓存、数据库、检测结果、虚拟环境、node_modules 或原始 GeoTIFF。
4. 不要提交真实影像路径、访问令牌、私钥、内部域名或个人机器配置。
5. 为发布包提供模型下载地址、SHA-256、许可证和至少两期测试影像说明。

文档索引见 docs/README.md，环境安装见 ENVIRONMENT_SETUP.md，模型组件说明见 model-runtime/README.md。
