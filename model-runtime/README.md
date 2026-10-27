# 模型运行组件

按 2026-10-12 的适配器及 CLI 核对。该目录为平台调用的 ONNX 推理组件；旧独立交付包目录说明已归档。平台使用方式见 [管理与配置](../docs/MANAGEMENT.md)。

## 当前结构与平台调用

```text
model-runtime/
├── src/predict_city_virtual_patch_pipeline_onnx.py
├── model/enforcement_2.onnx       # 本机模型制品，完整交付需另行携带
├── requirements.windows-gpu.txt
├── requirements.gpu.txt
├── requirements.txt
└── test_onnx.py
```

权重和虚拟环境不属于可凭源码克隆重建的业务数据；没有当前 `delivery_manifest.json` 或 `ChangeDetection_Deployment/model/model.onnx` 交付目录约定。

平台 `hebei_onnx` 适配器使用 `CHANGE_ONNX_PYTHON` 指定的解释器运行 `src/predict_city_virtual_patch_pipeline_onnx.py`，传入批次冻结的模型路径、映射、阈值、分辨率、GPU、批大小、预取与进度目录。当前平台单个任务选择一个 GPU，并强制 `--provider cuda`，不允许静默 CPU 执行。CLI 保留 CPU/auto、多 GPU 参数不表示管理界面提供这些选项。

模型须满足两个 RGB 时相、ImageNet 归一化、512×512 NCHW 输入及支持的 logits 输出契约；模型文件以 SHA-256 校验，真实 CUDA 验证后才可创建新批次。详情见管理文档。CUDA provider 名称存在不等于模型实际可执行，也不等于业务精度已验证。

平台传入 `--no_vector`，读取 CLI 概率/原始掩膜后重投影回裁切网格，再由平台后处理、共同有效掩膜、矢量化与统计生成产品。不能将独立 CLI 的原始 Shapefile 与平台最终图斑口径混用。

## 环境

Windows 按 [环境安装](../ENVIRONMENT_SETUP.md) 创建 `model-runtime/.venv-gpu`，使用 `requirements.windows-gpu.txt` 安装核心依赖。安装脚本会复用已配置的完整 CUDA 12 / cuDNN 9 DLL 目录，否则由 ONNX Runtime 的 `cuda,cudnn` extras 安装运行库。可使用 `scripts/check_gpu_runtime.py` 验证实际 GPU 卷积，无需业务模型。Linux GPU 依赖见 `requirements.gpu.txt`，模型还需 NumPy/Rasterio；现有 Docker GPU stage 从服务基础环境安装 GPU ORT 与 CUDA 包。

`requirements.txt` 是独立 CLI 的 GPU 依赖组合，不替代平台服务锁文件。多文件 VRT 构建和 CLI 独立矢量导出还需 GDAL 命令行工具；已有单个 GeoTIFF、关闭矢量输出的示例不要求执行这些工具。

## 独立 CLI 示例

以下是组件调试入口，会写输出文件，不通过平台队列/数据库/批次协议。路径为示例，需替换；输入 CRS 应为米制投影，或显式传入适当的 `--dst_crs`，因为默认分辨率单位为 0.8 米。

从项目根目录，在已配置 DLL 环境的 PowerShell 执行单 GPU、单文件推理：

```powershell
model-runtime/.venv-gpu/Scripts/python.exe model-runtime/src/predict_city_virtual_patch_pipeline_onnx.py --phase_a 'D:\影像\before.tif' --phase_b 'D:\影像\after.tif' --onnx_model 'model-runtime/model/enforcement_2.onnx' --output_dir 'D:\独立测试输出' --provider cuda --device cuda:0 --no_vector
```

显式 `--onnx_model` 很重要：独立 CLI 的路径回退查找 `model.onnx`，不会自动找到平台内置 `enforcement_2.onnx`。CLI `CHANGE_MODEL_PATH` 只影响其默认路径，不能覆盖已冻结的平台模型。更多参数以 `--help` 和 `parse_args()` 为准；平台任务不接受直接注入任意 CLI 参数。

CLI 输出 `*_prob.tif` 为 uint16，0–65535 对应概率 0–1，`*_mask.tif` 为 uint8，变化值 255；未使用 `--no_vector` 时可生成 Shapefile。平台最终掩膜/置信度/统计则以 [API](../docs/API.md) 和工作区口径为准。`test_onnx.py` 是本地探索脚本，不是当前产品验收入口；真实平台验收见 [开发文档](../docs/DEVELOPMENT.md)。
