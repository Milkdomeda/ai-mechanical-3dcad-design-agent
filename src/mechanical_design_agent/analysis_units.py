"""Exact, bounded decimal conversion to the N–mm–s–tonne unit system."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import re

from .analysis_types import AnalysisError, JsonObject

_DECIMAL = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?\Z")
_UNITS = {
    "length": ("mm", {"mm": "1", "m": "1000", "cm": "10", "um": "0.001"}),
    "area": ("mm^2", {"mm^2": "1", "m^2": "1000000"}),
    "volume": ("mm^3", {"mm^3": "1", "m^3": "1000000000"}),
    "mass": ("tonne", {"tonne": "1", "kg": "0.001", "g": "0.000001"}),
    "time": ("s", {"s": "1"}),
    "force": ("N", {"N": "1", "kN": "1000"}),
    "stress": ("MPa", {"MPa": "1", "N/mm^2": "1", "Pa": "0.000001", "GPa": "1000"}),
    "pressure": ("MPa", {"MPa": "1", "N/mm^2": "1", "Pa": "0.000001", "GPa": "1000"}),
    "density": ("tonne/mm^3", {"tonne/mm^3": "1", "kg/m^3": "0.000000000001", "g/cm^3": "0.000000001"}),
    "acceleration": ("mm/s^2", {"mm/s^2": "1", "m/s^2": "1000"}),
    "moment": ("N*mm", {"N*mm": "1", "N*m": "1000"}),
    "energy": ("N*mm", {"N*mm": "1", "J": "1000"}),
    "dimensionless": ("1", {"1": "1"}),
}


def canonical_decimal(value: str) -> str:
    if type(value) is not str or len(value) > 128 or not _DECIMAL.fullmatch(value):
        raise AnalysisError("ANALYSIS_QUANTITY_INVALID", "quantity values must be finite decimal strings")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise AnalysisError("ANALYSIS_QUANTITY_INVALID", "invalid decimal quantity") from exc
    if not number.is_finite() or abs(number.as_tuple().exponent) > 128 or abs(number.adjusted()) > 128:
        raise AnalysisError("ANALYSIS_QUANTITY_RANGE", "quantity exceeds the supported decimal range")
    if number.is_zero():
        return "0"
    text = format(number, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def normalize_quantity(value: str, unit: str, *, dimension: str) -> JsonObject:
    original = canonical_decimal(value)
    if type(dimension) is not str or dimension not in _UNITS:
        raise AnalysisError("ANALYSIS_DIMENSION_UNSUPPORTED", "unsupported physical dimension")
    base, factors = _UNITS[dimension]
    if type(unit) is not str or unit not in factors:
        raise AnalysisError("ANALYSIS_UNIT_MISMATCH", "unit is unsupported for the required dimension", details={"dimension": dimension})
    with localcontext() as context:
        context.prec = 256
        converted = Decimal(original) * Decimal(factors[unit])
    return {"value": canonical_decimal(format(converted, "f")), "unit": base, "input": {"value": original, "unit": unit}}


def validate_normalized_quantity(quantity: JsonObject) -> None:
    value = canonical_decimal(quantity["value"])
    unit = quantity["unit"]
    dimensions = [name for name, (base, _) in _UNITS.items() if base == unit]
    if not dimensions:
        raise AnalysisError("ANALYSIS_UNIT_MISMATCH", "stored physical quantities must use the consistent unit system")
    original = quantity.get("input")
    if original is not None:
        for dimension in dimensions:
            if original["unit"] in _UNITS[dimension][1]:
                converted = normalize_quantity(original["value"], original["unit"], dimension=dimension)
                if converted["value"] == value:
                    return
        raise AnalysisError("ANALYSIS_QUANTITY_ORIGIN_MISMATCH", "normalized quantity does not match its recorded input")
