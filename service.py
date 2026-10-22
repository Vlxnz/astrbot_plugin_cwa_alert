from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit

TAIPEI_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Taipei")
DEFAULT_POLL_INTERVAL_SECONDS = 120
DEFAULT_FORECAST_TIMES = ("07:30", "18:30")
MAX_FORECAST_TIMES = 4
MAX_MESSAGE_LENGTH = 1800
STATE_RETENTION_DAYS = 30
STATE_MAX_ENTRIES = 2000

OFFICIAL_CITIES = (
    "臺北市",
    "新北市",
    "桃園市",
    "臺中市",
    "臺南市",
    "高雄市",
    "基隆市",
    "新竹縣",
    "新竹市",
    "苗栗縣",
    "彰化縣",
    "南投縣",
    "雲林縣",
    "嘉義縣",
    "嘉義市",
    "屏東縣",
    "宜蘭縣",
    "花蓮縣",
    "臺東縣",
    "澎湖縣",
    "金門縣",
    "連江縣",
)

ALERT_TYPE_LABELS = {
    "significant_earthquake": "顯著有感地震",
    "local_earthquake": "小區域有感地震",
    "tsunami": "海嘯",
    "typhoon": "颱風",
    "rain": "豪雨／大雨",
    "wind": "陸上強風",
    "fog": "濃霧",
    "heat": "高溫",
    "cold": "低溫",
    "other": "其他天氣警報",
}
ALERT_TYPES = tuple(ALERT_TYPE_LABELS)
NATIONAL_ALERT_TYPES = frozenset(
    {"significant_earthquake", "local_earthquake", "tsunami", "typhoon"}
)
SOURCE_MODES = frozenset({"auto", "api", "rss"})
CITIES = OFFICIAL_CITIES
DEFAULT_SETTINGS = {
    "revision": 0,
    "enabled": False,
    "source_mode": "auto",
    "api_key": "",
    "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
    "subscriptions": [],
}

_CITY_ALIASES = {
    "台北市": "臺北市",
    "台中市": "臺中市",
    "台南市": "臺南市",
    "台東縣": "臺東縣",
}
_CATEGORY_ALIASES = {
    **{key: key for key in ALERT_TYPES},
    "earthquake": "significant_earthquake",
    "significant-earthquake": "significant_earthquake",
    "local-earthquake": "local_earthquake",
    "heavy_rain": "rain",
    "heavy-rain": "rain",
    "strong_wind": "wind",
    "strong-wind": "wind",
    "high_temperature": "heat",
    "high-temperature": "heat",
    "low_temperature": "cold",
    "low-temperature": "cold",
    "顯著有感地震": "significant_earthquake",
    "小區域有感地震": "local_earthquake",
    "地震": "significant_earthquake",
    "海嘯": "tsunami",
    "颱風": "typhoon",
    "台風": "typhoon",
    "豪雨": "rain",
    "大雨": "rain",
    "豪雨／大雨": "rain",
    "陸上強風": "wind",
    "強風": "wind",
    "濃霧": "fog",
    "高溫": "heat",
    "低溫": "cold",
    "其他天氣警報": "other",
}
_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_RSS_TITLE_PREFIX_PATTERN = re.compile(
    r"^\s*\d{1,2}\s*/\s*\d{1,2}\s+"
    r"\d{1,2}\s*:\s*\d{2}(?:\s*:\s*\d{2})?\s*"
    r"(?:發布|發佈)\s*[:：\-－—|｜]?\s*",
)


class ValidationError(ValueError):
    """Represent invalid persisted or dashboard configuration."""


@dataclass(frozen=True, slots=True)
class SourceDecision:
    """Describe the data source selected for the next polling cycle."""

    requested_mode: str
    active_source: str
    is_fallback: bool
    reason: str
    api_key_configured: bool

    @property
    def status_label(self) -> str:
        """Return a Traditional Chinese label for the dashboard.

        Returns:
            Human-readable source status label.
        """
        if self.active_source == "api":
            return "開放資料 API"
        if self.active_source == "rss" and self.is_fallback:
            return "RSS 降級中"
        if self.active_source == "rss":
            return "RSS 模式"
        return "資料來源不可用"


@dataclass(frozen=True, slots=True)
class ForecastSlot:
    """Represent one due daily forecast delivery slot."""

    date: str
    time: str
    key: str


def default_settings() -> dict[str, Any]:
    """Return a new default settings dictionary.

    Returns:
        Independent settings dictionary suitable for plugin KV storage.
    """
    return {
        "revision": 0,
        "enabled": False,
        "source_mode": "auto",
        "api_key": "",
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
        "subscriptions": [],
    }


def default_source_state() -> dict[str, Any]:
    """Return a new source-health state dictionary.

    Returns:
        Serializable source state with neutral health values.
    """
    return {
        "active_source": "rss",
        "requested_mode": "auto",
        "is_fallback": False,
        "api_available": None,
        "rss_available": None,
        "api_consecutive_failures": 0,
        "last_api_success_at": None,
        "last_api_error": None,
        "last_rss_success_at": None,
        "last_rss_error": None,
        "last_switch_at": None,
    }


def normalize_city(value: Any) -> str:
    """Normalize one city name to the CWA official spelling.

    Args:
        value: Candidate city name.

    Returns:
        Official city or county name.

    Raises:
        ValidationError: The value is not one of Taiwan's 22 cities/counties.
    """
    city = str(value or "").strip()
    city = _CITY_ALIASES.get(city, city)
    if city not in OFFICIAL_CITIES:
        raise ValidationError(f"不支援的縣市：{city or '空白'}")
    return city


def normalize_alert_type(value: Any) -> str:
    """Normalize one alert category to its internal identifier.

    Args:
        value: Internal or Traditional Chinese category value.

    Returns:
        Canonical alert category identifier.

    Raises:
        ValidationError: The category is not supported.
    """
    candidate = str(value or "").strip()
    normalized = _CATEGORY_ALIASES.get(candidate)
    if normalized is None:
        normalized = _CATEGORY_ALIASES.get(candidate.lower())
    if normalized is None:
        raise ValidationError(f"不支援的災害類型：{candidate or '空白'}")
    return normalized


