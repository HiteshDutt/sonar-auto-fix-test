"""
servicebus_trigger.py
=====================

Container Job entry-point triggered by an Azure Service Bus message.

Architecture
------------
Callers publish a JSON message to an Azure Service Bus queue.  KEDA monitors
the queue and scales a Container Apps Job from 0 → N instances.  Each
instance runs this script once:

    1.  Receive & **complete** one message from the queue.
    2.  Validate the payload and dead-letter if it is malformed.
    3.  Download the SonarQube Excel export from the URL in the payload.
    4.  Run the full Sonar Auto-Fix pipeline (clone → fix → commit → PR).
    5.  Exit.  The Container Apps Job runtime removes the instance.

No instance idles; cost is essentially zero between jobs.

Required environment variables
-------------------------------
    AZURE_SERVICEBUS_CONNECTION_STRING
        Service Bus namespace connection string
        (e.g. ``Endpoint=sb://ns.servicebus.windows.net/;SharedAccessKeyName=...``).
    AZURE_SERVICEBUS_QUEUE_NAME
        Name of the queue to receive job messages from.

Optional environment variables (secrets — override message payload fields)
--------------------------------------------------------------------------
    GITHUB_PAT          GitHub Personal Access Token for push + PR creation.
    GITHUB_TOKEN        GitHub token used by the Copilot SDK.
    LOG_LEVEL           Default log level: DEBUG | INFO | WARNING | ERROR.

Message JSON payload schema
---------------------------
See ``deploy/servicebus_message_schema.json`` for the full schema.

Minimal example::

    {
        "excel_url": "https://mystg.blob.core.windows.net/exports/DOTNET.xlsx?<sas>",
        "repo":      "https://github.com/org/repo.git",
        "branch":    "dev"
    }

All other fields are optional and mirror the ``sonar_autofix.py`` CLI flags.

Exit codes
----------
    0   Pipeline completed (all issues fixed or none found).
    1   Partial pipeline success (some issues not fixed).
    2   Fatal error (bad message, download failure, clone failure, etc.).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Bootstrap: ensure src/ is importable when this script runs inside the image
# ---------------------------------------------------------------------------
_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def _configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


log = logging.getLogger("servicebus_trigger")


# ---------------------------------------------------------------------------
# Excel download
# ---------------------------------------------------------------------------

def _download_excel(url: str, dest_dir: Path) -> Path:
    """
    Download the SonarQube Excel workbook from *url* to *dest_dir*.

    Supports any HTTP/HTTPS URL including Azure Blob SAS URLs and Azure Blob
    URLs accessed via managed identity (when the SDK is configured through
    ``DefaultAzureCredential``).

    Returns the local ``Path`` to the downloaded file.

    Raises
    ------
    requests.HTTPError
        If the server returns a non-2xx response.
    """
    import requests  # runtime dep already in requirements.txt

    log.info("Downloading Excel workbook: %s", url)
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()

    # Derive filename from the URL path, stripping query-string SAS tokens.
    filename = Path(urlparse(url).path).name or "sonar_issues.xlsx"
    dest = dest_dir / filename
    dest.write_bytes(resp.content)
    log.info("Download complete: %s (%d bytes)", dest, len(resp.content))
    return dest


# ---------------------------------------------------------------------------
# Service Bus: receive one message
# ---------------------------------------------------------------------------

# Fields that must be present in the JSON payload.
_REQUIRED_FIELDS = ("excel_url", "repo", "branch")


def _receive_one_message() -> dict[str, Any]:
    """
    Connect to the configured Azure Service Bus queue, receive exactly one
    message, validate its JSON payload, acknowledge it (``complete``), and
    return the parsed ``dict``.

    Malformed or incomplete messages are dead-lettered so they do not block
    the queue and the job exits with code 2.

    Environment variables read
    --------------------------
    AZURE_SERVICEBUS_CONNECTION_STRING, AZURE_SERVICEBUS_QUEUE_NAME

    Raises
    ------
    RuntimeError
        On configuration errors, read timeouts, or invalid payloads.
    """
    from azure.servicebus import ServiceBusClient  # azure-servicebus>=7.x

    conn_str = os.environ.get("AZURE_SERVICEBUS_CONNECTION_STRING", "").strip()
    queue_name = os.environ.get("AZURE_SERVICEBUS_QUEUE_NAME", "").strip()

    if not conn_str:
        raise RuntimeError(
            "Environment variable AZURE_SERVICEBUS_CONNECTION_STRING is not set."
        )
    if not queue_name:
        raise RuntimeError(
            "Environment variable AZURE_SERVICEBUS_QUEUE_NAME is not set."
        )

    log.info("Connecting to Service Bus queue '%s'.", queue_name)

    with ServiceBusClient.from_connection_string(conn_str) as client:
        # max_wait_time: give KEDA a grace period; if it's truly 0, raise.
        with client.get_queue_receiver(
            queue_name, max_wait_time=60
        ) as receiver:
            messages = receiver.receive_messages(
                max_message_count=1, max_wait_time=30
            )

            if not messages:
                raise RuntimeError(
                    "No message received from the Service Bus queue within the "
                    "wait timeout.  Ensure KEDA triggered the job only when a "
                    "message is available."
                )

            msg = messages[0]
            raw_body = b"".join(msg.body)  # body is an iterator of bytes chunks

            # --- Parse JSON ---
            try:
                payload: dict[str, Any] = json.loads(raw_body)
            except json.JSONDecodeError as exc:
                receiver.dead_letter_message(
                    msg,
                    reason="InvalidJsonPayload",
                    error_description=str(exc),
                )
                raise RuntimeError(
                    f"Service Bus message is not valid JSON: {exc}"
                ) from exc

            # --- Validate required fields ---
            missing = [f for f in _REQUIRED_FIELDS if not payload.get(f)]
            if missing:
                receiver.dead_letter_message(
                    msg,
                    reason="MissingRequiredFields",
                    error_description=f"Missing fields: {missing}",
                )
                raise RuntimeError(
                    f"Message payload is missing required field(s): {missing}"
                )

            # Acknowledge — completing before running the pipeline.
            # If the pipeline fails the PR is still owed a re-queue by the
            # caller; we do NOT re-queue automatically to avoid duplicate fixes.
            receiver.complete_message(msg)
            log.info(
                "Message received and acknowledged | repo=%s branch=%s",
                payload.get("repo"), payload.get("branch"),
            )
            return payload


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def _run_pipeline(payload: dict[str, Any], excel_path: Path) -> int:
    """
    Build an :class:`OrchestratorConfig` from the message *payload*, run the
    full Sonar Auto-Fix pipeline, and return an exit code (0 = success,
    1 = partial failure).

    Secrets priority (highest → lowest)
    ------------------------------------
    1. ``GITHUB_PAT`` / ``GITHUB_TOKEN`` environment variables (set from
       Azure Key Vault secrets by the Container Apps Job).
    2. ``pat`` / ``github_token`` fields in the message payload.
    """
    from orchestration.orchestrator import Orchestrator, OrchestratorConfig

    # Resolve auth token: env vars from Key Vault take precedence over message
    pat = (
        os.environ.get("GITHUB_PAT")
        or os.environ.get("GITHUB_TOKEN")
        or payload.get("pat")
        or payload.get("github_token")
    )
    github_token = (
        os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GITHUB_PAT")
        or payload.get("github_token")
        or payload.get("pat")
    )

    if not pat:
        log.warning(
            "No GitHub token found in env (GITHUB_PAT / GITHUB_TOKEN) or "
            "message payload.  PR creation will be skipped."
        )

    # rules: accept JSON array or comma-separated string
    raw_rules = payload.get("rules")
    allowed_rules: set[str] | None = None
    if raw_rules:
        if isinstance(raw_rules, list):
            allowed_rules = {r.strip() for r in raw_rules if r.strip()}
        elif isinstance(raw_rules, str):
            allowed_rules = {r.strip() for r in raw_rules.split(",") if r.strip()}

    cfg = OrchestratorConfig(
        excel_path=excel_path,
        repo_url=payload["repo"],
        branch=payload["branch"],
        pat=pat,
        github_token=github_token,
        model=payload.get("model") or None,  # None → SDK default
        allowed_rules=allowed_rules,
        severity_threshold=payload.get("severity"),
        pr_title=payload.get("pr_title"),
        pr_body=payload.get("pr_body"),
        issue_timeout=float(payload.get("timeout", 300)),
    )

    log.info(
        "Pipeline config | repo=%s branch=%s model=%s rules=%s",
        cfg.repo_url, cfg.branch, cfg.model or "auto", allowed_rules or "all",
    )

    summary = await Orchestrator(cfg).run()
    print()
    print(summary)
    if summary.pr_url:
        print(f"\nPull Request: {summary.pr_url}")

    return 0 if summary.failed == 0 else 1


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO")
    _configure_logging(log_level)

    # Step 1 — receive the Service Bus message
    try:
        payload = _receive_one_message()
    except RuntimeError as exc:
        log.error("Failed to receive Service Bus message: %s", exc)
        sys.exit(2)

    # Step 2 — download the Excel export into a temp dir that is cleaned up
    #           automatically when the container job exits
    with tempfile.TemporaryDirectory(prefix="sonarfix_excel_") as tmp:
        tmp_dir = Path(tmp)
        try:
            excel_path = _download_excel(payload["excel_url"], tmp_dir)
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to download Excel workbook: %s", exc)
            sys.exit(2)

        # Step 3 — run the full fix pipeline
        try:
            exit_code = asyncio.run(_run_pipeline(payload, excel_path))
        except KeyboardInterrupt:
            log.warning("Job interrupted by signal.")
            exit_code = 2
        except Exception as exc:  # noqa: BLE001
            log.exception("Fatal pipeline error: %s", exc)
            exit_code = 2

    sys.exit(exit_code)


if __name__ == "__main__":
    main()
