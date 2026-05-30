"""Tests para las claves de entidad del Sprint 2 (normalize_phone, extract_domain)."""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRAPER_DIR = os.path.abspath(os.path.join(HERE, "..", "scraper"))
if SCRAPER_DIR not in sys.path:
    sys.path.insert(0, SCRAPER_DIR)

from db import normalize_phone, extract_domain  # noqa: E402


# ─── normalize_phone ──────────────────────────────────────────────────

def test_phone_strips_formatting_keeps_plus():
    assert normalize_phone("+34 911 23 45 67") == "+34911234567"

def test_phone_00_prefix_becomes_plus():
    assert normalize_phone("0034 911234567") == "+34911234567"

def test_phone_national_no_plus():
    assert normalize_phone("911 234 567") == "911234567"

def test_phone_parens_and_dashes():
    assert normalize_phone("(033) 1-23-45-67") == "0331234567"

def test_phone_too_short_is_none():
    assert normalize_phone("12345") is None

def test_phone_empty_and_none():
    assert normalize_phone(None) is None
    assert normalize_phone("") is None
    assert normalize_phone("   ") is None


# ─── extract_domain ───────────────────────────────────────────────────

def test_domain_strips_scheme_www_path():
    assert extract_domain("https://www.campingx.com/contacto?ref=1") == "campingx.com"

def test_domain_keeps_subdomain_non_www():
    assert extract_domain("http://reservas.campingx.es/") == "reservas.campingx.es"

def test_domain_aggregator_returns_none():
    # Dominios de agregador agruparían spots NO relacionados → None.
    assert extract_domain("https://www.park4night.com/es/place/12345") is None
    assert extract_domain("https://campercontact.com/x") is None

def test_domain_invalid_or_empty():
    assert extract_domain(None) is None
    assert extract_domain("") is None
    assert extract_domain("notaurl") is None  # sin punto → None
