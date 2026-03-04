"""
Tests for execution/sonar_fix_engine.py

The GitHub Copilot SDK client is completely mocked so these tests run
without a Copilot CLI installation or a valid GitHub token.
"""
from __future__ import annotations

import asyncio
import sys
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from execution.sonar_fix_engine import SonarFixEngine, FixResult, SonarFixError


# ---------------------------------------------------------------------------
# Helpers — fake Copilot SDK objects
# ---------------------------------------------------------------------------

class _FakeEventType(Enum):
    assistant_message = "assistant.message"
    session_idle = "session.idle"
    session_error = "session.error"


def _make_fake_session(
    response_text: str = "Fixed the issue successfully.",
    trigger_error: bool = False,
):
    """
    Build a fake CopilotSession-like object whose ``on`` callback fires
    assistant.message then session.idle (or session.error) when ``send`` is called.
    """
    handlers = []

    async def _send(payload):
        # Simulate the agent firing events
        if trigger_error:
            err_evt = SimpleNamespace(
                type=SimpleNamespace(value="session.error"),
                data=SimpleNamespace(error="Something went wrong"),
            )
            for h in handlers:
                h(err_evt)
        else:
            msg_evt = SimpleNamespace(
                type=SimpleNamespace(value="assistant.message"),
                data=SimpleNamespace(content=response_text),
            )
            for h in handlers:
                h(msg_evt)

        idle_evt = SimpleNamespace(
            type=SimpleNamespace(value="session.idle"),
            data=SimpleNamespace(),
        )
        for h in handlers:
            h(idle_evt)

    def _on(handler):
        handlers.append(handler)
        return lambda: handlers.remove(handler)   # unsubscribe fn

    session = MagicMock()
    session.send = _send
    session.on = _on
    session.destroy = AsyncMock()
    return session


def _make_fake_client(session):
    client = MagicMock()
    client.start = AsyncMock()
    client.stop = AsyncMock()
    client.create_session = AsyncMock(return_value=session)
    return client


# ---------------------------------------------------------------------------
# Fake domain objects (RuleInfo / IssueModel)
# ---------------------------------------------------------------------------

def _make_rule(key="cs-S1006", name="Method override", severity="CRITICAL"):
    rule = SimpleNamespace(key=key, name=name, severity=severity, language="cs")
    return rule


def _make_issue(key="uuid-001", line=42, component="repo:main:src/Foo.cs", message="Fix me"):
    return SimpleNamespace(
        key=key,
        line=line,
        component=component,
        message=message,
        severity="CRITICAL",
        rule=_make_rule(),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def repo_path(tmp_path):
    return tmp_path / "fake-repo"


class TestSonarFixEngineStart:
    def test_raises_when_sdk_not_installed(self, repo_path):
        """Engine must raise SonarFixError when github-copilot-sdk is absent."""
        with patch.dict("sys.modules", {"copilot": None}):
            engine = SonarFixEngine(repo_path=repo_path)
            with pytest.raises(SonarFixError, match="github-copilot-sdk"):
                asyncio.run(engine.start())

    def test_raises_when_fix_called_before_start(self, repo_path):
        engine = SonarFixEngine(repo_path=repo_path)
        with pytest.raises(SonarFixError, match="not started"):
            asyncio.run(engine.fix_rule(_make_rule(), [_make_issue()]))


class TestFixRule:
    def _run_fix(self, repo_path, session, issues=None, rule=None, model=None):
        """Helper: patch CopilotClient, start engine, run fix_rule, return results."""
        if issues is None:
            issues = [_make_issue()]
        if rule is None:
            rule = _make_rule()

        client = _make_fake_client(session)

        async def _go():
            with patch("copilot.CopilotClient", return_value=client):
                engine = SonarFixEngine(repo_path=repo_path, model=model)
                await engine.start()
                results = await engine.fix_rule(rule, issues)
                await engine.stop()
                return results

        # Provide a fake copilot module
        fake_copilot = MagicMock()
        fake_copilot.CopilotClient = MagicMock(return_value=client)
        with patch.dict("sys.modules", {"copilot": fake_copilot}):
            return asyncio.run(_go())

    def test_successful_fix(self, repo_path):
        session = _make_fake_session("Applied the fix.")
        results = self._run_fix(repo_path, session)
        assert len(results) == 1
        assert results[0].success is True
        assert "Applied the fix." in results[0].summary

    def test_failed_fix_on_error_event(self, repo_path):
        session = _make_fake_session(trigger_error=True)
        results = self._run_fix(repo_path, session)
        assert len(results) == 1
        assert results[0].success is False
        assert "Something went wrong" in results[0].error

    def test_empty_issues_returns_empty(self, repo_path):
        session = _make_fake_session()
        results = self._run_fix(repo_path, session, issues=[])
        assert results == []

    def test_model_none_means_auto(self, repo_path):
        """When model is None the session config should NOT contain a 'model' key."""
        session = _make_fake_session()
        client = _make_fake_client(session)

        async def _go():
            fake_copilot = MagicMock()
            fake_copilot.CopilotClient = MagicMock(return_value=client)
            with patch.dict("sys.modules", {"copilot": fake_copilot}):
                engine = SonarFixEngine(repo_path=repo_path, model=None)
                await engine.start()
                await engine.fix_rule(_make_rule(), [_make_issue()])
                await engine.stop()

        asyncio.run(_go())
        call_kwargs = client.create_session.call_args[0][0]
        assert "model" not in call_kwargs

    def test_explicit_model_forwarded(self, repo_path):
        """An explicit model string must appear in the session config."""
        session = _make_fake_session()
        client = _make_fake_client(session)

        async def _go():
            fake_copilot = MagicMock()
            fake_copilot.CopilotClient = MagicMock(return_value=client)
            with patch.dict("sys.modules", {"copilot": fake_copilot}):
                engine = SonarFixEngine(repo_path=repo_path, model="claude-sonnet-4-5")
                await engine.start()
                await engine.fix_rule(_make_rule(), [_make_issue()])
                await engine.stop()

        asyncio.run(_go())
        call_kwargs = client.create_session.call_args[0][0]
        assert call_kwargs.get("model") == "claude-sonnet-4-5"

    def test_multiple_issues_same_session(self, repo_path):
        """Multiple issues for the same rule share one Copilot session."""
        session = _make_fake_session("Fixed it.")
        issues = [_make_issue(key=f"uuid-{i:03d}") for i in range(3)]
        results = self._run_fix(repo_path, session, issues=issues)
        assert len(results) == 3
        assert all(r.success for r in results)
        # Only one session should be created
        fake_copilot = MagicMock()
        fake_copilot.CopilotClient = MagicMock(return_value=_make_fake_client(session))
