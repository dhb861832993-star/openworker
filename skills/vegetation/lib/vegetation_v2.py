#!/usr/bin/env python3
"""植被散布 v2 - 多生态群落 + 坡向 + 湿度

支持多个生态群落配置文件组合使用，按真实生态规则分布植被。
新增：坡向分析（北坡/南坡）、湿度估算（流量累积）、群落标签输出。
"""

import argparse, json, os, math, csv, random
import numpy as np
try:
    from PIL import Image
except ImportError:
    raise SystemExit("Pillow required")


def compute_aspect(heightmap):
    """计算坡向（0-360度，0=北, 90=东, 180=南, 270=西）"""
    gy, gx = np.gradient(heightmap)
    aspect = np.degrees(np.arctan2(gx, gy))
    aspect = np.where(aspect < 0, aspect + 360, aspect)
    return aspect


def aspect_to_cardinal(deg):
    """坡向角度转方位（N/NE/E/SE/S/SW/W/NW）"""
    directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
    idx = int((deg + 22.5) / 45) % 8
    return directions[idx]


def compute_moisture(flow, heightmap):
    """估算湿度：流量累积百分位秩 + 低洼区加权。

    用秩而非线性归一化：flow 是幂律分布（主干道远大于细流），
    线性归一化会把绝大多数有流像素压到 ~0，导致湿度区分度消失。
    """
    v = flow[flow > 0]
    if len(v) == 0:
        return np.zeros_like(flow, dtype=np.float32)
    sv = np.sort(v)
    m = np.searchsorted(sv, flow) / len(sv)  # 有流像素：秩均匀铺满 0-1
    # 低洼区（高度低于p25）湿度额外加权
    low_land = heightmap < np.percentile(heightmap, 25)
    m = np.where(low_land, m * 1.5 + 0.1, m)
    return np.clip(m, 0, 1)


def poisson_disk(size, min_dist, max_attempts=30, seed=42):
    """Poisson disk 采样"""
    cell = min_dist / math.sqrt(2)
    gw = int(math.ceil(size / cell))
    grid = [None] * (gw * gw)
    points, active = [], []
    rng = random.Random(seed)
    p0 = (rng.uniform(0, size), rng.uniform(0, size))
    points.append(p0); active.append(p0)
    grid[int(p0[1]/cell)*gw + int(p0[0]/cell)] = 0
    while active:
        idx = rng.randint(0, len(active)-1)
        cx, cy = active[idx]
        found = False
        for _ in range(max_attempts):
            a = rng.uniform(0, 2*math.pi)
            r = rng.uniform(min_dist, min_dist*2)
            nx, ny = cx + r*math.cos(a), cy + r*math.sin(a)
            if nx<0 or nx>=size or ny<0 or ny>=size: continue
            gx_i, gy_i = int(nx/cell), int(ny/cell)
            ok = True
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    gy2, gx2 = gy_i+dy, gx_i+dx
                    if 0<=gy2<gw and 0<=gx2<gw:
                        c = grid[gy2*gw+gx2]
                        if c is not None:
                            px, py = points[c]
                            if (nx-px)**2 + (ny-py)**2 < min_dist*min_dist:
                                ok = False; break
                if not ok: break
            if ok:
                points.append((nx, ny)); active.append((nx, ny))
                grid[gy_i*gw+gx_i] = len(points)-1; found = True; break
        if not found: active.pop(idx)
    return points


def cluster_points(size, cluster_dist, cluster_radius, per_cluster,
                   min_inner_dist, seed=42):
    """簇状泊松采样：先撒簇中心（Poisson disk 大间距），
    每个簇中心周围撒 N 棵树（簇内带最小间距，避免重叠）。

    真实森林是成簇成簇的（林窗、地形小洼地、种子扩散聚集），
    而不是全局均匀铺开。簇间留空，簇内紧凑。
    """
    centers = poisson_disk(size, cluster_dist, seed=seed)
    rng = random.Random(seed + 7777)
    pts = []
    for (cx, cy) in centers:
        n = max(2, int(per_cluster * rng.uniform(0.4, 1.4)))
        local = []
        tries = 0
        while len(local) < n and tries < 300:
            tries += 1
            a = rng.uniform(0, 2 * math.pi)
            r = cluster_radius * math.sqrt(rng.random())
            nx, ny = cx + r * math.cos(a), cy + r * math.sin(a)
            if not (0 <= nx < size and 0 <= ny < size):
                continue
            if all((nx - px) ** 2 + (ny - py) ** 2 >= min_inner_dist ** 2
                   for px, py in local):
                local.append((nx, ny))
        pts.extend(local)
    return pts


