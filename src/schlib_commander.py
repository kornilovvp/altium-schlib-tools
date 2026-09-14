#!/usr/bin/env python3
"""schlib_commander.py -- two-panel (Norton Commander style) browser / editor for Altium .SchLib files.

    python schlib_commander.py [left.SchLib] [right.SchLib]

Two identical panels, one of them active (Tab or a mouse click switches).  Each panel:
    [Open] [Save] [Save as]   <library file>
    left table  = components of the library   (Description / Designator / DesignItemId editable in place)
    right table = parameters of the current component (Name / Value editable in place)

DOS-style keys (every key also has a button with an icon in the bottom bar):
    F1 help   F2 save   F3 open library   F4 add parameter   F5 copy to other panel   F6 move to other panel
    F7 rename   F8 delete   F10 quit   Tab switch panel   Insert mark/unmark component (Ctrl/Shift+click also work)
F5/F6/F7/F8 act on components when the component table has the focus and on parameters when the
parameter table has the focus.  Yellow cell = changed since load; bold component = modified.
Saving writes a .bak copy next to the file.
"""
import os
import sys

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import altium_schlib as asl

COMP_COLUMNS = ["#", "LibReference", "Description", "Designator", "DesignItemId", "Footprints", "Params"]
COMP_EDITABLE = {"Description": "description", "Designator": "designator", "DesignItemId": "design_item_id"}
PARAM_COLUMNS = ["Name", "Value"]
ROLE_INDEX = Qt.UserRole          # component index / parameter index stored on every item

COLOR_CHANGED = QtGui.QColor(255, 244, 150)
COLOR_READONLY = QtGui.QColor(236, 236, 236)

TITLE_ACTIVE = "QFrame#title { background: #0a246a; } QLabel { color: #ffff55; font-weight: bold; }"
TITLE_INACTIVE = "QFrame#title { background: #5a5a5a; } QLabel { color: #d0d0d0; }"
FKEY_STYLE = ("QPushButton { background: #00aaaa; color: #000000; border: none; padding: 3px 10px; "
              "font-family: Consolas, 'Courier New', monospace; font-weight: bold; } "
              "QPushButton:hover { background: #33cccc; } QPushButton:disabled { color: #446666; }")

HELP_TEXT = """<b>SchLib Commander</b><br><br>
<table cellpadding=3>
<tr><td><b>Tab</b></td><td>switch between the left and the right panel (or click into a panel)</td></tr>
<tr><td><b>F1</b></td><td>this help</td></tr>
<tr><td><b>F2 / Ctrl+S</b></td><td>save the library of the active panel (a .bak copy is kept)</td></tr>
<tr><td><b>F3</b></td><td>open a library in the active panel</td></tr>
<tr><td><b>F4</b></td><td>add a parameter to the current component</td></tr>
<tr><td><b>F5</b></td><td>copy marked components (or parameters) to the other panel</td></tr>
<tr><td><b>F6</b></td><td>move marked components to the other panel (copy, then delete here)</td></tr>
<tr><td><b>F7</b></td><td>rename the current component (or parameter)</td></tr>
<tr><td><b>F8 / Delete</b></td><td>delete marked components (or the current parameter)</td></tr>
<tr><td><b>F10</b></td><td>quit</td></tr>
<tr><td><b>Insert</b></td><td>mark / unmark the current component and go to the next one</td></tr>
<tr><td><b>Ctrl+click, Shift+click</b></td><td>mark several components with the mouse</td></tr>
<tr><td><b>Enter / double-click</b></td><td>edit the cell in place</td></tr>
</table><br>
Copy / move / rename / delete act on <b>components</b> when the component table has the focus and on
<b>parameters</b> when the parameter table has the focus.<br>
Yellow cell = changed since the library was loaded, bold name = modified component."""


def glyph_icon(text: str, fg="#000000", bg="#ffffff", size=18) -> QtGui.QIcon:
    """Small text glyph rendered as an icon (works without any icon theme)."""
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtGui.QColor(bg))
    painter = QtGui.QPainter(pm)
    painter.setRenderHint(QtGui.QPainter.Antialiasing)
    font = QtGui.QFont("Consolas")
    font.setBold(True)
    font.setPixelSize(size - 4 if len(text) == 1 else size - 8)
    painter.setFont(font)
    painter.setPen(QtGui.QColor(fg))
    painter.drawText(pm.rect(), Qt.AlignCenter, text)
    painter.end()
    return QtGui.QIcon(pm)


