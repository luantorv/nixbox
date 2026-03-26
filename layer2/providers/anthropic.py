from __future__ import annotations

import anthropic

from .base import (
    CompletionResponse,
    Message,
    Provider,
    ToolCall,
    ToolParam,
    ToolResult,
    register,
)


def _to_anthropic_tools(tools: list[ToolParam]) -> list[dict]:
    return [
        {
            "name": t.name,
            "description": t.description,
            "input_schema": t.parameters,
        }
        for t in tools
    ]


def _to_anthropic_messages(messages: list[Message]) -> list[dict]:
    result = []
    for msg in messages:
        if msg.role == "tool":
            result.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tr.tool_call_id,
                        "content": tr.content,
                        "is_error": tr.is_error,
                    }
                    for tr in msg.tool_results
                ],
            })
        elif msg.tool_calls:
            result.append({
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    }
                    for tc in msg.tool_calls
                ],
            })
        else:
            result.append({"role": msg.role, "content": msg.content or ""})
    return result


class AnthropicProvider:
    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self,
        messages: list[Message],
        model: str,
        system: str | None = None,
        tools: list[ToolParam] | None = None,
        max_tokens: int = 4096,
    ) -> CompletionResponse:
        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": _to_anthropic_messages(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = _to_anthropic_tools(tools)

        response = await self._client.messages.create(**kwargs)

        tool_calls = []
        text_content = None
        for block in response.content:
            if block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    arguments=block.input,
                ))
            elif block.type == "text":
                text_content = block.text

        stop_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }

        return CompletionResponse(
            message=Message(
                role="assistant",
                content=text_content,
                tool_calls=tool_calls,
            ),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=stop_map.get(response.stop_reason, "end_turn"),
        )


def init(api_key: str) -> None:
    register("anthropic", AnthropicProvider(api_key))
