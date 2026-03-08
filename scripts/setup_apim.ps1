<#
.SYNOPSIS
    Deploys the Azure AI Foundry APIM gateway in the correct sequence.

.DESCRIPTION
    Executes 6 steps in order:
      1. Create Named Value (agent-map-<usecasename>)
      2. Deploy Policy Fragment: foundry-agent-orchestrator  [skip if exists]
      3. Deploy Policy Fragment: foundry-error-handler       [skip if exists]
      4. Create API in APIM                                  [skip if exists]
      5. Create Operation in API                             [skip if exists]
      6. Apply Operation Policy

.PARAMETER SubscriptionId       Azure subscription ID
.PARAMETER ResourceGroup        Resource group containing APIM
.PARAMETER ServiceName          APIM service name
.PARAMETER FoundryName          Azure AI Foundry instance name
.PARAMETER ProjectId            Foundry project ID
.PARAMETER AgentMapJson         JSON map of agentName → assistantId
.PARAMETER UseCaseName          Use case name (used in Named Value naming)
.PARAMETER ApiId                APIM API ID
.PARAMETER OperationId          APIM Operation ID
.PARAMETER OperationPath        URL path for the operation (e.g. /agents/invoke)
.PARAMETER ApiDisplayName       Display name for the API
.PARAMETER TenantId             Azure AD tenant ID (used in JWT policy)
.PARAMETER UseCliAuth           Use az CLI auth instead of Connect-AzAccount

.EXAMPLE
    ./setup_apim.ps1 `
        -SubscriptionId  "00000000-0000-0000-0000-000000000000" `
        -ResourceGroup   "rg-apim-prod" `
        -ServiceName     "my-apim-service" `
        -FoundryName     "my-foundry-dev" `
        -ProjectId       "proj-default" `
        -AgentMapJson    '{"devops":"asst_abc123","security":"asst_xyz456"}' `
        -UseCaseName     "devops" `
        -ApiId           "foundry-agents" `
        -OperationId     "invoke-agent" `
        -OperationPath   "/agents/invoke" `
        -TenantId        "your-tenant-id"
#>

[CmdletBinding()]
param (
    [Parameter(Mandatory)] [string] $SubscriptionId,
    [Parameter(Mandatory)] [string] $ResourceGroup,
    [Parameter(Mandatory)] [string] $ServiceName,
    [Parameter(Mandatory)] [string] $FoundryName,
    [Parameter(Mandatory)] [string] $ProjectId,
    [Parameter(Mandatory)] [string] $AgentMapJson,
    [Parameter(Mandatory)] [string] $UseCaseName,
    [string] $ApiId           = "foundry-agents",
    [string] $OperationId     = "invoke-agent",
    [string] $OperationPath   = "/agents/invoke",
    [string] $ApiDisplayName  = "Foundry Agent API",
    [string] $TenantId        = "",
    [switch] $UseCliAuth
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Helpers ────────────────────────────────────────────────────────────────────
function Write-Step([string]$n, [string]$msg) {
    Write-Host "`n$("─"*60)" -ForegroundColor DarkGray
    Write-Host "  Step $n — $msg" -ForegroundColor Cyan
    Write-Host "$("─"*60)" -ForegroundColor DarkGray
}
function Write-OK([string]$msg)   { Write-Host "  ✅ $msg" -ForegroundColor Green }
function Write-Skip([string]$msg) { Write-Host "  ℹ️  $msg" -ForegroundColor Yellow }
function Write-Fail([string]$msg) { Write-Host "  ❌ $msg" -ForegroundColor Red; throw $msg }

# ── Paths ──────────────────────────────────────────────────────────────────────
$RepoRoot      = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
$PoliciesDir   = Join-Path $PSScriptRoot ".." "apim-policies"
$OrchestratorFile  = Join-Path $PoliciesDir "fragment-orchestrator.xml"
$ErrorHandlerFile  = Join-Path $PoliciesDir "fragment-error-handler.xml"
$OperationPolicyFile = Join-Path $PoliciesDir "operation-policy-template.xml"

foreach ($f in @($OrchestratorFile, $ErrorHandlerFile, $OperationPolicyFile)) {
    if (-not (Test-Path $f)) { Write-Fail "Required file not found: $f" }
}

