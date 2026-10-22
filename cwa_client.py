"""Central Weather Administration network and normalization client.

This module deliberately keeps transport, parsing, and source normalization in
one dependency-light layer.  It does not depend on AstrBot, which makes the
security-sensitive parsers testable without loading the bot runtime.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import re
import ssl
import warnings
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import aiohttp
from PIL import Image, UnidentifiedImageError

API_BASE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"
API_DATASETS = {
    "weather": "F-C0032-001",
    "significant_earthquake": "E-A0015-001",
    "local_earthquake": "E-A0016-001",
    "tsunami": "E-A0014-001",
    "weather_warning": "W-C0033-002",
    "cold": "W-C0033-004",
    "heat": "W-C0033-005",
    "typhoon": "W-C0034-001",
}
ALERT_DATASET_IDS = tuple(
    dataset_id for name, dataset_id in API_DATASETS.items() if name != "weather"
)

RSS_WARNING_URL = "https://www.cwa.gov.tw/rss/Data/cwa_warning.xml"
RSS_FORECAST_URL_TEMPLATE = "https://www.cwa.gov.tw/rss/forecast/{code}.xml"
TYPHOON_BULLETIN_URL = "https://www.cwa.gov.tw/Data/typhoon/I10.png"

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_PNG_BYTES = 8 * 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CATEGORY_SIGNIFICANT_EARTHQUAKE = "significant_earthquake"
CATEGORY_LOCAL_EARTHQUAKE = "local_earthquake"
CATEGORY_TSUNAMI = "tsunami"
CATEGORY_TYPHOON = "typhoon"
CATEGORY_RAIN = "rain"
CATEGORY_WIND = "wind"
CATEGORY_FOG = "fog"
CATEGORY_HEAT = "heat"
CATEGORY_COLD = "cold"
CATEGORY_OTHER = "other"

CATEGORY_LABELS = {
    CATEGORY_SIGNIFICANT_EARTHQUAKE: "顯著有感地震",
    CATEGORY_LOCAL_EARTHQUAKE: "小區域有感地震",
    CATEGORY_TSUNAMI: "海嘯",
    CATEGORY_TYPHOON: "颱風",
    CATEGORY_RAIN: "豪雨／大雨",
    CATEGORY_WIND: "陸上強風",
    CATEGORY_FOG: "濃霧",
    CATEGORY_HEAT: "高溫",
    CATEGORY_COLD: "低溫",
    CATEGORY_OTHER: "其他天氣警報",
}

# The codes are published on the CWA RSS service page.  Keep the canonical
# spelling used by the agency and normalize common 台/臺 input separately.
CITY_RSS_CODES = {
    "基隆市": "36_03",
    "臺北市": "36_01",
    "新北市": "36_04",
    "桃園市": "36_05",
    "新竹市": "36_14",
    "新竹縣": "36_06",
    "苗栗縣": "36_07",
    "臺中市": "36_08",
    "南投縣": "36_10",
    "彰化縣": "36_09",
    "雲林縣": "36_11",
    "嘉義縣": "36_12",
    "嘉義市": "36_16",
    "臺南市": "36_13",
    "高雄市": "36_02",
    "屏東縣": "36_15",
    "臺東縣": "36_19",
    "花蓮縣": "36_18",
    "宜蘭縣": "36_17",
    "澎湖縣": "36_20",
    "金門縣": "36_21",
    "連江縣": "36_22",
}

_CITY_ALIASES = {
    city.replace("臺", "台"): city for city in CITY_RSS_CODES if "臺" in city
}
_CITY_ALIASES.update({city: city for city in CITY_RSS_CODES})

_XML_DECLARATION_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.I)
_BULLETIN_PATTERN = re.compile(r"第\s*(\d+)\s*報")
_CANCELLATION_PATTERN = re.compile(
    r"(?:解除|取消|撤銷|終止|結束).{0,8}(?:警報|特報|資訊)|(?:警報|特報).{0,8}(?:解除|取消|結束)"
)


class CWAClientError(RuntimeError):
    """Base exception with a safe Traditional Chinese public message."""


class CWAAlertBatchError(CWAClientError):
    """Report partial alert results from a concurrent API batch.

    ``dataset_errors`` intentionally retains the original exceptions for
    programmatic handling.  Its mapping is read-only, while ``str(error)`` and
    ``failure_summaries`` contain only sanitized text.

    Args:
        partial_events: Events returned by successful datasets, already in
            deterministic dataset and event order.
        dataset_errors: Failed dataset IDs paired with their original
            exceptions, in deterministic dataset order.
        secrets: Exact secret values that must not appear in public summaries.
    """

    def __init__(
        self,
        partial_events: Iterable[AlertEvent],
        dataset_errors: Sequence[tuple[str, BaseException]],
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        self.partial_events = tuple(partial_events)
        ordered_errors = tuple(dataset_errors)
        self.failed_dataset_ids = tuple(dataset_id for dataset_id, _ in ordered_errors)
        self.dataset_errors = MappingProxyType(dict(ordered_errors))

        secret_values = tuple(str(secret) for secret in secrets if str(secret))
        summaries: dict[str, str] = {}
        url_pattern = re.compile(r"https?://[^\s<>'\"]+", re.I)
        authorization_pattern = re.compile(
            r"(authorization\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|[^&\s,;]+)",
            re.I,
        )
        for dataset_id, error in ordered_errors:
            summary = str(error).strip() or error.__class__.__name__

            def scrub_url(match: re.Match[str]) -> str:
                parsed = urlsplit(match.group(0))
                return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))

            summary = url_pattern.sub(scrub_url, summary)
            summary = authorization_pattern.sub(r"\1[已遮罩]", summary)
            for secret in secret_values:
                summary = summary.replace(secret, "[已遮罩]")
            summaries[dataset_id] = summary[:300]
        self.failure_summaries = MappingProxyType(summaries)

        details = "；".join(
            f"{dataset_id}：{summaries[dataset_id]}"
            for dataset_id in self.failed_dataset_ids
        )
        super().__init__(
            f"中央氣象署部分警報資料集查詢失敗"
            f"（失敗 {len(self.failed_dataset_ids)} 個資料集）："
            f"{details}"
        )


class CWAHTTPError(CWAClientError):
    """Raised when an upstream endpoint returns an unexpected status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class CWAAuthenticationError(CWAHTTPError):
    """Raised when an API authorization code is absent or rejected."""


