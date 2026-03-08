"""
setup_apim.py
=============
Deploys the Azure AI Foundry APIM gateway in the correct sequence:

  1. Create Named Value   (agent-map-<usecasename>)
  2. Deploy Fragment      foundry-agent-orchestrator  [skip if exists]
  3. Deploy Fragment      foundry-error-handler       [skip if exists]
  4. Create API           in APIM                     [skip if exists]
  5. Create Operation     in the API                  [skip if exists]
  6. Apply Operation Policy

Usage
-----
  pip install -r requirements.txt
  python scripts/setup_apim.py

  # Or pass values directly (overrides .env):
  python scripts/setup_apim.py \\
    --subscription-id  "00000000-0000-0000-0000-000000000000" \\
    --resource-group   "rg-apim-prod" \\
    --service-name     "my-apim-service" \\
    --foundry-name     "my-foundry-dev" \\
    --project-id       "proj-default" \\
    --agent-map-json   '{"devops":"asst_abc123"}' \\
    --use-case-name    "devops" \\
    --api-id           "foundry-agents" \\
    --operation-id     "invoke-agent" \\
    --operation-path   "/agents/invoke"

Environment Variables (.env)
-----------------------------
  SUBSCRIPTION_ID, APIM_RESOURCE_GROUP, APIM_SERVICE_NAME,
  FOUNDRY_NAME, FOUNDRY_PROJECT_ID, AGENT_MAP_JSON, USE_CASE_NAME,
  APIM_API_ID, APIM_OPERATION_ID, APIM_OPERATION_PATH,
  APIM_API_DISPLAY_NAME, AZURE_TENANT_ID
"""

from __future__ import annotations

import os
import sys
import json
import argparse
import logging
from pathlib import Path

from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential, AzureCliCredential
from azure.mgmt.apimanagement import ApiManagementClient
from azure.mgmt.apimanagement.models import (
    ApiCreateOrUpdateParameter,
    OperationContract,
    NamedValueCreateContract,
    PolicyFragmentContract,
    PolicyContract,
    PolicyContentFormat,
    ApiType,
    Protocol,
)
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("setup_apim")

load_dotenv()

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
POLICIES_DIR = REPO_ROOT / "apim-policies"

ORCHESTRATOR_XML   = POLICIES_DIR / "fragment-orchestrator.xml"
ERROR_HANDLER_XML  = POLICIES_DIR / "fragment-error-handler.xml"
OPERATION_POLICY   = POLICIES_DIR / "operation-policy-template.xml"


# ── Argument parsing ───────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Deploy Foundry APIM gateway policies")
    p.add_argument("--subscription-id",   default=os.getenv("SUBSCRIPTION_ID"))
    p.add_argument("--resource-group",    default=os.getenv("APIM_RESOURCE_GROUP"))
    p.add_argument("--service-name",      default=os.getenv("APIM_SERVICE_NAME"))
    p.add_argument("--foundry-name",      default=os.getenv("FOUNDRY_NAME"))
    p.add_argument("--project-id",        default=os.getenv("FOUNDRY_PROJECT_ID"))
    p.add_argument("--agent-map-json",    default=os.getenv("AGENT_MAP_JSON"))
    p.add_argument("--use-case-name",     default=os.getenv("USE_CASE_NAME"))
    p.add_argument("--api-id",            default=os.getenv("APIM_API_ID", "foundry-agents"))
    p.add_argument("--operation-id",      default=os.getenv("APIM_OPERATION_ID", "invoke-agent"))
    p.add_argument("--operation-path",    default=os.getenv("APIM_OPERATION_PATH", "/agents/invoke"))
    p.add_argument("--api-display-name",  default=os.getenv("APIM_API_DISPLAY_NAME", "Foundry Agent API"))
    p.add_argument("--tenant-id",         default=os.getenv("AZURE_TENANT_ID"))
    p.add_argument("--use-cli-auth",      action="store_true",
                   help="Use AzureCliCredential instead of DefaultAzureCredential")
    return p.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    required = {
        "subscription-id":  args.subscription_id,
        "resource-group":   args.resource_group,
        "service-name":     args.service_name,
        "foundry-name":     args.foundry_name,
        "project-id":       args.project_id,
        "agent-map-json":   args.agent_map_json,
        "use-case-name":    args.use_case_name,
    }
    missing = [k for k, v in required.items() if not v]
    if missing:
        log.error("Missing required values: %s", ", ".join(f"--{m}" for m in missing))
        sys.exit(1)

    try:
        agent_map = json.loads(args.agent_map_json)
        if not isinstance(agent_map, dict) or not agent_map:
            raise ValueError("Must be a non-empty JSON object")
        args._agent_map = agent_map
    except (json.JSONDecodeError, ValueError) as e:
        log.error("--agent-map-json is not valid JSON: %s", e)
        sys.exit(1)

    for path in [ORCHESTRATOR_XML, ERROR_HANDLER_XML, OPERATION_POLICY]:
        if not path.exists():
            log.error("Policy file not found: %s", path)
            sys.exit(1)


