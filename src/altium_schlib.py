#!/usr/bin/env python3
"""altium_schlib.py -- read, modify and write Altium Designer *.SchLib files.

Reverse-engineered file structure (verified byte-for-byte on real libraries):

  OLE2 Compound Document (D0 CF 11 E0)
    FileHeader             one text record:
                             |HEADER=Protel for Windows - Schematic Library Editor Binary File Version 5.0
                             |Weight=<total record count in all Data streams + 1>|...|CompCount=N
                             |LibRef0=..|CompDescr0=..|PartCount0=..|LibRef1=..|...
    Storage                |HEADER=Icon storage  (embedded pictures, kept verbatim)
    SectionKeys            optional  |KeyCount=n|LibRef0=..|SectionKey0=..  (LibRef -> storage name
                           when the name is longer than 31 chars or contains illegal characters)
    <component>/Data       the component itself: a sequence of records (see below)
    <component>/PinFrac    per-pin fractional coordinates      (zlib blobs, kept verbatim)
    <component>/PinTextData, PinWideText, PinSymbolLineWidth ... (kept verbatim)

  Record  = 4-byte header + payload.  header = uint24 little-endian payload length, uint8 type
    type 0  text record  "|KEY=VALUE|KEY=VALUE...\\0"
                         non-ASCII values are written twice:  |%UTF8%KEY=<utf-8>|||KEY=<cp1252>
                         boolean true = "T", default values (0, empty, false) are simply omitted
    type 1  binary record, only used for pins (RECORD=2), see decode_pin()

  Record 0 of Data is RECORD=1 (component header: LibReference, ComponentDescription, DesignItemId,
  PartCount, AllPinCount ...).  Then, in Altium's own order:
    RECORD=41 parameters (Name=, Text=)   <- what the parameter table edits
    graphics (6 polyline, 12 arc, 13 line, 14 rectangle, 4 label ...) and binary pins
    RECORD=34 designator, RECORD=41 Comment, RECORD=44 implementation list,
    RECORD=45 model (footprint, ModelName=), RECORD=46 / 48 model children.

  Cross references inside Data are *positional*:
    OwnerIndex   = index of the owning record inside Data   (45 -> 44, 46/48 -> 45)
    IndexInSheet = own index - 1  (or -1 for designator / comment / models)
  so inserting or deleting a record requires renumbering, which Component.serialize_data() does.

Writing uses the Windows Structured Storage API (pywin32) on a copy of the original file, which is the
same implementation Altium itself uses to read the file; only modified streams are rewritten.
"""
from __future__ import annotations

import os
import random
import re
import shutil
import string
import struct
from typing import Dict, Iterator, List, Optional, Tuple

import olefile

__version__ = "0.1.0"
__all__ = [
    "SchLib", "Component", "Parameter", "Record", "Fields", "SchLibError",
    "TEXT_RECORD", "BINARY_RECORD", "iter_records", "parse_text_record", "encode_text_record",
    "pack_record", "decode_pin", "RECORD_NAMES", "ELECTRICAL_TYPES",
]

TEXT_RECORD = 0
BINARY_RECORD = 1

ELECTRICAL_TYPES = {0: "Input", 1: "I/O", 2: "Output", 3: "Open Collector", 4: "Passive",
                    5: "HiZ", 6: "Open Emitter", 7: "Power"}

RECORD_NAMES = {
    1: "Component", 2: "Pin", 3: "IEEE Symbol", 4: "Label", 5: "Bezier", 6: "Polyline", 7: "Polygon",
    8: "Ellipse", 9: "Piechart", 10: "Round Rectangle", 11: "Elliptical Arc", 12: "Arc", 13: "Line",
    14: "Rectangle", 28: "Text Frame", 30: "Image", 34: "Designator", 41: "Parameter",
    44: "Implementation List", 45: "Implementation (model)", 46: "Impl. Pin Association",
    47: "Impl. Pin", 48: "Impl. Parameter List",
}

# Key order Altium uses when it writes a parameter record (observed in real files).
PARAM_KEY_ORDER = ["RECORD", "IndexInSheet", "OwnerPartId", "Location.X", "Location.X_Frac",
                   "Location.Y", "Location.Y_Frac", "Justification", "Color", "FontID", "IsHidden",
                   "Text", "Name", "UniqueID"]

ANSI = "cp1252"


class SchLibError(Exception):
    """Any structural problem with a .SchLib file or an invalid edit."""


# ----------------------------------------------------------------------------------------------
# Low level: record framing and text record encoding
# ----------------------------------------------------------------------------------------------

