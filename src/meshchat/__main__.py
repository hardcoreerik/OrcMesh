import sys


class _NullStream:
    """Stand-in for sys.stdout/stderr when running windowed (no console).

    PyInstaller builds with console=False set sys.stdout/stderr to None,
    which crashes any library that calls .write()/.flush() on them
    (e.g. bleak's WinRT backend during BLE scanning).
    """

    def write(self, *args, **kwargs):
        pass

    def flush(self, *args, **kwargs):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

from meshchat.app import main

if __name__ == "__main__":
    sys.exit(main())
