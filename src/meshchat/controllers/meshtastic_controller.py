"""MeshChat – MeshtasticController: connection state machine and Meshtastic integration."""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

from pubsub import pub
from PySide6.QtCore import QObject, QThread, Signal, Slot

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums and data models
# ---------------------------------------------------------------------------

class ConnectionState(Enum):
    DISCONNECTED = "disconnected"
    SCANNING = "scanning"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    DISCONNECTING = "disconnecting"
    ERROR = "error"


class ErrorCode(Enum):
    BLE_NOT_AVAILABLE = "ble_not_available"
    BLE_DEVICE_NOT_FOUND = "ble_device_not_found"
    BLE_PAIRING_REQUIRED = "ble_pairing_required"
    BLE_CONNECTION_FAILED = "ble_connection_failed"
    TCP_HOST_NOT_FOUND = "tcp_host_not_found"
    TCP_REFUSED = "tcp_refused"
    TCP_TIMEOUT = "tcp_timeout"
    SERIAL_PORT_NOT_FOUND = "serial_port_not_found"
    SERIAL_CONNECTION_FAILED = "serial_connection_failed"
    CONFIG_SYNC_TIMEOUT = "config_sync_timeout"
    CONNECTION_LOST = "connection_lost"
    SEND_FAILED = "send_failed"
    INVALID_MESSAGE = "invalid_message"
    INTERNAL_ERROR = "internal_error"


# Meshtastic's reserved "send to everyone" node number.
BROADCAST_NUM = 0xFFFFFFFF

#: Maximum UTF-8 payload we will hand to the radio for one text message.
#: Single-sourced: the composer's byte counter and both send paths read this,
#: so the limit shown to the user cannot drift from the one enforced.
MAX_MESSAGE_BYTES = 200