# ── Validate agent map JSON ────────────────────────────────────────────────────
try {
    $AgentMap  = $AgentMapJson | ConvertFrom-Json -ErrorAction Stop
    $AgentKeys = ($AgentMap.PSObject.Properties.Name) -join ", "
} catch {
    Write-Fail "AgentMapJson is not valid JSON: $_"
}

$UseCaseLower = $UseCaseName.ToLower() -replace "\s+", "-"
$NamedValueId = "agent-map-$UseCaseLower"

# ── Banner ─────────────────────────────────────────────────────────────────────
Write-Host "`n$("="*60)" -ForegroundColor Yellow
Write-Host "  Azure AI Foundry — APIM Gateway Setup" -ForegroundColor Yellow
Write-Host "$("="*60)" -ForegroundColor Yellow
Write-Host "  APIM Service   : $ServiceName"
Write-Host "  Resource Group : $ResourceGroup"
Write-Host "  Use Case       : $UseCaseLower"
Write-Host "  Foundry        : $FoundryName"
Write-Host "  Project        : $ProjectId"
Write-Host "  Agents         : $AgentKeys"
Write-Host "  API            : $ApiId"
Write-Host "  Operation      : $OperationId  →  POST $OperationPath"
Write-Host "$("="*60)`n" -ForegroundColor Yellow

# ── Authenticate ───────────────────────────────────────────────────────────────
Write-Host "  Authenticating..." -ForegroundColor Cyan
if ($UseCliAuth) {
    $token = (az account get-access-token --resource "https://management.azure.com/" | ConvertFrom-Json).accessToken
    $Headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
    Write-OK "Azure CLI authentication successful."
} else {
    $ctx = Get-AzContext -ErrorAction SilentlyContinue
    if (-not $ctx -or $ctx.Subscription.Id -ne $SubscriptionId) {
        Connect-AzAccount -Subscription $SubscriptionId | Out-Null
    }
    Set-AzContext -SubscriptionId $SubscriptionId | Out-Null
    $token = (Get-AzAccessToken -ResourceUrl "https://management.azure.com/").Token
    $Headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }
    Write-OK "Azure PowerShell authentication successful."
}

$BaseUri = "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ApiManagement/service/$ServiceName"
$ApiVersion = "api-version=2023-05-01-preview"

function Invoke-ApimRest([string]$Method, [string]$Uri, [string]$Body = "") {
    $params = @{ Method = $Method; Uri = $Uri; Headers = $Headers }
    if ($Body) { $params["Body"] = $Body }
    return Invoke-RestMethod @params
}

# ── Step 1: Named Value ────────────────────────────────────────────────────────
Write-Step "1" "Named Value: $NamedValueId"
$nvBody = @{
    properties = @{
        displayName = $NamedValueId
        value       = $AgentMapJson
        secret      = $false
        tags        = @("foundry", "agent-map", $UseCaseLower)
    }
} | ConvertTo-Json -Depth 5

try {
    Invoke-ApimRest "PUT" "$BaseUri/namedValues/$($NamedValueId)?$ApiVersion" $nvBody | Out-Null
    Write-OK "Named Value '$NamedValueId' created/updated."
    Write-Host "     Agents: $AgentKeys" -ForegroundColor Gray
} catch {
    Write-Fail "Failed to create Named Value: $_"
}

# ── Step 2: Fragment — orchestrator ────────────────────────────────────────────
Write-Step "2" "Policy Fragment: foundry-agent-orchestrator"
$orchXml = Get-Content $OrchestratorFile -Raw
if ($TenantId) {
    $orchXml = $orchXml -replace "\{\{tenant-id\}\}", $TenantId
}
$orchBody = @{
    properties = @{
        value       = $orchXml
        description = "Foundry 5-call orchestration: JWT validation, thread/message/run lifecycle, polling, response extraction."
        format      = "rawxml"
    }
} | ConvertTo-Json -Depth 5

