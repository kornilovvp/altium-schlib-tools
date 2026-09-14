#!/usr/bin/env python3
"""schlib_param_editor.py -- Excel-like parameter table editor for Altium .SchLib files (PyQt5).

    python schlib_param_editor.py [Libs/MC3.SchLib]

Rows are components, columns are parameters (plus Description / DesignItemId / Designator / Footprints).
  hatched cell   the component has no such parameter (typing a value creates it)
  yellow cell    changed since load
  Ctrl+C / Ctrl+V   copy / paste tab separated blocks (Excel compatible)
  Ctrl+D            fill the selection with the top cell of each column
  Delete            clear the value (parameter stays)
  Ctrl+Delete       remove the parameter from the selected components
"""
import os
import sys
import traceback

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import altium_schlib as asl
import schlib_params as tbl

ROLE_COMP = Qt.UserRole            # index into lib.components
ROLE_MISSING = Qt.UserRole + 1     # True when the component has no such parameter
FIXED = tbl.FIXED_COLUMNS[1:]      # LibReference is shown in the vertical header
EDITABLE_FIXED = set(tbl.FIXED_ATTRS)

COLOR_MISSING = QtGui.QColor(150, 150, 150)
COLOR_CHANGED = QtGui.QColor(255, 244, 150)
COLOR_READONLY = QtGui.QColor(232, 232, 232)


class ParamTable(QtWidgets.QTableWidget):
    """QTableWidget with Excel-style clipboard, fill-down and delete keys."""

    removeParamRequested = QtCore.pyqtSignal()
    clearRequested = QtCore.pyqtSignal()
    fillDownRequested = QtCore.pyqtSignal()

    def keyPressEvent(self, ev):
        if ev.matches(QtGui.QKeySequence.Copy):
            self.copy_selection()
            return
        if ev.matches(QtGui.QKeySequence.Paste):
            self.paste_clipboard()
            return
        if ev.key() == Qt.Key_Delete:
            if ev.modifiers() & Qt.ControlModifier:
                self.removeParamRequested.emit()
            else:
                self.clearRequested.emit()
            return
        if ev.key() == Qt.Key_D and ev.modifiers() & Qt.ControlModifier:
            self.fillDownRequested.emit()
            return
        super().keyPressEvent(ev)

    def copy_selection(self):
        ranges = self.selectedRanges()
        if not ranges:
            return
        r = ranges[0]
        lines = []
        for row in range(r.topRow(), r.bottomRow() + 1):
            if self.isRowHidden(row):
                continue
            cells = []
            for col in range(r.leftColumn(), r.rightColumn() + 1):
                it = self.item(row, col)
                cells.append(it.text() if it else "")
            lines.append("\t".join(cells))
        QtWidgets.QApplication.clipboard().setText("\n".join(lines))

    def paste_clipboard(self):
        text = QtWidgets.QApplication.clipboard().text()
        if not text:
            return
        block = [line.split("\t") for line in text.replace("\r\n", "\n").rstrip("\n").split("\n")]
        cur = self.currentItem()
        r0, c0 = (cur.row(), cur.column()) if cur else (0, 0)
        sel = self.selectedIndexes()
        if len(block) == 1 and len(block[0]) == 1 and len(sel) > 1:       # one value -> whole selection
            for idx in sel:
                self.set_cell(idx.row(), idx.column(), block[0][0])
            return
        visible = [r for r in range(r0, self.rowCount()) if not self.isRowHidden(r)]
        for i, vals in enumerate(block):
            if i >= len(visible):
                break
            for j, v in enumerate(vals):
                if c0 + j >= self.columnCount():
                    break
                self.set_cell(visible[i], c0 + j, v)

    def set_cell(self, row, col, text):
        it = self.item(row, col)
        if it is not None and (it.flags() & Qt.ItemIsEditable):
            it.setText(text)                         # emits itemChanged -> MainWindow.on_item_changed