def load_biomes(config_paths):
    """加载多个生态群落配置"""
    biomes = []
    for path in config_paths:
        with open(path) as f:
            biomes.append(json.load(f))
    return biomes


def _passes_rules(rules, h, s, mat, asp_deg, moist, allowed_aspects):
    """单点是否满足该物种的全部地理规则（海拔/坡度/材质/湿度/坡向）"""
    if "min_height" in rules and h < rules["min_height"]: return False
    if "max_height" in rules and h > rules["max_height"]: return False
    if "min_slope" in rules and s < rules["min_slope"]: return False
    if "max_slope" in rules and s > rules["max_slope"]: return False
    if "materials" in rules and mat not in rules["materials"]: return False
    if "min_moisture" in rules and moist < rules["min_moisture"]: return False
    if "max_moisture" in rules and moist > rules["max_moisture"]: return False
    if allowed_aspects:
        card = aspect_to_cardinal(asp_deg)
        if card not in allowed_aspects: return False
    return True


def _rules_mask(rules, h, s, moist, mat, card, allowed_aspects):
    """向量化地理规则评估 -> 布尔掩码（该物种理论上可生长的全部区域）"""
    m = np.ones(h.shape, dtype=bool)
    if "min_height" in rules:   m &= h >= rules["min_height"]
    if "max_height" in rules:   m &= h <= rules["max_height"]
    if "min_slope" in rules:    m &= s >= rules["min_slope"]
    if "max_slope" in rules:    m &= s <= rules["max_slope"]
    if "min_moisture" in rules: m &= moist >= rules["min_moisture"]
    if "max_moisture" in rules: m &= moist <= rules["max_moisture"]
    if "materials" in rules:    m &= np.isin(mat, rules["materials"])
    if allowed_aspects:         m &= np.isin(card, list(allowed_aspects))
    return m


def _edge_distance(mask, max_d=16):
    """到掩码边缘的像素距离（chamfer 腐蚀，无 scipy 依赖）。
    边缘=1，每向内一圈 +1；内部未触及处保持 max_d+1。"""
    cur = mask.copy()
    dist = np.full(mask.shape, float(max_d + 1), dtype=np.float32)
    for d in range(1, max_d + 1):
        nbr = np.zeros_like(cur, dtype=bool)
        nbr[1:, :]    |= cur[:-1, :]
        nbr[:-1, :]   |= cur[1:, :]
        nbr[:, 1:]    |= cur[:, :-1]
        nbr[:, :-1]   |= cur[:, 1:]
        nbr[1:, 1:]   |= cur[:-1, :-1]
        nbr[:-1, :-1] |= cur[1:, 1:]
        nbr[1:, :-1]  |= cur[:-1, 1:]
        nbr[:-1, 1:]  |= cur[1:, :-1]
        edge = cur & ~nbr
        dist[edge] = d
        cur = cur & nbr
        if not cur.any():
            break
    return dist


def _sample_by_density(D, target, min_dist, seed):
    """按密度场加权采样树位 + 最小间距约束（Poisson 风格拒绝采样）。
    密度高的区域（林地内部）树密，密度低的区域（林窗/林缘）树疏。"""
    ys, xs = np.nonzero(D > 0.04)
    if len(xs) == 0 or target <= 0:
        return []
    w = D[ys, xs].astype(np.float64)
    w = w / w.sum()
    rng = np.random.RandomState(seed)
    n_cand = min(len(xs), target * 5)
    idx = rng.choice(len(xs), size=n_cand, p=w, replace=True)
    cand = [(float(xs[i]), float(ys[i])) for i in idx]
    rng.shuffle(cand)

    cell = min_dist / math.sqrt(2)
    grid = {}
    result = []
    for (px, py) in cand:
        gxi, gyi = int(px / cell), int(py / cell)
        ok = True
        for dy in range(-2, 3):
            for dx in range(-2, 3):
                key = (gyi + dy, gxi + dx)
                if key in grid:
                    qx, qy = grid[key]
                    if (px - qx) ** 2 + (py - qy) ** 2 < min_dist * min_dist:
                        ok = False
                        break
            if not ok:
                break
        if ok:
            grid[(gyi, gxi)] = (px, py)
            result.append((px, py))
            if len(result) >= target:
                break
    return result