class SortItem(QtWidgets.QTableWidgetItem):
    """Table item that sorts numbers numerically and text case-insensitively (header click sorting)."""

    def __lt__(self, other):
        a, b = self.text(), other.text()
        try:
            return int(a) < int(b)
        except ValueError:
            return a.lower() < b.lower()


def footprint_text(comp) -> str:
    return "; ".join(name + (" @ " + lib if lib else "") for name, lib in comp.footprint_links)


class PanelTable(QtWidgets.QTableWidget):
    """Table that reports focus, turns Tab into 'switch panel' and Insert/Delete into panel actions."""

    focused = QtCore.pyqtSignal()
    switchRequested = QtCore.pyqtSignal()
    insertRequested = QtCore.pyqtSignal()
    deleteRequested = QtCore.pyqtSignal()

    def focusInEvent(self, ev):
        super().focusInEvent(ev)
        self.focused.emit()

    def keyPressEvent(self, ev):
        key = ev.key()
        editing = self.state() == QtWidgets.QAbstractItemView.EditingState
        if key in (Qt.Key_Tab, Qt.Key_Backtab) and not editing:
            self.switchRequested.emit()
            return
        if key == Qt.Key_Insert and not editing:
            self.insertRequested.emit()
            return
        if key == Qt.Key_Delete and not editing:
            self.deleteRequested.emit()
            return
        super().keyPressEvent(ev)


