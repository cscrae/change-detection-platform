# 服务器迁移及分块计算验收

本配方提供单机服务器部署模板及验收脚本，需在实际目标服务器上验证。源码与模板本身不构成 Linux 镜像构建、容器 GPU 或服务器负载测试通过的证据。

目标为单台 Linux NVIDIA GPU 服务器。先保留一个 Go 实例、一个 Python API 实例及 `runtime.workers=1`。SQLite 与结果保存在服务器本地磁盘。原始 TIFF 可以留在中转站，通过服务器已挂载的只读 SMB 目录访问；也可由管理员预先放到服务器数据盘。本配方不会建立挂载或索取密码。

## 1. 准备可迁移的 test_up 索引

Windows 的 VRT 可包含 UNC 共享路径，例如 `\\nas.example.invalid\imagery\hebei\test_up\…`；直接拷贝到 Linux 不能读图。先按 [test_up 接入说明](../TEST_UP_IMPORT.md) 在本机 `.env.local.json` 的 `TEST_UP_SOURCE_ROOT` 字段配置真实共享根目录，已有配置仅补充该字段，不要覆盖其他设置。首次创建可从 `local-settings.example.json` 复制；真实配置不提交到 GitHub。

运行下面命令，仅在**新目录**复制当前 catalog 引用的 VRT XML 与 JSON，并将引用改成容器路径；不打开或复制 TIFF、不修改原目录、不会应用平台设置。项目路径为示例，需换成实际位置：

```powershell
Set-Location 'D:\Projects\change-detection-platform'
& '.\backend-py\.venv-local\Scripts\python.exe' '.\scripts\server_relocate_test_up.py' --input '.\data\imagery\hebei-test-up' --output '.\data\server-export\hebei-test-up-linux'
```

迁移工具按 `--windows-root` 参数 → 进程环境变量 `TEST_UP_SOURCE_ROOT` → 项目 `.env.local.json` 同名字段读取原始 Windows 来源根目录。上述 Windows 命令省略参数时使用本机配置；在没有该配置的 Linux 环境执行迁移时，应显式提供 `--windows-root`，其值必须与输入 VRT 的原始共享根一致。`--container-root` 默认是 `/mnt/imagery`。例如：

```bash
python3 scripts/server_relocate_test_up.py \
  --input /srv/change-detection/catalog/windows-index \
  --output /srv/change-detection/catalog/hebei-test-up-linux \
  --windows-root '\\nas.example.invalid\imagery\hebei\test_up'
```

示例共享地址必须替换为实际值。输出目录必须不存在；再次导出请使用新目录名。把输出目录整体传到服务器，例如 `/srv/change-detection/catalog/hebei-test-up-linux`。可传源码、模型、边界及这一小份索引；无需传整批原图。迁移工具保留当前 catalog 的 scene ID、日期精度、分组和行政区映射，不把月份占位日期解释为实际拍摄日。生成的索引及迁移报告可能包含真实来源路径，应通过有访问控制的方式交付，不作为公开源码提交。

示例映射：

| Windows 来源 | 容器路径 |
| --- | --- |
| `\\nas.example.invalid\imagery\hebei\test_up`（替换为实际配置） | `/mnt/imagery` |
| 导出的索引目录 | `/opt/assets/imagery` |
| `enforcement_2.onnx` 所在模型目录 | `/opt/models`（Go/Python 同路径可见） |
| 专用服务器运行目录 | `/data`（本地可写磁盘） |
| 本地 POI GeoJSON 目录（可为空） | `/opt/assets/poi`（只读） |

例如服务器已把共享根挂到 `/mnt/transit`，则示例原图路径为 `/mnt/transit/hebei/test_up`，对应 `SERVER_IMAGERY_DIR`。服务器的共享登录身份必须有读取权限。断网会使按需读取失败；持续负载时优先使用服务器到共享的稳定链路，并测量实际吞吐。

## 2. 配置及启动（在确定服务器后执行）

