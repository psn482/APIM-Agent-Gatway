"""
foundry_agent_client.py
========================
Production-ready Python client for invoking Azure AI Foundry agents
through the APIM gateway.

Supported authentication modes
--------------------------------
  default          →  DefaultAzureCredential (auto-detect — recommended for prod)
  cli              →  AzureCliCredential (local development via az login)
  system_mi        →  ManagedIdentityCredential() (system-assigned)
  user_mi          →  ManagedIdentityCredential(client_id=...) (user-assigned)
  service_principal→  ClientSecretCredential (client secret)
  certificate      →  CertificateCredential (client certificate)

Quick start
-----------
  pip install -r requirements.txt
  cp .env.example .env   # fill in your values
  python client/foundry_agent_client.py

Import usage
------------
  from foundry_agent_client import FoundryAgentClient, AuthMode

  client = FoundryAgentClient.from_env()
  reply  = client.invoke(agent_name="devops", message="What ran today?")
  print(reply.text)

Token scope (required)
-----------------------
  https://cognitiveservices.azure.com/.default
"""

from __future__ import annotations

import os
import sys
import time
import json
import logging
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, List

import requests
from dotenv import load_dotenv
from azure.identity import (
    DefaultAzureCredential,
    ManagedIdentityCredential,
    AzureCliCredential,
    ClientSecretCredential,
    CertificateCredential,
)
from azure.core.exceptions import ClientAuthenticationError

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("foundry_client")

load_dotenv()

# Token scope for Azure AI / Cognitive Services
FOUNDRY_SCOPE = "https://ai.azure.com/.default"


# ── Auth mode enum ─────────────────────────────────────────────────────────────
class AuthMode(str, Enum):
    DEFAULT            = "default"
    CLI                = "cli"
    SYSTEM_MI          = "system_mi"
    USER_MI            = "user_mi"
    SERVICE_PRINCIPAL  = "service_principal"
    CERTIFICATE        = "certificate"


# ── Typed response ─────────────────────────────────────────────────────────────
@dataclass
class AgentMessage:
    role:       str
    text:       str
    message_id: str = ""
    thread_id:  str = ""


@dataclass
class AgentResponse:
    """
    Parsed, typed response from a Foundry agent invocation.

    Attributes:
        text        — the first assistant message text (most common use case)
        messages    — all messages returned (assistant + any others)
        thread_id   — Foundry thread ID
        latency_ms  — round-trip latency in milliseconds
        raw         — full raw JSON response body
    """
    text:       str
    messages:   List[AgentMessage]
    thread_id:  str
    latency_ms: float
    raw:        dict

    @classmethod
    def parse(cls, data: dict, latency_ms: float) -> "AgentResponse":
        messages: List[AgentMessage] = []
        thread_id = ""

        for item in data.get("data", []):
            role      = item.get("role", "")
            thread_id = item.get("thread_id", thread_id)
            msg_id    = item.get("id", "")
            content   = item.get("content", [])
            for block in content:
                if block.get("type") == "text":
                    text = block.get("text", {}).get("value", "")
                    messages.append(AgentMessage(
                        role=role, text=text,
                        message_id=msg_id, thread_id=thread_id
                    ))

        # First assistant reply
        assistant_text = next(
            (m.text for m in messages if m.role == "assistant"), ""
        )

        return cls(
            text=assistant_text,
            messages=messages,
            thread_id=thread_id,
            latency_ms=latency_ms,
            raw=data,
        )


# ── Gateway error ──────────────────────────────────────────────────────────────
class FoundryGatewayError(Exception):
    """Raised when the APIM gateway returns a structured error response."""

    def __init__(self, http_status: int, error_code: str, message: str,
                 run_id: str = "", thread_id: str = ""):
        self.http_status = http_status
        self.error_code  = error_code
        self.message     = message
        self.run_id      = run_id
        self.thread_id   = thread_id
        super().__init__(f"[HTTP {http_status}] {error_code}: {message}")


