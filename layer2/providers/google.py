from __future__ import annotations

import json

from google import genai
from google.genai import types as gtypes

from .base import (
    CompletionResponse,
    Message,
    Provider,
    ToolCall,
    ToolParam,
    ToolResult,
    register,
)


def _to_google_tools(tools: list[ToolParam]) -> list[gtypes.Tool]:
    declarations = [
        gtypes.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters=t.parameters,
        )
        for t in tools
    ]
    return [gtypes.Tool(function_declarations=declarations)]


def _to_google_contents(messages: list[Message]) -> list[gtypes.Content]:
    result = []
    for msg in messages:
        if msg.role == "tool":
            parts = [
                gtypes.Part(
                    function_response=gtypes.FunctionResponse(
                        name=tr.tool_call_id,
                        response={"content": tr.content, "is_error": tr.is_error},
                    )
                )
                for tr in msg.tool_results
            ]
            result.append(gtypes.Content(role="user", parts=parts))
        elif msg.tool_calls:
            parts = [
                gtypes.Part(
                    function_call=gtypes.FunctionCall(
                        name=tc.name,
                        args=tc.arguments,
                    )
                )
                for tc in msg.tool_calls
            ]
            result.append(gtypes.Content(role="model", parts=parts))
        else:
            role = "model" if msg.role == "assistant" else "user"
            result.append(gtypes.Content(
                role=role,
                parts=[gtypes.Part(text=msg.content or "")],
            ))
    return result


class GoogleProvider:
    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def complete(
        self,
        messages: list[Message],
        model: str,
        system: str | None = None,
        tools: list[ToolParam] | None = None,
        max_tokens: int = 4096,
    ) -> CompletionResponse:
        config = gtypes.GenerateContentConfig(
            max_output_tokens=max_tokens,
            system_instruction=system,
            tools=_to_google_tools(tools) if tools else None,
        )

        response = await self._client.aio.models.generate_content(
            model=model,
            contents=_to_google_contents(messages),
            config=config,
        )

        candidate = response.candidates[0]
        tool_calls = []
        text_content = None

        for part in candidate.content.parts:
            if part.function_call:
                tool_calls.append(ToolCall(
                    id=part.function_call.name,  # Gemini no provee un id separado
                    name=part.function_call.name,
                    arguments=dict(part.function_call.args),
                ))
            elif part.text:
                text_content = part.text

        finish_map = {
            "STOP": "end_turn",
            "MAX_TOKENS": "max_tokens",
        }
        finish = candidate.finish_reason.name if candidate.finish_reason else "STOP"
        stop_reason = "tool_use" if tool_calls else finish_map.get(finish, "end_turn")

        usage = response.usage_metadata
        return CompletionResponse(
            message=Message(
                role="assistant",
                content=text_content,
                tool_calls=tool_calls,
            ),
            input_tokens=usage.prompt_token_count or 0,
            output_tokens=usage.candidates_token_count or 0,
            stop_reason=stop_reason,
        )


def init(api_key: str) -> None:
    register("google", GoogleProvider(api_key))