先安装服务器驱动、Docker Engine、Compose 插件及 NVIDIA Container Toolkit，并由管理员配置容器 GPU 访问。具体以 [Docker GPU 配置](https://docs.docker.com/compose/how-tos/gpu-support/) 和 [NVIDIA 安装说明](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) 为准。此处不自动安装或修改宿主机配置。

在服务器项目根目录执行，先修改示例路径，确认所有目录已存在：

```bash
cp deploy/server.env.example deploy/server.env
# 编辑 deploy/server.env，使目录、CPU/内存上限匹配这台服务器。
# 以下路径对应示例值；若已修改变量，请创建实际配置的目录。
mkdir -p /srv/change-detection/runtime /srv/change-detection/poi
python3 scripts/server_preflight.py --env-file deploy/server.env --output preflight-host.json
docker compose --env-file deploy/server.env -f deploy/docker-compose.server.yml config --quiet
docker compose --env-file deploy/server.env -f deploy/docker-compose.server.yml up --build -d
docker compose --env-file deploy/server.env -f deploy/docker-compose.server.yml ps
python3 scripts/server_preflight.py --env-file deploy/server.env --runtime --output preflight-runtime.json
```

这是**独立** Compose 文件，不与旧 `docker-compose.yml` 叠加。真实的 `deploy/server.env` 已被 Git 忽略，仅在目标机器保存；公开仓库只保留 `deploy/server.env.example`。`SERVER_DATA_DIR` 必须是新建专用目录，不能复用有 Windows 绝对路径的 SQLite。不得将 SQLite 放在 SMB/NFS。Go/Python 共用该目录，并通过只读同路径模型挂载完成模型登记。服务器镜像不打包业务权重。预检会记录实际模型 SHA256、挂载和资源证据。

`server_preflight.py` 只读检查，不会启动服务、改配置或提交检测。`--runtime` 会读两期代表 VRT 的 16×16 样本像元，不会运行模型。CUDA provider 出现在列表中仍不等于模型已用 GPU 运算。

入口保留回环限制及现有代理令牌。客户端用 SSH 转发访问：

```powershell
# 将 SERVER_ALIAS 换成已配置且授权的 SSH 服务器别名。
ssh -N -L 8088:127.0.0.1:8088 SERVER_ALIAS
```

浏览器打开 `http://localhost:8088`。不直接公开 Go/Python 端口，不在未建立认证的情况下把入口改成 `0.0.0.0`。固定 Docker 子网 `172.30.88.0/24` 必须无冲突；如需更换，要同时修改 Compose 子网与现有 `deploy/nginx.conf` 的网关白名单。

管理页添加或启用来源 `/opt/assets/imagery`，年份留空，校验并应用；验证、启用内置 ONNX 模型。设置 `runtime`：GPU 0、workers 1、batch_size 1、prefetch_batches 1 或 2、queue_capacity 16、timeout_seconds 7200，再校验及应用。这里的 runtime 字段通过管理页持久化并冻结到新任务批次；**不存在** `QUEUE_WORKERS`、`BATCH_SIZE` 等通用环境变量开关。已有持久化瓦片设置优先于 `TILE_*` 启动默认值。

容器默认只请求 1 块 GPU，`gpu_device=0` 指容器内可见序号。不要通过增加 Python API workers 或多个容器共享同一 SQLite 来扩容。多机调度、多 GPU 分配及结果汇总需要后续设计。

### 地图底图、经纬网及 POI

默认底图是 WGS84 基准的 EPSG:3857 XYZ OpenStreetMap 瓦片，浏览器需要能访问该瓦片服务。经纬网、地图中心/鼠标经纬度由页面绘制。配置自有 XYZ 服务时，在 `deploy/server.env` 同时填写 `VITE_BASEMAP_URL`（含 `{z}/{x}/{y}`）、`VITE_BASEMAP_CRS=EPSG:3857`、`VITE_BASEMAP_DATUM=WGS84`、来源署名 `VITE_BASEMAP_ATTRIBUTION`，可选 `VITE_BASEMAP_MAX_NATIVE_ZOOM`（0–22）。前端会拒绝未声明坐标体系或 GCJ-02 / BD-09 底图，避免与遥感影像及经纬网错位；此处不提供坐标偏移猜测。

这些变量经 Docker build args 传入 Vite，是**构建时公开参数**；修改后需重新构建 nginx 前端镜像并重建该服务，单纯重启容器不会更新它们。私钥和服务端密钥不得放进 `VITE_*` 或瓦片 URL，因为值会进入浏览器代码，详见 [Vite 环境变量说明](https://vite.dev/guide/env-and-mode)。

```bash
docker compose --env-file deploy/server.env -f deploy/docker-compose.server.yml up --build -d nginx
```

`SERVER_POI_DIR` 必须是已存在的目录，可以为空；预检会记录文件数并提示“POI 未配置”，不会因尚无数据而阻止启动。把有授权、坐标已确认的真实 `*.geojson` 点数据放入该目录即可由后端默认 `ASSET_DIR/poi` 读取，无需配置 `POI_GEOJSON_PATH`，原文件在容器内只读。默认 WGS84 经度、纬度顺序；EPSG:3857 数据必须带正确坐标声明，GCJ-02 / BD-09 须先经可靠流程转换。不得将示例点伪装为真实地点。放大后页面仅查询当前视野，并显示数据未配置或错误信息。点数据的名称、类别、地址及来源字段说明见 [POI 数据说明](../docs/POI_DATA.md)。

## 3. 先规划，再逐阶段计算

现有推理管线虽按模型窗口推理，但平台后处理会建立整幅概率、掩膜等数组，并读取整幅数据。因此**不能承诺无限制单景面积下内存有界**。应把所选 AOI 拆成独立小块，限制单块像元，并先实测再调高上限。4,000,000 像元只是一项初始准入预算，绝不等于“只需 20 MB 内存”；模型、重投影、瓦片和中间数组都会额外占用内存。

离线规划示例采用灵寿小范围坐标，使用前需确认自己的两期数据覆盖该区域，日期必须与 catalog 完全一致：

```bash
python3 scripts/server_benchmark.py plan \
  --bounds 113.904080 38.700050 113.909960 38.704660 \
  --region-id 130100 --before 2024-11-01 --after 2025-08-01 \
  --resolution 0.8 --max-pixels 4000000 --output lingshou-plan.json
python3 scripts/server_benchmark.py run --plan lingshou-plan.json --output dry-run
```

以上不连接服务、不读影像像元，也不提交任务。经纬度换算使用保守估计加 2% 裕量；真实裁切后还会检查输入宽×高，超过预算就拒绝继续检测。它不是精密的内存预测器。扩大 AOI 前核实两期实际覆盖；矩形网格可能落在稀疏测试切片的空洞，遇到无数据会停止，不能据此误判 GPU 性能。

目标服务器预检、模型 CUDA 验证和地图显示都通过后，显式使用 `--execute`：

```bash
python3 scripts/server_benchmark.py run --plan lingshou-plan.json --stage smoke \
  --catalog /srv/change-detection/catalog/hebei-test-up-linux/catalog.json \
  --storage-dir /srv/change-detection/runtime --execute --output smoke-run
```

工具按当前平台 API 裁切、冻结批次、提交检测、轮询结果；**不自动修改任何管理配置**。它会核对原始 catalog 与当前启用来源的文件、日期和行政区，提交平台实际的来源前缀场景 ID，并只选取与每块相交的两期影像；若多个启用来源匹配，使用 `--source-id` 明确选择管理页中的来源 ID。顺序执行且同时最多 1 个任务。成功后用同一计划逐级验收，pilot 最多前 16 块，scale 为全部计划块：

```bash
python3 scripts/server_benchmark.py run --plan lingshou-plan.json --stage pilot \
  --prior-report smoke-run/summary.json --catalog /srv/change-detection/catalog/hebei-test-up-linux/catalog.json \
  --storage-dir /srv/change-detection/runtime --execute --output pilot-run
python3 scripts/server_benchmark.py run --plan lingshou-plan.json --stage scale \
  --prior-report pilot-run/summary.json --catalog /srv/change-detection/catalog/hebei-test-up-linux/catalog.json \
  --storage-dir /srv/change-detection/runtime --execute --output scale-run
```

小样只有 1 块，上述步骤仍只验证 1 块；要开展规模测试，先给实际有两期覆盖的新 AOI 生成新计划，从该计划的 smoke 重新开始。扩大块数验证排队和累计读写；扩大**单块**像元预算验证峰值内存，两者不可混为一项。不要直接把全省稀疏切片的外包矩形作为单次任务。

每阶段输出 `summary.json`、逐块结果 JSON 及 `metrics.jsonl`，记录：裁切至检测完成耗时、真实裁切像元、任务状态、Docker CPU/内存/网络/块 I/O、Python 容器各进程 RSS 及总 RSS、显存使用/利用率、磁盘剩余空间。总 RSS 可能重复计算共享页；宿主 GPU 指标可能包含其他任务，必须在报告中注明。结果的空间正确性、边界与日期也要人工抽检，并从界面抽样下载 mask、GeoJSON、CSV、Shapefile ZIP 验证产物。

默认失败条件包括：任务失败/超时、来源或配置版本错误、CUDA 验证不足、指标不可用、显存达到 90%、任一服务容器内存达到 85%、运行盘剩余不足 10 GiB、真实裁切像元超预算。阶段失败不会继续提交后续块，也不会自动扩大参数或重试 POST。监测超时/网络中断并不取消服务端任务；先按报告的 `current_task_id` / `progress_id` 查明状态，避免重复提交。工具不会杀进程或容器。

**尚未实现**块间拼接、接缝修正、矢量边界去重及跨块统一统计；每块为独立结果。模型窗口边缘可能形成接缝，不能把分块输出直接视为无缝全域产品。现有任务队列在进程重启后也不保证自动恢复执行；发生重启应检查任务状态后人工恢复。

默认 HTTP 代理读取超时为现有 600 秒；Go 到 Python 的裁切及检测长请求上限为 25 小时，但浏览器入口不会因此无限等待。先测小块，不通过“无限调大超时”掩盖慢读或内存问题；模型检测异步任务自身按持久化 `timeout_seconds` 执行。负载工具的监测超时仅控制客户端等待。

## 4. 后续服务器验收

服务器明确后还需实测：镜像构建与 `pip check`、NVIDIA 库和 ONNX CUDA 真推理、共享断线恢复、真实两期对齐、四类下载、瓦片/底图叠加、持续负载下延迟和峰值资源、服务重建后的数据持久化、备份恢复。未完成这些证据前，不称为“服务器部署或大尺度测试已通过”。

业务数据保留在 `SERVER_DATA_DIR`，原图和模型单独保留。升级不得删除该目录。进行一致性备份时先停止新任务、等待在途完成，再按数据库及其引用文件整体备份；备份恢复步骤需在目标服务器单独验证。

CUDA/cuDNN 版本匹配依据 [ONNX Runtime CUDA 官方文档](https://onnxruntime.ai/docs/execution-providers/CUDA-ExecutionProvider.html)。本项目使用已锁定版本构建，实际 wheel 获取、驱动兼容及依赖可用性仍须服务器验收。
