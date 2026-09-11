"""Apply sparse, user-selected process settings to a slice profile."""

from __future__ import annotations

import json
import logging
import re

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def _normalise_scalar(value: object) -> str | None:
    """Convert a supported JSON scalar to the form used by process presets."""
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float, str)):
        return str(value)
    return None


def normalise_process_overrides(overrides: dict[str, object]) -> dict[str, str | list[str]]:
    """Filter malformed keys/values while preserving valid sparse settings."""
    clean: dict[str, str | list[str]] = {}
    for key, value in overrides.items():
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            logger.warning("Ignoring process override with unusable key: %r", key)
            continue
        if isinstance(value, list):
            parts = [_normalise_scalar(item) for item in value]
            if any(part is None for part in parts):
                logger.warning("Ignoring process override %s: list contains a non-scalar entry", key)
                continue
            clean[key] = [part for part in parts if part is not None]
            continue
        scalar = _normalise_scalar(value)
        if scalar is None:
            logger.warning("Ignoring process override %s: unsupported value type", key)
            continue
        clean[key] = scalar
    return clean


def apply_process_overrides(process_json: str, overrides: dict[str, object]) -> str:
    """Apply valid overrides to a process JSON object.

    Invalid override entries or an invalid base profile leave the original
    profile usable. The sidecar remains responsible for slicer-specific range
    validation, while this boundary prevents malformed client values from
    changing the profile shape.
    """
    if not overrides:
        return process_json
    clean = normalise_process_overrides(overrides)
    if not clean:
        return process_json
    try:
        process_config = json.loads(process_json)
    except json.JSONDecodeError:
        logger.warning("Process preset JSON is unparseable; skipping user overrides")
        return process_json
    if not isinstance(process_config, dict):
        return process_json
    process_config.update(clean)
    return json.dumps(process_config)
