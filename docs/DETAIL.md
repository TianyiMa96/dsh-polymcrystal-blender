# SPDX-License-Identifier: GPL-3.0-or-later
# PolyCrystal Blender bridge — detail

DSH × Blender 聚合物晶体桥接插件。三个组成部分:

## 架构与数据流

```
结构文件(CIF/POSCAR/XYZ/PDB/LAMMPS)
        │
        ▼
sidecar(polycrystal_core,隔离 venv)     src/(DSH 宿主,ESM)
  解析 → 空间群扩胞 → 周期键图        polycrystal_load 组装 CLI 参数
  → PBC 聚类展开 → bundle.npz          │
    + manifest.json                    ▼
        │                       127.0.0.1:39888 网关(RPC 长轮询)
        └──────────────►  extension(Blender 4.2+)
                          bridge_client 后台线程 → 主线程 timer
                          → loader_npz 建 mesh
                          → PolyCrystal_Master_GN(Geometry Nodes)
```

- **宿主 `src/`**:5 个模型工具(env/load/modify/inspect/render)、venv 管理、网关与会话认领。不碰 Blender Python。
- **sidecar `sidecar/polycrystal_core/`**:纯算法核心只依赖 numpy/scipy/networkx;晶体学 I/O(gemmi/pymatgen/spglib/ase)与快速邻居表(vesin)为可选加速。
- **扩展 `extension/dsh_polycrystal_ext/`**:`PolyCrystal_Crystal`(原子/键/主链)与 `PolyCrystal_UnitCell`(仅 12 条晶胞边)两个对象共享同一棵 `PolyCrystal_Master_GN`(`GROUP_VERSION 17`);N 面板滑块与 RPC 驱动同一组 GN socket。

## 默认参数(0.3.1)

| 项 | 默认 |
| --- | --- |
| `pbc_mode` | `pbc_cluster`(ghost / pad / half-bond 均为 0) |
| `supercell` | `[1,1,1]`(`"auto"` 需显式指定) |
| 相机 | `ORTHO`,`fill 0.92`,`iso` 视角,`azimuth` 可选 |
| 球面细分 | `Subdivision Quality 3`(320 面/球,162 顶点) |
| 材质 | `DSH_PolyCrystal_Surface` v4(rough/coat 属性驱动);晶胞 `DSH_PolyCrystal_UnitCell` v2 |
| 灯光 | 自适应 Key/Fill/Rim + `PolyCrystal_Spec` 面光 |
| 世界 / 色彩 | 竖向渐变 `0.012→0.085`;AgX Medium High Contrast,exposure +0.2 |
| ghost 原子 | 隐藏(`Show Ghost Boundary Atoms = False`) |

## 功能

1. **环境自动装配**(`polycrystal_env`):跨平台探测 Blender 4.2+ 与 Python/uv,在 `$DSH_HOME/plugins/dsh-polycrystal/.venv` 构建隔离的晶体学 sidecar 环境,一键安装 Blender 扩展、启动/认领 GUI 实例。
2. **结构加载**(`polycrystal_load`):解析 CIF / POSCAR / XYZ / PDB / LAMMPS data,sidecar 完成空间群扩胞、无序组清洗、周期键图同调分类(离散分子 / 一维聚合物链 / 二维片 / 三维网络),按四种 PBC 模式输出,并推送到 Blender 实时显示。
   - `pbc_cluster`(默认):侧车内在导出前做 PBC 聚类——按最小镜像成键、连通分量聚类、从种子原子沿键展开坐标,每条分子/链成为连贯实体;`is_ghost`/`is_boundary_pad`/`half_bond` 全为 0(硬不变量:渲染键长 ≤ 3.5 Å,绝不跨盒伪键)。
   - `periodic_clipped`:晶胞截断 + 半键端帽 + 幽灵边界原子(legacy,补丁产物多)。
   - `chain_continuous`:主链连续展开,完整呈现锯齿/螺旋构象。
   - `hkl_lamella`:(hkl) 晶面切片片晶。
3. **实时修改**(`polycrystal_modify`):样式(球棍/范德华/甘草/主链管)、着色(CPK/链号/主链侧链/无序组)、隐藏氢、高亮链、超胞重建——尊重人类锁定的材质与机位。
4. **状态回读**(`polycrystal_inspect`):读取人类在 Blender N 面板调整的每个滑块、AI 编辑锁状态、选中对象/链、相机,以及视口截图。
5. **出版级渲染**(`polycrystal_render`):EEVEE / Cycles(5.x 引擎 id 为 `BLENDER_EEVEE`),可沿晶轴正交投影,支持透明背景。

## 渲染外观与材质管线

