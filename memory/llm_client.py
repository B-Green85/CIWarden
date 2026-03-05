"""Abstract LLM client interface with Anthropic and httpx implementations."""

from __future__ import annotations

import json
import os
import re
from abc import ABC, abstractmethod

import anthropic
import httpx

from memory.models import ContractSummary

_EXTRACTION_SYSTEM_PROMPT = """\
You are an architectural contract extractor. Given source code, extract a JSON object \
describing the architectural contracts present in the code.

Return ONLY a valid JSON object (no markdown fencing, no explanation) with this exact schema:

{
  "module": "<module name — provided in the user message>",
  "generation_id": "<generation id — provided in the user message>",
  "sequence": <sequence number — provided in the user message>,
  "contracts": [
    {
      "type": "interface" | "function" | "class" | "module" | "dependency",
      "name": "<name of the contract>",
      "fields": ["<field names if applicable>"],
      "consumed_by": ["<names of consumers if known>"],
      "methods": ["<method names if applicable>"]
    }
  ],
  "assumptions": ["<implicit or explicit assumptions the code makes>"],
  "dependencies": ["<external dependencies the code relies on>"],
  "exposes": ["<public interfaces, functions, or types this code exports>"]
}

Extract ALL contracts: classes, functions, interfaces, type aliases, and module-level \
dependencies. Include implicit assumptions about units, formats, coordinate systems, etc.\
"""


def _strip_markdown_fencing(text: str) -> str:
    """Remove markdown code fencing from LLM responses."""
    stripped = text.strip()
    pattern = r"^```(?:json)?\s*\n?(.*?)\n?\s*```$"
    match = re.match(pattern, stripped, re.DOTALL)
    if match:
        return match.group(1).strip()
    return stripped


def _parse_contract_response(text: str, module: str, generation_id: str, sequence: int) -> ContractSummary:
    """Parse LLM response text into a ContractSummary."""
    cleaned = _strip_markdown_fencing(text)
    data = json.loads(cleaned)
    data["module"] = module
    data["generation_id"] = generation_id
    data["sequence"] = sequence
    return ContractSummary.model_validate(data)


class LLMClient(ABC):
    """Abstract interface for LLM-powered contract extraction."""

    @abstractmethod
    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        """Extract architectural contracts from source code."""
        ...


class AnthropicClient(LLMClient):
    """Contract extraction using the Anthropic Python SDK."""

    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-4-20250514") -> None:
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        if not self.api_key:
            msg = "ANTHROPIC_API_KEY is required for AnthropicClient"
            raise ValueError(msg)

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        client = anthropic.AsyncAnthropic(api_key=self.api_key)
        user_message = (
            f"Module: {module_name}\n"
            f"Generation ID: {generation_id}\n"
            f"Sequence: {sequence}\n\n"
            f"Source code:\n```python\n{source_code}\n```"
        )
        response = await client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=_EXTRACTION_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text  # type: ignore[union-attr]
        return _parse_contract_response(text, module_name, generation_id, sequence)


class HttpxClient(LLMClient):
    """Contract extraction using raw httpx for provider-agnostic use.

    Compatible with OpenAI-style chat completion APIs.
    """

    def __init__(self, base_url: str, api_key: str, model: str = "gpt-4") -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model

    async def extract_contracts(
        self,
        source_code: str,
        module_name: str,
        generation_id: str,
        sequence: int,
    ) -> ContractSummary:
        user_message = (
            f"Module: {module_name}\n"
            f"Generation ID: {generation_id}\n"
            f"Sequence: {sequence}\n\n"
            f"Source code:\n```python\n{source_code}\n```"
        )
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _EXTRACTION_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 4096,
        }
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=120.0,
            )
            response.raise_for_status()
            data = response.json()
            text: str = data["choices"][0]["message"]["content"]
            return _parse_contract_response(text, module_name, generation_id, sequence)
