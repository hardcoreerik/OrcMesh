"""MeshChat – Deep-Space Holographic theme stylesheet and palette."""
from __future__ import annotations

# ── Palette ────────────────────────────────────────────────────────────────
BG_DEEP    = "#070D1F"   # main window / panel background
BG_CARD    = "#0D1530"   # card / sidebar background
BG_HOVER   = "#131C3A"   # hover state
BG_BORDER  = "#1A2448"   # subtle border
BG_INPUT   = "#0A1025"   # input fields

ACCENT_CYAN   = "#00D4FF"   # primary accent
ACCENT_PURPLE = "#8B5CF6"   # secondary accent / identity
ACCENT_GREEN  = "#00FF88"   # healthy / recent
ACCENT_AMBER  = "#FFB800"   # warning / stale
ACCENT_RED    = "#FF4060"   # error / exhausted
ACCENT_BLUE   = "#4F9EFF"   # informational / telemetry
ACCENT_GRAY   = "#5A6690"   # unknown / unavailable

TEXT_PRIMARY   = "#C8D8FF"
TEXT_SECONDARY = "#7A8FBF"
TEXT_DIM       = "#3A4870"
TEXT_BRIGHT    = "#FFFFFF"

SCROLLBAR_BG     = "#0D1530"
SCROLLBAR_HANDLE = "#2A3B6A"


