"""Crash-isolated VST3 scanner subprocess.

The parent process should call :func:`scan_vst_plugins_subprocess`. The child
entrypoint imports the compiled engine and performs the native VST scan in a
short-lived process so plugin DLL crashes do not terminate PyDAW itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any


DEFAULT_SCAN_TIMEOUT = float(os.environ.get("PYDAW_VST_SCAN_TIMEOUT", "30"))


@dataclass(frozen=True)
class VstScanResult:
    plugins: list[Any]
    error: str = ""
    timed_out: bool = False
    returncode: int = 0


def _plugin_to_dict(plugin: Any) -> dict[str, Any]:
    return {
        "path": str(getattr(plugin, "path", "")),
        "name": str(getattr(plugin, "name", "")),
        "vendor": str(getattr(plugin, "vendor", "")),
        "category": str(getattr(plugin, "category", "")),
        "sub_categories": str(getattr(plugin, "sub_categories", "")),
        "uid": str(getattr(plugin, "uid", "")),
        "is_instrument": bool(getattr(plugin, "is_instrument", False)),
    }


def _dict_to_plugin(data: dict[str, Any]) -> Any:
    try:
        from pydaw import pydaw_engine

        info = pydaw_engine.VstPluginInfo()
        # Current pybind class exposes readonly C++ fields and cannot be
        # constructed from Python attributes. Fall through to the lightweight
        # object below when assignment is not supported.
        for key, value in data.items():
            setattr(info, key, value)
        return info
    except Exception:
        class PluginInfo:
            pass

        info = PluginInfo()
        info.path = str(data.get("path", ""))
        info.name = str(data.get("name", ""))
        info.vendor = str(data.get("vendor", ""))
        info.category = str(data.get("category", ""))
        info.sub_categories = str(data.get("sub_categories", ""))
        info.uid = str(data.get("uid", ""))
        info.is_instrument = bool(data.get("is_instrument", False))
        return info


def _child_main(argv: list[str]) -> int:
    if os.environ.get("PYDAW_VST_SCAN_CHILD_CRASH") == "1":
        os._exit(86)
    if os.environ.get("PYDAW_VST_SCAN_CHILD_HANG") == "1":
        import time

        time.sleep(3600)

    from pydaw import pydaw_engine

    plugins = pydaw_engine.AudioEngine.scan_vst_plugins(argv)
    payload = {"plugins": [_plugin_to_dict(p) for p in plugins]}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return 0


def scan_vst_plugins_subprocess(
    dirs: list[str], timeout: float = DEFAULT_SCAN_TIMEOUT,
    env: dict[str, str] | None = None,
) -> VstScanResult:
    cmd = [sys.executable, "-m", "pydaw.vst_scan_subprocess", *dirs]
    # Do not expose unrelated application/debug variables to native plugins.
    # Security: TEMP/TMP removed to prevent DLL hijacking attacks
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR",
               "SystemRoot", "PYTHONPATH", "PYDAW_VST_SCAN_CHILD_CRASH",
               "PYDAW_VST_SCAN_CHILD_HANG"}
    child_env = {key: value for key, value in os.environ.items()
                 if key in allowed}
    if env:
        child_env.update({key: value for key, value in env.items()
                          if key in allowed})
    child_env.pop("PYDAW_VST_SCAN_IN_PROCESS", None)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=child_env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return VstScanResult([], error=str(exc), timed_out=True, returncode=-1)

    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        return VstScanResult([], error=err, returncode=proc.returncode)

    try:
        payload = json.loads(proc.stdout or "{}")
        plugins = [_dict_to_plugin(p) for p in payload.get("plugins", [])]
        return VstScanResult(plugins, returncode=proc.returncode)
    except Exception as exc:
        return VstScanResult([], error=f"invalid scanner JSON: {exc}",
                             returncode=proc.returncode)


def main() -> int:
    return _child_main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
