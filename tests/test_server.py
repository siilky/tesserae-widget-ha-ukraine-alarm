from __future__ import annotations

import importlib.util
import json
import re
import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
# server.py imports flask at module level; a standalone checkout may not have it.
try:
    import flask  # noqa: F401
except ImportError:
    sys.modules["flask"] = SimpleNamespace(current_app=None)
SPEC = importlib.util.spec_from_file_location("ha_ukraine_alarm_server", ROOT / "server.py")
assert SPEC is not None and SPEC.loader is not None
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)

REGION = "binary_sensor.m_kyiv"
NOW = datetime.now(UTC)


def at(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).isoformat()


def ms(hours_ago: float) -> int:
    return round((NOW - timedelta(hours=hours_ago)).timestamp() * 1000)


def sensor(alert_type: str, state: str, changed_hours_ago: float) -> dict[str, Any]:
    return {
        "entity_id": f"{REGION}_{alert_type}",
        "state": state,
        "last_changed": at(changed_hours_ago),
        "attributes": {
            "attribution": "Data provided by Ukraine Alarm",
            "friendly_name": f"м. Київ {alert_type.title()}",
        },
    }


class FakeCore:
    def __init__(self, states: list[dict[str, Any]], history: dict[str, list[dict[str, Any]]]):
        self.states = states
        self.history_by_id = history
        self.history_error: Exception | None = None

    def get_states(self) -> list[dict[str, Any]]:
        return self.states

    def history(self, entity_id: str, *, hours: int) -> list[dict[str, Any]]:
        if self.history_error is not None:
            raise self.history_error
        return self.history_by_id.get(entity_id, [])

    def coerce_error(self, error: Exception) -> str:
        return str(error)


def samples(*pairs: tuple[str, float]) -> list[dict[str, Any]]:
    return [{"state": state, "last_changed": at(hours)} for state, hours in pairs]