class CWARateLimitError(CWAHTTPError):
    """Raised when CWA applies request throttling."""


class CWAResponseTooLargeError(CWAClientError):
    """Raised before parsing a response that exceeds its byte limit."""


class CWAParseError(CWAClientError):
    """Raised for malformed or unsupported CWA data."""


class CWASecurityError(CWAClientError):
    """Raised when a response or redirect violates a security policy."""


@dataclass(frozen=True, slots=True)
class ForecastPeriod:
    """One normalized forecast period."""

    start_time: str
    end_time: str
    weather: str
    rain_probability: str
    min_temperature: str
    max_temperature: str
    comfort: str


@dataclass(frozen=True, slots=True)
class WeatherForecast:
    """A normalized 36-hour forecast for one CWA county or city."""

    source: str
    city: str
    published_at: str
    periods: tuple[ForecastPeriod, ...]
    link: str = ""
    official_id: str = ""


@dataclass(frozen=True, slots=True)
class AlertEvent:
    """A normalized disaster or weather alert."""

    source: str
    category: str
    title: str
    description: str
    published_at: str
    effective_at: str
    expires_at: str
    areas: tuple[str, ...]
    link: str
    official_id: str
    bulletin_no: str
    is_cancellation: bool


@dataclass(frozen=True, slots=True)
class RSSItem:
    """The subset of RSS 2.0 fields used by this plugin."""

    title: str
    description: str
    link: str
    guid: str
    published_at: str


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """Metadata for a validated and atomically stored typhoon bulletin."""

    path: Path
    sha256: str
    size: int
    content_type: str
    final_url: str


def normalize_city(city: str) -> str:
    """Return the canonical CWA city name.

    Args:
        city: User-provided county or city name.

    Raises:
        ValueError: If the city is not one of the 22 supported CWA areas.
    """

    normalized = re.sub(r"\s+", "", str(city or "")).strip()
    canonical = _CITY_ALIASES.get(normalized)
    if canonical is None:
        raise ValueError(f"不支援的縣市：{city}")
    return canonical


def extract_cities(text: str) -> tuple[str, ...]:
    """Extract canonical CWA city names from official text."""

    normalized_text = str(text or "").replace("台", "臺")
    return tuple(city for city in CITY_RSS_CODES if city in normalized_text)


def classify_alert(title: str, description: str = "") -> str:
    """Classify an official alert without rewriting its content."""

    title_text = str(title or "")
    text = f"{title_text}\n{description or ''}"
    if re.search(r"小區域.{0,8}地震|地震小區域", text):
        return CATEGORY_LOCAL_EARTHQUAKE
    if "地震" in title_text or re.search(r"地震(?:報告|資訊|消息)", text):
        return CATEGORY_SIGNIFICANT_EARTHQUAKE
    if "海嘯" in text:
        return CATEGORY_TSUNAMI
    if re.search(r"颱風|熱帶性低氣壓", text):
        return CATEGORY_TYPHOON
    if re.search(r"豪雨|大雨|降雨|雨量", title_text):
        return CATEGORY_RAIN
    if re.search(r"強風|風力", title_text):
        return CATEGORY_WIND
    if "濃霧" in text:
        return CATEGORY_FOG
    if re.search(r"高溫|酷熱", title_text):
        return CATEGORY_HEAT
    if re.search(r"低溫|寒冷", title_text):
        return CATEGORY_COLD
    return CATEGORY_OTHER


