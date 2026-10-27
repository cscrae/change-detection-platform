# 本地 POI 数据接入

平台的 POI 是本地只读 GeoJSON 数据。代码不调用外部 POI 服务，不随程序提供虚构的业务 POI。

## 数据位置

- 未设置环境变量时，读取 `ASSET_DIR/poi/*.geojson`，不递归搜索其他目录。`ASSET_DIR` 默认是项目的 `data` 目录，即项目下的 `data/poi`。
- 设置 `POI_GEOJSON_PATH` 时，仅读取该变量指定的一个本地文件，优先于上述目录。服务器需挂载该文件，并让 Python 服务进程拥有读取权限。
- 数据文件只读；文件大小或修改时间变化后，下次查询会重新解析。多个请求复用缓存及经度索引。
- 最多 32 个文件，总大小 64 MiB，最多 250,000 个要素。超过限制返回明确错误状态，需先裁剪数据或拆分服务数据。

## 格式与坐标系

必须是 `FeatureCollection`，仅展示合法 `Point`。下面只是格式示例，**坐标和名称均为测试占位内容，不能当作真实兴趣点导入**：

```json
{
  "type": "FeatureCollection",
  "name": "数据来源名称",
  "coordinate_system": "EPSG:4326",
  "features": [
    {
      "type": "Feature",
      "geometry": { "type": "Point", "coordinates": [114.3, 38.0] },
      "properties": { "name": "测试占位点", "category": "测试分类", "address": "测试地址" }
    }
  ]
}
```

RFC 7946 GeoJSON 未声明坐标系时按 WGS84 经纬度处理，顺序为 `[经度, 纬度]`。支持顶层 `coordinate_system: "EPSG:4326"`，或命名式 `crs: {"type":"name","properties":{"name":"EPSG:4326"}}`。

明确声明 `EPSG:3857` 的点数据可以自动转换到 WGS84；超出 Web Mercator 有效范围的点会忽略。`GCJ-02`、`BD-09`、未知或冲突的坐标系声明会拒绝整份查询，返回空要素及 `invalid_data` 状态，不猜测偏移关系。影像投影 EPSG:4526 的 POI 应先明确重投影成 EPSG:4326 或 EPSG:3857。

接口只返回 `name`、`category`、`address` 三个显示字段和服务器生成的 `source_id`，丢弃原始属性中的其他内容。显示字段会移除 HTML、控制字符并限制长度。前端弹窗仍应通过 DOM `textContent` 写入文本，不能把任何 POI 字符串当作 HTML 模板。

## 接口

```text
GET /py/local/poi?bbox=113,37,115,39&limit=500
```

`bbox` 必填，顺序为西、南、东、北，使用 WGS84，经纬度边界分别为 ±180、±90；必须西小于东、南小于北，不接受跨日期变更线的反向框。查询包含边界点。`limit` 默认 500，上限 2000。

返回 GeoJSON `FeatureCollection`，附加：

| 字段 | 含义 |
| --- | --- |
| `configured` | 是否发现/明确指定了 POI 数据源 |
| `status` | `ready`、`not_configured`、`unavailable` 或 `invalid_data` |
| `message` | 可直接展示的数据状态说明，不含服务端绝对文件路径 |
| `coordinate_system` | 输出始终为 `EPSG:4326` |
| `source_coordinate_system` | 原始坐标系；多源混合为 `mixed`；未加载时为 `null` |
| `matched_count` | 查询框内所有有效点数 |
| `truncated` | 有效点数是否超过本次 `limit` |
| `skipped_count` | 数据源中忽略的无效或非点要素数 |
| `sources` | 数据源 `id`、显示名称和原始坐标系；可由要素 `source_id` 关联 |

未配置、共享不可用、GeoJSON 错误等数据状态以 HTTP 200 空要素及上述状态返回。请求 `bbox` 不合法返回 HTTP 400；缺少必填参数或 `limit` 越界返回 HTTP 422。

## 只读校验

PowerShell 示例（将示例项目目录和文件路径替换为实际位置，真实机器路径不要写回公共文档）：

```powershell
Set-Location 'D:\Projects\change-detection-platform'
& '.\backend-py\.venv-local\Scripts\python.exe' '.\scripts\validate_poi.py' --path 'D:\Datasets\poi\places.geojson'
```

命令默认只输出状态、计数及坐标系摘要；需要查看安全化后的要素时增加 `--show-features`。状态 `ready` 时退出码为 0，缺少数据或数据错误时为 1，参数无效为 2。命令不下载数据，不写入源文件，不修改服务配置；`--path` 只影响本次进程。生产部署需设置 Python 服务自身的 `POI_GEOJSON_PATH`。
