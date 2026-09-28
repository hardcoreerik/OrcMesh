"""The setup wizard: radio, then regions.

Follows the house dialog convention rather than inventing one — imperative
widgets, validation *after* ``exec()`` returns, and results handed back as a
plain params dict — so it reads like `device_page._cut_pack` and stays usable
from widget tests without a MainWindow.

The wizard deliberately owns no services. It is given a
:class:`~meshchat.services.provisioning.capability.SetupCapability` and emits
requests; ``MainWindow`` is what talks to the controller and OrcMaps. That keeps
the interesting decisions in one place and makes this dialog testable on its own.

Steps are ordered so the user is told what is possible before being asked to
choose anything:

1. **What can be done here** — the capability report, including why not, when
   something is missing.
2. **Your radio** — the device this setup is for.
3. **Your regions** — the areas to install, each with a size estimate, plus the
   destination they land in.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from meshchat.services.provisioning.capability import (
    MODE_UNAVAILABLE,
    SetupCapability,
    capability_summary,
)
from meshchat.services.provisioning.destinations import (
    default_map_dir,
    describe_destination,
)
from meshchat.services.provisioning.regions import (
    TIER_LABELS,
    TIER_MAX_ZOOM,
    estimate_region_bytes,
)

_STEP_TITLES = ("What can be done here", "Your radio", "Your regions")


class SetupWizard(QDialog):
    """A small linear wizard. Emits requests; MainWindow does the work."""

    serial_ports_requested = Signal()
    ble_scan_requested = Signal()
    regions_accepted = Signal(object)  # list of region params dicts

    def __init__(self, capability: SetupCapability, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up OrcMesh")
        self.setMinimumWidth(680)

        self._capability = capability
        self._regions: list[dict] = []
        self._destination = default_map_dir()

        layout = QVBoxLayout(self)
        self._step_label = QLabel()
        self._step_label.setStyleSheet("font-size: 15px; font-weight: 700;")
        layout.addWidget(self._step_label)

        self._stack = QStackedWidget()
        self._stack.addWidget(self._build_capability_page())
        self._stack.addWidget(self._build_radio_page())
        self._stack.addWidget(self._build_regions_page())
        layout.addWidget(self._stack, 1)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._back = self._buttons.addButton("Back", QDialogButtonBox.ButtonRole.ActionRole)
        self._ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        # The same button walks the steps and then finishes: a wizard with a
        # dead "Finish" on step 1 of 3 invites the user to leave early.
        self._ok.clicked.disconnect()
        self._ok.clicked.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)
        self._back.clicked.connect(self._go_back)
        layout.addWidget(self._buttons)

        self._show_step(0)

    # ── navigation ──────────────────────────────────────────────────────

    def _show_step(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        last = len(_STEP_TITLES) - 1
        self._step_label.setText(f"Step {index + 1} of {len(_STEP_TITLES)}: {_STEP_TITLES[index]}")
        self._ok.setText("Finish" if index == last else "Next")
        self._back.setEnabled(index > 0)
        self._ok.setEnabled(index != 0 or self._capability.mode != MODE_UNAVAILABLE)
        # Recompute the estimate whenever the region step comes up: the radius
        # and tier may have been left over from an earlier visit.
        if index == 2:
            self._update_estimate()

    def _go_back(self) -> None:
        self._show_step(max(0, self._stack.currentIndex() - 1))

    def _on_ok(self) -> None:
        index = self._stack.currentIndex()
        if index < len(_STEP_TITLES) - 1:
            self._show_step(index + 1)
            return
        self._on_finish()

    def _on_finish(self) -> None:
        """Collect what to install, then hand it to MainWindow."""
        if self._pending_region() is not None:
            self._add_current_region()
        self.regions_accepted.emit(list(self._regions))
        self.accept()

    # ── step 1: capability ──────────────────────────────────────────────

    def _build_capability_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self._capability_summary = QLabel(capability_summary(self._capability))
        self._capability_summary.setWordWrap(True)
        self._capability_summary.setStyleSheet("font-size: 13px;")
        layout.addWidget(self._capability_summary)

        self._capability_reasons = QLabel("\n\n".join(self._capability.reasons))
        self._capability_reasons.setWordWrap(True)
        self._capability_reasons.setStyleSheet("color: #7A8FBF;")
        layout.addWidget(self._capability_reasons)

        if self._capability.mode == MODE_UNAVAILABLE:
            note = QLabel(
                "Setup can be run again later from the Map menu once the missing "
                "pieces are in place."
            )
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ── step 2: radio ───────────────────────────────────────────────────

    def _build_radio_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "Choose the radio this setup is for. OrcMesh remembers it, so the "
            "next launch can connect without hunting for it."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        box = QGroupBox("Device")
        form = QFormLayout(box)

        self._transport = QComboBox()
        self._transport.addItems(["USB / Serial", "Bluetooth"])
        self._transport.currentIndexChanged.connect(self._on_transport_changed)
        form.addRow("Connection", self._transport)

        self._device = QComboBox()
        self._device.setMinimumWidth(320)
        form.addRow("Serial port", self._device)

        row = QHBoxLayout()
        self._refresh_btn = QPushButton("Refresh list")
        self._refresh_btn.clicked.connect(self._on_refresh)
        self._scan_btn = QPushButton("Scan for Bluetooth devices")
        self._scan_btn.clicked.connect(self.ble_scan_requested)
        self._scan_btn.setVisible(False)
        row.addWidget(self._refresh_btn)
        row.addWidget(self._scan_btn)
        row.addStretch(1)
        form.addRow("", row)

        self._radio_hint = QLabel("No devices found yet.")
        self._radio_hint.setWordWrap(True)
        self._radio_hint.setStyleSheet("color: #7A8FBF;")
        form.addRow("", self._radio_hint)

        layout.addWidget(box)
        layout.addStretch(1)
        return page

    def _on_transport_changed(self, index: int) -> None:
        bluetooth = index == 1
        self._scan_btn.setVisible(bluetooth)
        self._refresh_btn.setVisible(not bluetooth)
        self._device.clear()
        self._radio_hint.setText(
            "Press Scan — a Bluetooth scan takes about 10 seconds."
            if bluetooth
            else "No devices found yet."
        )
        self._on_refresh()

    def _on_refresh(self) -> None:
        if self._transport.currentIndex() == 0:
            self.serial_ports_requested.emit()
        else:
            self.ble_scan_requested.emit()

    def set_serial_ports(self, ports: list) -> None:
        """Fill the port list from ``serial_ports_found``."""
        if self._transport.currentIndex() != 0:
            return
        self._device.clear()
        for port in ports:
            label = f"{port.device} — {port.description}" if port.description else port.device
            self._device.addItem(label, userData=port.device)
        self._radio_hint.setText(
            f"{len(ports)} port(s) found."
            if ports
            else "No serial ports found. Is the radio plugged in?"
        )

    def set_ble_devices(self, devices: list) -> None:
        """Fill the device list from ``ble_scan_finished``."""
        self._device.clear()
        for device in devices:
            self._device.addItem(f"{device.name} ({device.address})", userData=device.address)
        self._radio_hint.setText(
            f"{len(devices)} device(s) found."
            if devices
            else "No Meshtastic radios found. They must be advertising, and "
                 "paired in Windows settings first."
        )

    def selected_device(self) -> tuple[str, str]:
        """(transport, target) — 'serial'/'ble' and the port or address."""
        transport = "ble" if self._transport.currentIndex() == 1 else "serial"
        return transport, str(self._device.currentData() or "")

    # ── step 3: regions ─────────────────────────────────────────────────

    def _build_regions_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        intro = QLabel(
            "Pick the areas to install. A region covers your county; local detail "
            "covers your town in more zoom. Each one shows an estimated size "
            "before it is built."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        entry = QGroupBox("Add a region")
        form = QFormLayout(entry)

        self._region_name = QLineEdit()
        self._region_name.setPlaceholderText("e.g. lane-county")
        self._region_name.setToolTip("Letters, digits, periods and hyphens only")
        form.addRow("Name", self._region_name)

        self._tier = QComboBox()
        for tier, label in TIER_LABELS.items():
            self._tier.addItem(label, userData=tier)
        self._tier.currentIndexChanged.connect(self._update_estimate)
        form.addRow("Detail level", self._tier)

        self._radius = QDoubleSpinBox()
        self._radius.setRange(0.5, 1000.0)
        self._radius.setDecimals(1)
        self._radius.setValue(25.0)
        self._radius.setSuffix(" km")
        self._radius.valueChanged.connect(self._update_estimate)
        form.addRow("Radius", self._radius)

        # A pack is cut around one point (OrcMaps' provision_pack --lat/--lon),
        # so a region needs a centre before it can be built at all. Prefilled
        # from the radio's own position when it has one.
        self._lat = QDoubleSpinBox()
        self._lat.setRange(-90.0, 90.0)
        self._lat.setDecimals(6)
        self._lat.valueChanged.connect(self._update_estimate)
        self._lon = QDoubleSpinBox()
        self._lon.setRange(-180.0, 180.0)
        self._lon.setDecimals(6)
        self._lon.valueChanged.connect(self._update_estimate)
        centre_row = QHBoxLayout()
        centre_row.addWidget(self._lat)
        centre_row.addWidget(self._lon)
        form.addRow("Centre (lat, lon)", centre_row)

        self._estimate_label = QLabel()
        self._estimate_label.setWordWrap(True)
        form.addRow("Estimated size", self._estimate_label)

        add_row = QHBoxLayout()
        add_btn = QPushButton("Add to list")
        add_btn.clicked.connect(self._add_current_region)
        add_row.addWidget(add_btn)
        add_row.addStretch(1)
        form.addRow("", add_row)

        layout.addWidget(entry)

        self._region_table = QTableWidget(0, 4)
        self._region_table.setHorizontalHeaderLabels(["Name", "Detail", "Estimated", "Status"])
        self._region_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._region_table.verticalHeader().setVisible(False)
        self._region_table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self._region_table, 1)

        dest = QGroupBox("Install to")
        dest_form = QFormLayout(dest)
        self._destination_label = QLabel(describe_destination(self._destination))
        self._destination_label.setWordWrap(True)
        choose = QPushButton("Choose folder…")
        choose.clicked.connect(self._choose_destination)
        dest_form.addRow("", self._destination_label)
        dest_form.addRow("", choose)
        layout.addWidget(dest)
        return page

    def _current_tier(self) -> str:
        return str(self._tier.currentData() or "region")

    def _pending_region(self) -> dict | None:
        """The region described by the form, or None when it is unusable."""
        name = self._region_name.text().strip()
        if not name:
            return None
        lat = float(self._lat.value())
        lon = float(self._lon.value())
        if lat == 0.0 and lon == 0.0:
            # (0, 0) is in the Gulf of Guinea: it means "nobody set this", and
            # cutting 25 km of ocean is worse than refusing the region.
            return None
        radius = float(self._radius.value())
        tier = self._current_tier()
        estimate = estimate_region_bytes(radius_km=radius, max_zoom=TIER_MAX_ZOOM[tier])
        return {
            "name": name,
            "tier": tier,
            "lat": lat,
            "lon": lon,
            "radius_km": radius,
            "max_zoom": TIER_MAX_ZOOM[tier],
            "estimate_bytes": estimate.expected,
            "estimate_basis": estimate.basis,
        }

    def _update_estimate(self) -> None:
        pending = self._pending_region()
        if pending is None:
            if not self._region_name.text().strip():
                self._estimate_label.setText(
                    "Give the region a name first (letters, digits, periods and hyphens)."
                )
            else:
                self._estimate_label.setText(
                    "Set the centre — from the radio's own position, or type the "
                    "coordinates of the place you want covered."
                )
            return
        estimate = estimate_region_bytes(
            radius_km=pending["radius_km"], max_zoom=pending["max_zoom"]
        )
        self._estimate_label.setText(f"{estimate.format_range()} — {estimate.basis}")

    def _add_current_region(self) -> None:
        pending = self._pending_region()
        if pending is None:
            self._update_estimate()
            return
        self._regions.append(pending)
        row = self._region_table.rowCount()
        self._region_table.insertRow(row)
        for column, text in enumerate((
            pending["name"],
            TIER_LABELS[pending["tier"]],
            f"{pending['estimate_bytes'] / 1e6:.0f} MB",
            "Not installed yet",
        )):
            self._region_table.setItem(row, column, QTableWidgetItem(text))
        # Clear the form so the next region starts blank. Without this, pressing
        # Finish while the form is still filled would add the same region twice.
        self._region_name.clear()
        self._update_estimate()

    def _choose_destination(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "Select a folder", str(self._destination))
        if not chosen:
            return
        self._destination = Path(chosen)
        self._destination_label.setText(describe_destination(self._destination))

    # ── results ─────────────────────────────────────────────────────────

    def regions(self) -> list[dict]:
        return list(self._regions)

    def capability(self) -> SetupCapability:
        """What this wizard was told is possible here.

        The caller needs it back to resolve which source pack serves each
        region's requested zoom — a decision the wizard deliberately does not
        make, since it does not touch packs.
        """
        return self._capability

    def destination(self) -> Path:
        return self._destination

    def set_destination(self, path: Path) -> None:
        """Seam for tests and for MainWindow, which knows the saved setting."""
        self._destination = path
        self._destination_label.setText(describe_destination(path))

    def set_centre(self, lat: float, lon: float) -> None:
        """Prefill the region centre — normally the radio's own position.

        Refuses (0, 0), which is what a radio with no fix reports: leaving the
        fields unset is honest, silently aiming the cut at the Gulf of Guinea
        is not.
        """
        if lat == 0.0 and lon == 0.0:
            return
        self._lat.setValue(lat)
        self._lon.setValue(lon)
        self._update_estimate()