一整套默认视觉是在 Blender 5.3 上逐项测出来的,不是拍脑袋定的参数(以下描述 0.3.1 基线):

- **单一表面材质 + 属性着色**。整个晶体使用一个 `DSH_PolyCrystal_Surface` 材质,颜色/透明度/自发光/粗糙度/清漆来自几何属性:`col`(FLOAT_COLOR 基色)、`alpha`(不透明度)、`glow`(自发光强度)、`rough`(基色粗糙度)、`coat`(清漆权重)。原因见下节"Join Geometry 会重排材质索引"。(0.3.1 起晶胞线框拆成独立对象,见下节。)
- **PBR 清漆**。Principled 上叠一层薄清漆:`Coat Roughness 0.06`、`Specular IOR Level 0.5`、`IOR 1.46`、`Diffuse Roughness 0.15`;`rough` 在材质端再做 `MAXIMUM(attr, 0.26)` 兜底,避免属性缺失时退化成镜面。
- **球棍默认**。`Style Mode 0`、`Atom Scale 0.36`(vdw×0.36)、`Bond Radius 0.15`;CPK 碳 `(0.58,0.59,0.63)`、键 `(0.33,0.34,0.39)`——暗背景下球和棍必须亮度分离,否则糊成一团。
- **晶胞框克制**。0.04 Å 管 + 弱自发光;早期版本因 1 Å 半径变成发光黄笼,把整幅画面染成琥珀色。
- **背景/色彩管理**。世界是竖向暗渐变(`0.012 → 0.085`),AgX "Medium High Contrast"、exposure +0.2、EEVEE 开 raytracing/shadow、64 spp。
- **自适应灯光**。`Key/Fill/Rim` 按 `_mesh_bounds()` 的跨度缩放能量(`gain = (radius/9)²`)并对准结构中心;额外一盏小面光 `PolyCrystal_Spec` 只负责清漆的紧致高光。固定坐标会把主光放进 17 Å 高的晶体内部,只照亮一个角。
- **正交相机默认**。`ensure_camera()` 建 ORTHO 相机;`frame_camera()` 先做 bbox 八角点的透视解,再对 evaluated 顶点迭代收敛——Geometry Nodes 对象的 `obj.bound_box` 只是基础网格,不迭代会差一倍。`fill`(默认 0.92)表示主体最远点到达画面边缘的比例。`perspective=true` 才切回透视相机。
- **相机方位**。`view` 支持 `iso/front/top/side/a/b/c`,`azimuth` 绕世界 Z 偏航(度),`orthographic_crystal_axis` 可在渲染时按晶轴对齐。
- **sensor fit**。`sensor_fit = AUTO` 时 36 mm 传感器对应*较长*的图像边,竖构图必须把宽高角互换,否则竖图会裁掉晶体的两端。

## PBC 聚类展开(0.3.0)

0.2.1 及之前的默认 `periodic_clipped` 是"把单胞内的碎片丢给 Blender 再补":边界 pad 镜像原子被画成完整球、跨边界键退化成半键残端、ghost 补位球,视觉上是贴面/孤立球。0.3.0 把连贯性前移到侧车(`polycrystal_core/polymer_unwrap.py::realize_pbc_cluster`):

1. **周期感知成键**(已有):`pbc_topology.build_bond_graph` 用 `vesin → ASE → 27 镜像 scipy` 的邻居搜索,每条边都带整数格矢平移 `ell`(最小镜像),单胞边界不会切断键。
2. **连通分量聚类**(已有):`analyze_components` 在多重图上做 BFS 生成树,得到 `sigma[v]`(相对种子原子的累计谷矢)与同调秩 rank∈{0,1,2,3}。
3. **unwrap 展开**(新增):`frac[u] + sigma[u]` 把每个原子抬到与种子同一条连续支上——链自然伸出单胞(supercell `[1,1,2]` 下贯穿 2 个周期),小分子也恢复成完整整体。
4. **闭合边裁剪**:展开后逐边复核长度,超过 3.5 Å 的边只可能是周期闭合边(把环闭回单胞的那一根),直接丢弃并计入 `cut_periodic_edges`——不再生成半键端帽或 ghost 拷贝。
5. **刚性摆位**:rank-0 分子整体做整数格矢平移居中;链/片/网络只做横向整数平移(格矢平移,保留链间配准),再对整体沿链轴做一次平移使链在单胞两端对称外伸。
6. **产物清零**:`is_ghost`、`is_boundary_pad`、`half_bond_*` 全为空;manifest 里 `"pbc_unwrapped": true`,`pbc_cluster.clusters[]` 逐簇记录原子数、键数、是否跨周期、unwrap 最大位移、包围盒、度数直方图、端点原子。
7. **回退**:`--pbc-cluster auto|off`。unwrap 自洽性检查失败(生成树边展开后仍超长)时抛 `UnwrapError`,CLI 回退到 `periodic_clipped` 并在 `report.pbc_cluster.warning` 里写明原因;显式指定 legacy `pbc_mode` 时不做聚类。