class LibraryPanel(QtWidgets.QWidget):
    activated = QtCore.pyqtSignal(object)
    switchRequested = QtCore.pyqtSignal()
    changed = QtCore.pyqtSignal()
    componentAction = QtCore.pyqtSignal(str)      # "delete" (from the Delete key)

    def __init__(self, side: str, parent=None):
        super().__init__(parent)
        self.side = side
        self.lib = None
        self.comp = None                 # current component
        self.current_params = []         # Parameter objects shown in the params table
        self.orig_comp = {}              # (id(component), column) -> loaded value
        self.orig_param = {}             # id(record) -> (name, value) at load time
        self.focus_kind = "comps"        # which table had the focus last: "comps" | "params"
        self._loading = False
        self._build_ui()
        self.set_active(False)

    # ------------------------------------------------------------------ ui
    def _build_ui(self):
        style = self.style()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(2)

        title = QtWidgets.QFrame()
        title.setObjectName("title")
        self.title = title
        tl = QtWidgets.QHBoxLayout(title)
        tl.setContentsMargins(6, 3, 6, 3)
        self.btn_open = QtWidgets.QPushButton(style.standardIcon(QtWidgets.QStyle.SP_DialogOpenButton), "Open library...")
        self.btn_save = QtWidgets.QPushButton(style.standardIcon(QtWidgets.QStyle.SP_DialogSaveButton), "Save")
        self.btn_save_as = QtWidgets.QPushButton("Save as...")
        for b in (self.btn_open, self.btn_save, self.btn_save_as):
            b.setFocusPolicy(Qt.NoFocus)
        self.btn_open.clicked.connect(self.open_dialog)
        self.btn_save.clicked.connect(self.save)
        self.btn_save_as.clicked.connect(self.save_as)
        self.lbl_file = QtWidgets.QLabel("%s panel - no library" % self.side)
        # 'Ignored' lets the label be narrower than its text, otherwise a long path blocks resizing
        self.lbl_file.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        for w in (self.btn_open, self.btn_save, self.btn_save_as):
            tl.addWidget(w)
        tl.addWidget(self.lbl_file, 1)
        layout.addWidget(title)

        split = QtWidgets.QSplitter(Qt.Horizontal)
        self.comps = PanelTable(0, len(COMP_COLUMNS))
        self.comps.setHorizontalHeaderLabels(COMP_COLUMNS)
        self.comps.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.comps.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.comps.verticalHeader().setDefaultSectionSize(22)
        self.comps.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                   | QtWidgets.QAbstractItemView.EditKeyPressed)
        self.comps.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)   # library order by default
        self.comps.currentCellChanged.connect(self.on_comp_current_changed)
        self.comps.itemChanged.connect(self.on_comp_item_changed)
        self.comps.itemSelectionChanged.connect(self.update_labels)
        self.comps.focused.connect(lambda: self._focus("comps"))
        self.comps.switchRequested.connect(self.switchRequested)
        self.comps.insertRequested.connect(self.toggle_mark)
        self.comps.deleteRequested.connect(lambda: self.componentAction.emit("delete"))

        self.params = PanelTable(0, len(PARAM_COLUMNS))
        self.params.setHorizontalHeaderLabels(PARAM_COLUMNS)
        self.params.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.params.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        self.params.verticalHeader().setDefaultSectionSize(22)
        self.params.setEditTriggers(QtWidgets.QAbstractItemView.DoubleClicked
                                    | QtWidgets.QAbstractItemView.EditKeyPressed
                                    | QtWidgets.QAbstractItemView.AnyKeyPressed)
        self.params.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)
        self.params.itemChanged.connect(self.on_param_item_changed)
        self.params.focused.connect(lambda: self._focus("params"))
        self.params.switchRequested.connect(self.switchRequested)
        self.params.insertRequested.connect(self.add_param)
        self.params.deleteRequested.connect(self.delete_params)
        self.params.horizontalHeader().setStretchLastSection(True)

        split.addWidget(self.comps)
        split.addWidget(self.params)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)

        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setStyleSheet("color: #444; padding: 1px 4px;")
        self.lbl_status.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)
        layout.addWidget(self.lbl_status)

    def _focus(self, kind):
        self.focus_kind = kind
        self.activated.emit(self)

    def set_active(self, active: bool):
        self.title.setStyleSheet(TITLE_ACTIVE if active else TITLE_INACTIVE)

    def focus_table(self):
        (self.params if self.focus_kind == "params" else self.comps).setFocus()

    def is_dirty(self) -> bool:
        return bool(self.lib and self.lib.dirty)

    # ------------------------------------------------------------------ file handling
    def confirm_discard(self) -> bool:
        if not self.is_dirty():
            return True
        r = QtWidgets.QMessageBox.question(
            self, "Unsaved changes", "%s panel: save changes to %s?" % (self.side, os.path.basename(self.lib.path)),
            QtWidgets.QMessageBox.Save | QtWidgets.QMessageBox.Discard | QtWidgets.QMessageBox.Cancel)
        if r == QtWidgets.QMessageBox.Save:
            self.save()
            return not self.is_dirty()
        return r == QtWidgets.QMessageBox.Discard

    def open_dialog(self):
        self.activated.emit(self)
        if not self.confirm_discard():
            return
        start = os.path.dirname(self.lib.path) if self.lib else ""
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open schematic library", start,
                                                        "Altium schematic library (*.SchLib);;All files (*)")
        if path:
            self.open_library(path)

    def open_library(self, path: str):
        try:
            lib = asl.SchLib.load(path)
        except Exception as exc:                          # noqa: BLE001
            QtWidgets.QMessageBox.critical(self, "Cannot open", "%s\n\n%s" % (path, exc))
            return
        self.lib = lib
        self.comp = None
        self.reset_baseline()
        self.load_components()
        self.update_labels()
        self.changed.emit()
        if lib.warnings:
            QtWidgets.QMessageBox.warning(self, "Warnings", "\n".join(lib.warnings))

    def reset_baseline(self):
        self.orig_comp = {}
        self.orig_param = {}
        for c in self.lib.components:
            for col, attr in COMP_EDITABLE.items():
                self.orig_comp[(id(c), col)] = getattr(c, attr)
            for p in c.params:
                self.orig_param[id(p.record)] = (p.name, p.value)

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
        self.reset_baseline()
        self.restyle_components()
        cur = self.param_at_row(self.params.currentRow())
        self.load_params(keep_name=cur.name if cur else None)
        self.update_labels()
        self.changed.emit()
        self.lbl_status.setText("Saved %s" % out)

    # ------------------------------------------------------------------ components table
    def comp_at_row(self, row):
        """Component shown in a table row (rows move when the table is sorted)."""
        if self.lib is None or row < 0 or row >= self.comps.rowCount():
            return None
        item = self.comps.item(row, 0)
        if item is None:
            return None
        idx = item.data(ROLE_INDEX)
        return self.lib.components[idx] if 0 <= idx < len(self.lib.components) else None

    def row_of_comp(self, comp):
        for r in range(self.comps.rowCount()):
            if self.comp_at_row(r) is comp:
                return r
        return -1

    def _current_sort(self, table):
        h = table.horizontalHeader()
        sec = h.sortIndicatorSection()
        return (sec, h.sortIndicatorOrder()) if 0 <= sec < table.columnCount() else (0, Qt.AscendingOrder)

    def load_components(self, select=None):
        """Rebuild the component table; `select` = component (or table row) to make current."""
        t = self.comps
        prev = self.comp
        sort_col, sort_order = self._current_sort(t)
        self._loading = True
        t.blockSignals(True)
        t.setSortingEnabled(False)
        try:
            t.setRowCount(0)
            t.setRowCount(len(self.lib.components))
            for r, c in enumerate(self.lib.components):
                values = [str(r), c.lib_reference, c.description, c.designator, c.design_item_id,
                          footprint_text(c), str(len(c.params))]
                for col, v in enumerate(values):
                    item = SortItem(v)
                    item.setData(ROLE_INDEX, r)
                    if COMP_COLUMNS[col] not in COMP_EDITABLE:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                        item.setBackground(QtGui.QBrush(COLOR_READONLY))
                    t.setItem(r, col, item)
            t.sortItems(sort_col, sort_order)
            t.resizeColumnsToContents()
            for col in range(t.columnCount()):
                t.setColumnWidth(col, min(max(t.columnWidth(col), 40 if col == 0 else 60), 320))
        finally:
            t.setSortingEnabled(True)
            t.blockSignals(False)
            self._loading = False
        self.restyle_components()
        row = -1
        if isinstance(select, int):
            row = select
        elif select is not None:
            row = self.row_of_comp(select)
        if row < 0 and prev is not None:
            row = self.row_of_comp(prev)
        if row < 0 and t.rowCount():
            row = 0
        row = min(row, t.rowCount() - 1)
        if row >= 0:
            t.setCurrentCell(row, 1)
            self.on_comp_current_changed(row, 1, -1, -1)
        else:
            self.comp = None
            self.load_params()
        self.update_labels()

    def restyle_components(self):
        t = self.comps
        t.blockSignals(True)
        t.setSortingEnabled(False)
        try:
            for r in range(t.rowCount()):
                comp = self.comp_at_row(r)
                if comp is None:
                    continue
                for col, name in enumerate(COMP_COLUMNS):
                    item = t.item(r, col)
                    if item is None:
                        continue
                    if name in COMP_EDITABLE:
                        key = (id(comp), name)
                        changed = key not in self.orig_comp or item.text() != self.orig_comp[key]
                        item.setBackground(QtGui.QBrush(COLOR_CHANGED) if changed else QtGui.QBrush())
                    elif name == "Params":
                        item.setText(str(len(comp.params)))
                    elif name == "Footprints":
                        item.setText(footprint_text(comp))
                ref = t.item(r, 1)
                font = ref.font()
                font.setBold(comp.dirty)
                ref.setFont(font)
                ref.setBackground(QtGui.QBrush(COLOR_CHANGED) if (id(comp), "Description") not in self.orig_comp
                                  else QtGui.QBrush(COLOR_READONLY))
        finally:
            t.setSortingEnabled(True)
            t.blockSignals(False)

    def on_comp_current_changed(self, row, col, prev_row, prev_col):
        if self._loading or self.lib is None:
            return
        comp = self.comp_at_row(row)
        if comp is self.comp and prev_row == row:
            return
        self.comp = comp
        self.load_params()
        self.update_labels()

    def on_comp_item_changed(self, item):
        if self._loading or self.lib is None:
            return
        name = COMP_COLUMNS[item.column()]
        if name not in COMP_EDITABLE:
            return
        idx = item.data(ROLE_INDEX)
        comp = self.lib.components[idx] if 0 <= idx < len(self.lib.components) else None
        if comp is None:
            return
        try:
            setattr(comp, COMP_EDITABLE[name], item.text())
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid value", str(exc))
            self.comps.blockSignals(True)
            item.setText(getattr(comp, COMP_EDITABLE[name]))
            self.comps.blockSignals(False)
        self.restyle_components()
        row = self.row_of_comp(comp)
        if row >= 0 and self.comps.currentRow() != row:
            self.comps.setCurrentCell(row, item.column())
        self.update_labels()
        self.changed.emit()

    def toggle_mark(self):
        """Insert key: mark / unmark the current component and move to the next one (NC style)."""
        t = self.comps
        row = t.currentRow()
        if row < 0:
            return
        sm = t.selectionModel()
        sm.select(t.model().index(row, 0), QtCore.QItemSelectionModel.Toggle | QtCore.QItemSelectionModel.Rows)
        if row + 1 < t.rowCount():
            t.setCurrentCell(row + 1, t.currentColumn(), QtCore.QItemSelectionModel.NoUpdate)

    def selected_components(self):
        """Marked components, or the current one when nothing is marked."""
        if not self.lib:
            return []
        rows = sorted({i.row() for i in self.comps.selectionModel().selectedRows()})
        comps = [c for c in (self.comp_at_row(r) for r in rows) if c is not None]
        if not comps and self.comp is not None:
            return [self.comp]
        return comps

    def param_at_row(self, row):
        if row < 0 or row >= self.params.rowCount():
            return None
        item = self.params.item(row, 0)
        if item is None:
            return None
        idx = item.data(ROLE_INDEX)
        return self.current_params[idx] if 0 <= idx < len(self.current_params) else None

    def selected_params(self):
        rows = sorted({i.row() for i in self.params.selectionModel().selectedRows()})
        params = [p for p in (self.param_at_row(r) for r in rows) if p is not None]
        if not params:
            cur = self.param_at_row(self.params.currentRow())
            params = [cur] if cur is not None else []
        return params

    # ------------------------------------------------------------------ parameters table
    def load_params(self, keep_name=None):
        """Rebuild the parameter table; `keep_name` = parameter to keep current."""
        t = self.params
        sort_col, sort_order = self._current_sort(t)
        t.blockSignals(True)
        t.setSortingEnabled(False)
        try:
            t.setRowCount(0)
            self.current_params = self.comp.params if self.comp else []
            t.setRowCount(len(self.current_params))
            for r, p in enumerate(self.current_params):
                orig = self.orig_param.get(id(p.record))
                for col, text in enumerate((p.name, p.value)):
                    item = SortItem(text)
                    item.setData(ROLE_INDEX, r)
                    changed = orig is None or orig[col] != text
                    if changed:
                        item.setBackground(QtGui.QBrush(COLOR_CHANGED))
                        item.setToolTip("new parameter" if orig is None else "was: %r" % orig[col])
                    t.setItem(r, col, item)
            t.sortItems(sort_col, sort_order)
            t.resizeColumnToContents(0)
            t.setColumnWidth(0, min(max(t.columnWidth(0), 120), 300))
        finally:
            t.setSortingEnabled(True)
            t.blockSignals(False)
        if keep_name is not None:
            for r in range(t.rowCount()):
                p = self.param_at_row(r)
                if p is not None and p.name.lower() == keep_name.lower():
                    t.setCurrentCell(r, 1)
                    break

    def on_param_item_changed(self, item):
        if self.lib is None or self.comp is None:
            return
        idx = item.data(ROLE_INDEX)
        if idx is None or idx >= len(self.current_params):
            return
        p = self.current_params[idx]
        text = item.text()
        keep = p.name
        try:
            if item.column() == 0:
                if text.strip() == "":
                    raise asl.SchLibError("parameter name may not be empty")
                self.comp.rename_param(p.name, text)
                keep = text
            else:
                p.value = text
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid value", str(exc))
        self.load_params(keep_name=keep)
        self.restyle_components()
        self.update_labels()
        self.changed.emit()

    def add_param(self):
        if self.comp is None:
            return
        name, ok = QtWidgets.QInputDialog.getText(self, "Add parameter",
                                                  "Parameter name for %s:" % self.comp.lib_reference)
        name = name.strip()
        if not ok or not name:
            return
        if self.comp.find_param(name) is not None:
            QtWidgets.QMessageBox.information(self, "Add parameter", "Parameter %r already exists" % name)
            return
        try:
            self.comp.set_param(name, "")
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Add parameter", str(exc))
            return
        self.load_params(keep_name=name)
        row = self.params.currentRow()
        if row >= 0:
            self.params.setFocus()
            self.params.editItem(self.params.item(row, 1))
        self.restyle_components()
        self.update_labels()
        self.changed.emit()

    def rename_param(self):
        p = self.param_at_row(self.params.currentRow())
        if self.comp is None or p is None:
            return
        new, ok = QtWidgets.QInputDialog.getText(self, "Rename parameter", "New name for %r:" % p.name, text=p.name)
        new = new.strip()
        if not ok or not new or new == p.name:
            return
        try:
            self.comp.rename_param(p.name, new)
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Rename parameter", str(exc))
            new = p.name
        self.load_params(keep_name=new)
        self.restyle_components()
        self.update_labels()
        self.changed.emit()

    def delete_params(self):
        if self.comp is None:
            return
        params = self.selected_params()
        if not params:
            return
        names = ", ".join(p.name for p in params)
        if QtWidgets.QMessageBox.question(
                self, "Delete parameter", "Delete %d parameter(s) (%s) from %s?" % (len(params), names, self.comp.lib_reference)
        ) != QtWidgets.QMessageBox.Yes:
            return
        row = self.params.currentRow()
        for p in params:
            self.comp.delete_param(p.name)
        self.load_params()
        if self.params.rowCount():
            self.params.setCurrentCell(min(max(row, 0), self.params.rowCount() - 1), 1)
        self.restyle_components()
        self.update_labels()
        self.changed.emit()

    # ------------------------------------------------------------------ labels
    def update_labels(self):
        if not self.lib:
            self.lbl_file.setText("%s panel - no library" % self.side)
            self.lbl_status.setText("")
            return
        star = " *" if self.lib.dirty else ""
        self.lbl_file.setText("%s panel - %s%s" % (self.side, os.path.basename(self.lib.path), star))
        self.lbl_file.setToolTip(self.lib.path)
        modified = sum(1 for c in self.lib.components if c.dirty)
        marked = len(self.comps.selectionModel().selectedRows())
        cur = ""
        if self.comp is not None:
            cur = "   |   %s: %d parameters, %d pins, footprints: %s" % (
                self.comp.lib_reference, len(self.comp.params), self.comp.pin_count,
                footprint_text(self.comp) or "-")
        self.lbl_status.setText("%d components, %d marked, %d modified%s" % (
            len(self.lib.components), marked, modified, cur))


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, left_path=None, right_path=None):
        super().__init__()
        self.setWindowTitle("SchLib Commander")
        self.resize(1600, 850)
        self.left = LibraryPanel("Left")
        self.right = LibraryPanel("Right")
        self.panels = [self.left, self.right]
        self.active = self.left

        central = QtWidgets.QWidget()
        vl = QtWidgets.QVBoxLayout(central)
        vl.setContentsMargins(4, 4, 4, 2)
        split = QtWidgets.QSplitter(Qt.Horizontal)
        split.addWidget(self.left)
        split.addWidget(self.right)
        split.setSizes([800, 800])
        vl.addWidget(split, 1)
        vl.addWidget(self._build_fkey_bar())
        self.setCentralWidget(central)

        for p in self.panels:
            p.activated.connect(self.set_active)
            p.switchRequested.connect(self.switch_panel)
            p.changed.connect(self.update_title)
            p.componentAction.connect(self.on_component_action)
        self.set_active(self.left)

        for keys, slot in [(("F1",), self.cmd_help), (("F2", "Ctrl+S"), self.cmd_save), (("F3",), self.cmd_open),
                           (("F4",), self.cmd_add), (("F5",), self.cmd_copy), (("F6",), self.cmd_move),
                           (("F7",), self.cmd_rename), (("F8",), self.cmd_delete), (("F10",), self.close)]:
            for k in keys:
                QtWidgets.QShortcut(QtGui.QKeySequence(k), self, slot)

        if left_path:
            self.left.open_library(left_path)
        if right_path:
            self.right.open_library(right_path)
        self.left.focus_table()

    def _build_fkey_bar(self):
        style = self.style()
        std = style.standardIcon
        bar = QtWidgets.QFrame()
        bar.setStyleSheet("QFrame { background: #000000; }")
        hl = QtWidgets.QHBoxLayout(bar)
        hl.setContentsMargins(4, 2, 4, 2)
        hl.setSpacing(6)
        self.fkey_buttons = {}
        for key, text, icon, slot, tip in [
                ("F1", "Help", std(QtWidgets.QStyle.SP_DialogHelpButton), self.cmd_help, "Show the key reference"),
                ("F2", "Save", std(QtWidgets.QStyle.SP_DialogSaveButton), self.cmd_save, "Save the active panel's library"),
                ("F3", "Open", std(QtWidgets.QStyle.SP_DialogOpenButton), self.cmd_open, "Open a library in the active panel"),
                ("F4", "AddPar", glyph_icon("+", "#006600"), self.cmd_add, "Add a parameter to the current component"),
                ("F5", "Copy", std(QtWidgets.QStyle.SP_ArrowRight), self.cmd_copy, "Copy marked components / parameters to the other panel"),
                ("F6", "Move", std(QtWidgets.QStyle.SP_ArrowForward), self.cmd_move, "Move marked components to the other panel"),
                ("F7", "Rename", glyph_icon("ab", "#000088"), self.cmd_rename, "Rename the current component / parameter"),
                ("F8", "Delete", std(QtWidgets.QStyle.SP_TrashIcon), self.cmd_delete, "Delete marked components / parameters"),
                ("Tab", "Switch", glyph_icon("<>", "#000000"), self.switch_panel, "Switch the active panel"),
                ("F10", "Quit", std(QtWidgets.QStyle.SP_DialogCloseButton), self.close, "Quit")]:
            lbl = QtWidgets.QLabel(key)
            lbl.setStyleSheet("color: #ffffff; font-family: Consolas, 'Courier New', monospace; font-weight: bold;")
            btn = QtWidgets.QPushButton(icon, text)
            btn.setStyleSheet(FKEY_STYLE)
            btn.setFocusPolicy(Qt.NoFocus)
            btn.setToolTip("%s (%s)" % (tip, key))
            btn.clicked.connect(slot)
            hl.addWidget(lbl)
            hl.addWidget(btn)
            self.fkey_buttons[key] = btn
        hl.addStretch(1)
        return bar

    # ------------------------------------------------------------------ panel switching
    @property
    def other(self):
        return self.right if self.active is self.left else self.left

    def set_active(self, panel):
        self.active = panel
        for p in self.panels:
            p.set_active(p is panel)
        std = self.style().standardIcon
        to_right = panel is self.left
        self.fkey_buttons["F5"].setIcon(std(QtWidgets.QStyle.SP_ArrowRight if to_right else QtWidgets.QStyle.SP_ArrowLeft))
        self.fkey_buttons["F6"].setIcon(std(QtWidgets.QStyle.SP_ArrowForward if to_right else QtWidgets.QStyle.SP_ArrowBack))
        self.update_title()

    def switch_panel(self):
        other = self.other
        self.set_active(other)
        other.focus_table()

    def update_title(self):
        name = os.path.basename(self.active.lib.path) if self.active.lib else "no library"
        star = "*" if self.active.is_dirty() else ""
        self.setWindowTitle("SchLib Commander - %s panel: %s%s" % (self.active.side, name, star))

    def on_component_action(self, action):
        if action == "delete":
            self.cmd_delete()

    # ------------------------------------------------------------------ commands on the active panel
    def cmd_help(self):
        QtWidgets.QMessageBox.information(self, "Keys", HELP_TEXT)

    def cmd_save(self):
        self.active.save()
        self.update_title()

    def cmd_open(self):
        self.active.open_dialog()
        self.update_title()

    def cmd_add(self):
        self.active.add_param()
        self.update_title()

    def cmd_rename(self):
        src = self.active
        if not src.lib:
            return
        if src.focus_kind == "params":
            src.rename_param()
            return
        comp = src.comp
        if comp is None:
            return
        new, ok = QtWidgets.QInputDialog.getText(self, "Rename component", "New name for %r:" % comp.lib_reference,
                                                 text=comp.lib_reference)
        new = new.strip()
        if not ok or not new or new == comp.lib_reference:
            return
        try:
            src.lib.rename_component(comp, new)
        except asl.SchLibError as exc:
            QtWidgets.QMessageBox.warning(self, "Rename component", str(exc))
            return
        src.load_components(select=comp)
        src.changed.emit()
        self.update_title()

    def cmd_delete(self):
        src = self.active
        if not src.lib:
            return
        if src.focus_kind == "params":
            src.delete_params()
            return
        comps = src.selected_components()
        if not comps:
            return
        names = "\n".join(c.lib_reference for c in comps[:15]) + ("\n..." if len(comps) > 15 else "")
        if QtWidgets.QMessageBox.question(
                self, "Delete components", "Delete %d component(s) from %s?\n\n%s"
                % (len(comps), os.path.basename(src.lib.path), names)) != QtWidgets.QMessageBox.Yes:
            return
        row = src.comps.currentRow()
        for c in comps:
            src.lib.remove_component(c)
        src.load_components(select=row)
        src.changed.emit()
        src.lbl_status.setText("%d component(s) deleted" % len(comps))
        self.update_title()

    def cmd_copy(self):
        self._transfer(move=False)

    def cmd_move(self):
        self._transfer(move=True)

    def _ask_conflict(self, name, dst_name):
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Component exists")
        box.setIcon(QtWidgets.QMessageBox.Question)
        box.setText("Component %r already exists in %s." % (name, dst_name))
        buttons = {}
        for text, key in [("Overwrite", "overwrite"), ("Overwrite all", "overwrite_all"), ("Rename...", "rename"),
                          ("Skip", "skip"), ("Skip all", "skip_all"), ("Cancel", "cancel")]:
            buttons[box.addButton(text, QtWidgets.QMessageBox.ActionRole)] = key
        box.exec_()
        return buttons.get(box.clickedButton(), "cancel")

    def _transfer(self, move: bool):
        src, dst = self.active, self.other
        verb = "Move" if move else "Copy"
        if not src.lib:
            return
        if not dst.lib:
            QtWidgets.QMessageBox.information(self, verb, "Open a library in the %s panel first." % dst.side)
            return
        if os.path.normcase(src.lib.path) == os.path.normcase(dst.lib.path):
            QtWidgets.QMessageBox.warning(self, verb, "Both panels show the same file. Open a different library "
                                          "in one of the panels (or use Save as first).")
            return
        if src.focus_kind == "params":
            self._transfer_params(src, dst)
            return
        comps = src.selected_components()
        if not comps:
            return
        names = "\n".join(c.lib_reference for c in comps[:15]) + ("\n..." if len(comps) > 15 else "")
        if QtWidgets.QMessageBox.question(
                self, verb, "%s %d component(s) to %s?\n\n%s" % (verb, len(comps), os.path.basename(dst.lib.path), names)
        ) != QtWidgets.QMessageBox.Yes:
            return
        policy = None
        done = []
        last = None
        for comp in comps:
            name = comp.lib_reference
            existing = dst.lib.component(name)
            if existing is not None:
                choice = policy or self._ask_conflict(name, os.path.basename(dst.lib.path))
                if choice == "cancel":
                    break
                if choice in ("skip", "skip_all"):
                    policy = "skip_all" if choice == "skip_all" else policy
                    continue
                if choice == "rename":
                    new, ok = QtWidgets.QInputDialog.getText(self, "Rename copy", "New name for the copy:", text=name + "_copy")
                    new = new.strip()
                    if not ok or not new or dst.lib.component(new) is not None:
                        continue
                    name = new
                else:
                    policy = "overwrite_all" if choice == "overwrite_all" else policy
                    dst.lib.remove_component(existing)
            try:
                last = dst.lib.add_component(comp, name)
            except asl.SchLibError as exc:
                QtWidgets.QMessageBox.warning(self, verb, str(exc))
                continue
            done.append(comp)
        if move:
            row = src.comps.currentRow()
            for comp in done:
                src.lib.remove_component(comp)
            src.load_components(select=row)
        else:
            src.comps.clearSelection()
        dst.load_components(select=last)
        src.changed.emit()
        dst.changed.emit()
        src.lbl_status.setText("%s: %d component(s) -> %s panel" % (verb, len(done), dst.side))
        self.update_title()

    def _transfer_params(self, src, dst):
        if src.comp is None or dst.comp is None:
            QtWidgets.QMessageBox.information(self, "Copy parameters", "Select a component in both panels first.")
            return
        params = src.selected_params()
        if not params:
            return
        n = 0
        for p in params:
            try:
                dst.comp.set_param(p.name, p.value)
                n += 1
            except asl.SchLibError as exc:
                QtWidgets.QMessageBox.warning(self, "Copy parameters", str(exc))
        dst.load_params()
        dst.restyle_components()
        dst.update_labels()
        dst.changed.emit()
        src.lbl_status.setText("%d parameter(s) copied to %s" % (n, dst.comp.lib_reference))
        self.update_title()

    def closeEvent(self, ev):
        for p in self.panels:
            if not p.confirm_discard():
                ev.ignore()
                return
        ev.accept()


def main():
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    win = MainWindow(args[0] if args else None, args[1] if len(args) > 1 else None)
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