# ── APIM client ────────────────────────────────────────────────────────────────
def get_client(args: argparse.Namespace) -> ApiManagementClient:
    cred = AzureCliCredential() if args.use_cli_auth else DefaultAzureCredential()
    return ApiManagementClient(cred, args.subscription_id)


# ── Helper ─────────────────────────────────────────────────────────────────────
def separator(title: str) -> None:
    log.info("─" * 60)
    log.info("  %s", title)
    log.info("─" * 60)


# ── Step 1: Named Value ────────────────────────────────────────────────────────
def deploy_named_value(client: ApiManagementClient, args: argparse.Namespace) -> str:
    use_case  = args.use_case_name.lower().replace(" ", "-")
    nv_id     = f"agent-map-{use_case}"
    separator(f"Step 1 — Named Value: {nv_id}")

    try:
        client.named_value.begin_create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            named_value_id      = nv_id,
            parameters          = NamedValueCreateContract(
                display_name = nv_id,
                value        = args.agent_map_json,
                secret       = False,
                tags         = ["foundry", "agent-map", use_case],
            ),
        ).result()
        log.info("  ✅ Named Value '%s' created/updated.", nv_id)
        log.info("     Agents: %s", list(args._agent_map.keys()))
    except HttpResponseError as e:
        log.error("  ❌ Failed: %s", e.message)
        raise

    return nv_id


# ── Step 2: Fragment — orchestrator ────────────────────────────────────────────
def deploy_orchestrator(client: ApiManagementClient, args: argparse.Namespace) -> None:
    fragment_id = "foundry-agent-orchestrator"
    separator(f"Step 2 — Policy Fragment: {fragment_id}")

    xml = ORCHESTRATOR_XML.read_text(encoding="utf-8")

    # Substitute tenant-id Named Value placeholder if tenant-id provided
    if args.tenant_id:
        xml = xml.replace("{{tenant-id}}", args.tenant_id)

    try:
        # Check if already exists
        try:
            existing = client.policy_fragment.get(
                args.resource_group, args.service_name, fragment_id
            )
            log.info("  ℹ️  Fragment already exists — updating.")
        except ResourceNotFoundError:
            log.info("  ℹ️  Fragment not found — creating.")

        client.policy_fragment.begin_create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            id                  = fragment_id,
            parameters          = PolicyFragmentContract(
                value       = xml,
                description = "Foundry 5-call orchestration: JWT validation, thread/message/run lifecycle, polling, response extraction.",
                format      = PolicyContentFormat.RAWXML,
            ),
        ).result()
        log.info("  ✅ Fragment '%s' deployed.", fragment_id)
    except HttpResponseError as e:
        log.error("  ❌ Failed: %s", e.message)
        raise


# ── Step 3: Fragment — error handler ──────────────────────────────────────────
def deploy_error_handler(client: ApiManagementClient, args: argparse.Namespace) -> None:
    fragment_id = "foundry-error-handler"
    separator(f"Step 3 — Policy Fragment: {fragment_id}")

    xml = ERROR_HANDLER_XML.read_text(encoding="utf-8")

    try:
        try:
            client.policy_fragment.get(args.resource_group, args.service_name, fragment_id)
            log.info("  ℹ️  Fragment already exists — updating.")
        except ResourceNotFoundError:
            log.info("  ℹ️  Fragment not found — creating.")

        client.policy_fragment.begin_create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            id                  = fragment_id,
            parameters          = PolicyFragmentContract(
                value       = xml,
                description = "Structured error handling: 401, 403, 504, and unexpected 500 errors with actionable JSON bodies.",
                format      = PolicyContentFormat.RAWXML,
            ),
        ).result()
        log.info("  ✅ Fragment '%s' deployed.", fragment_id)
    except HttpResponseError as e:
        log.error("  ❌ Failed: %s", e.message)
        raise


