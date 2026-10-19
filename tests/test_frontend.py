import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from custom_components.heating_pulse_controller_v2 import frontend


async def test_frontend_registration_once_for_parallel_room_setup(monkeypatch):
    async def executor(fn):
        return await asyncio.to_thread(fn)

    hass = SimpleNamespace(
        config=SimpleNamespace(components={"frontend"}),
        data={},
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
        async_add_executor_job=executor,
    )
    add = Mock()
    monkeypatch.setattr(frontend.frontend, "add_extra_js_url", add)
    await asyncio.gather(frontend.register(hass), frontend.register(hass))
    hass.http.async_register_static_paths.assert_awaited_once()
    assert "/heating_pulse_controller_v2/thermostat-card.js?v=" in add.call_args.args[1]


async def test_frontend_failure_does_not_prevent_room_setup(monkeypatch):
    hass = SimpleNamespace(
        config=SimpleNamespace(components={"frontend"}),
        data={},
        http=SimpleNamespace(
            async_register_static_paths=AsyncMock(side_effect=OSError("http down"))
        ),
        async_add_executor_job=AsyncMock(return_value='{"version":"test"}'),
    )
    await frontend.register(hass)
    assert frontend.KEY not in hass.data