class Fields:
    """Ordered key/value store with case-insensitive keys that keeps the original spelling/order."""

    __slots__ = ("_keys", "_vals", "_pos")

    def __init__(self, pairs=()):
        self._keys: List[str] = []
        self._vals: List[str] = []
        self._pos: Dict[str, int] = {}
        for k, v in pairs:
            self[k] = v

    def _reindex(self):
        self._pos = {k.lower(): i for i, k in enumerate(self._keys)}

    def __len__(self):
        return len(self._keys)

    def __iter__(self):
        return iter(self._keys)

    def __contains__(self, key):
        return key.lower() in self._pos

    def __getitem__(self, key):
        i = self._pos.get(key.lower())
        if i is None:
            raise KeyError(key)
        return self._vals[i]

    def get(self, key, default=None):
        i = self._pos.get(key.lower())
        return default if i is None else self._vals[i]

    def __setitem__(self, key, value):
        i = self._pos.get(key.lower())
        if i is None:
            self._pos[key.lower()] = len(self._keys)
            self._keys.append(key)
            self._vals.append(value)
        else:
            self._vals[i] = value

    def __delitem__(self, key):
        i = self._pos.pop(key.lower(), None)
        if i is None:
            raise KeyError(key)
        del self._keys[i]
        del self._vals[i]
        self._reindex()

    def insert_before(self, anchor, key, value):
        """Insert key before `anchor` (or append when the anchor is absent)."""
        if key.lower() in self._pos:
            self[key] = value
            return
        i = self._pos.get(anchor.lower())
        if i is None:
            self[key] = value
            return
        self._keys.insert(i, key)
        self._vals.insert(i, value)
        self._reindex()

    def keys(self):
        return list(self._keys)

    def values(self):
        return list(self._vals)

    def items(self):
        return list(zip(self._keys, self._vals))

    def copy(self):
        return Fields(self.items())

    def to_dict(self):
        return dict(self.items())

    def __repr__(self):
        return "Fields(%r)" % (self.items(),)


def iter_records(data: bytes) -> Iterator[Tuple[int, bytes]]:
    """Yield (type, payload) for every record in a Data / FileHeader stream."""
    off = 0
    n = len(data)
    while off + 4 <= n:
        length = data[off] | (data[off + 1] << 8) | (data[off + 2] << 16)
        rtype = data[off + 3]
        end = off + 4 + length
        if end > n:
            raise SchLibError("truncated record at offset %d (length %d, stream size %d)" % (off, length, n))
        yield rtype, data[off + 4:end]
        off = end
    if off != n:
        raise SchLibError("%d trailing bytes after the last record" % (n - off))


def pack_record(rtype: int, payload: bytes) -> bytes:
    if len(payload) > 0xFFFFFF:
        raise SchLibError("record too long")
    return struct.pack("<I", len(payload) | (rtype << 24)) + payload


def parse_text_record(payload: bytes) -> Fields:
    """'|KEY=VALUE|...\\0' -> Fields.  %UTF8% duplicates are folded into their plain key."""
    body = payload[:-1] if payload.endswith(b"\x00") else payload
    fields = Fields()
    utf8: Dict[str, str] = {}
    for part in body.split(b"|"):
        if not part:
            continue
        k, _, v = part.partition(b"=")
        if k.startswith(b"%UTF8%"):
            utf8[k[6:].decode(ANSI, "replace")] = v.decode("utf-8", "replace")
        else:
            fields[k.decode(ANSI, "replace")] = v.decode(ANSI, "replace")
    for k, v in utf8.items():          # the UTF-8 variant is authoritative
        fields[k] = v
    return fields


def encode_text_record(fields: Fields) -> bytes:
    """Fields -> payload bytes in exactly the layout Altium writes (incl. the %UTF8% doubling)."""
    out = bytearray()
    for key, value in fields.items():
        if value is None:
            continue
        kb = key.encode(ANSI, "replace")
        if any(ord(ch) > 127 for ch in value):
            out += b"|%UTF8%" + kb + b"=" + value.encode("utf-8") + b"||"
        out += b"|" + kb + b"=" + value.encode(ANSI, "replace")
    out += b"\x00"
    return bytes(out)


def new_unique_id() -> str:
    return "".join(random.choices(string.ascii_uppercase, k=8))


def _check_text(value: str, what: str = "value"):
    if not isinstance(value, str):
        raise SchLibError("%s must be a string" % what)
    for bad in ("|", "\x00", "\r", "\n"):
        if bad in value:
            raise SchLibError("%s may not contain %r (Altium record separator)" % (what, bad))


# ----------------------------------------------------------------------------------------------
# Pins (binary RECORD=2)
# ----------------------------------------------------------------------------------------------

