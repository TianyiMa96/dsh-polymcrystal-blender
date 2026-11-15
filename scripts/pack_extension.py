# SPDX-License-Identifier: GPL-3.0-or-later
#!/usr/bin/env python3
"""Package ``extension/dsh_polycrystal_ext`` into a Blender 4.2+ extension zip.

Uses only the Python standard library so it can run on CI without Blender:

    python scripts/pack_extension.py
    python scripts/pack_extension.py --output-dir dist

The archive root contains ``blender_manifest.toml`` and the ``.py`` modules
directly (no wrapping directory), exactly what
``blender --command extension install-file`` expects.
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO_ROOT / "extension" / "dsh_polycrystal_ext"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "extension" / "dist"

REQUIRED_FILES = (
    "blender_manifest.toml",
    "__init__.py",
    "bridge_client.py",
    "props.py",
    "loader_npz.py",
    "geonodes_builder.py",
    "interop_backends.py",
    "ui_panel.py",
)
EXCLUDED_DIRS = {"__pycache__", ".git", ".idea", ".vscode"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".blend1", ".zip"}


def iter_source_files(source: Path):
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        if path.name.startswith("."):
            continue
        yield path, relative


def verify_sources(source: Path):
    problems = []
    for name in REQUIRED_FILES:
        if not (source / name).is_file():
            problems.append(f"missing required file: {name}")
    for path in source.glob("*.py"):
        try:
            header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:5])
        except Exception as exc:
            problems.append(f"cannot read {path.name}: {exc}")
            continue
        if "SPDX-License-Identifier: GPL-3.0-or-later" not in header:
            problems.append(f"{path.name}: missing GPL-3.0-or-later SPDX header")
    manifest = source / "blender_manifest.toml"
    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        for key in ("schema_version", "id", "version", "blender_version_min"):
            if key not in text:
                problems.append(f"blender_manifest.toml: missing '{key}'")
    return problems


def build_zip(source: Path, output: Path):
    files = list(iter_source_files(source))
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path, relative in files:
            archive.write(path, arcname=str(relative).replace("\\", "/"))
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pack the DSH PolyCrystal Blender extension")
    parser.add_argument("--source-dir", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--name", default="dsh_polycrystal_ext.zip")
    args = parser.parse_args(argv)

    source = args.source_dir.resolve()
    if not source.is_dir():
        print(f"error: source directory not found: {source}", file=sys.stderr)
        return 2

    problems = verify_sources(source)
    if problems:
        for problem in problems:
            print(f"error: {problem}", file=sys.stderr)
        return 1

    output = (args.output_dir / args.name).resolve()
    files = build_zip(source, output)
    total = sum(path.stat().st_size for path, _ in files)
    print(f"packed {len(files)} files ({total} bytes) -> {output}")
    for _path, relative in files:
        print(f"  {relative.as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