class _PlainTextHTMLParser(HTMLParser):
    """Convert the small HTML subset used in RSS descriptions to text."""

    _BLOCK_TAGS = {"br", "div", "p", "li", "tr", "hr"}
    _IGNORED_TAGS = {"script", "style"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignore_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS:
            self._ignore_depth += 1
        elif lowered in self._BLOCK_TAGS and not self._ignore_depth:
            self.parts.append("\n")

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in self._IGNORED_TAGS and self._ignore_depth:
            self._ignore_depth -= 1
        elif lowered in self._BLOCK_TAGS and not self._ignore_depth:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._ignore_depth:
            self.parts.append(data)

    def text(self) -> str:
        """Return normalized plain text while retaining line boundaries."""

        joined = html.unescape("".join(self.parts)).replace("\xa0", " ")
        lines = [re.sub(r"[\t ]+", " ", line).strip() for line in joined.splitlines()]
        return "\n".join(line for line in lines if line)


def html_to_text(value: str) -> str:
    """Safely convert RSS description markup to plain text."""

    parser = _PlainTextHTMLParser()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except (ValueError, AssertionError) as exc:
        raise CWAParseError("RSS 內容含有無法解析的標記。") from exc
    return parser.text()


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(element: ElementTree.Element, name: str) -> str:
    lowered = name.lower()
    for child in element:
        if _local_name(child.tag) != lowered:
            continue
        if lowered == "link" and not "".join(child.itertext()).strip():
            return str(child.attrib.get("href", "")).strip()
        return "".join(child.itertext()).strip()
    return ""


def _normalize_rss_date(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def parse_rss_items(
    data: bytes, *, max_bytes: int = MAX_XML_BYTES
) -> tuple[RSSItem, ...]:
    """Parse a bounded RSS feed and reject entity declarations.

    Args:
        data: Raw, decompressed XML bytes.
        max_bytes: Maximum accepted response size.

    Raises:
        CWAResponseTooLargeError: If the response exceeds ``max_bytes``.
        CWASecurityError: If a DTD or entity declaration is present.
        CWAParseError: If the XML is malformed.
    """

    if len(data) > max_bytes:
        raise CWAResponseTooLargeError("RSS 回應超過 2 MiB 安全上限。")
    if _XML_DECLARATION_PATTERN.search(data):
        raise CWASecurityError("RSS 回應含有不允許的 DTD 或 ENTITY 宣告。")
    try:
        root = ElementTree.fromstring(data)
    except (ElementTree.ParseError, ValueError) as exc:
        raise CWAParseError("中央氣象署 RSS XML 格式無法解析。") from exc

    result: list[RSSItem] = []
    for element in root.iter():
        if _local_name(element.tag) != "item":
            continue
        title = html_to_text(_child_text(element, "title"))
        description = html_to_text(_child_text(element, "description"))
        link = html.unescape(_child_text(element, "link")).strip()
        guid = html.unescape(_child_text(element, "guid")).strip()
        published = _normalize_rss_date(_child_text(element, "pubDate"))
        if title or description or link or guid:
            result.append(
                RSSItem(
                    title=title,
                    description=description,
                    link=link,
                    guid=guid,
                    published_at=published,
                )
            )
    return tuple(result)


def normalize_rss_alerts(items: Iterable[RSSItem]) -> tuple[AlertEvent, ...]:
    """Normalize RSS warning items into source-independent alert events."""

    events: list[AlertEvent] = []
    for item in items:
        combined = f"{item.title}\n{item.description}"
        bulletin_match = _BULLETIN_PATTERN.search(combined)
        events.append(
            AlertEvent(
                source="rss",
                category=classify_alert(item.title, item.description),
                title=item.title,
                description=item.description,
                published_at=item.published_at,
                effective_at=item.published_at,
                expires_at="",
                areas=extract_cities(combined),
                link=item.link,
                official_id=item.guid or item.link,
                bulletin_no=bulletin_match.group(1) if bulletin_match else "",
                is_cancellation=bool(_CANCELLATION_PATTERN.search(combined)),
            )
        )
    return tuple(events)


_FORECAST_LABELS = (
    "今日白天",
    "今晚明晨",
    "明日白天",
    "明晚後天",
    "今明兩天",
    "今晚至明晨",
    "明日晚上",
    "後日白天",
    "白天",
    "晚上",
)
_FORECAST_LINE_PATTERN = re.compile(
    rf"^(?P<label>(?:\d{{2}}/\d{{2}}\s*)?(?:{'|'.join(_FORECAST_LABELS)}))\s+"
    r"(?P<weather>.+?)\s+溫度\s*:\s*(?P<min>-?\d+(?:\.\d+)?)\s*[~～至]\s*"
    r"(?P<max>-?\d+(?:\.\d+)?)"
    r"(?:\s+降雨機率\s*:\s*(?P<rain>\d+)\s*%?)?$"
)
_PUBLISHED_IN_TITLE_PATTERN = re.compile(r"\s*\(([^()]+?)發布\)\s*$")


def _parse_rss_forecast_line(line: str) -> ForecastPeriod | None:
    normalized = re.sub(r"\s+", " ", line).strip()
    match = _FORECAST_LINE_PATTERN.match(normalized)
    if not match:
        return None
    return ForecastPeriod(
        start_time=match.group("label").strip(),
        end_time="",
        weather=match.group("weather").strip(),
        rain_probability=(match.group("rain") or "").strip(),
        min_temperature=match.group("min"),
        max_temperature=match.group("max"),
        comfort="",
    )


def normalize_rss_weather(city: str, items: Sequence[RSSItem]) -> WeatherForecast:
    """Normalize the first 36-hour item in a county forecast RSS feed."""

    canonical = normalize_city(city)
    selected = next((item for item in items if "一週" not in item.title), None)
    if selected is None:
        raise CWAParseError(f"{canonical} RSS 目前沒有 36 小時天氣預報。")

    title_without_city = selected.title
    for alias, alias_canonical in _CITY_ALIASES.items():
        if alias_canonical == canonical and title_without_city.startswith(alias):
            title_without_city = title_without_city[len(alias) :]
            break
    title_without_published = _PUBLISHED_IN_TITLE_PATTERN.sub("", title_without_city)
    lines = [title_without_published, *selected.description.splitlines()]
    periods = tuple(
        parsed
        for line in lines
        if (parsed := _parse_rss_forecast_line(line)) is not None
    )
    if not periods:
        raise CWAParseError(f"{canonical} RSS 天氣欄位格式無法辨識。")
    return WeatherForecast(
        source="rss",
        city=canonical,
        published_at=selected.published_at,
        periods=periods[:3],
        link=selected.link,
        official_id=selected.guid or selected.link,
    )


def parse_json_document(
    data: bytes, *, max_bytes: int = MAX_JSON_BYTES
) -> Mapping[str, Any]:
    """Decode one bounded JSON object returned by the Open Data API."""

    if len(data) > max_bytes:
        raise CWAResponseTooLargeError("開放資料 API 回應超過 4 MiB 安全上限。")
    try:
        value = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CWAParseError("中央氣象署 API JSON 格式無法解析。") from exc
    if not isinstance(value, Mapping):
        raise CWAParseError("中央氣象署 API 回應不是 JSON 物件。")
    success = value.get("success", True)
    if success not in (True, "true", "True", 1, "1"):
        raise CWAParseError("中央氣象署 API 回報查詢失敗。")
    return value


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_sequence(value: Any) -> Sequence[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return ()


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value).strip()
    return ""


def _element_parameter_value(time_item: Mapping[str, Any]) -> str:
    parameter = _as_mapping(time_item.get("parameter"))
    for key in ("parameterName", "parameterValue", "value"):
        result = _string_value(parameter.get(key))
        if result:
            return result
    values = _as_sequence(time_item.get("elementValue"))
    for value in values:
        mapping = _as_mapping(value)
        result = _string_value(mapping.get("value") or mapping.get("elementValue"))
        if result:
            return result
    return _string_value(time_item.get("value"))


def normalize_api_weather(payload: Mapping[str, Any]) -> tuple[WeatherForecast, ...]:
    """Normalize the CWA 36-hour API response."""

    records = _as_mapping(payload.get("records"))
    locations = _as_sequence(records.get("location") or records.get("Location"))
    forecasts: list[WeatherForecast] = []
    for location_value in locations:
        location = _as_mapping(location_value)
        city_value = _string_value(
            location.get("locationName") or location.get("LocationName")
        )
        try:
            city = normalize_city(city_value)
        except ValueError:
            continue

        periods_by_key: dict[tuple[str, str], dict[str, str]] = {}
        period_order: list[tuple[str, str]] = []
        elements = _as_sequence(
            location.get("weatherElement") or location.get("WeatherElement")
        )
        for element_value in elements:
            element = _as_mapping(element_value)
            element_name = _string_value(
                element.get("elementName") or element.get("ElementName")
            )
            for time_value in _as_sequence(element.get("time") or element.get("Time")):
                time_item = _as_mapping(time_value)
                start = _string_value(
                    time_item.get("startTime") or time_item.get("StartTime")
                )
                end = _string_value(
                    time_item.get("endTime") or time_item.get("EndTime")
                )
                key = (start, end)
                if key not in periods_by_key:
                    periods_by_key[key] = {}
                    period_order.append(key)
                periods_by_key[key][element_name] = _element_parameter_value(time_item)

        periods: list[ForecastPeriod] = []
        for start, end in period_order:
            values = periods_by_key[(start, end)]
            periods.append(
                ForecastPeriod(
                    start_time=start,
                    end_time=end,
                    weather=values.get("Wx", ""),
                    rain_probability=values.get("PoP", values.get("PoP12h", "")),
                    min_temperature=values.get("MinT", ""),
                    max_temperature=values.get("MaxT", ""),
                    comfort=values.get("CI", ""),
                )
            )
        if periods:
            forecasts.append(
                WeatherForecast(
                    source="api",
                    city=city,
                    published_at=_string_value(
                        records.get("publishTime") or records.get("updateTime")
                    ),
                    periods=tuple(periods[:3]),
                    official_id=API_DATASETS["weather"],
                )
            )
    return tuple(forecasts)


def _iter_mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _iter_mappings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            yield from _iter_mappings(child)


def _find_first_value(value: Any, keys: Sequence[str]) -> str:
    wanted = {key.lower() for key in keys}
    for mapping in _iter_mappings(value):
        for key, candidate in mapping.items():
            if str(key).lower() in wanted:
                result = _string_value(candidate)
                if result:
                    return result
    return ""


def _collect_values(value: Any, keys: Sequence[str]) -> tuple[str, ...]:
    wanted = {key.lower() for key in keys}
    results: list[str] = []
    for mapping in _iter_mappings(value):
        for key, candidate in mapping.items():
            if str(key).lower() not in wanted:
                continue
            if text := _string_value(candidate):
                results.append(text)
    return tuple(dict.fromkeys(results))


def _find_record_list(value: Any, keys: Sequence[str]) -> tuple[Mapping[str, Any], ...]:
    wanted = {key.lower() for key in keys}
    for mapping in _iter_mappings(value):
        for key, candidate in mapping.items():
            if str(key).lower() not in wanted:
                continue
            values = _as_sequence(candidate)
            records = tuple(
                _as_mapping(item) for item in values if isinstance(item, Mapping)
            )
            if records:
                return records
            if isinstance(candidate, Mapping):
                return (_as_mapping(candidate),)
    return ()


def _api_event_records(
    dataset_id: str, records: Mapping[str, Any]
) -> tuple[Mapping[str, Any], ...]:
    if dataset_id in {
        API_DATASETS["significant_earthquake"],
        API_DATASETS["local_earthquake"],
    }:
        keys = ("Earthquake", "earthquake")
    elif dataset_id == API_DATASETS["tsunami"]:
        keys = ("Tsunami", "tsunami", "record")
    elif dataset_id == API_DATASETS["typhoon"]:
        keys = ("tropicalCyclone", "typhoon", "record")
    else:
        keys = ("record", "hazard", "location")
    found = _find_record_list(records, keys)
    if found:
        return found
    return (records,) if records else ()


def _fixed_category_for_dataset(dataset_id: str) -> str | None:
    return {
        API_DATASETS["significant_earthquake"]: CATEGORY_SIGNIFICANT_EARTHQUAKE,
        API_DATASETS["local_earthquake"]: CATEGORY_LOCAL_EARTHQUAKE,
        API_DATASETS["tsunami"]: CATEGORY_TSUNAMI,
        API_DATASETS["typhoon"]: CATEGORY_TYPHOON,
        API_DATASETS["cold"]: CATEGORY_COLD,
        API_DATASETS["heat"]: CATEGORY_HEAT,
    }.get(dataset_id)


def normalize_api_alerts(
    dataset_id: str,
    payload: Mapping[str, Any],
) -> tuple[AlertEvent, ...]:
    """Normalize one supported CWA alert dataset.

    The CWA datasets are not schema-identical.  Exact, stable field names are
    preferred first; recursive aliases cover the documented earthquake, CAP,
    weather-warning, tsunami, and tropical-cyclone shapes.
    """

    if dataset_id not in ALERT_DATASET_IDS:
        raise ValueError(f"不支援的中央氣象署資料集：{dataset_id}")
    records = _as_mapping(payload.get("records"))
    fixed_category = _fixed_category_for_dataset(dataset_id)
    dataset_description = _find_first_value(
        records,
        ("datasetDescription", "DatasetDescription"),
    )
    events: list[AlertEvent] = []
    for record in _api_event_records(dataset_id, records):
        title = _find_first_value(
            record,
            (
                "ReportType",
                "reportType",
                "headline",
                "event",
                "title",
                "phenomena",
                "typhoonName",
            ),
        )
        description = _find_first_value(
            record,
            (
                "ReportContent",
                "reportContent",
                "description",
                "content",
                "instruction",
                "summary",
            ),
        )
        if not title:
            title = dataset_description
        if not title and not description:
            continue
        category = fixed_category or classify_alert(title, description)
        if not title:
            title = CATEGORY_LABELS[category]

        published = _find_first_value(
            record,
            ("ReportTime", "sent", "publishTime", "issueTime", "updateTime"),
        )
        effective = _find_first_value(
            record,
            ("effective", "startTime", "OriginTime", "originTime"),
        )
        expires = _find_first_value(record, ("expires", "endTime"))
        official_id = _find_first_value(
            record,
            (
                "EarthquakeNo",
                "identifier",
                "eventId",
                "eventID",
                "typhoonNo",
                "reportNo",
                "id",
            ),
        )
        link = _find_first_value(
            record,
            ("Web", "web", "link", "url", "ReportURI", "reportURI"),
        )
        area_texts = _collect_values(
            record,
            ("locationName", "LocationName", "areaDesc", "areaName"),
        )
        area_source = "\n".join((*area_texts, title, description))
        areas = extract_cities(area_source)
        bulletin_direct = _find_first_value(
            record,
            ("bulletinNo", "bulletinNumber", "reportNo"),
        )
        bulletin_match = _BULLETIN_PATTERN.search(f"{title}\n{description}")
        bulletin_no = bulletin_direct or (
            bulletin_match.group(1) if bulletin_match else ""
        )
        combined = f"{title}\n{description}"
        events.append(
            AlertEvent(
                source="api",
                category=category,
                title=title,
                description=description,
                published_at=published or effective,
                effective_at=effective or published,
                expires_at=expires,
                areas=areas,
                link=link,
                official_id=official_id,
                bulletin_no=bulletin_no,
                is_cancellation=bool(_CANCELLATION_PATTERN.search(combined)),
            )
        )
    return tuple(events)


def validate_typhoon_url(url: str) -> str:
    """Validate and normalize a bulletin URL before any network request."""

    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise CWASecurityError("颱風警報單網址格式不正確。") from exc
    if parsed.scheme.lower() != "https":
        raise CWASecurityError("颱風警報單只允許使用 HTTPS。")
    if parsed.username or parsed.password:
        raise CWASecurityError("颱風警報單網址不得包含使用者資訊。")
    if (parsed.hostname or "").lower() != "www.cwa.gov.tw" or port not in (None, 443):
        raise CWASecurityError("颱風警報單重新導向至未允許的主機。")
    if parsed.path != "/Data/typhoon/I10.png":
        raise CWASecurityError("颱風警報單路徑不在允許清單內。")
    return urlunsplit(("https", "www.cwa.gov.tw", parsed.path, parsed.query, ""))


def validate_typhoon_png(data: bytes) -> str:
    """Validate PNG signature and decoding, then return its SHA-256 hash."""

    if len(data) > MAX_PNG_BYTES:
        raise CWAResponseTooLargeError("颱風警報單超過 8 MiB 安全上限。")
    if not data.startswith(PNG_SIGNATURE):
        raise CWASecurityError("颱風警報單不是有效的 PNG 檔案。")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                if image.format != "PNG":
                    raise CWASecurityError("颱風警報單的圖片格式不是 PNG。")
                image.verify()
    except CWASecurityError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise CWASecurityError("颱風警報單 PNG 解碼驗證失敗。") from exc
    return hashlib.sha256(data).hexdigest()


def _validate_cwa_data_url(url: str, source_kind: str) -> str:
    """Validate one API or RSS URL before issuing its request.

    Args:
        url: Initial or redirected URL.
        source_kind: Either ``api`` or ``rss``.

    Returns:
        Normalized HTTPS URL without a fragment.

    Raises:
        CWASecurityError: The URL leaves the source-specific allowlist.
        ValueError: ``source_kind`` is unsupported.
    """

    try:
        parsed = urlsplit(str(url))
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise CWASecurityError("中央氣象署重新導向網址格式不正確。") from exc
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password:
        raise CWASecurityError("中央氣象署資料來源只允許使用受驗證的 HTTPS 網址。")
    if port not in (None, 443):
        raise CWASecurityError("中央氣象署重新導向使用未允許的連接埠。")

    hostname = (parsed.hostname or "").lower()
    if source_kind == "api":
        allowed_paths = {
            f"/api/v1/rest/datastore/{dataset_id}"
            for dataset_id in API_DATASETS.values()
        }
        if hostname != "opendata.cwa.gov.tw" or parsed.path not in allowed_paths:
            raise CWASecurityError("開放資料 API 重新導向離開允許的主機或資料集。")
        netloc = "opendata.cwa.gov.tw"
    elif source_kind == "rss":
        allowed_paths = {"/rss/Data/cwa_warning.xml"}
        allowed_paths.update(
            f"/rss/forecast/{code}.xml" for code in CITY_RSS_CODES.values()
        )
        if hostname != "www.cwa.gov.tw" or parsed.path not in allowed_paths:
            raise CWASecurityError("RSS 重新導向離開允許的主機或頻道。")
        netloc = "www.cwa.gov.tw"
    else:
        raise ValueError(f"不支援的中央氣象署資料來源種類：{source_kind}")
    return urlunsplit(("https", netloc, parsed.path, parsed.query, ""))


def build_cwa_ssl_context() -> ssl.SSLContext:
    """Build a verified TLS context compatible with the current CWA chain.

    Python 3.14 enables ``VERIFY_X509_STRICT`` in its default client context.
    The current CWA certificate chain contains a CA certificate without a
    Subject Key Identifier, which OpenSSL rejects only in strict RFC 5280
    mode.  Clearing that one flag retains CA-chain verification,
    ``CERT_REQUIRED``, and hostname verification; it does not trust self-signed
    certificates or disable TLS validation.

    Returns:
        A verified TLS client context suitable for CWA HTTPS endpoints.
    """

    context = ssl.create_default_context()
    strict_flag = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict_flag:
        context.verify_flags &= ~strict_flag
    return context


class CWAClient:
    """Async client using exactly one reusable aiohttp session.

    Args:
        session: Optional session owned by the caller.  When omitted, one
            session is created lazily and closed by :meth:`close`.
        api_key: Optional CWA Open Data authorization code.
        timeout_seconds: Total timeout applied to each request.
        request_attempts: Bounded attempts for transient HTTP and network
            failures. Authentication and parse failures are never retried.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession | None = None,
        api_key: str | None = None,
        *,
        timeout_seconds: float = 15.0,
        request_attempts: int = 3,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("HTTP 逾時秒數必須大於零。")
        if not 1 <= request_attempts <= 5:
            raise ValueError("HTTP 重試次數必須介於 1 至 5 次。")
        self._session = session
        self._owns_session = session is None
        self.api_key = str(api_key or "").strip()
        self.timeout_seconds = float(timeout_seconds)
        self.request_attempts = request_attempts
        self._ssl_context = ssl_context or build_cwa_ssl_context()

    @property
    def session(self) -> aiohttp.ClientSession | None:
        """Return the current shared session without creating it."""

        return self._session

    async def __aenter__(self) -> CWAClient:
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                trust_env=True,
                headers={
                    "Accept-Encoding": "gzip, deflate",
                    "User-Agent": "astrbot-plugin-cwa-alert/0.1.2",
                },
            )
        if self._session.closed:
            raise CWAClientError("中央氣象署 HTTP 工作階段已關閉。")
        return self._session

    async def close(self) -> None:
        """Close the lazily owned session; injected sessions remain caller-owned."""

        if (
            self._owns_session
            and self._session is not None
            and not self._session.closed
        ):
            await self._session.close()

    async def _read_limited(
        self, response: aiohttp.ClientResponse, limit: int
    ) -> bytes:
        content_length = response.content_length
        if content_length is not None and content_length > limit:
            raise CWAResponseTooLargeError("中央氣象署回應超過安全大小上限。")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.content.iter_chunked(64 * 1024):
            size += len(chunk)
            if size > limit:
                raise CWAResponseTooLargeError("中央氣象署回應超過安全大小上限。")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _content_type(response: aiohttp.ClientResponse) -> str:
        return (
            str(response.headers.get("Content-Type", ""))
            .split(";", 1)[0]
            .strip()
            .lower()
        )

    @staticmethod
    def _raise_status(status: int, source_label: str) -> None:
        if status in (401, 403):
            raise CWAAuthenticationError(
                status, "中央氣象署授權碼無效或沒有資料集權限。"
            )
        if status == 429:
            raise CWARateLimitError(status, "中央氣象署暫時限制請求頻率，請稍後再試。")
        raise CWAHTTPError(status, f"{source_label}回應 HTTP {status}。")

    async def _request_bytes(
        self,
        url: str,
        *,
        source_kind: str,
        source_label: str,
        limit: int,
        accepted_types: tuple[str, ...],
        params: Mapping[str, str] | None = None,
    ) -> bytes:
        session = await self._ensure_session()
        last_error: BaseException | None = None
        initial_url = _validate_cwa_data_url(url, source_kind)
        request_params = dict(params or {})
        for attempt in range(self.request_attempts):
            try:
                current_url = initial_url
                should_retry = False
                loop = asyncio.get_running_loop()
                deadline = loop.time() + self.timeout_seconds
                for redirect_count in range(4):
                    remaining_timeout = deadline - loop.time()
                    if remaining_timeout <= 0:
                        raise asyncio.TimeoutError
                    async with session.get(
                        current_url,
                        params=request_params,
                        allow_redirects=False,
                        ssl=self._ssl_context,
                        timeout=aiohttp.ClientTimeout(total=remaining_timeout),
                    ) as response:
                        status = response.status
                        if status in (301, 302, 303, 307, 308):
                            if redirect_count >= 3:
                                raise CWASecurityError(
                                    f"{source_label}重新導向次數超過 3 次。"
                                )
                            location = str(response.headers.get("Location", "")).strip()
                            if not location:
                                raise CWASecurityError(
                                    f"{source_label}重新導向缺少目標網址。"
                                )
                            current_url = _validate_cwa_data_url(
                                urljoin(current_url, location),
                                source_kind,
                            )
                            continue
                        if status >= 400:
                            if (status == 429 or status >= 500) and (
                                attempt + 1 < self.request_attempts
                            ):
                                should_retry = True
                                break
                            self._raise_status(status, source_label)
                        content_type = self._content_type(response)
                        if content_type not in accepted_types:
                            raise CWAParseError(
                                f"{source_label}回傳非預期的 Content-Type："
                                f"{content_type or '未提供'}。"
                            )
                        return await self._read_limited(response, limit)
                if should_retry:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise CWASecurityError(f"{source_label}重新導向流程異常。")
            except (CWAClientError, asyncio.CancelledError):
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
                last_error = exc
                if attempt + 1 < self.request_attempts:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
        raise CWAClientError(f"{source_label}連線失敗或逾時。") from last_error

    async def fetch_api_dataset(self, dataset_id: str) -> Mapping[str, Any]:
        """Fetch and validate one allowlisted Open Data API dataset."""

        if dataset_id not in API_DATASETS.values():
            raise ValueError(f"不支援的中央氣象署資料集：{dataset_id}")
        if not self.api_key:
            raise CWAAuthenticationError(401, "尚未設定中央氣象署開放資料授權碼。")
        raw = await self._request_bytes(
            f"{API_BASE_URL}/{dataset_id}",
            source_kind="api",
            source_label="中央氣象署開放資料 API",
            limit=MAX_JSON_BYTES,
            accepted_types=("application/json", "text/json"),
            params={"Authorization": self.api_key, "format": "JSON"},
        )
        return parse_json_document(raw)

    async def fetch_api_weather(
        self,
        cities: Iterable[str] | None = None,
    ) -> tuple[WeatherForecast, ...]:
        """Fetch normalized API forecasts, optionally filtered by city."""

        payload = await self.fetch_api_dataset(API_DATASETS["weather"])
        forecasts = normalize_api_weather(payload)
        if cities is None:
            return forecasts
        selected = {normalize_city(city) for city in cities}
        return tuple(forecast for forecast in forecasts if forecast.city in selected)

    async def fetch_api_alert_dataset(self, dataset_id: str) -> tuple[AlertEvent, ...]:
        """Fetch and normalize one alert dataset."""

        if dataset_id not in ALERT_DATASET_IDS:
            raise ValueError(f"不支援的中央氣象署警報資料集：{dataset_id}")
        payload = await self.fetch_api_dataset(dataset_id)
        return normalize_api_alerts(dataset_id, payload)

    async def fetch_api_alerts(
        self,
        dataset_ids: Iterable[str] | None = None,
    ) -> tuple[AlertEvent, ...]:
        """Fetch selected alert datasets concurrently and preserve ordering.

        Args:
            dataset_ids: Optional alert dataset selection. Defaults to all
                seven IDs in ``ALERT_DATASET_IDS`` order.

        Returns:
            All normalized events in dataset order and each dataset's original
            event order.

        Raises:
            ValueError: A selected dataset is not an alert dataset.
            CWAAlertBatchError: One or more datasets failed. Successful events
                remain available through ``partial_events``.
            asyncio.CancelledError: The caller or a child request was cancelled.
        """

        selected = tuple(dataset_ids) if dataset_ids is not None else ALERT_DATASET_IDS
        invalid = tuple(
            dataset_id for dataset_id in selected if dataset_id not in ALERT_DATASET_IDS
        )
        if invalid:
            raise ValueError(f"不支援的中央氣象署警報資料集：{invalid[0]}")

        results = await asyncio.gather(
            *(self.fetch_api_alert_dataset(dataset_id) for dataset_id in selected),
            return_exceptions=True,
        )
        events: list[AlertEvent] = []
        failures: list[tuple[str, BaseException]] = []
        for dataset_id, result in zip(selected, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, BaseException):
                if not isinstance(result, Exception):
                    raise result
                failures.append((dataset_id, result))
                continue
            events.extend(result)
        if failures:
            raise CWAAlertBatchError(
                events,
                failures,
                secrets=(self.api_key,),
            )
        return tuple(events)

    async def fetch_rss_items(self, url: str) -> tuple[RSSItem, ...]:
        """Fetch one allowlisted CWA RSS URL."""

        url = _validate_cwa_data_url(url, "rss")
        raw = await self._request_bytes(
            url,
            source_kind="rss",
            source_label="中央氣象署 RSS",
            limit=MAX_XML_BYTES,
            accepted_types=("text/xml", "application/xml", "application/rss+xml"),
        )
        return parse_rss_items(raw)

    async def fetch_rss_alerts(self) -> tuple[AlertEvent, ...]:
        """Fetch and normalize the official warning RSS feed."""

        return normalize_rss_alerts(await self.fetch_rss_items(RSS_WARNING_URL))

    async def fetch_rss_weather(self, city: str) -> WeatherForecast:
        """Fetch and normalize one county forecast RSS feed."""

        canonical = normalize_city(city)
        url = RSS_FORECAST_URL_TEMPLATE.format(code=CITY_RSS_CODES[canonical])
        return normalize_rss_weather(canonical, await self.fetch_rss_items(url))

    async def test_api(self) -> Mapping[str, Any]:
        """Test API connectivity without exposing the authorization code."""

        payload = await self.fetch_api_dataset(API_DATASETS["weather"])
        count = len(normalize_api_weather(payload))
        return {
            "ok": True,
            "source": "api",
            "message": f"開放資料 API 連線成功，共取得 {count} 個縣市預報。",
            "count": count,
        }

    async def test_rss(self) -> Mapping[str, Any]:
        """Test warning RSS connectivity; an empty channel is still healthy."""

        items = await self.fetch_rss_items(RSS_WARNING_URL)
        return {
            "ok": True,
            "source": "rss",
            "message": f"RSS 連線成功，目前頻道有 {len(items)} 筆項目。",
            "count": len(items),
        }

    async def download_typhoon_bulletin(
        self,
        destination: Path | str,
        *,
        cache_token: str | None = None,
    ) -> DownloadedImage:
        """Download, validate, and atomically store the current I10 bulletin.

        Redirects are followed manually so every destination is validated
        before the next request, preventing an allowlisted endpoint from being
        used as an SSRF redirector.
        """

        session = await self._ensure_session()
        target = Path(destination)
        part = target.with_name(f"{target.name}.part")
        query = dict(parse_qsl(urlsplit(TYPHOON_BULLETIN_URL).query))
        query["_"] = cache_token or str(int(datetime.now(timezone.utc).timestamp()))
        initial = urlsplit(TYPHOON_BULLETIN_URL)
        current_url = validate_typhoon_url(
            urlunsplit(
                (initial.scheme, initial.netloc, initial.path, urlencode(query), "")
            )
        )
        try:
            for redirect_count in range(4):
                async with session.get(
                    current_url,
                    allow_redirects=False,
                    ssl=self._ssl_context,
                    timeout=aiohttp.ClientTimeout(total=self.timeout_seconds),
                    headers={"Accept": "image/png"},
                ) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        if redirect_count >= 3:
                            raise CWASecurityError("颱風警報單重新導向次數過多。")
                        location = str(response.headers.get("Location", "")).strip()
                        if not location:
                            raise CWASecurityError("颱風警報單重新導向缺少目標網址。")
                        current_url = validate_typhoon_url(
                            urljoin(current_url, location)
                        )
                        continue
                    if response.status >= 400:
                        self._raise_status(response.status, "中央氣象署颱風警報單")
                    final_url = validate_typhoon_url(str(response.url or current_url))
                    content_type = self._content_type(response)
                    if content_type != "image/png":
                        raise CWASecurityError(
                            "颱風警報單回傳非預期的 Content-Type："
                            f"{content_type or '未提供'}。"
                        )
                    data = await self._read_limited(response, MAX_PNG_BYTES)
                    digest = validate_typhoon_png(data)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    part.write_bytes(data)
                    part.replace(target)
                    return DownloadedImage(
                        path=target,
                        sha256=digest,
                        size=len(data),
                        content_type=content_type,
                        final_url=final_url,
                    )
            raise CWASecurityError("颱風警報單重新導向流程異常。")
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = [
    "ALERT_DATASET_IDS",
    "API_BASE_URL",
    "API_DATASETS",
    "CATEGORY_COLD",
    "CATEGORY_FOG",
    "CATEGORY_HEAT",
    "CATEGORY_LABELS",
    "CATEGORY_LOCAL_EARTHQUAKE",
    "CATEGORY_OTHER",
    "CATEGORY_RAIN",
    "CATEGORY_SIGNIFICANT_EARTHQUAKE",
    "CATEGORY_TSUNAMI",
    "CATEGORY_TYPHOON",
    "CATEGORY_WIND",
    "CITY_RSS_CODES",
    "CWAAuthenticationError",
    "CWAAlertBatchError",
    "CWAClient",
    "CWAClientError",
    "CWAHTTPError",
    "CWAParseError",
    "CWARateLimitError",
    "CWAResponseTooLargeError",
    "CWASecurityError",
    "DownloadedImage",
    "ForecastPeriod",
    "MAX_JSON_BYTES",
    "MAX_PNG_BYTES",
    "MAX_XML_BYTES",
    "RSS_FORECAST_URL_TEMPLATE",
    "RSS_WARNING_URL",
    "RSSItem",
    "TYPHOON_BULLETIN_URL",
    "WeatherForecast",
    "AlertEvent",
    "classify_alert",
    "build_cwa_ssl_context",
    "extract_cities",
    "html_to_text",
    "normalize_api_alerts",
    "normalize_api_weather",
    "normalize_city",
    "normalize_rss_alerts",
    "normalize_rss_weather",
    "parse_json_document",
    "parse_rss_items",
    "validate_typhoon_png",
    "validate_typhoon_url",
]