# ── Step 4: Create API ─────────────────────────────────────────────────────────
def ensure_api(client: ApiManagementClient, args: argparse.Namespace) -> None:
    separator(f"Step 4 — API: {args.api_id}")

    try:
        client.api.get(args.resource_group, args.service_name, args.api_id)
        log.info("  ℹ️  API '%s' already exists — skipping creation.", args.api_id)
        return
    except ResourceNotFoundError:
        log.info("  ℹ️  API not found — creating.")

    try:
        client.api.begin_create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            api_id              = args.api_id,
            parameters          = ApiCreateOrUpdateParameter(
                display_name = args.api_display_name,
                path         = args.api_id,
                protocols    = [Protocol.HTTPS],
                api_type     = ApiType.HTTP,
                description  = "Azure AI Foundry Agent Gateway — single endpoint for all agent invocations.",
            ),
        ).result()
        log.info("  ✅ API '%s' created.", args.api_id)
    except HttpResponseError as e:
        log.error("  ❌ Failed to create API: %s", e.message)
        raise


# ── Step 5: Create Operation ───────────────────────────────────────────────────
def ensure_operation(client: ApiManagementClient, args: argparse.Namespace) -> None:
    separator(f"Step 5 — Operation: {args.operation_id}")

    try:
        client.api_operation.get(
            args.resource_group, args.service_name, args.api_id, args.operation_id
        )
        log.info("  ℹ️  Operation '%s' already exists — skipping creation.", args.operation_id)
        return
    except ResourceNotFoundError:
        log.info("  ℹ️  Operation not found — creating.")

    try:
        client.api_operation.create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            api_id              = args.api_id,
            operation_id        = args.operation_id,
            parameters          = OperationContract(
                display_name = "Invoke Agent",
                method       = "POST",
                url_template = args.operation_path,
                description  = (
                    "Invoke an Azure AI Foundry agent. "
                    "Handles all 5 Foundry API calls internally. "
                    "Required fields: foundryName, projectId, agentName, message."
                ),
            ),
        )
        log.info("  ✅ Operation '%s' created at POST %s.", args.operation_id, args.operation_path)
    except HttpResponseError as e:
        log.error("  ❌ Failed to create operation: %s", e.message)
        raise


# ── Step 6: Apply Operation Policy ────────────────────────────────────────────
def apply_operation_policy(
    client: ApiManagementClient, args: argparse.Namespace, nv_id: str
) -> None:
    separator(f"Step 6 — Operation Policy → {{{{ {nv_id} }}}}")

    template = OPERATION_POLICY.read_text(encoding="utf-8")
    policy_xml = template.replace("{{agent-map-devops}}", f"{{{{{nv_id}}}}}")

    try:
        client.api_operation_policy.create_or_update(
            resource_group_name = args.resource_group,
            service_name        = args.service_name,
            api_id              = args.api_id,
            operation_id        = args.operation_id,
            policy_id           = "policy",
            parameters          = PolicyContract(
                value  = policy_xml,
                format = PolicyContentFormat.RAWXML,
            ),
        )
        log.info("  ✅ Operation Policy applied.")
        log.info("     Named Value reference: {{%s}}", nv_id)
    except HttpResponseError as e:
        log.error("  ❌ Failed to apply operation policy: %s", e.message)
        raise


# ── Main ───────────────────────────────────────────────────────────────────────
def main() -> None:
    args = parse_args()
    validate_args(args)

    use_case = args.use_case_name.lower().replace(" ", "-")

    log.info("=" * 60)
    log.info("  Azure AI Foundry — APIM Gateway Setup")
    log.info("=" * 60)
    log.info("  APIM Service   : %s", args.service_name)
    log.info("  Resource Group : %s", args.resource_group)
    log.info("  Use Case       : %s", use_case)
    log.info("  Foundry        : %s", args.foundry_name)
    log.info("  Project        : %s", args.project_id)
    log.info("  Agents         : %s", list(args._agent_map.keys()))
    log.info("  API            : %s", args.api_id)
    log.info("  Operation      : %s  →  POST %s", args.operation_id, args.operation_path)
    log.info("=" * 60)

    client = get_client(args)

    nv_id = deploy_named_value(client, args)
    deploy_orchestrator(client, args)
    deploy_error_handler(client, args)
    ensure_api(client, args)
    ensure_operation(client, args)
    apply_operation_policy(client, args, nv_id)

    log.info("=" * 60)
    log.info("  ✅ Deployment complete.")
    log.info("  Endpoint: POST https://%s.azure-api.net/%s%s",
             args.service_name, args.api_id, args.operation_path)
    log.info("  Agents available: %s", list(args._agent_map.keys()))
    log.info("=" * 60)


if __name__ == "__main__":
    main()