# ── Client ─────────────────────────────────────────────────────────────────────
class FoundryAgentClient:
    """
    Production-ready client for the Azure AI Foundry APIM gateway.

    Parameters
    ----------
    apim_url              : Full APIM endpoint URL
                            e.g. https://my-apim.azure-api.net/foundry-agents/invoke
    apim_subscription_key : APIM subscription key (Ocp-Apim-Subscription-Key header)
    foundry_name          : Foundry instance name (e.g. my-foundry-dev)
    project_id            : Foundry project ID (e.g. proj-default)
    tenant_id             : Azure AD tenant ID
    auth_mode             : AuthMode enum value (default: AuthMode.DEFAULT)
    timeout_seconds       : Per-request timeout (default: 120)
    max_retries           : Retry count for 504/408/network errors (default: 3)
    retry_backoff_base    : Exponential backoff base in seconds (default: 2.0)
    """

    def __init__(
        self,
        apim_url:               str,
        apim_subscription_key:  str,
        foundry_name:           str,
        project_id:             str,
        tenant_id:              str,
        auth_mode:              AuthMode = AuthMode.DEFAULT,
        managed_identity_client_id: Optional[str] = None,
        sp_client_id:               Optional[str] = None,
        sp_client_secret:           Optional[str] = None,
        sp_certificate_path:        Optional[str] = None,
        timeout_seconds:    int   = 120,
        max_retries:        int   = 3,
        retry_backoff_base: float = 2.0,
    ):
        self.apim_url              = apim_url.rstrip("/")
        self.apim_subscription_key = apim_subscription_key
        self.foundry_name          = foundry_name
        self.project_id            = project_id
        self.tenant_id             = tenant_id
        self.auth_mode             = auth_mode
        self.timeout_seconds       = timeout_seconds
        self.max_retries           = max_retries
        self.retry_backoff_base    = retry_backoff_base

        self._credential = self._build_credential(
            auth_mode                  = auth_mode,
            tenant_id                  = tenant_id,
            managed_identity_client_id = managed_identity_client_id,
            sp_client_id               = sp_client_id,
            sp_client_secret           = sp_client_secret,
            sp_certificate_path        = sp_certificate_path,
        )

        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type":              "application/json",
            "Ocp-Apim-Subscription-Key": self.apim_subscription_key,
        })

        log.info(
            "FoundryAgentClient initialised | auth=%s | foundry=%s | project=%s",
            auth_mode.value, foundry_name, project_id
        )

    # ── Factory ────────────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls) -> "FoundryAgentClient":
        """Build client from environment variables / .env file."""
        required = [
            "APIM_URL", "APIM_SUBSCRIPTION_KEY",
            "FOUNDRY_NAME", "FOUNDRY_PROJECT_ID", "AZURE_TENANT_ID",
        ]
        missing = [v for v in required if not os.getenv(v)]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Copy .env.example to .env and fill in your values."
            )

        return cls(
            apim_url               = os.environ["APIM_URL"],
            apim_subscription_key  = os.environ["APIM_SUBSCRIPTION_KEY"],
            foundry_name           = os.environ["FOUNDRY_NAME"],
            project_id             = os.environ["FOUNDRY_PROJECT_ID"],
            tenant_id              = os.environ["AZURE_TENANT_ID"],
            auth_mode              = AuthMode(os.getenv("AUTH_MODE", AuthMode.DEFAULT)),
            managed_identity_client_id = os.getenv("MANAGED_IDENTITY_CLIENT_ID"),
            sp_client_id               = os.getenv("AZURE_CLIENT_ID"),
            sp_client_secret           = os.getenv("AZURE_CLIENT_SECRET"),
            sp_certificate_path        = os.getenv("AZURE_CLIENT_CERTIFICATE_PATH"),
            timeout_seconds            = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
            max_retries                = int(os.getenv("MAX_RETRIES", "3")),
        )

    # ── Credential factory ─────────────────────────────────────────────────────
    def _build_credential(
        self,
        auth_mode:                  AuthMode,
        tenant_id:                  str,
        managed_identity_client_id: Optional[str],
        sp_client_id:               Optional[str],
        sp_client_secret:           Optional[str],
        sp_certificate_path:        Optional[str],
    ):
        if auth_mode == AuthMode.DEFAULT:
            log.debug("Auth: DefaultAzureCredential")
            return DefaultAzureCredential()

        if auth_mode == AuthMode.CLI:
            log.debug("Auth: AzureCliCredential")
            return AzureCliCredential(tenant_id=tenant_id)

        if auth_mode == AuthMode.SYSTEM_MI:
            log.debug("Auth: System-assigned ManagedIdentityCredential")
            return ManagedIdentityCredential()

        if auth_mode == AuthMode.USER_MI:
            if not managed_identity_client_id:
                raise ValueError("AUTH_MODE=user_mi requires MANAGED_IDENTITY_CLIENT_ID")
            log.debug("Auth: User-assigned ManagedIdentityCredential(client_id=%s)",
                      managed_identity_client_id)
            return ManagedIdentityCredential(client_id=managed_identity_client_id)

        if auth_mode == AuthMode.SERVICE_PRINCIPAL:
            if not (sp_client_id and sp_client_secret):
                raise ValueError(
                    "AUTH_MODE=service_principal requires AZURE_CLIENT_ID and AZURE_CLIENT_SECRET"
                )
            log.debug("Auth: ClientSecretCredential(client_id=%s)", sp_client_id)
            return ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=sp_client_id,
                client_secret=sp_client_secret,
            )

        if auth_mode == AuthMode.CERTIFICATE:
            if not (sp_client_id and sp_certificate_path):
                raise ValueError(
                    "AUTH_MODE=certificate requires AZURE_CLIENT_ID and AZURE_CLIENT_CERTIFICATE_PATH"
                )
            log.debug("Auth: CertificateCredential(client_id=%s)", sp_client_id)
            return CertificateCredential(
                tenant_id=tenant_id,
                client_id=sp_client_id,
                certificate_path=sp_certificate_path,
            )

        raise ValueError(f"Unknown auth mode: {auth_mode}")

    # ── Token acquisition ──────────────────────────────────────────────────────
    def _get_token(self) -> str:
        try:
            return self._credential.get_token(FOUNDRY_SCOPE).token
        except ClientAuthenticationError as e:
            raise FoundryGatewayError(
                401, "token_acquisition_failed",
                f"Failed to acquire Azure AD token for scope '{FOUNDRY_SCOPE}'. "
                f"Check your credentials and auth mode. Detail: {e}"
            ) from e

    # ── Core invoke ────────────────────────────────────────────────────────────
    def invoke(self, agent_name: str, message: str) -> AgentResponse:
        """
        Invoke a Foundry agent through the APIM gateway.

        Parameters
        ----------
        agent_name : Agent name as registered in the APIM agent-map Named Value.
        message    : User message to send to the agent.

        Returns
        -------
        AgentResponse with .text (first reply), .messages, .thread_id, .latency_ms

        Raises
        ------
        FoundryGatewayError     — structured gateway error (4xx / 5xx)
        requests.RequestException — network-level failure after all retries
        """
        payload = {
            "foundryName": self.foundry_name,
            "projectId":   self.project_id,
            "agentName":   agent_name,
            "message":     message,
        }

        log.info("Invoking agent '%s' | message length: %d chars",
                 agent_name, len(message))

        last_exc: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                token = self._get_token()
                self._session.headers["Authorization"] = f"Bearer {token}"

                t0   = time.monotonic()
                resp = self._session.post(
                    self.apim_url,
                    json    = payload,
                    timeout = self.timeout_seconds,
                )
                latency_ms = (time.monotonic() - t0) * 1000

                log.info("HTTP %d | %.0f ms | attempt %d/%d",
                         resp.status_code, latency_ms, attempt, self.max_retries)

                # Parse response body
                try:
                    data = resp.json()
                except ValueError:
                    data = {"raw_text": resp.text}

                # ── Success ────────────────────────────────────────────────────
                if resp.status_code == 200:
                    result = AgentResponse.parse(data, latency_ms)
                    log.info("Agent reply received | thread_id=%s | %.0f ms",
                             result.thread_id, latency_ms)
                    return result

                # ── Structured gateway error ───────────────────────────────────
                err   = data.get("error", {}) if isinstance(data, dict) else {}
                code  = err.get("code") or data.get("error", "unknown_error")
                msg   = err.get("message") or data.get("message", resp.text)
                run_id    = data.get("runId", "")
                thread_id = data.get("threadId", "")

                # Retryable: 504 timeout, 408 expired
                if resp.status_code in (504, 408) and attempt < self.max_retries:
                    wait = self.retry_backoff_base ** attempt
                    log.warning(
                        "Retryable [%d] %s — retry %d/%d in %.1fs",
                        resp.status_code, code, attempt, self.max_retries, wait
                    )
                    time.sleep(wait)
                    last_exc = FoundryGatewayError(
                        resp.status_code, code, msg, run_id, thread_id
                    )
                    continue

                raise FoundryGatewayError(resp.status_code, code, msg, run_id, thread_id)

            except FoundryGatewayError:
                raise

            except requests.exceptions.Timeout:
                last_exc = FoundryGatewayError(504, "client_timeout",
                                               "Request timed out at the client level.")
                if attempt < self.max_retries:
                    wait = self.retry_backoff_base ** attempt
                    log.warning("Timeout on attempt %d/%d — retry in %.1fs",
                                attempt, self.max_retries, wait)
                    time.sleep(wait)
                    continue

            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = self.retry_backoff_base ** attempt
                    log.warning("Network error on attempt %d/%d — retry in %.1fs: %s",
                                attempt, self.max_retries, wait, exc)
                    time.sleep(wait)
                    continue

        raise last_exc or FoundryGatewayError(
            500, "max_retries_exceeded",
            f"All {self.max_retries} retry attempts failed."
        )


