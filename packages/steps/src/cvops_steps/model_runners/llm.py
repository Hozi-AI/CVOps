"""LLM-based annotation runner for text classification.

Env vars:
  LLM_API_KEY          — API key (required)
  LLM_BASE_URL         — base URL (default: https://api.anthropic.com)
  LLM_MODEL            — model ID (default: claude-haiku-4-5-20251001)
  LLM_PROMPT_TEMPLATE  — optional override; use {text} and {labels} placeholders
"""
from __future__ import annotations
import json
import os
from cvops_steps.model_runners.base import ModelRunner

_DEFAULT_PROMPT = (
    "Classify the following text into one of these categories: {labels}.\n"
    'Respond with a JSON object: {{"class_key": "<category>", "confidence": <0-1>}}.\n'
    "Text: {text}"
)


class LlmModelRunner(ModelRunner):
    name = "llm"

    async def predict(self, sample_id, blob_hash, modality, model_bytes, config, storage) -> list[dict]:
        import httpx  # noqa: PLC0415

        if modality != "text":
            raise ValueError(f"LLM runner only supports text samples, got modality={modality!r}")

        raw = await storage.get_bytes(blob_hash)
        text = raw.decode("utf-8", errors="replace")

        labels = config.get("label_names", [])
        prompt_tmpl = os.environ.get("LLM_PROMPT_TEMPLATE", _DEFAULT_PROMPT)
        prompt = prompt_tmpl.format(text=text, labels=", ".join(labels))

        api_key = os.environ.get("LLM_API_KEY", "")
        base_url = os.environ.get("LLM_BASE_URL", "https://api.anthropic.com")
        model_id = os.environ.get("LLM_MODEL", "claude-haiku-4-5-20251001")

        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(
                f"{base_url}/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model_id,
                    "max_tokens": 256,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            r.raise_for_status()
            content = r.json()["content"][0]["text"]

        try:
            parsed = json.loads(content)
            return [parsed] if isinstance(parsed, dict) else parsed
        except json.JSONDecodeError:
            # ponytail: LLM returned prose instead of JSON — return empty rather than crash
            return []
