"""
Entry point for PyInstaller build.
This wrapper ensures pydaw is imported as a package, not a script.

The executable is built in console mode so that any unhandled exception is
printed as a full traceback and the console window stays open afterwards.
This lets users copy the error text and report it in GitHub Issues.
"""
import sys
import os
import traceback

# Restore stderr/stdout if they are None (e.g. detached console).
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w')
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w')


def _pause_before_exit() -> None:
    """Keep the console window open so the error stays readable.

    Only pause when a real console is attached (a frozen build launched by
    double-click); never block a normal source run or a piped process.
    """
    if not getattr(sys, "frozen", False):
        return
    try:
        if sys.stdin is None or not sys.stdin.isatty():
            return
    except (ValueError, OSError):
        return
    try:
        input("\nPress Enter to close this window...")
    except (EOFError, OSError):
        pass


def main() -> int:
    from pydaw.app import main as app_main

    try:
        return app_main()
    except BaseException:
        # Full traceback so users can copy the crash log into a GitHub issue.
        print("\n" + "=" * 70, file=sys.stderr)
        print("PyDAW crashed with an unhandled error:", file=sys.stderr)
        print("=" * 70, file=sys.stderr)
        traceback.print_exc()
        print("=" * 70, file=sys.stderr)
        print(
            "Please report this error at "
            "https://github.com/TheJustSavior/PyDAW/issues",
            file=sys.stderr,
        )
        print("=" * 70, file=sys.stderr)
        try:
            sys.stderr.flush()
        except (ValueError, OSError):
            pass
        _pause_before_exit()
        return 1


if __name__ == "__main__":
    sys.exit(main())