def decode_pin(raw: bytes) -> dict:
    """Decode a binary pin record.  Layout (verified against several libraries):

        0      u8    record type = 2
        1..4   u32   unknown (0)
        5..6   i16   OwnerPartId
        7      u8    OwnerPartDisplayMode
        8..11  u8x4  Symbol_InnerEdge, Symbol_OuterEdge, Symbol_Inside, Symbol_Outside
        12     pstr  Description           (u8 length + chars)
        +0     u8    FormalType (1)
        +1     u8    Electrical (see ELECTRICAL_TYPES)
        +2     u8    PinConglomerate: bits0-1 orientation*90, 0x08 name visible, 0x10 designator visible
        +3     i16   PinLength   +5 i16 Location.X   +7 i16 Location.Y   +9 u32 Color
        then   pstr  Name, pstr Designator, and further pascal strings (swap ids etc.)
    """
    d: dict = {"RECORD": 2}
    try:
        if raw[0] != 2:
            return {"RECORD": raw[0], "hex": raw.hex()}
        d["OwnerPartId"] = struct.unpack_from("<h", raw, 5)[0]
        d["OwnerPartDisplayMode"] = raw[7]
        d["Symbol_InnerEdge"], d["Symbol_OuterEdge"], d["Symbol_Inside"], d["Symbol_Outside"] = raw[8:12]
        p = 12
        n = raw[p]
        d["Description"] = raw[p + 1:p + 1 + n].decode(ANSI, "replace")
        p += 1 + n
        d["FormalType"] = raw[p]
        d["Electrical"] = raw[p + 1]
        d["ElectricalName"] = ELECTRICAL_TYPES.get(raw[p + 1], "?")
        congl = raw[p + 2]
        d["PinConglomerate"] = congl
        d["Orientation"] = (congl & 3) * 90
        d["ShowName"] = bool(congl & 0x08)
        d["ShowDesignator"] = bool(congl & 0x10)
        d["PinLength"], d["Location.X"], d["Location.Y"] = struct.unpack_from("<hhh", raw, p + 3)
        d["Color"] = struct.unpack_from("<I", raw, p + 9)[0]
        p += 13
        strings = []
        while p < len(raw):
            n = raw[p]
            strings.append(raw[p + 1:p + 1 + n].decode(ANSI, "replace"))
            p += 1 + n
        d["Name"] = strings[0] if strings else ""
        d["Designator"] = strings[1] if len(strings) > 1 else ""
        d["Extra"] = strings[2:]
        d["_consumed"] = p == len(raw)
    except (IndexError, struct.error) as exc:
        d["error"] = "undecodable pin record: %s" % exc
        d["hex"] = raw.hex()
    return d


# ----------------------------------------------------------------------------------------------
# Records / parameters / components
# ----------------------------------------------------------------------------------------------

class Record:
    """One record of a Data stream.  Unmodified records are written back from their original bytes."""

    __slots__ = ("kind", "raw", "fields", "dirty", "orig_index")

    def __init__(self, kind: int, raw: Optional[bytes] = None, fields: Optional[Fields] = None,
                 orig_index: Optional[int] = None):
        self.kind = kind
        self.raw = raw
        self.fields = fields
        self.dirty = raw is None
        self.orig_index = orig_index
        if kind == TEXT_RECORD and fields is None:
            self.fields = parse_text_record(raw)

    @property
    def is_text(self) -> bool:
        return self.kind == TEXT_RECORD

    @property
    def record_id(self) -> int:
        if self.is_text:
            try:
                return int(self.fields.get("RECORD", "-1"))
            except ValueError:
                return -1
        return self.raw[0] if self.raw else -1

    @property
    def type_name(self) -> str:
        return RECORD_NAMES.get(self.record_id, "RECORD=%d" % self.record_id)

    def get(self, key: str, default=None):
        return self.fields.get(key, default) if self.fields is not None else default

    def set(self, key: str, value: Optional[str]):
        """Set a field (value None removes the key).  Only marks dirty when something changes."""
        if not self.is_text:
            raise SchLibError("binary records are read-only")
        if value is None:
            if key in self.fields:
                del self.fields[key]
                self.dirty = True
        elif self.fields.get(key) != value:
            self.fields[key] = value
            self.dirty = True

    def payload(self) -> bytes:
        if self.dirty or self.raw is None:
            return encode_text_record(self.fields)
        return self.raw

    def to_bytes(self) -> bytes:
        return pack_record(self.kind, self.payload())

    def mark_clean(self):
        self.raw = self.payload()
        self.dirty = False

    def describe(self) -> dict:
        """Human readable dict of the record (pins decoded)."""
        if self.is_text:
            return self.fields.to_dict()
        return decode_pin(self.raw)

    def __repr__(self):
        return "<Record %s %s>" % (self.type_name, "text" if self.is_text else "binary")


def is_component_parameter(rec: Record) -> bool:
    """RECORD=41 owned by the component itself (not by a model)."""
    return rec.is_text and rec.record_id == 41 and rec.get("OwnerIndex", "0") in ("0", "")


class Parameter:
    """A view on one RECORD=41 record of a component."""

    __slots__ = ("component", "record")

    def __init__(self, component: "Component", record: Record):
        self.component = component
        self.record = record

    @property
    def name(self) -> str:
        return self.record.get("Name", "")

    @name.setter
    def name(self, value: str):
        _check_text(value, "parameter name")
        if not value.strip():
            raise SchLibError("parameter name may not be empty")
        self.record.set("Name", value)

    @property
    def value(self) -> str:
        return self.record.get("Text", "")

    @value.setter
    def value(self, value: str):
        _check_text(value, "parameter value")
        if value == "":
            self.record.set("Text", None)          # Altium omits empty values
        elif "Text" in self.record.fields:
            self.record.set("Text", value)
        else:
            self.record.fields.insert_before("Name", "Text", value)
            self.record.dirty = True

    @property
    def hidden(self) -> bool:
        return self.record.get("IsHidden", "") == "T"

    @property
    def unique_id(self) -> str:
        return self.record.get("UniqueID", "")

    def __repr__(self):
        return "<Parameter %s=%r>" % (self.name, self.value)


