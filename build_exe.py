"""
PyInstaller build script for PyDAW standalone executable.
Usage: python build_exe.py
"""
import PyInstaller.__main__
import sys
from pathlib import Path

# Get project root
project_root = Path(__file__).parent.absolute()

# Check for required files
pyd_file = project_root / "pydaw" / "pydaw_engine.cp314-win_amd64.pyd"
vst_host = project_root / "pydaw" / "pydaw_vst_host.exe"

print("Building PyDAW standalone executable...")
print(f"Project root: {project_root}")
print(f"\nChecking required files:")
print(f"  Engine module: {pyd_file} - {'OK' if pyd_file.exists() else 'MISSING'}")
print(f"  VST host: {vst_host} - {'OK' if vst_host.exists() else 'MISSING'}")

# PyInstaller arguments
args = [
    str(project_root / 'run_pydaw.py'),  # Entry point
    '--name=PyDAW',
    '--windowed',  # No console window
    '--onedir',  # Directory bundle (easier to debug than onefile)
    
    # Include data files
    f'--add-data={pyd_file};pydaw',
    f'--add-data={vst_host};pydaw',
    
    # Hidden imports
    '--hidden-import=PySide6.QtCore',
    '--hidden-import=PySide6.QtGui',
    '--hidden-import=PySide6.QtWidgets',
    '--hidden-import=numpy',
    '--hidden-import=soundfile',
    '--hidden-import=pydaw.model',
    '--hidden-import=pydaw.ui',
    '--hidden-import=pydaw.engine_bridge',
    '--hidden-import=shiboken6',
    
    # Collect submodules
    '--collect-submodules=pydaw',
    '--collect-all=PySide6',
    
    # Copy binary extensions
    '--copy-metadata=pydaw',
    
    # Output directory
    f'--distpath={project_root}/dist_pyinstaller',
    f'--workpath={project_root}/build_pyinstaller',
    
    # Clean build
    '--clean',
    
    # Additional options
    '--noconfirm',  # Overwrite without asking
]

print("\nPyInstaller arguments:")
for arg in args:
    print(f"  {arg}")

try:
    PyInstaller.__main__.run(args)
    print("\n[OK] Build completed successfully!")
    print(f"Executable: {project_root}/dist_pyinstaller/PyDAW/PyDAW.exe")
except Exception as e:
    print(f"\n[ERROR] Build failed: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
    sys.exit(1)
