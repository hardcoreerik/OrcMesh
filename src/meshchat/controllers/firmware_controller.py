"""Threaded facade for firmware network I/O and flashing."""
from __future__ import annotations

from PySide6.QtCore import QObject, QThread, Signal, Slot

from meshchat.services.firmware import discover_release, flash_bundle, prepare_bundle


class _FirmwareWorker(QObject):
    release_found = Signal(object)
    progress = Signal(int, int)
    bundle_ready = Signal(object)
    log = Signal(str)
    completed = Signal(str, bool, str)

    @Slot(str, bool)
    def discover(self, pio_env: str, include_prerelease: bool) -> None:
        try:
            release = discover_release(pio_env, include_prerelease)
            self.release_found.emit(release)
            self.completed.emit("discover", True, f"Found Meshtastic {release.version}")
        except Exception as exc:
            self.completed.emit("discover", False, str(exc))

    @Slot(object, str, str)
    def prepare(self, release, pio_env: str, hw_model: str) -> None:
        try:
            bundle = prepare_bundle(
                release, pio_env, hw_model,
                lambda done, total: self.progress.emit(done, total),
            )
            self.bundle_ready.emit(bundle)
            self.completed.emit("prepare", True, "Firmware downloaded and verified")
        except Exception as exc:
            self.completed.emit("prepare", False, str(exc))

    @Slot(object, str, bool, object)
    def flash(self, bundle, port: str, full_install: bool, expected_usb) -> None:
        try:
            flash_bundle(
                bundle, port, full_install, expected_usb,
                lambda line: self.log.emit(line),
            )
            self.completed.emit("flash", True, "Firmware flash completed")
        except Exception as exc:
            self.completed.emit("flash", False, str(exc))


class FirmwareController(QObject):
    release_found = Signal(object)
    progress = Signal(int, int)
    bundle_ready = Signal(object)
    log = Signal(str)
    completed = Signal(str, bool, str)

    _discover_requested = Signal(str, bool)
    _prepare_requested = Signal(object, str, str)
    _flash_requested = Signal(object, str, bool, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = _FirmwareWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._worker.release_found.connect(self.release_found)
        self._worker.progress.connect(self.progress)
        self._worker.bundle_ready.connect(self.bundle_ready)
        self._worker.log.connect(self.log)
        self._worker.completed.connect(self.completed)
        self._discover_requested.connect(self._worker.discover)
        self._prepare_requested.connect(self._worker.prepare)
        self._flash_requested.connect(self._worker.flash)
        self._thread.start()

    def discover(self, pio_env: str, include_prerelease: bool = False) -> None:
        self._discover_requested.emit(pio_env, include_prerelease)

    def prepare(self, release, pio_env: str, hw_model: str) -> None:
        self._prepare_requested.emit(release, pio_env, hw_model)

    def flash(self, bundle, port: str, full_install: bool, expected_usb) -> None:
        self._flash_requested.emit(bundle, port, full_install, expected_usb)

    def shutdown(self) -> None:
        self._thread.quit()
        self._thread.wait(5000)
