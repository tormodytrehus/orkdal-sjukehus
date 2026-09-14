from xml.etree import ElementTree as ET

from monitor import Item, build_rss, clean_url, keyword_matches, stable_id


def test_keyword_matching_is_case_insensitive():
    assert keyword_matches("NYTT VED ORKDAL SJUKEHUS", ["Orkdal sjukehus"]) == ["Orkdal sjukehus"]


def test_tracking_parameters_do_not_change_id():
    a = "https://example.no/sak?utm_source=x&id=1"
    b = "https://example.no/sak?id=1"
    assert clean_url(a) == b
    assert stable_id("x", a) == stable_id("x", b)


def test_rss_is_valid_xml():
    cfg = {"feed": {"title": "T", "description": "D", "site_url": "https://example.no"}}
    item = Item("1", "Sak", "https://example.no/sak", "Kilde", "kilde",
                "2026-01-01T10:00:00+01:00", "2026-01-01T10:00:00+00:00", "Ingress", ["Orkdal"])
    root = ET.fromstring(build_rss(cfg, [item]))
    assert root.tag == "rss"
    assert root.findtext("./channel/item/guid") == "1"

