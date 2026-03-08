# Azure AI Foundry — APIM Agent Gateway

> **One Endpoint. Zero Latency. Production Ready.**
>
> A production-grade Azure API Management gateway that exposes Azure AI Foundry agents
> running behind private networking via a single unified endpoint — handling all 5 Foundry
> API calls, JWT authentication, RBAC authorization, polling, and structured error handling internally.

---

## 📋 Table of Contents

- [The Problem](#the-problem)
- [The Solution](#the-solution)
- [Architecture](#architecture)
- [Repository Structure](#repository-structure)
- [Prerequisites](#prerequisites)
- [Quick Start](#quick-start)
- [Step-by-Step Setup](#step-by-step-setup)
- [Calling the Endpoint](#calling-the-endpoint)
- [Adding a New Use Case](#adding-a-new-use-case)
- [Error Reference](#error-reference)
- [Latency Benchmarks](#latency-benchmarks)
- [Production Readiness](#production-readiness)

---

## The Problem

When Azure AI Foundry is secured with **Selected Networking** (no public access), invoking a
single agent requires 5 sequential REST API calls:

```
1. POST /threads                    → Create a conversation thread
2. POST /threads/{id}/messages      → Add the user message
3. POST /threads/{id}/runs          → Trigger the agent
4. GET  /threads/{id}/runs/{id}     → Poll until complete (multiple times)
5. GET  /threads/{id}/messages      → Fetch the agent reply
```

Every use case team would need to build this logic independently — handling polling,
auth forwarding, error states, and retries — with a risk of inconsistent implementations
and higher latency.

---

## The Solution

This repository provides a **single APIM endpoint** that handles everything internally.
The caller sends one request, receives one reply.

```
POST /agents/invoke
{
  "foundryName": "my-foundry",
  "projectId":   "proj-default",
  "agentName":   "devops",
  "message":     "What deployment pipelines ran today?"
}
```

---

## Architecture

```
Caller (App / Managed Identity / User)
         │
         │  POST /agents/invoke
         │  Authorization: Bearer <AAD token>
         ▼
┌─────────────────────────────────────────┐
│          Azure API Management           │
│                                         │
│  1. Validate Bearer token (JWT)         │
│  2. Check aud + iss claims              │
│  3. Validate all 4 input fields         │
│  4. Resolve agentName → assistantId     │
│     (from Named Value: agent-map)       │
│  5. POST /threads                       │
│  6. POST /threads/{id}/messages         │
│  7. POST /threads/{id}/runs             │
│  8. GET  /runs/{id}  (poll loop ×24)    │
│  9. GET  /messages   → return reply     │
└─────────────────────────────────────────┘
         │  Private Endpoint / VNet
         ▼
  Azure AI Foundry
  (Selected Networking — No Public Access)
    ├── Azure AI Search    [private endpoint]
    ├── Azure Cosmos DB    [private endpoint]
    └── Azure Storage      [private endpoint]
```

---

## Repository Structure

```
foundry-apim-gateway/
│
├── README.md
│
├── apim-policies/
│   ├── fragment-orchestrator.xml        ← Core: full 5-call lifecycle (deploy once)
│   ├── fragment-error-handler.xml       ← Error handling fragment (deploy once)
│   └── operation-policy-template.xml   ← Per use case: 3 lines only
│
├── scripts/
│   ├── setup_apim.py                    ← Python: deploy all policies + create API operations
│   └── setup_apim.ps1                   ← PowerShell: same, for Windows / Azure DevOps
│
├── client/
│   └── foundry_agent_client.py          ← Production Python client (all auth modes)
│
├── .github/
│   └── workflows/
│       └── onboard-foundry-api.yml      ← Self-service GitHub Actions onboarding
│
├── .env.example                         ← All environment variables documented
└── requirements.txt                     ← Python dependencies
```

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Azure Subscription | Contributor access |
| Azure AI Foundry | Deployed with Selected Networking (private) |
| Azure API Management | Developer tier or above, VNet integrated |
| Python 3.9+ | For scripts and client |
| Azure CLI | Authenticated — `az login` |
| PowerShell 7+ | For `.ps1` setup script |

---

## Quick Start

```bash
# 1. Clone
git clone https://github.com/<your-org>/foundry-apim-gateway.git
cd foundry-apim-gateway

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env with your values

# 4. Deploy all APIM policies (creates Named Values, Fragments, Operations)
python scripts/setup_apim.py

# 5. Test the endpoint
python client/foundry_agent_client.py
```

---

## Step-by-Step Setup

### Step 1 — Named Values

One Named Value per use case. Maps `agentName` (what callers send) to `assistantId`
(the actual Foundry assistant ID).

```
APIM Portal → Named Values → + Add

Name  : agent-map-devops
Type  : Plain
Value : {"devops":"asst_abc123def456"}
```

**Find your assistant ID:**
AI Foundry Portal → Your Project → Agents → Select Agent → Copy Agent ID

Multiple agents per use case:
```json
{"devops": "asst_abc123", "security": "asst_xyz456"}
```

---

### Step 2 — Policy Fragment: `foundry-agent-orchestrator` *(one-time per APIM service)*

```
APIM Portal → Policy Fragments → + Create
Name: foundry-agent-orchestrator
```

Paste the contents of `apim-policies/fragment-orchestrator.xml`.

This fragment handles: JWT validation, input validation, agent allowlist lookup,
thread creation, message posting, run triggering, status polling (up to 24 retries × 1s),
and final message retrieval.

---

### Step 3 — Policy Fragment: `foundry-error-handler` *(one-time per APIM service)*

```
APIM Portal → Policy Fragments → + Create
Name: foundry-error-handler
```

Paste the contents of `apim-policies/fragment-error-handler.xml`.

This fragment handles: 401/403 access denied, 504 gateway timeout, and all unexpected
errors — always returning structured JSON with actionable messages.

---

### Step 4 — Operation Policy *(per use case — 3 lines)*

```
APIM Portal → APIs → Your API → Operations → Your Operation → Policies
```

Paste the contents of `apim-policies/operation-policy-template.xml`.
Change only the Named Value reference to match your use case:

```xml
<set-variable name="agentMapNamedValue" value="{{agent-map-devops}}" />
```

---

### Step 5 — Automated Setup (Python or PowerShell)

Instead of manual portal steps, run the setup script:

```bash
# Python
python scripts/setup_apim.py

# PowerShell
./scripts/setup_apim.ps1 `
  -SubscriptionId "00000000-0000-0000-0000-000000000000" `
  -ResourceGroup  "rg-apim-prod" `
  -ServiceName    "my-apim-service" `
  -FoundryName    "my-foundry" `
  -ProjectId      "proj-default" `
  -AgentMapJson   '{"devops":"asst_abc123"}' `
  -UseCaseName    "devops" `
  -ApiId          "foundry-agents" `
  -OperationId    "invoke-agent" `
  -OperationPath  "/agents/invoke"
```

The script deploys in the correct sequence:
1. Creates Named Value (`agent-map-<usecasename>`)
2. Deploys `foundry-agent-orchestrator` fragment (skips if already exists)
3. Deploys `foundry-error-handler` fragment (skips if already exists)
4. Creates the API and Operation in APIM (if not exists)
5. Applies the Operation Policy

---

## Calling the Endpoint

See `client/foundry_agent_client.py` for a full production client.

**Minimal example:**
```python
import requests
from azure.identity import AzureCliCredential

token = AzureCliCredential(tenant_id="<tenant-id>") \
        .get_token("https://cognitiveservices.azure.com/.default").token

response = requests.post(
    "https://<your-apim>.azure-api.net/<path>",
    json={
        "foundryName": "my-foundry",
        "projectId":   "proj-default",
        "agentName":   "devops",
        "message":     "What is AWS?"
    },
    headers={
        "Authorization":             f"Bearer {token}",
        "Content-Type":              "application/json",
        "Ocp-Apim-Subscription-Key": "<your-apim-key>"
    },
    timeout=60
)
print(response.json())
```

**Token scope (required):**
```
https://cognitiveservices.azure.com/.default
```

---

## Adding a New Use Case

Only 2 things needed:

1. **Create a Named Value:**
   ```
   Name  : agent-map-finance
   Value : {"finance":"asst_fin001","audit":"asst_aud002"}
   ```

2. **Create an Operation with a 3-line policy:**
   ```xml
   <set-variable name="agentMapNamedValue" value="{{agent-map-finance}}" />
   <include-fragment fragment-id="foundry-agent-orchestrator" />
   ```

No changes to fragments. No code changes. Live in under 5 minutes.

---

## Error Reference

| HTTP | Error Code | Cause | Resolution |
|---|---|---|---|
| 400 | `invalid_request` | Body missing or not valid JSON | Send a valid JSON body |
| 400 | `missing_field` | Required field empty | Check `field` property in response |
| 401 | *(APIM)* | Token missing or invalid | Acquire fresh token for correct scope |
| 404 | `agent_not_found` | agentName not in Named Value map | Fix agentName or update agent-map |
| 502 | `thread_creation_failed` | Foundry rejected thread creation | Check RBAC role on Foundry RG |
| 502 | `message_post_failed` | Foundry rejected message post | Check threadId in response |
| 502 | `run_creation_failed` | Foundry rejected agent run | Verify assistantId is valid |
| 500 | `agent_run_failed` | Agent returned failed status | Retry; raise support with runId |
| 408 | `agent_run_expired` | Run timed out | Simplify query or retry |
| 409 | `agent_run_cancelled` | Run was cancelled | Retry |
| 202 | `requires_action` | Agent needs tool output | Submit tool output with runId |
| 403 | `access_denied` | No Foundry RBAC | Assign Azure AI Developer role on Foundry RG |
| 504 | `timeout` | Gateway timeout to Foundry | Retry — likely transient network issue |
| 500 | `internal_error` | Unexpected APIM error | Check `detail` field; contact support |

---

## Latency Benchmarks

Benchmarked against direct Microsoft Azure AI Projects SDK across 5 independent runs:

| Observation | Direct SDK avg | APIM avg | Verdict |
|---|---|---|---|
| 1 — 20 req, simple agent | 7.33s | 8.02s | +0.69s (within LLM variance) |
| 2 — 10 req, multi-topic | 11.007s | 9.34s | **APIM faster by 1.67s ✅** |
| 3 — 10 req, multi-topic | 11.619s | 9.165s | **APIM faster by 2.45s ✅** |
| 4 — 20 req, extended | 9.09s | 8.03s | **APIM faster by 1.06s ✅** |
| 5 — 20 req, final | 8.06s | 8.06s | **Identical ✅** |

**Conclusion:** APIM gateway introduces no meaningful latency overhead. In 4 of 5 observations,
APIM was equal or faster than the direct Microsoft SDK.

---

## Production Readiness

| Capability | Status |
|---|---|
| JWT signature validation | ✅ |
| Audience (aud) enforcement | ✅ |
| Issuer (iss) tenant lock | ✅ |
| All 4 input fields validated | ✅ |
| Agent allowlist enforced | ✅ |
| Per-step Foundry error handling | ✅ |
| All run terminal states handled | ✅ |
| Latency matches Microsoft SDK | ✅ |
| Policy Fragment — zero duplication | ✅ |
| One Named Value per use case | ✅ |
| Works across all Foundry projects | ✅ |
| Actionable structured error messages | ✅ |
| On-error handler via shared fragment | ✅ |
| New use case onboarding in < 5 min | ✅ |
| GitHub Actions self-service workflow | ✅ |

---

## Security Notes

- The Foundry Admin key is **never exposed** to consumers — injected server-side via Named Values
- The caller's own Bearer token is forwarded to Foundry — APIM does not impersonate callers
- Public network access on the Foundry resource must be set to **Disabled**
- Only agents explicitly listed in the `agent-map` Named Value can be invoked

---

*Built for enterprise Azure AI Foundry deployments running behind private networking.*
