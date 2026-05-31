"""Tests for memory.llm_client — LLM-powered contract extraction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from memory.llm_client import (
    AnthropicClient,
    HttpxClient,
    LLMClient,
    _parse_contract_response,
    _strip_markdown_fencing,
)
from memory.models import ContractType


class TestStripMarkdownFencing:
    def test_plain_json(self) -> None:
        assert _strip_markdown_fencing('{"a": 1}') == '{"a": 1}'

    def test_json_fenced(self) -> None:
        text = '```json\n{"a": 1}\n```'
        assert _strip_markdown_fencing(text) == '{"a": 1}'

    def test_bare_fenced(self) -> None:
        text = '```\n{"a": 1}\n```'
        assert _strip_markdown_fencing(text) == '{"a": 1}'


class TestParseContractResponse:
    def test_valid_json(self) -> None:
        data = {
            "module": "test",
            "generation_id": "gen_001",
            "sequence": 1,
            "contracts": [{"type": "class", "name": "Foo"}],
            "assumptions": [],
            "dependencies": [],
            "exposes": [],
        }
        result = _parse_contract_response(json.dumps(data), "test", "gen_001", 1)
        assert result.module == "test"
        assert result.contracts[0].type == ContractType.CLASS

    def test_invalid_json_raises(self) -> None:
        with pytest.raises(json.JSONDecodeError):
            _parse_contract_response("not json", "test", "gen_001", 1)


class TestLLMClientAbstract:
    def test_cannot_instantiate(self) -> None:
        with pytest.raises(TypeError):
            LLMClient()  # type: ignore[abstract]


class TestAnthropicClient:
    def test_requires_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True), \
             pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
            AnthropicClient(api_key="")

    @pytest.mark.asyncio
    async def test_extract_contracts(self) -> None:
        response_data = {
            "module": "gates",
            "generation_id": "gen_001",
            "sequence": 1,
            "contracts": [{"type": "class", "name": "BaseGate"}],
            "assumptions": ["Python 3.12+"],
            "dependencies": ["fastapi"],
            "exposes": ["BaseGate"],
        }
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=json.dumps(response_data))]

        mock_client_instance = AsyncMock()
        mock_client_instance.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client_instance):
            client = AnthropicClient(api_key="test-key")
            result = await client.extract_contracts("class Foo: pass", "gates", "gen_001", 1)

            assert result.module == "gates"
            assert len(result.contracts) == 1
            assert result.contracts[0].name == "BaseGate"

    @pytest.mark.asyncio
    async def test_extract_handles_markdown_fencing(self) -> None:
        response_data = {
            "module": "test",
            "generation_id": "gen_001",
            "sequence": 1,
            "contracts": [],
            "assumptions": [],
            "dependencies": [],
            "exposes": [],
        }
        fenced = f"```json\n{json.dumps(response_data)}\n```"
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text=fenced)]

        mock_client_instance = AsyncMock()
        mock_client_instance.messages.create = AsyncMock(return_value=mock_response)

        with patch("anthropic.AsyncAnthropic", return_value=mock_client_instance):
            client = AnthropicClient(api_key="test-key")
            result = await client.extract_contracts("x = 1", "test", "gen_001", 1)
            assert result.module == "test"


class TestHttpxClient:
    @pytest.mark.asyncio
    async def test_extract_contracts(self) -> None:
        response_data = {
            "module": "test",
            "generation_id": "gen_001",
            "sequence": 1,
            "contracts": [{"type": "function", "name": "main"}],
            "assumptions": [],
            "dependencies": [],
            "exposes": ["main"],
        }
        api_response = {
            "choices": [{"message": {"content": json.dumps(response_data)}}]
        }

        mock_response = MagicMock()
        mock_response.json.return_value = api_response
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("memory.llm_client.httpx.AsyncClient", return_value=mock_client):
            client = HttpxClient(base_url="http://localhost:8080", api_key="test", model="gpt-4")
            result = await client.extract_contracts("def main(): pass", "test", "gen_001", 1)

            assert result.module == "test"
            assert result.contracts[0].name == "main"
