"""Offline integration tests for the AstrBot-facing plugin layer."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
TEST_PACKAGE = "cwa_alert_main_under_test"


class StubMessageType:
    """Provide the message types used by the plugin."""

    GROUP_MESSAGE = "group"
    FRIEND_MESSAGE = "private"


class StubMessageSession:
    """Parse the compact UMO representation used by these tests."""

    def __init__(self, platform_id: str, message_type: str, session_id: str) -> None:
        self.platform_id = platform_id
        self.message_type = message_type
        self.session_id = session_id

    @classmethod
    def from_str(cls, value: str) -> StubMessageSession:
        """Parse ``platform:group|private:session``.

        Args:
            value: Test UMO string.

        Returns:
            Parsed test session.

        Raises:
            ValueError: The UMO is malformed or uses an unknown scene type.
        """

        parts = str(value).split(":", 2)
        if len(parts) != 3 or not all(parts):
            raise ValueError("invalid UMO")
        platform_id, scene, session_id = parts
        if scene == "group":
            message_type = StubMessageType.GROUP_MESSAGE
        elif scene == "private":
            message_type = StubMessageType.FRIEND_MESSAGE
        else:
            raise ValueError("invalid UMO scene")
        return cls(platform_id, message_type, session_id)


class StubMessageChain:
    """Capture text and image operations without loading AstrBot components."""

    def __init__(self) -> None:
        self.parts: list[tuple[str, str]] = []
        self.t2i_enabled: bool | None = None

    def message(self, text: str) -> StubMessageChain:
        """Append a text component and return this chain."""

        self.parts.append(("text", str(text)))
        return self

    def file_image(self, path: str) -> StubMessageChain:
        """Append an image component and return this chain."""

        self.parts.append(("image", str(path)))
        return self

    def use_t2i(self, enabled: bool) -> StubMessageChain:
        """Record the text-to-image choice and return this chain."""

        self.t2i_enabled = bool(enabled)
        return self


@dataclass(slots=True)
class StubWebResponse:
    """Represent the payload and status returned by a Plugin Page route."""

    body: dict[str, Any]
    status_code: int = 200


class StubRequest:
    """Provide mutable JSON input for Plugin Page route tests."""

    def __init__(self) -> None:
        self.payload: Any = {}

    async def json(self, default: Any = None) -> Any:
        """Return a deep copy of the configured request payload."""

        if self.payload is None:
            return copy.deepcopy(default)
        return copy.deepcopy(self.payload)


STUB_REQUEST = StubRequest()


def _json_response(payload: dict[str, Any], status_code: int = 200) -> StubWebResponse:
    """Create a successful stub Web response."""

    return StubWebResponse(dict(payload), status_code)


def _error_response(message: str, status_code: int = 400) -> StubWebResponse:
    """Create an error stub Web response."""

    return StubWebResponse({"ok": False, "message": str(message)}, status_code)


class StubFilter:
    """Return identity decorators for AstrBot command metadata."""

    PermissionType = SimpleNamespace(ADMIN="admin")

    @staticmethod
    def command(name: str):
        """Return an identity command decorator."""

        del name
        return lambda function: function

    @staticmethod
    def permission_type(permission: str):
        """Return an identity permission decorator."""

        del permission
        return lambda function: function


class StubStar:
    """Supply the small Star persistence surface used by the plugin."""

    def __init__(self, context: Any, config: dict[str, Any] | None = None) -> None:
        self.context = context
        self.config = config or {}
        self._kv: dict[str, Any] = {}

    async def get_kv_data(self, key: str, default: Any = None) -> Any:
        """Read one deep-copied in-memory KV value."""

        return copy.deepcopy(self._kv.get(key, default))

    async def put_kv_data(self, key: str, value: Any) -> None:
        """Persist one deep-copied in-memory KV value."""

        self._kv[key] = copy.deepcopy(value)


class StubPlatform:
    """Expose AstrBot platform metadata used for proactive-message checks."""

    def __init__(self, name: str, *, proactive: bool = True) -> None:
        self._metadata = SimpleNamespace(
            name=name,
            support_proactive_message=proactive,
        )

    def meta(self) -> Any:
        """Return immutable-like platform metadata."""

        return self._metadata


class StubContext:
    """Capture registered routes and proactive sends."""

    def __init__(self) -> None:
        self.platforms: dict[str, StubPlatform] = {}
        self.routes: list[tuple[Any, ...]] = []
        self.sent: list[tuple[str, StubMessageChain]] = []
        self.send_results: list[bool | BaseException] = []

    def register_web_api(self, *args: Any) -> None:
        """Record one Plugin Page route registration."""

        self.routes.append(args)

    def get_platform_inst(self, platform_id: str) -> StubPlatform | None:
        """Return one configured platform instance."""

        return self.platforms.get(platform_id)

    async def send_message(self, target_umo: str, chain: StubMessageChain) -> bool:
        """Record one proactive send and return its queued result."""

        self.sent.append((target_umo, chain))
        if self.send_results:
            result = self.send_results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return True


class StubEvent:
    """Represent an AstrBot message event for command tests."""

    def __init__(
        self,
        umo: str,
        *,
        message_type: str = StubMessageType.GROUP_MESSAGE,
        group_name: str = "測試群組",
    ) -> None:
        self.unified_msg_origin = umo
        self._message_type = message_type
        self._group_id = umo.rsplit(":", 1)[-1]
        self.message_obj = SimpleNamespace(group=SimpleNamespace(group_name=group_name))
        self.sent: list[StubMessageChain] = []

    def get_message_type(self) -> str:
        """Return the configured message scene type."""

        return self._message_type

    def get_group_id(self) -> str:
        """Return the configured group identifier."""

        return self._group_id

    async def send(self, chain: StubMessageChain) -> None:
        """Capture one command reply."""

        self.sent.append(chain)

    def last_text(self) -> str:
        """Return the last text component sent by the command."""

        if not self.sent:
            return ""
        return "\n".join(value for kind, value in self.sent[-1].parts if kind == "text")


def _install_astrbot_stubs() -> dict[str, types.ModuleType]:
    """Build the module graph imported by ``main.py``.

    Returns:
        Mapping of fully qualified module names to stub modules.
    """

    modules: dict[str, types.ModuleType] = {}
    for name in (
        "astrbot",
        "astrbot.api",
        "astrbot.api.star",
        "astrbot.api.event",
        "astrbot.api.platform",
        "astrbot.api.web",
        "astrbot.core",
        "astrbot.core.message",
        "astrbot.core.message.message_event_result",
        "astrbot.core.platform",
        "astrbot.core.platform.message_session",
        "astrbot.core.utils",
        "astrbot.core.utils.astrbot_path",
    ):
        module = types.ModuleType(name)
        if name in {
            "astrbot",
            "astrbot.api",
            "astrbot.core",
            "astrbot.core.message",
            "astrbot.core.platform",
            "astrbot.core.utils",
        }:
            module.__path__ = []  # type: ignore[attr-defined]
        modules[name] = module

    modules["astrbot.api.star"].Star = StubStar
    modules["astrbot.api.star"].Context = StubContext
    modules["astrbot.api"].star = modules["astrbot.api.star"]
    modules["astrbot.api.event"].AstrMessageEvent = StubEvent
    modules["astrbot.api.event"].filter = StubFilter()
    modules["astrbot.api.platform"].MessageType = StubMessageType
    modules["astrbot.api.web"].json_response = _json_response
    modules["astrbot.api.web"].error_response = _error_response
    modules["astrbot.api.web"].request = STUB_REQUEST
    modules["astrbot.core.message.message_event_result"].MessageChain = StubMessageChain
    modules["astrbot.core.platform.message_session"].MessageSession = StubMessageSession
    modules[
        "astrbot.core.utils.astrbot_path"
    ].get_astrbot_temp_path = tempfile.gettempdir
    return modules


def _load_main_module() -> types.ModuleType:
    """Load the plugin under a private package while AstrBot is stubbed."""

    package = types.ModuleType(TEST_PACKAGE)
    package.__path__ = [str(PLUGIN_ROOT)]  # type: ignore[attr-defined]
    module_name = f"{TEST_PACKAGE}.main"
    main_spec = importlib.util.spec_from_file_location(
        module_name, PLUGIN_ROOT / "main.py"
    )
    assert main_spec is not None and main_spec.loader is not None
    main_module = importlib.util.module_from_spec(main_spec)

    stubs = _install_astrbot_stubs()
    touched_names = (*stubs, TEST_PACKAGE, module_name)
    previous = {name: sys.modules.get(name) for name in touched_names}
    try:
        sys.modules.update(stubs)
        sys.modules[TEST_PACKAGE] = package
        sys.modules[module_name] = main_module
        main_spec.loader.exec_module(main_module)
    finally:
        for name, prior in previous.items():
            if prior is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior
    return main_module


main = _load_main_module()
service = main.service


def _subscription(target_umo: str, *alert_types: str) -> dict[str, Any]:
    """Create one normalized enabled test subscription."""

    return service.normalize_subscription(
        {
            "target_umo": target_umo,
            "cities": ["臺北市"],
            "forecast_enabled": False,
            "alert_types": list(alert_types or ("typhoon",)),
        }
    )


def _alert(
    *,
    category: str = "typhoon",
    title: str = "海上颱風警報",
    source: str = "api",
    official_id: str = "alert-1",
) -> Any:
    """Create one current normalized alert event."""

    timestamp = datetime.now(timezone.utc).isoformat()
    return main.AlertEvent(
        source=source,
        category=category,
        title=title,
        description="官方測試警報內容。",
        published_at=timestamp,
        effective_at=timestamp,
        expires_at="",
        areas=("臺北市",),
        link="https://www.cwa.gov.tw/",
        official_id=official_id,
        bulletin_no="1",
        is_cancellation=False,
    )


def _forecast(city: str, *, source: str = "rss") -> Any:
    """Create one minimal normalized weather forecast."""

    return main.WeatherForecast(
        source=source,
        city=city,
        published_at=datetime.now(timezone.utc).isoformat(),
        periods=(),
        link="https://www.cwa.gov.tw/",
        official_id=f"forecast-{city}",
    )


class LifecycleClient:
    """Track client lifecycle without opening a network session."""

    instances: list[LifecycleClient] = []

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self.closed = False
        self.__class__.instances.append(self)

    async def close(self) -> None:
        """Record client closure."""

        self.closed = True


class SourceClient:
    """Provide deterministic API failures and queued RSS results."""

    def __init__(self) -> None:
        self.api_calls = 0
        self.rss_calls = 0
        self.rss_results: list[Sequence[Any] | BaseException] = []

    async def fetch_api_alerts(self) -> Sequence[Any]:
        """Always fail the API request."""

        self.api_calls += 1
        raise main.CWAClientError("API 測試失敗")

    async def fetch_rss_alerts(self) -> Sequence[Any]:
        """Return or raise the next queued RSS result."""

        self.rss_calls += 1
        value = self.rss_results.pop(0) if self.rss_results else []
        if isinstance(value, BaseException):
            raise value
        return value


class BulletinClient:
    """Return a stable image hash or fail before writing."""

    def __init__(self, *, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def download_typhoon_bulletin(
        self,
        destination: Path,
        *,
        cache_token: str | None = None,
    ) -> Any:
        """Return one deterministic image result."""

        del cache_token
        self.calls += 1
        if self.error is not None:
            raise self.error
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"test-image")
        return SimpleNamespace(path=destination, sha256="stable-image-hash")


class LifecycleAndRegistrationTests(unittest.IsolatedAsyncioTestCase):
    """Validate task lifetime and explicit group registration rules."""

    async def test_initialize_keeps_one_poll_task_and_terminate_cancels_it(
        self,
    ) -> None:
        context = StubContext()
        LifecycleClient.instances = []
        with patch.object(main, "CWAClient", LifecycleClient):
            plugin = main.CWAAlertPlugin(context)
            await plugin.initialize()
            first_task = plugin._poll_task
            await plugin.initialize()
            second_task = plugin._poll_task

            self.assertIs(first_task, second_task)
            self.assertIsNotNone(first_task)
            active_pollers = [
                task
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name() == "astrbot_plugin_cwa_alert:poller"
                and not task.done()
            ]
            self.assertEqual(len(active_pollers), 1)

            await plugin.terminate()
            self.assertTrue(first_task.done())
            self.assertIsNone(plugin._poll_task)
            self.assertTrue(LifecycleClient.instances[-1].closed)

    async def test_umo_validation_rejects_invalid_private_missing_and_passive(
        self,
    ) -> None:
        context = StubContext()
        context.platforms["ok"] = StubPlatform("aiocqhttp", proactive=True)
        context.platforms["passive"] = StubPlatform("weixin", proactive=False)
        plugin = main.CWAAlertPlugin(context)

        self.assertIsNone(plugin._validate_target_umo("ok:group:100"))
        self.assertIn("格式無效", plugin._validate_target_umo("broken") or "")
        self.assertIn("只支援群組", plugin._validate_target_umo("ok:private:100") or "")
        self.assertIn(
            "目前未載入", plugin._validate_target_umo("missing:group:100") or ""
        )
        self.assertIn(
            "不支援主動訊息", plugin._validate_target_umo("passive:group:100") or ""
        )

    async def test_register_rejects_private_and_second_wecom_webhook_target(
        self,
    ) -> None:
        context = StubContext()
        context.platforms["wecom"] = StubPlatform("wecom_ai_bot", proactive=True)
        plugin = main.CWAAlertPlugin(context)

        private_event = StubEvent(
            "wecom:private:user",
            message_type=StubMessageType.FRIEND_MESSAGE,
        )
        await plugin.cwa_register(private_event)
        self.assertIn("只能在群組", private_event.last_text())

        first = StubEvent("wecom:group:one", group_name="第一群")
        second = StubEvent("wecom:group:two", group_name="第二群")
        await plugin.cwa_register(first)
        await plugin.cwa_register(second)

        self.assertEqual(len(plugin._groups), 1)
        self.assertEqual(plugin._groups[0]["display_name"], "第一群")
        self.assertIn("只能登記一個目標", second.last_text())

    async def test_unregister_preserves_and_disables_subscription(self) -> None:
        context = StubContext()
        context.platforms["qq"] = StubPlatform("aiocqhttp", proactive=True)
        plugin = main.CWAAlertPlugin(context)
        event = StubEvent("qq:group:weather", group_name="氣象群組")
        await plugin.cwa_register(event)
        plugin._settings["subscriptions"] = [
            _subscription(event.unified_msg_origin, "rain")
        ]

        await plugin.cwa_unregister(event)

        self.assertEqual(plugin._groups, [])
        self.assertEqual(len(plugin._settings["subscriptions"]), 1)
        self.assertFalse(plugin._settings["subscriptions"][0]["enabled"])
        self.assertEqual(plugin._settings["revision"], 1)
        self.assertIn("規則已停用", event.last_text())

    async def test_status_reports_mode_and_both_source_health_states(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update({"source_mode": "auto", "api_key": "test-key"})
        plugin._source_state.update(
            {
                "api_available": False,
                "rss_available": True,
                "api_consecutive_failures": 2,
            }
        )
        event = StubEvent("qq:group:100")

        await plugin.cwa_status(event)

        text = event.last_text()
        self.assertIn("資料模式：自動", text)
        self.assertIn("API 健康：異常（連續失敗 2 次）", text)
        self.assertIn("RSS 健康：正常", text)


class SourceHealthAndBaselineTests(unittest.IsolatedAsyncioTestCase):
    """Validate health transitions and source-specific baselines."""

    async def test_three_api_failures_degrade_to_rss(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update({"source_mode": "auto", "api_key": "test-key"})
        client = SourceClient()
        client.rss_results = [[]]
        plugin._client = client

        for _ in range(2):
            with self.assertRaises(main.CWAClientError):
                await plugin._poll_alerts()
        await plugin._poll_alerts()

        self.assertEqual(client.api_calls, 3)
        self.assertEqual(client.rss_calls, 1)
        self.assertEqual(plugin._source_state["api_consecutive_failures"], 3)
        self.assertEqual(plugin._source_state["active_source"], "rss")
        self.assertTrue(plugin._source_state["is_fallback"])
        self.assertTrue(plugin._source_state["rss_available"])

    async def test_rss_failure_updates_health_state(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["source_mode"] = "rss"
        client = SourceClient()
        client.rss_results = [main.CWAClientError("RSS 測試失敗")]
        plugin._client = client

        with self.assertRaises(main.CWAClientError):
            await plugin._poll_alerts()

        self.assertFalse(plugin._source_state["rss_available"])
        self.assertEqual(plugin._source_state["last_rss_error"], "RSS 測試失敗")
        self.assertIn(main.SOURCE_STATE_KEY, plugin._kv)

    async def test_rss_failure_during_auto_fallback_updates_health_state(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update({"source_mode": "auto", "api_key": "test-key"})
        plugin._source_state.update(
            {
                "active_source": "rss",
                "is_fallback": True,
                "api_consecutive_failures": 3,
                "rss_available": True,
            }
        )
        client = SourceClient()
        client.rss_results = [main.CWAClientError("降級 RSS 失敗")]
        plugin._client = client

        with self.assertRaises(main.CWAClientError):
            await plugin._poll_alerts()

        self.assertFalse(plugin._source_state["rss_available"])
        self.assertEqual(plugin._source_state["last_rss_error"], "降級 RSS 失敗")
        self.assertIn(main.SOURCE_STATE_KEY, plugin._kv)

    async def test_switch_to_rss_baselines_existing_then_sends_only_new_item(
        self,
    ) -> None:
        target = "qq:group:100"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update(
            {
                "source_mode": "rss",
                "subscriptions": [_subscription(target, "rain")],
            }
        )
        plugin._source_state["active_source"] = "api"
        existing = _alert(
            category="rain",
            title="大雨特報",
            source="rss",
            official_id="rain-existing",
        )
        new_item = _alert(
            category="rain",
            title="豪雨特報",
            source="rss",
            official_id="rain-new",
        )
        client = SourceClient()
        client.rss_results = [[existing], [existing, new_item]]
        plugin._client = client
        plugin._send_text = AsyncMock(return_value=(True, None))

        await plugin._poll_alerts()
        self.assertEqual(plugin._send_text.await_count, 0)
        self.assertTrue(plugin._source_state["rss_alerts_baselined"])

        await plugin._poll_alerts()
        self.assertEqual(plugin._send_text.await_count, 1)
        sent_text = plugin._send_text.await_args.args[1]
        self.assertIn("豪雨特報", sent_text)
        self.assertNotIn("大雨特報", sent_text)

    async def test_partial_api_batch_delivers_successes_and_keeps_failure_state(
        self,
    ) -> None:
        target = "qq:group:100"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update(
            {
                "source_mode": "auto",
                "api_key": "test-key",
                "subscriptions": [_subscription(target, "rain")],
            }
        )
        available = _alert(
            category="rain",
            title="豪雨特報",
            source="api",
            official_id="rain-partial",
        )
        batch_error = main.CWAAlertBatchError(
            (available,),
            (("W-C0033-004", main.CWAClientError("低溫資料集失敗")),),
        )
        plugin._client = SimpleNamespace(
            api_key="",
            fetch_api_alerts=AsyncMock(side_effect=batch_error),
        )
        plugin._send_text = AsyncMock(return_value=(True, None))

        await plugin._poll_alerts()

        plugin._send_text.assert_awaited_once()
        self.assertEqual(plugin._source_state["api_consecutive_failures"], 1)
        self.assertFalse(plugin._source_state["api_available"])
        self.assertEqual(plugin._source_state["last_alert_source"], "api")

    async def test_third_partial_api_failure_delivers_partial_before_rss_baseline(
        self,
    ) -> None:
        target = "qq:group:100"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update(
            {
                "source_mode": "auto",
                "api_key": "test-key",
                "subscriptions": [_subscription(target, "rain")],
            }
        )
        plugin._source_state.update(
            {
                "api_consecutive_failures": 2,
                "active_source": "api",
                "last_alert_source": "api",
            }
        )
        partial = _alert(
            category="rain",
            title="API 新豪雨特報",
            source="api",
            official_id="api-partial",
        )
        current_rss = _alert(
            category="rain",
            title="RSS 既有大雨特報",
            source="rss",
            official_id="rss-existing",
        )
        batch_error = main.CWAAlertBatchError(
            (partial,),
            (("W-C0033-004", main.CWAClientError("低溫資料集失敗")),),
        )
        plugin._client = SimpleNamespace(
            api_key="",
            fetch_api_alerts=AsyncMock(side_effect=batch_error),
            fetch_rss_alerts=AsyncMock(return_value=(current_rss,)),
        )
        plugin._send_text = AsyncMock(return_value=(True, None))

        await plugin._poll_alerts()

        plugin._send_text.assert_awaited_once()
        self.assertIn("API 新豪雨特報", plugin._send_text.await_args.args[1])
        self.assertEqual(plugin._source_state["api_consecutive_failures"], 3)
        self.assertEqual(plugin._source_state["active_source"], "rss")
        self.assertTrue(plugin._source_state["rss_alerts_baselined"])

    async def test_rss_weather_fetches_cities_concurrently_and_keeps_successes(
        self,
    ) -> None:
        class ConcurrentWeatherClient:
            def __init__(self) -> None:
                self.api_key = ""
                self.started: set[str] = set()
                self.all_started = asyncio.Event()
                self.release = asyncio.Event()

            async def fetch_rss_weather(self, city: str) -> Any:
                self.started.add(city)
                if len(self.started) == 2:
                    self.all_started.set()
                await self.release.wait()
                if city == "臺北市":
                    raise main.CWAClientError("臺北市 RSS 暫時失敗")
                return _forecast(city)

        plugin = main.CWAAlertPlugin(StubContext())
        client = ConcurrentWeatherClient()
        plugin._client = client

        task = asyncio.create_task(
            plugin._fetch_weather_source("rss", ["臺北市", "新北市"])
        )
        await asyncio.wait_for(client.all_started.wait(), timeout=1)
        client.release.set()
        forecasts = await task

        self.assertEqual([forecast.city for forecast in forecasts], ["新北市"])
        self.assertEqual(
            plugin._source_state["last_weather_rss_city_errors"],
            {"臺北市": "臺北市 RSS 暫時失敗"},
        )

    async def test_weather_success_does_not_reset_alert_api_failures(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update({"source_mode": "auto", "api_key": "test-key"})
        plugin._source_state["api_consecutive_failures"] = 2
        plugin._source_state["api_available"] = False
        plugin._client = SimpleNamespace(
            api_key="",
            fetch_api_weather=AsyncMock(
                return_value=(_forecast("臺北市", source="api"),)
            ),
        )

        source, forecasts = await plugin._fetch_weather_with_fallback(["臺北市"])

        self.assertEqual(source, "api")
        self.assertEqual(len(forecasts), 1)
        self.assertEqual(plugin._source_state["api_consecutive_failures"], 2)
        self.assertFalse(plugin._source_state["api_available"])

    async def test_missing_api_city_uses_rss_for_the_weather_request(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings.update({"source_mode": "auto", "api_key": "test-key"})

        async def fetch_rss_weather(city: str) -> Any:
            return _forecast(city)

        plugin._client = SimpleNamespace(
            api_key="",
            fetch_api_weather=AsyncMock(
                return_value=(_forecast("臺北市", source="api"),)
            ),
            fetch_rss_weather=fetch_rss_weather,
        )

        source, forecasts = await plugin._fetch_weather_with_fallback(
            ["臺北市", "新北市"]
        )

        self.assertEqual(source, "rss")
        self.assertEqual(
            [forecast.city for forecast in forecasts], ["臺北市", "新北市"]
        )
        self.assertIn("新北市", plugin._source_state["last_weather_api_error"])

    async def test_failed_city_does_not_block_other_scheduled_group(self) -> None:
        successful_target = "qq:group:success"
        waiting_target = "qq:group:waiting"
        successful_rule = service.normalize_subscription(
            {
                "target_umo": successful_target,
                "cities": ["新北市"],
                "forecast_enabled": True,
                "forecast_times": ["07:30"],
                "alert_types": ["rain"],
            }
        )
        waiting_rule = service.normalize_subscription(
            {
                "target_umo": waiting_target,
                "cities": ["臺北市"],
                "forecast_enabled": True,
                "forecast_times": ["07:30"],
                "alert_types": ["rain"],
            }
        )
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["subscriptions"] = [successful_rule, waiting_rule]
        plugin._fetch_weather_with_fallback = AsyncMock(
            return_value=("rss", (_forecast("新北市"),))
        )
        plugin._send_text = AsyncMock(return_value=(True, None))

        def due_slots(subscription: dict[str, Any], sent: Any) -> list[Any]:
            del sent
            target = subscription["target_umo"]
            return [service.ForecastSlot("2026-07-12", "07:30", f"slot:{target}")]

        with patch.object(service, "due_forecast_slots", side_effect=due_slots):
            await plugin._send_due_forecasts()

        plugin._send_text.assert_awaited_once()
        self.assertEqual(plugin._send_text.await_args.args[0], successful_target)
        slots = plugin._source_state["forecast_slots"]
        self.assertIn(f"slot:{successful_target}", slots)
        self.assertNotIn(f"slot:{waiting_target}", slots)

    async def test_old_earthquake_is_not_replayed_to_new_subscription(self) -> None:
        target = "qq:group:100"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["subscriptions"] = [
            _subscription(target, "significant_earthquake")
        ]
        old_timestamp = (
            datetime.now(timezone.utc) - main.timedelta(minutes=10)
        ).isoformat()
        event = main.AlertEvent(
            source="api",
            category="significant_earthquake",
            title="顯著有感地震報告",
            description="官方地震報告內容。",
            published_at=old_timestamp,
            effective_at=old_timestamp,
            expires_at="",
            areas=("臺北市",),
            link="https://www.cwa.gov.tw/",
            official_id="old-earthquake",
            bulletin_no="",
            is_cancellation=False,
        )
        plugin._send_text = AsyncMock(return_value=(True, None))

        await plugin._dispatch_alert_events([event], baseline_only=False)

        plugin._send_text.assert_not_awaited()


class TyphoonDeliveryTests(unittest.IsolatedAsyncioTestCase):
    """Validate independent text and image delivery state."""

    async def test_text_success_is_retained_when_image_download_fails(self) -> None:
        target = "qq:group:100"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["subscriptions"] = [_subscription(target, "typhoon")]
        event = _alert()
        fingerprint = service.event_fingerprint(event)
        plugin._client = BulletinClient(error=main.CWAClientError("圖片下載失敗"))
        plugin._send_text = AsyncMock(return_value=(True, None))

        await plugin._dispatch_alert_events([event], baseline_only=False)

        self.assertTrue(
            service.delivery_succeeded(
                plugin._delivery_status,
                fingerprint,
                target,
                "text",
            )
        )
        self.assertFalse(
            service.delivery_succeeded(
                plugin._delivery_status,
                fingerprint,
                target,
                "image",
            )
        )
        image_key = service.delivery_key(fingerprint, target, "image")
        self.assertEqual(plugin._delivery_status[image_key]["error"], "圖片下載失敗")

    async def test_partial_image_success_retries_only_failed_target_with_same_image(
        self,
    ) -> None:
        first_target = "qq:group:first"
        second_target = "qq:group:second"
        plugin = main.CWAAlertPlugin(StubContext())
        client = BulletinClient()
        plugin._client = client
        event = _alert()
        fingerprint = service.event_fingerprint(event)
        attempts: list[str] = []

        async def send_image(target_umo: str, path: Path) -> tuple[bool, str | None]:
            del path
            attempts.append(target_umo)
            if target_umo == second_target and attempts.count(second_target) == 1:
                return False, "平台暫時失敗"
            return True, None

        plugin._send_image = send_image
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                plugin,
                "_temp_directory",
                return_value=Path(temp_dir),
            ):
                await plugin._dispatch_typhoon_image(
                    fingerprint,
                    [first_target, second_target],
                )
                await plugin._dispatch_typhoon_image(
                    fingerprint,
                    [first_target, second_target],
                )

        self.assertEqual(attempts, [first_target, second_target, second_target])
        self.assertTrue(
            service.delivery_succeeded(
                plugin._delivery_status,
                fingerprint,
                first_target,
                "image",
            )
        )
        self.assertTrue(
            service.delivery_succeeded(
                plugin._delivery_status,
                fingerprint,
                second_target,
                "image",
            )
        )

    async def test_pending_image_retries_without_waiting_for_next_alert_poll(
        self,
    ) -> None:
        target = "qq:group:retry"
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._client = BulletinClient()
        fingerprint = service.event_fingerprint(_alert())
        attempts = 0

        async def send_image(target_umo: str, path: Path) -> tuple[bool, str | None]:
            nonlocal attempts
            del target_umo, path
            attempts += 1
            if attempts == 1:
                return False, "平台暫時失敗"
            return True, None

        plugin._send_image = send_image
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.object(
                plugin,
                "_temp_directory",
                return_value=Path(temp_dir),
            ):
                await plugin._dispatch_typhoon_image(fingerprint, [target])
                pending = plugin._source_state["pending_typhoon_images"][fingerprint]
                pending["last_attempt_at"] = (
                    datetime.now(service.TAIPEI_TIMEZONE) - timedelta(seconds=61)
                ).isoformat()

                await plugin._retry_pending_typhoon_images()

        self.assertEqual(attempts, 2)
        self.assertNotIn(
            fingerprint, plugin._source_state.get("pending_typhoon_images", {})
        )


class PluginPageTests(unittest.IsolatedAsyncioTestCase):
    """Validate secret masking and optimistic concurrency responses."""

    async def test_page_state_masks_api_key_and_returns_only_suffix(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["api_key"] = "CWA-VERY-SECRET-1234"

        with patch.dict(os.environ, {}, clear=True):
            response = await plugin.page_state()

        public = response.body["settings"]
        self.assertNotIn("api_key", public)
        self.assertTrue(public["api_key_configured"])
        self.assertEqual(public["api_key_suffix"], "1234")
        self.assertNotIn("CWA-VERY-SECRET-1234", repr(response.body))

    async def test_save_rejects_stale_revision_without_mutating_settings(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        plugin._settings["revision"] = 7
        original = copy.deepcopy(plugin._settings)
        STUB_REQUEST.payload = {"revision": 6, "enabled": True}

        response = await plugin.page_save_settings()

        self.assertEqual(response.status_code, 409)
        self.assertIn("重新整理", response.body["message"])
        self.assertEqual(plugin._settings, original)

    async def test_concurrent_saves_allow_only_one_revision_winner(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        STUB_REQUEST.payload = {
            "revision": 0,
            "enabled": True,
            "source_mode": "rss",
            "poll_interval_seconds": 120,
            "subscriptions": [],
        }

        first, second = await asyncio.gather(
            plugin.page_save_settings(),
            plugin.page_save_settings(),
        )

        self.assertEqual(sorted((first.status_code, second.status_code)), [200, 409])
        self.assertEqual(plugin._settings["revision"], 1)

    async def test_save_preserves_disabled_rule_for_unregistered_group(self) -> None:
        plugin = main.CWAAlertPlugin(StubContext())
        plugin._settings = service.default_settings()
        STUB_REQUEST.payload = {
            "revision": 0,
            "enabled": False,
            "source_mode": "rss",
            "poll_interval_seconds": 120,
            "subscriptions": [
                {
                    "target_umo": "offline:group:retained",
                    "enabled": False,
                    "cities": ["臺北市"],
                    "forecast_enabled": True,
                    "forecast_times": ["07:30"],
                    "alert_types": ["rain"],
                }
            ],
        }

        response = await plugin.page_save_settings()

        self.assertEqual(response.status_code, 200)
        self.assertFalse(plugin._settings["subscriptions"][0]["enabled"])
        self.assertEqual(
            plugin._settings["subscriptions"][0]["target_umo"],
            "offline:group:retained",
        )


if __name__ == "__main__":
    unittest.main()