# ── CLI demo ───────────────────────────────────────────────────────────────────
def run_demo() -> None:
    """
    Demo: shows all auth modes and runs a live call if env vars are set.
    """
    print("\n" + "=" * 64)
    print("  Azure AI Foundry APIM Client — Auth Mode Reference")
    print("=" * 64)

    modes = [
        (AuthMode.DEFAULT,           "Auto-detect (recommended for prod/CI)"),
        (AuthMode.CLI,               "az login  (local development)"),
        (AuthMode.SYSTEM_MI,         "System-assigned Managed Identity"),
        (AuthMode.USER_MI,           "User-assigned Managed Identity"),
        (AuthMode.SERVICE_PRINCIPAL, "Service Principal — client secret"),
        (AuthMode.CERTIFICATE,       "Service Principal — certificate"),
    ]

    for mode, desc in modes:
        print(f"\n  [{mode.value}]  {desc}")
        print(f"  {'─'*52}")

        if mode == AuthMode.DEFAULT:
            print("  from azure.identity import DefaultAzureCredential")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.DEFAULT)")

        elif mode == AuthMode.CLI:
            print("  # Requires: az login")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.CLI)")

        elif mode == AuthMode.SYSTEM_MI:
            print("  # Requires: system-assigned managed identity on the resource")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.SYSTEM_MI)")

        elif mode == AuthMode.USER_MI:
            print("  # Requires: MANAGED_IDENTITY_CLIENT_ID env var")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.USER_MI,")
            print("                     managed_identity_client_id='<client-id>')")

        elif mode == AuthMode.SERVICE_PRINCIPAL:
            print("  # Requires: AZURE_CLIENT_ID + AZURE_CLIENT_SECRET env vars")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.SERVICE_PRINCIPAL,")
            print("                     sp_client_id='<id>', sp_client_secret='<secret>')")

        elif mode == AuthMode.CERTIFICATE:
            print("  # Requires: AZURE_CLIENT_ID + AZURE_CLIENT_CERTIFICATE_PATH env vars")
            print("  FoundryAgentClient(..., auth_mode=AuthMode.CERTIFICATE,")
            print("                     sp_client_id='<id>', sp_certificate_path='<path>')")

    print("\n" + "=" * 64)
    print("  Token scope (always required):")
    print(f"  {FOUNDRY_SCOPE}")
    print("=" * 64)

    # ── Live invocation ────────────────────────────────────────────────────────
    if not (os.getenv("APIM_URL") and os.getenv("APIM_SUBSCRIPTION_KEY")):
        print("\n  ℹ️  Set APIM_URL + APIM_SUBSCRIPTION_KEY in .env for a live test.")
        return

    print("\n" + "─" * 64)
    print("  Live Invocation")
    print("─" * 64)

    agent_name = os.getenv("TEST_AGENT_NAME", "devops")
    message    = os.getenv("TEST_MESSAGE",    "Hello, what can you help me with?")

    print(f"  Agent   : {agent_name}")
    print(f"  Message : {message}")

    try:
        client = FoundryAgentClient.from_env()
        result = client.invoke(agent_name=agent_name, message=message)

        print(f"\n  ✅ Reply      : {result.text}")
        print(f"     Thread ID  : {result.thread_id}")
        print(f"     Latency    : {result.latency_ms:.0f} ms")
        print(f"     Messages   : {len(result.messages)}")

    except FoundryGatewayError as e:
        print(f"\n  ❌ Gateway error [{e.http_status}] {e.error_code}")
        print(f"     Message   : {e.message}")
        if e.run_id:    print(f"     Run ID    : {e.run_id}")
        if e.thread_id: print(f"     Thread ID : {e.thread_id}")

    except EnvironmentError as e:
        print(f"\n  ⚠️  {e}")

    except Exception as e:
        print(f"\n  ❌ Unexpected error: {e}")

    print()


if __name__ == "__main__":
    run_demo()
