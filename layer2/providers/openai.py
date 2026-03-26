from __future__ import annotations

import json

from openai import AsyncOpenAI

from .base import (
    CompletionResponse,
    Message,
    Provider,
    ToolCall,
    ToolParam,
    ToolResult,
    register,
)


def _to_openai_tools(tools: list[ToolParam]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in tools
    ]


def _to_openai_messages(messages: list[Message]) -> list[dict]:
    result = []
    for msg in messages:
        if msg.role == "tool":
            for tr in msg.tool_results:
                result.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_call_id,
                    "content": tr.content,
                })
        elif msg.tool_calls:
            result.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ],
            })
        else:
            result.append({"role": msg.role, "content": msg.content or ""})
    return result


class OpenAIProvider:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def complete(
        self,
        messages: list[Message],
        model: str,
        system: str | None = None,
        tools: list[ToolParam] | None = None,
        max_tokens: int = 4096,
    ) -> CompletionResponse:
        all_messages = []
        if system:
            all_messages.append({"role": "system", "content": system})
        all_messages.extend(_to_openai_messages(messages))

        kwargs: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": all_messages,
        }
        if tools:
            kwargs["tools"] = _to_openai_tools(tools)

        response = await self._client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=json.loads(tc.function.arguments),
                ))

        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }

        return CompletionResponse(
            message=Message(
                role="assistant",
                content=msg.content,
                tool_calls=tool_calls,
            ),
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            stop_reason=stop_map.get(choice.finish_reason, "end_turn"),
        )


def init(api_key: str) -> None:
    register("openai", OpenAIProvider(api_key))
