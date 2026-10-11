"""Typed, reversible JSON for libkis filter Properties (QVariant values)."""
import base64
import binascii
import math

from .compat import QColor, QByteArray

MAX_BINARY_BYTES = 1024 * 1024


def describe(value):
    """Return a JSON value plus honest editability metadata; never stringify Qt."""
    kind = type(value).__name__
    info = {"type": kind, "writable": True}
    if isinstance(value, QColor):
        if not value.isValid():
            return {"type": "QColor", "valid": False}, {
                "type": "QColor", "writable": False, "reason": "Invalid default color"}
        rgba = list(value.getRgbF())
        if any(not 0 <= c <= 1 for c in rgba):
            info.update(writable=False, reason="Extended RGB colors are read-only")
        info["encoding"] = "Tagged QColor; rgba contains four normalized RGB/alpha components"
        return {"type": "QColor", "rgba": rgba}, info
    if isinstance(value, QByteArray):
        raw = bytes(value)
        if len(raw) > MAX_BINARY_BYTES:
            return {"type": "QByteArray", "byte_length": len(raw)}, {
                "type": "QByteArray", "writable": False, "reason": "Binary default exceeds 1 MiB"}
        info["encoding"] = "Tagged QByteArray; base64 bytes, not a text string"
        return {"type": "QByteArray", "base64": base64.b64encode(raw).decode("ascii"),
                "byte_length": len(raw)}, info
    if value is None or isinstance(value, (bool, int, str)):
        if type(value) is int and not -(2**63) <= value < 2**63:
            info.update(writable=False, reason="Integer exceeds signed 64-bit QVariant range")
        return value, info
    if isinstance(value, float) and math.isfinite(value):
        return value, info
    if isinstance(value, (list, tuple, dict)):
        pairs = value.items() if isinstance(value, dict) else enumerate(value)
        converted = {}
        for key, child in pairs:
            encoded, child_info = describe(child)
            converted[key] = encoded
            if not child_info["writable"]:
                info.update(writable=False, reason="Contains an unsupported value")
        return (converted if isinstance(value, dict) else list(converted.values())), info
    return {"type": kind, "unsupported": True}, {
        "type": kind, "writable": False, "reason": "No reversible JSON codec for this type"}


def decode(value, default):
    """Validate against the actual default's type before any filter is started."""
    _, info = describe(default)
    if not info["writable"]:
        raise ValueError(info["reason"])
    if isinstance(default, QColor):
        if not isinstance(value, dict) or set(value) != {"type", "rgba"} or value["type"] != "QColor":
            raise ValueError('Expected {"type":"QColor","rgba":[r,g,b,a]} with components in 0..1')
        rgba = value["rgba"]
        if not isinstance(rgba, list) or len(rgba) != 4 or any(
                isinstance(c, bool) or not isinstance(c, (int, float)) or
                not math.isfinite(c) or not 0 <= c <= 1 for c in rgba):
            raise ValueError("QColor rgba must contain four finite numbers in 0..1")
        return QColor.fromRgbF(*rgba)
    if isinstance(default, QByteArray):
        if (not isinstance(value, dict) or value.get("type") != "QByteArray" or
                not {"type", "base64"} <= set(value) or
                set(value) - {"type", "base64", "byte_length"}):
            raise ValueError('Expected {"type":"QByteArray","base64":"..."}')
        data = value["base64"]
        if not isinstance(data, str) or len(data) > (MAX_BINARY_BYTES + 2) // 3 * 4:
            raise ValueError("QByteArray base64 exceeds 1 MiB or is not a string")
        try:
            raw = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("Invalid QByteArray base64")
        if len(raw) > MAX_BINARY_BYTES:
            raise ValueError("QByteArray exceeds 1 MiB")
        if "byte_length" in value and (type(value["byte_length"]) is not int or value["byte_length"] != len(raw)):
            raise ValueError("QByteArray byte_length does not match decoded bytes")
        return QByteArray(raw)
    if isinstance(default, bool):
        valid = isinstance(value, bool)
    elif isinstance(default, int):
        valid = type(value) is int and -(2**63) <= value < 2**63
    elif isinstance(default, float):
        valid = type(value) in (int, float) and math.isfinite(value)
        if valid:
            return float(value)
    elif isinstance(default, str):
        valid = isinstance(value, str)
    elif default is None:
        valid = value is None
    elif isinstance(default, (list, tuple)):
        if not isinstance(value, list) or len(value) != len(default):
            raise ValueError("List length must match the published default")
        return [decode(v, d) for v, d in zip(value, default)]
    elif isinstance(default, dict):
        if not isinstance(value, dict) or set(value) != set(default):
            raise ValueError("Object keys must match the published default")
        return {k: decode(value[k], d) for k, d in default.items()}
    else:
        valid = False
    if not valid:
        raise ValueError("Expected a value of type " + type(default).__name__)
    return value