class AddColumnDialog(QtWidgets.QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Add parameter column")
        form = QtWidgets.QFormLayout(self)
        self.name = QtWidgets.QLineEdit()
        self.value = QtWidgets.QLineEdit()
        self.create_all = QtWidgets.QCheckBox("Create the parameter on every component now")
        self.create_all.setChecked(True)
        form.addRow("Parameter name:", self.name)
        form.addRow("Initial value:", self.value)
        form.addRow("", self.create_all)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, path=None):
        super().__init__()
        self.lib = None
        self.columns = []
        self.orig = {}                 # (component index, column) -> value or None (missing)
        self.renamed = {}              # new column name -> original name
        self.sort_rows = False
        self._loading = False
        self._build_ui()
        self.resize(1400, 800)
        self.update_title()
        if path:
            self.open_file(path)

    # ------------------------------------------------------------------ UI construction
    def _build_ui(self):
        tb = self.addToolBar("Main")
        tb.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        style = self.style()

        def act(text, slot, icon=None, shortcut=None, tip=None):
            a = QtWidgets.QAction(style.standardIcon(icon) if icon else QtGui.QIcon(), text, self)
            a.triggered.connect(slot)
            if shortcut:
                a.setShortcut(shortcut)
            if tip:
                a.setToolTip(tip + (" (%s)" % shortcut if shortcut else ""))
            tb.addAction(a)
            return a

        act("Open", self.open_dialog, QtWidgets.QStyle.SP_DialogOpenButton, "Ctrl+O")
        self.act_save = act("Save", self.save, QtWidgets.QStyle.SP_DialogSaveButton, "Ctrl+S",
                            "Save into the same file (a .bak copy is kept)")
        act("Save As", self.save_as, None, "Ctrl+Shift+S")
        act("Reload", self.reload, QtWidgets.QStyle.SP_BrowserReload, "F5", "Discard all changes")
        tb.addSeparator()
        act("Add column", self.add_column, None, None, "Add a new parameter")
        act("Rename column", self.rename_column, None, None, "Rename the parameter in every component")
        act("Delete column", self.delete_column, None, None, "Remove the parameter from every component")
        tb.addSeparator()
        act("Create in selection", self.create_in_selection, None, None,
            "Create the parameter (empty) where the selected cells are hatched")
        act("Remove in selection", self.remove_param_selected, None, "Ctrl+Del",
            "Remove the parameter from the selected components")
        act("Revert selection", self.revert_selection, None, None, "Restore the loaded values")
        tb.addSeparator()
        act("Export XLSX", self.export_xlsx, None, None, "Export the table to Excel")
        act("Import XLSX", self.import_table, None, None, "Apply an edited Excel / CSV table")
        tb.addSeparator()
        self.act_sort = act("Sort A-Z", self.toggle_sort, None, None, "Sort rows alphabetically")
        self.act_sort.setCheckable(True)
        tb.addSeparator()
        tb.addWidget(QtWidgets.QLabel(" Filter: "))
        self.filter = QtWidgets.QLineEdit()
        self.filter.setPlaceholderText("type to filter rows")
        self.filter.setClearButtonEnabled(True)
        self.filter.setMaximumWidth(260)
        self.filter.textChanged.connect(self.apply_filter)
        tb.addWidget(self.filter)

        self.table = ParamTable()
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                   | QtWidgets.QAbstractItemView.EditKeyPressed
                                   | QtWidgets.QAbstractItemView.AnyKeyPressed)
        self.table.horizontalHeader().setSectionsMovable(True)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.setAlternatingRowColors(True)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        self.table.itemChanged.connect(self.on_item_changed)
        self.table.currentCellChanged.connect(self.on_current_changed)
        self.table.removeParamRequested.connect(self.remove_param_selected)
        self.table.clearRequested.connect(self.clear_selection)
        self.table.fillDownRequested.connect(self.fill_down)
        self.setCentralWidget(self.table)

        dock = QtWidgets.QDockWidget("Component details (all records, read-only)", self)
        self.details = QtWidgets.QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setFont(QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont))
        self.details.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        dock.setWidget(self.details)
        self.addDockWidget(Qt.BottomDockWidgetArea, dock)
        dock.setMinimumHeight(140)
        self.status = self.statusBar()
        self.setAcceptDrops(True)

    # ------------------------------------------------------------------ file handling
    def open_dialog(self):
        if not self.confirm_discard():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open schematic library", "",
                                                        "Altium schematic library (*.SchLib);;All files (*)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        try:
            lib = asl.SchLib.load(path)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Cannot open", "%s\n\n%s" % (path, exc))
            return
        self.lib = lib
        self.renamed = {}
        self.build_table()
        self.snapshot()
        self.restyle_all()
        self.update_title()
        if lib.warnings:
            QtWidgets.QMessageBox.warning(self, "Warnings", "\n".join(lib.warnings))

    def reload(self):
        if self.lib and self.confirm_discard():
            self.open_file(self.lib.path)

    def save(self):
        if not self.lib:
            return
        self._save_to(None)

    def save_as(self):
        if not self.lib:
            return
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save library as", self.lib.path,
                                                        "Altium schematic library (*.SchLib)")
        if path:
            self._save_to(path)

    def _save_to(self, path):
        try:
            out = self.lib.save(path)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Save failed", str(exc))
            return
        self.renamed = {}
        self.snapshot()
        self.restyle_all()
        self.update_title()
        self.status.showMessage("Saved %s" % out, 5000)

    def confirm_discard(self):
        if not self.lib or not self.lib.dirty:
            return True
        r = QtWidgets.QMessageBox.question(self, "Unsaved changes", "Save changes before continuing?",
                                           QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard
                                           | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            self.save()
            return not self.lib.dirty
        return r == QtWidgets.QMessageBox.Discard

    def closeEvent(self, ev):
        if self.confirm_discard():
            ev.accept()
        else:
            ev.ignore()

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        for url in ev.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(".schlib") and self.confirm_discard():
                self.open_file(p)
                break

    # ------------------------------------------------------------------ table building
    def row_order(self):
        order = list(range(len(self.lib.components)))
        if self.sort_rows:
            order.sort(key=lambda i: self.lib.components[i].lib_reference.lower())
        return order

    def cell_value(self, comp, col):
        """Current value of a cell from the library; None = parameter missing."""
        if col in tbl.FIXED_ATTRS:
            return getattr(comp, tbl.FIXED_ATTRS[col])
        if col == "Footprints":
            return "; ".join(comp.footprints)
        p = comp.find_param(col)
        return None if p is None else p.value

    def build_table(self):
        self._loading = True
        t = self.table
        t.blockSignals(True)
        try:
            self.columns = FIXED + self.lib.all_param_names("frequency")
            t.clear()
            t.setColumnCount(len(self.columns))
            t.setHorizontalHeaderLabels(self.columns)
            comps = self.lib.components
            order = self.row_order()
            t.setRowCount(len(order))
            for r, ci in enumerate(order):
                comp = comps[ci]
                vh = QtWidgets.QTableWidgetItem(comp.lib_reference)
                vh.setToolTip("%s\nstorage: %s\n%d parts, %d pins" % (comp.description, comp.storage_name,
                                                                        comp.part_count, comp.pin_count))
                t.setVerticalHeaderItem(r, vh)
                for c, col in enumerate(self.columns):
                    item = QtWidgets.QTableWidgetItem()
                    item.setData(ROLE_COMP, ci)
                    value = self.cell_value(comp, col)
                    item.setData(ROLE_MISSING, value is None)
                    item.setText(value or "")
                    if col == "Footprints":
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    t.setItem(r, c, item)
            t.resizeColumnsToContents()
            for c in range(t.columnCount()):
                t.setColumnWidth(c, min(max(t.columnWidth(c), 70), 320))
        finally:
            t.blockSignals(False)
            self._loading = False
        self.apply_filter(self.filter.text())
        self.update_status()

    def snapshot(self):
        self.orig = {}
        for ci, comp in enumerate(self.lib.components):
            for col in self.columns:
                self.orig[(ci, col)] = self.cell_value(comp, col)

    def restyle_all(self):
        t = self.table
        t.blockSignals(True)
        try:
            for r in range(t.rowCount()):
                for c in range(t.columnCount()):
                    self.style_item(t.item(r, c))
        finally:
            t.blockSignals(False)
        self.update_status()

    def style_item(self, item):
        if item is None:
            return
        ci = item.data(ROLE_COMP)
        col = self.columns[item.column()]
        missing = bool(item.data(ROLE_MISSING))
        value = None if missing else item.text()
        changed = (value != self.orig.get((ci, col))) or col in self.renamed
        font = item.font()
        font.setBold(bool(changed))
        item.setFont(font)
        if col == "Footprints":
            item.setBackground(QtGui.QBrush(COLOR_READONLY))
            item.setToolTip("footprint models (read-only)")
        elif missing:
            item.setBackground(QtGui.QBrush(COLOR_MISSING, Qt.BDiagPattern))
            item.setToolTip("no parameter %r on this component - type a value to create it" % col)
        elif changed:
            item.setBackground(QtGui.QBrush(COLOR_CHANGED))
            old = self.orig.get((ci, col))
            item.setToolTip("changed, was: %s" % ("<missing>" if old is None else repr(old)))
        else:
            item.setBackground(QtGui.QBrush())
            item.setToolTip("")

    def set_item_state(self, item, value):
        """Update one cell after the library changed (value None = parameter missing)."""
        self.table.blockSignals(True)
        try:
            item.setData(ROLE_MISSING, value is None)
            item.setText(value or "")
            self.style_item(item)
        finally:
            self.table.blockSignals(False)

    def refresh_row(self, row):
        for c in range(self.table.columnCount()):
            item = self.table.item(row, c)
            comp = self.lib.components[item.data(ROLE_COMP)]
            self.set_item_state(item, self.cell_value(comp, self.columns[c]))

    # ------------------------------------------------------------------ editing
    def on_item_changed(self, item):
        if self._loading or self.lib is None:
            return
        ci = item.data(ROLE_COMP)
        col = self.columns[item.column()]
        comp = self.lib.components[ci]
        text = item.text()
        try:
            if col in tbl.FIXED_ATTRS:
                setattr(comp, tbl.FIXED_ATTRS[col], text)
            elif col == "Footprints":
                return
            else:
                comp.set_param(col, text)
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid value", str(exc))
            self.set_item_state(item, self.cell_value(comp, col))
            return
        self.set_item_state(item, self.cell_value(comp, col))
        self.update_status()
        self.update_title()
        self.refresh_details()

    def selected_items(self, editable_only=True):
        items = []
        for idx in self.table.selectedIndexes():
            it = self.table.item(idx.row(), idx.column())
            if it is None or self.table.isRowHidden(idx.row()):
                continue
            if editable_only and not (it.flags() & Qt.ItemIsEditable):
                continue
            items.append(it)
        return items

    def clear_selection(self):
        for it in self.selected_items():
            if not it.data(ROLE_MISSING) and it.text() != "":
                it.setText("")

    def fill_down(self):
        for rng in self.table.selectedRanges():
            for c in range(rng.leftColumn(), rng.rightColumn() + 1):
                top = self.table.item(rng.topRow(), c)
                if top is None:
                    continue
                value = top.text()
                for r in range(rng.topRow() + 1, rng.bottomRow() + 1):
                    if not self.table.isRowHidden(r):
                        self.table.set_cell(r, c, value)

    def create_in_selection(self):
        n = 0
        for it in self.selected_items():
            col = self.columns[it.column()]
            if it.data(ROLE_MISSING) and col not in FIXED:
                comp = self.lib.components[it.data(ROLE_COMP)]
                comp.set_param(col, "")
                self.set_item_state(it, "")
                n += 1
        self.after_bulk_change("%d parameter(s) created" % n)

    def remove_param_selected(self):
        targets = [it for it in self.selected_items() if self.columns[it.column()] not in FIXED
                   and not it.data(ROLE_MISSING)]
        if not targets:
            return
        names = sorted({self.columns[it.column()] for it in targets})
        if QtWidgets.QMessageBox.question(
                self, "Remove parameter", "Remove %d parameter value(s) (%s) from the selected components?"
                % (len(targets), ", ".join(names))) != QtWidgets.QMessageBox.Yes:
            return
        for it in targets:
            comp = self.lib.components[it.data(ROLE_COMP)]
            comp.delete_param(self.columns[it.column()])
            self.set_item_state(it, None)
        self.after_bulk_change("%d parameter(s) removed" % len(targets))

    def revert_selection(self):
        n = 0
        for it in self.selected_items():
            ci, col = it.data(ROLE_COMP), self.columns[it.column()]
            comp = self.lib.components[ci]
            orig = self.orig.get((ci, col))
            cur = None if it.data(ROLE_MISSING) else it.text()
            if orig == cur:
                continue
            try:
                if col in tbl.FIXED_ATTRS:
                    setattr(comp, tbl.FIXED_ATTRS[col], orig or "")
                elif orig is None:
                    comp.delete_param(col)
                else:
                    comp.set_param(col, orig)
            except asl.SchLibError as exc:
                QtWidgets.QMessageBox.warning(self, "Cannot revert", str(exc))
                continue
            self.set_item_state(it, self.cell_value(comp, col))
            n += 1
        self.after_bulk_change("%d cell(s) reverted" % n)

    def after_bulk_change(self, msg):
        self.update_status()
        self.update_title()
        self.refresh_details()
        self.status.showMessage(msg, 4000)

    # ------------------------------------------------------------------ columns
    def param_columns(self):
        return [c for c in self.columns if c not in FIXED]

    def current_param_column(self):
        item = self.table.currentItem()
        if item is None:
            return None
        col = self.columns[item.column()]
        return col if col not in FIXED else None

    def add_column(self):
        if not self.lib:
            return
        dlg = AddColumnDialog(self)
        if dlg.exec_() != QtWidgets.QDialog.Accepted:
            return
        name = dlg.name.text().strip()
        if not name:
            return
        if name.lower() in {c.lower() for c in self.columns}:
            QtWidgets.QMessageBox.information(self, "Add column", "Column %r already exists" % name)
            return
        try:
            if dlg.create_all.isChecked():
                for comp in self.lib.components:
                    comp.set_param(name, dlg.value.text())
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Add column", str(exc))
        self.columns.append(name)
        self.rebuild_keep_changes()

    def rename_column(self):
        col = self.current_param_column()
        if col is None:
            QtWidgets.QMessageBox.information(self, "Rename column", "Select a cell in a parameter column first")
            return
        new, ok = QtWidgets.QInputDialog.getText(self, "Rename parameter", "New name for %r:" % col, text=col)
        new = new.strip()
        if not ok or not new or new == col:
            return
        if new.lower() != col.lower() and new.lower() in {c.lower() for c in self.columns}:
            QtWidgets.QMessageBox.information(self, "Rename column", "Column %r already exists" % new)
            return
        try:
            for comp in self.lib.components:
                comp.rename_param(col, new)
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Rename column", str(exc))
        origin = self.renamed.pop(col, col)
        self.renamed[new] = origin
        for ci in range(len(self.lib.components)):
            if (ci, col) in self.orig:
                self.orig[(ci, new)] = self.orig.pop((ci, col))
        self.columns[self.columns.index(col)] = new
        self.rebuild_keep_changes()

    def delete_column(self):
        col = self.current_param_column()
        if col is None:
            QtWidgets.QMessageBox.information(self, "Delete column", "Select a cell in a parameter column first")
            return
        n = sum(1 for c in self.lib.components if c.find_param(col))
        if QtWidgets.QMessageBox.question(
                self, "Delete column", "Remove parameter %r from %d component(s)?" % (col, n)) != QtWidgets.QMessageBox.Yes:
            return
        for comp in self.lib.components:
            comp.delete_param(col)
        self.rebuild_keep_changes()

    def rebuild_keep_changes(self):
        """Rebuild the grid without losing the 'changed' highlighting."""
        current = self.lib.all_param_names("frequency")
        known = [c for c in self.columns if c in FIXED or c.lower() in {n.lower() for n in current}
                 or c not in FIXED]
        extra = [n for n in current if n.lower() not in {c.lower() for c in known}]
        wanted = known + extra
        self._loading = True
        self.table.blockSignals(True)
        try:
            self.columns = wanted
            self.table.clear()
            self.table.setColumnCount(len(self.columns))
            self.table.setHorizontalHeaderLabels(self.columns)
            order = self.row_order()
            self.table.setRowCount(len(order))
            for r, ci in enumerate(order):
                comp = self.lib.components[ci]
                vh = QtWidgets.QTableWidgetItem(comp.lib_reference)
                self.table.setVerticalHeaderItem(r, vh)
                for c, col in enumerate(self.columns):
                    item = QtWidgets.QTableWidgetItem()
                    item.setData(ROLE_COMP, ci)
                    value = self.cell_value(comp, col)
                    item.setData(ROLE_MISSING, value is None)
                    item.setText(value or "")
                    if col == "Footprints":
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                    self.table.setItem(r, c, item)
                    self.style_item(item)
            self.table.resizeColumnsToContents()
            for c in range(self.table.columnCount()):
                self.table.setColumnWidth(c, min(max(self.table.columnWidth(c), 70), 320))
        finally:
            self.table.blockSignals(False)
            self._loading = False
        self.apply_filter(self.filter.text())
        self.update_status()
        self.update_title()

    # ------------------------------------------------------------------ excel
    def export_xlsx(self):
        if not self.lib:
            return
        default = os.path.splitext(self.lib.path)[0] + "_params.xlsx"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export table", default,
                                                        "Excel (*.xlsx);;CSV (*.csv);;JSON (*.json)")
        if not path:
            return
        try:
            tbl.write_table(path, self.lib)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Export failed", str(exc))
            return
        self.status.showMessage("Exported %s" % path, 5000)

    def import_table(self):
        if not self.lib:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Import table", os.path.dirname(self.lib.path),
                                                        "Tables (*.xlsx *.csv);;All files (*)")
        if not path:
            return
        try:
            columns, rows = tbl.read_table(path)
            changes, warnings = tbl.plan_changes(self.lib, columns, rows)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Import failed", str(exc))
            return
        if not changes:
            QtWidgets.QMessageBox.information(self, "Import", "The table contains no changes.\n" + "\n".join(warnings))
            return
        preview = "\n".join(str(ch) for ch in changes[:40])
        if len(changes) > 40:
            preview += "\n... and %d more" % (len(changes) - 40)
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Import")
        box.setText("Apply %d change(s) from %s?" % (len(changes), os.path.basename(path)))
        box.setDetailedText(preview + ("\n\nWarnings:\n" + "\n".join(warnings) if warnings else ""))
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)
        if box.exec_() != QtWidgets.QMessageBox.Yes:
            return
        try:
            tbl.apply_changes(self.lib, changes)
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Import", "Stopped: %s" % exc)
        self.rebuild_keep_changes()

    # ------------------------------------------------------------------ misc
    def toggle_sort(self):
        self.sort_rows = self.act_sort.isChecked()
        if self.lib:
            self.rebuild_keep_changes()

    def apply_filter(self, text):
        text = text.strip().lower()
        t = self.table
        for r in range(t.rowCount()):
            if not text:
                t.setRowHidden(r, False)
                continue
            vh = t.verticalHeaderItem(r)
            hay = [vh.text() if vh else ""]
            hay += [t.item(r, c).text() for c in range(t.columnCount()) if t.item(r, c)]
            t.setRowHidden(r, text not in " ".join(hay).lower())

    def context_menu(self, pos):
        menu = QtWidgets.QMenu(self)
        menu.addAction("Copy\tCtrl+C", self.table.copy_selection)
        menu.addAction("Paste\tCtrl+V", self.table.paste_clipboard)
        menu.addAction("Fill down\tCtrl+D", self.fill_down)
        menu.addSeparator()
        menu.addAction("Clear value\tDel", self.clear_selection)
        menu.addAction("Create parameter here", self.create_in_selection)
        menu.addAction("Remove parameter here\tCtrl+Del", self.remove_param_selected)
        menu.addAction("Revert", self.revert_selection)
        menu.exec_(self.table.viewport().mapToGlobal(pos))

    def on_current_changed(self, row, col, prev_row, prev_col):
        if row != prev_row:
            self.refresh_details()

    def refresh_details(self):
        item = self.table.currentItem()
        if item is None or self.lib is None:
            self.details.setPlainText("")
            return
        comp = self.lib.components[item.data(ROLE_COMP)]
        try:
            self.details.setPlainText(comp.describe())
        except Exception:                                 # noqa: BLE001
            self.details.setPlainText(traceback.format_exc())

    def update_status(self):
        if not self.lib:
            self.status.showMessage("no library loaded")
            return
        modified = sum(1 for c in self.lib.components if c.dirty)
        self.status.showMessage("%s   |   %d components   |   %d parameter columns   |   %d component(s) modified"
                                % (self.lib.path, len(self.lib.components), len(self.param_columns()), modified))

    def update_title(self):
        name = os.path.basename(self.lib.path) if self.lib else "no file"
        star = "*" if self.lib and self.lib.dirty else ""
        self.setWindowTitle("SchLib Parameter Table Editor - %s%s" % (name, star))


def main():
    app = QtWidgets.QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    win = MainWindow(path)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