# 尝试导入 terrain-gen 的 simplex fbm（林地斑块/林窗噪声）
try:
    _TG_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "..", "terrain-gen", "lib")
    sys.path.insert(0, os.path.normpath(_TG_LIB))
    from simplex import fbm as _fbm
except Exception:
    _fbm = None


def scatter(heightmap, material_id, slope, aspect, moisture, biomes, size, seed=42):
    """生态密度场散布（树位竞争版）。

    真实森林：
      1) 连片林地——同一海拔/坡向/湿度带内符合条件的区域整体成林（密度场高分区）；
      2) 林窗/空地——低频噪声天然造出林中空地（风倒/岩石/草地）；
      3) 林缘渐变——掩码边缘 14px 内密度线性衰减，树线破碎、林缘疏林过渡；
      4) 混交——一个树位一棵树，物种按该处各物种密度场加权竞争（生态位）；
      5) 尺寸梯度——密林中心大树、林缘/林窗小树（最小 = 最大一半）。
    """
    rng = random.Random(seed)
    vegetation = []
    density_map = np.zeros((size, size), dtype=np.float32)
    layer_map = np.zeros((size, size), dtype=np.int8)  # 群落分层着色

    card = np.array([aspect_to_cardinal(a) for a in aspect.ravel()],
                    dtype="<U2").reshape(aspect.shape)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)

    # ---- 第一遍：每物种密度场（掩码 x 斑块噪声 x 林缘衰减） ----
    fields = []  # (sp, D, biome_name)
    for biome_idx, biome in enumerate(biomes):
        biome_name = biome.get("name", f"biome_{biome_idx}")
        for sp in biome.get("species", []):
            rules = sp.get("rules", {})
            allowed_aspects = set(rules.get("aspects", []))
            mask = _rules_mask(rules, heightmap, slope, moisture,
                               material_id, card, allowed_aspects)
            if int(mask.sum()) < 60:
                print(f"  {sp.get('label', sp['name']):8s} ({biome_name}): 无适宜区")
                continue
            D = mask.astype(np.float32)
            if _fbm is not None:
                n_lo = _fbm(xx / 70.0, yy / 70.0, octaves=3, persistence=0.55,
                            seed=seed + biome_idx * 7 + len(sp["name"]))
                n_hi = _fbm(xx / 24.0, yy / 24.0, octaves=2, persistence=0.5,
                            seed=seed + biome_idx * 13 + len(sp["name"]) + 5)
                D = D * (0.30 + 0.70 * n_lo) * (0.82 + 0.18 * n_hi)
            dist = _edge_distance(mask, 14)
            D = D * np.clip(dist / 14.0, 0.0, 1.0)
            fields.append((sp, D, biome_name))
            print(f"  适宜区 {sp.get('label', sp['name']):6s} ({biome_name}): {int(mask.sum()):6d}px")

    if not fields:
        return vegetation, density_map, layer_map

    # ---- 全局密度场：重叠区 = 混交密林（取各物种最大值） ----
    D_global = np.zeros((size, size), dtype=np.float32)
    for _, D, _ in fields:
        np.maximum(D_global, D, out=D_global)

    # ---- 全局树位采样 ----
    dense_n = int((D_global >= 0.5).sum())
    sparse_n = int(((D_global >= 0.12) & (D_global < 0.5)).sum())
    target = int(dense_n / 7.0 + sparse_n / 150.0)   # 成林区树距≈2.65px
    target = max(20, min(target, 12000))              # 上限控 3D 渲染量
    points = _sample_by_density(D_global, target, 2.3, seed + 987654)
    print(f"  全局成林区 {dense_n}px / 稀疏区 {sparse_n}px -> 目标树位 {target}, 实采 {len(points)}")

    # ---- 树位 -> 物种（按各物种局部密度加权竞争） + 尺寸梯度 ----
    valid = 0
    biome_idx_of = {bf: i + 1 for i, (_, _, bf) in enumerate(fields)}
    for (px, py) in points:
        ix, iy = int(px), int(py)
        ws = [float(D[iy, ix]) for _, D, _ in fields]
        tot = sum(ws)
        if tot <= 0.02:
            continue
        r = rng.uniform(0.0, tot)
        acc = 0.0
        sp, D_sp, biome_name = fields[-1]
        for (spf, Df, bf), w in zip(fields, ws):
            acc += w
            if r <= acc:
                sp, D_sp, biome_name = spf, Df, bf
                break
        d_local = float(D_sp[iy, ix])
        h = float(heightmap[iy, ix])

        shape = sp.get("shape", "unknown")
        factor = 0.5 if shape == "shrub" else 0.4
        max_s = sp.get("max_size", 1.5) * factor
        sf = 0.5 + 0.5 * min(1.0, d_local / 0.55)
        sf *= rng.uniform(0.95, 1.05)
        scale = sf * max_s
        rotation = rng.uniform(0, 360)

        vegetation.append({
            "x": round(float(px), 2), "y": round(float(py), 2),
            "z": round(h, 4),
            "type": sp["name"], "label": sp.get("label", sp["name"]),
            "shape": sp.get("shape", "unknown"),
            "biome": biome_name,
            "scale": round(scale, 3), "rotation": round(rotation, 1),
            "color": sp.get("color", [128, 128, 128]),
        })
        density_map[iy, ix] += 1
        layer_map[iy, ix] = biome_idx_of[biome_name]
        valid += 1

    print(f"  植被总数: {valid} 棵")
    return vegetation, density_map, layer_map