class Component:
    def __init__(self, lib: "SchLib", index: int, storage_name: str, records: List[Record],
                 extra_streams: Dict[str, bytes]):
        self.lib = lib
        self.index = index                    # position in FileHeader (LibRef{index})
        self.storage_name = storage_name
        self.records = records
        self.extra_streams = extra_streams    # PinFrac, PinTextData ... kept verbatim
        self.structure_changed = False
        if not records or not records[0].is_text or records[0].record_id != 1:
            raise SchLibError("component %r: first record is not RECORD=1" % storage_name)

    # --- header (RECORD=1) -------------------------------------------------------------------
    @property
    def header(self) -> Record:
        return self.records[0]

    @property
    def lib_reference(self) -> str:
        return self.header.get("LibReference", "")

    @property
    def description(self) -> str:
        return self.header.get("ComponentDescription", "")

    @description.setter
    def description(self, value: str):
        _check_text(value, "description")
        self.header.set("ComponentDescription", value or None)
        self.lib.header.set("CompDescr%d" % self.index, value or None)

    @property
    def design_item_id(self) -> str:
        return self.header.get("DesignItemId", "")

    @design_item_id.setter
    def design_item_id(self, value: str):
        _check_text(value, "DesignItemId")
        self.header.set("DesignItemId", value or None)

    @property
    def part_count(self) -> int:
        try:
            return int(self.header.get("PartCount", "1"))
        except ValueError:
            return 1

    @property
    def pin_count(self) -> int:
        try:
            return int(self.header.get("AllPinCount", "0"))
        except ValueError:
            return 0

    # --- designator (RECORD=34) --------------------------------------------------------------
    @property
    def designator_record(self) -> Optional[Record]:
        for r in self.records:
            if r.is_text and r.record_id == 34:
                return r
        return None

    @property
    def designator(self) -> str:
        r = self.designator_record
        return r.get("Text", "") if r else ""

    @designator.setter
    def designator(self, value: str):
        _check_text(value, "designator")
        r = self.designator_record
        if r is None:
            raise SchLibError("component %s has no designator record" % self.lib_reference)
        if value == "":
            r.set("Text", None)
        elif "Text" in r.fields:
            r.set("Text", value)
        else:
            r.fields.insert_before("Name", "Text", value)
            r.dirty = True

    # --- models (RECORD=45) ------------------------------------------------------------------
    @property
    def models(self) -> List[dict]:
        out = []
        for r in self.records:
            if r.is_text and r.record_id == 45:
                out.append({"ModelName": r.get("ModelName", ""), "ModelType": r.get("ModelType", ""),
                            "IsCurrent": r.get("IsCurrent", "") == "T", "Description": r.get("Description", "")})
        return out

    @property
    def footprints(self) -> List[str]:
        return [m["ModelName"] for m in self.models if m["ModelType"].upper() == "PCBLIB"]

    @property
    def footprint_links(self) -> List[Tuple[str, str]]:
        """(footprint name, library restriction) for every PCB model.  Altium keeps the footprint name in
        ModelDatafileEntity0 when the link is 'Any library'; with 'Library name' / 'Library path' the
        library file name / full path is stored there instead, which is what the second element returns
        ('' = any library)."""
        out = []
        for r in self.records:
            if r.is_text and r.record_id == 45 and r.get("ModelType", "").upper() == "PCBLIB":
                name = r.get("ModelName", "")
                entity = r.get("ModelDatafileEntity0", "")
                out.append((name, "" if entity == name else entity))
        return out

    # --- parameters (RECORD=41) --------------------------------------------------------------
    @property
    def params(self) -> List[Parameter]:
        return [Parameter(self, r) for r in self.records if is_component_parameter(r)]

    def find_param(self, name: str) -> Optional[Parameter]:
        name_l = name.lower()
        for p in self.params:
            if p.name.lower() == name_l:
                return p
        return None

    def get_param(self, name: str, default=None):
        p = self.find_param(name)
        return default if p is None else p.value

    def set_param(self, name: str, value: str) -> Parameter:
        """Set a parameter value, creating the parameter when the component lacks it."""
        p = self.find_param(name)
        if p is None:
            return self._add_param(name, value)
        p.value = value
        return p

    def delete_param(self, name: str) -> bool:
        p = self.find_param(name)
        if p is None:
            return False
        self.records.remove(p.record)
        self.structure_changed = True
        return True

    def rename_param(self, old: str, new: str) -> bool:
        p = self.find_param(old)
        if p is None:
            return False
        other = self.find_param(new)
        if other is not None and other.record is not p.record:
            raise SchLibError("%s already has a parameter named %r" % (self.lib_reference, new))
        p.name = new
        return True

    def _first_param_block_end(self) -> int:
        """Index just after the run of parameter records that follows the component header."""
        i = 1
        while i < len(self.records) and is_component_parameter(self.records[i]):
            i += 1
        return i

    def _add_param(self, name: str, value: str) -> Parameter:
        _check_text(name, "parameter name")
        _check_text(value, "parameter value")
        if not name.strip():
            raise SchLibError("parameter name may not be empty")
        idx = self._first_param_block_end()
        template = self.records[idx - 1] if idx > 1 else None
        if template is None:
            for r in self.records:
                if is_component_parameter(r):
                    template = r
                    break
        f = Fields()
        f["RECORD"] = "41"
        if idx - 1 > 0:
            f["IndexInSheet"] = str(idx - 1)
        f["OwnerPartId"] = template.get("OwnerPartId", "-1") if template else "-1"
        for key in ("Location.X", "Location.X_Frac", "Location.Y", "Location.Y_Frac", "Justification",
                    "Color", "FontID"):
            if template is not None and key in template.fields:
                f[key] = template.fields[key]
        if "Color" not in f:
            f["Color"] = "8388608"
        if "FontID" not in f:
            f["FontID"] = "1"
        f["IsHidden"] = "T"
        if value:
            f["Text"] = value
        f["Name"] = name
        f["UniqueID"] = new_unique_id()
        rec = Record(TEXT_RECORD, None, f)
        self.records.insert(idx, rec)
        self.structure_changed = True
        return Parameter(self, rec)

    # --- pins --------------------------------------------------------------------------------
    @property
    def pins(self) -> List[dict]:
        out = []
        for r in self.records:
            if not r.is_text and r.raw and r.raw[0] == 2:
                out.append(decode_pin(r.raw))
            elif r.is_text and r.record_id == 2:
                out.append(r.fields.to_dict())
        return out

    # --- serialization -----------------------------------------------------------------------
    @property
    def dirty(self) -> bool:
        return self.structure_changed or any(r.dirty for r in self.records)

    def _renumber(self):
        """Fix positional references after records were inserted / removed."""
        old_to_new = {r.orig_index: i for i, r in enumerate(self.records) if r.orig_index is not None}
        for new_idx, rec in enumerate(self.records):
            if not rec.is_text:
                continue
            owner = rec.get("OwnerIndex")
            if owner is not None:
                try:
                    o = int(owner)
                except ValueError:
                    o = None
                if o is not None and o in old_to_new and old_to_new[o] != o:
                    rec.set("OwnerIndex", str(old_to_new[o]))
            iis = rec.get("IndexInSheet")
            if iis is not None:
                try:
                    v = int(iis)
                except ValueError:
                    continue
                if v >= 0 and v != new_idx - 1:
                    rec.set("IndexInSheet", str(new_idx - 1))
        for i, rec in enumerate(self.records):
            rec.orig_index = i

    def serialize_data(self) -> bytes:
        self._renumber()
        return b"".join(r.to_bytes() for r in self.records)

    def mark_clean(self):
        for r in self.records:
            r.mark_clean()
        self.structure_changed = False

    def describe(self) -> str:
        """Multi-line text dump of every record (for a details view)."""
        lines = ["%s   [storage %r, %d records, %d parts, %d pins]" % (
            self.lib_reference, self.storage_name, len(self.records), self.part_count, self.pin_count)]
        for i, r in enumerate(self.records):
            d = r.describe()
            if r.is_text:
                body = "  ".join("%s=%s" % (k, v) for k, v in d.items() if k != "RECORD")
                lines.append("%3d  %-24s %s" % (i, r.type_name, body))
            elif "error" in d:
                lines.append("%3d  %-24s %s" % (i, "Pin (binary)", d["error"]))
            else:
                lines.append("%3d  %-24s Designator=%r  Name=%r  %s  %d deg  len=%d  at (%d, %d)  part=%d%s" % (
                    i, "Pin (binary)", d["Designator"], d["Name"], d["ElectricalName"], d["Orientation"],
                    d["PinLength"], d["Location.X"], d["Location.Y"], d["OwnerPartId"],
                    ("  desc=%r" % d["Description"]) if d["Description"] else ""))
        if self.extra_streams:
            lines.append("     extra streams: " + ", ".join(
                "%s (%d bytes)" % (k, len(v)) for k, v in self.extra_streams.items()))
        return "\n".join(lines)

    def __repr__(self):
        return "<Component %s: %d params, %d pins>" % (self.lib_reference, len(self.params), self.pin_count)


