"""Offline tests for the CWA transport and normalization layer."""

from __future__ import annotations

import asyncio
import importlib.util
import ssl
import sys
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from PIL import Image

MODULE_PATH = Path(__file__).parents[1] / "cwa_client.py"
SPEC = importlib.util.spec_from_file_location("cwa_client_under_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
cwa = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cwa
SPEC.loader.exec_module(cwa)


WARNING_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/">
  <channel>
    <title>中央氣象署警報、特報</title>
    <item>
      <pubDate>Sat, 11 Jul 2026 21:30:00 GMT</pubDate>
      <title><![CDATA[07/12 05:30 發布海上颱風警報]]></title>
      <link>https://www.cwa.gov.tw/V8/C/P/Typhoon/TY_WARN.html?T=1</link>
      <guid>typhoon-22</guid>
      <description><![CDATA[中度颱風巴威(第22報)。臺北市、高雄市請注意。<br>請留意最新消息。]]></description>
    </item>
    <item>
      <pubDate>Sat, 11 Jul 2026 22:00:00 GMT</pubDate>
      <title>07/12 06:00 解除颱風警報</title>
      <link>https://www.cwa.gov.tw/V8/C/P/Typhoon/TY_WARN.html?T=2</link>
      <description><![CDATA[颱風警報解除。]]></description>
    </item>
  </channel>
</rss>
""".encode()


FORECAST_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <pubDate>Sat, 11 Jul 2026 22:17:42 GMT</pubDate>
      <title><![CDATA[臺北市07/12 今日白天 陰短暫陣雨 溫度: 27 ~ 35 降雨機率: 30% (07/12 05:00發布)]]></title>
      <link>https://www.cwa.gov.tw/V8/C/W/County/County.html?CID=63</link>
      <guid>weather-taipei-20260712</guid>
      <description><![CDATA[
        今晚明晨 多雲時陰短暫陣雨 溫度: 28 ~ 32 降雨機率: 30% <br>
        明日白天 多雲午後短暫雷陣雨 溫度: 28 ~ 34 降雨機率: 80%
      ]]></description>
    </item>
    <item>
      <title>臺北市 一週天氣預報</title>
      <description>不應選取此項目</description>
    </item>
  </channel>
</rss>
""".encode()


API_WEATHER = {
    "success": "true",
    "records": {
        "publishTime": "2026-07-12T05:00:00+08:00",
        "location": [
            {
                "locationName": "臺北市",
                "weatherElement": [
                    {
                        "elementName": "Wx",
                        "time": [
                            {
                                "startTime": "2026-07-12 06:00:00",
                                "endTime": "2026-07-12 18:00:00",
                                "parameter": {"parameterName": "多雲"},
                            }
                        ],
                    },
                    {
                        "elementName": "PoP",
                        "time": [
                            {
                                "startTime": "2026-07-12 06:00:00",
                                "endTime": "2026-07-12 18:00:00",
                                "parameter": {"parameterName": "40"},
                            }
                        ],
                    },
                    {
                        "elementName": "MinT",
                        "time": [
                            {
                                "startTime": "2026-07-12 06:00:00",
                                "endTime": "2026-07-12 18:00:00",
                                "parameter": {"parameterName": "27"},
                            }
                        ],
                    },
                    {
                        "elementName": "MaxT",
                        "time": [
                            {
                                "startTime": "2026-07-12 06:00:00",
                                "endTime": "2026-07-12 18:00:00",
                                "parameter": {"parameterName": "35"},
                            }
                        ],
                    },
                    {
                        "elementName": "CI",
                        "time": [
                            {
                                "startTime": "2026-07-12 06:00:00",
                                "endTime": "2026-07-12 18:00:00",
                                "parameter": {"parameterName": "悶熱"},
                            }
                        ],
                    },
                ],
            },
            {"locationName": "不存在市", "weatherElement": []},
        ],
    },
}


API_ALERT_CASES = {
    "significant_earthquake": (
        "significant_earthquake",
        {
            "records": {
                "Earthquake": [
                    {
                        "ReportType": "顯著有感地震報告",
                        "ReportContent": "臺東縣發生有感地震。",
                        "ReportTime": "2026-07-12 08:00:00",
                    }
                ]
            }
        },
    ),
    "local_earthquake": (
        "local_earthquake",
        {
            "records": {
                "Earthquake": [
                    {
                        "ReportType": "小區域有感地震報告",
                        "ReportContent": "花蓮縣發生小區域有感地震。",
                        "ReportTime": "2026-07-12 08:01:00",
                    }
                ]
            }
        },
    ),
    "tsunami": (
        "tsunami",
        {
            "records": {
                "Tsunami": [
                    {
                        "headline": "海嘯警報",
                        "description": "臺東縣沿海請注意海嘯。",
                        "sent": "2026-07-12T08:02:00+08:00",
                    }
                ]
            }
        },
    ),
    "weather_warning": (
        "rain",
        {
            "records": {
                "record": [
                    {
                        "headline": "豪雨特報",
                        "description": "高雄市有局部豪雨。",
                        "sent": "2026-07-12T08:03:00+08:00",
                    }
                ]
            }
        },
    ),
    "cold": (
        "cold",
        {
            "records": {
                "record": [
                    {
                        "headline": "低溫特報",
                        "description": "新北市有持續低溫。",
                        "sent": "2026-07-12T08:04:00+08:00",
                    }
                ]
            }
        },
    ),
    "heat": (
        "heat",
        {
            "records": {
                "record": [
                    {
                        "headline": "高溫資訊",
                        "description": "臺北市有連續高溫。",
                        "sent": "2026-07-12T08:05:00+08:00",
                    }
                ]
            }
        },
    ),
    "typhoon": (
        "typhoon",
        {
            "records": {
                "tropicalCyclone": [
                    {
                        "headline": "海上颱風警報",
                        "description": "第 3 報，請注意最新颱風動態。",
                        "sent": "2026-07-12T08:06:00+08:00",
                    }
                ]
            }
        },
    ),
}


class FakeContent:
    """Minimal aiohttp stream double."""

    def __init__(self, data: bytes, *, chunk_size: int | None = None) -> None:
        self.data = data
        self.chunk_size = chunk_size or max(1, len(data))

    async def iter_chunked(self, requested_size: int):
        del requested_size
        for index in range(0, len(self.data), self.chunk_size):
            yield self.data[index : index + self.chunk_size]


class FakeResponse:
    """Minimal async context manager matching aiohttp.ClientResponse."""

    def __init__(
        self,
        data: bytes = b"",
        *,
        status: int = 200,
        content_type: str = "application/json",
        url: str = cwa.TYPHOON_BULLETIN_URL,
        headers: dict[str, str] | None = None,
        content_length: int | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self.status = status
        self.url = url
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.content_length = len(data) if content_length is None else content_length
        self.content = FakeContent(data, chunk_size=chunk_size)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback


class FakeSession:
    """Queue-backed session double used to verify requests and redirects."""

    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.closed = False

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((url, kwargs))
        if not self.responses:
            raise AssertionError("測試回應佇列已用完")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def close(self) -> None:
        self.closed = True


class CityAndClassificationTests(unittest.TestCase):
    def test_city_map_contains_exactly_22_unique_codes(self) -> None:
        self.assertEqual(len(cwa.CITY_RSS_CODES), 22)
        self.assertEqual(len(set(cwa.CITY_RSS_CODES.values())), 22)
        self.assertEqual(cwa.normalize_city("台北市"), "臺北市")
        self.assertEqual(cwa.normalize_city(" 台中市 "), "臺中市")

    def test_invalid_city_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "不支援的縣市"):
            cwa.normalize_city("上海市")

    def test_category_order_and_unknown_fallback(self) -> None:
        self.assertEqual(cwa.classify_alert("小區域有感地震報告"), "local_earthquake")
        self.assertEqual(cwa.classify_alert("海嘯警報", "由地震造成"), "tsunami")
        self.assertEqual(cwa.classify_alert("大雨特報"), "rain")
        self.assertEqual(cwa.classify_alert("未分類測試"), "other")


class RSSTests(unittest.TestCase):
    def test_warning_rss_normalizes_html_dates_areas_and_bulletin(self) -> None:
        items = cwa.parse_rss_items(WARNING_RSS)
        self.assertEqual(len(items), 2)
        self.assertIn("\n", items[0].description)
        self.assertEqual(items[0].published_at, "2026-07-11T21:30:00+00:00")

        events = cwa.normalize_rss_alerts(items)
        self.assertEqual(events[0].category, "typhoon")
        self.assertEqual(events[0].areas, ("臺北市", "高雄市"))
        self.assertEqual(events[0].bulletin_no, "22")
        self.assertFalse(events[0].is_cancellation)
        self.assertTrue(events[1].is_cancellation)

    def test_forecast_rss_returns_three_periods(self) -> None:
        forecast = cwa.normalize_rss_weather(
            "台北市",
            cwa.parse_rss_items(FORECAST_RSS),
        )
        self.assertEqual(forecast.city, "臺北市")
        self.assertEqual(len(forecast.periods), 3)
        self.assertEqual(forecast.periods[0].rain_probability, "30")
        self.assertEqual(forecast.periods[2].weather, "多雲午後短暫雷陣雨")

    def test_dtd_and_entity_declarations_are_rejected(self) -> None:
        samples = (
            b'<?xml version="1.0"?><!DOCTYPE rss><rss/>',
            b'<?xml version="1.0"?><!ENTITY x "y"><rss/>',
        )
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(cwa.CWASecurityError):
                    cwa.parse_rss_items(sample)

    def test_xml_size_and_malformed_xml_are_rejected(self) -> None:
        with self.assertRaises(cwa.CWAResponseTooLargeError):
            cwa.parse_rss_items(b"<rss></rss>", max_bytes=5)
        with self.assertRaises(cwa.CWAParseError):
            cwa.parse_rss_items(b"<rss>")

    def test_default_namespace_elements_are_parsed_by_local_name(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss xmlns="https://example.test/rss" version="2.0">
          <channel>
            <item>
              <title>濃霧特報</title>
              <description>金門縣能見度偏低。</description>
              <link>https://www.cwa.gov.tw/warning/fog</link>
              <guid>fog-1</guid>
              <pubDate>Sat, 11 Jul 2026 23:00:00 GMT</pubDate>
            </item>
          </channel>
        </rss>
        """.encode()

        items = cwa.parse_rss_items(xml)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "濃霧特報")
        self.assertEqual(items[0].guid, "fog-1")
        self.assertEqual(items[0].published_at, "2026-07-11T23:00:00+00:00")

    def test_missing_guid_and_pub_date_use_documented_fallbacks(self) -> None:
        xml = """<rss version="2.0"><channel><item>
          <title>大雨特報</title>
          <link>https://www.cwa.gov.tw/warning/rain</link>
          <description>宜蘭縣請注意大雨。</description>
        </item></channel></rss>""".encode()

        items = cwa.parse_rss_items(xml)
        events = cwa.normalize_rss_alerts(items)

        self.assertEqual(items[0].guid, "")
        self.assertEqual(items[0].published_at, "")
        self.assertEqual(events[0].official_id, items[0].link)
        self.assertEqual(events[0].published_at, "")

    def test_empty_channel_returns_no_items_or_alerts(self) -> None:
        items = cwa.parse_rss_items(b'<rss version="2.0"><channel /></rss>')

        self.assertEqual(items, ())
        self.assertEqual(cwa.normalize_rss_alerts(items), ())


class APINormalizationTests(unittest.TestCase):
    def test_all_eight_fixed_dataset_ids_are_locked(self) -> None:
        self.assertEqual(
            cwa.API_DATASETS,
            {
                "weather": "F-C0032-001",
                "significant_earthquake": "E-A0015-001",
                "local_earthquake": "E-A0016-001",
                "tsunami": "E-A0014-001",
                "weather_warning": "W-C0033-002",
                "cold": "W-C0033-004",
                "heat": "W-C0033-005",
                "typhoon": "W-C0034-001",
            },
        )
        self.assertEqual(
            set(cwa.ALERT_DATASET_IDS),
            set(cwa.API_DATASETS.values()) - {cwa.API_DATASETS["weather"]},
        )

    def test_weather_elements_are_joined_by_time(self) -> None:
        forecasts = cwa.normalize_api_weather(API_WEATHER)
        self.assertEqual(len(forecasts), 1)
        forecast = forecasts[0]
        self.assertEqual(forecast.city, "臺北市")
        self.assertEqual(forecast.periods[0].weather, "多雲")
        self.assertEqual(forecast.periods[0].rain_probability, "40")
        self.assertEqual(forecast.periods[0].comfort, "悶熱")

    def test_earthquake_event_uses_documented_fields(self) -> None:
        payload = {
            "success": True,
            "records": {
                "Earthquake": [
                    {
                        "EarthquakeNo": 123,
                        "ReportType": "顯著有感地震報告",
                        "ReportContent": "07月12日臺東縣發生規模5.1地震。",
                        "ReportTime": "2026-07-12 08:00:00",
                        "Web": "https://www.cwa.gov.tw/V8/C/E/EQ/EQ.html",
                        "EarthquakeInfo": {"OriginTime": "2026-07-12 07:58:00"},
                    }
                ]
            },
        }
        events = cwa.normalize_api_alerts(
            cwa.API_DATASETS["significant_earthquake"], payload
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].official_id, "123")
        self.assertEqual(events[0].category, "significant_earthquake")
        self.assertEqual(events[0].areas, ("臺東縣",))
        self.assertEqual(events[0].effective_at, "2026-07-12 07:58:00")

    def test_warning_event_is_classified_and_extracts_areas(self) -> None:
        payload = {
            "records": {
                "record": [
                    {
                        "headline": "豪雨特報",
                        "description": "臺南市及高雄市有局部豪雨。",
                        "sent": "2026-07-12T08:00:00+08:00",
                        "identifier": "rain-1",
                        "area": {
                            "location": [
                                {"locationName": "臺南市"},
                                {"locationName": "高雄市"},
                            ]
                        },
                    }
                ]
            }
        }
        events = cwa.normalize_api_alerts(cwa.API_DATASETS["weather_warning"], payload)
        self.assertEqual(events[0].category, "rain")
        self.assertEqual(events[0].areas, ("臺南市", "高雄市"))

    def test_all_seven_alert_datasets_normalize_minimal_fixtures(self) -> None:
        for name, (expected_category, payload) in API_ALERT_CASES.items():
            with self.subTest(dataset=name):
                events = cwa.normalize_api_alerts(cwa.API_DATASETS[name], payload)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].category, expected_category)
                self.assertTrue(events[0].title)

    def test_all_eight_datasets_accept_empty_results(self) -> None:
        self.assertEqual(cwa.normalize_api_weather({"records": {}}), ())
        for name, dataset_id in cwa.API_DATASETS.items():
            if name == "weather":
                continue
            with self.subTest(dataset=name):
                self.assertEqual(
                    cwa.normalize_api_alerts(dataset_id, {"records": {}}),
                    (),
                )

    def test_all_eight_datasets_ignore_records_missing_required_content(self) -> None:
        weather_missing_elements = {
            "records": {"location": [{"locationName": "臺北市"}]}
        }
        self.assertEqual(cwa.normalize_api_weather(weather_missing_elements), ())

        missing_alert_content = {
            "records": {"record": [{"identifier": "missing-content"}]}
        }
        for name, dataset_id in cwa.API_DATASETS.items():
            if name == "weather":
                continue
            with self.subTest(dataset=name):
                self.assertEqual(
                    cwa.normalize_api_alerts(dataset_id, missing_alert_content),
                    (),
                )

    def test_json_bounds_shape_and_success_flag(self) -> None:
        self.assertEqual(
            cwa.parse_json_document(b'{"success":"true"}')["success"], "true"
        )
        with self.assertRaises(cwa.CWAResponseTooLargeError):
            cwa.parse_json_document(b"{}", max_bytes=1)
        with self.assertRaises(cwa.CWAParseError):
            cwa.parse_json_document(b"[]")
        with self.assertRaises(cwa.CWAParseError):
            cwa.parse_json_document(b'{"success":"false"}')

    def test_malformed_and_non_utf8_json_are_rejected(self) -> None:
        samples = (b"{", b'{"success": tru}', b"\xff\xfe\x00")
        for sample in samples:
            with self.subTest(sample=sample):
                with self.assertRaises(cwa.CWAParseError):
                    cwa.parse_json_document(sample)


class ImageSecurityTests(unittest.TestCase):
    @staticmethod
    def make_png() -> bytes:
        buffer = BytesIO()
        Image.new("RGBA", (2, 2), (255, 255, 255, 255)).save(buffer, format="PNG")
        return buffer.getvalue()

    def test_png_signature_decode_and_hash(self) -> None:
        data = self.make_png()
        digest = cwa.validate_typhoon_png(data)
        self.assertEqual(len(digest), 64)
        with self.assertRaises(cwa.CWASecurityError):
            cwa.validate_typhoon_png(b"not-png")

    def test_url_allowlist_rejects_scheme_host_port_and_path(self) -> None:
        self.assertEqual(
            cwa.validate_typhoon_url("https://www.cwa.gov.tw/Data/typhoon/I10.png?_=1"),
            "https://www.cwa.gov.tw/Data/typhoon/I10.png?_=1",
        )
        invalid = (
            "http://www.cwa.gov.tw/Data/typhoon/I10.png",
            "https://example.com/Data/typhoon/I10.png",
            "https://www.cwa.gov.tw:444/Data/typhoon/I10.png",
            "https://www.cwa.gov.tw/Data/typhoon/B20.png",
        )
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(cwa.CWASecurityError):
                    cwa.validate_typhoon_url(url)

    def test_cwa_tls_context_retains_certificate_and_hostname_checks(self) -> None:
        context = cwa.build_cwa_ssl_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
        if strict_flag:
            self.assertFalse(context.verify_flags & strict_flag)


class APIAlertBatchTests(unittest.IsolatedAsyncioTestCase):
    """Validate concurrent API alert aggregation and partial failures."""

    @staticmethod
    def make_event(dataset_id: str, index: int = 0) -> cwa.AlertEvent:
        """Create one deterministic normalized event.

        Args:
            dataset_id: Dataset ID embedded in the event identity.
            index: Event position within that dataset.

        Returns:
            Normalized alert event fixture.
        """

        return cwa.AlertEvent(
            source="api",
            category="other",
            title=f"{dataset_id}-{index}",
            description="測試警報",
            published_at="2026-07-12T08:00:00+08:00",
            effective_at="2026-07-12T08:00:00+08:00",
            expires_at="",
            areas=(),
            link="https://www.cwa.gov.tw/",
            official_id=f"{dataset_id}-{index}",
            bulletin_no="",
            is_cancellation=False,
        )

    async def test_one_failure_retains_other_events_and_original_error(self) -> None:
        selected = cwa.ALERT_DATASET_IDS
        failed_dataset_id = selected[3]
        secret = "CWA-BATCH-SECRET"
        raw_error = cwa.CWAAuthenticationError(
            401,
            "授權失敗：https://opendata.cwa.gov.tw/data?Authorization="
            f"{secret}&format=JSON",
        )
        client = cwa.CWAClient(session=FakeSession([]), api_key=secret)

        async def fetch(dataset_id: str) -> tuple[cwa.AlertEvent, ...]:
            if dataset_id == failed_dataset_id:
                raise raw_error
            return (self.make_event(dataset_id),)

        with patch.object(client, "fetch_api_alert_dataset", new=fetch):
            with self.assertRaises(cwa.CWAAlertBatchError) as caught:
                await client.fetch_api_alerts(selected)

        error = caught.exception
        self.assertEqual(
            [event.official_id for event in error.partial_events],
            [
                f"{dataset_id}-0"
                for dataset_id in selected
                if dataset_id != failed_dataset_id
            ],
        )
        self.assertEqual(error.failed_dataset_ids, (failed_dataset_id,))
        self.assertIs(error.dataset_errors[failed_dataset_id], raw_error)
        self.assertIsInstance(
            error.dataset_errors[failed_dataset_id],
            cwa.CWAAuthenticationError,
        )
        with self.assertRaises(TypeError):
            error.dataset_errors[failed_dataset_id] = RuntimeError("不可修改")
        self.assertNotIn(secret, str(error))
        self.assertNotIn("?", str(error))
        self.assertNotIn(secret, error.failure_summaries[failed_dataset_id])

    async def test_all_success_preserves_dataset_and_event_order(self) -> None:
        selected = cwa.ALERT_DATASET_IDS
        releases = {dataset_id: asyncio.Event() for dataset_id in selected}
        all_started = asyncio.Event()
        started: list[str] = []
        client = cwa.CWAClient(session=FakeSession([]), api_key="test-key")

        async def fetch(dataset_id: str) -> tuple[cwa.AlertEvent, ...]:
            started.append(dataset_id)
            if len(started) == len(selected):
                all_started.set()
            await releases[dataset_id].wait()
            return (
                self.make_event(dataset_id, 0),
                self.make_event(dataset_id, 1),
            )

        with patch.object(client, "fetch_api_alert_dataset", new=fetch):
            task = asyncio.create_task(client.fetch_api_alerts(selected))
            await asyncio.wait_for(all_started.wait(), timeout=1)
            for dataset_id in reversed(selected):
                releases[dataset_id].set()
                await asyncio.sleep(0)
            events = await task

        self.assertEqual(started, list(selected))
        self.assertEqual(
            [event.official_id for event in events],
            [f"{dataset_id}-{index}" for dataset_id in selected for index in (0, 1)],
        )

    async def test_cancellation_is_not_wrapped_as_partial_failure(self) -> None:
        selected = cwa.ALERT_DATASET_IDS[:2]
        client = cwa.CWAClient(session=FakeSession([]), api_key="test-key")

        async def fetch(dataset_id: str) -> tuple[cwa.AlertEvent, ...]:
            await asyncio.sleep(0)
            if dataset_id == selected[0]:
                raise asyncio.CancelledError
            return (self.make_event(dataset_id),)

        with patch.object(client, "fetch_api_alert_dataset", new=fetch):
            with self.assertRaises(asyncio.CancelledError):
                await client.fetch_api_alerts(selected)


class AsyncTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_injected_session_is_not_closed(self) -> None:
        session = FakeSession([])
        client = cwa.CWAClient(session=session)
        await client.close()
        self.assertFalse(session.closed)

    async def test_api_same_host_redirect_is_allowed_and_keeps_key(self) -> None:
        dataset_id = cwa.API_DATASETS["weather"]
        redirected_path = f"/api/v1/rest/datastore/{dataset_id}?canonical=1"
        session = FakeSession(
            [
                FakeResponse(status=302, headers={"Location": redirected_path}),
                FakeResponse(b'{"success":true,"records":{}}'),
            ]
        )
        secret = "CWA-REDIRECT-SECRET"
        client = cwa.CWAClient(
            session=session,
            api_key=secret,
            request_attempts=1,
        )

        payload = await client.fetch_api_dataset(dataset_id)

        self.assertEqual(payload["records"], {})
        self.assertEqual(len(session.requests), 2)
        self.assertTrue(
            session.requests[1][0].startswith("https://opendata.cwa.gov.tw/")
        )
        self.assertIn("canonical=1", session.requests[1][0])
        for _, kwargs in session.requests:
            self.assertFalse(kwargs["allow_redirects"])
            self.assertEqual(kwargs["params"]["Authorization"], secret)

    async def test_rss_same_host_allowed_feed_redirect_is_followed(self) -> None:
        session = FakeSession(
            [
                FakeResponse(
                    status=301,
                    headers={"Location": "/rss/forecast/36_01.xml"},
                ),
                FakeResponse(
                    b'<rss version="2.0"><channel /></rss>',
                    content_type="text/xml",
                ),
            ]
        )
        client = cwa.CWAClient(session=session, request_attempts=1)

        items = await client.fetch_rss_items(cwa.RSS_WARNING_URL)

        self.assertEqual(items, ())
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(
            session.requests[1][0],
            "https://www.cwa.gov.tw/rss/forecast/36_01.xml",
        )
        self.assertEqual(session.requests[1][1]["params"], {})

    async def test_api_redirect_policy_rejects_before_key_can_leak(self) -> None:
        dataset_id = cwa.API_DATASETS["weather"]
        allowed_path = f"/api/v1/rest/datastore/{dataset_id}"
        secret = "CWA-NEVER-FORWARD"
        invalid_locations = (
            f"https://evil.example{allowed_path}",
            f"http://opendata.cwa.gov.tw{allowed_path}",
            f"https://opendata.cwa.gov.tw:444{allowed_path}",
            "https://opendata.cwa.gov.tw/not-an-allowed-dataset",
        )
        for location in invalid_locations:
            with self.subTest(location=location):
                session = FakeSession(
                    [FakeResponse(status=302, headers={"Location": location})]
                )
                client = cwa.CWAClient(
                    session=session,
                    api_key=secret,
                    request_attempts=1,
                )

                with self.assertRaises(cwa.CWASecurityError) as caught:
                    await client.fetch_api_dataset(dataset_id)

                self.assertEqual(len(session.requests), 1)
                self.assertNotIn("evil.example", session.requests[0][0])
                self.assertNotIn(secret, str(caught.exception))

    async def test_rss_bad_path_and_fourth_redirect_are_rejected(self) -> None:
        bad_path_session = FakeSession(
            [
                FakeResponse(
                    status=302,
                    headers={"Location": "/rss/forecast/36_99.xml"},
                )
            ]
        )
        bad_path_client = cwa.CWAClient(
            session=bad_path_session,
            request_attempts=1,
        )
        with self.assertRaises(cwa.CWASecurityError):
            await bad_path_client.fetch_rss_items(cwa.RSS_WARNING_URL)
        self.assertEqual(len(bad_path_session.requests), 1)

        redirect_responses = [
            FakeResponse(
                status=302,
                headers={"Location": "/rss/Data/cwa_warning.xml"},
            )
            for _ in range(4)
        ]
        redirect_session = FakeSession(redirect_responses)
        redirect_client = cwa.CWAClient(
            session=redirect_session,
            request_attempts=1,
        )
        with self.assertRaisesRegex(cwa.CWASecurityError, "超過 3 次"):
            await redirect_client.fetch_rss_items(cwa.RSS_WARNING_URL)
        self.assertEqual(len(redirect_session.requests), 4)

    async def test_streaming_limit_stops_oversized_body(self) -> None:
        response = FakeResponse(b"123456", chunk_size=2)
        client = cwa.CWAClient(session=FakeSession([]))
        with self.assertRaises(cwa.CWAResponseTooLargeError):
            await client._read_limited(response, 5)

    async def test_download_validates_and_atomically_writes_png(self) -> None:
        data = ImageSecurityTests.make_png()
        response = FakeResponse(
            data,
            content_type="image/png; charset=binary",
            url="https://www.cwa.gov.tw/Data/typhoon/I10.png?_=fixed",
        )
        session = FakeSession([response])
        client = cwa.CWAClient(session=session)
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "bulletin.png"
            result = await client.download_typhoon_bulletin(
                destination,
                cache_token="fixed",
            )
            self.assertEqual(destination.read_bytes(), data)
            self.assertEqual(result.size, len(data))
            self.assertFalse(destination.with_name("bulletin.png.part").exists())
        self.assertEqual(len(session.requests), 1)
        self.assertFalse(session.requests[0][1]["allow_redirects"])

    async def test_redirect_is_validated_before_second_request(self) -> None:
        redirect = FakeResponse(
            status=302,
            content_type="text/html",
            headers={"Location": "https://example.com/steal.png"},
        )
        session = FakeSession([redirect])
        client = cwa.CWAClient(session=session)
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(cwa.CWASecurityError):
                await client.download_typhoon_bulletin(Path(temp_dir) / "bulletin.png")
        self.assertEqual(len(session.requests), 1)

    async def test_api_key_is_not_in_error_message(self) -> None:
        secret = "CWA-SECRET-DO-NOT-LOG"
        for status in (401, 403):
            with self.subTest(status=status):
                response = FakeResponse(
                    b"unauthorized",
                    status=status,
                    content_type="text/plain",
                )
                session = FakeSession([response])
                client = cwa.CWAClient(
                    session=session,
                    api_key=secret,
                    request_attempts=1,
                )
                with self.assertRaises(cwa.CWAAuthenticationError) as caught:
                    await client.fetch_api_dataset(cwa.API_DATASETS["weather"])
                self.assertEqual(caught.exception.status, status)
                self.assertNotIn(secret, str(caught.exception))
                self.assertEqual(
                    session.requests[0][1]["params"]["Authorization"], secret
                )

    async def test_timeout_is_wrapped_after_bounded_attempts(self) -> None:
        session = FakeSession([asyncio.TimeoutError()])
        client = cwa.CWAClient(
            session=session,
            api_key="test-key",
            request_attempts=1,
        )

        with self.assertRaisesRegex(cwa.CWAClientError, "連線失敗或逾時"):
            await client.fetch_api_dataset(cwa.API_DATASETS["weather"])

        self.assertEqual(len(session.requests), 1)

    async def test_rate_limit_raises_specialized_error(self) -> None:
        session = FakeSession([FakeResponse(status=429)])
        client = cwa.CWAClient(
            session=session,
            api_key="test-key",
            request_attempts=1,
        )

        with self.assertRaises(cwa.CWARateLimitError) as caught:
            await client.fetch_api_dataset(cwa.API_DATASETS["weather"])

        self.assertEqual(caught.exception.status, 429)

    async def test_transient_5xx_retries_then_succeeds(self) -> None:
        response = FakeResponse(b'{"success":true,"records":{}}')
        session = FakeSession([FakeResponse(status=503), response])
        client = cwa.CWAClient(
            session=session,
            api_key="test-key",
            request_attempts=2,
        )

        with patch.object(cwa.asyncio, "sleep", new=AsyncMock()) as sleep:
            payload = await client.fetch_api_dataset(cwa.API_DATASETS["weather"])

        self.assertEqual(payload["records"], {})
        self.assertEqual(len(session.requests), 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_exhausted_5xx_raises_http_error(self) -> None:
        for status in (500, 502, 503):
            with self.subTest(status=status):
                session = FakeSession([FakeResponse(status=status)])
                client = cwa.CWAClient(
                    session=session,
                    api_key="test-key",
                    request_attempts=1,
                )
                with self.assertRaises(cwa.CWAHTTPError) as caught:
                    await client.fetch_api_dataset(cwa.API_DATASETS["weather"])
                self.assertEqual(caught.exception.status, status)

    async def test_declared_api_body_over_size_limit_is_rejected(self) -> None:
        response = FakeResponse(
            b"{}",
            content_length=cwa.MAX_JSON_BYTES + 1,
        )
        client = cwa.CWAClient(
            session=FakeSession([response]),
            api_key="test-key",
            request_attempts=1,
        )

        with self.assertRaises(cwa.CWAResponseTooLargeError):
            await client.fetch_api_dataset(cwa.API_DATASETS["weather"])

    async def test_malformed_api_json_response_is_rejected(self) -> None:
        client = cwa.CWAClient(
            session=FakeSession([FakeResponse(b"{")]),
            api_key="test-key",
            request_attempts=1,
        )

        with self.assertRaises(cwa.CWAParseError):
            await client.fetch_api_dataset(cwa.API_DATASETS["weather"])


if __name__ == "__main__":
    unittest.main()
