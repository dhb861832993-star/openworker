#!/usr/bin/env python3
"""植被 CSV -> UE5 PCG DataFromCSV 导出器。

将 `/植被摆放` 的输出 (vegetation.csv, 像素/高度图坐标) 转换为
UE5 PCG `DataFromCSV` 节点可直接读取的 CSV (世界坐标, 厘米),
并生成"物种 -> StaticMesh 资产路径"映射模板 (ue5_mesh_map.json),
供 UE5 端 StaticMeshSpawner 按属性选择网格使用。

用法:
  python3 ue5_export.py --input vegetation.csv --output ue5_vegetation.csv \
      --world-scale 100 --mesh-map ue5_mesh_map.json

坐标约定:
  vegetation.csv 的 (x, y, z) 是高度图网格坐标 (像素 + 高度值)。
  导出为 UE 世界坐标 (厘米): X = (x - 0.5*size) * world_scale,
  Y = (y - 0.5*size) * world_scale, Z = z * world_scale。
  world_scale 表示"1 像素 = 多少厘米"，例如地形 2048 像素代表 2km
  时 world_scale = 200000/2048 ≈ 97.7。

输出列 (DataFromCSV 兼容):
  X, Y, Z, Roll, Pitch, Yaw, ScaleX, ScaleY, ScaleZ, Type, Biome, Label

依赖: 仅 Python 标准库。
"""
import argparse
import csv
import json
import os


def load_mesh_map(path: str) -> dict:
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def write_mesh_map_template(types: list[str], path: str) -> None:
    """写入物种 -> StaticMesh 资产路径映射模板（不覆盖已有映射）。"""
    existing = load_mesh_map(path)
    entries = existing.get("meshes", {})
    changed = False
    for t in sorted(set(types)):
        if t not in entries:
            entries[t] = {
                "path": "/Game/Foliage/",
                "scale": 1.0,
                "collision": True,
            }
            changed = True
    if changed or not existing:
        existing["meshes"] = entries
        existing["note"] = (
            "把每种植物的 path 改成你工程里实际的 StaticMesh 资产路径；"
            "scale 是额外整体缩放倍率（配合 CSV 里的 ScaleX/Y/Z 相乘）。"
        )
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"网格映射模板: {path}（请按需编辑路径）")


def main():
    parser = argparse.ArgumentParser(description="植被 CSV -> UE5 PCG DataFromCSV 导出")
    parser.add_argument("--input", "-i", default="vegetation.csv")
    parser.add_argument("--output", "-o", default="ue5_vegetation.csv")
    parser.add_argument("--world-scale", type=float, default=100.0,
                        help="1 像素 = N 厘米 (默认 100 = 1 像素 1 米)")
    parser.add_argument("--center", action="store_true",
                        help="坐标以高度图中心为原点 (默认: 从 0 开始)")
    parser.add_argument("--mesh-map", default="ue5_mesh_map.json",
                        help="物种->资产路径映射 JSON (模板自动生成)")
    args = parser.parse_args()

    rows = []
    with open(args.input, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if not rows:
        print("输入 CSV 为空")
        return

    size = None
    if args.center:
        xs = [float(r["x"]) for r in rows]
        ys = [float(r["y"]) for r in rows]
        size = (max(xs) + min(xs)) / 2.0
        size_y = (max(ys) + min(ys)) / 2.0
    else:
        size, size_y = 0.0, 0.0

    out_rows = []
    for r in rows:
        x = (float(r["x"]) - size) * args.world_scale
        y = (float(r["y"]) - size_y) * args.world_scale
        z = float(r["z"]) * args.world_scale
        scale = float(r["scale"])
        out_rows.append({
            "X": f"{x:.2f}", "Y": f"{y:.2f}", "Z": f"{z:.2f}",
            "Roll": "0", "Pitch": "0", "Yaw": r["rotation"],
            "ScaleX": f"{scale:.4f}", "ScaleY": f"{scale:.4f}", "ScaleZ": f"{scale:.4f}",
            "Type": r["type"], "Biome": r.get("biome", ""), "Label": r.get("label", ""),
        })

    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)
    print(f"UE5 点数据: {args.output} ({len(out_rows)} 株, "
          f"world_scale={args.world_scale} cm/px, center={args.center})")

    types = sorted({r["Type"] for r in out_rows})
    write_mesh_map_template(types, args.mesh_map)

    print("\n下一步 (UE5 编辑器):")
    print("  1. 打开 PCG 图, 添加 DataFromCSV 节点, 文件路径填上面输出的 CSV 绝对路径")
    print("  2. 用 Filter (Attribute: Type) 或直接接 StaticMeshSpawner")
    print("  3. StaticMeshSpawner 的 Mesh 选择用 ByAttribute, 读取 Type 对应资产")
    print("  4. 或用 MCP: PCGToolset.CreateGraph -> AddNode -> ConnectNodePins -> SpawnGraphInstance")


if __name__ == "__main__":
    main()