try {
    Invoke-ApimRest "PUT" "$BaseUri/policyFragments/foundry-agent-orchestrator?$ApiVersion" $orchBody | Out-Null
    Write-OK "Fragment 'foundry-agent-orchestrator' deployed."
} catch {
    Write-Fail "Failed to deploy orchestrator fragment: $_"
}

# ── Step 3: Fragment — error handler ──────────────────────────────────────────
Write-Step "3" "Policy Fragment: foundry-error-handler"
$ehXml  = Get-Content $ErrorHandlerFile -Raw
$ehBody = @{
    properties = @{
        value       = $ehXml
        description = "Structured error handling: 401, 403, 504, and unexpected 500 errors with actionable JSON bodies."
        format      = "rawxml"
    }
} | ConvertTo-Json -Depth 5

try {
    Invoke-ApimRest "PUT" "$BaseUri/policyFragments/foundry-error-handler?$ApiVersion" $ehBody | Out-Null
    Write-OK "Fragment 'foundry-error-handler' deployed."
} catch {
    Write-Fail "Failed to deploy error handler fragment: $_"
}

# ── Step 4: Create API ─────────────────────────────────────────────────────────
Write-Step "4" "API: $ApiId"
try {
    Invoke-ApimRest "GET" "$BaseUri/apis/$($ApiId)?$ApiVersion" | Out-Null
    Write-Skip "API '$ApiId' already exists — skipping creation."
} catch {
    $apiBody = @{
        properties = @{
            displayName = $ApiDisplayName
            path        = $ApiId
            protocols   = @("https")
            description = "Azure AI Foundry Agent Gateway — single endpoint for all agent invocations."
        }
    } | ConvertTo-Json -Depth 5

    try {
        Invoke-ApimRest "PUT" "$BaseUri/apis/$($ApiId)?$ApiVersion" $apiBody | Out-Null
        Write-OK "API '$ApiId' created."
    } catch {
        Write-Fail "Failed to create API: $_"
    }
}

# ── Step 5: Create Operation ───────────────────────────────────────────────────
Write-Step "5" "Operation: $OperationId  →  POST $OperationPath"
try {
    Invoke-ApimRest "GET" "$BaseUri/apis/$ApiId/operations/$($OperationId)?$ApiVersion" | Out-Null
    Write-Skip "Operation '$OperationId' already exists — skipping creation."
} catch {
    $opBody = @{
        properties = @{
            displayName = "Invoke Agent"
            method      = "POST"
            urlTemplate = $OperationPath
            description = "Invoke an Azure AI Foundry agent. Handles all 5 Foundry API calls internally. Required fields: foundryName, projectId, agentName, message."
        }
    } | ConvertTo-Json -Depth 5

    try {
        Invoke-ApimRest "PUT" "$BaseUri/apis/$ApiId/operations/$($OperationId)?$ApiVersion" $opBody | Out-Null
        Write-OK "Operation '$OperationId' created at POST $OperationPath."
    } catch {
        Write-Fail "Failed to create operation: $_"
    }
}

# ── Step 6: Apply Operation Policy ────────────────────────────────────────────
Write-Step "6" "Operation Policy → {{$NamedValueId}}"
$policyXml = (Get-Content $OperationPolicyFile -Raw) -replace "\{\{agent-map-devops\}\}", "{{$NamedValueId}}"
$policyBody = @{
    properties = @{
        value  = $policyXml
        format = "rawxml"
    }
} | ConvertTo-Json -Depth 5

try {
    Invoke-ApimRest "PUT" "$BaseUri/apis/$ApiId/operations/$OperationId/policies/policy?$ApiVersion" $policyBody | Out-Null
    Write-OK "Operation Policy applied with Named Value reference: {{$NamedValueId}}"
} catch {
    Write-Fail "Failed to apply operation policy: $_"
}

# ── Summary ────────────────────────────────────────────────────────────────────
Write-Host "`n$("="*60)" -ForegroundColor Green
Write-Host "  ✅ Deployment complete." -ForegroundColor Green
Write-Host "  Endpoint: POST https://$ServiceName.azure-api.net/$ApiId$OperationPath" -ForegroundColor Green
Write-Host "  Agents  : $AgentKeys" -ForegroundColor Green
Write-Host "$("="*60)`n" -ForegroundColor Green
