# SPDX-License-Identifier: GPL-3.0-or-later
# dsh-plugin-polycrystal-bridge

DSH × Blender 聚合物晶体结构桥接插件。三个组成部分:

- `src/` — DSH 宿主插件(纯 ESM JavaScript,无需构建):环境探测、sidecar venv 管理、`127.0.0.1:39888` 网关、5 个模型工具(`polycrystal_env/load/modify/inspect/render`)。
- `sidecar/` — 隔离 Python 晶体学引擎(gemmi/pymatgen/spglib/ase/vesin):周期键图同调分类(Rank 0/1/2/3)、聚合物 PBC 三模式、`.npz` + `manifest.json` 输出。
- `extension/` — Blender 4.2+ 扩展 `dsh_polycrystal_ext`:反向拨号网关、N 面板人机协同滑块、Geometry Nodes 程序化渲染。

架构、默认参数与 Blender 5.x 兼容细节:`docs/DETAIL.md`。

## 开发命令

```powershell
# Sidecar 单测(在 venv 就绪后)
uv run --directory sidecar pytest -vv

# 打包 Blender 扩展 zip(fallback: python scripts/pack_extension.py)
```

约束:GPL-3.0-or-later;禁止 AGPL 代码;禁止污染 Blender 内置 Python;禁止监听 9876 端口。
