import csv
import os
import sys
import time
from logging import getLogger
from PySide6.QtCore import (
    Qt, Slot, QAbstractTableModel, QSortFilterProxyModel, QTimer,
)
from PySide6.QtGui import QIcon, QColor, QFont
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QComboBox, QSpinBox, QCheckBox,
    QProgressBar, QFileDialog, QMessageBox, QTableView,
    QHeaderView, QPlainTextEdit, QSplitter, QGroupBox, QGridLayout,
    QSizePolicy, QFrame, QAbstractSpinBox,
)
from backend import (
    ScanWorker, SubscanV2, format_tokens, DEFAULT_BASE_URL, DECIMAL_PLACES, SSC,
)

COLORS = {
    "BlockReward": QColor(46, 160, 67),     # green
    "VoteReward":  QColor(46, 125, 200),     # blue
    "BlockFees":   QColor(200, 140, 30),     # amber
    "Transfer":    QColor(160, 160, 160),    # grey
}

_COLUMNS = ["Block", "Type", "Sub", "Amount", "Counterparty"]
_COL_KEYS = ["block", "type", "sub", "amount", "counterparty"]

def get_running_path(relative_path):
    if "_internal" in os.listdir():
        return os.path.join("_internal", relative_path)
    return relative_path

# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------

class TransactionTableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent=None):
        return len(self._rows)

    def columnCount(self, parent=None):
        return len(_COLUMNS)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        key = _COL_KEYS[index.column()]
        val = row.get(key, "")

        if role == Qt.DisplayRole:
            if key == "amount":
                return f"{val:,.6f} AI3"
            return str(val)

        if role == Qt.ForegroundRole:
            etype = row.get("type", "")
            return COLORS.get(etype, QColor(220, 220, 220))

        if role == Qt.TextAlignmentRole:
            if key == "amount":
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if key == "block":
                return int(Qt.AlignRight | Qt.AlignVCenter)
            return int(Qt.AlignLeft | Qt.AlignVCenter)

        if role == Qt.UserRole:
            # Raw data for sorting
            if key == "amount":
                return val
            return str(val)

        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return _COLUMNS[section]
        return None

# ---------------------------------------------------------------------------
# Filter proxy
# ---------------------------------------------------------------------------

