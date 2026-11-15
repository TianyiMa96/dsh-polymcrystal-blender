# SPDX-License-Identifier: GPL-3.0-or-later
# dsh-plugin-polycrystal-bridge / PolyCrystal Blender bridge

[![](https://img.shields.io/badge/powered_by-dsh-4D6BFE?style=flat-square&logo=deepseek&logoColor=white)](https://github.com/deepseek-ai/deepseek-harness)
[![npm version](https://img.shields.io/npm/v/dsh-plugin-polycrystal-bridge?style=flat-square)](https://www.npmjs.com/package/dsh-plugin-polycrystal-bridge)
[![npm downloads](https://img.shields.io/npm/dm/dsh-plugin-polycrystal-bridge?style=flat-square)](https://www.npmjs.com/package/dsh-plugin-polycrystal-bridge)
[![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue?style=flat-square)](./LICENSE)

DSH × Blender 聚合物晶体结构桥接插件(GPL-3.0-or-later)。加载 CIF/POSCAR/LAMMPS 聚合物晶体,经隔离 Python PBC 引擎处理周期性边界,推送到 Blender 4.2+ GUI(已在 5.3 上端到端验证)供人与 AI 实时协同编辑与出版级渲染。

- `src/` — DSH 宿主插件(纯 ESM JS):环境探测、sidecar venv、`127.0.0.1:39888` 网关、5 个模型工具。
- `sidecar/` — 隔离 Python 晶体学引擎(pymatgen/spglib/gemmi/ase/vesin):周期键图同调、PBC 聚类展开(默认 `pbc_cluster`)、`bundle.npz` 输出。
- `extension/` — Blender 4.2+ 扩展 `dsh_polycrystal_ext`:反向拨号、N 面板、Geometry Nodes。
- `skills/polymer-crystallography/` — 指导模型的标准操作技能。
- `scripts/` — 扩展打包(`pack_extension.py`)与 headless 冒烟测试(`test_blender_ext.py`)。

## 特性(0.3.1)

- **PBC 聚类展开(默认)**。侧车按最小镜像成键、连通分量聚类、沿生成树展开(`polycrystal_core/polymer_unwrap.py`),链/分子在写出 bundle 前就是连贯实体;`ghost`、`boundary_pad`、`half_bond` 全为 0,渲染键长 ≤ 3.5 Å。`periodic_clipped` / `chain_continuous` / `hkl_lamella` 作为显式回退。
- **超胞默认 `[1,1,1]`**。输入 CIF 通常已是扩胞结构;需要沿链轴拉伸时显式传 `"auto"`。
- **正交相机默认**。`frame_camera` 对 evaluated 几何精确装箱(`fill` 默认 0.92),支持 `azimuth` 绕世界 Z 偏航与 `a`/`b`/`c`/`chain_axis` 晶轴视角;`perspective=true` 可切回透视。
- **球面精度默认 3**。icosphere 320 面/球(162 顶点),近景轮廓无棱线。
- **单材质属性着色**。一个 `DSH_PolyCrystal_Surface` 材质,颜色/透明度/自发光/粗糙度/清漆由 `col`/`alpha`/`glow`/`rough`/`coat` 几何属性驱动,规避 Blender 5.x `Join Geometry` 的材质索引重排。
- **晶胞独立对象**。`PolyCrystal_UnitCell` 只含 12 条晶胞边,设 `visible_shadow=False`,不再在球面上投出环状阴影条。
- **默认观感**。ghost 原子默认隐藏;自适应三点光 + `PolyCrystal_Spec` 小面光;竖向渐变世界;AgX Medium High Contrast;球棍 `Atom Scale 0.36` / `Bond Radius 0.15`。
- **宿主工具**。`polycrystal_env/load/modify/inspect/render`,支持实时改样式、回读 N 面板状态、EEVEE/Cycles 出版级渲染。

改动历史见 `CHANGELOG.md`,架构与 5.x 兼容细节见 `docs/DETAIL.md`,开发命令见 `AGENTS.md`。

## 安装

1. DSH 中安装本插件目录(Plugins > Add plugin)。
2. 首轮对话模型会自动调用 `polycrystal_env(action="doctor")`,完成 sidecar `.venv` 构建与 Blender 扩展安装;也可手动执行:

```bash
# 打包扩展(仅标准库,无需 Blender)
python scripts/pack_extension.py

# 安装到 Blender 用户扩展目录(非破坏式)
"C:/Program Files/Blender Foundation/Blender 5.3/blender.exe" --command extension install-file \
  -r user_default -e extension/dist/dsh_polycrystal_ext.zip

# 可选:侧车可编辑安装
"<venv>/Scripts/python.exe" -m pip install -e sidecar
```

3. `polycrystal_env(action="launch_gui")` 打开 Blender GUI,之后 `polycrystal_load` 加载结构。

## 快速上手

```text
polycrystal_env(action="doctor")
polycrystal_load(file_path="PCLtrue.cif")                 # pbc_cluster + [1,1,1]
polycrystal_modify(style_mode=0, color_scheme=1, highlight_chain_ids=[0])
polycrystal_inspect(include_viewport_capture=true)        # 回读人类在 N 面板的调整
polycrystal_render(resolution=[1600,1200], fill=0.92, orthographic_crystal_axis="c")
```

N 面板(`N` → **PolyCrystal**)提供同样的样式/着色/PBC 开关、AI 编辑暂停、相机与材质锁;AI 每轮修改前应先 `polycrystal_inspect`。

## 工具与参数

### `polycrystal_env`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `action` | enum | 必填 | `doctor` / `setup_sidecar` / `install_blender_ext` / `launch_gui` / `list_sessions` / `claim_session` |
| `session_id` | string | — | `claim_session` 时要认领的实例;省略则认领第一个活跃实例 |

### `polycrystal_load`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `file_path` | string | 必填 | `.cif` / `POSCAR` / `CONTCAR` / `.xyz` / `.pdb` / LAMMPS `.data` |
| `supercell` | int[3] \| `"auto"` | `[1,1,1]` | 显式扩胞;`"auto"` 沿链轴扩到至少 2 个周期 |
| `pbc_mode` | enum | `pbc_cluster` | `pbc_cluster` / `periodic_clipped` / `chain_continuous` / `hkl_lamella` |
| `boundary_pad` | number[3] | `[0.05,0.05,0.05]` | legacy 模式的边界填充比例 |
| `disorder_filter` | enum | `default` | `default` / `all` / `group_2` |
| `backend` | enum | `native_gn` | `native_gn` / `batoms` / `molecularnodes` |
| `structure_id` | string | 自动 | 结构稳定 id(默认由文件+参数哈希派生) |
| `session_id` | string | — | 目标 Blender 实例 |

### `polycrystal_modify`

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `structure_id` / `session_id` | string | 目标结构与实例 |
| `style_mode` | int | 0 球棍 / 1 范德华 / 2 甘草 / 3 主链管+侧链棍 |
| `color_scheme` | int | 0 CPK / 1 链号 / 2 主链-侧链 / 3 无序组 |
| `hide_hydrogens` | bool | 隐藏氢及其键 |
| `show_backbone_tube` | bool | 主链平滑管 |
| `show_ghost_atoms` | bool | 显示周期边界 ghost 原子(默认关) |
| `show_half_bonds` | bool | 显示半键端帽(legacy 模式) |
| `show_unit_cell` | bool | 显示晶胞线框 |
| `atom_scale` / `bond_radius` | number | 原子缩放 / 键半径 |
| `highlight_chain_ids` | int[] | 高亮链号 |
| `hkl_plane` | number[4] | `[h,k,l,shift]` 晶面叠加 |
| `supercell` | int[3] | 新超胞,触发 sidecar 重建 |
| `pbc_mode` | enum | 新 PBC 模式,触发 sidecar 重建 |
| `respect_locks` | bool | 默认 `true`,遵守人类相机/材质锁 |

### `polycrystal_inspect`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `include_viewport_capture` | bool | `true` | 附带视口 PNG 截图 |
| `session_id` | string | — | 目标实例 |

### `polycrystal_render`

| 参数 | 类型 | 默认 | 说明 |
| --- | --- | --- | --- |
| `engine` | enum | `BLENDER_EEVEE_NEXT` | `BLENDER_EEVEE_NEXT` / `BLENDER_EEVEE` / `CYCLES`(5.x 自动映射到 `BLENDER_EEVEE`) |
| `samples` | int | 64(E) / 128(C) | 采样数 |
| `resolution` | int[2] | `[1600,1200]` | 输出分辨率 |
| `orthographic_crystal_axis` | enum | `current_user_view` | `a` / `b` / `c` / `chain_axis` / `current_user_view` |
| `transparent` | bool | `false` | 透明背景 |
| `refit` | bool | `true` | 分辨率变化后重新装箱(相机锁定时跳过) |
| `fill` | number | `0.92` | 主体到达画面边缘的比例(0.4–0.98) |
| `output_path` | string | staging/renders | PNG 输出路径 |
| `session_id` | string | — | 目标实例 |

## 测试与打包

```bash
# sidecar 单测(20 条)
<venv>/Scripts/python.exe -m pytest sidecar/tests -q

# 扩展 headless 冒烟(49 checks)
"C:/Program Files/Blender Foundation/Blender 5.3/blender.exe" -b --factory-startup \
  --python-exit-code 1 -P scripts/test_blender_ext.py

# 打包扩展 zip -> extension/dist/dsh_polycrystal_ext.zip
python scripts/pack_extension.py
```

宿主插件本身不打包 Blender,也不向 Blender 内置 Python 安装任何包。

## node_modules(宿主包内联)

`src/tools/*.js` 以官方模式 `import { defineTool } from '@deepseek-ai/dsh-tools'` 注册工具,但该包未发布到 npm(安装内版本 0.2.2-rc.3),profile 安装的插件无法从 DSH 安装目录解析它。因此本仓库自带 `node_modules/`(MIT 许可的 `@deepseek-ai/{dsh-tools,cordis,schemastery,dsh-scope,dsh-llm,dsh-util-values,cosmokit,dsh-typert-protocol,dsh-util-crypto,dsh-brand,dsh-timeout,dsh-error}` + `zod`,共约 6.7 MB,从本机 DSH 安装 `app.asar` 提取,与其版本精确匹配)。升级 DSH 后需重新提取同步。若日后 `@deepseek-ai/dsh-tools` 正式发布,可改为常规依赖。

## 致谢(Acknowledgments)

本项目站在以下开源项目的肩膀上,感谢这些库的作者与社区:

**平台与宿主**

- [Blender](https://www.blender.org/) — Blender Foundation,几何节点程序化建模与渲染宿主(GPL,本插件为其扩展)。
- [DeepSeek Harness (DSH)](https://github.com/deepseek-ai/deepseek-harness) — 插件宿主与 agent 工具框架;`@deepseek-ai/dsh-tools` 等运行时依赖(MIT)随仓库 `node_modules/` 内联分发。

**科学计算核心**(sidecar 直接依赖,均为宽松许可)

- [NumPy](https://numpy.org/) / [SciPy](https://scipy.org/) (BSD-3) — 数值与 27 镜像邻居搜索回退。
- [NetworkX](https://networkx.org/) (BSD-3) — 周期键图连通分量聚类与生成树展开。

**晶体学后端**(可选依赖 `crystal`/`fast` extra)

- [gemmi](https://gemmi.readthedocs.io/) (MPL-2.0) — CIF/结构读取与对称操作。
- [spglib](https://spglib.readthedocs.io/) (BSD-3) — 空间群识别。
- [ASE](https://wiki.fysik.dtu.dk/ase/) (LGPL-2.1) — 周期结构与邻居列表回退。
- [pymatgen](https://pymatgen.org/) (MIT) — 晶体结构分析与晶格操作。
- [vesin](https://github.com/Luthaf/vesin) (MPL-2.0) — 高性能周期邻居列表。

**借鉴与参考**

- [batoms](https://github.com/ex4cycle/batoms) 与 [Molecular Nodes](https://github.com/BradyAJohnston/MolecularNodes) — Blender 分子/结构可视化的先行者,本插件的可选渲染后端接口与其思路同源(见 `interop_backends.py`,当前仅作能力检测)。
- CPK 配色约定(Corey-Pauling-Koltun) — 元素标准着色方案。
- 单材质 + 几何属性着色的架构,是为规避 Blender 5.x `Join Geometry` 材质索引重排问题而独立设计;文中相关 5.x 兼容结论均为本仓库实测(见 `docs/DETAIL.md`)。

## 约束与许可

- 不打包、不分发 Blender 本体;要求用户自装 Blender 4.2+(实测 5.3)。
- 不污染 Blender Python;重型科学计算全部在 DSH 侧隔离 venv。
- 专属网关 `127.0.0.1:39888`(可用 `DSH_POLYCRYSTAL_PORT` 覆盖),绝不监听 9876。
- 禁 AGPL 代码;本仓库为 GPL-3.0-or-later。
