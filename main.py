from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.platform import MessageType
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

from . import service
from .cwa_client import (
    CATEGORY_LOCAL_EARTHQUAKE,
    CATEGORY_SIGNIFICANT_EARTHQUAKE,
    AlertEvent,
    CWAAlertBatchError,
    CWAClient,
    CWAClientError,
    WeatherForecast,
)

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_cwa_alert"
SETTINGS_KEY = "settings_v1"
GROUPS_KEY = "known_groups_v1"
SOURCE_STATE_KEY = "source_state_v1"
SEEN_EVENTS_KEY = "seen_events_v1"
DELIVERY_STATUS_KEY = "delivery_status_v1"


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
            for pattern in ("*.png", "*.part"):
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
        self._prune_pending_typhoon_images()
        await self.put_kv_data(SOURCE_STATE_KEY, self._source_state)
        await self.put_kv_data(SEEN_EVENTS_KEY, self._seen_events)
        await self.put_kv_data(DELIVERY_STATUS_KEY, self._delivery_status)

    async def _poll_loop(self) -> None:
        while True:
            try:
                if self._settings.get("enabled"):
                    await self._retry_pending_typhoon_images()
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

            matching = service.filter_subscriptions(subscriptions, event)
            delivered_targets: list[str] = []
            message = service.format_alert_event(event)
            for subscription in matching:
                target_umo = str(subscription.get("target_umo") or "")
                if service.delivery_succeeded(
                    self._delivery_status, fingerprint, target_umo, "text"
                ):
                    delivered_targets.append(target_umo)
                    continue
                succeeded, error = await self._send_text(target_umo, message)
                self._delivery_status = service.mark_delivery_status(
                    self._delivery_status,
                    event_fingerprint_value=fingerprint,
                    target_umo=target_umo,
                    part="text",
                    succeeded=succeeded,
                    error=error,
                )
                if succeeded:
                    delivered_targets.append(target_umo)

            if service.should_download_typhoon_bulletin(event) and delivered_targets:
                await self._dispatch_typhoon_image(fingerprint, delivered_targets)

    async def _dispatch_typhoon_image(
        self,
        fingerprint: str,
        target_umos: Sequence[str],
    ) -> None:
        pending_state = self._source_state.get("pending_typhoon_images")
        if not isinstance(pending_state, dict):
            pending_state = {}
            self._source_state["pending_typhoon_images"] = pending_state
        stored_pending_record = pending_state.get(fingerprint)
        pending_record = (
            dict(stored_pending_record)
            if isinstance(stored_pending_record, Mapping)
            else {}
        )
        stored_targets = pending_record.get("target_umos")
        known_targets = list(
            dict.fromkeys(
                [
                    str(target)
                    for target in (
                        list(stored_targets)
                        if isinstance(stored_targets, Sequence)
                        and not isinstance(stored_targets, (str, bytes))
                        else []
                    )
                    if str(target)
                ]
                + [str(target) for target in target_umos if str(target)]
            )
        )
        pending_record["target_umos"] = known_targets
        pending = [
            target
            for target in known_targets
            if not service.delivery_succeeded(
                self._delivery_status, fingerprint, target, "image"
            )
        ]
        if not pending:
            pending_state.pop(fingerprint, None)
            return
        if pending_record.get("expired_at"):
            return
        now = datetime.now(service.TAIPEI_TIMEZONE)
        first_attempt = self._parse_datetime(pending_record.get("first_attempt_at"))
        if first_attempt and now - first_attempt > timedelta(minutes=10):
            pending_record["expired_at"] = now.isoformat(timespec="seconds")
            pending_record["last_error"] = "警報單圖片在十分鐘內仍未更新或完成傳送。"
            pending_state[fingerprint] = pending_record
            with suppress(OSError):
                (self._temp_directory() / f"typhoon_{fingerprint[:16]}.png").unlink(
                    missing_ok=True
                )
            return
        if not first_attempt:
            pending_record["first_attempt_at"] = now.isoformat(timespec="seconds")

        temp_dir = self._temp_directory()
        temp_path = temp_dir / f"typhoon_{fingerprint[:16]}.png"
        remove_temp = False
        try:
            image = await self._client_instance().download_typhoon_bulletin(
                temp_path,
                cache_token=str(int(time.time())),
            )
            previous_hash = str(self._source_state.get("last_typhoon_image_hash") or "")
            event_hash = str(pending_record.get("image_hash") or "")
            if (
                previous_hash
                and image.sha256 == previous_hash
                and event_hash != image.sha256
            ):
                pending_record["last_error"] = "中央氣象署警報單圖片尚未更新。"
                pending_record["last_attempt_at"] = now.isoformat(timespec="seconds")
                pending_state[fingerprint] = pending_record
                remove_temp = True
                return

            pending_record["image_hash"] = image.sha256
            pending_record["last_attempt_at"] = now.isoformat(timespec="seconds")
            all_succeeded = True
            any_succeeded = False
            for target_umo in pending:
                succeeded, error = await self._send_image(target_umo, image.path)
                self._delivery_status = service.mark_delivery_status(
                    self._delivery_status,
                    event_fingerprint_value=fingerprint,
                    target_umo=target_umo,
                    part="image",
                    succeeded=succeeded,
                    error=error,
                )
                all_succeeded = all_succeeded and succeeded
                any_succeeded = any_succeeded or succeeded
            if any_succeeded:
                self._source_state["last_typhoon_image_hash"] = image.sha256
            if all_succeeded:
                pending_state.pop(fingerprint, None)
                remove_temp = True
            else:
                pending_record["last_error"] = (
                    "部分群組的警報單圖片傳送失敗，等待重試。"
                )
                pending_state[fingerprint] = pending_record
        except Exception as exc:
            error = self._safe_error(exc)
            pending_record["last_error"] = error
            pending_record["last_attempt_at"] = now.isoformat(timespec="seconds")
            pending_state[fingerprint] = pending_record
            for target_umo in pending:
                self._delivery_status = service.mark_delivery_status(
                    self._delivery_status,
                    event_fingerprint_value=fingerprint,
                    target_umo=target_umo,
                    part="image",
                    succeeded=False,
                    error=error,
                )
        finally:
            if remove_temp:
                with suppress(OSError):
                    temp_path.unlink(missing_ok=True)

    async def _retry_pending_typhoon_images(self) -> None:
        pending_state = self._source_state.get("pending_typhoon_images")
        if not isinstance(pending_state, Mapping):
            self._source_state["pending_typhoon_images"] = {}
            return
        now = datetime.now(service.TAIPEI_TIMEZONE)
        for fingerprint, stored_record in list(pending_state.items()):
            if not isinstance(stored_record, Mapping) or stored_record.get(
                "expired_at"
            ):
                continue
            last_attempt = self._parse_datetime(stored_record.get("last_attempt_at"))
            if last_attempt and now - last_attempt < timedelta(seconds=60):
                continue
            target_umos = stored_record.get("target_umos")
            if not isinstance(target_umos, Sequence) or isinstance(
                target_umos, (str, bytes)
            ):
                continue
            await self._dispatch_typhoon_image(
                str(fingerprint), [str(target) for target in target_umos]
            )
        self._prune_pending_typhoon_images(now=now)

    def _prune_pending_typhoon_images(self, *, now: datetime | None = None) -> None:
        pending_state = self._source_state.get("pending_typhoon_images")
        if not isinstance(pending_state, Mapping):
            self._source_state["pending_typhoon_images"] = {}
            return
        current = now or datetime.now(service.TAIPEI_TIMEZONE)
        cutoff = current - timedelta(days=service.STATE_RETENTION_DAYS)
        retained: list[tuple[datetime, str, dict[str, Any]]] = []
        removed: set[str] = set()
        for fingerprint, stored_record in pending_state.items():
            if not isinstance(stored_record, Mapping):
                removed.add(str(fingerprint))
                continue
            record = dict(stored_record)
            timestamp = (
                self._parse_datetime(record.get("expired_at"))
                or self._parse_datetime(record.get("last_attempt_at"))
                or self._parse_datetime(record.get("first_attempt_at"))
                or current
            )
            if timestamp.astimezone(service.TAIPEI_TIMEZONE) < cutoff:
                removed.add(str(fingerprint))
                continue
            retained.append((timestamp, str(fingerprint), record))
        retained.sort(key=lambda item: item[0], reverse=True)
        for _, fingerprint, _ in retained[service.STATE_MAX_ENTRIES :]:
            removed.add(fingerprint)
        self._source_state["pending_typhoon_images"] = {
            fingerprint: record
            for _, fingerprint, record in retained[: service.STATE_MAX_ENTRIES]
        }
        for fingerprint in removed:
            with suppress(OSError):
                (self._temp_directory() / f"typhoon_{fingerprint[:16]}.png").unlink(
                    missing_ok=True
                )

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
            succeeded, error = await self._send_text(
                str(subscription.get("target_umo") or ""), text
            )
            self._delivery_status = service.mark_delivery_status(
                self._delivery_status,
                event_fingerprint_value=slot.key,
                target_umo=str(subscription.get("target_umo") or ""),
                part="text",
                succeeded=succeeded,
                error=error,
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

    async def _send_text(self, target_umo: str, text: str) -> tuple[bool, str | None]:
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            return False, validation_error
        try:
            chunks = service.split_message(text)
            for index, chunk in enumerate(chunks):
                sent = await self.context.send_message(
                    target_umo, MessageChain().message(chunk).use_t2i(False)
                )
                if not sent:
                    return False, "找不到目標所屬的平台實例。"
                if index < len(chunks) - 1:
                    await asyncio.sleep(0.2)
            return True, None
        except Exception as exc:
            return False, self._safe_error(exc)

    async def _send_image(self, target_umo: str, path: Path) -> tuple[bool, str | None]:
        validation_error = self._validate_target_umo(target_umo)
        if validation_error:
            return False, validation_error
        try:
            sent = await self.context.send_message(
                target_umo, MessageChain().file_image(str(path)).use_t2i(False)
            )
            return (True, None) if sent else (False, "找不到目標所屬的平台實例。")
        except Exception as exc:
            return False, self._safe_error(exc)

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
                self._groups = [
                    item for item in self._groups if item["target_umo"] != target_umo
                ]
                self._groups.append(target)
                await self.put_kv_data(GROUPS_KEY, self._groups)
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
                f"已登記群組「{target['display_name']}」。請前往外掛管理頁建立播報規則。{suffix}"
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
        succeeded, error = await self._send_text(
            target["target_umo"],
            "【中央氣象署播報測試】\n主動訊息傳送已交由 AstrBot 平台介面處理。",
        )
        if not succeeded:
            await event.send(MessageChain().message(f"測試失敗：{error}"))
            return
        target["tested_at"] = datetime.now(service.TAIPEI_TIMEZONE).isoformat(
            timespec="seconds"
        )
        await self.put_kv_data(GROUPS_KEY, self._groups)

    async def page_state(self):
        targets = [self._target_public_state(target) for target in self._groups]
        deliveries = sorted(
            (
                dict(record)
                for record in self._delivery_status.values()
                if isinstance(record, Mapping)
            ),
            key=lambda item: str(
                item.get("last_attempt_at") or item.get("delivered_at") or ""
            ),
            reverse=True,
        )[:100]
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
                "started_at": self._started_at.isoformat(timespec="seconds"),
            }
        )

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
                    target_error = self._validate_target_umo(
                        subscription["target_umo"],
                        require_available=bool(subscription["enabled"]),
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
        succeeded, error = await self._send_text(
            target["target_umo"],
            "【中央氣象署播報測試】\n此訊息由外掛管理頁主動傳送。",
        )
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