# ----------------------------------------------------------------------------------------------
# Library
# ----------------------------------------------------------------------------------------------

COMPONENT_STORAGE_CLSID = "{001BDCD6-EC9E-4077-B6F0-54BAB68DEEAA}"   # seen on every component storage
_INDEXED_HEADER_KEY = re.compile(r"^(LibRef|CompDescr|PartCount)(\d+)$", re.IGNORECASE)


class SchLib:
    def __init__(self):
        self.path: str = ""
        self.streams: Dict[Tuple[str, ...], bytes] = {}
        self.storages: List[str] = []
        self.header: Optional[Record] = None
        self._header_tail: bytes = b""       # any extra records after the first FileHeader record
        self.components: List[Component] = []
        self.warnings: List[str] = []
        self.component_clsid: str = COMPONENT_STORAGE_CLSID
        self._section_keys: Dict[str, str] = {}      # LibRef -> storage name (only when they differ)
        self._section_keys_dirty = False
        self._added: set = set()                      # storage names that do not exist on disk yet
        self._removed: List[str] = []                 # storage names to destroy on save

    # --- loading -----------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str) -> "SchLib":
        self = cls()
        self.path = os.path.abspath(path)
        if not olefile.isOleFile(path):
            raise SchLibError("%s is not an OLE compound file (not a binary .SchLib)" % path)
        ole = olefile.OleFileIO(path)
        try:
            for entry in ole.listdir(streams=True, storages=False):
                self.streams[tuple(entry)] = ole.openstream(entry).read()
            self.storages = [e[0] for e in ole.listdir(streams=False, storages=True) if len(e) == 1]
            for d in ole.direntries:            # CLSID Altium stamps on component storages
                if d is not None and d.entry_type == olefile.STGTY_STORAGE and d.name in self.storages \
                        and d.clsid and d.clsid != "00000000-0000-0000-0000-000000000000":
                    self.component_clsid = "{%s}" % d.clsid
                    break
        finally:
            ole.close()
        if ("FileHeader",) not in self.streams:
            raise SchLibError("no FileHeader stream: not a schematic library")
        records = list(iter_records(self.streams[("FileHeader",)]))
        if not records or records[0][0] != TEXT_RECORD:
            raise SchLibError("FileHeader is not a text record")
        self.header = Record(TEXT_RECORD, records[0][1], orig_index=0)
        self._header_tail = b"".join(pack_record(t, p) for t, p in records[1:])
        if "Schematic Library" not in self.header.get("HEADER", ""):
            self.warnings.append("unexpected HEADER: %r" % self.header.get("HEADER", ""))
        section_keys = self._read_section_keys()
        self._section_keys = dict(section_keys)
        try:
            count = int(self.header.get("CompCount", "0"))
        except ValueError:
            count = 0
        used = set()
        for i in range(count):
            libref = self.header.get("LibRef%d" % i)
            if libref is None:
                self.warnings.append("FileHeader lacks LibRef%d" % i)
                continue
            storage = self._resolve_storage(libref, section_keys)
            if storage is None:
                self.warnings.append("component %r: storage not found, skipped" % libref)
                continue
            used.add(storage)
            data = self.streams.get((storage, "Data"))
            if data is None:
                self.warnings.append("component %r: no Data stream, skipped" % libref)
                continue
            recs = [Record(t, p, orig_index=n) for n, (t, p) in enumerate(iter_records(data))]
            extra = {e[1]: b for e, b in self.streams.items()
                     if len(e) == 2 and e[0] == storage and e[1] != "Data"}
            self.components.append(Component(self, i, storage, recs, extra))
        for s in self.storages:
            if s not in used:
                self.warnings.append("storage %r is not referenced by FileHeader (kept untouched)" % s)
        return self

    def _read_section_keys(self) -> Dict[str, str]:
        raw = self.streams.get(("SectionKeys",))
        keys: Dict[str, str] = {}
        if not raw:
            return keys
        try:
            for t, p in iter_records(raw):
                if t != TEXT_RECORD:
                    continue
                f = parse_text_record(p)
                try:
                    n = int(f.get("KeyCount", "0"))
                except ValueError:
                    n = 0
                for i in range(n):
                    ref, key = f.get("LibRef%d" % i), f.get("SectionKey%d" % i)
                    if ref is not None and key is not None:
                        keys[ref] = key
        except SchLibError as exc:
            self.warnings.append("SectionKeys unreadable: %s" % exc)
        return keys

    def _resolve_storage(self, libref: str, section_keys: Dict[str, str]) -> Optional[str]:
        cands = []
        if libref in section_keys:
            cands.append(section_keys[libref])
        s = libref
        for ch in "/\\:!":
            s = s.replace(ch, "_")
        cands += [libref, s, s[:31]]
        lower = {n.lower(): n for n in self.storages}
        for c in cands:
            if c in self.storages:
                return c
            if c.lower() in lower:
                return lower[c.lower()]
        for n in self.storages:                     # last resort: look inside every Data stream
            data = self.streams.get((n, "Data"))
            if not data:
                continue
            try:
                t, p = next(iter_records(data))
            except (StopIteration, SchLibError):
                continue
            if t == TEXT_RECORD and parse_text_record(p).get("LibReference", "").lower() == libref.lower():
                return n
        return None

    # --- queries -----------------------------------------------------------------------------
    def component(self, libref: str) -> Optional[Component]:
        l = libref.lower()
        for c in self.components:
            if c.lib_reference.lower() == l:
                return c
        for c in self.components:
            if c.storage_name.lower() == l:
                return c
        return None

    def param_name_counts(self) -> Dict[str, int]:
        """{parameter name: number of components having it}, first-seen order, case-insensitive merge."""
        counts: Dict[str, int] = {}
        spelling: Dict[str, str] = {}
        for c in self.components:
            seen = set()
            for p in c.params:
                k = p.name.lower()
                if k in seen:
                    continue
                seen.add(k)
                spelling.setdefault(k, p.name)
                counts[spelling[k]] = counts.get(spelling[k], 0) + 1
        return counts

    def all_param_names(self, sort: str = "frequency") -> List[str]:
        counts = self.param_name_counts()
        names = list(counts)
        if sort == "frequency":
            order = {n: i for i, n in enumerate(names)}
            names.sort(key=lambda n: (-counts[n], order[n]))
        elif sort == "alpha":
            names.sort(key=str.lower)
        return names

    def total_records(self) -> int:
        return sum(len(c.records) for c in self.components)

    @property
    def dirty(self) -> bool:
        return (self.header.dirty or self._section_keys_dirty or bool(self._added) or bool(self._removed)
                or any(c.dirty for c in self.components))

    # --- adding / removing / renaming components ---------------------------------------------
    def _make_storage_name(self, libref: str) -> str:
        """OLE storage name for a component: illegal chars replaced, max 31 chars, unique."""
        s = libref
        for ch in "/\\:!":
            s = s.replace(ch, "_")
        s = s.strip() or "component"
        s = s[:31]
        taken = {c.storage_name.lower() for c in self.components}
        taken |= {n.lower() for n in self.storages if n not in self._removed}
        taken |= {"fileheader", "storage", "sectionkeys"}
        if s.lower() not in taken:
            return s
        base = s[:27]
        for i in range(1, 10000):
            cand = "%s~%d" % (base, i)
            if cand.lower() not in taken:
                return cand
        raise SchLibError("cannot find a free storage name for %r" % libref)

    def _rebuild_header_list(self):
        """Rewrite CompCount / LibRef{i} / CompDescr{i} / PartCount{i} from self.components."""
        items = self.header.fields.items()
        first = next((i for i, (k, _) in enumerate(items) if _INDEXED_HEADER_KEY.match(k)), len(items))
        before = [(k, v) for k, v in items[:first] if not _INDEXED_HEADER_KEY.match(k)]
        after = [(k, v) for k, v in items[first:] if not _INDEXED_HEADER_KEY.match(k)]
        new = Fields(before)
        new["CompCount"] = str(len(self.components))
        for i, c in enumerate(self.components):
            c.index = i
            new["LibRef%d" % i] = c.lib_reference
            if c.description:
                new["CompDescr%d" % i] = c.description
            new["PartCount%d" % i] = c.header.get("PartCount", "1")
        for k, v in after:
            new[k] = v
        if new.items() != items:
            self.header.fields = new
            self.header.dirty = True

    def _register_storage(self, comp: "Component", libref: str):
        comp.storage_name = self._make_storage_name(libref)
        self._added.add(comp.storage_name)
        comp.structure_changed = True
        if comp.storage_name != libref:
            self._section_keys[libref] = comp.storage_name
            self._section_keys_dirty = True

    def add_component(self, source: "Component", new_name: Optional[str] = None) -> "Component":
        """Deep-copy a component (usually from another library) into this library."""
        name = (new_name or source.lib_reference).strip()
        _check_text(name, "component name")
        if not name:
            raise SchLibError("component name may not be empty")
        if self.component(name) is not None:
            raise SchLibError("component %r already exists in %s" % (name, os.path.basename(self.path)))
        source._renumber()                       # make positional references match current positions
        records = []
        for i, r in enumerate(source.records):
            records.append(Record(r.kind, r.payload(), r.fields.copy() if r.is_text else None, orig_index=i))
        comp = Component(self, len(self.components), "", records, dict(source.extra_streams))
        if name != source.lib_reference:
            if comp.design_item_id == source.lib_reference:
                comp.header.set("DesignItemId", name)
            comp.header.set("LibReference", name)
        self._register_storage(comp, name)
        self.components.append(comp)
        self._rebuild_header_list()
        return comp

    def remove_component(self, comp: "Component"):
        if comp not in self.components:
            raise SchLibError("component %r is not in this library" % comp.lib_reference)
        self.components.remove(comp)
        if comp.storage_name in self._added:
            self._added.discard(comp.storage_name)
        else:
            self._removed.append(comp.storage_name)
        if self._section_keys.pop(comp.lib_reference, None) is not None:
            self._section_keys_dirty = True
        self._rebuild_header_list()

    def rename_component(self, comp: "Component", new_name: str):
        new_name = new_name.strip()
        _check_text(new_name, "component name")
        if not new_name:
            raise SchLibError("component name may not be empty")
        other = self.component(new_name)
        if other is not None and other is not comp:
            raise SchLibError("component %r already exists" % new_name)
        old_name = comp.lib_reference
        if comp.storage_name in self._added:
            self._added.discard(comp.storage_name)
        else:
            self._removed.append(comp.storage_name)
        if self._section_keys.pop(old_name, None) is not None:
            self._section_keys_dirty = True
        if comp.design_item_id == old_name:
            comp.header.set("DesignItemId", new_name)
        comp.header.set("LibReference", new_name)
        self._register_storage(comp, new_name)
        self._rebuild_header_list()

    def _section_keys_stream(self) -> bytes:
        f = Fields()
        f["KeyCount"] = str(len(self._section_keys))
        for i, (ref, key) in enumerate(self._section_keys.items()):
            f["LibRef%d" % i] = ref
            f["SectionKey%d" % i] = key
        return pack_record(TEXT_RECORD, encode_text_record(f))

    # --- saving ------------------------------------------------------------------------------
    def save(self, out_path: Optional[str] = None, backup: bool = True) -> str:
        """Write the library.  Without out_path the original file is overwritten (a .bak copy is kept
        when backup=True).  Only modified streams are rewritten; everything else stays byte-identical."""
        try:
            import pythoncom
            from win32com import storagecon as sc
        except ImportError as exc:
            raise SchLibError("saving needs pywin32 (pip install pywin32): %s" % exc)
        src = self.path
        dst = os.path.abspath(out_path) if out_path else src
        if os.path.normcase(dst) == os.path.normcase(src):
            if backup:
                shutil.copyfile(src, src + ".bak")
        else:
            shutil.copyfile(src, dst)
        mode = sc.STGM_READWRITE | sc.STGM_SHARE_EXCLUSIVE

        def write_stream(storage, name, data):
            try:
                strm = storage.OpenStream(name, None, mode, 0)
            except pythoncom.com_error:
                strm = storage.CreateStream(name, mode | sc.STGM_CREATE, 0, 0)
            strm.SetSize(len(data))
            strm.Seek(0, 0)
            strm.Write(data)

        self._update_weight()
        try:
            stg = pythoncom.StgOpenStorage(dst, None, mode, None, 0)
        except pythoncom.com_error as exc:
            raise SchLibError("cannot open %s for writing (is it open in Altium?): %s" % (dst, exc))
        try:
            for name in self._removed:
                try:
                    stg.DestroyElement(name)
                except pythoncom.com_error as exc:
                    raise SchLibError("cannot delete storage %r: %s" % (name, exc))
                for key in [k for k in self.streams if k[0] == name]:
                    del self.streams[key]
                if name in self.storages:
                    self.storages.remove(name)
            for comp in self.components:
                if comp.storage_name in self._added:
                    data = comp.serialize_data()
                    sub = stg.CreateStorage(comp.storage_name, mode | sc.STGM_CREATE, 0, 0)
                    if self.component_clsid:
                        sub.SetClass(pythoncom.MakeIID(self.component_clsid))
                    write_stream(sub, "Data", data)
                    self.streams[(comp.storage_name, "Data")] = data
                    for sname, sdata in comp.extra_streams.items():
                        write_stream(sub, sname, sdata)
                        self.streams[(comp.storage_name, sname)] = sdata
                    sub.Commit(sc.STGC_DEFAULT)
                    sub = None
                    self.storages.append(comp.storage_name)
                elif comp.dirty:
                    data = comp.serialize_data()
                    sub = stg.OpenStorage(comp.storage_name, None, mode, None, 0)
                    write_stream(sub, "Data", data)
                    sub.Commit(sc.STGC_DEFAULT)
                    sub = None
                    self.streams[(comp.storage_name, "Data")] = data
            if self._section_keys_dirty:
                if self._section_keys:
                    data = self._section_keys_stream()
                    write_stream(stg, "SectionKeys", data)
                    self.streams[("SectionKeys",)] = data
                elif ("SectionKeys",) in self.streams:
                    stg.DestroyElement("SectionKeys")
                    del self.streams[("SectionKeys",)]
            if self.header.dirty:
                data = self.header.to_bytes() + self._header_tail
                write_stream(stg, "FileHeader", data)
                self.streams[("FileHeader",)] = data
            stg.Commit(sc.STGC_DEFAULT)
        finally:
            stg = None
        for comp in self.components:
            comp.mark_clean()
        self.header.mark_clean()
        self._added = set()
        self._removed = []
        self._section_keys_dirty = False
        self.path = dst
        return dst

    def _update_weight(self):
        self.header.set("Weight", str(self.total_records() + 1))

    def __repr__(self):
        return "<SchLib %s: %d components>" % (os.path.basename(self.path), len(self.components))


if __name__ == "__main__":       # quick dump:  python altium_schlib.py file.SchLib [LibRef]
    import sys
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lib = SchLib.load(sys.argv[1])
    for w in lib.warnings:
        print("warning:", w)
    if len(sys.argv) > 2:
        print(lib.component(sys.argv[2]).describe())
    else:
        for c in lib.components:
            print("%-30s %-40s %s" % (c.lib_reference, c.description[:40],
                                       ", ".join("%s=%s" % (p.name, p.value) for p in c.params)))
