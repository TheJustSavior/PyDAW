from __future__ import annotations

import os
import sys

from pydaw import vst_scan_subprocess
from pydaw.engine_bridge import ENGINE_AVAILABLE, EngineBridge
from pydaw.vst_scan_subprocess import VstScanResult, scan_vst_plugins_subprocess


def test_scan_subprocess_missing_dir_returns_empty() -> None:
    result = scan_vst_plugins_subprocess(["Z:/definitely/no/such/vst3/dir"])
    assert result.returncode == 0
    assert not result.timed_out
    assert result.error == ""
    assert result.plugins == []


def test_scan_subprocess_child_crash_does_not_crash_parent() -> None:
    result = scan_vst_plugins_subprocess(
        ["Z:/definitely/no/such/vst3/dir"],
        env={"PYDAW_VST_SCAN_CHILD_CRASH": "1"},
    )
    assert result.returncode != 0
    assert result.plugins == []


def test_scan_subprocess_timeout_does_not_crash_parent() -> None:
    result = scan_vst_plugins_subprocess(
        ["Z:/definitely/no/such/vst3/dir"],
        timeout=0.2,
        env={"PYDAW_VST_SCAN_CHILD_HANG": "1"},
    )
    assert result.timed_out
    assert result.plugins == []


def test_engine_bridge_uses_subprocess_scanner(monkeypatch) -> None:
    if not ENGINE_AVAILABLE:
        return
    monkeypatch.delenv("PYDAW_VST_SCAN_IN_PROCESS", raising=False)
    calls = []
    sentinel = object()

    def fake_scan(dirs: list[str]) -> VstScanResult:
        calls.append(dirs)
        return VstScanResult([sentinel])

    monkeypatch.setattr(
        vst_scan_subprocess, "scan_vst_plugins_subprocess", fake_scan)
    bridge = EngineBridge()
    try:
        plugins = bridge.scan_vst_plugins(["Z:/definitely/no/such/vst3/dir"])
        assert calls == [["Z:/definitely/no/such/vst3/dir"]]
        assert plugins == [sentinel]
    finally:
        bridge.shutdown()


def test_engine_bridge_in_process_fallback(monkeypatch) -> None:
    if not ENGINE_AVAILABLE:
        return
    monkeypatch.setenv("PYDAW_VST_SCAN_IN_PROCESS", "1")
    bridge = EngineBridge()
    try:
        plugins = bridge.scan_vst_plugins(["Z:/definitely/no/such/vst3/dir"])
        assert plugins == []
    finally:
        bridge.shutdown()


def test_scan_module_cli_returns_json_for_missing_dir() -> None:
    import json
    import subprocess

    proc = subprocess.run(
        [sys.executable, "-m", "pydaw.vst_scan_subprocess",
         "Z:/definitely/no/such/vst3/dir"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={k: v for k, v in os.environ.items()
             if k != "PYDAW_VST_SCAN_IN_PROCESS"},
        check=False,
    )
    assert proc.returncode == 0
    assert json.loads(proc.stdout) == {"plugins": []}
