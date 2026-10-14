"""
Entry point for PyInstaller build.
This wrapper ensures pydaw is imported as a package, not a script.
"""
import sys
import os

# Fix for --windowed mode: restore stderr/stdout if they're None
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')

from pydaw.app import main

if __name__ == "__main__":
    sys.exit(main())