def normalize_outgoing_text(text: str) -> str:
    """Canonical form of a message, as it will actually be transmitted.

    Must be applied before *counting* bytes as well as before sending, or the
    composer measures something different from what the radio receives. It
    previously counted raw editor text while the send path normalised first,
    so 199 characters followed by a CRLF measured 201 bytes and the Send
    button greyed out on a message that was in fact 199 bytes and valid.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


class MessageDirection(Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    SYSTEM = "system"


class MessageStatus(Enum):
    RECEIVED = "received"
    SENDING = "sending"
    ACCEPTED_BY_RADIO = "accepted_by_radio"
    ACKNOWLEDGED = "acknowledged"
    FAILED = "failed"
    UNKNOWN_DELIVERY = "unknown_delivery"


@dataclass(frozen=True)
class BleDeviceSummary:
    name: str
    address: str


@dataclass(frozen=True)
class SerialPortSummary:
    device: str
    description: str


@dataclass(frozen=True)
class ChannelSummary:
    index: int
    name: str
    role: str


@dataclass(frozen=True)
class DeviceSummary:
    node_id: str | None
    long_name: str | None
    short_name: str | None
    hw_model: str | None
    firmware_version: str | None
    node_num: int | None = None


@dataclass(frozen=True)
class LoRaConfigSummary:
    region: str | None            # e.g. "US", "EU_868" — RegionCode enum name
    modem_preset: str | None      # e.g. "LONG_FAST" — ModemPreset enum name, only meaningful if use_preset
    use_preset: bool
    channel_num: int
    frequency_offset_mhz: float
    bandwidth_khz: int
    spread_factor: int
    coding_rate: int
    tx_power_dbm: int
    hop_limit: int


@dataclass(frozen=True)
class ChatMessage:
    local_id: str
    packet_id: int | None
    channel_index: int
    direction: MessageDirection
    sender_num: int | None
    sender_id: str | None
    sender_name: str
    text: str
    timestamp: datetime
    status: MessageStatus
    snr: float | None = None
    hop_count: int | None = None
    # Set to the conversation partner's node_num for a direct message
    # (regardless of inbound/outbound direction); None for a channel/broadcast message.
    destination_num: int | None = None
    # Which app run (NetworkSession) this message belongs to — one id for
    # the whole app lifetime, constant across reconnects (see
    # ARCHITECTURE.md). Defaults to "" here since the controller/worker
    # that constructs inbound messages doesn't know about NetworkSession
    # (that's a MainWindow-level concept) — MainWindow stamps the real id
    # on before persisting.
    session_id: str = ""


@dataclass(frozen=True)
class UserFacingError:
    code: ErrorCode
    title: str
    message: str
    technical_detail: str | None
    recoverable: bool


# ---------------------------------------------------------------------------
# BLE error classification
# ---------------------------------------------------------------------------

_PAIRING_ERROR_MARKERS = (
    "insufficient authentication",
    "insufficient encryption",
    "not paired",
    "access is denied",
)


def _is_pairing_error(exc: BaseException) -> bool:
    """True if exc (or any cause in its chain) indicates the OS hasn't
    completed Bluetooth pairing with the device — the fix is to pair in
    Windows Settings, not to retry the connection."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = str(current).lower()
        if any(marker in text for marker in _PAIRING_ERROR_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


# ---------------------------------------------------------------------------
# Channel extraction helper
# ---------------------------------------------------------------------------

def _extract_channels(interface) -> list[ChannelSummary]:
    result: list[ChannelSummary] = []
    try:
        from meshtastic.protobuf import channel_pb2
        local_node = getattr(interface, "localNode", None)
        channels = getattr(local_node, "channels", None) or []
        for ch in channels:
            try:
                role_name = channel_pb2.Channel.Role.Name(ch.role)
            except Exception:
                role_name = "UNKNOWN"
            if role_name == "DISABLED":
                continue
            name = (ch.settings.name or "").strip() if hasattr(ch, "settings") else ""
            if not name:
                name = "Primary" if role_name == "PRIMARY" else f"Channel {ch.index}"
            result.append(ChannelSummary(index=int(ch.index), name=name, role=role_name))
    except Exception as exc:
        log.warning("Channel extraction failed: %s", exc)
    return sorted(result, key=lambda c: c.index)


def _device_summary(interface) -> DeviceSummary:
    try:
        my_info = getattr(interface, "myInfo", None) or {}
        metadata = getattr(interface, "metadata", None)
        fw = getattr(metadata, "firmware_version", None) if metadata else None
        nodes_by_num = getattr(interface, "nodesByNum", {}) or {}
        local_num = getattr(my_info, "my_node_num", None)
        node_info = nodes_by_num.get(local_num, {}) if local_num else {}
        user = node_info.get("user", {}) if isinstance(node_info, dict) else {}
        return DeviceSummary(
            node_id=user.get("id") or (f"!{local_num:08x}" if local_num else None),
            long_name=user.get("longName"),
            short_name=user.get("shortName"),
            hw_model=user.get("hwModel"),
            firmware_version=fw,
            node_num=local_num,
        )
    except Exception as exc:
        log.warning("DeviceSummary extraction failed: %s", exc)
        return DeviceSummary(None, None, None, None, None, None)


def _extract_lora_config(interface) -> LoRaConfigSummary | None:
    try:
        from meshtastic.protobuf import config_pb2

        local_node = getattr(interface, "localNode", None)
        local_config = getattr(local_node, "localConfig", None)
        lora = getattr(local_config, "lora", None)
        if lora is None:
            return None

        try:
            region_name = config_pb2.Config.LoRaConfig.RegionCode.Name(lora.region)
        except (ValueError, TypeError):
            region_name = None
        if region_name == "UNSET":
            region_name = None

        try:
            preset_name = config_pb2.Config.LoRaConfig.ModemPreset.Name(lora.modem_preset)
        except (ValueError, TypeError):
            preset_name = None

        return LoRaConfigSummary(
            region=region_name,
            modem_preset=preset_name,
            use_preset=bool(lora.use_preset),
            channel_num=lora.channel_num,
            frequency_offset_mhz=lora.frequency_offset,
            bandwidth_khz=lora.bandwidth,
            spread_factor=lora.spread_factor,
            coding_rate=lora.coding_rate,
            tx_power_dbm=lora.tx_power,
            hop_limit=lora.hop_limit,
        )
    except Exception as exc:
        log.warning("LoRaConfigSummary extraction failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Worker (lives on QThread)
# ---------------------------------------------------------------------------

class MeshtasticWorker(QObject):
    """All blocking Meshtastic operations run here, never on the GUI thread."""

    # Outbound signals → GUI thread
    connection_state_changed = Signal(object, str)   # (ConnectionState, detail)
    connected = Signal(object)                        # DeviceSummary
    disconnected = Signal(str)
    channels_updated = Signal(list)                   # list[ChannelSummary]
    lora_config_updated = Signal(object)              # LoRaConfigSummary | None
    message_received = Signal(object)                 # ChatMessage
    message_status_changed = Signal(int, object, str) # (packet_id, MessageStatus, detail)
    node_updated = Signal(dict)                       # raw node dict
    # object, not int: Meshtastic node_num is a 32-bit *unsigned* value and
    # can exceed 0x7FFFFFFF — a Qt-typed int signal/slot is C++ int32 and
    # silently wraps it to negative (verified across Q_ARG(int, ...) queued
    # invocation into the worker thread, same failure mode as the map-pin
    # chain — see ARCHITECTURE.md).
    node_action_completed = Signal(object, str, str)  # (node_num, action, human-readable result)
    nodedb_synced = Signal(list)                       # list[dict] — full NodeDB right after connect
    ble_scan_started = Signal()
    ble_scan_finished = Signal(list)                  # list[BleDeviceSummary]
    serial_ports_found = Signal(list)                  # list[SerialPortSummary]
    error_occurred = Signal(object)                   # UserFacingError
    diagnostic_log = Signal(str)
    raw_packet = Signal(dict)                         # for monitor ingestion

    def __init__(self, parent=None):
        super().__init__(parent)
        self._interface = None
        self._state = ConnectionState.DISCONNECTED
        self._subscribed = False

    # -----------------------------------------------------------------------
    # State helpers
    # -----------------------------------------------------------------------

    def _set_state(self, state: ConnectionState, detail: str = "") -> None:
        self._state = state
        self.connection_state_changed.emit(state, detail)
        log.info("Connection state → %s  %s", state.value, detail)

    def _is_active_interface(self, event_interface) -> bool:
        return (
            self._interface is not None
            and event_interface is self._interface
        )

    # -----------------------------------------------------------------------
    # PubSub subscriptions
    # -----------------------------------------------------------------------

    def _subscriptions(self) -> tuple[tuple, ...]:
        """(callback, topic) pairs. Single source of truth so subscribe and
        unsubscribe can't drift apart and orphan a listener."""
        return (
            (self._on_text_received, "meshtastic.receive.text"),
            (self._on_packet_received, "meshtastic.receive"),
            (self._on_connection_established, "meshtastic.connection.established"),
            (self._on_connection_lost, "meshtastic.connection.lost"),
            (self._on_node_updated, "meshtastic.node.updated"),
        )

    def _subscribe(self) -> None:
        if self._subscribed:
            return
        for callback, topic in self._subscriptions():
            pub.subscribe(callback, topic)
        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        # Each topic gets its own try/except: a single shared one meant that a
        # failure on the first topic silently skipped the remaining four,
        # leaking those callbacks into the next connection.
        for callback, topic in self._subscriptions():
            try:
                pub.unsubscribe(callback, topic)
            except Exception as exc:
                # Never let teardown raise, but don't discard it silently.
                log.warning("PubSub unsubscribe failed for %s: %s", topic, exc)

        # Deliberately cleared even if an unsubscribe failed. Leaving this True
        # to allow a retry would make _subscribe() short-circuit on the next
        # connect, so the app would reconnect with no callbacks at all and
        # receive nothing — far worse than a leaked callback. PyPubSub also
        # de-duplicates identical listeners, so re-subscribing is harmless.
        self._subscribed = False

    # -----------------------------------------------------------------------
    # PubSub callbacks  (MUST NOT touch widgets; emit Qt signals instead)
    # -----------------------------------------------------------------------

    def _on_connection_established(self, interface=None, topics=None) -> None:
        if not self._is_active_interface(interface):
            return
        try:
            summary = _device_summary(interface)
            channels = _extract_channels(interface)
            lora_config = _extract_lora_config(interface)
            self._set_state(ConnectionState.CONNECTED, summary.long_name or "")
            self.connected.emit(summary)
            self.channels_updated.emit(channels)
            self.lora_config_updated.emit(lora_config)

            # The meshtastic library has already downloaded the radio's full
            # NodeDB by the time this event fires — push it out so the UI can
            # populate names/positions for nodes we haven't heard live packets
            # from yet this session, instead of waiting for them to re-announce.
            nodes_by_num = getattr(interface, "nodesByNum", {}) or {}
            if nodes_by_num:
                with_pos = sum(
                    1 for n in nodes_by_num.values()
                    if isinstance(n, dict) and (n.get("position") or {}).get("latitude") is not None
                )
                log.info(
                    "NodeDB sync: %d node(s) known, %d with a position",
                    len(nodes_by_num), with_pos,
                )
                self.nodedb_synced.emit(list(nodes_by_num.values()))
        except Exception as exc:
            log.exception("Error in connection established handler")
            self._emit_error(ErrorCode.INTERNAL_ERROR, "Connection setup failed", str(exc), True)

    def _on_connection_lost(self, interface=None, topics=None) -> None:
        if not self._is_active_interface(interface):
            return
        self._set_state(ConnectionState.ERROR, "Connection lost")
        self.disconnected.emit("Radio connection lost")
        self._emit_error(
            ErrorCode.CONNECTION_LOST,
            "Connection Lost",
            "The radio connection was lost. You can try reconnecting.",
            True,
        )

    def _on_text_received(self, packet=None, interface=None, topics=None) -> None:
        if not self._is_active_interface(interface):
            return
        # Text packets are also broadcast on meshtastic.receive; emit raw_packet only once
        # from _on_packet_received to avoid double ingestion.
        try:
            if not isinstance(packet, dict):
                return
            decoded = packet.get("decoded", {}) or {}
            text = decoded.get("text")
            if not isinstance(text, str) or not text.strip():
                payload = decoded.get("payload")
                if isinstance(payload, bytes):
                    try:
                        text = payload.decode("utf-8")
                    except UnicodeDecodeError:
                        return
            if not text:
                return

            # Resolve sender name
            sender_num = packet.get("from") or packet.get("fromNum")
            sender_id = packet.get("fromId")
            nodes_by_num = getattr(interface, "nodesByNum", {}) or {}
            sender_name = "Unknown"
            if sender_num and sender_num in nodes_by_num:
                user = nodes_by_num[sender_num].get("user", {})
                sender_name = user.get("longName") or user.get("shortName") or sender_id or f"!{sender_num:08x}"
            elif sender_id:
                sender_name = sender_id

            rx_ts = packet.get("rxTime")
            if rx_ts:
                ts = datetime.fromtimestamp(float(rx_ts), tz=timezone.utc)
            else:
                ts = datetime.now(timezone.utc)

            # A packet addressed to us specifically (not the broadcast address)
            # is a direct message; group it under the sender's conversation.
            to_num = packet.get("to") or packet.get("toNum")
            destination_num = sender_num if to_num not in (None, BROADCAST_NUM) else None

            msg = ChatMessage(
                local_id=str(uuid.uuid4()),
                packet_id=packet.get("id"),
                channel_index=packet.get("channel", 0),
                direction=MessageDirection.INBOUND,
                sender_num=sender_num,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                timestamp=ts,
                status=MessageStatus.RECEIVED,
                snr=packet.get("rxSnr"),
                hop_count=None,
                destination_num=destination_num,
            )
            self.message_received.emit(msg)
        except Exception as exc:
            log.exception("Error processing received text: %s", exc)

    def _on_packet_received(self, packet=None, interface=None, topics=None) -> None:
        if not self._is_active_interface(interface):
            return
        if isinstance(packet, dict):
            self.raw_packet.emit(packet)

    def _on_node_updated(self, node=None, interface=None, topics=None) -> None:
        if not self._is_active_interface(interface):
            return
        if isinstance(node, dict):
            self.node_updated.emit(node)

    # -----------------------------------------------------------------------
    # Slot: BLE scan
    # -----------------------------------------------------------------------

    @Slot()
    def scan_ble(self) -> None:
        self._set_state(ConnectionState.SCANNING, "Scanning for Meshtastic devices…")
        self.ble_scan_started.emit()
        try:
            from meshtastic.ble_interface import BLEInterface
            raw_devices = BLEInterface.scan()
            seen: set[str] = set()
            devices: list[BleDeviceSummary] = []
            for d in raw_devices:
                addr = getattr(d, "address", None) or str(d)
                if addr in seen:
                    continue
                seen.add(addr)
                name = getattr(d, "name", None) or ""
                if not name:
                    name = "Unnamed Meshtastic device"
                devices.append(BleDeviceSummary(name=name, address=addr))
            devices.sort(key=lambda x: (not bool(x.name), x.name, x.address))
            self.ble_scan_finished.emit(devices)
            self._set_state(ConnectionState.DISCONNECTED, f"{len(devices)} device(s) found")
        except Exception as exc:
            log.exception("BLE scan failed")
            self._set_state(ConnectionState.DISCONNECTED, "Scan failed")
            self._emit_error(
                ErrorCode.BLE_NOT_AVAILABLE,
                "Bluetooth Scan Failed",
                f"Could not scan for BLE devices: {exc}",
                True,
            )

    # -----------------------------------------------------------------------
    # Slot: BLE connect
    # -----------------------------------------------------------------------

    @Slot(str)
    def connect_ble(self, address: str) -> None:
        if self._state not in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            return
        self._close_interface()
        self._subscribe()
        self._set_state(ConnectionState.CONNECTING, address)
        try:
            from meshtastic.ble_interface import BLEInterface
            self._interface = BLEInterface(address=address, timeout=45)
            self._set_state(ConnectionState.SYNCING, "Downloading radio configuration…")
        except Exception as exc:
            log.exception("BLE connect failed")
            self._interface = None
            self._set_state(ConnectionState.ERROR, str(exc))
            self._set_state(ConnectionState.DISCONNECTED)
            if _is_pairing_error(exc):
                self._emit_error(
                    ErrorCode.BLE_PAIRING_REQUIRED,
                    "Bluetooth Pairing Required",
                    (
                        f"Windows has not paired with this radio yet ({address}). "
                        "Pair it in Windows Bluetooth settings, then try connecting again."
                    ),
                    True,
                    str(exc),
                )
            else:
                self._emit_error(
                    ErrorCode.BLE_CONNECTION_FAILED,
                    "Bluetooth Connection Failed",
                    (
                        "Could not connect to the Meshtastic radio. "
                        "Make sure the radio is awake, Bluetooth is enabled, "
                        "and any Windows pairing prompt has been completed."
                    ),
                    True,
                    str(exc),
                )

    # -----------------------------------------------------------------------
    # Slot: TCP connect
    # -----------------------------------------------------------------------

    @Slot(str, int)
    def connect_tcp(self, host: str, port: int = 4403) -> None:
        if self._state not in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            return
        host = host.strip()
        if not host:
            self._emit_error(ErrorCode.TCP_HOST_NOT_FOUND, "No Host", "Enter a hostname or IP address.", False)
            return
        if host.startswith(("http://", "https://")):
            self._emit_error(ErrorCode.TCP_HOST_NOT_FOUND, "Invalid Host", "Enter a hostname or IP address, not a URL.", False)
            return
        if not (1 <= port <= 65535):
            self._emit_error(ErrorCode.TCP_HOST_NOT_FOUND, "Invalid Port", "Port must be between 1 and 65535.", False)
            return

        self._close_interface()
        self._subscribe()
        self._set_state(ConnectionState.CONNECTING, f"{host}:{port}")
        try:
            from meshtastic.tcp_interface import TCPInterface
            self._interface = TCPInterface(hostname=host, portNumber=port, timeout=45)
            self._set_state(ConnectionState.SYNCING, "Downloading radio configuration…")
        except OSError as exc:
            log.exception("TCP connect failed: %s", exc)
            detail = str(exc)
            code = ErrorCode.TCP_TIMEOUT
            title = "TCP Connection Failed"
            msg = f"Could not connect to {host}:{port}."
            if "refused" in detail.lower():
                code = ErrorCode.TCP_REFUSED
                msg = f"Connection refused at {host}:{port}. Check that the radio's TCP service is running."
            elif "timed out" in detail.lower() or "timeout" in detail.lower():
                msg = f"Connection to {host}:{port} timed out. Check that the radio is reachable."
            elif "name or service" in detail.lower() or "nodename" in detail.lower():
                code = ErrorCode.TCP_HOST_NOT_FOUND
                msg = f"Host not found: {host}. Try an IP address if .local resolution is unavailable."
            self._interface = None
            self._set_state(ConnectionState.ERROR, detail)
            self._set_state(ConnectionState.DISCONNECTED)
            self._emit_error(code, title, msg, True, detail)
        except Exception as exc:
            log.exception("TCP connect unexpected error")
            self._interface = None
            self._set_state(ConnectionState.ERROR, str(exc))
            self._set_state(ConnectionState.DISCONNECTED)
            self._emit_error(ErrorCode.INTERNAL_ERROR, "Connection Error", str(exc), True)

    # -----------------------------------------------------------------------
    # Slot: Serial (USB) port listing
    # -----------------------------------------------------------------------

    @Slot()
    def list_serial_ports(self) -> None:
        try:
            from serial.tools import list_ports
            ports = [
                SerialPortSummary(device=p.device, description=p.description or p.device)
                for p in list_ports.comports()
            ]
            ports.sort(key=lambda p: p.device)
            self.serial_ports_found.emit(ports)
        except Exception:
            log.exception("Serial port enumeration failed")
            self.serial_ports_found.emit([])

    # -----------------------------------------------------------------------
    # Slot: Serial (USB) connect
    # -----------------------------------------------------------------------

    @Slot(str)
    def connect_serial(self, port: str) -> None:
        if self._state not in (ConnectionState.DISCONNECTED, ConnectionState.ERROR):
            return
        port = port.strip()
        if not port:
            self._emit_error(
                ErrorCode.SERIAL_PORT_NOT_FOUND, "No Port Selected",
                "Choose a serial (COM) port first.", False,
            )
            return

        self._close_interface()
        self._subscribe()
        self._set_state(ConnectionState.CONNECTING, port)
        try:
            from meshtastic.serial_interface import SerialInterface
            self._interface = SerialInterface(devPath=port, timeout=45)
            self._set_state(ConnectionState.SYNCING, "Downloading radio configuration…")
        except Exception as exc:
            log.exception("Serial connect failed")
            self._interface = None
            self._set_state(ConnectionState.ERROR, str(exc))
            self._set_state(ConnectionState.DISCONNECTED)
            self._emit_error(
                ErrorCode.SERIAL_CONNECTION_FAILED,
                "USB Connection Failed",
                (
                    f"Could not connect to the radio on {port}. "
                    "Make sure the device is plugged in, no other program "
                    "(e.g. the Meshtastic web client) has the port open, "
                    "and the correct port is selected."
                ),
                True,
                str(exc),
            )

    # -----------------------------------------------------------------------
    # Slot: Disconnect
    # -----------------------------------------------------------------------

    @Slot()
    def disconnect(self) -> None:
        if self._state == ConnectionState.DISCONNECTED:
            return
        self._set_state(ConnectionState.DISCONNECTING, "Disconnecting…")
        self._close_interface()
        self._set_state(ConnectionState.DISCONNECTED)
        self.disconnected.emit("Disconnected by user")

    # -----------------------------------------------------------------------
    # Slot: Send text
    # -----------------------------------------------------------------------

    def _prepare_outgoing(self, text: str) -> str | None:
        """Validate and normalise outgoing text.

        Returns the text to send, or None if it must not be sent (an error has
        already been emitted where one is warranted). Shared by both send
        paths so the rules cannot diverge between broadcast and direct.
        """
        if self._state != ConnectionState.CONNECTED or self._interface is None:
            self._emit_error(
                ErrorCode.SEND_FAILED, "Not Connected",
                "Cannot send: not connected to a radio.", False,
            )
            return None

        text = normalize_outgoing_text(text)
        if not text:
            return None  # empty input is a no-op, not an error worth surfacing

        byte_len = len(text.encode("utf-8"))
        if byte_len > MAX_MESSAGE_BYTES:
            self._emit_error(
                ErrorCode.INVALID_MESSAGE,
                "Message Too Long",
                f"Message is {byte_len} UTF-8 bytes. The safety limit is "
                f"{MAX_MESSAGE_BYTES} bytes. Shorten the message.",
                False,
            )
            return None
        return text

    def _dispatch_text(self, text: str, **send_kwargs) -> None:
        """Hand a validated message to the radio and report the outcome."""
        try:
            packet = self._interface.sendText(text=text, wantAck=True, **send_kwargs)
            self.message_status_changed.emit(
                getattr(packet, "id", None) or 0,
                MessageStatus.ACCEPTED_BY_RADIO,
                "Accepted by radio",
            )
        except Exception as exc:
            log.exception("Send failed")
            self._emit_error(
                ErrorCode.SEND_FAILED, "Send Failed",
                f"Could not send message: {exc}", True,
            )

    @Slot(str, int)
    def send_channel_text(self, text: str, channel_index: int) -> None:
        prepared = self._prepare_outgoing(text)
        if prepared is None:
            return
        self._dispatch_text(prepared, destinationId="^all", channelIndex=channel_index)

    # -----------------------------------------------------------------------
    # Slot: Send direct message
    # -----------------------------------------------------------------------

    @Slot(str, "qlonglong")
    def send_direct_text(self, text: str, destination_num: int) -> None:
        prepared = self._prepare_outgoing(text)
        if prepared is None:
            return
        self._dispatch_text(prepared, destinationId=destination_num)

    # -----------------------------------------------------------------------
    # Slots: per-node actions (right-click menu on the Nodes page)
    # -----------------------------------------------------------------------

    def _require_connection(self, what: str) -> bool:
        if self._state != ConnectionState.CONNECTED or self._interface is None:
            self._emit_error(
                ErrorCode.SEND_FAILED, "Not Connected",
                f"Cannot {what}: not connected to a radio.", False,
            )
            return False
        return True

    @Slot("qlonglong")
    def request_position(self, node_num: int) -> None:
        if not self._require_connection("request position"):
            return
        try:
            self._interface.sendPosition(destinationId=node_num, wantResponse=True)
            self.node_action_completed.emit(node_num, "position", "Position request sent")
        except Exception as exc:
            log.exception("Position request failed")
            self._emit_error(ErrorCode.SEND_FAILED, "Request Failed",
                             f"Could not request position: {exc}", True)

    @Slot("qlonglong")
    def request_telemetry(self, node_num: int) -> None:
        if not self._require_connection("request telemetry"):
            return
        try:
            self._interface.sendTelemetry(destinationId=node_num, wantResponse=True)
            self.node_action_completed.emit(node_num, "telemetry", "Telemetry request sent")
        except Exception as exc:
            log.exception("Telemetry request failed")
            self._emit_error(ErrorCode.SEND_FAILED, "Request Failed",
                             f"Could not request telemetry: {exc}", True)

    @Slot("qlonglong", int)
    def send_traceroute(self, node_num: int, hop_limit: int = 7) -> None:
        # sendTraceRoute blocks waiting for the reply to come back through the
        # mesh, which is why this must stay on the worker thread.
        if not self._require_connection("run traceroute"):
            return
        try:
            self._interface.sendTraceRoute(dest=node_num, hopLimit=hop_limit)
            self.node_action_completed.emit(node_num, "traceroute", "Traceroute complete — see packet log")
        except Exception as exc:
            log.exception("Traceroute failed")
            self._emit_error(ErrorCode.SEND_FAILED, "Traceroute Failed",
                             f"Traceroute did not complete: {exc}", True)

    @Slot("qlonglong", bool)
    def set_favorite(self, node_num: int, favorite: bool) -> None:
        if not self._require_connection("change favorite"):
            return
        try:
            local_node = self._interface.localNode
            if favorite:
                local_node.setFavorite(node_num)
                self.node_action_completed.emit(node_num, "favorite", "Marked as favorite on the radio")
            else:
                local_node.removeFavorite(node_num)
                self.node_action_completed.emit(node_num, "favorite", "Removed from favorites on the radio")
        except Exception as exc:
            log.exception("Favorite change failed")
            self._emit_error(ErrorCode.SEND_FAILED, "Action Failed",
                             f"Could not change favorite: {exc}", True)

    @Slot("qlonglong")
    def remove_node(self, node_num: int) -> None:
        if not self._require_connection("remove node"):
            return
        try:
            self._interface.localNode.removeNode(node_num)
            self.node_action_completed.emit(node_num, "remove", "Node removed from the radio's NodeDB")
        except Exception as exc:
            log.exception("Node removal failed")
            self._emit_error(ErrorCode.SEND_FAILED, "Action Failed",
                             f"Could not remove node: {exc}", True)

    # -----------------------------------------------------------------------
    # Slot: Shutdown
    # -----------------------------------------------------------------------

    @Slot()
    def shutdown(self) -> None:
        self._close_interface()
        self._unsubscribe()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _close_interface(self) -> None:
        iface = self._interface
        self._interface = None
        if iface is not None:
            try:
                iface.close()
            except Exception as exc:
                log.warning("Closing the radio interface failed: %s", exc)

    def _emit_error(
        self,
        code: ErrorCode,
        title: str,
        message: str,
        recoverable: bool,
        technical_detail: str | None = None,
    ) -> None:
        err = UserFacingError(code=code, title=title, message=message,
                              technical_detail=technical_detail, recoverable=recoverable)
        self.error_occurred.emit(err)
        log.error("[%s] %s — %s", code.value, title, technical_detail or message)


# ---------------------------------------------------------------------------
# Controller facade (created on GUI thread, manages worker thread)
# ---------------------------------------------------------------------------

class MeshtasticController(QObject):
    """
    Public interface for the GUI.  All signals forwarded from the worker.
    """
    connection_state_changed = Signal(object, str)
    connected = Signal(object)
    disconnected = Signal(str)
    channels_updated = Signal(list)
    lora_config_updated = Signal(object)
    message_received = Signal(object)
    message_status_changed = Signal(int, object, str)
    node_updated = Signal(dict)
    node_action_completed = Signal(object, str, str)  # object: see MeshtasticWorker's matching signal
    nodedb_synced = Signal(list)
    ble_scan_started = Signal()
    ble_scan_finished = Signal(list)
    serial_ports_found = Signal(list)
    error_occurred = Signal(object)
    diagnostic_log = Signal(str)
    raw_packet = Signal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = MeshtasticWorker()
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)

        # Forward all signals
        w = self._worker
        w.connection_state_changed.connect(self.connection_state_changed)
        w.connected.connect(self.connected)
        w.disconnected.connect(self.disconnected)
        w.channels_updated.connect(self.channels_updated)
        w.lora_config_updated.connect(self.lora_config_updated)
        w.message_received.connect(self.message_received)
        w.message_status_changed.connect(self.message_status_changed)
        w.node_updated.connect(self.node_updated)
        w.node_action_completed.connect(self.node_action_completed)
        w.nodedb_synced.connect(self.nodedb_synced)
        w.ble_scan_started.connect(self.ble_scan_started)
        w.ble_scan_finished.connect(self.ble_scan_finished)
        w.serial_ports_found.connect(self.serial_ports_found)
        w.error_occurred.connect(self.error_occurred)
        w.diagnostic_log.connect(self.diagnostic_log)
        w.raw_packet.connect(self.raw_packet)

        self._thread.start()

    # Slots delegated to worker via queued invocation
    def scan_ble(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._worker, "scan_ble", Qt.ConnectionType.QueuedConnection)

    def connect_ble(self, address: str) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "connect_ble",
                                 Qt.ConnectionType.QueuedConnection, Q_ARG(str, address))

    def connect_tcp(self, host: str, port: int = 4403) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "connect_tcp",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG(str, host), Q_ARG(int, port))

    def list_serial_ports(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._worker, "list_serial_ports", Qt.ConnectionType.QueuedConnection)

    def connect_serial(self, port: str) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "connect_serial",
                                 Qt.ConnectionType.QueuedConnection, Q_ARG(str, port))

    def disconnect(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._worker, "disconnect", Qt.ConnectionType.QueuedConnection)

    def send_channel_text(self, text: str, channel_index: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "send_channel_text",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG(str, text), Q_ARG(int, channel_index))

    def send_direct_text(self, text: str, destination_num: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "send_direct_text",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG(str, text), Q_ARG("qlonglong", destination_num))

    def request_position(self, node_num: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "request_position",
                                 Qt.ConnectionType.QueuedConnection, Q_ARG("qlonglong", node_num))

    def request_telemetry(self, node_num: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "request_telemetry",
                                 Qt.ConnectionType.QueuedConnection, Q_ARG("qlonglong", node_num))

    def send_traceroute(self, node_num: int, hop_limit: int = 7) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "send_traceroute",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG("qlonglong", node_num), Q_ARG(int, hop_limit))

    def set_favorite(self, node_num: int, favorite: bool) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "set_favorite",
                                 Qt.ConnectionType.QueuedConnection,
                                 Q_ARG("qlonglong", node_num), Q_ARG(bool, favorite))

    def remove_node(self, node_num: int) -> None:
        from PySide6.QtCore import QMetaObject, Qt, Q_ARG
        QMetaObject.invokeMethod(self._worker, "remove_node",
                                 Qt.ConnectionType.QueuedConnection, Q_ARG("qlonglong", node_num))

    def shutdown(self) -> None:
        from PySide6.QtCore import QMetaObject, Qt
        QMetaObject.invokeMethod(self._worker, "shutdown", Qt.ConnectionType.QueuedConnection)
        self._thread.quit()
        self._thread.wait(5000)