class ServerTest(unittest.TestCase):
    def fetch(self, core: FakeCore, region: str = REGION, **options: Any) -> dict[str, Any]:
        app = SimpleNamespace(config={"PLUGIN_REGISTRY": {"ha_core": SimpleNamespace(server_module=core)}})
        with mock.patch.object(SERVER, "current_app", app):
            return SERVER.fetch({"region": region, **options}, {}, ctx={})

    def test_active_alert_starts_before_an_ha_restart(self) -> None:
        # Air went on 3 h ago without a level; a restart 1 h ago re-recorded it
        # with a fresh last_changed.
        core = FakeCore(
            [sensor("air", "on", 1), sensor("air_red", "off", 1), sensor("artillery", "off", 1)],
            {
                f"{REGION}_air": samples(("off", 30), ("on", 20), ("off", 18), ("on", 3), ("unavailable", 1.01), ("on", 1)),
            },
        )
        data = self.fetch(core)
        self.assertEqual(data["title"], "м. Київ")
        self.assertEqual(data["alert_count"], 1)
        self.assertEqual(data["alerts"][0]["label_key"], "alert.air")
        self.assertEqual(data["alerts"][0]["since"], ms(3))
        self.assertEqual(data["timeline"][0], [ms(20), ms(18)])
        self.assertEqual(data["timeline"][1][0], ms(3))

    def test_air_level_replaces_the_plain_air_alert(self) -> None:
        # The plain air alert above had no level, so it was listed; with one it is not.
        core = FakeCore(
            [sensor("air", "on", 2), sensor("air_yellow", "on", 2), sensor("air_red", "off", 2)],
            {f"{REGION}_air_yellow": samples(("off", 30), ("on", 2))},
        )
        data = self.fetch(core)
        self.assertEqual([a["label_key"] for a in data["alerts"]], ["alert.air_yellow"])
        self.assertEqual(data["alert_count"], 1)
        self.assertEqual(data["alerts"][0]["since"], ms(2))

    def test_overlapping_spans_merge_across_types_and_restarts(self) -> None:
        core = FakeCore(
            [sensor("air", "off", 0.5), sensor("artillery", "off", 0.5)],
            {
                f"{REGION}_air": samples(("on", 6), ("off", 4), ("unavailable", 0.51), ("off", 0.5)),
                f"{REGION}_artillery": samples(("off", 100), ("on", 5), ("off", 4.5)),
            },
        )
        data = self.fetch(core)
        self.assertEqual(data["alerts"], [])
        self.assertEqual(data["timeline"], [[ms(6), ms(4)]])

    def test_hidden_types_are_left_out_everywhere(self) -> None:
        # With its yellow level hidden, the plain air alert is listed again.
        core = FakeCore(
            [sensor("air", "on", 2), sensor("air_yellow", "on", 2), sensor("artillery", "off", 1)],
            {
                f"{REGION}_air": samples(("off", 30), ("on", 2)),
                f"{REGION}_artillery": samples(("off", 100), ("on", 10), ("off", 9)),
            },
        )
        data = self.fetch(core, hidden_types=["air_yellow", "artillery"])
        self.assertEqual([a["label_key"] for a in data["alerts"]], ["alert.air"])
        self.assertEqual(len(data["timeline"]), 1)
        self.assertEqual(data["timeline"][0][0], ms(2))

    def test_timeline_window(self) -> None:
        core = FakeCore(
            [sensor("air", "off", 2)],
            {f"{REGION}_air": samples(("off", 30), ("on", 10), ("off", 8), ("on", 3), ("off", 2))},
        )
        data = self.fetch(core, timeline="6")
        self.assertEqual((data["timeline_hours"], data["timeline"]), (6, [[ms(3), ms(2)]]))
        data = self.fetch(core, timeline="0")
        self.assertEqual((data["timeline_hours"], data["timeline"]), (0, []))
        self.assertEqual(self.fetch(core, timeline="7")["timeline_hours"], 24)

    def test_alert_seen_only_in_current_state_uses_its_last_changed(self) -> None:
        # Cached history still ends "off"; the state says the alert began 1 min ago.
        core = FakeCore([sensor("air", "on", 1 / 60)], {f"{REGION}_air": samples(("off", 10))})
        self.assertEqual(self.fetch(core)["alerts"][0]["since"], ms(1 / 60))

    def test_history_failure_hides_times_but_keeps_states(self) -> None:
        core = FakeCore([sensor("air", "on", 1), sensor("nuclear", "off", 1)], {})
        core.history_error = RuntimeError("timeout")
        data = self.fetch(core)
        self.assertEqual(data["alerts"][0]["since"], None)
        self.assertEqual(data["history_error"], "timeout")

    def test_missing_or_unavailable_region_is_an_error(self) -> None:
        core = FakeCore([sensor("air", "unavailable", 1)], {})
        self.assertEqual(self.fetch(core)["error_key"], "error.no_region")
        self.assertEqual(self.fetch(core, "binary_sensor.other")["error_key"], "error.no_region")

    def test_region_choices_group_sensors(self) -> None:
        core = FakeCore([sensor("air", "on", 1), sensor("air_red", "off", 1), sensor("urban_fights", "off", 1)], {})
        app = SimpleNamespace(config={"PLUGIN_REGISTRY": {"ha_core": SimpleNamespace(server_module=core)}})
        with mock.patch.object(SERVER, "current_app", app):
            self.assertEqual(SERVER.choices("region"), [{"value": REGION, "label": "м. Київ"}])

    def test_client_keys_are_translated(self) -> None:
        client = (ROOT / "client.js").read_text(encoding="utf-8")
        keys = set(re.findall(r'"((?:setup|error|status|title)\.\w+)"', client))
        keys |= {f"alert.{alert_type}" for alert_type in SERVER._ALERT_TYPES}
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        for locale in manifest["locales"]:
            catalog = json.loads((ROOT / "strings" / f"{locale}.json").read_text(encoding="utf-8"))
            self.assertLessEqual(keys, set(catalog), locale)


if __name__ == "__main__":
    unittest.main()
