from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "service.py"
SPEC = importlib.util.spec_from_file_location("cwa_alert_service", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
service = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = service
SPEC.loader.exec_module(service)


@dataclass
class PeriodFixture:
    """Provide a cwa_client-compatible weather period fixture."""

    start_time: str
    end_time: str
    weather: str
    rain_probability: int
    min_temperature: int
    max_temperature: int
    comfort: str


@dataclass
class ForecastFixture:
    """Provide a cwa_client-compatible weather forecast fixture."""

    city: str
    periods: list[PeriodFixture]
    issued_at: str


class SettingsTests(unittest.TestCase):
    """Validate settings and registry normalization."""

    def test_defaults_are_independent_and_disabled(self) -> None:
        first = service.default_settings()
        second = service.default_settings()

        first["subscriptions"].append({"id": "changed"})

        self.assertFalse(second["enabled"])
        self.assertEqual(second["source_mode"], "auto")
        self.assertEqual(second["poll_interval_seconds"], 120)
        self.assertEqual(second["subscriptions"], [])

    def test_normalize_settings_preserves_secret_and_normalizes_rule(self) -> None:
        settings = service.normalize_settings(
            {
                "revision": 4,
                "enabled": "true",
                "source_mode": "AUTO",
                "poll_interval_seconds": "180",
                "subscriptions": [
                    {
                        "target_umo": "qq:group:123",
                        "cities": ["台北市", "臺北市", "高雄市"],
                        "forecast_times": ["18:30", "07:30", "07:30"],
                        "alert_types": ["颱風", "rain", "颱風"],
                    }
                ],
            },
            existing_api_key="secret",
        )

        self.assertEqual(settings["api_key"], "secret")
        self.assertEqual(settings["source_mode"], "auto")
        rule = settings["subscriptions"][0]
        self.assertTrue(rule["id"].startswith("subscription-"))
        self.assertEqual(rule["cities"], ["臺北市", "高雄市"])
        self.assertEqual(rule["forecast_times"], ["07:30", "18:30"])
        self.assertEqual(rule["alert_types"], ["typhoon", "rain"])

    def test_normalize_settings_rejects_invalid_and_duplicate_rules(self) -> None:
        with self.assertRaisesRegex(service.ValidationError, "60 至 600"):
            service.normalize_settings({"poll_interval_seconds": 30})
        with self.assertRaisesRegex(service.ValidationError, "至少需要選擇"):
            service.normalize_subscription({"target_umo": "qq:group:1", "cities": []})
        with self.assertRaisesRegex(service.ValidationError, "最多只能建立一條"):
            service.normalize_settings(
                {
                    "subscriptions": [
                        {
                            "id": "a",
                            "target_umo": "qq:group:1",
                            "cities": ["臺北市"],
                        },
                        {
                            "id": "b",
                            "target_umo": "qq:group:1",
                            "cities": ["高雄市"],
                        },
                    ]
                }
            )

    def test_group_registry_normalizes_and_rejects_duplicates(self) -> None:
        raw = {
            "one": {
                "target_umo": "qq:group:123",
                "platform_id": "qq-main",
                "platform_type": "aiocqhttp",
                "group_id": "123",
                "display_name": "防災群",
                "registered_at": "2026-07-12T08:00:00+08:00",
            }
        }
        targets = service.normalize_group_registry(raw)
        self.assertEqual(targets[0]["display_name"], "防災群")
        self.assertTrue(targets[0]["id"].startswith("target-"))

        with self.assertRaisesRegex(service.ValidationError, "重複目標"):
            service.normalize_group_registry([raw["one"], raw["one"]])

    def test_public_settings_never_returns_api_key(self) -> None:
        result = service.public_settings(
            {"api_key": "stored-secret", "source_mode": "auto"},
            {"CWA_API_KEY": "environment-secret"},
        )

        self.assertNotIn("api_key", result)
        self.assertTrue(result["api_key_configured"])
        self.assertEqual(result["api_key_suffix"], "cret")
        self.assertTrue(result["api_key_from_environment"])

    def test_known_targets_rejects_orphan_subscription(self) -> None:
        payload = {
            "subscriptions": [
                {
                    "target_umo": "qq:group:missing",
                    "cities": ["臺北市"],
                }
            ]
        }

        with self.assertRaisesRegex(service.ValidationError, "尚未登記"):
            service.normalize_settings(
                payload,
                [{"target_umo": "qq:group:registered"}],
            )

        payload["subscriptions"][0]["enabled"] = False
        settings = service.normalize_settings(
            payload,
            [{"target_umo": "qq:group:registered"}],
        )
        self.assertFalse(settings["subscriptions"][0]["enabled"])


class SourceSelectionTests(unittest.TestCase):
    """Validate API/RSS source choice and health transitions."""

    def test_auto_without_key_uses_rss_without_fallback_label(self) -> None:
        decision = service.select_data_source(
            {"source_mode": "auto", "api_key": ""}, {}, {}
        )

        self.assertEqual(decision.active_source, "rss")
        self.assertFalse(decision.is_fallback)
        self.assertEqual(decision.status_label, "RSS 模式")

    def test_auto_with_key_degrades_after_three_failures(self) -> None:
        settings = {"source_mode": "auto", "api_key": "key"}
        decision = service.select_data_source(
            settings, {"api_consecutive_failures": 3}, {}
        )

        self.assertEqual(decision.active_source, "rss")
        self.assertTrue(decision.is_fallback)
        self.assertEqual(decision.status_label, "RSS 降級中")

    def test_api_mode_without_key_is_explicitly_unavailable(self) -> None:
        decision = service.select_data_source(
            {"source_mode": "api", "api_key": ""}, {}, {}
        )

        self.assertEqual(decision.active_source, "unavailable")
        self.assertIn("需要", decision.reason)

    def test_environment_key_takes_precedence(self) -> None:
        settings = {"source_mode": "auto", "api_key": "stored"}
        self.assertEqual(
            service.effective_api_key(settings, {"CWA_API_KEY": "environment"}),
            "environment",
        )

    def test_successful_api_probe_recovers_from_fallback(self) -> None:
        settings = {"source_mode": "auto", "api_key": "key"}
        state = {
            "active_source": "rss",
            "api_consecutive_failures": 3,
            "is_fallback": True,
        }

        updated = service.update_source_state(
            settings,
            state,
            source="api",
            succeeded=True,
            now=datetime(2026, 7, 12, 9, 0, tzinfo=service.TAIPEI_TIMEZONE),
            environ={},
        )

        self.assertEqual(updated["api_consecutive_failures"], 0)
        self.assertEqual(updated["active_source"], "api")
        self.assertFalse(updated["is_fallback"])
        self.assertIsNotNone(updated["last_switch_at"])


class EventTests(unittest.TestCase):
    """Validate event identity, filtering, and formatting."""

    def setUp(self) -> None:
        """Create a reusable normalized subscription."""
        self.subscription = service.normalize_subscription(
            {
                "target_umo": "qq:group:123",
                "cities": ["臺北市"],
                "alert_types": ["typhoon", "rain", "fog"],
            }
        )

    def test_fingerprint_ignores_source_ids_and_timezone_representation(self) -> None:
        api_event = {
            "source": "api",
            "official_id": "API-123",
            "category": "typhoon",
            "title": "海上颱風警報",
            "bulletin_no": "第 3 報",
            "published_at": "2026-07-12T08:30:00+08:00",
            "areas": ["臺灣東部海面"],
            "link": "https://example.test/api?id=secret",
        }
        rss_event = {
            "source": "rss",
            "guid": "RSS-999",
            "category": "typhoon",
            "title": "海上颱風警報",
            "report_no": "3",
            "published_at": "2026-07-12T00:30:00Z",
            "areas": ["臺灣東部海面"],
            "link": "https://example.test/rss",
        }

        self.assertEqual(
            service.event_fingerprint(api_event),
            service.event_fingerprint(rss_event),
        )

    def test_real_shaped_api_and_rss_warning_share_fingerprint(self) -> None:
        api_event = {
            "source": "api",
            "official_id": "W-C0034-001-22",
            "category": "typhoon",
            "title": "海上陸上颱風警報",
            "description": "中度颱風巴威（第 22 報）。臺北市、高雄市請注意。",
            "published_at": "2026-07-12T05:30:00+08:00",
            "areas": ["臺北市", "高雄市"],
            "bulletin_no": "第 22 報",
            "link": "https://opendata.cwa.gov.tw/warning/22",
        }
        rss_event = {
            "source": "rss",
            "official_id": "rss-typhoon-22",
            "category": "typhoon",
            "description": "中度颱風巴威(第22報)，台北市、高雄市請注意！",
            "published_at": "2026-07-11T21:30:00Z",
            "areas": ["高雄市", "台北市"],
            "bulletin_no": "22",
            "link": "https://www.cwa.gov.tw/V8/C/P/Typhoon/TY_WARN.html",
        }

        for wrapped_title in (
            "07/12 05:30 發布海上陸上颱風警報",
            "07/12 05:30:00 發佈：海上陸上颱風警報",
        ):
            with self.subTest(wrapped_title=wrapped_title):
                self.assertEqual(
                    service.event_fingerprint(api_event),
                    service.event_fingerprint({**rss_event, "title": wrapped_title}),
                )

    def test_fingerprint_distinguishes_bulletin_time_and_body_changes(self) -> None:
        base = {
            "category": "typhoon",
            "title": "海上陸上颱風警報",
            "description": "中度颱風巴威。臺北市、高雄市請注意。",
            "published_at": "2026-07-12T05:30:00+08:00",
            "areas": ["臺北市", "高雄市"],
            "bulletin_no": "22",
        }
        variants = (
            {**base, "bulletin_no": "23"},
            {**base, "published_at": "2026-07-12T05:30:01+08:00"},
            {
                **base,
                "description": "中度颱風巴威。臺北市、高雄市請注意強風。",
            },
        )
        base_fingerprint = service.event_fingerprint(base)

        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(
                    base_fingerprint,
                    service.event_fingerprint(variant),
                )

    def test_national_alert_ignores_city_but_local_alert_uses_intersection(
        self,
    ) -> None:
        self.assertTrue(
            service.subscription_accepts_event(
                self.subscription,
                {"category": "typhoon", "areas": ["高雄市"]},
            )
        )
        self.assertFalse(
            service.subscription_accepts_event(
                self.subscription,
                {"category": "rain", "areas": ["高雄市、屏東縣"]},
            )
        )
        self.assertTrue(
            service.subscription_accepts_event(
                self.subscription,
                {"category": "rain", "areas": ["新北市及臺北市"]},
            )
        )

    def test_unknown_local_area_is_delivered_to_avoid_missing_warning(self) -> None:
        self.assertTrue(
            service.subscription_accepts_event(
                self.subscription,
                {"category": "fog", "areas": ["西半部地區"]},
            )
        )

    def test_unselected_category_and_disabled_rule_are_rejected(self) -> None:
        self.assertFalse(
            service.subscription_accepts_event(
                self.subscription, {"category": "heat", "areas": ["臺北市"]}
            )
        )
        disabled = dict(self.subscription, enabled=False)
        self.assertFalse(
            service.subscription_accepts_event(
                disabled, {"category": "typhoon", "areas": []}
            )
        )

    def test_alert_formatter_uses_traditional_chinese_and_cleans_query(self) -> None:
        text = service.format_alert_event(
            {
                "category": "typhoon",
                "title": "海上陸上颱風警報",
                "bulletin_no": "第 9 報",
                "published_at": "2026-07-12T00:30:00Z",
                "areas": ["臺北市", "新北市"],
                "description": "請嚴加戒備。",
                "link": "https://www.cwa.gov.tw/warning?id=secret#top",
            }
        )

        self.assertIn("【中央氣象署｜颱風】", text)
        self.assertIn("2026/07/12 08:30", text)
        self.assertIn("影響區域：臺北市、新北市", text)
        self.assertIn("https://www.cwa.gov.tw/warning", text)
        self.assertNotIn("secret", text)
        self.assertTrue(text.endswith("資料來源：交通部中央氣象署"))

    def test_cancellation_formatter_is_explicit(self) -> None:
        text = service.format_alert_event(
            {
                "category": "typhoon",
                "title": "解除颱風警報",
                "is_cancellation": True,
            }
        )
        self.assertIn("解除／取消資訊", text)

    def test_typhoon_bulletin_requires_active_sea_warning(self) -> None:
        self.assertTrue(
            service.should_download_typhoon_bulletin(
                {"category": "typhoon", "title": "海上陸上颱風警報"}
            )
        )
        self.assertFalse(
            service.should_download_typhoon_bulletin(
                {
                    "category": "typhoon",
                    "title": "解除海上颱風警報",
                    "is_cancellation": True,
                }
            )
        )
        self.assertFalse(
            service.should_download_typhoon_bulletin(
                {"category": "typhoon", "title": "颱風消息"}
            )
        )

    def test_event_freshness_handles_age_clock_skew_and_missing_time(self) -> None:
        now = datetime(2026, 7, 12, 8, 0, tzinfo=service.TAIPEI_TIMEZONE)
        self.assertTrue(
            service.event_is_recent(
                {"published_at": "2026-07-12T07:55:00+08:00"}, now=now
            )
        )
        self.assertTrue(
            service.event_is_recent(
                {"published_at": "2026-07-12T08:03:00+08:00"}, now=now
            )
        )
        self.assertFalse(
            service.event_is_recent(
                {"published_at": "2026-07-12T07:54:59+08:00"}, now=now
            )
        )
        self.assertFalse(service.event_is_recent({}, now=now))


class FormattingAndScheduleTests(unittest.TestCase):
    """Validate weather output, text splitting, and due-slot calculation."""

    def test_weather_formatter_accepts_dataclasses_and_limits_periods(self) -> None:
        periods = [
            PeriodFixture(
                f"2026-07-1{day}T08:00:00+08:00",
                f"2026-07-1{day}T20:00:00+08:00",
                "多雲短暫雨",
                40,
                26,
                31,
                "悶熱",
            )
            for day in (2, 3, 4, 5)
        ]
        forecast = ForecastFixture(
            city="台北市",
            periods=periods,
            issued_at="2026-07-12T07:00:00+08:00",
        )

        text = service.format_weather_forecast(forecast)

        self.assertIn("【臺北市 36 小時天氣預報】", text)
        self.assertIn("降雨機率：40%", text)
        self.assertIn("溫度：26～31 °C", text)
        self.assertIn("3. ", text)
        self.assertNotIn("4. ", text)

    def test_weather_formatter_handles_rss_label_without_end_time(self) -> None:
        forecast = {
            "source": "rss",
            "city": "臺北市",
            "published_at": "2026-07-12T07:00:00+08:00",
            "periods": [
                {
                    "start_time": "今日白天",
                    "end_time": "",
                    "weather": "晴時多雲",
                    "rain_probability": "10",
                    "min_temperature": "28",
                    "max_temperature": "35",
                    "comfort": "",
                }
            ],
        }

        text = service.format_weather_forecast(forecast)

        self.assertIn("1. 今日白天", text)
        self.assertNotIn("今日白天－未提供", text)

    def test_split_message_preserves_content_and_limit(self) -> None:
        text = "標題\n\n" + "甲" * 25 + "。" + "乙" * 25 + "\n\n結尾"
        chunks = service.split_message(text, limit=20)

        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(0 < len(chunk) <= 20 for chunk in chunks))
        self.assertEqual("".join(chunks).replace("\n", ""), text.replace("\n", ""))

    def test_due_slot_is_inclusive_and_deduplicated(self) -> None:
        subscription = service.normalize_subscription(
            {
                "target_umo": "qq:group:123",
                "cities": ["臺北市"],
                "forecast_times": ["07:30", "18:30"],
            }
        )
        now = datetime(2026, 7, 12, 8, 0, tzinfo=service.TAIPEI_TIMEZONE)
        slots = service.due_forecast_slots(subscription, set(), now=now)

        self.assertEqual(
            [(slot.date, slot.time) for slot in slots],
            [("2026-07-12", "07:30")],
        )
        self.assertEqual(
            service.due_forecast_slots(subscription, {slots[0].key}, now=now), []
        )

    def test_due_slot_handles_cross_midnight_window(self) -> None:
        subscription = service.normalize_subscription(
            {
                "target_umo": "qq:group:123",
                "cities": ["臺北市"],
                "forecast_times": ["23:50"],
            }
        )
        now = datetime(2026, 7, 13, 0, 10, tzinfo=service.TAIPEI_TIMEZONE)

        slots = service.due_forecast_slots(subscription, {}, now=now)

        self.assertEqual(
            [(slot.date, slot.time) for slot in slots],
            [("2026-07-12", "23:50")],
        )

    def test_due_slot_rejects_old_or_disabled_schedule(self) -> None:
        subscription = service.normalize_subscription(
            {
                "target_umo": "qq:group:123",
                "cities": ["臺北市"],
                "forecast_times": ["07:30"],
            }
        )
        now = datetime(2026, 7, 12, 8, 1, tzinfo=service.TAIPEI_TIMEZONE)
        self.assertEqual(service.due_forecast_slots(subscription, {}, now=now), [])
        self.assertEqual(
            service.due_forecast_slots(dict(subscription, enabled=False), {}, now=now),
            [],
        )


