"""Provider-neutral structured generation interface and first provider adapter."""

import json
from typing import Protocol, TypeVar

import httpx
from pydantic import BaseModel

from .config import Limits

T = TypeVar("T", bound=BaseModel)


class ModelError(RuntimeError):
    pass


class StructuredModel(Protocol):
    usage: list[dict]

    async def generate(self, instructions: str, data: dict, schema: type[T]) -> T: ...


class OpenAIModel:
    def __init__(self, client: httpx.AsyncClient, api_key: str, model: str, limits: Limits):
        self.client = client
        self.api_key = api_key
        self.model = model
        self.limits = limits
        self.calls = 0
        self.usage: list[dict] = []

    async def generate(self, instructions: str, data: dict, schema: type[T]) -> T:
        if self.calls >= self.limits.max_model_calls:
            raise ModelError("Model call budget exhausted")
        self.calls += 1
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/responses",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "store": False,
                    "instructions": instructions,
                    "input": json.dumps(data, ensure_ascii=False),
                    "max_output_tokens": self.limits.max_output_tokens,
                    "text": {
                        "format": {
                            "type": "json_schema",
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        }
                    },
                },
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            details = {}
            try:
                error = exc.response.json().get("error", {})
                if isinstance(error, dict):
                    details = {
                        k: str(error[k]).replace(self.api_key, "[redacted]")[:400]
                        for k in ("code", "param", "message")
                        if error.get(k)
                    }
            except (ValueError, AttributeError):
                pass
            record = {"call": self.calls, "status": exc.response.status_code}
            if details:
                record["error"] = details
            self.usage.append(record)
            raise ModelError(
                f"OpenAI HTTP {exc.response.status_code}: {details.get('message', 'check model, key, and quota')}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            self.usage.append({"call": self.calls, "status": "request_failed"})
            raise ModelError("OpenAI request failed or returned invalid JSON") from exc
        self.usage.append(
            {
                "call": self.calls,
                "model": self.model,
                "usage": body.get("usage", {}),
                "status": body.get("status"),
            }
        )
        if body.get("status") != "completed":
            raise ModelError("OpenAI response incomplete; no generated content accepted")
        output = "".join(
            part.get("text", "")
            for item in body.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        )
        try:
            return schema.model_validate_json(output)
        except ValueError as exc:
            raise ModelError(
                "Model refused or returned content outside the expected schema"
            ) from exc
