"""Current Ukraine Alarm alerts for one region, from the HA ukraine_alarm sensors."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

from flask import current_app


_ATTRIBUTION = "Data provided by Ukraine Alarm"
# The integration's alert keys, which are also the entity_id suffixes HA derives
# from them. Active alerts are listed in this order.
_ALERT_TYPES = (
    "air",
    "air_red",
    "air_yellow",
    "artillery",
    "urban_fights",
    "chemical",
    "nuclear",
    "unknown",
)
_ALERT_LABELS = {
    "air": "Air raid",
    "air_red": "Missiles (red)",
    "air_yellow": "Drones (yellow)",
    "artillery": "Artillery",
    "urban_fights": "Urban fighting",
    "chemical": "Chemical",
    "nuclear": "Nuclear",
    "unknown": "Alert",
}
_ALERT_ICONS = {
    "air": "rocket-launch",
    "air_red": "rocket-launch",
    "air_yellow": "airplane-tilt",
    "artillery": "bomb",
    "urban_fights": "crosshair",
    "chemical": "biohazard",
    "nuclear": "radioactive",
    "unknown": "warning",
}
# History reaches back further than the longest strip: an alert that began
# days ago still shows its real start time.
_HISTORY_HOURS = 168
_TIMELINE_HOURS = {"0": 0, "6": 6, "12": 12, "24": 24, "48": 48}


def _core() -> Any:
    plugin = current_app.config["PLUGIN_REGISTRY"].get("ha_core")
    return plugin.server_module if plugin is not None else None


def _regions(states: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Ukraine Alarm sensors grouped by region: the entity_id with the alert suffix removed."""
    regions: dict[str, dict[str, Any]] = {}
    for state in states:
        entity_id = str(state.get("entity_id") or "")
        attrs = state.get("attributes") or {}
        if not entity_id.startswith("binary_sensor.") or attrs.get("attribution") != _ATTRIBUTION:
            continue
        for alert_type in _ALERT_TYPES:
            if entity_id.endswith(f"_{alert_type}"):
                prefix = entity_id.removesuffix(f"_{alert_type}")
                regions.setdefault(prefix, {})[alert_type] = state
                break
    return regions


def _region_name(sensors: dict[str, Any], fallback: str) -> str:
    # Friendly names are "<region> <alert name>" with the alert name in HA's
    # language, so the shared prefix is the region in any language. The prefix
    # is cut back to a word boundary: "Air" and "Artillery" share an "A".
    names = [str((s.get("attributes") or {}).get("friendly_name") or "") for s in sensors.values()]
    name = os.path.commonprefix(names).rsplit(" ", 1)[0].strip() if len(names) > 1 else ""
    return name or fallback


def choices(name: str) -> list[dict[str, str]]:
    core = _core()
    if name != "region" or core is None:
        return []
    try:
        states = core.get_states()
    except Exception:
        return [{"value": "", "label": "Home Assistant unreachable, check URL + token"}]
    return sorted(
        (
            {"value": prefix, "label": _region_name(sensors, prefix)}
            for prefix, sensors in _regions(states).items()
        ),
        key=lambda choice: choice["label"].lower(),
    )


def _epoch_ms(raw: Any) -> int | None:
    """A HA timestamp as epoch milliseconds; a naive one is read as UTC."""
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return round(moment.timestamp() * 1000)


def _runs(samples: list[dict[str, Any]], current: dict[str, Any]) -> list[tuple[str, int]]:
    """Alternating (state, start ms) on/off runs of one sensor.

    An HA restart or an API outage records "unavailable" and then the same state
    again with a fresh last_changed, so only on/off samples count and a repeated
    state continues its run: the alert start and the all-clear keep their real
    times. The current state is appended because history is cached longer than
    states; a change the cached history has not seen yet still starts a run.
    """
    runs: list[tuple[str, int]] = []
    for sample in [*samples, current]:
        state = sample.get("state")
        moment = _epoch_ms(sample.get("last_changed"))
        if state not in ("on", "off") or moment is None:
            continue
        if not runs or runs[-1][0] != state:
            runs.append((state, moment))
    return runs


def fetch(
    options: dict[str, Any], settings: dict[str, Any], *, ctx: dict[str, Any]
) -> dict[str, Any]:
    del settings, ctx
    region = str(options.get("region") or "").strip()
    title = str(options.get("title") or "").strip()
    if not region:
        return {"setup": True, "title": title}

    core = _core()
    if core is None:
        # The host recognises a failed fetch only by a non-empty "error".
        return {
            "error_key": "error.missing_core",
            "error": "ha_core plugin is not installed",
            "title": title,
        }
    try:
        states = core.get_states()
    except Exception as error:
        return {"error_key": "error.fetch", "error": core.coerce_error(error), "title": title}

    sensors = _regions(states).get(region, {})
    known = {
        alert_type: state
        for alert_type, state in sensors.items()
        if state.get("state") in ("on", "off")
    }
    if not known:
        return {
            "error_key": "error.no_region",
            "error": f"No Ukraine Alarm data for {region}",
            "title": title,
        }

    # A hidden type is left out everywhere, as if its sensor did not exist. The
    # region check above runs first: hiding every type reads as no alerts.
    hidden = options.get("hidden_types")
    hidden = set(hidden) if isinstance(hidden, list) else set()
    shown = {alert_type: state for alert_type, state in known.items() if alert_type not in hidden}

    now = round(datetime.now(UTC).timestamp() * 1000)
    # 0 turns the strip off: no span ends after a window that starts now.
    timeline_hours = _TIMELINE_HOURS.get(str(options.get("timeline")), 24)
    timeline_start = now - timeline_hours * 3_600_000
    alerts: list[dict[str, Any]] = []
    spans: list[list[int]] = []
    history_error = ""
    # The plain air alert is the union of its red and yellow levels, so it is
    # listed only when the API sent no level. It still feeds the timeline.
    air_level_on = any(shown.get(t, {}).get("state") == "on" for t in ("air_red", "air_yellow"))
    for alert_type in (t for t in _ALERT_TYPES if t in shown):
        try:
            samples = core.history(shown[alert_type]["entity_id"], hours=_HISTORY_HOURS)
        except Exception as error:
            # Without history the current last_changed may be an HA restart, so
            # no time is shown rather than a wrong one.
            history_error = core.coerce_error(error)
            runs = []
        else:
            runs = _runs(samples, shown[alert_type])

        if shown[alert_type]["state"] == "on" and not (alert_type == "air" and air_level_on):
            alerts.append({
                "label_key": f"alert.{alert_type}",
                "label": _ALERT_LABELS[alert_type],
                "icon": _ALERT_ICONS[alert_type],
                "since": runs[-1][1] if runs else None,
            })
        for index, (state, start) in enumerate(runs):
            if state == "on":
                end = runs[index + 1][1] if index + 1 < len(runs) else now
                if end > timeline_start:
                    spans.append([max(start, timeline_start), end])

    # Overlapping alerts of different types draw as one continuous span.
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    return {
        "title": title or _region_name(sensors, region),
        "alert_count": len(alerts),
        "alerts": alerts,
        "history_error": history_error,
        "timeline_hours": timeline_hours,
        "timeline_start": timeline_start,
        "timeline_end": now,
        "timeline": merged,
    }
