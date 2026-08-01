---
name: 植被摆放
description: 多生态群落植被散布 - 针叶林/阔叶林/草甸/灌木/湿地/荒漠，按真实生态规则分布
allowed-tools: read_file, write_file, replace_in_file, apply_patch, run_shell, grep, todo_write
---

# 植被摆放 Skill

根据地形高度、坡度、坡向、材质、湿度、流量，按真实生态学规则散布植被。

## 使用方式

### 基本用法
```
/植被摆放 在玉龙雪山地形上散布植被
```

### 指定生态群落
```
/植被摆放 散布针叶林+阔叶林+高山草甸，海拔范围3000-5000m
```

### 自定义参数
```
/植被摆放 密度高一些，针叶林占主导，河谷加湿地灌木
```

## 工作流程

### 第一步：分析地形
读取高度图、材质图、流量图，计算：
- 海拔分布（min/max/分位数）
- 坡度分布
- 坡向（北坡/南坡/东坡/西坡）-- 影响光照和蒸发
- 湿度估算（流量累积 + 低洼区）

### 第二步：选择生态群落
根据气候带和海拔范围，从 `config/` 目录加载对应的生态群落配置：
- `config/coniferous.json` - 针叶林（云杉/冷杉/松树/柏树）
- `config/broadleaf.json` - 阔叶林（栎树/桦树/杨树/槭树）
- `config/alpine_meadow.json` - 高山草甸（嵩草/苔草/高山杜鹃）
- `config/shrubland.json` - 灌木/荒漠（锦鸡儿/蒿类/多肉）
- `config/wetland.json` - 湿地/河岸（芦苇/柳树/苔草）
- `config/tropical.json` - 热带雨林（棕榈/榕树/蕨类）

可以组合多个群落，按海拔/坡度/湿度的垂直分带自动选择。

### 第三步：散布植被
使用 `lib/vegetation_v2.py` 执行散布：
```bash
python3 lib/vegetation_v2.py \
  --heightmap eroded.npy \
  --material material_id.npy \
  --flow flow.npy \
  --output vegetation.csv \
  --config config/coniferous.json config/broadleaf.json config/alpine_meadow.json
```

### 第四步：生成预览
散布结果自动输出：
- `vegetation.csv` - 点云数据（x, y, z, type, scale, rotation,群落）
- `vegetation_density.png` - 密度分布热力图
- `vegetation_layers.png` - 各群落分层着色图

## 生态规则引擎

每个物种的分布由以下条件共同决定：

| 条件 | 参数 | 说明 |
|------|------|------|
| 海拔 | min_height, max_height | 垂直分带（如云杉 3000-4000m） |
| 坡度 | min_slope, max_slope | 陡坡只有灌木，缓坡才有乔木 |
| 坡向 | aspects | 北坡喜阴，南坡喜阳 |
| 材质 | materials | 只在特定地表类型生长 |
| 湿度 | min_moisture, max_moisture | 流量累积高=湿润 |
| 密度 | density | Poisson disk 采样间距 |

## 垂直分带示例（温带高山）

```
5000m+    雪线以上：无植被 / 地衣
4000-5000m 高山流石滩：稀疏草本、地衣
3800-4000m 高山草甸：嵩草、苔草、高山杜鹃
3200-3800m 针叶林：云杉、冷杉（北坡密，南坡稀）
2800-3200m 针阔混交林：桦树+云杉
2500-2800m 阔叶林：栎树、杨树
2000-2500m 河谷灌木：柳树、锦鸡儿
```

## 对接 UE5 引擎（PCG）

散布结果可一键转换为 UE5 PCG `DataFromCSV` 可读格式，再通过 MCP
（`mcp__unreal-editor__*`，UE5.5+，PCG 插件）自动在编辑器里建图出植被。

### 第一步：导出 UE5 格式

```bash
python3 lib/ue5_export.py \
  --input vegetation.csv \
  --output ue5_vegetation.csv \
  --world-scale 100 \
  --center \
  --mesh-map ue5_mesh_map.json
```

- 坐标从像素/高度图网格转为 UE 世界坐标（厘米），默认 1 像素 = 1 米
- 输出列：`X, Y, Z, Roll, Pitch, Yaw, ScaleX, ScaleY, ScaleZ, Type, Biome, Label`
- 自动生成 `ue5_mesh_map.json` 物种→StaticMesh 资产路径映射模板，需按工程实际路径编辑

### 第二步（模式A）：MCP 自动构建 PCG 图（推荐）

通过 UE5 MCP（`mcp__unreal-editor__call_tool`）调用 PCG 工具集，构建
`DataFromCSV → AttributeFilter(Type) → StaticMeshSpawner` 图表：

1. `PCGToolset.CreateGraph` - 新建 PCG 图资产（如 `/Game/PCG/PCG_Vegetation`）
2. `PCGToolset.AddNode` - 添加 `DataFromCSV` 节点（jsonParams 填 CSV 文件路径）
3. `PCGToolset.AddNode` - 添加 `AttributeFilter`（按 `Type` 分离各物种，或
   `FilterDataByAttribute`）
4. `PCGToolset.AddNode` - 添加 `StaticMeshSpawner`（Mesh 选择 ByAttribute，
   依据 `Type` 查 `ue5_mesh_map.json` 中的资产路径）
5. `PCGToolset.ConnectNodePins` - 依次连线（Out → In / 输出引脚 → 输入引脚）
6. `PCGToolset.SpawnGraphInstance` - 在场景中生成 PCGVolume（缩放覆盖地形范围）
7. `PCGToolset.ExecuteGraphInstance` - 执行生成

节点可用性以 `ListNativeNodes` / `GetNativeNodeSchema` 查询结果为准；
无法程序化创建时降级到模式B。

### 第三步（模式B）：手动导入（无 MCP）

1. UE 编辑器右键创建 PCG 图，添加 `DataFromCSV` 节点，文件路径指向导出的 CSV
2. 接 `StaticMeshSpawner`，Mesh 选择 By Attribute，或先 `AttributeFilter` 按 `Type` 分物种
3. 场景中放 PCGVolume 覆盖地形，执行图

### 坐标对齐提示

- `ue5_export.py` 输出的坐标以高度图原点或中心为参考；若与 UE 地形错位，
  在 SpawnGraphInstance 的 transform.location 加上偏移即可
- 地形高度图 z 轴缩放（`world_scale`）需与 UE Landscape 的高度尺度一致

## 自定义生态群落

创建新的 JSON 配置文件放在 `config/` 目录下：
```json
{
  "name": "my_forest",
  "climate": "temperate",
  "species": [
    {
      "name": "spruce",
      "color": [38, 70, 46],
      "shape": "conifer",
      "min_size": 1.0, "max_size": 2.0,
      "rules": {
        "min_height": 0.30, "max_height": 0.65,
        "max_slope": 35,
        "aspects": ["N", "NE", "NW"],
        "materials": [4, 5],
        "min_moisture": 0.1
      },
      "density": 0.7
    }
  ]
}
```

## 注意事项
- 依赖 numpy 和 Pillow；`ue5_export.py` 仅用标准库
- 大分辨率（2048）散布可能需要 10-30 秒
- 输出文件默认放在工作区的 pcg_output/ 目录下
- 植被数据可与 `/地形生成` Skill 的输出无缝衔接
- UE5 对接依赖本机 `unreal-editor` MCP（localhost:8000）与 UE5.5+ 的 PCG 插件
