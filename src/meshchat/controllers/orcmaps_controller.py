"""Threaded facade for OrcMaps pack management (verify / cut a pack).

Pack work shells out to OrcMaps' host tooling, which can run for minutes and
prints a long stream of output, so it happens on its own QThread and every line
is streamed back to the UI.

One deliberate difference from FirmwareController: a running operation *is*
cancellable. The child process handle is kept so ``cancel()`` (and therefore app
shutdown) terminates the external tool instead of blocking exit on a stuck
`go-pmtiles`, which copying the firmware controller's unbounded
``thread.wait()`` would have risked.
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from meshchat.services.orcmaps import (
    OrcMapsError,
    OrcMapsTools,
    PinPackRequest,
    provision_pin_pack,
    verify_directory,
)

log = logging.getLogger(__name__)


class _OrcMapsWorker(QObject):
    """Blocking OrcMaps work. Lives on the controller's QThread."""

    log = Signal(str)
    completed = Signal(str, bool, str)   # operation, success, detail

    def __init__(self) -> None:
        super().__init__()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    # ── Operations ─────────────────────────────────────────────────────

    @Slot(object, object)
    def verify(self, tools: OrcMapsTools, directory: Path) -> None:
        try:
            ok, output = verify_directory(tools, Path(directory))
        except OrcMapsError as exc:
            log.exception("Pack verification failed")
            self.completed.emit("verify", False, str(exc))
            return
        for line in output.splitlines():
            self.log.emit(line)
        self.completed.emit(
            "verify", ok,
            "Verified — a device would install these packs." if ok
            else "Verification failed — a device would not use this directory as-is.",
        )

    @Slot(object, object)
    def provision(self, tools: OrcMapsTools, request: PinPackRequest) -> None:
        self._cancelled = False
        try:
            ok, output = provision_pin_pack(
                tools, request, on_output=self.log.emit, on_start=self._on_start,
            )
        except OrcMapsError as exc:
            log.exception("Pack build could not run")
            self.completed.emit("provision", False, str(exc))
            return
        finally:
            self._process = None

        if self._cancelled:
            self.completed.emit("provision", False, "Pack build cancelled")
            return
        if not ok:
            tail = output.strip().splitlines()[-1] if output.strip() else "see the log"
            self.completed.emit("provision", False, f"Pack build failed: {tail}")
            return
        if request.dry_run:
            # A preview cuts nothing, so there is no output to verify — checking
            # it would report "no usable packs" for a run that worked exactly as
            # asked.
            self.completed.emit("provision", True, "Preview complete — nothing was cut")
            return

        # Built. Now prove the staged card layout with OrcMaps' own discovery, so
        # a pack a device would refuse is never reported to the user as success.
        pack_dir = request.pack_dir
        if pack_dir is None:
            self.completed.emit("provision", True, "Pack built (nothing to stage)")
            return
        try:
            verified, verify_output = verify_directory(tools, pack_dir)
        except OrcMapsError as exc:
            self.completed.emit(
                "provision", False, f"Pack built, but verification could not run: {exc}"
            )
            return
        for line in verify_output.splitlines():
            self.log.emit(line)
        if verified:
            self.completed.emit("provision", True, f"Pack built and verified in {pack_dir}")
        else:
            self.completed.emit(
                "provision", False,
                "Pack was built but failed OrcMaps' own verification — see the log",
            )

    # ── Cancellation ───────────────────────────────────────────────────

    def _on_start(self, process: subprocess.Popen) -> None:
        self._process = process

    def cancel(self) -> None:
        """Terminate a running external tool.

        Called directly rather than through a queued connection: this thread is
        blocked inside the child process, so a queued slot would not run until
        the work it is meant to interrupt had already finished — the same
        reasoning as SdrController.stop().
        """
        proc, self._process = self._process, None
        self._cancelled = True
        if proc is not None and proc.poll() is None:
            log.info("Cancelling the running OrcMaps operation")
            proc.terminate()


class OrcMapsController(QObject):
    """GUI-facing facade; owns the worker thread."""

    log = Signal(str)
    completed = Signal(str, bool, str)

    _verify_requested = Signal(object, object)
    _provision_requested = Signal(object, object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker = _OrcMapsWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        self._worker.log.connect(self.log)
        self._worker.completed.connect(self.completed)
        self._verify_requested.connect(self._worker.verify)
        self._provision_requested.connect(self._worker.provision)

        self._thread.start()

    def verify(self, tools: OrcMapsTools, directory: Path) -> None:
        self._verify_requested.emit(tools, directory)

    def provision(self, tools: OrcMapsTools, request: PinPackRequest) -> None:
        self._provision_requested.emit(tools, request)

    def cancel(self) -> None:
        self._worker.cancel()

    def shutdown(self) -> None:
        self.cancel()
        self._thread.quit()
        if not self._thread.wait(5000):
            # The child tool was terminated, so this should be unreachable; the
            # second wait keeps Qt from destroying a running thread if it isn't.
            log.warning("OrcMaps operation still unwinding; waiting for it")
            self._thread.wait()