class StateTests(unittest.TestCase):
    """Validate seen-event and per-target delivery state."""

    def setUp(self) -> None:
        """Create deterministic timestamps and a reusable event."""
        self.now = datetime(2026, 7, 12, 8, 0, tzinfo=service.TAIPEI_TIMEZONE)
        self.event = {
            "category": "rain",
            "title": "豪雨特報",
            "published_at": self.now.isoformat(),
            "areas": ["臺北市"],
        }

    def test_seen_event_round_trip(self) -> None:
        state = service.mark_event_seen({}, self.event, now=self.now)

        self.assertTrue(service.event_was_seen(state, self.event))
        record = next(iter(state.values()))
        self.assertEqual(record["category"], "rain")

    def test_seen_event_update_preserves_integration_metadata(self) -> None:
        fingerprint = service.event_fingerprint(self.event)
        state = {
            fingerprint: {
                "fingerprint": fingerprint,
                "category": "rain",
                "first_seen_at": self.now.isoformat(),
                "last_seen_at": self.now.isoformat(),
                "dispatchable": True,
            }
        }

        updated = service.mark_event_seen(
            state,
            self.event,
            now=self.now + timedelta(minutes=1),
        )

        self.assertTrue(updated[fingerprint]["dispatchable"])
        self.assertEqual(
            updated[fingerprint]["first_seen_at"],
            self.now.isoformat(),
        )

    def test_seen_pruning_applies_age_and_count(self) -> None:
        records = {
            "old": {
                "last_seen_at": (self.now - timedelta(days=31)).isoformat(),
            },
            "newer": {"last_seen_at": self.now.isoformat()},
            "newest": {"last_seen_at": (self.now + timedelta(seconds=1)).isoformat()},
            "malformed": {"last_seen_at": "not-a-date"},
        }

        result = service.prune_seen_events(records, now=self.now, max_entries=1)

        self.assertEqual(list(result), ["newest"])

    def test_delivery_tracks_text_and_image_independently(self) -> None:
        fingerprint = service.event_fingerprint(self.event)
        state = service.mark_delivery_status(
            {},
            event_fingerprint_value=fingerprint,
            target_umo="qq:group:123",
            part="text",
            succeeded=True,
            now=self.now,
        )
        state = service.mark_delivery_status(
            state,
            event_fingerprint_value=fingerprint,
            target_umo="qq:group:123",
            part="image",
            succeeded=False,
            error="圖片尚未更新",
            now=self.now,
        )

        self.assertTrue(
            service.delivery_succeeded(state, fingerprint, "qq:group:123", "text")
        )
        self.assertFalse(
            service.delivery_succeeded(state, fingerprint, "qq:group:123", "image")
        )
        image_key = service.delivery_key(fingerprint, "qq:group:123", "image")
        self.assertEqual(state[image_key]["attempts"], 1)
        self.assertEqual(state[image_key]["error"], "圖片尚未更新")

    def test_failed_delivery_attempts_increment_and_success_clears_error(self) -> None:
        fingerprint = service.event_fingerprint(self.event)
        state: dict[str, object] = {}
        for succeeded in (False, False, True):
            state = service.mark_delivery_status(
                state,
                event_fingerprint_value=fingerprint,
                target_umo="qq:group:123",
                part="image",
                succeeded=succeeded,
                error="失敗",
                now=self.now,
            )
        key = service.delivery_key(fingerprint, "qq:group:123", "image")

        self.assertEqual(state[key]["attempts"], 3)
        self.assertEqual(state[key]["status"], "success")
        self.assertIsNone(state[key]["error"])
        self.assertIsNotNone(state[key]["delivered_at"])

    def test_prune_delivery_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValueError):
            service.prune_delivery_status({}, retention_days=-1)
        with self.assertRaises(ValueError):
            service.prune_delivery_status({}, max_entries=-1)


if __name__ == "__main__":
    unittest.main()
