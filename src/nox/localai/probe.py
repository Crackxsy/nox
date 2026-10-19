"""The check after the download: can the new model talk, and can it call a tool natively?

A model that answers in prose where a tool call belongs can talk and do nothing (see
`docs/LOCAL_MODELS.md`), so the setup writes the model to `user.yaml` only after both questions
came back right. The questions go through the real `OllamaProvider` - its payload builder, its
NDJSON reader and its tool-call parser - so the probe tests exactly what Nox will later run, and
no parser is duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass

from nox.ai.base import AiRequest, AiRole, Message, ToolDefinition
from nox.ai.errors import ProviderError
from nox.ai.providers.ollama import OllamaProvider

__all__ = ["PROBE_TIMEOUT_S", "PROBE_TOOL", "ProbeResult", "probe_model"]

#: Generous: the first question also loads the freshly downloaded model.
PROBE_TIMEOUT_S = 180.0

#: One tool from what Nox really offers, asked for in German - the case the protocol has to survive.
PROBE_TOOL = ToolDefinition(
    name="desktop.windows",
    description="The open windows.",
    input_schema={"type": "object", "properties": {}},
)
_TOOL_QUESTION = "Welche Fenster sind gerade offen?"
_TEXT_QUESTION = "Sag in einem kurzen Satz Hallo."


@dataclass(frozen=True, slots=True)
class ProbeResult:
    text_ok: bool
    tool_ok: bool
    #: Why it failed, in the provider's words; empty on success.
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.text_ok and self.tool_ok


def _request(request_id: str, model: str, question: str, *, tools: bool) -> AiRequest:
    return AiRequest(
        request_id=request_id,
        role=AiRole.CHAT,
        messages=[
            Message(role="system", content="You are Nox. Answer briefly."),
            Message(role="user", content=question),
        ],
        temperature=0.0,
        max_tokens=128,
        timeout_s=PROBE_TIMEOUT_S,
        stream=tools,
        metadata={"model": model},
        tools=[PROBE_TOOL] if tools else [],
    )


async def probe_model(provider: OllamaProvider, model: str) -> ProbeResult:
    """Ask `model` one plain question and one that needs `PROBE_TOOL`."""
    try:
        answer = await provider.complete(
            _request("localai-probe-text", model, _TEXT_QUESTION, tools=False)
        )
    except ProviderError as exc:
        return ProbeResult(text_ok=False, tool_ok=False, reason=f"text: {exc}")
    if not answer.text.strip():  # the provider refuses empty answers; this is belt and braces
        return ProbeResult(text_ok=False, tool_ok=False, reason="text: empty answer")

    if not await provider.supports_tools(model):
        return ProbeResult(
            text_ok=True, tool_ok=False, reason="the model does not declare native tool calling"
        )
    request = _request("localai-probe-tool", model, _TOOL_QUESTION, tools=True)
    names: list[str] = []
    try:
        async for chunk in provider.stream(request):
            names += [call.name for call in chunk.tool_calls]
    except ProviderError as exc:
        return ProbeResult(text_ok=True, tool_ok=False, reason=f"tool: {exc}")
    if PROBE_TOOL.name not in names:
        got = ", ".join(names) or "an answer in prose"
        return ProbeResult(
            text_ok=True, tool_ok=False, reason=f"tool: expected {PROBE_TOOL.name}, got {got}"
        )
    return ProbeResult(text_ok=True, tool_ok=True)
