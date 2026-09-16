"""`creative.screenshot.analyze` from the plugin's side (Spec v0.7 §3.2/§5, ST-16-05): the actual
consent-gate/zone-refusal decision is core-side and covered by `tests/unit/creative/
test_screenshot.py`; this file covers the plugin's half of the contract - it never assumes
success, relays the core's decision verbatim, and degrades honestly if the core never answers."""

from __future__ import annotations

import asyncio

from nox_plugin_creative.plugin import CreativePlugin, EmptyInput

from .conftest import FakeClient, make_api


class TestScreenshotAnalyze:
    async def test_refuses_when_no_app_detected(self) -> None:
        api = make_api(FakeClient())
        plugin = CreativePlugin(api)

        result = await plugin.screenshot_analyze(EmptyInput())

        assert result["status"] == "refused"
        assert "no creative app" in result["reason"]

    async def test_relays_the_core_decision(self) -> None:
        client = FakeClient()
        api = make_api(client)
        plugin = CreativePlugin(api)
        await plugin.start()
        # Simulate a sustained detection so `screenshot_analyze` has a target app.
        plugin.detector.current = "blender"
        plugin._last_title = "Untitled.blend"

        async def respond_soon() -> None:
            await asyncio.sleep(0)
            corr = client.events[-1][1]["corr"]
            await client.fire(
                "creative.screenshot.result",
                {"corr": corr, "status": "captured", "reason": "", "path": "C:/shot.png"},
            )

        asyncio.ensure_future(respond_soon())
        result = await plugin.screenshot_analyze(EmptyInput())

        assert result["status"] == "captured"
        assert result["path"] == "C:/shot.png"
        assert client.events[-1][0] == "creative.screenshot.requested"
        assert client.events[-1][1]["app"] == "blender"

    async def test_honestly_unavailable_when_core_never_responds(self) -> None:
        client = FakeClient()
        api = make_api(client)
        plugin = CreativePlugin(api)
        await plugin.start()
        plugin.detector.current = "blender"
        plugin._last_title = "Untitled.blend"

        import nox_plugin_creative.plugin as plugin_mod

        original = plugin_mod.SCREENSHOT_RESULT_TIMEOUT_S
        plugin_mod.SCREENSHOT_RESULT_TIMEOUT_S = 0.05
        try:
            result = await plugin.screenshot_analyze(EmptyInput())
        finally:
            plugin_mod.SCREENSHOT_RESULT_TIMEOUT_S = original

        assert result["status"] == "unavailable"
        assert "no response" in result["reason"]
