"""Synchronize freshly built native PyDAW binaries for dev launches.

This module intentionally does not import ``pydaw``: it can run before the
package imports pydaw_engine, avoiding stale editable-install redirects.
"""

from __future__ import annotations

import shutil
import site
from pathlib import Path


def _candidate_build_dirs(root: Path) -> list[Path]:
    build = root / "build"
    dirs = [build / "cp314-cp314-win_amd64" / "Release"]
    if build.is_dir():
        dirs.extend(sorted(build.glob("*/Release")))
    seen: set[Path] = set()
    result: list[Path] = []
    for path in dirs:
        resolved = path.resolve() if path.exists() else path
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def _newest(patterns: list[Path]) -> Path | None:
    files = [p for pattern in patterns for p in pattern.parent.glob(pattern.name)]
    files = [p for p in files if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _installed_package_dirs(local_pkg: Path) -> list[Path]:
    dirs: list[Path] = []
    candidates: list[str] = []
    try:
        candidates.extend(site.getsitepackages())
    except AttributeError:
        pass
    candidates.append(site.getusersitepackages())
    local_resolved = local_pkg.resolve()
    for base in candidates:
        pkg = Path(base) / "pydaw"
        if not pkg.is_dir():
            continue
        try:
            if pkg.resolve() == local_resolved:
                continue
        except OSError:
            pass
        if pkg not in dirs:
            dirs.append(pkg)
    return dirs


def _copy(src: Path | None, destinations: list[Path]) -> None:
    if src is None:
        return
    for dest_dir in destinations:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        shutil.copy2(src, dest)
        print(f"synced {src.name} -> {dest}")


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    local_pkg = root / "pydaw"
    build_dirs = _candidate_build_dirs(root)
    engine = _newest([d / "pydaw_engine*.pyd" for d in build_dirs])
    host = _newest([d / "pydaw_vst_host.exe" for d in build_dirs])
    destinations = [local_pkg, *_installed_package_dirs(local_pkg)]
    _copy(engine, destinations)
    _copy(host, destinations)
    if engine is None or host is None:
        missing = []
        if engine is None:
            missing.append("pydaw_engine*.pyd")
        if host is None:
            missing.append("pydaw_vst_host.exe")
        print("native sync skipped missing: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
