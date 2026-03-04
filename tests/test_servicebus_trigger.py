"""
tests/test_servicebus_trigger.py

Unit tests for src/servicebus_trigger.py.

All Azure Service Bus, HTTP, and orchestrator interactions are fully mocked so
the tests run without real cloud resources.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure src/ is importable regardless of how pytest is invoked
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import servicebus_trigger as trigger  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sb_message(payload: dict) -> MagicMock:
    """Create a mock ServiceBusReceivedMessage whose body returns *payload* as JSON."""
    msg = MagicMock()
    raw = json.dumps(payload).encode()
    # body is accessed via b"".join(msg.body)
    msg.body = iter([raw])
    return msg


VALID_PAYLOAD = {
    "excel_url": "https://example.com/issues.xlsx",
    "repo":      "https://github.com/org/repo.git",
    "branch":    "main",
}


# ---------------------------------------------------------------------------
# _download_excel
# ---------------------------------------------------------------------------

class TestDownloadExcel:
    def test_downloads_and_saves_file(self, tmp_path, requests_mock):
        xlsx_bytes = b"PK\x03\x04fake-xlsx-content"
        requests_mock.get("https://example.com/report.xlsx", content=xlsx_bytes)

        result = trigger._download_excel("https://example.com/report.xlsx", tmp_path)

        assert result.name == "report.xlsx"
        assert result.read_bytes() == xlsx_bytes

    def test_filename_derived_from_url_path(self, tmp_path, requests_mock):
        requests_mock.get(
            "https://blob.core.windows.net/ct/MY_EXPORT.xlsx?sv=2023&sig=xxx",
            content=b"data",
        )
        result = trigger._download_excel(
            "https://blob.core.windows.net/ct/MY_EXPORT.xlsx?sv=2023&sig=xxx",
            tmp_path,
        )
        assert result.name == "MY_EXPORT.xlsx"

    def test_fallback_filename_when_path_is_empty(self, tmp_path, requests_mock):
        requests_mock.get("https://example.com/", content=b"data")
        result = trigger._download_excel("https://example.com/", tmp_path)
        assert result.name == "sonar_issues.xlsx"

    def test_raises_on_http_error(self, tmp_path, requests_mock):
        import requests as req_lib
        requests_mock.get("https://example.com/bad.xlsx", status_code=403)
        with pytest.raises(req_lib.HTTPError):
            trigger._download_excel("https://example.com/bad.xlsx", tmp_path)


# ---------------------------------------------------------------------------
# _receive_one_message
# ---------------------------------------------------------------------------

class TestReceiveOneMessage:
    def _make_receiver(self, messages: list) -> MagicMock:
        receiver = MagicMock()
        receiver.__enter__ = MagicMock(return_value=receiver)
        receiver.__exit__ = MagicMock(return_value=False)
        receiver.receive_messages = MagicMock(return_value=messages)
        receiver.complete_message = MagicMock()
        receiver.dead_letter_message = MagicMock()
        return receiver

    def _make_client(self, receiver: MagicMock) -> MagicMock:
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get_queue_receiver = MagicMock(return_value=receiver)
        return client

    def test_returns_payload_on_valid_message(self, monkeypatch):
        monkeypatch.setenv("AZURE_SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://test/")
        monkeypatch.setenv("AZURE_SERVICEBUS_QUEUE_NAME", "q1")

        msg = _make_sb_message(VALID_PAYLOAD)
        receiver = self._make_receiver([msg])
        client = self._make_client(receiver)

        with patch("azure.servicebus.ServiceBusClient") as MockSB:
            MockSB.from_connection_string.return_value = client
            result = trigger._receive_one_message()

        assert result["repo"] == VALID_PAYLOAD["repo"]
        assert result["branch"] == VALID_PAYLOAD["branch"]
        receiver.complete_message.assert_called_once_with(msg)

    def test_raises_when_connection_string_missing(self, monkeypatch):
        monkeypatch.delenv("AZURE_SERVICEBUS_CONNECTION_STRING", raising=False)
        monkeypatch.setenv("AZURE_SERVICEBUS_QUEUE_NAME", "q1")
        with pytest.raises(RuntimeError, match="AZURE_SERVICEBUS_CONNECTION_STRING"):
            trigger._receive_one_message()

    def test_raises_when_queue_name_missing(self, monkeypatch):
        monkeypatch.setenv("AZURE_SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://test/")
        monkeypatch.delenv("AZURE_SERVICEBUS_QUEUE_NAME", raising=False)
        with pytest.raises(RuntimeError, match="AZURE_SERVICEBUS_QUEUE_NAME"):
            trigger._receive_one_message()

    def test_raises_when_queue_is_empty(self, monkeypatch):
        monkeypatch.setenv("AZURE_SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://test/")
        monkeypatch.setenv("AZURE_SERVICEBUS_QUEUE_NAME", "q1")

        receiver = self._make_receiver([])
        client = self._make_client(receiver)

        with patch("azure.servicebus.ServiceBusClient") as MockSB:
            MockSB.from_connection_string.return_value = client
            with pytest.raises(RuntimeError, match="No message received"):
                trigger._receive_one_message()

    def test_dead_letters_invalid_json(self, monkeypatch):
        monkeypatch.setenv("AZURE_SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://test/")
        monkeypatch.setenv("AZURE_SERVICEBUS_QUEUE_NAME", "q1")

        bad_msg = MagicMock()
        bad_msg.body = iter([b"not-json{{{"])
        receiver = self._make_receiver([bad_msg])
        client = self._make_client(receiver)

        with patch("azure.servicebus.ServiceBusClient") as MockSB:
            MockSB.from_connection_string.return_value = client
            with pytest.raises(RuntimeError, match="not valid JSON"):
                trigger._receive_one_message()

        receiver.dead_letter_message.assert_called_once()
        _, kwargs = receiver.dead_letter_message.call_args
        assert kwargs["reason"] == "InvalidJsonPayload"

    def test_dead_letters_missing_required_fields(self, monkeypatch):
        monkeypatch.setenv("AZURE_SERVICEBUS_CONNECTION_STRING", "Endpoint=sb://test/")
        monkeypatch.setenv("AZURE_SERVICEBUS_QUEUE_NAME", "q1")

        incomplete = {"repo": "https://github.com/org/repo.git"}  # missing excel_url, branch
        msg = _make_sb_message(incomplete)
        receiver = self._make_receiver([msg])
        client = self._make_client(receiver)

        with patch("azure.servicebus.ServiceBusClient") as MockSB:
            MockSB.from_connection_string.return_value = client
            with pytest.raises(RuntimeError, match="missing required field"):
                trigger._receive_one_message()

        receiver.dead_letter_message.assert_called_once()
        _, kwargs = receiver.dead_letter_message.call_args
        assert kwargs["reason"] == "MissingRequiredFields"


# ---------------------------------------------------------------------------
# _run_pipeline
# ---------------------------------------------------------------------------

class TestRunPipeline:
    def _make_summary(self, fixed: int = 1, failed: int = 0, pr_url: str = "") -> MagicMock:
        summary = MagicMock()
        summary.fixed = fixed
        summary.failed = failed
        summary.pr_url = pr_url
        summary.__str__ = MagicMock(return_value="Run Summary")
        return summary

    def test_returns_0_on_full_success(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        excel = tmp_path / "issues.xlsx"
        excel.write_bytes(b"fake")
        summary = self._make_summary(fixed=2, failed=0)

        with patch("orchestration.orchestrator.Orchestrator") as MockOrch:
            MockOrch.return_value.run = AsyncMock(return_value=summary)
            code = asyncio.run(trigger._run_pipeline(VALID_PAYLOAD, excel))

        assert code == 0

    def test_returns_1_on_partial_failure(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        excel = tmp_path / "issues.xlsx"
        excel.write_bytes(b"fake")
        summary = self._make_summary(fixed=1, failed=1)

        with patch("orchestration.orchestrator.Orchestrator") as MockOrch:
            MockOrch.return_value.run = AsyncMock(return_value=summary)
            code = asyncio.run(trigger._run_pipeline(VALID_PAYLOAD, excel))

        assert code == 1

    def test_env_token_takes_precedence_over_payload(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GITHUB_PAT", "ghp_from_env")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        excel = tmp_path / "issues.xlsx"
        excel.write_bytes(b"fake")
        summary = self._make_summary()

        captured_cfg = {}

        def fake_init(cfg):
            captured_cfg["pat"] = cfg.pat
            instance = MagicMock()
            instance.run = AsyncMock(return_value=summary)
            return instance

        with patch("orchestration.orchestrator.Orchestrator", side_effect=fake_init):
            asyncio.run(trigger._run_pipeline(
                {**VALID_PAYLOAD, "pat": "ghp_from_message"},
                excel,
            ))

        assert captured_cfg["pat"] == "ghp_from_env"

    def test_rules_parsed_from_list(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        excel = tmp_path / "issues.xlsx"
        excel.write_bytes(b"fake")
        summary = self._make_summary()
        captured_cfg = {}

        def fake_init(cfg):
            captured_cfg["rules"] = cfg.allowed_rules
            instance = MagicMock()
            instance.run = AsyncMock(return_value=summary)
            return instance

        with patch("orchestration.orchestrator.Orchestrator", side_effect=fake_init):
            asyncio.run(trigger._run_pipeline(
                {**VALID_PAYLOAD, "rules": ["csharpsquid:S1118", "csharpsquid:S6966"]},
                excel,
            ))

        assert captured_cfg["rules"] == {"csharpsquid:S1118", "csharpsquid:S6966"}

    def test_rules_parsed_from_comma_string(self, tmp_path, monkeypatch):
        monkeypatch.delenv("GITHUB_PAT", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        excel = tmp_path / "issues.xlsx"
        excel.write_bytes(b"fake")
        summary = self._make_summary()
        captured_cfg = {}

        def fake_init(cfg):
            captured_cfg["rules"] = cfg.allowed_rules
            instance = MagicMock()
            instance.run = AsyncMock(return_value=summary)
            return instance

        with patch("orchestration.orchestrator.Orchestrator", side_effect=fake_init):
            asyncio.run(trigger._run_pipeline(
                {**VALID_PAYLOAD, "rules": "csharpsquid:S1118,csharpsquid:S6966"},
                excel,
            ))

        assert captured_cfg["rules"] == {"csharpsquid:S1118", "csharpsquid:S6966"}