def normalize_subscription(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one subscription rule.

    Args:
        raw: Subscription mapping received from KV or the dashboard.

    Returns:
        Serializable normalized subscription.

    Raises:
        ValidationError: A required field or selection is invalid.
    """
    if not isinstance(raw, Mapping):
        raise ValidationError("訂閱規則必須是物件。")

    target_umo = str(raw.get("target_umo") or "").strip()
    if not target_umo:
        raise ValidationError("訂閱規則缺少群組目標。")
    if len(target_umo) > 1024:
        raise ValidationError("群組目標格式過長。")

    subscription_id = str(raw.get("id") or "").strip()
    if not subscription_id:
        digest = hashlib.sha256(target_umo.encode("utf-8")).hexdigest()[:16]
        subscription_id = f"subscription-{digest}"
    if len(subscription_id) > 128:
        raise ValidationError("訂閱識別碼格式過長。")

    cities_value = raw.get("cities", [])
    if isinstance(cities_value, (str, bytes)) or not isinstance(cities_value, Sequence):
        raise ValidationError("訂閱縣市必須是陣列。")
    cities: list[str] = []
    for value in cities_value:
        city = normalize_city(value)
        if city not in cities:
            cities.append(city)

    enabled = _normalize_bool(raw.get("enabled", True), "訂閱啟用狀態")
    forecast_enabled = _normalize_bool(
        raw.get("forecast_enabled", True), "天氣播報啟用狀態"
    )
    if forecast_enabled and not cities:
        raise ValidationError("啟用天氣播報時至少需要選擇一個縣市。")

    times_value = raw.get("forecast_times", list(DEFAULT_FORECAST_TIMES))
    if isinstance(times_value, (str, bytes)) or not isinstance(times_value, Sequence):
        raise ValidationError("天氣播報時間必須是陣列。")
    forecast_times: list[str] = []
    for value in times_value:
        candidate = str(value or "").strip()
        if not _TIME_PATTERN.fullmatch(candidate):
            raise ValidationError(f"無效的天氣播報時間：{candidate or '空白'}")
        if candidate not in forecast_times:
            forecast_times.append(candidate)
    forecast_times.sort()
    if forecast_enabled and not forecast_times:
        raise ValidationError("啟用天氣播報時至少需要一個播報時間。")
    if len(forecast_times) > MAX_FORECAST_TIMES:
        raise ValidationError(f"每個群組最多設定 {MAX_FORECAST_TIMES} 個播報時間。")

    alerts_value = raw.get("alert_types", list(ALERT_TYPES))
    if isinstance(alerts_value, (str, bytes)) or not isinstance(alerts_value, Sequence):
        raise ValidationError("災害類型必須是陣列。")
    alert_types: list[str] = []
    for value in alerts_value:
        alert_type = normalize_alert_type(value)
        if alert_type not in alert_types:
            alert_types.append(alert_type)

    return {
        "id": subscription_id,
        "target_umo": target_umo,
        "enabled": enabled,
        "cities": cities,
        "forecast_enabled": forecast_enabled,
        "forecast_times": forecast_times,
        "alert_types": alert_types,
    }


def normalize_settings(
    raw: Mapping[str, Any] | None,
    known_targets: Sequence[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    previous: Mapping[str, Any] | None = None,
    *,
    existing_api_key: str | None = None,
) -> dict[str, Any]:
    """Validate and normalize all plugin settings.

    Args:
        raw: Settings mapping. Missing fields use documented defaults.
        known_targets: Optional registered targets used to reject enabled orphan rules.
        previous: Optional previous settings used to preserve an omitted secret.
        existing_api_key: Explicit secret to preserve when ``api_key`` is omitted.

    Returns:
        Serializable normalized settings.

    Raises:
        ValidationError: A field is malformed or subscriptions conflict.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValidationError("外掛設定必須是物件。")

    try:
        revision = int(raw.get("revision", 0))
    except (TypeError, ValueError) as exc:
        raise ValidationError("設定版本必須是非負整數。") from exc
    if revision < 0:
        raise ValidationError("設定版本必須是非負整數。")

    source_mode = str(raw.get("source_mode", "auto") or "").strip().lower()
    if source_mode not in SOURCE_MODES:
        raise ValidationError("資料來源模式必須是 auto、api 或 rss。")

    try:
        poll_interval = int(
            raw.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS)
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("輪詢間隔必須是 60 至 600 秒的整數。") from exc
    if not 60 <= poll_interval <= 600:
        raise ValidationError("輪詢間隔必須介於 60 至 600 秒。")

    preserved_api_key = existing_api_key
    if preserved_api_key is None and isinstance(previous, Mapping):
        preserved_api_key = str(previous.get("api_key") or "")
    if "api_key" in raw:
        api_key = str(raw.get("api_key") or "").strip()
    else:
        api_key = str(preserved_api_key or "").strip()
    if len(api_key) > 512:
        raise ValidationError("中央氣象署授權碼格式過長。")

    subscriptions_value = raw.get("subscriptions", [])
    if isinstance(subscriptions_value, (str, bytes)) or not isinstance(
        subscriptions_value, Sequence
    ):
        raise ValidationError("訂閱規則必須是陣列。")
    subscriptions: list[dict[str, Any]] = []
    subscription_ids: set[str] = set()
    target_umos: set[str] = set()
    for item in subscriptions_value:
        subscription = normalize_subscription(item)
        if subscription["id"] in subscription_ids:
            raise ValidationError("訂閱識別碼不可重複。")
        if subscription["target_umo"] in target_umos:
            raise ValidationError("每個群組最多只能建立一條訂閱規則。")
        subscription_ids.add(subscription["id"])
        target_umos.add(subscription["target_umo"])
        subscriptions.append(subscription)

    if known_targets is not None:
        target_values: Any = (
            list(known_targets.values())
            if isinstance(known_targets, Mapping)
            else known_targets
        )
        registered_umos = {
            str(target.get("target_umo") or "").strip()
            for target in target_values
            if isinstance(target, Mapping)
        }
        orphaned = [
            subscription["target_umo"]
            for subscription in subscriptions
            if subscription["enabled"]
            and subscription["target_umo"] not in registered_umos
        ]
        if orphaned:
            raise ValidationError("訂閱規則包含尚未登記的群組目標。")

    return {
        "revision": revision,
        "enabled": _normalize_bool(raw.get("enabled", False), "外掛啟用狀態"),
        "source_mode": source_mode,
        "api_key": api_key,
        "poll_interval_seconds": poll_interval,
        "subscriptions": subscriptions,
    }


def normalize_group_target(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize one explicitly registered group target.

    Args:
        raw: Group registry mapping assembled by the AstrBot integration.

    Returns:
        Serializable normalized group target.

    Raises:
        ValidationError: Required target metadata is missing or too long.
    """
    if not isinstance(raw, Mapping):
        raise ValidationError("群組目標必須是物件。")
    target_umo = str(raw.get("target_umo") or "").strip()
    if not target_umo:
        raise ValidationError("群組目標缺少完整 UMO。")
    if len(target_umo) > 1024:
        raise ValidationError("群組目標 UMO 格式過長。")

    target_id = str(raw.get("id") or "").strip()
    if not target_id:
        digest = hashlib.sha256(target_umo.encode("utf-8")).hexdigest()[:16]
        target_id = f"target-{digest}"

    normalized: dict[str, Any] = {
        "id": _bounded_text(target_id, "群組目標識別碼", 128, required=True),
        "target_umo": target_umo,
        "platform_id": _bounded_text(
            raw.get("platform_id"), "平台實例識別碼", 256, required=True
        ),
        "platform_type": _bounded_text(
            raw.get("platform_type"), "平台類型", 128, required=True
        ),
        "group_id": _bounded_text(
            raw.get("group_id"), "群組識別碼", 256, required=True
        ),
        "display_name": _bounded_text(
            raw.get("display_name") or raw.get("group_id"),
            "群組顯示名稱",
            80,
            required=True,
        ),
        "registered_at": _normalize_timestamp(
            raw.get("registered_at") or datetime.now(TAIPEI_TIMEZONE)
        ),
        "enabled": _normalize_bool(raw.get("enabled", True), "群組目標啟用狀態"),
    }
    tested_at = raw.get("tested_at")
    normalized["tested_at"] = (
        _normalize_timestamp(tested_at) if tested_at not in (None, "") else None
    )
    return normalized


def normalize_group_registry(raw: Any) -> list[dict[str, Any]]:
    """Validate and normalize the explicitly registered group collection.

    Args:
        raw: Sequence of targets, or a mapping whose values are targets.

    Returns:
        Deduplicated normalized group target list.

    Raises:
        ValidationError: Registry structure or a target is invalid.
    """
    if raw is None:
        return []
    values: Any = list(raw.values()) if isinstance(raw, Mapping) else raw
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise ValidationError("群組登記資料必須是陣列或物件。")

    targets: list[dict[str, Any]] = []
    ids: set[str] = set()
    umos: set[str] = set()
    for item in values:
        target = normalize_group_target(item)
        if target["id"] in ids or target["target_umo"] in umos:
            raise ValidationError("群組登記資料含有重複目標。")
        ids.add(target["id"])
        umos.add(target["target_umo"])
        targets.append(target)
    return targets


def effective_api_key(
    settings: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> str:
    """Resolve the API key with the environment variable taking precedence.

    Args:
        settings: Normalized or partially normalized plugin settings.
        environ: Optional environment mapping, primarily for deterministic tests.

    Returns:
        Effective API key, or an empty string when none is configured.
    """
    environment = os.environ if environ is None else environ
    return str(environment.get("CWA_API_KEY") or settings.get("api_key") or "").strip()


def public_settings(
    settings: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Create a dashboard-safe settings view without the API secret.

    Args:
        settings: Plugin settings containing an optional API key.
        environ: Optional environment mapping used to resolve key precedence.

    Returns:
        Settings copy with key presence and suffix metadata.
    """
    result = dict(settings)
    result.pop("api_key", None)
    key = effective_api_key(settings, environ)
    result["api_key_configured"] = bool(key)
    result["api_key_suffix"] = key[-4:] if key else ""
    result["api_key_from_environment"] = bool(
        (os.environ if environ is None else environ).get("CWA_API_KEY")
    )
    return result


def select_data_source(
    settings: Mapping[str, Any],
    source_state: Mapping[str, Any] | None = None,
    environ: Mapping[str, str] | None = None,
) -> SourceDecision:
    """Select API, RSS, or unavailable according to mode and health state.

    Args:
        settings: Plugin settings containing ``source_mode`` and optional key.
        source_state: Persisted source health information.
        environ: Optional environment mapping for API key resolution.

    Returns:
        Immutable source selection decision.
    """
    mode = str(settings.get("source_mode") or "auto").strip().lower()
    if mode not in SOURCE_MODES:
        mode = "auto"
    has_key = bool(effective_api_key(settings, environ))
    state = source_state or {}
    try:
        failures = max(0, int(state.get("api_consecutive_failures", 0)))
    except (TypeError, ValueError):
        failures = 0

    if mode == "rss":
        return SourceDecision(mode, "rss", False, "已設定為僅使用 RSS。", has_key)
    if mode == "api":
        if has_key:
            return SourceDecision(
                mode, "api", False, "已設定為僅使用開放資料 API。", True
            )
        return SourceDecision(
            mode,
            "unavailable",
            False,
            "僅 API 模式需要中央氣象署授權碼。",
            False,
        )
    if not has_key:
        return SourceDecision(mode, "rss", False, "未設定授權碼，使用官方 RSS。", False)
    if failures >= 3:
        return SourceDecision(
            mode,
            "rss",
            True,
            "開放資料 API 連續失敗三次，暫時改用官方 RSS。",
            True,
        )
    return SourceDecision(mode, "api", False, "授權碼可用，優先使用 API。", True)


def update_source_state(
    settings: Mapping[str, Any],
    source_state: Mapping[str, Any] | None,
    *,
    source: str,
    succeeded: bool,
    error: str | None = None,
    now: datetime | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Record one source attempt and recompute the active source.

    Args:
        settings: Current plugin settings.
        source_state: Existing serializable health state.
        source: Attempted source, either ``api`` or ``rss``.
        succeeded: Whether the attempt completed successfully.
        error: Optional sanitized error summary.
        now: Attempt time. Defaults to current Taipei time.
        environ: Optional environment mapping for source selection.

    Returns:
        Updated serializable health state.

    Raises:
        ValidationError: The source identifier is unsupported.
    """
    if source not in {"api", "rss"}:
        raise ValidationError("來源健康狀態只支援 api 或 rss。")
    state = default_source_state()
    if source_state:
        state.update(dict(source_state))
    timestamp = _normalize_timestamp(now or datetime.now(TAIPEI_TIMEZONE))

    if source == "api":
        state["api_available"] = bool(succeeded)
        if succeeded:
            state["api_consecutive_failures"] = 0
            state["last_api_success_at"] = timestamp
            state["last_api_error"] = None
        else:
            try:
                failures = int(state.get("api_consecutive_failures", 0))
            except (TypeError, ValueError):
                failures = 0
            state["api_consecutive_failures"] = max(0, failures) + 1
            state["last_api_error"] = str(error or "未知錯誤")[:500]
    else:
        state["rss_available"] = bool(succeeded)
        if succeeded:
            state["last_rss_success_at"] = timestamp
            state["last_rss_error"] = None
        else:
            state["last_rss_error"] = str(error or "未知錯誤")[:500]

    previous_source = str(state.get("active_source") or "")
    decision = select_data_source(settings, state, environ)
    state["active_source"] = decision.active_source
    state["requested_mode"] = decision.requested_mode
    state["is_fallback"] = decision.is_fallback
    if previous_source != decision.active_source:
        state["last_switch_at"] = timestamp
    return state


def event_fingerprint(event: Any) -> str:
    """Build a source-independent SHA-256 identity for one alert event.

    API-only identifiers, RSS GUIDs, source names, and tracking query strings are
    deliberately excluded so a source switch does not replay the same warning.

    Args:
        event: Mapping, dataclass, or object representing an alert event.

    Returns:
        Lowercase hexadecimal SHA-256 digest.
    """
    category = _event_category(event)
    raw_title = unicodedata.normalize(
        "NFKC", str(_value(event, "title", "headline", default="") or "")
    )
    title = _canonical_fingerprint_text(
        _RSS_TITLE_PREFIX_PATTERN.sub("", raw_title, count=1)
    )
    bulletin = _normalize_bulletin(
        _value(event, "bulletin_no", "report_no", "serial_no", default="")
    )
    published = _canonical_event_time(
        _value(
            event,
            "published_at",
            "published_time",
            "sent_at",
            "update_time",
            default=None,
        )
    )
    areas = sorted(
        {
            canonical
            for area in _event_areas(event)
            if (canonical := _canonical_fingerprint_text(area))
        }
    )
    description = _canonical_fingerprint_text(
        _value(event, "description", "summary", "content", default="")
    )
    parts = [category, title, bulletin, published, "|".join(areas), description]
    canonical = "\x1f".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def subscription_accepts_event(subscription: Mapping[str, Any], event: Any) -> bool:
    """Check category and city filters for one subscription and event.

    Args:
        subscription: Normalized subscription rule.
        event: Mapping, dataclass, or alert object.

    Returns:
        True when the rule should receive the event.
    """
    if not bool(subscription.get("enabled", False)):
        return False
    category = _event_category(event)
    selected_types = {
        _CATEGORY_ALIASES.get(str(value), str(value))
        for value in subscription.get("alert_types", [])
    }
    if category not in selected_types:
        return False
    if category in NATIONAL_ALERT_TYPES:
        return True

    selected_cities: set[str] = set()
    for value in subscription.get("cities", []):
        try:
            selected_cities.add(normalize_city(value))
        except ValidationError:
            continue
    if not selected_cities:
        return False

    affected_cities = _cities_mentioned(_event_areas(event))
    if not affected_cities:
        return True
    return bool(selected_cities.intersection(affected_cities))


def filter_subscriptions(
    subscriptions: Sequence[Mapping[str, Any]], event: Any
) -> list[Mapping[str, Any]]:
    """Return enabled subscriptions matching one alert event.

    Args:
        subscriptions: Subscription rules to evaluate.
        event: Alert event to filter.

    Returns:
        Matching rules in input order.
    """
    return [
        subscription
        for subscription in subscriptions
        if subscription_accepts_event(subscription, event)
    ]


def format_weather_forecast(forecast: Any, *, max_periods: int = 3) -> str:
    """Format a CWA forecast as Traditional Chinese plain text.

    Args:
        forecast: Mapping, dataclass, or object with city and period data.
        max_periods: Maximum number of forecast periods to display.

    Returns:
        Plain-text forecast preserving the provider's original wording.

    Raises:
        ValueError: ``max_periods`` is not positive.
    """
    if max_periods <= 0:
        raise ValueError("max_periods 必須大於零。")
    city = str(
        _value(forecast, "city", "location_name", "location", default="未指定縣市")
        or "未指定縣市"
    ).strip()
    try:
        city = normalize_city(city)
    except ValidationError:
        pass

    lines = [f"【{city} 36 小時天氣預報】"]
    issued_at = _value(
        forecast,
        "issued_at",
        "published_at",
        "update_time",
        default=None,
    )
    if issued_at:
        lines.append(f"更新時間：{_display_time(issued_at)}")

    periods = _value(forecast, "periods", "time_periods", default=[])
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        periods = []
    for index, period in enumerate(periods[:max_periods], start=1):
        start = _value(period, "start_time", "start", default=None)
        end = _value(period, "end_time", "end", default=None)
        if start and end:
            time_range = f"{_display_time(start)}－{_display_time(end)}"
        elif start:
            time_range = _display_time(start)
        elif end:
            time_range = _display_time(end)
        else:
            time_range = "預報時段未提供"
        period_parts = [f"{index}. {time_range}"]

        weather = _value(period, "weather", "description", "wx", default=None)
        if weather not in (None, ""):
            period_parts.append(str(weather).strip())
        probability = _value(
            period,
            "rain_probability",
            "precipitation_probability",
            "pop",
            default=None,
        )
        if probability not in (None, ""):
            probability_text = str(probability).strip()
            if not probability_text.endswith("%"):
                probability_text += "%"
            period_parts.append(f"降雨 {probability_text}")
        minimum = _value(
            period, "min_temperature", "min_temp", "minimum_temperature", default=None
        )
        maximum = _value(
            period, "max_temperature", "max_temp", "maximum_temperature", default=None
        )
        if minimum not in (None, "") or maximum not in (None, ""):
            minimum_text = str(minimum).strip() if minimum not in (None, "") else "—"
            maximum_text = str(maximum).strip() if maximum not in (None, "") else "—"
            period_parts.append(f"{minimum_text}～{maximum_text} °C")
        comfort = _value(period, "comfort", "comfort_index", "ci", default=None)
        if comfort not in (None, ""):
            period_parts.append(f"舒適度 {str(comfort).strip()}")
        lines.append("｜".join(period_parts))

    if not periods:
        lines.append("目前沒有可顯示的預報時段。")
    link = _value(forecast, "link", "url", default=None)
    if link:
        lines.append(f"\n官方連結：{_clean_url(str(link))}")
    return "\n".join(lines)


def format_alert_event(event: Any) -> str:
    """Format one CWA alert as Traditional Chinese plain text.

    Args:
        event: Mapping, dataclass, or object representing an alert.

    Returns:
        Plain-text alert with official wording and source attribution.
    """
    category = _event_category(event)
    label = ALERT_TYPE_LABELS[category]
    cancelled = bool(
        _value(event, "is_cancellation", "is_cancelled", "cancelled", default=False)
    )
    heading = f"【中央氣象署｜{label}{'解除／取消資訊' if cancelled else ''}】"
    lines = [heading]
    title = _value(event, "title", "headline", default=None)
    if title:
        title_text = str(title).strip()
        if title_text != label:
            lines.append(title_text)

    bulletin = _value(event, "bulletin_no", "report_no", "serial_no", default=None)
    if bulletin:
        lines.append(f"報次：{str(bulletin).strip()}")
    published = _value(
        event,
        "published_at",
        "published_time",
        "sent_at",
        default=None,
    )
    if published:
        lines.append(f"發布時間：{_display_time(published)}")
    areas = _event_areas(event)
    if areas:
        lines.append(f"影響區域：{'、'.join(areas)}")
    magnitude = _value(event, "magnitude", default=None)
    if magnitude not in (None, ""):
        lines.append(f"地震規模：{str(magnitude).strip()}")
    location = _value(event, "location", "epicenter", default=None)
    if location:
        lines.append(f"位置：{str(location).strip()}")

    description = _value(event, "description", "summary", "content", default=None)
    if description:
        lines.append(f"\n{str(description).strip()}")
    link = _value(event, "link", "url", default=None)
    if link:
        lines.append(f"\n官方連結：{_clean_url(str(link))}")
    return "\n".join(lines)


def split_message(
    text: str,
    max_chars: int = MAX_MESSAGE_LENGTH,
    *,
    limit: int | None = None,
) -> list[str]:
    """Split plain text into platform-safe chunks without losing content.

    Paragraph and line boundaries are preferred. Hard splitting is only used
    when an individual unbroken segment itself exceeds the configured limit.

    Args:
        text: Plain text to split.
        max_chars: Maximum number of Python characters in each chunk.
        limit: Backward-compatible keyword override for ``max_chars``.

    Returns:
        Non-empty chunks, each no longer than ``limit``.

    Raises:
        ValueError: The selected character limit is not positive.
    """
    selected_limit = max_chars if limit is None else limit
    if selected_limit <= 0:
        raise ValueError("訊息長度上限必須大於零。")
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    pieces: list[str] = []
    for paragraph in normalized.split("\n\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        pieces.extend(_split_long_segment(paragraph, selected_limit))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        separator = "\n\n" if current else ""
        if len(current) + len(separator) + len(piece) <= selected_limit:
            current += separator + piece
            continue
        if current:
            chunks.append(current)
        current = piece
    if current:
        chunks.append(current)
    return chunks


def event_is_recent(
    event: Any,
    *,
    now: datetime | None = None,
    window_minutes: int = 5,
) -> bool:
    """Check whether an event falls inside a symmetric freshness window.

    The small future allowance handles clock skew between the bot host and the
    official feed. This is intended for first-start earthquake baselining.

    Args:
        event: Alert event mapping, dataclass, or object.
        now: Comparison time. Defaults to current Taipei time.
        window_minutes: Maximum absolute age in minutes.

    Returns:
        True when the published time is parseable and inside the window.

    Raises:
        ValueError: ``window_minutes`` is negative.
    """
    if window_minutes < 0:
        raise ValueError("事件新鮮度時間窗不可為負數。")
    published = _parse_datetime(
        _value(
            event,
            "published_at",
            "published_time",
            "sent_at",
            default=None,
        )
    )
    if published is None:
        return False
    current = now or datetime.now(TAIPEI_TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TAIPEI_TIMEZONE)
    age = abs(current.astimezone(timezone.utc) - published.astimezone(timezone.utc))
    return age <= timedelta(minutes=window_minutes)


def should_download_typhoon_bulletin(event: Any) -> bool:
    """Check whether an alert requires the official typhoon bulletin image.

    Args:
        event: Alert event mapping, dataclass, or object.

    Returns:
        True only for an active sea or sea-and-land typhoon warning.
    """
    if _event_category(event) != "typhoon":
        return False
    if bool(
        _value(event, "is_cancellation", "is_cancelled", "cancelled", default=False)
    ):
        return False
    text = "\n".join(
        str(_value(event, name, default="") or "") for name in ("title", "description")
    )
    return "海上" in text and "颱風警報" in text


def forecast_slot_key(target_umo: str, date: str, send_time: str) -> str:
    """Build a compact stable key for one target's daily forecast slot.

    Args:
        target_umo: Full unified message origin.
        date: Local calendar date in ISO format.
        send_time: Scheduled local time in HH:MM format.

    Returns:
        Stable non-secret slot key.

    Raises:
        ValidationError: Date or time format is invalid.
    """
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValidationError("天氣播報日期格式必須是 YYYY-MM-DD。") from exc
    if not _TIME_PATTERN.fullmatch(str(send_time or "")):
        raise ValidationError("天氣播報時間格式必須是 HH:MM。")
    digest = hashlib.sha256(str(target_umo).encode("utf-8")).hexdigest()[:16]
    return f"forecast:{digest}:{date}:{send_time}"


def due_forecast_slots(
    subscription: Mapping[str, Any],
    sent_slot_keys: Collection[str] | Mapping[str, Any],
    *,
    now: datetime | None = None,
    grace_minutes: int = 30,
) -> list[ForecastSlot]:
    """Return unsent forecast slots inside the allowed late-send window.

    Yesterday is also checked so a 23:xx schedule can be delivered shortly
    after midnight without widening the normal 30-minute recovery window.

    Args:
        subscription: Normalized subscription rule.
        sent_slot_keys: Collection or mapping of already completed slot keys.
        now: Current time. Naive values are interpreted as Taipei local time.
        grace_minutes: Inclusive late-send allowance in minutes.

    Returns:
        Due slots ordered by their scheduled instant.

    Raises:
        ValueError: ``grace_minutes`` is negative.
    """
    if grace_minutes < 0:
        raise ValueError("補送時間窗不可為負數。")
    if not bool(subscription.get("enabled", False)) or not bool(
        subscription.get("forecast_enabled", False)
    ):
        return []
    current = _as_taipei(now or datetime.now(TAIPEI_TIMEZONE))
    sent = (
        set(sent_slot_keys.keys())
        if isinstance(sent_slot_keys, Mapping)
        else set(sent_slot_keys)
    )
    candidates: list[tuple[datetime, ForecastSlot]] = []
    for day_offset in (1, 0):
        local_date = (current - timedelta(days=day_offset)).date()
        for send_time in subscription.get("forecast_times", []):
            send_time = str(send_time)
            if not _TIME_PATTERN.fullmatch(send_time):
                continue
            hour, minute = (int(part) for part in send_time.split(":"))
            scheduled = datetime(
                local_date.year,
                local_date.month,
                local_date.day,
                hour,
                minute,
                tzinfo=TAIPEI_TIMEZONE,
            )
            if not scheduled <= current <= scheduled + timedelta(minutes=grace_minutes):
                continue
            date_text = local_date.isoformat()
            key = forecast_slot_key(
                str(subscription.get("target_umo") or ""), date_text, send_time
            )
            if key not in sent:
                candidates.append((scheduled, ForecastSlot(date_text, send_time, key)))
    candidates.sort(key=lambda item: item[0])
    return [slot for _, slot in candidates]


def mark_event_seen(
    seen_events: Mapping[str, Any] | None,
    event: Any,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record an event fingerprint and prune the resulting state.

    Args:
        seen_events: Existing fingerprint-to-record mapping.
        event: Alert event to mark.
        now: Observation time.

    Returns:
        New pruned seen-event mapping.
    """
    result = dict(seen_events or {})
    fingerprint = event_fingerprint(event)
    timestamp = _normalize_timestamp(now or datetime.now(TAIPEI_TIMEZONE))
    existing = result.get(fingerprint)
    record = dict(existing) if isinstance(existing, Mapping) else {}
    first_seen = (
        existing.get("first_seen_at")
        if isinstance(existing, Mapping) and existing.get("first_seen_at")
        else timestamp
    )
    record.update(
        {
            "fingerprint": fingerprint,
            "category": _event_category(event),
            "first_seen_at": first_seen,
            "last_seen_at": timestamp,
        }
    )
    result[fingerprint] = record
    return prune_seen_events(result, now=now)


def event_was_seen(seen_events: Mapping[str, Any] | None, event: Any) -> bool:
    """Check whether an event fingerprint is already stored.

    Args:
        seen_events: Existing fingerprint-to-record mapping.
        event: Alert event to check.

    Returns:
        True when the event is present in state.
    """
    return event_fingerprint(event) in (seen_events or {})


def prune_seen_events(
    seen_events: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    retention_days: int = STATE_RETENTION_DAYS,
    max_entries: int = STATE_MAX_ENTRIES,
) -> dict[str, Any]:
    """Remove expired, malformed, and excess seen-event records.

    Args:
        seen_events: Fingerprint-to-record mapping.
        now: Current time used for retention comparison.
        retention_days: Maximum record age in days.
        max_entries: Maximum retained records after age pruning.

    Returns:
        New mapping ordered from newest to oldest.

    Raises:
        ValueError: Retention or entry limits are invalid.
    """
    return _prune_records(
        seen_events,
        timestamp_fields=("last_seen_at", "first_seen_at"),
        now=now,
        retention_days=retention_days,
        max_entries=max_entries,
    )


def delivery_key(event_fingerprint_value: str, target_umo: str, part: str) -> str:
    """Build a stable key for one event/target/message-part delivery.

    Args:
        event_fingerprint_value: Event SHA-256 identity.
        target_umo: Full unified message origin.
        part: ``text`` or ``image``.

    Returns:
        Stable delivery-state key.

    Raises:
        ValidationError: The part is unsupported.
    """
    if part not in {"text", "image"}:
        raise ValidationError("傳送部分只支援 text 或 image。")
    target_digest = hashlib.sha256(str(target_umo).encode("utf-8")).hexdigest()[:16]
    return f"{event_fingerprint_value}:{target_digest}:{part}"


def mark_delivery_status(
    delivery_status: Mapping[str, Any] | None,
    *,
    event_fingerprint_value: str,
    target_umo: str,
    part: str,
    succeeded: bool,
    error: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record one text or image delivery attempt and prune state.

    Args:
        delivery_status: Existing delivery state mapping.
        event_fingerprint_value: Event SHA-256 identity.
        target_umo: Full unified message origin.
        part: ``text`` or ``image``.
        succeeded: Whether AstrBot accepted the delivery.
        error: Optional sanitized failure summary.
        now: Attempt time.

    Returns:
        New pruned delivery-state mapping.
    """
    result = dict(delivery_status or {})
    key = delivery_key(event_fingerprint_value, target_umo, part)
    previous = result.get(key)
    try:
        attempts = (
            int(previous.get("attempts", 0)) if isinstance(previous, Mapping) else 0
        )
    except (TypeError, ValueError):
        attempts = 0
    timestamp = _normalize_timestamp(now or datetime.now(TAIPEI_TIMEZONE))
    delivered_at = (
        timestamp
        if succeeded
        else previous.get("delivered_at")
        if isinstance(previous, Mapping)
        else None
    )
    result[key] = {
        "event_fingerprint": str(event_fingerprint_value),
        "target_umo": str(target_umo),
        "part": part,
        "status": "success" if succeeded else "failed",
        "attempts": max(0, attempts) + 1,
        "last_attempt_at": timestamp,
        "delivered_at": delivered_at,
        "error": None if succeeded else str(error or "未知錯誤")[:500],
    }
    return prune_delivery_status(result, now=now)


def delivery_succeeded(
    delivery_status: Mapping[str, Any] | None,
    event_fingerprint_value: str,
    target_umo: str,
    part: str,
) -> bool:
    """Check whether one event part was delivered successfully.

    Args:
        delivery_status: Existing delivery state mapping.
        event_fingerprint_value: Event SHA-256 identity.
        target_umo: Full unified message origin.
        part: ``text`` or ``image``.

    Returns:
        True only for a stored successful delivery.
    """
    key = delivery_key(event_fingerprint_value, target_umo, part)
    record = (delivery_status or {}).get(key)
    return isinstance(record, Mapping) and record.get("status") == "success"


def prune_delivery_status(
    delivery_status: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    retention_days: int = STATE_RETENTION_DAYS,
    max_entries: int = STATE_MAX_ENTRIES,
) -> dict[str, Any]:
    """Remove expired, malformed, and excess delivery records.

    Args:
        delivery_status: Delivery-key-to-record mapping.
        now: Current time used for retention comparison.
        retention_days: Maximum record age in days.
        max_entries: Maximum retained records after age pruning.

    Returns:
        New mapping ordered from newest to oldest.

    Raises:
        ValueError: Retention or entry limits are invalid.
    """
    return _prune_records(
        delivery_status,
        timestamp_fields=("last_attempt_at", "delivered_at"),
        now=now,
        retention_days=retention_days,
        max_entries=max_entries,
    )


def normalize_registry(raw: Any) -> list[dict[str, Any]]:
    """Compatibility wrapper for group registry normalization.

    Args:
        raw: Sequence or mapping of explicitly registered group targets.

    Returns:
        Normalized group target list.
    """
    return normalize_group_registry(raw)


def alert_matches_subscription(event: Any, subscription: Mapping[str, Any]) -> bool:
    """Compatibility wrapper using event-first argument order.

    Args:
        event: Alert event mapping, dataclass, or object.
        subscription: Normalized subscription rule.

    Returns:
        True when the subscription should receive the event.
    """
    return subscription_accepts_event(subscription, event)


def format_alert(event: Any) -> str:
    """Compatibility wrapper for Traditional Chinese alert formatting.

    Args:
        event: Alert event mapping, dataclass, or object.

    Returns:
        Formatted alert text.
    """
    return format_alert_event(event)


def format_forecast(forecast: Any, *, max_periods: int = 3) -> str:
    """Compatibility wrapper for Traditional Chinese forecast formatting.

    Args:
        forecast: Forecast mapping, dataclass, or object.
        max_periods: Maximum number of periods to display.

    Returns:
        Formatted forecast text.
    """
    return format_weather_forecast(forecast, max_periods=max_periods)


def _normalize_bool(value: Any, field_name: str) -> bool:
    """Normalize a common JSON-compatible boolean representation.

    Args:
        value: Candidate boolean value.
        field_name: Traditional Chinese field label for errors.

    Returns:
        Normalized boolean.

    Raises:
        ValidationError: The value cannot be interpreted as a boolean.
    """
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in {"true", "1", "yes", "on"}:
            return True
        if candidate in {"false", "0", "no", "off"}:
            return False
    raise ValidationError(f"{field_name}必須是布林值。")


def _bounded_text(
    value: Any, field_name: str, max_length: int, *, required: bool
) -> str:
    """Validate one trimmed bounded string field.

    Args:
        value: Candidate text.
        field_name: Traditional Chinese field label for errors.
        max_length: Maximum character count.
        required: Whether blank text is rejected.

    Returns:
        Trimmed text.

    Raises:
        ValidationError: Required text is blank or too long.
    """
    text = str(value or "").strip()
    if required and not text:
        raise ValidationError(f"{field_name}不可空白。")
    if len(text) > max_length:
        raise ValidationError(f"{field_name}不可超過 {max_length} 個字元。")
    return text


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    """Read the first available name from a mapping, dataclass, or object.

    Args:
        obj: Source mapping or object.
        *names: Candidate field names in priority order.
        default: Value returned when no field exists.

    Returns:
        First matching field value or ``default``.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        obj = asdict(obj)
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj:
                return obj[name]
        return default
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _event_category(event: Any) -> str:
    """Read an event category with a safe ``other`` fallback.

    Args:
        event: Alert event mapping or object.

    Returns:
        Canonical internal alert category.
    """
    value = str(
        _value(event, "category", "alert_type", "type", default="other") or "other"
    )
    return _CATEGORY_ALIASES.get(value, _CATEGORY_ALIASES.get(value.lower(), "other"))


def _event_areas(event: Any) -> list[str]:
    """Extract non-empty affected-area strings from an event.

    Args:
        event: Alert event mapping or object.

    Returns:
        Deduplicated area strings in provider order.
    """
    value = _value(
        event,
        "areas",
        "affected_areas",
        "locations",
        "area",
        default=[],
    )
    if isinstance(value, str):
        values: Sequence[Any] = [value]
    elif isinstance(value, Sequence):
        values = value
    else:
        values = []
    areas: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("name") or item.get("location_name") or item.get("area")
        text = str(item or "").strip()
        if text and text not in areas:
            areas.append(text)
    return areas


def _cities_mentioned(areas: Sequence[str]) -> set[str]:
    """Find official cities mentioned anywhere in provider area text.

    Args:
        areas: Provider area descriptions.

    Returns:
        Set of official city names, empty when location is indeterminate.
    """
    text = " ".join(str(area) for area in areas)
    normalized_text = text.replace("台北", "臺北").replace("台中", "臺中")
    normalized_text = normalized_text.replace("台南", "臺南").replace("台東", "臺東")
    return {city for city in OFFICIAL_CITIES if city in normalized_text}


def _normalized_text(value: Any) -> str:
    """Normalize Unicode and whitespace for stable event identities.

    Args:
        value: Text-like value.

    Returns:
        NFKC-normalized single-line text.
    """
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).split())


def _canonical_fingerprint_text(value: Any) -> str:
    """Normalize semantically equivalent event text for stable identities.

    Whitespace and prose punctuation are ignored, while punctuation embedded
    between digits is retained so dates, times, ranges, and decimal values do
    not collapse into different numbers.

    Args:
        value: Provider text from an event title, body, or affected area.

    Returns:
        Case-folded canonical text suitable for event fingerprinting.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).replace("台", "臺")
    result: list[str] = []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        if unicodedata.category(character).startswith("P"):
            previous = text[index - 1] if index else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if character in {".", ":", "/", "-"} and (
                previous.isdigit() and following.isdigit()
            ):
                result.append(character)
            elif (
                character in {"+", "-"}
                and following.isdigit()
                and (not previous or not previous.isalnum())
            ):
                result.append(character)
            continue
        result.append(character.casefold())
    return "".join(result)


def _normalize_bulletin(value: Any) -> str:
    """Normalize numeric warning issue labels across API and RSS sources.

    Args:
        value: Bulletin value such as ``3`` or ``第 3 報``.

    Returns:
        Numeric issue string when recognized, otherwise normalized text.
    """
    text = _normalized_text(value)
    match = re.fullmatch(r"(?:第\s*)?(\d+)(?:\s*報)?", text)
    return str(int(match.group(1))) if match else text


def _parse_datetime(value: Any) -> datetime | None:
    """Parse datetime, ISO-8601, or RFC-2822 values.

    Args:
        value: Candidate datetime value.

    Returns:
        Timezone-aware datetime when parseable, otherwise ``None``.
    """
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(text)
            except (TypeError, ValueError, OverflowError):
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TIMEZONE)
    return parsed


def _as_taipei(value: datetime) -> datetime:
    """Convert a datetime to the fixed Taiwan civil timezone.

    Args:
        value: Aware or naive datetime.

    Returns:
        Aware datetime with the Taipei UTC+8 timezone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=TAIPEI_TIMEZONE)
    return value.astimezone(TAIPEI_TIMEZONE)


def _normalize_timestamp(value: Any) -> str:
    """Normalize a timestamp to an ISO-8601 string.

    Args:
        value: Datetime or parseable timestamp text.

    Returns:
        ISO-8601 timestamp with second precision.

    Raises:
        ValidationError: The timestamp cannot be parsed.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        raise ValidationError(f"無效的日期時間：{value}")
    return parsed.isoformat(timespec="seconds")


def _canonical_event_time(value: Any) -> str:
    """Normalize event time to a UTC second for cross-source matching.

    Args:
        value: Event timestamp.

    Returns:
        UTC second string, or normalized original text when unparseable.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        return _normalized_text(value)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _display_time(value: Any) -> str:
    """Format one timestamp for Traditional Chinese user messages.

    Args:
        value: Datetime or timestamp text.

    Returns:
        Taipei-local display time or the untouched source text.
    """
    parsed = _parse_datetime(value)
    if parsed is None:
        return str(value or "未提供").strip()
    return _as_taipei(parsed).strftime("%Y/%m/%d %H:%M")


def _clean_url(value: str) -> str:
    """Remove tracking/cache query strings from an official display URL.

    Args:
        value: URL to display.

    Returns:
        URL without query string or fragment.
    """
    parts = urlsplit(value.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _split_long_segment(segment: str, limit: int) -> list[str]:
    """Split one oversized paragraph at natural punctuation where possible.

    Args:
        segment: Non-empty paragraph text.
        limit: Maximum output length.

    Returns:
        Non-empty pieces no longer than the limit.
    """
    pieces: list[str] = []
    remaining = segment.strip()
    while len(remaining) > limit:
        window = remaining[: limit + 1]
        split_at = -1
        include_boundary = False
        for boundary in ("\n", "。", "！", "？", "；", "，", " "):
            candidate = window.rfind(boundary, 0, limit + 1)
            if candidate > split_at:
                split_at = candidate
                include_boundary = boundary not in {"\n", " "}
        if split_at < max(1, limit // 3):
            split_at = limit
            include_boundary = False
        elif include_boundary:
            split_at += 1
        piece = remaining[:split_at].strip()
        if not piece:
            piece = remaining[:limit]
            split_at = limit
        pieces.append(piece)
        remaining = remaining[split_at:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


def _prune_records(
    records: Mapping[str, Any] | None,
    *,
    timestamp_fields: Sequence[str],
    now: datetime | None,
    retention_days: int,
    max_entries: int,
) -> dict[str, Any]:
    """Prune a timestamped state mapping by age and count.

    Args:
        records: Key-to-record mapping.
        timestamp_fields: Timestamp fields in priority order.
        now: Current time for age comparison.
        retention_days: Maximum age in days.
        max_entries: Maximum number of records.

    Returns:
        New mapping ordered from newest to oldest.

    Raises:
        ValueError: Retention or count values are invalid.
    """
    if retention_days < 0:
        raise ValueError("保留天數不可為負數。")
    if max_entries < 0:
        raise ValueError("紀錄數量上限不可為負數。")
    current = _as_taipei(now or datetime.now(TAIPEI_TIMEZONE))
    cutoff = current - timedelta(days=retention_days)
    valid: list[tuple[datetime, str, dict[str, Any]]] = []
    for key, value in (records or {}).items():
        if not isinstance(value, Mapping):
            continue
        timestamp = None
        for field in timestamp_fields:
            timestamp = _parse_datetime(value.get(field))
            if timestamp is not None:
                break
        if timestamp is None or timestamp.astimezone(TAIPEI_TIMEZONE) < cutoff:
            continue
        valid.append((timestamp, str(key), dict(value)))
    valid.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return {key: value for _, key, value in valid[:max_entries]}


__all__ = [
    "ALERT_TYPES",
    "ALERT_TYPE_LABELS",
    "CITIES",
    "DEFAULT_FORECAST_TIMES",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SETTINGS",
    "ForecastSlot",
    "MAX_MESSAGE_LENGTH",
    "NATIONAL_ALERT_TYPES",
    "OFFICIAL_CITIES",
    "SOURCE_MODES",
    "SourceDecision",
    "TAIPEI_TIMEZONE",
    "ValidationError",
    "alert_matches_subscription",
    "default_settings",
    "default_source_state",
    "delivery_key",
    "delivery_succeeded",
    "due_forecast_slots",
    "effective_api_key",
    "event_fingerprint",
    "event_is_recent",
    "event_was_seen",
    "filter_subscriptions",
    "format_alert",
    "forecast_slot_key",
    "format_alert_event",
    "format_forecast",
    "format_weather_forecast",
    "mark_delivery_status",
    "mark_event_seen",
    "normalize_alert_type",
    "normalize_city",
    "normalize_group_registry",
    "normalize_group_target",
    "normalize_registry",
    "normalize_settings",
    "normalize_subscription",
    "prune_delivery_status",
    "prune_seen_events",
    "public_settings",
    "select_data_source",
    "should_download_typhoon_bulletin",
    "split_message",
    "subscription_accepts_event",
    "update_source_state",
]
