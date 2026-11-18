"""The QR encoder root draws the console's Wi-Fi code with
(os/pi-gen/stage-nox/04-firstboot/files/nox_qr.py): standard library only, so root never runs
code from Nox's own environment. Checked module by module against segno (Nox's dependency for
the pairing QR codes) for every version it supports and every mask."""

from __future__ import annotations

import pytest

from tests.unit.nox_os.conftest import FIRSTBOOT, load_tool

qr = load_tool("nox_qr", FIRSTBOOT)
segno = pytest.importorskip("segno")

#: Bytes that fill each version exactly at level M (byte mode): the symbol then has no pad
#: codewords. segno adds a zero byte before the pad codewords when the bit stream already ends on
#: a codeword boundary (still a valid code, not the canonical one), so only full symbols can be
#: compared module by module; the padding itself is checked against the standard below.
FULL = {1: 14, 2: 26, 3: 42, 4: 62, 5: 84, 6: 106, 7: 122, 8: 152, 9: 180, 10: 213}
TEXT = "WIFI:T:WPA;S:Nox-Setup-1A2B;P:ABCD-EFGH-JKMN-PQRS;;" * 5  # fictitious


@pytest.mark.parametrize("version", sorted(FULL))
def test_every_version_and_mask_matches_segno(version: int) -> None:
    data = TEXT[: FULL[version]]
    assert qr.version_for(len(data)) == version
    for mask in range(8):
        ours = qr.matrix(data.encode(), mask=mask)
        theirs = segno.make(data, error="m", mode="byte", mask=mask, boost_error=False, micro=False)
        assert theirs.version == version
        assert ours == [[bool(m) for m in row] for row in theirs.matrix], (version, mask)


def test_the_data_codewords_are_the_standards() -> None:
    # "hello" in byte mode, version 1-M: mode, count, data, terminator, then 0xEC/0x11 pads.
    words = qr._codewords(b"hello", 1)  # noqa: SLF001
    assert words[:9] == [0x40, 0x56, 0x86, 0x56, 0xC6, 0xC6, 0xF0, 0xEC, 0x11]
    assert len(words) == 26  # 16 data + 10 error-correction codewords


def test_a_mask_is_chosen_and_too_much_data_is_refused() -> None:
    chosen = qr.matrix(TEXT[:51].encode())
    assert len(chosen) == 33 and chosen in [qr.matrix(TEXT[:51].encode(), mask=m) for m in range(8)]
    with pytest.raises(ValueError):
        qr.version_for(214)
