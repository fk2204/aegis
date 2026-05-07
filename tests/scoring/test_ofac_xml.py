"""Test the SDN.XML parser against a small fixture.

Treasury's actual SDN.XML uses a default namespace; the parser strips
namespaces before matching tag names so the same fixture exercises the
production code path.
"""

from __future__ import annotations

from aegis.scoring.ofac import parse_sdn_xml

_FIXTURE_NS = b"""<?xml version="1.0" encoding="utf-8"?>
<sdnList xmlns="http://tempuri.org/sdnList.xsd">
  <sdnEntry>
    <uid>1001</uid>
    <firstName>Vladimir</firstName>
    <lastName>Putin</lastName>
    <sdnType>Individual</sdnType>
    <akaList>
      <aka>
        <uid>9001</uid>
        <category>strong</category>
        <firstName>Vlad</firstName>
        <lastName>Putin</lastName>
      </aka>
      <aka>
        <uid>9002</uid>
        <category>strong</category>
        <firstName>Vladimir</firstName>
        <lastName>Vladimirovich Putin</lastName>
      </aka>
    </akaList>
  </sdnEntry>
  <sdnEntry>
    <uid>1002</uid>
    <lastName>Sanctioned Front Co</lastName>
    <sdnType>Entity</sdnType>
    <akaList></akaList>
  </sdnEntry>
  <sdnEntry>
    <uid>1003</uid>
    <sdnType>Individual</sdnType>
  </sdnEntry>
</sdnList>"""


_FIXTURE_NO_NS = b"""<?xml version="1.0" encoding="utf-8"?>
<sdnList>
  <sdnEntry>
    <uid>2001</uid>
    <firstName>Jane</firstName>
    <lastName>Sanction</lastName>
    <sdnType>Individual</sdnType>
  </sdnEntry>
</sdnList>"""


def test_parse_with_namespace() -> None:
    entries = parse_sdn_xml(_FIXTURE_NS)
    # 3 sdnEntries in fixture but the third has no name → skipped.
    assert len(entries) == 2

    putin = entries[0]
    assert putin.primary_name == "Putin, Vladimir"
    assert "Putin, Vlad" in putin.aliases
    assert "Vladimirovich Putin, Vladimir" in putin.aliases

    entity = entries[1]
    assert entity.primary_name == "Sanctioned Front Co"
    assert entity.aliases == ()


def test_parse_without_namespace() -> None:
    entries = parse_sdn_xml(_FIXTURE_NO_NS)
    assert len(entries) == 1
    assert entries[0].primary_name == "Sanction, Jane"


def test_parse_invalid_xml_raises() -> None:
    import pytest

    from aegis.scoring.ofac import OFACFetchError

    with pytest.raises(OFACFetchError, match="parse failed"):
        parse_sdn_xml(b"<not xml")
