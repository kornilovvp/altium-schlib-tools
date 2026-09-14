"""Round-trip / write tests for altium_schlib.   python tests/test_roundtrip.py [Libs/MC3.SchLib]"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import olefile  # noqa: E402
import altium_schlib as asl  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "Libs", "MC3.SchLib")
tmp = tempfile.mkdtemp(prefix="schlib_test_")

# 1. parse + lossless re-encode of every text record and of the FileHeader
lib = asl.SchLib.load(SRC)
print(lib, "warnings:", lib.warnings)
assert lib.components, "no components parsed"
n_text = n_pins = 0
for comp in lib.components:
    for rec in comp.records:
        if rec.is_text:
            assert asl.encode_text_record(asl.parse_text_record(rec.raw)) == rec.raw, (comp.lib_reference, rec.raw)
            n_text += 1
        else:
            d = asl.decode_pin(rec.raw)
            assert "error" not in d and d["_consumed"], (comp.lib_reference, d)
            n_pins += 1
    assert len(comp.pins) == comp.pin_count, (comp.lib_reference, len(comp.pins), comp.pin_count)
    assert comp.serialize_data() == lib.streams[(comp.storage_name, "Data")], comp.lib_reference
assert asl.encode_text_record(asl.parse_text_record(lib.header.raw)) == lib.header.raw
assert int(lib.header.get("Weight")) == lib.total_records() + 1
print("lossless: %d text records, %d pins re-encoded identically" % (n_text, n_pins))
print("parameter names:", lib.all_param_names())

# 2. modify + save to a new file
work = os.path.join(tmp, "work.SchLib")
shutil.copyfile(SRC, work)
lib = asl.SchLib.load(work)
c0, c1, c2 = lib.components[0], lib.components[1], lib.components[2]
ref0, ref1, ref2 = c0.lib_reference, c1.lib_reference, c2.lib_reference
c0.set_param("Manufacturer", "Test Manufacturer")                # existing
c0.set_param("Operating Temperature", "-40°C ~ 125°C (тест)")     # non-ascii -> %UTF8%
c0.set_param("New Param", "новое значение")                       # add
c0.set_param("Empty Param", "")                                  # add empty
assert c0.delete_param("Publisher")                               # delete
assert c0.rename_param("Supplier 3", "Supplier 3 renamed")
c0.description = "Changed description"
c0.design_item_id = ref0
c0.designator = "Q?"
c1.set_param("Value", "")                                         # empty an existing one
c1.set_param("Comment", "=Value")
c2_footprints = c2.footprints
out = os.path.join(tmp, "out.SchLib")
lib.save(out)
assert not lib.dirty
assert os.path.exists(out)

# 3. reload and verify
lib2 = asl.SchLib.load(out)
assert not lib2.warnings, lib2.warnings
d0 = lib2.component(ref0)
assert d0.get_param("Manufacturer") == "Test Manufacturer"
assert d0.get_param("Operating Temperature") == "-40°C ~ 125°C (тест)"
assert d0.get_param("New Param") == "новое значение"
assert d0.find_param("Empty Param") is not None and d0.get_param("Empty Param") == ""
assert d0.find_param("Publisher") is None
assert d0.find_param("Supplier 3") is None and d0.get_param("Supplier 3 renamed") == "jlcpcb"
assert d0.description == "Changed description"
assert lib2.header.get("CompDescr%d" % d0.index) == "Changed description"
assert d0.design_item_id == ref0 and d0.designator == "Q?"
assert lib2.component(ref1).get_param("Value") == ""
assert lib2.component(ref1).get_param("Comment") == "=Value"
assert lib2.component(ref2).footprints == c2_footprints
assert int(lib2.header.get("Weight")) == lib2.total_records() + 1
# positional references still consistent
for comp in lib2.components:
    for i, rec in enumerate(comp.records):
        if not rec.is_text:
            continue
        if rec.record_id == 45:
            assert comp.records[int(rec.get("OwnerIndex"))].record_id == 44, (comp.lib_reference, i)
        if rec.record_id in (46, 48):
            assert comp.records[int(rec.get("OwnerIndex"))].record_id == 45, (comp.lib_reference, i)
        iis = rec.get("IndexInSheet")
        if iis is not None and int(iis) >= 0:
            assert int(iis) == i - 1, (comp.lib_reference, i, iis)
    assert len(comp.pins) == comp.pin_count
# raw check of the utf8 layout in the rewritten record
raw = d0.find_param("Operating Temperature").record.raw
assert (b"|%UTF8%Text=-40\xc2\xb0C ~ 125\xc2\xb0C (\xd1\x82\xd0\xb5\xd1\x81\xd1\x82)"
        b"|||Text=-40\xb0C ~ 125\xb0C (????)|Name=") in raw, raw
# untouched streams identical
a, b = olefile.OleFileIO(SRC), olefile.OleFileIO(out)
changed = []
for e in a.listdir(streams=True, storages=False):
    if a.openstream(e).read() != b.openstream(e).read():
        changed.append("/".join(e))
a.close()
b.close()
expected = sorted(["FileHeader", c0.storage_name + "/Data", c1.storage_name + "/Data"])
assert sorted(changed) == expected, (changed, expected)
print("changed streams:", changed)

# 4. save in place with backup
lib3 = asl.SchLib.load(out)
lib3.component(ref0).set_param("Manufacturer", "In-place")
lib3.save()
assert os.path.exists(out + ".bak")
assert asl.SchLib.load(out).component(ref0).get_param("Manufacturer") == "In-place"
assert asl.SchLib.load(out + ".bak").component(ref0).get_param("Manufacturer") == "Test Manufacturer"

# 5. invalid input rejected
try:
    lib3.component(ref0).set_param("Bad", "a|b")
    raise AssertionError("pipe accepted")
except asl.SchLibError:
    pass
print("ALL TESTS PASSED  (temp dir %s)" % tmp)
