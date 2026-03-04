# =============================================================================
# Sonar Auto-Fix Platform — Container Image
# =============================================================================
#
# Build:
#   docker build -t sonar-autofix:latest .
#
# Run locally (for testing):
#   docker run --rm \
#     -e AZURE_SERVICEBUS_CONNECTION_STRING="Endpoint=sb://..." \
#     -e AZURE_SERVICEBUS_QUEUE_NAME="sonar-autofix-jobs" \
#     -e GITHUB_PAT="ghp_xxx" \
#     sonar-autofix:latest
#
# The container:
#   1. Starts automatically.
#   2. Receives ONE message from the configured Service Bus queue.
#   3. Downloads the Excel export, runs the full fix pipeline.
#   4. Exits — Azure Container Apps Job removes the instance.
# =============================================================================

# ---------------------------------------------------------------------------
# Stage 1 — dependency layer (cached unless requirements.txt changes)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS deps

# System packages needed at runtime:
#   git         — gitpython calls the system git binary
#   curl        — used by GitHub CLI installer
#   ca-certificates — TLS for HTTPS clone and API calls
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install GitHub CLI — required to authenticate the Copilot SDK
RUN curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies into a separate prefix for layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------------------------------------------------------------------------
# Stage 2 — final runtime image
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Copy system binaries installed in the deps stage
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy GitHub CLI from deps stage
COPY --from=deps /usr/bin/gh /usr/bin/gh
COPY --from=deps /usr/share/keyrings/githubcli-archive-keyring.gpg \
                 /usr/share/keyrings/githubcli-archive-keyring.gpg

# Copy installed Python packages
COPY --from=deps /install /usr/local

WORKDIR /app

# Copy application source and config
COPY src/       ./src/
COPY config/    ./config/

# Create writable workdir for repo clones (ephemeral per job instance)
RUN mkdir -p /app/workdir && chmod 777 /app/workdir

# ---------------------------------------------------------------------------
# Runtime environment defaults (all can be overridden by Container Apps Job)
# ---------------------------------------------------------------------------

# Required — set by Azure Container Apps Job from Key Vault / config:
# ENV AZURE_SERVICEBUS_CONNECTION_STRING=""
# ENV AZURE_SERVICEBUS_QUEUE_NAME=""
# ENV GITHUB_PAT=""
# ENV GITHUB_TOKEN=""

ENV LOG_LEVEL="INFO"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Put the cloned repos in the ephemeral /app/workdir
ENV SONARFIX_WORKDIR="/app/workdir"

# Git safe directory — needed when the repo is cloned as root inside the container
RUN git config --global --add safe.directory '*'

# ---------------------------------------------------------------------------
# Entry point — expects ONE Service Bus message per container run
# ---------------------------------------------------------------------------
ENTRYPOINT ["python", "src/servicebus_trigger.py"]
