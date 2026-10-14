"""PyDAW - Digital Audio Workstation (Python UI + C++ engine)."""

import os
import importlib.machinery
import importlib.util
import sys

# Editable/dev installs can leave the installed package directory before the
# workspace package in ``__path__``. Keep local native binaries paired with the
# local Python code so pydaw_engine and pydaw_vst_host do not come from an older
# site-packages build during development.
_here = os.path.dirname(__file__)
if _here in __path__:
    __path__.remove(_here)
__path__.insert(0, _here)


def _load_local_native_module(name: str) -> None:
    fullname = f"{__name__}.{name}"
    if fullname in sys.modules:
        return
    for suffix in importlib.machinery.EXTENSION_SUFFIXES:
        path = os.path.join(_here, name + suffix)
        if not os.path.exists(path):
            continue
        spec = importlib.util.spec_from_file_location(fullname, path)
        if spec is None or spec.loader is None:
            return
        module = importlib.util.module_from_spec(spec)
        sys.modules[fullname] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(fullname, None)
            return
        setattr(sys.modules[__name__], name, module)
        return


_load_local_native_module("pydaw_engine")

__version__ = "0.1.1"
