# 容器部署配方

新增服务器迁移、只读中转站挂载及分块负载验收，请使用[服务器与大尺度计算说明](SERVER_SCALE.md)和独立 `docker-compose.server.yml`。2026-10-27 目标服务器尚未确定；新配方已做本地索引迁移及离线计划验证，Linux 容器/GPU 服务器验收仍待执行。下文保留原本机容器配方。

按 2026-10-12 的 Compose、Dockerfile、nginx 和代理脚本核对。尚无已完成 Docker 构建、Linux GPU 或容器浏览器验收的证据；本机历史 CUDA 报告范围见 [系统状态](../docs/CURRENT_STATUS.md)。以下为待执行部署步骤，不表示已验证成功。

## 构建与资源

从项目根目录在支持 NVIDIA Container Toolkit 的 Docker 主机执行：

```powershell
docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml up --build -d
docker compose -f deploy/docker-compose.yml ps
```

默认 Compose 已使用 Python GPU stage 并请求 NVIDIA GPU；`docker-compose.gpu.yml` 是可叠加的兼容文件，不再是启用 GPU 的必需步骤。Dockerfile 的 cpu stage 是基础构建阶段，不提供当前平台 CPU 检测模式。

构建依赖互联网、当前锁文件及模型制品；源码克隆如缺少 `model-runtime/model` 的模型，须单独补齐。不能假定本项目已有可用的离线镜像包。`export-images.ps1` 可在完成实际构建后导出镜像，镜像归档不包括业务数据库和外部影像。

| 路径 | 用途 |
|---|---|
| `/data` | Go/Python 共用可写运行卷：SQLite、模型、裁切影像和结果 |
| `/opt/assets/imagery` | 默认只读影像挂载，初次业务配置来源 |
| `/opt/assets/boundaries` | 只读行政边界挂载 |
| `/opt/assets` | `ASSET_DIR` |
| 项目 `backend-go/data` → `/legacy` | init-data 的只读初始迁移来源 |

多影像来源先只读挂载到 Python 容器，再在管理页登记容器内绝对路径，不能直接登记 Windows 盘符。模型服务端路径导入需路径对 Go 容器可见；导入后制品写入共享 `/data/models`，Python 执行使用该共享文件。浏览器上传无需额外挂载。

init-data 使用 `scripts/relocate_data.py` 初始化/迁移共享卷；历史缺失文件不会因搬迁自动恢复。容器启动后仍需在管理页验证模型、应用有效来源并确认 Go/Python 同一配置版本。

## 本机入口

仅发布 `127.0.0.1:${HTTP_PORT:-80}`，默认浏览器入口为 `http://localhost`。固定子网 `172.30.88.0/24`，nginx 入口仅允许主机网关 `172.30.88.1` 和回环连接并检查 Host。子网冲突时须同时调整 Compose 与 nginx，实际可用性待目标主机验证。Go/Python 不发布主机端口。

Go 首次创建 `/data/control.token`；nginx 启动脚本从只读运行卷取令牌，代理 Go 时加入 `X-CDP-Proxy`，Go 仅在 `CDP_TRUST_LOCAL_PROXY=1` 时接受。浏览器不能直接调用 Python 管理与计算写入口。保留回环发布、入口限制和令牌约束；该配方针对本机单人使用，不是远程多用户部署。

## 待完成验收与数据保留

容器验收至少应包括健康与代理、配置同步、实际 CUDA 模型验证、真实裁切/检测、四类下载、瓦片显示、容器重建持久化及入口限制。健康检查通过不代表这些项目已通过。

业务设置保存在共享卷 SQLite 中；普通重建应保留共享卷。备份需一致地保留数据库和其引用的裁切、结果、模型，并单独备份原始影像及边界；实际备份/恢复流程尚待设计与验证。`docker compose down -v` 会删除运行卷，不能用作保留业务数据的升级操作。

历史迁移证据见 [迁移索引](../docs/migration/README.md)，旧部署说明见 [归档](../docs/archive/README.md)。未来容器验收与备份工作均为 [待决策候选](../docs/ROADMAP.md)。