def global_stylesheet() -> str:
    return f"""
/* ── Global ─────────────────────────────────────────────────── */
QMainWindow, QWidget {{
    background-color: {BG_DEEP};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", Consolas, sans-serif;
    font-size: 13px;
}}

/* ── Nav Rail ───────────────────────────────────────────────── */
#navRail {{
    background-color: {BG_CARD};
    border-right: 1px solid {BG_BORDER};
    min-width: 56px;
    max-width: 56px;
}}
#navRail QPushButton {{
    background: transparent;
    border: none;
    border-radius: 10px;
    color: {TEXT_SECONDARY};
    font-size: 10px;
    padding: 6px 4px;
    margin: 2px 6px;
    text-align: center;
}}
#navRail QPushButton:hover {{
    background-color: {BG_HOVER};
    color: {ACCENT_CYAN};
}}
#navRail QPushButton:checked {{
    background-color: rgba(0, 212, 255, 0.12);
    color: {ACCENT_CYAN};
    border-left: 2px solid {ACCENT_CYAN};
    border-radius: 0px 8px 8px 0px;
    margin-left: 0px;
    padding-left: 10px;
}}

/* ── Connection Bar ─────────────────────────────────────────── */
#connectionBar {{
    background-color: {BG_CARD};
    border-bottom: 1px solid {BG_BORDER};
    min-height: 44px;
    max-height: 44px;
    padding: 0 10px;
}}
#connectionBar QComboBox, #connectionBar QLineEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BG_BORDER};
    border-radius: 5px;
    color: {TEXT_PRIMARY};
    padding: 3px 8px;
    selection-background-color: {ACCENT_CYAN};
    selection-color: {BG_DEEP};
}}
#connectionBar QComboBox::drop-down {{
    border: none;
    width: 20px;
}}
#connectionBar QComboBox QAbstractItemView {{
    background-color: {BG_CARD};
    border: 1px solid {ACCENT_CYAN};
    selection-background-color: {ACCENT_CYAN};
    selection-color: {BG_DEEP};
}}

/* ── Buttons ────────────────────────────────────────────────── */
QPushButton {{
    background-color: rgba(0, 212, 255, 0.08);
    border: 1px solid {ACCENT_CYAN};
    border-radius: 5px;
    color: {ACCENT_CYAN};
    padding: 4px 12px;
    font-weight: 500;
}}
QPushButton:hover {{
    background-color: rgba(0, 212, 255, 0.18);
}}
QPushButton:pressed {{
    background-color: rgba(0, 212, 255, 0.30);
}}
QPushButton:disabled {{
    border-color: {BG_BORDER};
    color: {TEXT_DIM};
    background-color: transparent;
}}
QPushButton#sendBtn {{
    background-color: {ACCENT_CYAN};
    color: {BG_DEEP};
    font-weight: 700;
    border: none;
    border-radius: 5px;
    padding: 6px 18px;
    min-width: 70px;
}}
QPushButton#sendBtn:hover {{
    background-color: #33DFFF;
}}
QPushButton#sendBtn:disabled {{
    background-color: {BG_BORDER};
    color: {TEXT_DIM};
}}
QPushButton#dangerBtn {{
    border-color: {ACCENT_RED};
    color: {ACCENT_RED};
}}
QPushButton#dangerBtn:hover {{
    background-color: rgba(255, 64, 96, 0.18);
}}

/* ── Status indicator ───────────────────────────────────────── */
#statusDot[state="connected"] {{ color: {ACCENT_GREEN}; }}
#statusDot[state="connecting"] {{ color: {ACCENT_AMBER}; }}
#statusDot[state="disconnected"] {{ color: {ACCENT_GRAY}; }}
#statusDot[state="error"] {{ color: {ACCENT_RED}; }}
#statusDot[state="scanning"] {{ color: {ACCENT_CYAN}; }}

/* ── Channel list ───────────────────────────────────────────── */
#channelList {{
    background-color: {BG_CARD};
    border-right: 1px solid {BG_BORDER};
    min-width: 140px;
    max-width: 180px;
}}
#channelList QListWidget {{
    background: transparent;
    border: none;
    outline: none;
    color: {TEXT_SECONDARY};
}}
#channelList QListWidget::item {{
    padding: 8px 12px;
    border-radius: 6px;
    margin: 2px 6px;
}}
#channelList QListWidget::item:hover {{
    background-color: {BG_HOVER};
    color: {TEXT_PRIMARY};
}}
#channelList QListWidget::item:selected {{
    background-color: rgba(0, 212, 255, 0.12);
    color: {ACCENT_CYAN};
}}

/* ── Chat area ──────────────────────────────────────────────── */
#chatArea {{
    background-color: {BG_DEEP};
}}
#chatScrollArea {{
    background-color: {BG_DEEP};
    border: none;
}}

/* ── Message bubbles ────────────────────────────────────────── */
#bubbleOut {{
    background-color: rgba(0, 212, 255, 0.08);
    border: 1px solid rgba(0, 212, 255, 0.25);
    border-radius: 10px 2px 10px 10px;
}}
#bubbleIn {{
    background-color: rgba(139, 92, 246, 0.08);
    border: 1px solid rgba(139, 92, 246, 0.25);
    border-radius: 2px 10px 10px 10px;
}}
#bubbleSystem {{
    background-color: rgba(90, 102, 144, 0.15);
    border: 1px solid rgba(90, 102, 144, 0.3);
    border-radius: 6px;
}}
#senderLabel {{
    color: {ACCENT_CYAN};
    font-size: 11px;
    font-weight: 600;
}}
#senderLabelIn {{
    color: {ACCENT_PURPLE};
    font-size: 11px;
    font-weight: 600;
}}
#msgText {{
    color: {TEXT_PRIMARY};
}}
#msgMeta {{
    color: {TEXT_SECONDARY};
    font-size: 10px;
}}
#statusLabel[status="sending"]           {{ color: {ACCENT_AMBER}; }}
#statusLabel[status="accepted_by_radio"] {{ color: {ACCENT_CYAN}; }}
#statusLabel[status="acknowledged"]      {{ color: {ACCENT_GREEN}; }}
#statusLabel[status="failed"]            {{ color: {ACCENT_RED}; }}
#statusLabel[status="unknown_delivery"]  {{ color: {ACCENT_GRAY}; }}

/* ── Composer ───────────────────────────────────────────────── */
#composer {{
    background-color: {BG_CARD};
    border-top: 1px solid {BG_BORDER};
    padding: 8px 10px;
}}
#composerInput {{
    background-color: {BG_INPUT};
    border: 1px solid {BG_BORDER};
    border-radius: 8px;
    color: {TEXT_PRIMARY};
    padding: 6px 10px;
    font-size: 13px;
}}
#composerInput:focus {{
    border-color: {ACCENT_CYAN};
}}
#byteCount {{
    color: {TEXT_SECONDARY};
    font-size: 10px;
}}
#byteCountWarn {{ color: {ACCENT_AMBER}; }}
#byteCountOver {{ color: {ACCENT_RED}; }}

/* ── Monitor cards ──────────────────────────────────────────── */
#metricCard {{
    background-color: {BG_CARD};
    border: 1px solid {BG_BORDER};
    border-radius: 8px;
    padding: 8px;
}}
#metricCard:hover {{
    border-color: {ACCENT_CYAN};
}}
#cardTitle {{
    color: {TEXT_SECONDARY};
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 1px;
    text-transform: uppercase;
}}
#cardValue {{
    color: {ACCENT_CYAN};
    font-size: 20px;
    font-family: Consolas, monospace;
    font-weight: 700;
}}
#cardValueGreen {{ color: {ACCENT_GREEN}; font-size: 20px; font-family: Consolas, monospace; font-weight: 700; }}
#cardValueAmber {{ color: {ACCENT_AMBER}; font-size: 20px; font-family: Consolas, monospace; font-weight: 700; }}
#cardSub {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
}}

/* ── Monitor header ─────────────────────────────────────────── */
#monitorHeader {{
    background-color: {BG_CARD};
    border-bottom: 1px solid {BG_BORDER};
    padding: 4px 10px;
    min-height: 36px;
    max-height: 36px;
}}
#monitorTitle {{
    color: {ACCENT_CYAN};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 2px;
}}
#headerTelemetry {{
    color: {TEXT_SECONDARY};
    font-size: 11px;
    font-family: Consolas, monospace;
}}

/* ── Rankings panel ─────────────────────────────────────────── */
#rankPanel {{
    background-color: {BG_CARD};
    border: 1px solid {BG_BORDER};
    border-radius: 8px;
}}
#rankTitle {{
    background-color: rgba(0, 212, 255, 0.06);
    border-bottom: 1px solid {BG_BORDER};
    color: {ACCENT_CYAN};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    padding: 4px 8px;
}}
#rankTitle:hover {{
    background-color: rgba(0, 212, 255, 0.12);
}}
#rankItem {{
    border-bottom: 1px solid rgba(26, 36, 72, 0.5);
    padding: 4px 8px;
}}
#rankItem:hover {{
    background-color: {BG_HOVER};
}}

/* ── Packet log ─────────────────────────────────────────────── */
#packetLog {{
    background-color: {BG_DEEP};
    border: 1px solid {BG_BORDER};
    border-radius: 4px;
    font-family: Consolas, monospace;
    font-size: 11px;
    color: {TEXT_PRIMARY};
    gridline-color: {BG_BORDER};
}}
#packetLog QHeaderView::section {{
    background-color: {BG_CARD};
    border: none;
    border-right: 1px solid {BG_BORDER};
    color: {TEXT_SECONDARY};
    font-size: 10px;
    font-weight: 600;
    padding: 4px 6px;
}}

/* ── Scrollbars ─────────────────────────────────────────────── */
QScrollBar:vertical {{
    background: {SCROLLBAR_BG};
    width: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical {{
    background: {SCROLLBAR_HANDLE};
    border-radius: 3px;
    min-height: 30px;
}}
QScrollBar::handle:vertical:hover {{
    background: {ACCENT_CYAN};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{
    background: {SCROLLBAR_BG};
    height: 6px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal {{
    background: {SCROLLBAR_HANDLE};
    border-radius: 3px;
    min-width: 30px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{ width: 0; }}

/* ── Splitter ───────────────────────────────────────────────── */
QSplitter::handle {{
    background-color: {BG_BORDER};
    width: 2px;
    height: 2px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT_CYAN};
}}

/* ── Labels ─────────────────────────────────────────────────── */
QLabel#sectionLabel {{
    color: {TEXT_SECONDARY};
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1.5px;
    text-transform: uppercase;
}}

/* ── Tab widget ─────────────────────────────────────────────── */
QTabWidget::pane {{
    border: 1px solid {BG_BORDER};
    border-radius: 6px;
    background: {BG_DEEP};
}}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_SECONDARY};
    border-bottom: 2px solid transparent;
    padding: 6px 14px;
    font-size: 11px;
}}
QTabBar::tab:selected {{
    color: {ACCENT_CYAN};
    border-bottom-color: {ACCENT_CYAN};
}}
QTabBar::tab:hover {{
    color: {TEXT_PRIMARY};
}}

/* ── Tooltip ────────────────────────────────────────────────── */
QToolTip {{
    background-color: {BG_CARD};
    border: 1px solid {ACCENT_CYAN};
    color: {TEXT_PRIMARY};
    padding: 4px 8px;
    font-size: 11px;
}}

/* ── Table views ────────────────────────────────────────────── */
QTableView {{
    background-color: {BG_DEEP};
    gridline-color: {BG_BORDER};
    color: {TEXT_PRIMARY};
    border: 1px solid {BG_BORDER};
    selection-background-color: rgba(0, 212, 255, 0.12);
    selection-color: {ACCENT_CYAN};
    font-size: 12px;
}}
QTableView QHeaderView::section {{
    background-color: {BG_CARD};
    border: none;
    border-right: 1px solid {BG_BORDER};
    border-bottom: 1px solid {BG_BORDER};
    color: {TEXT_SECONDARY};
    font-size: 10px;
    font-weight: 600;
    padding: 4px 6px;
}}

/* ── Line edits / inputs ────────────────────────────────────── */
QLineEdit, QTextEdit, QPlainTextEdit {{
    background-color: {BG_INPUT};
    border: 1px solid {BG_BORDER};
    border-radius: 5px;
    color: {TEXT_PRIMARY};
    padding: 4px 8px;
    selection-background-color: {ACCENT_CYAN};
    selection-color: {BG_DEEP};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT_CYAN};
}}

/* ── Combo box ──────────────────────────────────────────────── */
QComboBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BG_BORDER};
    border-radius: 5px;
    color: {TEXT_PRIMARY};
    padding: 4px 8px;
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {BG_CARD};
    border: 1px solid {ACCENT_CYAN};
    color: {TEXT_PRIMARY};
    selection-background-color: {ACCENT_CYAN};
    selection-color: {BG_DEEP};
}}

/* ── Spin box ───────────────────────────────────────────────── */
QSpinBox {{
    background-color: {BG_INPUT};
    border: 1px solid {BG_BORDER};
    border-radius: 5px;
    color: {TEXT_PRIMARY};
    padding: 3px 6px;
}}

/* ── Check box ──────────────────────────────────────────────── */
QCheckBox {{ color: {TEXT_PRIMARY}; spacing: 6px; }}
QCheckBox::indicator {{
    width: 14px; height: 14px;
    border: 1px solid {ACCENT_CYAN};
    border-radius: 3px;
    background: {BG_INPUT};
}}
QCheckBox::indicator:checked {{
    background: {ACCENT_CYAN};
}}

/* ── Status bar ─────────────────────────────────────────────── */
QStatusBar {{
    background-color: {BG_CARD};
    border-top: 1px solid {BG_BORDER};
    color: {TEXT_SECONDARY};
    font-size: 11px;
}}

/* ── Menu ───────────────────────────────────────────────────── */
QMenuBar {{
    background-color: {BG_CARD};
    color: {TEXT_PRIMARY};
    border-bottom: 1px solid {BG_BORDER};
}}
QMenuBar::item:selected {{ background-color: {BG_HOVER}; color: {ACCENT_CYAN}; }}
QMenu {{
    background-color: {BG_CARD};
    border: 1px solid {BG_BORDER};
    color: {TEXT_PRIMARY};
}}
QMenu::item:selected {{ background-color: {ACCENT_CYAN}; color: {BG_DEEP}; }}
QMenu::separator {{ background-color: {BG_BORDER}; height: 1px; margin: 3px 10px; }}
"""
