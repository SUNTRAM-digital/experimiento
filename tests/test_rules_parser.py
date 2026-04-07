"""
Tests: rules_parser.py (Fase 4 - Lawyer's Edge)
Prueba el parser de reglas de resolucion, extraccion ICAO y detector de boundary zone.
Todos unitarios — sin APIs.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from rules_parser import (
    extract_icao_from_title,
    extract_data_type,
    extract_unit,
    extract_bucket_thresholds,
    parse_market_rules,
    detect_boundary_zone,
    format_rules_for_analyst,
)


# ── extract_icao_from_title ────────────────────────────────────────────────────

class TestExtractICAO:
    def test_new_york_maps_to_klga(self):
        assert extract_icao_from_title("Will the high temperature in New York exceed 85°F?") == "KLGA"

    def test_chicago_maps_to_kord(self):
        assert extract_icao_from_title("Chicago high temperature on July 4th") == "KORD"

    def test_dallas_maps_to_kdal_not_dfw(self):
        # Dallas debe ser KDAL (Love Field), no KDFW
        assert extract_icao_from_title("Will Dallas reach 95°F?") == "KDAL"

    def test_houston_maps_to_khou(self):
        assert extract_icao_from_title("Houston temperature above 90°F") == "KHOU"

    def test_explicit_icao_in_title(self):
        assert extract_icao_from_title("KLGA high temperature above 80°F") == "KLGA"

    def test_unknown_city_returns_none(self):
        assert extract_icao_from_title("Will it rain in Narnia?") is None

    def test_london_maps_to_egll(self):
        assert extract_icao_from_title("London high temperature exceed 75°F") == "EGLL"

    def test_nyc_alias(self):
        assert extract_icao_from_title("NYC high temperature today") == "KLGA"

    def test_los_angeles_maps_to_klax(self):
        assert extract_icao_from_title("Los Angeles temperature above 80°F") == "KLAX"

    def test_miami_maps_to_kmia(self):
        assert extract_icao_from_title("Miami high temperature exceed 90°F") == "KMIA"


# ── extract_data_type ──────────────────────────────────────────────────────────

class TestExtractDataType:
    def test_detects_intraday_high(self):
        assert extract_data_type("Will the HIGH temperature exceed 90°F?") == "intraday_high"

    def test_detects_maximum(self):
        assert extract_data_type("Maximum temperature above 85°F") == "intraday_high"

    def test_detects_low(self):
        assert extract_data_type("Will the LOW temperature be below 32°F?") == "intraday_low"

    def test_detects_average(self):
        assert extract_data_type("Average temperature for the day") == "daily_average"

    def test_unknown_returns_unknown(self):
        assert extract_data_type("Will temperature be 90°F?") == "unknown"


# ── extract_unit ───────────────────────────────────────────────────────────────

class TestExtractUnit:
    def test_fahrenheit_symbol(self):
        assert extract_unit("High temperature above 90°F") == "F"

    def test_celsius_symbol(self):
        assert extract_unit("Temperature above 32°C") == "C"

    def test_fahrenheit_word(self):
        assert extract_unit("Temperature above 90 Fahrenheit") == "F"

    def test_celsius_word(self):
        assert extract_unit("Temperature above 30 Celsius") == "C"

    def test_no_unit_returns_unknown(self):
        assert extract_unit("Will the high be above 85 degrees?") == "unknown"


# ── extract_bucket_thresholds ─────────────────────────────────────────────────

class TestExtractBucketThresholds:
    def test_above_pattern(self):
        result = extract_bucket_thresholds("Will the high exceed 90°F?")
        assert result is not None
        assert result["type"] == "above"
        assert result["low"] == 90.0

    def test_below_pattern(self):
        result = extract_bucket_thresholds("Will the high be below 70°F?")
        assert result is not None
        assert result["type"] == "below"
        assert result["high"] == 70.0

    def test_range_pattern(self):
        result = extract_bucket_thresholds("High temperature between 85-90°F?")
        assert result is not None
        assert result["type"] == "range"
        assert result["low"] == 85.0
        assert result["high"] == 90.0

    def test_at_least_pattern(self):
        result = extract_bucket_thresholds("Will it be at least 85°F?")
        assert result is not None
        assert result["type"] == "above"

    def test_or_higher_pattern(self):
        result = extract_bucket_thresholds("90°F or higher?")
        assert result is not None
        assert result["type"] == "above"

    def test_no_temp_returns_none(self):
        result = extract_bucket_thresholds("Will it rain tomorrow?")
        assert result is None


# ── parse_market_rules ─────────────────────────────────────────────────────────

class TestParseMarketRules:
    def test_full_parse_returns_all_fields(self):
        rules = parse_market_rules("Will the high temperature in New York exceed 90°F?")
        assert "icao" in rules
        assert "city" in rules
        assert "data_type" in rules
        assert "unit" in rules
        assert "bucket" in rules
        assert "warnings" in rules
        assert "confidence" in rules

    def test_high_confidence_when_all_detected(self):
        rules = parse_market_rules("Will the high temperature in New York exceed 90°F?")
        assert rules["confidence"] == "high"
        assert rules["icao"] == "KLGA"
        assert rules["unit"] == "F"
        assert rules["data_type"] == "intraday_high"

    def test_warning_for_unknown_city(self):
        rules = parse_market_rules("Will it be 90°F tomorrow?")
        assert any("ICAO" in w for w in rules["warnings"])

    def test_low_confidence_when_nothing_detected(self):
        rules = parse_market_rules("Will it be warm tomorrow?")
        assert rules["confidence"] in ("low", "medium")

    def test_no_false_dfw_for_dallas(self):
        rules = parse_market_rules("Will Dallas high exceed 95°F?")
        assert rules["icao"] == "KDAL"


# ── detect_boundary_zone ───────────────────────────────────────────────────────

class TestDetectBoundaryZone:
    def test_in_boundary_zone_when_close_to_limit(self):
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(91.0, 3.0, bucket)
        assert result["in_boundary_zone"] is True

    def test_not_in_zone_when_far_from_limit(self):
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(85.0, 3.0, bucket)
        assert result["in_boundary_zone"] is False

    def test_distance_calculated_correctly(self):
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(92.0, 3.0, bucket)
        assert result["distance_to_limit"] == pytest.approx(2.0, abs=0.01)

    def test_maximum_edge_when_right_at_limit(self):
        bucket = {"type": "below", "low": -999.0, "high": 90.0}
        result = detect_boundary_zone(90.3, 3.0, bucket)
        assert result["edge_quality"] == "maximum"

    def test_range_bucket_uses_nearest_limit(self):
        # Forecast 86°F, bucket 85-95°F — mas cerca del limite bajo (85)
        bucket = {"type": "range", "low": 85.0, "high": 95.0}
        result = detect_boundary_zone(86.0, 3.0, bucket)
        assert result["limit_f"] == 85.0
        assert result["in_boundary_zone"] is True

    def test_std_covers_limit_detection(self):
        # Forecast 92°F, limite 90°F, std 4°F → std cubre el limite
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(92.0, 4.0, bucket)
        assert result["std_covers_limit"] is True

    def test_above_limit_direction(self):
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(91.0, 3.0, bucket)
        assert result["direction"] == "above_limit"

    def test_below_limit_direction(self):
        bucket = {"type": "above", "low": 90.0, "high": 999.0}
        result = detect_boundary_zone(89.0, 3.0, bucket)
        assert result["direction"] == "below_limit"

    def test_none_bucket_returns_safe_defaults(self):
        result = detect_boundary_zone(90.0, 3.0, None)
        assert result["in_boundary_zone"] is False
        assert result["edge_quality"] == "normal"


# ── format_rules_for_analyst ──────────────────────────────────────────────────

class TestFormatRulesForAnalyst:
    def test_returns_string(self):
        rules = parse_market_rules("New York high temperature above 90°F")
        boundary = detect_boundary_zone(91.0, 3.0, {"type": "above", "low": 90.0, "high": 999.0})
        result = format_rules_for_analyst(rules, boundary)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_includes_icao(self):
        rules = parse_market_rules("New York high temperature above 90°F")
        boundary = detect_boundary_zone(95.0, 3.0, {"type": "above", "low": 90.0, "high": 999.0})
        result = format_rules_for_analyst(rules, boundary)
        assert "KLGA" in result

    def test_highlights_boundary_zone(self):
        rules = parse_market_rules("New York high temperature above 90°F")
        boundary = detect_boundary_zone(91.0, 3.0, {"type": "above", "low": 90.0, "high": 999.0})
        result = format_rules_for_analyst(rules, boundary)
        assert "CRITICA" in result or "boundary" in result.lower() or "***" in result


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
