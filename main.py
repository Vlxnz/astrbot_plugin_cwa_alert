from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import uuid4

from astrbot.api import logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.components import File
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from . import service
from .cwa_client import (
    API_DATASETS,
    CATEGORY_LOCAL_EARTHQUAKE,
    CATEGORY_SIGNIFICANT_EARTHQUAKE,
    STATIC_MEDIA,
    TYPHOON_BULLETIN_URL,
    AlertEvent,
    CWAAlertBatchError,
    CWAClient,
    CWAClientError,
    WeatherForecast,
)

PLUGIN_NAME = "astrbot_plugin_cwa_alert"
SETTINGS_KEY = "settings_v1"
GROUPS_KEY = "known_groups_v1"
SOURCE_STATE_KEY = "source_state_v1"
SEEN_EVENTS_KEY = "seen_events_v1"
DELIVERY_STATUS_KEY = "delivery_status_v1"
ALERT_MEDIA_TIMEOUT_SECONDS = 6


class CWAAlertPlugin(star.Star):
    """Broadcast CWA forecasts and alerts to registered AstrBot groups."""

    def __init__(self, context: star.Context, config: dict | None = None) -> None:
        """Initialize the plugin and register dashboard APIs.

        Args:
            context: AstrBot plugin context.
            config: Optional legacy plugin configuration.
        """
        super().__init__(context, config)
        self._settings: dict[str, Any] = service.default_settings()
        self._groups: list[dict[str, Any]] = []
        self._source_state: dict[str, Any] = service.default_source_state()
        self._seen_events: dict[str, Any] = {}
        self._delivery_status: dict[str, Any] = {}
        self._client: CWAClient | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._manual_media_targets: set[str] = set()
        self._next_alert_poll_at = 0.0
        self._next_weather_attempt_at = 0.0
        self._started_at = datetime.now(service.TAIPEI_TIMEZONE)
        self._last_loop_error: str | None = None
        self._last_alert_poll_at: str | None = None
        self._register_web_apis()

    def _register_web_apis(self) -> None:
        """Register authenticated Plugin Page endpoints."""
        routes = (
            ("state", self.page_state, ["GET"], "讀取氣象播報狀態"),
            ("settings", self.page_save_settings, ["POST", "PUT"], "儲存氣象播報設定"),
            ("source/test", self.page_test_source, ["POST"], "測試中央氣象署資料來源"),
            ("preview", self.page_preview, ["POST"], "預覽氣象播報內容"),
            (
                "delivery/detail",
                self.page_delivery_detail,
                ["POST"],
                "讀取傳送紀錄詳情",
            ),
            ("test-send", self.page_test_send, ["POST"], "傳送群組測試訊息"),
            ("targets/delete", self.page_delete_target, ["POST"], "移除已登記群組"),
            (
                "targets/<target_id>",
                self.page_delete_target,
                ["DELETE"],
                "移除已登記群組",
            ),
        )
        for suffix, handler, methods, description in routes:
            self.context.register_web_api(
                f"/{PLUGIN_NAME}/{suffix}", handler, methods, description
            )

    async def initialize(self) -> None:
        """Load persisted state and start the single scheduler task."""
        if self._poll_task and not self._poll_task.done():
            logger.warning("中央氣象署播報排程已在執行，略過重複啟動。")
            return
        self._settings = self._load_settings(
            await self.get_kv_data(SETTINGS_KEY, service.default_settings())
        )
        self._groups = self._load_groups(await self.get_kv_data(GROUPS_KEY, []))
        source_state = await self.get_kv_data(
            SOURCE_STATE_KEY, service.default_source_state()
        )
        self._source_state = service.default_source_state()
        if isinstance(source_state, Mapping):
            self._source_state.update(dict(source_state))
        self._source_state.pop("pending_typhoon_images", None)
        self._last_alert_poll_at = self._source_state.get("last_alert_poll_at")
        seen_events = await self.get_kv_data(SEEN_EVENTS_KEY, {})
        delivery_status = await self.get_kv_data(DELIVERY_STATUS_KEY, {})
        self._seen_events = service.prune_seen_events(
            seen_events if isinstance(seen_events, Mapping) else {}
        )
        self._delivery_status = service.prune_delivery_status(
            delivery_status if isinstance(delivery_status, Mapping) else {}
        )
        self._client = CWAClient(api_key=service.effective_api_key(self._settings))
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"{PLUGIN_NAME}:poller"
        )

    async def terminate(self) -> None:
        """Stop background work, persist state, and close network resources."""
        task = self._poll_task
        self._poll_task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        try:
            await self._persist_runtime_state()
        except Exception as exc:
            logger.error("儲存中央氣象署播報執行狀態失敗：%s", self._safe_error(exc))
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                logger.error(
                    "關閉中央氣象署網路工作階段失敗：%s", self._safe_error(exc)
                )
        temp_dir = self._temp_directory()
        if temp_dir.exists():
            for pattern in ("typhoon_*.png", "typhoon_*.part"):
                for path in temp_dir.glob(pattern):
                    with suppress(OSError):
                        path.unlink()

    def _load_settings(self, value: Any) -> dict[str, Any]:
        try:
            return service.normalize_settings(
                value if isinstance(value, Mapping) else {}
            )
        except service.ValidationError as exc:
            logger.error("氣象播報設定無效，已改用停用的預設值：%s", exc)
            return service.default_settings()

    def _load_groups(self, value: Any) -> list[dict[str, Any]]:
        try:
            return service.normalize_group_registry(value)
        except service.ValidationError as exc:
            logger.error("已登記群組資料無效，已使用空清單：%s", exc)
            return []

    def _client_instance(self) -> CWAClient:
        if self._client is None:
            self._client = CWAClient(api_key=service.effective_api_key(self._settings))
        self._client.api_key = service.effective_api_key(self._settings)
        return self._client

    def _temp_directory(self) -> Path:
        return Path(get_astrbot_temp_path()) / PLUGIN_NAME

    async def _persist_runtime_state(self) -> None:
        await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
        await self.put_kv_data(SEEN_EVENTS_KEY, self._seen_events)
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self._settings.get("enabled"):
                    monotonic_now = time.monotonic()
                    if monotonic_now >= self._next_alert_poll_at:
                        interval = int(
                            self._settings.get(
                                "poll_interval_seconds",
                                service.DEFAULT_POLL_INTERVAL_SECONDS,
                            )
                        )
                        self._next_alert_poll_at = monotonic_now + interval
                        await self._poll_alerts()
                    await self._send_due_forecasts()
                self._last_loop_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_loop_error = self._safe_error(exc)
                logger.error("中央氣象署播報排程失敗：%s", self._last_loop_error)
            await asyncio.sleep(15)

    async def _poll_alerts(self) -> None:
        decision = service.select_data_source(self._settings, self._source_state)
        source = decision.active_source
        if source == "unavailable":
            self._source_state["last_api_error"] = decision.reason
            await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
            return

        events: Sequence[AlertEvent] = ()
        partial_events: Sequence[AlertEvent] = ()
        api_error: BaseException | None = None
        should_try_api = source == "api" or (
            decision.is_fallback and decision.api_key_configured
        )
        if should_try_api:
            try:
                events = await self._client_instance().fetch_api_alerts()
            except CWAAlertBatchError as exc:
                partial_events = exc.partial_events
                api_error = exc
                self._source_state = service.update_source_state(
                    self._settings,
                    self._source_state,
                    source="api",
                    succeeded=False,
                    error=self._safe_error(exc),
                )
            except Exception as exc:
                api_error = exc
                self._source_state = service.update_source_state(
                    self._settings,
                    self._source_state,
                    source="api",
                    succeeded=False,
                    error=self._safe_error(exc),
                )
            else:
                self._source_state = service.update_source_state(
                    self._settings,
                    self._source_state,
                    source="api",
                    succeeded=True,
                )
                source = "api"

        if api_error is not None:
            next_decision = service.select_data_source(
                self._settings, self._source_state
            )
            use_rss = decision.is_fallback or next_decision.active_source == "rss"
            if use_rss:
                try:
                    events = await self._fetch_alert_source("rss")
                except Exception as rss_exc:
                    self._source_state = service.update_source_state(
                        self._settings,
                        self._source_state,
                        source="rss",
                        succeeded=False,
                        error=self._safe_error(rss_exc),
                    )
                    if partial_events:
                        await self._dispatch_alert_events(
                            partial_events,
                            baseline_only=bool(
                                self._source_state.get("force_alert_baseline")
                            ),
                        )
                        self._record_alert_poll_metadata("api", len(partial_events))
                        self._source_state["last_alert_source"] = "api"
                    await self._persist_runtime_state()
                    raise
                source = "rss"
                self._source_state = service.update_source_state(
                    self._settings,
                    self._source_state,
                    source="rss",
                    succeeded=True,
                )
            elif partial_events:
                events = partial_events
                partial_events = ()
                source = "api"
            else:
                await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
                raise api_error
        elif not should_try_api:
            try:
                events = await self._fetch_alert_source(source)
            except Exception as exc:
                self._source_state = service.update_source_state(
                    self._settings,
                    self._source_state,
                    source=source,
                    succeeded=False,
                    error=self._safe_error(exc),
                )
                await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
                raise
            self._source_state = service.update_source_state(
                self._settings,
                self._source_state,
                source=source,
                succeeded=True,
            )

        force_baseline = bool(self._source_state.get("force_alert_baseline"))
        if partial_events:
            await self._dispatch_alert_events(
                partial_events, baseline_only=force_baseline
            )
        self._record_alert_poll_metadata(source, len(events) + len(partial_events))
        previous_source = str(self._source_state.get("last_alert_source") or "")
        baseline_key = f"{source}_alerts_baselined"
        baseline_only = force_baseline or (
            source == "rss"
            and (
                previous_source not in {"", source}
                or not bool(self._source_state.get(baseline_key))
            )
        )
        await self._dispatch_alert_events(events, baseline_only=baseline_only)
        self._source_state[baseline_key] = True
        self._source_state["last_alert_source"] = source
        self._source_state.pop("force_alert_baseline", None)
        await self._persist_runtime_state()

    def _record_alert_poll_metadata(self, source: str, event_count: int) -> None:
        self._source_state["last_alert_count"] = max(0, int(event_count))
        self._last_alert_poll_at = datetime.now(service.TAIPEI_TIMEZONE).isoformat(
            timespec="seconds"
        )
        self._source_state["last_alert_poll_at"] = self._last_alert_poll_at

    async def _fetch_alert_source(self, source: str) -> Sequence[AlertEvent]:
        client = self._client_instance()
        if source == "api":
            return await client.fetch_api_alerts()
        if source == "rss":
            return await client.fetch_rss_alerts()
        raise CWAClientError("目前沒有可用的警報資料來源。")

    async def _dispatch_alert_events(
        self, events: Sequence[AlertEvent], *, baseline_only: bool
    ) -> None:
        subscriptions = self._settings.get("subscriptions", [])
        pending_media: dict[str, tuple[AlertEvent, str, list[Mapping[str, Any]]]] = {}
        latest_typhoon_update = max(
            (
                timestamp
                for event in events
                if event.category == "typhoon"
                and (
                    event.is_cancellation
                    or service.should_download_typhoon_bulletin(event)
                )
                and (
                    timestamp := self._parse_datetime(
                        event.published_at or event.effective_at
                    )
                )
            ),
            default=None,
        )
        for event in events:
            fingerprint = service.event_fingerprint(event)
            existing = self._seen_events.get(fingerprint)
            first_observation = not isinstance(existing, Mapping)
            dispatchable = not baseline_only
            is_earthquake = event.category in {
                CATEGORY_SIGNIFICANT_EARTHQUAKE,
                CATEGORY_LOCAL_EARTHQUAKE,
            }
            currently_eligible = True
            if is_earthquake:
                currently_eligible = self._event_is_recent(event, minutes=5)
            elif event.source == "rss":
                currently_eligible = self._event_is_recent(event, hours=6)
            dispatchable = dispatchable and currently_eligible

            self._seen_events = service.mark_event_seen(self._seen_events, event)
            record = dict(self._seen_events.get(fingerprint, {}))
            if first_observation:
                record["dispatchable"] = dispatchable
            self._seen_events[fingerprint] = record
            if not bool(record.get("dispatchable", False)) or not currently_eligible:
                continue
            if self._event_is_expired(event) and not event.is_cancellation:
                continue
            if fingerprint in pending_media:
                continue

            matching = service.filter_subscriptions(subscriptions, event)
            scheduled = []
            message = service.format_alert_event(event)
            for subscription in matching:
                target = subscription["target_umo"]
                previous = self._delivery_status.get(
                    service.delivery_key(fingerprint, target, "text"), {}
                )
                if previous.get("status") == "success":
                    continue
                if (
                    not event.is_cancellation
                    and subscription.get("media_types")
                    and event.category
                    in {
                        "typhoon",
                        "rain",
                        CATEGORY_SIGNIFICANT_EARTHQUAKE,
                        CATEGORY_LOCAL_EARTHQUAKE,
                    }
                    and not (previous.get("content") or {}).get("accepted")
                ):
                    scheduled.append(subscription)
                else:
                    await self._deliver_text(
                        fingerprint,
                        target,
                        message,
                        event.title,
                        kind=event.category,
                        refresh=True,
                    )
            if scheduled:
                pending_media[fingerprint] = (event, message, scheduled)

        # Groups receiving text only do not wait on additional network requests.
        for fingerprint, (event, message, scheduled) in pending_media.items():
            selected = {kind for rule in scheduled for kind in rule["media_types"]}
            directory = self._temp_directory()
            try:
                directory.mkdir(parents=True, exist_ok=True)
                temp = TemporaryDirectory(dir=directory, prefix="media_")
            except OSError as exc:
                logger.warning(
                    "無法建立中央氣象署圖資暫存目錄：%s", self._safe_error(exc)
                )
                for rule in scheduled:
                    await self._deliver_text(
                        fingerprint,
                        rule["target_umo"],
                        message,
                        event.title,
                        kind=event.category,
                        refresh=True,
                    )
                continue
            with temp:
                media = await self._collect_alert_media(
                    event, fingerprint, selected, Path(temp.name), latest_typhoon_update
                )
                for rule in scheduled:
                    target = rule["target_umo"]
                    chosen = {
                        kind: result
                        for kind, result in media.items()
                        if kind in rule["media_types"]
                    }
                    images = [
                        value
                        for kind, (value, _) in chosen.items()
                        if kind != "typhoon_pdf" and isinstance(value, Path)
                    ]
                    pdf, _ = chosen.get("typhoon_pdf", (None, None))
                    pdf_url = STATIC_MEDIA["typhoon_pdf"] if pdf else None
                    send_file = bool(pdf and self._can_send_pdf(target))
                    mixed: list[tuple[bool, str | None]] = []
                    _, error = await self._deliver_text(
                        fingerprint,
                        target,
                        message,
                        event.title,
                        kind=event.category,
                        images=images,
                        mixed=mixed,
                        refresh=True,
                        pdf_url=None if send_file else pdf_url,
                    )
                    for kind, (value, download_error) in chosen.items():
                        label = service.MEDIA_LABELS[kind]
                        content: dict[str, Any] = {
                            "title": label,
                            "kind": event.category,
                        }
                        attachment_sent = False
                        attachment_error = download_error
                        if value and kind == "typhoon_pdf":
                            text_key = service.delivery_key(fingerprint, target, "text")
                            first_chunk_sent = bool(
                                self._delivery_status.get(text_key, {})
                                .get("content", {})
                                .get("accepted")
                            )
                            if send_file and first_chunk_sent:
                                (
                                    attachment_sent,
                                    attachment_error,
                                ) = await self._send_pdf(target, value)
                                content["media_name"] = value.name
                                if not attachment_sent and pdf_url:
                                    (
                                        attachment_sent,
                                        fallback_error,
                                    ) = await self._send_text(target, pdf_url)
                                    if attachment_sent:
                                        content = {
                                            "title": label,
                                            "kind": event.category,
                                            "chunks": [pdf_url],
                                        }
                                    else:
                                        attachment_error = (
                                            fallback_error or attachment_error
                                        )
                            elif send_file:
                                attachment_error = error
                            else:
                                content["chunks"] = [pdf_url]
                                attachment_sent = first_chunk_sent
                                attachment_error = None if attachment_sent else error
                        elif value:
                            content["media_name"] = value.name
                            attachment_sent, attachment_error = (
                                mixed[0] if mixed else (False, error)
                            )
                        self._delivery_status = service.mark_delivery_status(
                            self._delivery_status,
                            event_fingerprint_value=fingerprint,
                            target_umo=target,
                            part=kind,
                            succeeded=attachment_sent,
                            error=attachment_error,
                            content=content,
                        )

    async def _collect_alert_media(
        self,
        event: AlertEvent,
        fingerprint: str,
        selected: set[str],
        directory: Path,
        latest_typhoon_update: datetime | None,
    ) -> dict[str, tuple[Path | None, str | None]]:
        """Fetch the selected official attachments before the alert is sent.

        Args:
            event: Alert whose media may be attached.
            fingerprint: Cross-source event identity.
            selected: Union of requested media types across pending groups.
            directory: Temporary attachment directory.
            latest_typhoon_update: Most recent typhoon bulletin or cancellation in the batch.

        Returns:
            Per-type validated attachment path or error.
        """
        timestamp = self._parse_datetime(event.published_at or event.effective_at)
        if event.category == "typhoon":
            urls = {
                "typhoon_bulletin": TYPHOON_BULLETIN_URL,
                "typhoon_track": STATIC_MEDIA["typhoon_track"],
                "typhoon_pdf": STATIC_MEDIA["typhoon_pdf"],
            }
        elif event.category == "rain":
            urls = {
                key: STATIC_MEDIA[key] for key in ("rain_warning_map", "rain_forecast")
            }
        else:
            urls = {
                "earthquake_report": event.report_image,
                "earthquake_shakemap": event.shakemap_image,
            }
        urls = {kind: url for kind, url in urls.items() if kind in selected}
        if not urls:
            return {}
        client = self._client_instance()

        async def download(kind: str, url: str) -> tuple[Path | None, str | None]:
            try:
                if not url:
                    raise CWAClientError("本次官方報告未提供此圖資。")
                cutoff = None
                if kind.startswith("typhoon_") or kind == "rain_warning_map":
                    if not timestamp:
                        raise CWAClientError("警報缺少發布時間，無法確認圖資是否更新。")
                    cutoff = timestamp - timedelta(minutes=5)
                elif kind == "rain_forecast":
                    cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
                label = service.MEDIA_LABELS[kind].split("（", 1)[0]
                path = await client.download_official_media(
                    url, directory, label, modified_since=cutoff
                )
                if kind == "typhoon_bulletin":
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    last = self._source_state.get("last_typhoon_image_hash")
                    record = self._seen_events.get(fingerprint, {})
                    if digest == last and digest != record.get("typhoon_image_hash"):
                        raise CWAClientError("中央氣象署警報單圖片尚未更新。")
                    record["typhoon_image_hash"] = digest
                    self._source_state["last_typhoon_image_hash"] = digest
                return path, None
            except Exception as exc:
                error = self._safe_error(exc)
                logger.warning("中央氣象署官方圖資處理失敗（類型=%s）：%s", kind, error)
                return None, error

        results: dict[str, tuple[Path | None, str | None]] = {}
        tasks: dict[str, asyncio.Task[tuple[Path | None, str | None]]] = {}
        # Bound the alert delay and keep successful attachments if another times out.
        try:
            async with asyncio.timeout(ALERT_MEDIA_TIMEOUT_SECONDS):
                if event.category == "typhoon":
                    if (
                        not timestamp
                        or (latest_typhoon_update and timestamp < latest_typhoon_update)
                        or not (
                            service.should_download_typhoon_bulletin(event)
                            or (event.source == "api" and event.title.strip() == "颱風")
                        )
                    ):
                        return {}
                    issued = await client.active_typhoon_issued_at()
                    if not issued or abs(
                        timestamp - issued.replace(tzinfo=service.TAIPEI_TIMEZONE)
                    ) > timedelta(minutes=30):
                        return dict.fromkeys(
                            urls, (None, "官網沒有與本次警報相符的颱風圖資。")
                        )
                async with asyncio.TaskGroup() as group:
                    for kind, url in urls.items():
                        tasks[kind] = group.create_task(download(kind, url))
        except (TimeoutError, CWAClientError) as exc:
            error = (
                "官方圖資查詢逾時。"
                if isinstance(exc, TimeoutError)
                else self._safe_error(exc)
            )
            logger.warning("中央氣象署警報圖資未全部取得：%s", error)
            results = dict.fromkeys(urls, (None, error))
        results.update(
            (kind, task.result())
            for kind, task in tasks.items()
            if not task.cancelled()
        )
        return results

    def _can_send_pdf(self, target_umo: str) -> bool:
        """Check whether the target adapter handles the File component.

        Args:
            target_umo: Destination group.

        Returns:
            Whether the configured adapter supports PDF files.
        """
        if self._validate_target_umo(target_umo):
            return False
        session = MessageSession.from_str(target_umo)
        platform = self.context.get_platform_inst(session.platform_id)
        return platform is not None and platform.meta().name in {
            "aiocqhttp",
            "discord",
            "lark",
            "telegram",
            "satori",
        }

    async def _send_pdf(self, target_umo: str, path: Path) -> tuple[bool, str | None]:
        """Send a validated PDF as its own platform message.

        Args:
            target_umo: Destination group.
            path: Validated temporary PDF file.

        Returns:
            Whether the platform accepted the file and an optional error.
        """
        if not self._can_send_pdf(target_umo):
            return False, "此平台改用官方 PDF 網址。"
        try:
            chain = MessageChain(chain=[File(name=path.name, file=str(path))]).use_t2i(
                False
            )
            if not await self.context.send_message(target_umo, chain):
                return False, "找不到目標所屬的平台實例。"
            return True, None
        except Exception as exc:
            error = self._safe_error(exc)
            logger.warning("中央氣象署 PDF 傳送失敗（目標=%s）：%s", target_umo, error)
            return False, error

    async def _send_due_forecasts(self) -> None:
        subscriptions = [
            item
            for item in self._settings.get("subscriptions", [])
            if item.get("enabled") and item.get("forecast_enabled")
        ]
        sent_slots = self._source_state.get("forecast_slots")
        if not isinstance(sent_slots, dict):
            sent_slots = {}
            self._source_state["forecast_slots"] = sent_slots
        due: list[tuple[Mapping[str, Any], service.ForecastSlot]] = []
        for subscription in subscriptions:
            for slot in service.due_forecast_slots(subscription, sent_slots):
                due.append((subscription, slot))
        if not due:
            self._prune_forecast_slots()
            return
        monotonic_now = time.monotonic()
        if monotonic_now < self._next_weather_attempt_at:
            return
        self._next_weather_attempt_at = monotonic_now + 60

        cities = sorted(
            {city for subscription, _ in due for city in subscription.get("cities", [])}
        )
        source, forecasts = await self._fetch_weather_with_fallback(cities)
        forecast_map = {forecast.city: forecast for forecast in forecasts}
        for subscription, slot in due:
            requested_cities = list(subscription.get("cities", []))
            missing_cities = [
                city for city in requested_cities if city not in forecast_map
            ]
            if missing_cities:
                logger.warning(
                    "定時天氣播報仍缺少縣市資料，保留時段等待重試：%s",
                    "、".join(missing_cities),
                )
                continue
            selected = [
                forecast_map[city] for city in requested_cities if city in forecast_map
            ]
            if not selected:
                continue
            text = "\n\n".join(
                service.format_weather_forecast(forecast) for forecast in selected
            )
            succeeded, error = await self._deliver_text(
                slot.key,
                subscription["target_umo"],
                text,
                "定時天氣預報",
                kind="weather",
            )
            if succeeded:
                sent_slots[slot.key] = {
                    "date": slot.date,
                    "time": slot.time,
                    "source": source,
                    "sent_at": datetime.now(service.TAIPEI_TIMEZONE).isoformat(
                        timespec="seconds"
                    ),
                }
            elif error:
                logger.warning("定時天氣播報失敗：%s", error)
        self._prune_forecast_slots()
        await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)

    def _prune_forecast_slots(self) -> None:
        slots = self._source_state.get("forecast_slots")
        if not isinstance(slots, Mapping):
            self._source_state["forecast_slots"] = {}
            return
        cutoff = datetime.now(service.TAIPEI_TIMEZONE) - timedelta(days=2)
        retained: dict[str, Any] = {}
        for key, value in slots.items():
            if not isinstance(value, Mapping):
                continue
            sent_at = self._parse_datetime(value.get("sent_at"))
            if sent_at and sent_at.astimezone(service.TAIPEI_TIMEZONE) >= cutoff:
                retained[str(key)] = dict(value)
        self._source_state["forecast_slots"] = retained

    async def _fetch_weather_with_fallback(
        self, cities: Sequence[str]
    ) -> tuple[str, Sequence[WeatherForecast]]:
        decision = service.select_data_source(self._settings, self._source_state)
        source = decision.active_source
        if source == "unavailable":
            raise CWAClientError(decision.reason)
        try:
            forecasts = await self._fetch_weather_source(source, cities)
            self._record_weather_source_attempt(source, succeeded=True)
            return source, forecasts
        except Exception as exc:
            self._record_weather_source_attempt(
                source, succeeded=False, error=self._safe_error(exc)
            )
            if source == "api" and decision.requested_mode == "auto":
                try:
                    forecasts = await self._fetch_weather_source("rss", cities)
                except Exception as rss_exc:
                    self._record_weather_source_attempt(
                        "rss", succeeded=False, error=self._safe_error(rss_exc)
                    )
                    raise
                self._record_weather_source_attempt("rss", succeeded=True)
                return "rss", forecasts
            raise

    def _record_weather_source_attempt(
        self, source: str, *, succeeded: bool, error: str | None = None
    ) -> None:
        timestamp = datetime.now(service.TAIPEI_TIMEZONE).isoformat(timespec="seconds")
        self._source_state["last_weather_source"] = source
        self._source_state[f"last_weather_{source}_available"] = bool(succeeded)
        if succeeded:
            self._source_state[f"last_weather_{source}_success_at"] = timestamp
            self._source_state[f"last_weather_{source}_error"] = None
        else:
            self._source_state[f"last_weather_{source}_error"] = str(
                error or "未知錯誤"
            )[:500]

    async def _fetch_weather_source(
        self, source: str, cities: Sequence[str]
    ) -> Sequence[WeatherForecast]:
        client = self._client_instance()
        if source == "api":
            forecasts = await client.fetch_api_weather(cities)
            returned_cities = {forecast.city for forecast in forecasts}
            missing_cities = [city for city in cities if city not in returned_cities]
            if missing_cities:
                raise CWAClientError(
                    "開放資料 API 未提供縣市天氣：" + "、".join(missing_cities)
                )
            return forecasts
        if source == "rss":
            selected_cities = list(dict.fromkeys(cities))
            responses = await asyncio.gather(
                *(client.fetch_rss_weather(city) for city in selected_cities),
                return_exceptions=True,
            )
            forecasts: list[WeatherForecast] = []
            city_errors: dict[str, str] = {}
            for city, result in zip(selected_cities, responses, strict=True):
                if isinstance(result, asyncio.CancelledError):
                    raise result
                if isinstance(result, BaseException):
                    city_errors[city] = self._safe_error(result)
                else:
                    forecasts.append(result)
            self._source_state["last_weather_rss_city_errors"] = city_errors
            if not forecasts and city_errors:
                raise CWAClientError(
                    "縣市天氣 RSS 皆無法取得：" + "、".join(city_errors)
                )
            return tuple(forecasts)
        raise CWAClientError("目前沒有可用的天氣資料來源。")

    async def _deliver_text(
        self,
        fingerprint: str,
        target_umo: str,
        text: str,
        title: str,
        *,
        kind: str,
        images: Sequence[Path] = (),
        mixed: list[tuple[bool, str | None]] | None = None,
        refresh: bool = False,
        pdf_url: str | None = None,
    ) -> tuple[bool, str | None]:
        key = service.delivery_key(fingerprint, target_umo, "text")
        previous = self._delivery_status.get(key, {})
        snapshot = previous.get("content") or {}
        chunks = snapshot.get("chunks")
        if (refresh and not snapshot.get("accepted")) or not chunks:
            if pdf_url:
                chunks = service.split_message(
                    text, service.MAX_MESSAGE_LENGTH - len(pdf_url) - 1
                )
                chunks[0] += f"\n{pdf_url}"
            else:
                chunks = service.split_message(text)
        accepted = list(chunks[: snapshot.get("accepted", 0)])
        succeeded, error = await self._send_text(
            target_umo,
            text,
            chunks=chunks,
            accepted=accepted,
            images=images,
            mixed=mixed,
        )
        self._delivery_status = service.mark_delivery_status(
            self._delivery_status,
            event_fingerprint_value=fingerprint,
            target_umo=target_umo,
            part="text",
            succeeded=succeeded,
            error=error,
            content={
                "title": title,
                "kind": kind,
                "chunks": chunks,
                "accepted": len(chunks) if succeeded else len(accepted),
            },
        )
        return succeeded, error

    async def _send_text(
        self,
        target_umo: str,
        text: str,
        *,
        chunks: Sequence[str] | None = None,
        accepted: list[str] | None = None,
        images: Sequence[Path] = (),
        mixed: list[tuple[bool, str | None]] | None = None,
    ) -> tuple[bool, str | None]:
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            return False, validation_error
        try:
            chunks = service.split_message(text) if chunks is None else chunks
            offset = len(accepted) if accepted is not None else 0
            for index, chunk in enumerate(chunks[offset:], start=offset):
                sent = False
                if index == 0 and images:
                    try:
                        chain = MessageChain().message(chunk).use_t2i(False)
                        for path in images:
                            chain.file_image(str(path))
                        sent = await self.context.send_message(target_umo, chain)
                        image_error = None if sent else "平台未接受文字與圖片合併訊息。"
                    except Exception as exc:
                        image_error = self._safe_error(exc)
                    if mixed is not None:
                        mixed.append((sent, image_error))
                    if not sent:
                        logger.warning(
                            "中央氣象署合併訊息未確認送出，改試純文字（目標=%s）：%s",
                            target_umo,
                            image_error,
                        )
                if not sent:
                    sent = await self.context.send_message(
                        target_umo, MessageChain().message(chunk).use_t2i(False)
                    )
                if not sent:
                    error = "找不到目標所屬的平台實例。"
                    logger.error(
                        "中央氣象署主動文字播報未送出（目標=%s）：%s",
                        target_umo,
                        error,
                    )
                    return False, error
                if accepted is not None:
                    accepted.append(chunk)
                if index < len(chunks) - 1:
                    await asyncio.sleep(0.2)
            return True, None
        except Exception as exc:
            error = self._safe_error(exc)
            logger.error(
                "中央氣象署主動文字播報失敗（目標=%s）：%s",
                target_umo,
                error,
            )
            return False, error

    def _validate_target_umo(
        self, target_umo: str, *, require_available: bool = True
    ) -> str | None:
        try:
            session = MessageSession.from_str(target_umo)
        except Exception:
            return "群組目標 UMO 格式無效。"
        if not session.platform_id.strip() or not session.session_id.strip():
            return "群組目標 UMO 格式無效。"
        if session.message_type != MessageType.GROUP_MESSAGE:
            return "主動播報只支援群組目標。"
        if not require_available:
            return None
        platform = self.context.get_platform_inst(session.platform_id)
        if platform is None:
            return "群組目標所屬平台目前未載入。"
        metadata = platform.meta()
        if metadata.name in {"wecom", "weixin_official_account"}:
            return "此平台不支援群組主動播報登記。"
        if not metadata.support_proactive_message:
            return "群組目標所屬平台不支援主動訊息。"
        return None

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cwa_register")
    async def cwa_register(
        self, event: AstrMessageEvent, display_name: str = ""
    ) -> None:
        """Register the current group as a broadcast target."""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await event.send(MessageChain().message("此指令只能在群組中使用。"))
            return
        target_umo = event.unified_msg_origin
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            await event.send(MessageChain().message(validation_error))
            return
        session = MessageSession.from_str(target_umo)
        platform = self.context.get_platform_inst(session.platform_id)
        assert platform is not None
        platform_type = platform.meta().name
        group = getattr(event.message_obj, "group", None)
        fallback_name = getattr(group, "group_name", None) or event.get_group_id()
        raw_target = {
            "target_umo": target_umo,
            "platform_id": session.platform_id,
            "platform_type": platform_type,
            "group_id": event.get_group_id() or session.session_id,
            "display_name": display_name.strip() or fallback_name or session.session_id,
            "registered_at": datetime.now(service.TAIPEI_TIMEZONE),
            "enabled": True,
        }
        try:
            target = service.normalize_group_target(raw_target)
        except service.ValidationError as exc:
            await event.send(MessageChain().message(str(exc)))
            return
        conflict = False
        async with self._state_lock:
            conflict = platform_type == "wecom_ai_bot" and any(
                item["platform_id"] == session.platform_id
                and item["target_umo"] != target_umo
                for item in self._groups
            )
            if not conflict:
                previous = next(
                    (item for item in self._groups if item["target_umo"] == target_umo),
                    None,
                )
                if previous:
                    if not display_name.strip():
                        target["display_name"] = previous["display_name"]
                    target["registered_at"] = previous["registered_at"]
                    target["tested_at"] = previous.get("tested_at")
                self._groups = [
                    item for item in self._groups if item["target_umo"] != target_umo
                ]
                self._groups.append(target)
                await self.put_kv_data(GROUPS_KEY, self._groups)
                subscriptions = self._settings.setdefault("subscriptions", [])
                if not any(
                    item.get("target_umo") == target_umo for item in subscriptions
                ):
                    subscriptions.append(
                        service.normalize_subscription(
                            {
                                "target_umo": target_umo,
                                "enabled": False,
                                "cities": [],
                                "forecast_enabled": False,
                                "forecast_times": list(service.DEFAULT_FORECAST_TIMES),
                                "alert_types": [],
                                "media_types": [],
                            }
                        )
                    )
                    self._settings["revision"] = (
                        int(self._settings.get("revision", 0)) + 1
                    )
                    await self.put_kv_data(SETTINGS_KEY, self._settings)
        if conflict:
            await event.send(
                MessageChain().message(
                    "此企業微信智慧機器人使用固定推送 Webhook，同一平台實例只能登記一個目標。"
                )
            )
            return
        suffix = (
            "\n提醒：QQ 官方平台重啟後，請在群內產生新訊息並再次測試主動傳送。"
            if platform_type in {"qq_official", "qqofficial"}
            else ""
        )
        await event.send(
            MessageChain().message(
                f"已登記群組「{target['display_name']}」。請在外掛管理頁的「群組播報」調整設定。{suffix}"
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cwa_unregister")
    async def cwa_unregister(self, event: AstrMessageEvent) -> None:
        """Unregister the current group and disable its subscription."""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await event.send(MessageChain().message("此指令只能在群組中使用。"))
            return
        target_umo = event.unified_msg_origin
        removed = False
        async with self._state_lock:
            original_count = len(self._groups)
            self._groups = [
                item for item in self._groups if item["target_umo"] != target_umo
            ]
            removed = len(self._groups) != original_count
            if removed:
                for subscription in self._settings.get("subscriptions", []):
                    if subscription.get("target_umo") == target_umo:
                        subscription["enabled"] = False
                self._settings["revision"] = int(self._settings.get("revision", 0)) + 1
                await self.put_kv_data(GROUPS_KEY, self._groups)
                await self.put_kv_data(SETTINGS_KEY, self._settings)
        if not removed:
            await event.send(MessageChain().message("目前群組尚未登記。"))
            return
        await event.send(
            MessageChain().message("已取消目前群組登記，相關播報規則已停用。")
        )

    @filter.command("cwa_weather")
    async def cwa_weather(self, event: AstrMessageEvent, city: str = "") -> None:
        """Query current 36-hour weather for a city or configured group."""
        cities: list[str] = []
        if city.strip():
            try:
                cities = [service.normalize_city(city)]
            except service.ValidationError as exc:
                await event.send(MessageChain().message(str(exc)))
                return
        else:
            subscription = next(
                (
                    item
                    for item in self._settings.get("subscriptions", [])
                    if item.get("target_umo") == event.unified_msg_origin
                ),
                None,
            )
            if subscription:
                cities = list(subscription.get("cities", []))
        if not cities:
            await event.send(
                MessageChain().message(
                    "請指定縣市，例如 /cwa_weather 臺北市，或先在管理頁設定目前群組的縣市。"
                )
            )
            return
        try:
            _, forecasts = await self._fetch_weather_with_fallback(cities)
            text = "\n\n".join(
                service.format_weather_forecast(forecast) for forecast in forecasts
            )
            returned_cities = {forecast.city for forecast in forecasts}
            missing_cities = [city for city in cities if city not in returned_cities]
            if missing_cities:
                text = (
                    (f"{text}\n\n" if text else "")
                    + "暫時無法取得以下縣市資料："
                    + "、".join(missing_cities)
                )
            for chunk in service.split_message(text):
                await event.send(MessageChain().message(chunk).use_t2i(False))
        except Exception as exc:
            await event.send(
                MessageChain().message(f"天氣查詢失敗：{self._safe_error(exc)}")
            )

    @filter.command("cwa_status")
    async def cwa_status(self, event: AstrMessageEvent) -> None:
        """Show source, scheduler, and current-group registration status."""
        decision = service.select_data_source(self._settings, self._source_state)
        mode_label = {
            "auto": "自動",
            "api": "僅開放資料 API",
            "rss": "僅官方 RSS",
        }.get(decision.requested_mode, "未知")

        def health_label(value: Any) -> str:
            if value is True:
                return "正常"
            if value is False:
                return "異常"
            return "尚未測試"

        try:
            api_failures = max(
                0, int(self._source_state.get("api_consecutive_failures") or 0)
            )
        except (TypeError, ValueError):
            api_failures = 0

        target = next(
            (
                item
                for item in self._groups
                if item.get("target_umo") == event.unified_msg_origin
            ),
            None,
        )
        lines = [
            "中央氣象署播報狀態",
            f"總開關：{'啟用' if self._settings.get('enabled') else '停用'}",
            f"資料模式：{mode_label}",
            f"目前來源：{decision.status_label}",
            f"授權碼：{'已設定' if decision.api_key_configured else '未設定（選填）'}",
            "API 健康："
            f"{health_label(self._source_state.get('api_available'))}"
            f"（連續失敗 {api_failures} 次）",
            f"RSS 健康：{health_label(self._source_state.get('rss_available'))}",
            f"已登記群組：{len(self._groups)} 個",
            f"目前群組：{target['display_name'] if target else '尚未登記'}",
            f"最近警報輪詢：{self._last_alert_poll_at or '尚未執行'}",
        ]
        last_error = (
            self._last_loop_error
            or self._source_state.get("last_api_error")
            or self._source_state.get("last_rss_error")
            or self._source_state.get("last_weather_api_error")
            or self._source_state.get("last_weather_rss_error")
        )
        if last_error:
            lines.append(f"最近錯誤：{last_error}")
        await event.send(MessageChain().message("\n".join(lines)).use_t2i(False))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cwa_test")
    async def cwa_test(self, event: AstrMessageEvent) -> None:
        """Send one proactive test message to the registered current group."""
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            await event.send(MessageChain().message("此指令只能在群組中使用。"))
            return
        target = next(
            (
                item
                for item in self._groups
                if item.get("target_umo") == event.unified_msg_origin
            ),
            None,
        )
        if not target:
            await event.send(
                MessageChain().message("目前群組尚未登記，請先使用 /cwa_register。")
            )
            return
        succeeded, error = await self._deliver_text(
            uuid4().hex,
            target["target_umo"],
            "【中央氣象署播報測試】\n主動訊息傳送已交由 AstrBot 平台介面處理。",
            "群組測試",
            kind="test",
        )
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)
        if not succeeded:
            await event.send(MessageChain().message(f"測試失敗：{error}"))
            return
        target["tested_at"] = datetime.now(service.TAIPEI_TIMEZONE).isoformat(
            timespec="seconds"
        )
        await self.put_kv_data(GROUPS_KEY, self._groups)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("cwa_media")
    async def cwa_media(self, event: AstrMessageEvent) -> None:
        """Fetch official media for the current group without polling.

        Args:
            event: The administrator's group command event.
        """
        target = event.unified_msg_origin
        if event.get_message_type() != MessageType.GROUP_MESSAGE:
            reason = "此指令只能在群組中使用。"
        elif not any(group.get("target_umo") == target for group in self._groups):
            reason = "目前群組尚未登記，請先使用 /cwa_register。"
        elif target in self._manual_media_targets:
            reason = "此群組已有圖資查詢正在進行，請稍候。"
        else:
            reason = self._validate_target_umo(target)
        if reason:
            await event.send(MessageChain().message(reason).use_t2i(False))
            return

        self._manual_media_targets.add(target)
        try:
            await self._send_latest_media(target)
        except Exception as exc:
            error = self._safe_error(exc)
            logger.warning("中央氣象署手動圖資查詢失敗：%s", error)
            await event.send(
                MessageChain().message(f"圖資查詢失敗：{error}").use_t2i(False)
            )
        finally:
            self._manual_media_targets.discard(target)

    async def _send_latest_media(self, target: str) -> None:
        """Collect and send the latest supported official attachments.

        Args:
            target: Validated unified message origin of the requesting group.

        """
        client = self._client_instance()
        notes: list[str] = []
        media = {
            key: (STATIC_MEDIA[key], None)
            for key in ("rain_warning_map", "rain_forecast")
        }

        if self._settings.get("source_mode") == "rss":
            notes.append("地震圖資：僅 RSS 模式不查詢開放資料 API。")
        elif not service.effective_api_key(self._settings):
            notes.append("地震圖資：需要設定開放資料 API 授權碼。")
        else:
            reports: Sequence[AlertEvent] | None = None
            try:
                reports = await client.fetch_api_alerts(
                    (
                        API_DATASETS["significant_earthquake"],
                        API_DATASETS["local_earthquake"],
                    )
                )
            except CWAAlertBatchError as exc:
                reports = exc.partial_events
                notes.append(f"地震資料集部分或全部無法取得：{self._safe_error(exc)}")
            except CWAClientError as exc:
                notes.append(f"無法查詢地震圖資：{self._safe_error(exc)}")

            latest = max(
                (
                    (issued, report)
                    for report in reports or ()
                    if not report.is_cancellation
                    and (report.report_image or report.shakemap_image)
                    and (
                        issued := self._parse_datetime(
                            report.published_at or report.effective_at
                        )
                    )
                ),
                key=lambda item: item[0],
                default=None,
            )
            if latest:
                issued, report = latest
                local = issued.astimezone(service.TAIPEI_TIMEZONE)
                notes.append(f"地震圖資報告時間：{local:%Y/%m/%d %H:%M}（臺灣時間）。")
                for kind, url in (
                    ("earthquake_report", report.report_image),
                    ("earthquake_shakemap", report.shakemap_image),
                ):
                    if url:
                        media[kind] = (url, None)
                    else:
                        notes.append(f"{service.MEDIA_LABELS[kind]}：該報告未提供。")
            elif reports is not None:
                notes.append("地震圖資：找不到附有圖片的地震報告。")

        issued = None
        try:
            issued = await client.active_typhoon_issued_at()
        except CWAClientError as exc:
            notes.append(f"颱風警報狀態無法確認，略過警報圖片：{self._safe_error(exc)}")
        else:
            if issued is None:
                notes.append("官網目前未發布有效颱風警報，略過警報單與動態圖。")
        cutoff = None
        if issued:
            cutoff = issued.replace(tzinfo=service.TAIPEI_TIMEZONE) - timedelta(
                minutes=5
            )
            media["typhoon_bulletin"] = (TYPHOON_BULLETIN_URL, cutoff)
            media["typhoon_track"] = (STATIC_MEDIA["typhoon_track"], cutoff)
        media["typhoon_pdf"] = (STATIC_MEDIA["typhoon_pdf"], cutoff)

        fingerprint = uuid4().hex
        directory = self._temp_directory()
        directory.mkdir(parents=True, exist_ok=True)
        contents: dict[str, dict[str, Any]] = {}
        outcomes: dict[str, tuple[bool, str | None]] = {}
        images: dict[str, Path] = {}
        with TemporaryDirectory(dir=directory, prefix="media_") as temp:
            for kind, (url, modified_since) in media.items():
                label = service.MEDIA_LABELS[kind].split("（", 1)[0]
                if kind == "typhoon_pdf" and not issued:
                    label += "（不作現行警報使用）"
                content: dict[str, Any] = {
                    "title": f"手動圖資｜{label}",
                    "kind": "test",
                }
                contents[kind] = content
                try:
                    path = await client.download_official_media(
                        url, Path(temp), label, modified_since=modified_since
                    )
                    if kind == "typhoon_pdf":
                        succeeded, error = await self._send_pdf(target, path)
                        if succeeded:
                            content["media_name"] = path.name
                            outcomes[kind] = (True, None)
                            notes.append(f"{label}：已另外交付檔案給平台。")
                        else:
                            content["chunks"] = [url]
                            notes.extend((f"{label}：{error or '無法傳檔'}", url))
                    else:
                        content["media_name"] = path.name
                        images[kind] = path
                        notes.append(f"{label}：已下載。")
                except Exception as exc:
                    error = self._safe_error(exc)
                    logger.warning(
                        "中央氣象署手動圖資處理失敗（類型=%s）：%s", label, error
                    )
                    outcomes[kind] = (False, error)
                    notes.append(f"{label}：{error}")
            text = "\n".join(
                [
                    "【中央氣象署｜手動圖資查詢】",
                    "可能包含過往報告，不代表有新警報。",
                    *notes,
                ]
            )
            mixed: list[tuple[bool, str | None]] = []
            succeeded, error = await self._deliver_text(
                fingerprint,
                target,
                text,
                "手動圖資查詢",
                kind="test",
                images=list(images.values()),
                mixed=mixed,
            )
            for kind in images:
                outcomes[kind] = mixed[0] if mixed else (False, error)
            if "typhoon_pdf" not in outcomes:
                outcomes["typhoon_pdf"] = (succeeded, error)
            for kind, (part_succeeded, part_error) in outcomes.items():
                self._delivery_status = service.mark_delivery_status(
                    self._delivery_status,
                    event_fingerprint_value=fingerprint,
                    target_umo=target,
                    part=kind,
                    succeeded=part_succeeded,
                    error=part_error,
                    content=contents[kind],
                )
            await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)
        if not succeeded:
            raise CWAClientError(error or "查詢結果未能交付平台。")

    async def page_state(self):
        targets = [self._target_public_state(target) for target in self._groups]
        deliveries = sorted(
            (
                {
                    **{
                        name: value
                        for name, value in record.items()
                        if name != "content"
                    },
                    "id": key,
                    "title": (record.get("content") or {}).get("title"),
                    "kind": (record.get("content") or {}).get("kind"),
                }
                for key, record in self._delivery_status.items()
                if isinstance(record, Mapping)
            ),
            key=lambda item: str(
                item.get("last_attempt_at") or item.get("delivered_at") or ""
            ),
            reverse=True,
        )
        source_state = dict(self._source_state)
        decision = service.select_data_source(self._settings, self._source_state)
        source_state.update(asdict(decision))
        source_state["status_label"] = decision.status_label
        source_state["last_loop_error"] = self._last_loop_error
        return json_response(
            {
                "revision": int(self._settings.get("revision", 0)),
                "settings": service.public_settings(self._settings),
                "targets": targets,
                "source_state": source_state,
                "delivery_status": deliveries,
                "cities": list(service.OFFICIAL_CITIES),
                "alert_types": dict(service.ALERT_TYPE_LABELS),
                "media_types": dict(service.MEDIA_LABELS),
                "started_at": self._started_at.isoformat(timespec="seconds"),
            }
        )

    async def page_delivery_detail(self):
        payload = await request.json(default={})
        key = payload.get("id") if isinstance(payload, Mapping) else None
        if not isinstance(key, str) or len(key) > 256:
            return error_response("傳送紀錄識別碼無效。")
        record = self._delivery_status.get(key)
        if not isinstance(record, Mapping):
            return error_response("這筆傳送紀錄已不存在。", status_code=404)
        return json_response({"record": dict(record)})

    async def page_save_settings(self):
        payload = await request.json(default={})
        if not isinstance(payload, Mapping):
            return error_response("設定內容必須是 JSON 物件。")
        try:
            supplied_revision = int(payload.get("revision", -1))
        except (TypeError, ValueError):
            return error_response("設定版本格式無效。")
        async with self._state_lock:
            current_revision = int(self._settings.get("revision", 0))
            if supplied_revision != current_revision:
                return error_response(
                    "設定已由其他操作更新，請重新整理後再儲存。",
                    status_code=409,
                )
            try:
                normalized = service.normalize_settings(
                    payload,
                    known_targets=self._groups,
                    existing_api_key=str(self._settings.get("api_key") or ""),
                )
                for subscription in normalized["subscriptions"]:
                    already_enabled = any(
                        item["target_umo"] == subscription["target_umo"]
                        and item["enabled"]
                        for item in self._settings["subscriptions"]
                    )
                    target_error = self._validate_target_umo(
                        subscription["target_umo"],
                        require_available=subscription["enabled"]
                        and not already_enabled,
                    )
                    if target_error:
                        raise service.ValidationError(target_error)
            except service.ValidationError as exc:
                return error_response(str(exc))
            normalized["revision"] = current_revision + 1
            if normalized["source_mode"] != self._settings.get("source_mode"):
                self._source_state["force_alert_baseline"] = True
            self._settings = normalized
            self._client_instance().api_key = service.effective_api_key(normalized)
            await self.put_kv_data(SETTINGS_KEY, normalized)
            await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
        return json_response(
            {
                "ok": True,
                "revision": normalized["revision"],
                "message": "設定已儲存。",
            }
        )

    async def page_test_source(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        source = str(payload.get("source") or "current").lower()
        if source == "current":
            source = service.select_data_source(
                self._settings, self._source_state
            ).active_source
        if source not in {"api", "rss"}:
            return json_response(
                {"ok": False, "source": source, "message": "目前沒有可測試的資料來源。"}
            )
        started = time.perf_counter()
        try:
            if source == "api":
                result = await self._client_instance().test_api()
            else:
                result = await self._client_instance().test_rss()
            result = dict(result)
            result["latency_ms"] = round((time.perf_counter() - started) * 1000)
            result["item_count"] = result.get("count", 0)
            return json_response(result)
        except Exception as exc:
            return json_response(
                {
                    "ok": False,
                    "source": source,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    "message": self._safe_error(exc),
                }
            )

    async def page_preview(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        kind = str(payload.get("kind") or "alerts").lower()
        source = str(payload.get("source") or "current").lower()
        if source == "current":
            source = service.select_data_source(
                self._settings, self._source_state
            ).active_source
        generated_at = datetime.now(service.TAIPEI_TIMEZONE).isoformat(
            timespec="seconds"
        )
        try:
            if kind == "weather":
                city = service.normalize_city(payload.get("city"))
                forecasts = await self._fetch_weather_source(source, [city])
                messages = [
                    service.format_weather_forecast(forecast) for forecast in forecasts
                ]
            elif kind == "alerts":
                events = await self._fetch_alert_source(source)
                messages = [service.format_alert_event(event) for event in events[:5]]
            else:
                raise service.ValidationError("預覽類型只支援天氣或警報。")
            return json_response(
                {
                    "ok": True,
                    "source": source,
                    "generated_at": generated_at,
                    "messages": messages,
                }
            )
        except Exception as exc:
            return json_response(
                {
                    "ok": False,
                    "source": source,
                    "generated_at": generated_at,
                    "message": self._safe_error(exc),
                    "messages": [],
                }
            )

    async def page_test_send(self):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        target_id = str(payload.get("target_id") or "")
        if len(target_id) > 128:
            return error_response("群組目標識別碼格式過長。")
        target = next((item for item in self._groups if item["id"] == target_id), None)
        if not target:
            return error_response("找不到指定的已登記群組。", status_code=404)
        succeeded, error = await self._deliver_text(
            uuid4().hex,
            target["target_umo"],
            "【中央氣象署播報測試】\n此訊息由外掛管理頁主動傳送。",
            "群組測試",
            kind="test",
        )
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)
        if not succeeded:
            return json_response({"ok": False, "message": error or "傳送失敗。"})
        sent_at = datetime.now(service.TAIPEI_TIMEZONE).isoformat(timespec="seconds")
        target["tested_at"] = sent_at
        await self.put_kv_data(GROUPS_KEY, self._groups)
        return json_response(
            {"ok": True, "message": "測試訊息已交由平台介面傳送。", "sent_at": sent_at}
        )

    async def page_delete_target(self, target_id: str | None = None):
        payload = await request.json(default={})
        payload = payload if isinstance(payload, Mapping) else {}
        target_id = str(target_id or payload.get("target_id") or "")
        if len(target_id) > 128:
            return error_response("群組目標識別碼格式過長。")
        try:
            revision = int(payload.get("revision", -1))
        except (TypeError, ValueError):
            return error_response("設定版本格式無效。")
        async with self._state_lock:
            if revision != int(self._settings.get("revision", 0)):
                return error_response(
                    "設定已由其他操作更新，請重新整理後再操作。",
                    status_code=409,
                )
            target = next(
                (item for item in self._groups if item["id"] == target_id), None
            )
            if not target:
                return error_response("找不到指定的已登記群組。", status_code=404)
            self._groups = [item for item in self._groups if item["id"] != target_id]
            self._settings["subscriptions"] = [
                item
                for item in self._settings.get("subscriptions", [])
                if item.get("target_umo") != target["target_umo"]
            ]
            self._settings["revision"] = revision + 1
            await self.put_kv_data(GROUPS_KEY, self._groups)
            await self.put_kv_data(SETTINGS_KEY, self._settings)
        return json_response(
            {
                "ok": True,
                "revision": self._settings["revision"],
                "message": "已移除群組及其播報規則。",
            }
        )

    def _target_public_state(self, target: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(target)
        error = self._validate_target_umo(str(target.get("target_umo") or ""))
        result["available"] = error is None
        result["availability_message"] = error or "平台已載入並支援主動訊息。"
        return result

    def _event_is_recent(
        self,
        event: AlertEvent,
        *,
        minutes: int | None = None,
        hours: int | None = None,
    ) -> bool:
        timestamp = self._parse_datetime(event.published_at or event.effective_at)
        if timestamp is None:
            return False
        maximum_age = (
            timedelta(minutes=minutes)
            if minutes is not None
            else timedelta(hours=hours or 0)
        )
        age = datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)
        earliest_age = -maximum_age if minutes is not None else timedelta(minutes=-5)
        return earliest_age <= age <= maximum_age

    def _event_is_expired(self, event: AlertEvent) -> bool:
        expires = self._parse_datetime(event.expires_at)
        return bool(
            expires and expires.astimezone(timezone.utc) < datetime.now(timezone.utc)
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
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
            parsed = parsed.replace(tzinfo=service.TAIPEI_TIMEZONE)
        return parsed

    def _safe_error(self, exc: BaseException) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        api_key = service.effective_api_key(self._settings)
        if api_key:
            message = message.replace(api_key, "***")
        if "Authorization=" in message:
            message = message.split("Authorization=", 1)[0].rstrip("?& ")
        return message[:500]


__all__ = ["CWAAlertPlugin"]