class TransactionFilterProxy(QSortFilterProxyModel):
    """Filters the table by text (searches all columns) and optionally by type."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text_filter = ""
        self._type_filter = ""       # empty = all types
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def set_text_filter(self, text: str):
        self._text_filter = text.strip()
        self.invalidateFilter()

    def set_type_filter(self, etype: str):
        self._type_filter = etype
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row, source_parent):
        model = self.sourceModel()
        if not model:
            return True

        row = model._rows[source_row]

        # Type filter
        if self._type_filter and row.get("type", "") != self._type_filter:
            return False

        # Text filter — search all display columns
        if self._text_filter:
            txt = self._text_filter.lower()
            hit = False
            for key in _COL_KEYS:
                val = str(row.get(key, "")).lower()
                if txt in val:
                    hit = True
                    break
            if not hit:
                return False

        return True

    def lessThan(self, left, right):
        """Sort by UserRole data (raw values)."""
        column = self.sortColumn()
        left_data = self.sourceModel().data(left, Qt.UserRole)
        right_data = self.sourceModel().data(right, Qt.UserRole)

        if _COL_KEYS[column] == "amount":
            try:
                return float(left_data) < float(right_data)
            except (ValueError, TypeError):
                pass
        if _COL_KEYS[column] == "block":
            try:
                return int(left_data) < int(right_data)
            except (ValueError, TypeError):
                pass
        return str(left_data) < str(right_data)

# ---------------------------------------------------------------------------
# Summary panel
# ---------------------------------------------------------------------------

class SummaryPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        layout = QGridLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)

        bold = QFont()
        bold.setBold(True)

        layout.addWidget(QLabel("Block Rewards:"), 0, 0)
        self.lbl_block_rewards = QLabel("—")
        self.lbl_block_rewards.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_block_rewards, 0, 1)

        layout.addWidget(QLabel("Vote Rewards:"), 1, 0)
        self.lbl_vote_rewards = QLabel("—")
        self.lbl_vote_rewards.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_vote_rewards, 1, 1)

        layout.addWidget(QLabel("BlockFees:"), 2, 0)
        self.lbl_fees = QLabel("—")
        self.lbl_fees.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_fees, 2, 1)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line, 3, 0, 1, 2)

        layout.addWidget(QLabel("Total Farmed:"), 4, 0)
        self.lbl_farmed = QLabel("—")
        self.lbl_farmed.setFont(bold)
        self.lbl_farmed.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_farmed, 4, 1)

        line2 = QFrame()
        line2.setFrameShape(QFrame.HLine)
        layout.addWidget(line2, 5, 0, 1, 2)

        layout.addWidget(QLabel("Transfers IN:"), 6, 0)
        self.lbl_tx_in = QLabel("—")
        self.lbl_tx_in.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_tx_in, 6, 1)

        layout.addWidget(QLabel("Transfers OUT:"), 7, 0)
        self.lbl_tx_out = QLabel("—")
        self.lbl_tx_out.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_tx_out, 7, 1)

        layout.addWidget(QLabel("Net Transfer:"), 8, 0)
        self.lbl_tx_net = QLabel("—")
        self.lbl_tx_net.setFont(bold)
        self.lbl_tx_net.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(self.lbl_tx_net, 8, 1)

        layout.setColumnStretch(0, 0)
        layout.setColumnStretch(1, 1)

    def update_summary(self, rows: list[dict]):
        totals = {
            "BlockReward": 0, "VoteReward": 0,
            "BlockFees": 0,
            "Transfers IN": 0, "Transfers OUT": 0,
        }
        for r in rows:
            etype = r["type"]
            raw = r["amount_raw"]
            if etype in ("BlockReward", "VoteReward"):
                totals[etype] += raw
            elif etype == "BlockFees":
                totals["BlockFees"] += raw
            elif etype == "Transfer":
                if r["sub"] == "IN":
                    totals["Transfers IN"] += raw
                else:
                    totals["Transfers OUT"] += raw

        farmed = totals["BlockReward"] + totals["VoteReward"] + totals["BlockFees"]
        net = totals["Transfers IN"] - totals["Transfers OUT"]

        self.lbl_block_rewards.setText(f"{totals['BlockReward'] / SSC:,.6f} AI3")
        self.lbl_vote_rewards.setText(f"{totals['VoteReward'] / SSC:,.6f} AI3")
        self.lbl_fees.setText(f"{totals['BlockFees'] / SSC:,.6f} AI3")
        self.lbl_farmed.setText(f"{farmed / SSC:,.6f} AI3")
        self.lbl_tx_in.setText(f"{totals['Transfers IN'] / SSC:,.6f} AI3")
        self.lbl_tx_out.setText(f"{totals['Transfers OUT'] / SSC:,.6f} AI3")
        self.lbl_tx_net.setText(f"{net / SSC:,.6f} AI3")

    def clear(self):
        for lbl in [self.lbl_block_rewards, self.lbl_vote_rewards, self.lbl_fees,
                     self.lbl_farmed, self.lbl_tx_in, self.lbl_tx_out, self.lbl_tx_net]:
            lbl.setText("—")

# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self._log = getLogger(__name__)
        self._worker: ScanWorker | None = None
        self._rows: list[dict] = []

        self._build_ui()
        self._restore_settings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        self.setWindowTitle("Autonomys Transaction History v" +
                            open(get_running_path("version.txt")).read().strip())
        self.setWindowIcon(QIcon(get_running_path("icon.ico")))
        self.setMinimumSize(900, 700)
        self.resize(1100, 850)

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)

        # ================================================================
        # Settings group
        # ================================================================
        settings_group = QGroupBox("Connection & Query Settings")
        settings_layout = QGridLayout(settings_group)
        settings_layout.setSpacing(6)

        # --- API Key ---
        settings_layout.addWidget(QLabel("API Key:"), 0, 0)
        self.api_key_input = QLineEdit()
        self.api_key_input.setEchoMode(QLineEdit.Password)
        self.api_key_input.setPlaceholderText("Subscan API key (get at pro.subscan.io)")
        settings_layout.addWidget(self.api_key_input, 0, 1)

        # --- Address ---
        settings_layout.addWidget(QLabel("Address:"), 1, 0)
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("Subspace SS58 address (prefix 6094)")
        settings_layout.addWidget(self.address_input, 1, 1)

        # --- Mode selector ---
        settings_layout.addWidget(QLabel("Mode:"), 2, 0)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Last N days", "days")
        self.mode_combo.addItem("Block range", "range")
        self.mode_combo.addItem("Entire history", "all")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        settings_layout.addWidget(self.mode_combo, 2, 1)

        # --- Days spinbox ---
        self.days_label = QLabel("Days:")
        settings_layout.addWidget(self.days_label, 3, 0)
        self.days_spin = QSpinBox()
        self.days_spin.setRange(1, 3650)
        self.days_spin.setValue(7)
        self.days_spin.setSuffix(" days ago")
        settings_layout.addWidget(self.days_spin, 3, 1)

        # --- Block range ---
        self.range_start_label = QLabel("From block:")
        settings_layout.addWidget(self.range_start_label, 4, 0)
        range_layout = QHBoxLayout()
        self.block_start_spin = QSpinBox()
        self.block_start_spin.setRange(0, 999_999_999)
        self.block_start_spin.setSpecialValueText("earliest")
        self.block_start_spin.setValue(0)
        range_layout.addWidget(self.block_start_spin)

        self.range_end_label = QLabel("  to:")
        range_layout.addWidget(self.range_end_label)
        self.block_end_spin = QSpinBox()
        self.block_end_spin.setRange(0, 999_999_999)
        self.block_end_spin.setSpecialValueText("latest")
        self.block_end_spin.setValue(0)
        range_layout.addWidget(self.block_end_spin)
        range_layout.addStretch()
        settings_layout.addLayout(range_layout, 4, 1)

        # Hide range by default
        self.range_start_label.setVisible(False)
        self.block_start_spin.setVisible(False)
        self.range_end_label.setVisible(False)
        self.block_end_spin.setVisible(False)

        main_layout.addWidget(settings_group)

        # ================================================================
        # Event types row
        # ================================================================
        types_layout = QHBoxLayout()
        types_layout.addWidget(QLabel("Event types:"))
        self.chk_rewards = QCheckBox("Rewards")
        self.chk_rewards.setChecked(True)
        types_layout.addWidget(self.chk_rewards)
        self.chk_fees = QCheckBox("Fees")
        self.chk_fees.setChecked(True)
        types_layout.addWidget(self.chk_fees)
        self.chk_transfers = QCheckBox("Transfers")
        self.chk_transfers.setChecked(True)
        types_layout.addWidget(self.chk_transfers)
        types_layout.addStretch()
        main_layout.addLayout(types_layout)

        # ================================================================
        # Action buttons + progress bar
        # ================================================================
        action_layout = QHBoxLayout()

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._on_scan)
        action_layout.addWidget(self.scan_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop)
        action_layout.addWidget(self.stop_btn)

        self.export_btn = QPushButton("Export CSV")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self._on_export)
        action_layout.addWidget(self.export_btn)

        action_layout.addSpacing(10)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(18)
        action_layout.addWidget(self.progress_bar, stretch=1)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("color: #888;")
        action_layout.addWidget(self.status_label)

        main_layout.addLayout(action_layout)

        # ================================================================
        # Filter row
        # ================================================================
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filter:"))

        self.filter_input = QLineEdit()
        self.filter_input.setPlaceholderText("Search all columns...")
        self.filter_input.setClearButtonEnabled(True)
        self.filter_input.textChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.filter_input, stretch=1)

        filter_layout.addWidget(QLabel("  Type:"))
        self.type_filter_combo = QComboBox()
        self.type_filter_combo.addItem("All", "")
        self.type_filter_combo.addItem("BlockReward", "BlockReward")
        self.type_filter_combo.addItem("VoteReward", "VoteReward")
        self.type_filter_combo.addItem("BlockFees", "BlockFees")
        self.type_filter_combo.addItem("Transfer", "Transfer")
        self.type_filter_combo.currentIndexChanged.connect(self._on_type_filter_changed)
        filter_layout.addWidget(self.type_filter_combo)

        self.result_count_label = QLabel("")
        filter_layout.addWidget(self.result_count_label)

        main_layout.addLayout(filter_layout)

        # ================================================================
        # Splitter: table + summary/console
        # ================================================================
        splitter = QSplitter(Qt.Vertical)

        # --- Table ---
        self.table_model = TransactionTableModel()
        self.proxy_model = TransactionFilterProxy()
        self.proxy_model.setSourceModel(self.table_model)
        self.proxy_model.setSortRole(Qt.UserRole)

        self.table_view = QTableView()
        self.table_view.setModel(self.proxy_model)
        self.table_view.setSortingEnabled(True)
        self.table_view.setAlternatingRowColors(True)
        self.table_view.setSelectionBehavior(QTableView.SelectRows)
        self.table_view.setSelectionMode(QTableView.SingleSelection)
        self.table_view.horizontalHeader().setStretchLastSection(True)
        self.table_view.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table_view.horizontalHeader().setMinimumSectionSize(60)
        self.table_view.setShowGrid(False)
        self.table_view.verticalHeader().setVisible(False)
        # Default column widths
        self.table_view.setColumnWidth(0, 90)   # Block
        self.table_view.setColumnWidth(1, 120)  # Type
        self.table_view.setColumnWidth(2, 80)   # Sub
        self.table_view.setColumnWidth(3, 160)  # Amount
        # Counterparty stretches (last section)
        splitter.addWidget(self.table_view)

        # --- Bottom: summary + console ---
        bottom = QWidget()
        bottom_layout = QHBoxLayout(bottom)
        bottom_layout.setContentsMargins(0, 4, 0, 0)

        self.summary_panel = SummaryPanel()
        self.summary_panel.setFixedWidth(320)
        bottom_layout.addWidget(self.summary_panel, 0)

        console_widget = QWidget()
        console_layout = QVBoxLayout(console_widget)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_header = QHBoxLayout()
        console_header.addWidget(QLabel("Console"))
        console_header.addStretch()
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._on_clear_console)
        console_header.addWidget(clear_btn)
        console_layout.addLayout(console_header)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        console_layout.addWidget(self.console)
        bottom_layout.addWidget(console_widget, 1)

        splitter.addWidget(bottom)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)

        main_layout.addWidget(splitter, stretch=1)

    # ------------------------------------------------------------------
    # Mode switching
    # ------------------------------------------------------------------

    @Slot()
    def _on_mode_changed(self):
        mode = self.mode_combo.currentData()
        is_days = (mode == "days")
        is_range = (mode == "range")

        self.days_label.setVisible(is_days)
        self.days_spin.setVisible(is_days)

        self.range_start_label.setVisible(is_range)
        self.block_start_spin.setVisible(is_range)
        self.range_end_label.setVisible(is_range)
        self.block_end_spin.setVisible(is_range)

    # ------------------------------------------------------------------
    # Scan
    # ------------------------------------------------------------------

    @Slot()
    def _on_scan(self):
        api_key = self.api_key_input.text().strip()
        address = self.address_input.text().strip()

        if not api_key:
            QMessageBox.warning(self, "Missing API Key",
                                "Please enter your Subscan API key.\n"
                                "Get one free at https://pro.subscan.io/")
            return
        if not address:
            QMessageBox.warning(self, "Missing Address",
                                "Please enter a Subspace address to scan.")
            return

        # Gather event types
        types = set()
        if self.chk_rewards.isChecked():
            types.add("rewards")
        if self.chk_fees.isChecked():
            types.add("fees")
        if self.chk_transfers.isChecked():
            types.add("transfers")
        if not types:
            QMessageBox.warning(self, "No Event Types",
                                "Select at least one event type to scan.")
            return

        # Compute block range
        mode = self.mode_combo.currentData()
        start_block: int | None = None
        end_block: int | None = None

        if mode == "days":
            end_block = 0  # will be resolved after metadata
            # We'll use the lookup via Subscan metadata at scan time
            # For now, start_block is computed in the worker
            start_block = None
        elif mode == "range":
            sb = self.block_start_spin.value()
            eb = self.block_end_spin.value()
            start_block = sb if sb > 0 else None
            end_block = eb if eb > 0 else None
        else:  # all
            start_block = None
            end_block = None

        # Save settings
        self._save_settings()

        # Set running state
        self._set_running_state(True)
        self._rows = []
        self.table_model.set_rows([])
        self.summary_panel.clear()
        self.result_count_label.setText("")
        self._log.info("Starting scan...")

        # Create worker
        self._worker = ScanWorker(
            api_key=api_key, address=address,
            start_block=start_block, end_block=end_block,
            types=types,
        )
        self._worker.step_changed.connect(self._on_step)
        self._worker.progress.connect(self._on_progress)
        self._worker.log_message.connect(self._on_log)
        self._worker.results_ready.connect(self._on_results)
        self._worker.fatal_error.connect(self._on_fatal_error)
        self._worker.scan_finished.connect(self._on_finished)

        # If days mode, resolve block range first
        if mode == "days":
            self._resolve_days_and_start(api_key, address, types,
                                         self.days_spin.value(),
                                         start_block, end_block)
        else:
            self._worker.start()

    def _resolve_days_and_start(self, api_key, address, types, days,
                                 start_block, end_block):
        """Fetch latest block, compute start from days, then launch worker."""
        try:
            api = SubscanV2(DEFAULT_BASE_URL, api_key)
            meta = api.metadata()
            latest = int(meta.get("blockNum", 0))
            # avgBlockTime ~6.4s → ~13,500 blocks/day
            start = max(1, latest - days * 13_500)

            self._worker._start_block = start
            self._worker._end_block = latest
            self._log.info(f"Resolved blocks: {start} -> {latest} (last {days} days)")
            self._worker.start()
        except Exception as exc:
            self._on_fatal_error(f"Failed to resolve block range: {exc}")

    # ------------------------------------------------------------------
    # Stop
    # ------------------------------------------------------------------

    @Slot()
    def _on_stop(self):
        if self._worker:
            self._worker.stop()
            self.status_label.setText("Stopping...")

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    @Slot()
    def _on_export(self):
        if not self._rows:
            QMessageBox.information(self, "Nothing to Export",
                                    "No results to export.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", "transactions.csv",
            "CSV Files (*.csv);;All Files (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(["block", "event_type", "sub_type",
                                 "amount_raw_shannon", "amount_ai3", "counterparty"])
                for r in self._rows:
                    writer.writerow([
                        r["block"], r["type"], r["sub"],
                        r["amount_raw"], f"{r['amount']:.6f}",
                        r["counterparty"],
                    ])
            self._log.info(f"Exported {len(self._rows)} rows to {path}")
        except Exception as exc:
            QMessageBox.critical(self, "Export Failed", str(exc))

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_filter_changed(self, text):
        self.proxy_model.set_text_filter(text)
        self._update_result_count()

    @Slot()
    def _on_type_filter_changed(self):
        self.proxy_model.set_type_filter(self.type_filter_combo.currentData())
        self._update_result_count()

    def _update_result_count(self):
        total = len(self._rows)
        visible = self.proxy_model.rowCount()
        if visible == total:
            self.result_count_label.setText(f"{total} rows")
        else:
            self.result_count_label.setText(f"{visible} / {total} rows shown")

    # ------------------------------------------------------------------
    # Worker slots
    # ------------------------------------------------------------------

    @Slot(str)
    def _on_step(self, msg):
        self.status_label.setText(msg)
        self._log.info(msg)

    @Slot(int, int)
    def _on_progress(self, current, total):
        if total > 0:
            self.progress_bar.setRange(0, total)
            self.progress_bar.setValue(current)
            self.progress_bar.setTextVisible(True)
            self.progress_bar.setFormat(f"{current}/{total}")
        else:
            self.progress_bar.setRange(0, 0)
            self.progress_bar.setTextVisible(False)

    @Slot(str)
    def _on_log(self, msg):
        self._log.info(msg)
        self.console.appendPlainText(msg)

    @Slot(list)
    def _on_results(self, rows):
        self._rows = rows
        self.table_model.set_rows(rows)
        self.summary_panel.update_summary(rows)
        self._update_result_count()

    @Slot(str)
    def _on_fatal_error(self, msg):
        self._log.error(msg)
        self.console.appendPlainText(f"ERROR: {msg}")
        QMessageBox.critical(self, "Error", f"Scan failed:\n\n{msg}")

    @Slot()
    def _on_finished(self):
        self._set_running_state(False)
        self.status_label.setText("Done")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self._log.info("Scan complete.")

    # ------------------------------------------------------------------
    # UI state
    # ------------------------------------------------------------------

    def _set_running_state(self, running: bool):
        self.api_key_input.setEnabled(not running)
        self.address_input.setEnabled(not running)
        self.mode_combo.setEnabled(not running)
        self.days_spin.setEnabled(not running)
        self.block_start_spin.setEnabled(not running)
        self.block_end_spin.setEnabled(not running)
        self.chk_rewards.setEnabled(not running)
        self.chk_fees.setEnabled(not running)
        self.chk_transfers.setEnabled(not running)
        self.scan_btn.setEnabled(not running)
        self.stop_btn.setEnabled(running)
        self.export_btn.setEnabled(not running and bool(self._rows))

    # ------------------------------------------------------------------
    # Console
    # ------------------------------------------------------------------

    @Slot()
    def _on_clear_console(self):
        self.console.clear()

    # ------------------------------------------------------------------
    # Settings persistence
    # ------------------------------------------------------------------

    def _save_settings(self):
        try:
            import json
            settings = {
                "api_key": self.api_key_input.text().strip(),
                "address": self.address_input.text().strip(),
                "mode": self.mode_combo.currentData(),
                "days": self.days_spin.value(),
                "block_start": self.block_start_spin.value(),
                "block_end": self.block_end_spin.value(),
                "rewards": self.chk_rewards.isChecked(),
                "fees": self.chk_fees.isChecked(),
                "transfers": self.chk_transfers.isChecked(),
            }
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "settings.json")
            with open(path, "w") as f:
                json.dump(settings, f, indent=2)
        except Exception:
            pass

    def _restore_settings(self):
        try:
            import json
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "settings.json")
            if not os.path.isfile(path):
                return
            with open(path, "r") as f:
                s = json.load(f)
            self.api_key_input.setText(s.get("api_key", ""))
            self.address_input.setText(s.get("address", ""))
            mode = s.get("mode", "days")
            idx = self.mode_combo.findData(mode)
            if idx >= 0:
                self.mode_combo.setCurrentIndex(idx)
            self.days_spin.setValue(s.get("days", 7))
            self.block_start_spin.setValue(s.get("block_start", 0))
            self.block_end_spin.setValue(s.get("block_end", 0))
            self.chk_rewards.setChecked(s.get("rewards", True))
            self.chk_fees.setChecked(s.get("fees", True))
            self.chk_transfers.setChecked(s.get("transfers", True))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def closeEvent(self, event):
        self._save_settings()
        if self._worker and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(5000)
        event.accept()