def main():
    parser = argparse.ArgumentParser(description="植被散布 v2")
    parser.add_argument("--heightmap", "-i", required=True)
    parser.add_argument("--material", default=None)
    parser.add_argument("--flow", default=None)
    parser.add_argument("--output", "-o", default="vegetation.csv")
    parser.add_argument("--config", nargs="+", required=True, help="生态群落 JSON 配置（可多个）")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    h = np.load(args.heightmap)
    size = h.shape[0]
    mat_id = np.load(args.material) if args.material and os.path.exists(args.material) else None
    flow = np.load(args.flow) if args.flow and os.path.exists(args.flow) else np.zeros_like(h)

    gy, gx = np.gradient(h)
    slope = np.degrees(np.arctan(np.sqrt(gx*gx + gy*gy)))
    aspect = compute_aspect(h)
    moisture = compute_moisture(flow, h)

    biomes = load_biomes(args.config)
    print(f"加载 {len(biomes)} 个生态群落:")
    for b in biomes:
        print(f"  - {b.get('label', b.get('name','?'))}: {b.get('description','')[:50]}")

    print(f"\n散布植被 ({size}x{size})...")
    veg, density_map, layer_map = scatter(h, mat_id, slope, aspect, moisture, biomes, size, args.seed)

    out_dir = os.path.dirname(args.output) or "."
    os.makedirs(out_dir, exist_ok=True)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["x","y","z","type","label","shape","biome","scale","rotation","color"])
        w.writeheader()
        w.writerows(veg)
    print(f"\n植被数据: {args.output} ({len(veg)} 株)")

    # 密度图
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        plt.imsave(os.path.join(out_dir, "vegetation_density.png"), density_map, cmap="YlGn")
        print(f"密度图: vegetation_density.png")

        # 群落分层图
        colors = np.zeros((size, size, 3), dtype=np.uint8)
        palette = [(38,70,46),(85,105,55),(120,140,70),(130,120,55),(80,110,55),(50,100,45)]
        for i in range(len(biomes)):
            c = palette[i % len(palette)]
            colors[layer_map == i+1] = c
        plt.imsave(os.path.join(out_dir, "vegetation_layers.png"), colors)
        print(f"群落分层图: vegetation_layers.png")
    except ImportError:
        d_norm = (density_map - density_map.min()) / (density_map.max() + 1e-9)
        Image.fromarray((d_norm*255).astype(np.uint8), mode="L").save(
            os.path.join(out_dir, "vegetation_density.png"))

    # 统计
    by_biome = {}
    for v in veg:
        by_biome[v["biome"]] = by_biome.get(v["biome"], 0) + 1
    print(f"\n统计:")
    for b, c in sorted(by_biome.items()):
        print(f"  {b}: {c} 株")


if __name__ == "__main__":
    main()