## 渲染观感与球面精度(0.3.1)

1. **PBR 表面**。`DSH_PolyCrystal_Surface`(v4)在 Principled 上叠一层清漆:清漆强度由 `coat` 属性按分支驱动(原子 0.26 / 主链 0.16 / 键 0.06),`Coat Roughness 0.06`;基色粗糙度由 `rough` 属性驱动(原子 0.30 / 键 0.40 / 主链 0.34),材质端再取 `MAXIMUM(attr, 0.26)` 兜底,避免属性缺失时退化成镜面。其余:`Specular IOR Level 0.5`、`IOR 1.46`、`Diffuse Roughness 0.15`。
2. **小面光 `PolyCrystal_Spec`**。清漆只在有"小而亮"的光源时才给出分子可视化软件那种紧致高光;能量 `220×gain`、`size = span×0.13`,放在相机一侧上方。Fill 120→150,世界渐变顶部 0.055→0.085(`WORLD_VERSION 3`)补一点暗部。
3. **球面精度**。`Subdivision Quality` 默认 2→3:ico 球 80→320 面/球(42→162 顶点)。pbc_111(32 原子)实测总面 2932→10612(+7680),EEVEE 1200×1500 渲染 0.25 s vs 0.25 s——细分不是瓶颈。近景(ortho_scale 3.4)轮廓无棱线。
4. **晶胞框独立对象**。`PolyCrystal_UnitCell`(8 顶点 / 12 边)与 `PolyCrystal_Crystal` 共用同一棵 `PolyCrystal_Master_GN`,各自 mesh 只带自己的岛,因此 N 面板的 `Show Unit Cell Box` 仍然同时驱动两者。晶胞对象设 `visible_shadow = False`,并挂 `DSH_PolyCrystal_UnitCell`(琥珀 `(0.94,0.62,0.14)`、emission 0.20)专用材质。此前 12 根管在球面上留下环状阴影条(占主体像素 0.49%),现在为 0。
5. **相机 `azimuth`**。`frame_camera()` 支持绕世界 Z 偏航(度),GUI 启动脚本早就在传 `azimuth=-35` 但一直被忽略。

## Blender 5.x 兼容要点(踩过的坑)

1. 几何节点修改器**没有 IDProperties** 了:`mod["Socket_3"]=v` 报 `id properties not supported for this type`。改用 `mod.properties.inputs.Socket_3.value`,4.x 回退仍在 `props.set_modifier_input()` 里。
2. `Curve to Mesh` **不看**逐点 `Set Curve Radius`,只用 profile 圆自身 radius → 半径要 `feed(profile.inputs["Radius"], r)`。
3. **`Join Geometry` 会重排分支材质索引**,`Set Material` 和 `Set Material Index` 都挡不住。要按分支上色就只能走属性着色。
4. `Realize Instances` 丢弃点域属性 → 需要存到 **INSTANCE** 域再 realize。
5. `Named Attribute` 节点的 `data_type` 默认 `FLOAT`,读 FLOAT_COLOR 只返回亮度 → 必须显式 `FLOAT_COLOR`。
6. 5.x 的引擎枚举只有 `BLENDER_EEVEE`(没有 `BLENDER_EEVEE_NEXT`)。
7. 注册阶段 `bpy.data.filepath` 处于 `_RestrictData` 状态,读它会抛 `AttributeError`。
8. 5.x 的 `surface_render_method` 只保留 `DITHERED` / `BLENDED`(没有 `OPAQUE`);不透明走 `DITHERED + alpha=1`。材质级 `shadow_method` 已不可靠,晶胞"无影"用对象级 `visible_shadow = False`(EEVEE 与 Cycles 都认)。

## 人机协同

Blender 3D 视口 `N` 键 → **PolyCrystal** 标签页:AI 编辑开关、材质/机位锁、PBC 滑块、无序组切换、晶体学卡片。AI 每轮编辑前先 `polycrystal_inspect` 即可感知人类的最新调整。

## 边界与限制

- 不打包、不分发 Blender 本体;要求用户自装 Blender 4.2+。
- 绝不向 Blender 内置 Python 安装任何包;重型科学计算全部在 DSH 侧隔离 venv。
- 专属网关 `127.0.0.1:39888`(可用 `DSH_POLYCRYSTAL_PORT` 覆盖),绝不监听 9876,与 `sixtysevenlf/dsh-blender-plugin`(:39877)零冲突共存。
- 许可:GPL-3.0-or-later。
