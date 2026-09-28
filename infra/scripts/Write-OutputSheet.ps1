#Requires -Version 7.3

<#
.SYNOPSIS
    Writes output.txt - the deployment wiring sheet: which Key Vault secret feeds which environment variable in
    which container app, plus every non-secret value an operator needs to run, verify or hand over the deployment.

.DESCRIPTION
    NO SECRET VALUES ARE WRITTEN, BY CONSTRUCTION.

    Secrets live in Key Vault and reach the containers as `keyVaultUrl` references resolved by the managed
    identity. This sheet records the *mapping* - env var <- secret name <- keyvaultref URI - which is the part
    that is genuinely hard to reconstruct by hand, and never the value, which the sheet tells you how to read
    from the vault when you actually need it.

    Three defences keep that promise:
      1. The live read projects only `name`, `keyVaultUrl`, `value` and `secretRef` with a server-side --query, so
         a secret value is never even requested from Azure.
      2. Any env var whose NAME looks like a credential but which carries an inline value is redacted and flagged,
         because an inline secret is itself a deployment defect (09-smoke.ps1 asserts there are none).
      3. Assert-NoSecretValues scans the finished text for credential-shaped strings and refuses to write the file
         if it finds one. A false positive is cheap; a leaked secret in a plain-text file is not.

    Sources: infra/env/<env>.outputs.json (written by steps 00-07, "ids and endpoints only - never secrets"),
    infra/containerapps/*.yaml.tmpl (the declared secret wiring) and, unless -FromTemplates, the live deployment.

.PARAMETER FromTemplates
    Skip Azure entirely and derive the wiring from the templates alone. Useful before the apps exist, or offline.
    Per-workload environment variables cannot be listed this way, because the templates carry a {{COMMON_ENV}}
    token that only 07-container-apps.ps1 expands.

.EXAMPLE
    ./infra/scripts/Write-OutputSheet.ps1 -Env dev
    ./infra/scripts/Write-OutputSheet.ps1 -Env dev -FromTemplates -Path ./wiring.txt
#>
[Diagnostics.CodeAnalysis.SuppressMessageAttribute('PSAvoidUsingWriteHost', '', Justification = 'Interactive console script')]
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    [string]$Path,
    [switch]$FromTemplates
)
. (Join-Path $PSScriptRoot 'common.ps1')

$Config = if ($FromTemplates) { Import-RagOsConfig -Env $Env } else {
    Initialize-RagOsScript -Env $Env -Title 'Wiring sheet'
}
$o = Get-Outputs -Config $Config
$n = $Config.Names
$rg = $n.ResourceGroup
if (-not $Path) { $Path = Join-Path $Config.RepoRoot 'output.txt' }

# The same seven workloads 07-container-apps.ps1 deploys, in the same order.
$workloads = @(
    @{ Name = 'rag-embed-query'; Job = $false }
    @{ Name = 'rag-embed-ingest'; Job = $false }
    @{ Name = 'rag-api'; Job = $false }
    @{ Name = 'rag-ingest-worker'; Job = $false }
    @{ Name = 'rag-chat-ui'; Job = $false }
    @{ Name = 'rag-scheduler'; Job = $true }
    @{ Name = 'rag-bootstrap'; Job = $true }
)

# An env var NAME that ends this way must never carry an inline value. Matches 07-container-apps.ps1's $reserved check.
$script:CredentialNamePattern = '(?i)(KEY|SECRET|PASSWORD|CONNECTION_STRING|TOKEN|CREDENTIAL)$'

function Get-TemplateWiring {
    <#
    .SYNOPSIS  Reads infra/containerapps/<name>.yaml.tmpl -> @{ Secrets = @(@{Name;Secret}); Refs = @(@{Var;Secret}) }.
    .DESCRIPTION
        A deliberately small line reader rather than a YAML parser: these files are generated-by-hand templates
        with {{TOKEN}} placeholders that no YAML parser will accept, and the two shapes we need are fixed.
    #>
    param([Parameter(Mandatory)][string]$TemplatePath)
    $secrets = [System.Collections.Generic.List[object]]::new()
    $refs = [System.Collections.Generic.List[object]]::new()
    $pendingSecret = $null
    $pendingVar = $null
    foreach ($line in (Get-Content -LiteralPath $TemplatePath)) {
        if ($line -match '^\s*-\s*name:\s*(?<v>[^\s#]+)\s*$') {
            $pendingSecret = $Matches['v']
            $pendingVar = $Matches['v']
            continue
        }
        if ($line -match '^\s*keyVaultUrl:\s*"?[^"]*?/secrets/(?<s>[A-Za-z0-9-]+)"?\s*$' -and $pendingSecret) {
            $secrets.Add([pscustomobject]@{ Name = $pendingSecret; Secret = $Matches['s'] })
            $pendingSecret = $null
            continue
        }
        if ($line -match '^\s*secretRef:\s*(?<s>[A-Za-z0-9-]+)\s*$' -and $pendingVar) {
            $refs.Add([pscustomobject]@{ Var = $pendingVar; Secret = $Matches['s'] })
            $pendingVar = $null
        }
    }
    return @{ Secrets = $secrets; Refs = $refs }
}

function Get-LiveWiring {
    <# .SYNOPSIS  The deployed truth for one workload, projected so no secret value is ever requested. #>
    param([Parameter(Mandatory)][hashtable]$Workload)
    $group = $Workload.Job ? @('containerapp', 'job') : @('containerapp')
    $query = '{secrets: properties.configuration.secrets[].{name: name, keyVaultUrl: keyVaultUrl}, ' +
    'env: properties.template.containers[0].env[].{name: name, value: value, secretRef: secretRef}}'
    return (Invoke-Az ($group + @('show', '-g', $rg, '-n', $Workload.Name, '--query', $query)) -AllowNotFound)
}

function Format-Table2 {
    <# .SYNOPSIS  Fixed-width plain-text table. Format-Table would wrap or truncate these long URIs. #>
    param([Parameter(Mandatory)][string[]]$Headers, [Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    if ($Rows.Count -eq 0) { return @('  (none)') }
    # A ragged row means a cell was lost on the way in - inside an array literal 'a' + "$b" parses as TWO elements,
    # which silently truncates a cell rather than failing. Catch it here instead of shipping a half-written sheet.
    foreach ($r in $Rows) {
        if (@($r).Count -ne $Headers.Count) {
            throw "Row has $(@($r).Count) cells but there are $($Headers.Count) headers: $(@($r) -join ' | ')"
        }
    }
    $widths = @(0) * $Headers.Count
    foreach ($i in 0..($Headers.Count - 1)) {
        $widths[$i] = ((@($Headers[$i]) + @($Rows | ForEach-Object { "$($_[$i])" })) | Measure-Object -Property Length -Maximum).Maximum
    }
    $line = {
        param($cells)
        $padded = for ($i = 0; $i -lt $Headers.Count; $i++) { "$($cells[$i])".PadRight($widths[$i]) }
        ('  ' + ($padded -join '  ')).TrimEnd()
    }
    $out = [System.Collections.Generic.List[string]]::new()
    $out.Add((& $line $Headers))
    $out.Add('  ' + (($widths | ForEach-Object { '-' * $_ }) -join '  '))
    foreach ($r in $Rows) { $out.Add((& $line $r)) }
    return $out
}

function Assert-NoSecretValues {
    <#
    .SYNOPSIS  Refuses to write the sheet if it contains anything credential-shaped. The last line of defence.
    .DESCRIPTION
        Patterns, in order: an App Insights connection string, a long base64 run (dev-jwt-signing-key is 64 random
        bytes = 88 base64 characters), a JWT, a storage/Service Bus key assignment, and a URL with a SAS token.
    #>
    param([Parameter(Mandatory)][string]$Text)
    $patterns = [ordered]@{
        'Application Insights connection string' = '(?i)InstrumentationKey=[0-9a-f]{8}-'
        'long base64 run (random secret?)'       = '[A-Za-z0-9+/]{60,}={0,2}'
        'JWT'                                    = 'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.'
        'inline key assignment'                  = '(?im)^\s*\S*(AccountKey|SharedAccessKey|Password)\s*=\s*\S'
        'SAS token'                              = '(?i)[?&]sig=[A-Za-z0-9%]{10,}'
    }
    foreach ($name in $patterns.Keys) {
        $hit = [regex]::Match($Text, $patterns[$name])
        if ($hit.Success) {
            $where = ($Text.Substring(0, $hit.Index) -split "`n").Count
            throw ("Refusing to write $Path : it looks like it contains a secret value ($name, line $where). " +
                'This sheet must carry references only - fix the generator, do not relax this check.')
        }
    }
}

function Get-Out {
    <# .SYNOPSIS  An outputs.json value, or a placeholder naming the step that would have produced it. #>
    param([Parameter(Mandatory)][string]$Name, [string]$Step = '')
    if ($o.ContainsKey($Name) -and $null -ne $o[$Name] -and "$($o[$Name])" -ne '') { return "$($o[$Name])" }
    return $Step ? "(not provisioned yet - step $Step)" : '(not set)'
}

# ============================================================================================== gather
Write-Step "Building the wiring sheet for env=$Env"
$declared = [ordered]@{}
foreach ($w in $workloads) {
    $tmpl = Join-Path $Config.InfraDir "containerapps/$($w.Name).yaml.tmpl"
    if (Test-Path -LiteralPath $tmpl) { $declared[$w.Name] = Get-TemplateWiring -TemplatePath $tmpl }
}
$live = [ordered]@{}
if (-not $FromTemplates) {
    foreach ($w in $workloads) {
        $result = Get-LiveWiring -Workload $w
        if ($null -eq $result) {
            # $null covers two different situations and the sheet must not claim to know which: the workload
            # genuinely does not exist, or az failed in a way that read as absence (an expired login reports
            # "was not found in the directory", which the not-found pattern matches). Either way the rows below
            # are declared-only, so DRIFT cannot be ruled out for this workload.
            Write-Warn "$($w.Name): no deployment could be read - either it is not deployed yet, or az could not report it."
            Write-Info 'Its rows show declared wiring only; drift is neither confirmed nor ruled out. Re-run with -Verbose to see what az said.'
            continue
        }
        $live[$w.Name] = $result
    }
}

# ---- secret wiring: one row per (workload, env var). Declared from the templates, verified against the deployment.
$secretRows = [System.Collections.Generic.List[object]]::new()
$vaultUri = Get-Out 'keyVaultUri' '02'
foreach ($name in $declared.Keys) {
    $byName = @{}
    foreach ($s in $declared[$name].Secrets) { $byName[$s.Name] = $s.Secret }
    foreach ($r in $declared[$name].Refs) {
        $secret = $byName.ContainsKey($r.Secret) ? $byName[$r.Secret] : $r.Secret
        $state = 'declared'
        if ($live.Contains($name)) {
            $liveSecrets = @(Get-Value $live[$name] 'secrets')
            $match = $liveSecrets | Where-Object { $_ -and $_.name -eq $r.Secret -and "$($_.keyVaultUrl)" -match "/secrets/$secret`$" }
            $state = $match ? 'deployed' : 'DRIFT'
        }
        $secretRows.Add(@($name, $r.Var, $secret, "$vaultUri/secrets/$secret", $state))
    }
}

# ---- non-secret env vars, live only ({{COMMON_ENV}} is expanded by 07, so the templates cannot answer this)
$envRows = [System.Collections.Generic.List[object]]::new()
$inlineSecretWarnings = [System.Collections.Generic.List[string]]::new()
foreach ($name in $live.Keys) {
    foreach ($e in @(Get-Value $live[$name] 'env')) {
        if (-not $e -or -not $e.name) { continue }
        if ($e.secretRef) { continue }                       # already covered by the secret table above
        $value = "$($e.value)"
        if ($e.name -match $script:CredentialNamePattern -and $value) {
            $inlineSecretWarnings.Add("$name/$($e.name) carries an inline value")
            $value = '<redacted - see the warning below>'
        }
        $envRows.Add(@($name, $e.name, $value))
    }
}

# ============================================================================================== render
$now = Get-Date -Format 'yyyy-MM-dd HH:mm:ss zzz'
$chatFqdn = Get-Out 'chatUiFqdn' '07'
$chatUrl = ($chatFqdn -like '(*') ? $chatFqdn : "https://$chatFqdn"
$L = [System.Collections.Generic.List[string]]::new()
$add = { param([string]$s = '') $L.Add($s) }
$section = { param([string]$t) $L.Add(''); $L.Add(('=' * 100)); $L.Add($t); $L.Add(('=' * 100)) }

& $add "RAG-OS deployment wiring sheet"
& $add "environment : $Env"
& $add "subscription: $(Get-Out 'subscriptionId' '00')"
& $add "tenant      : $(Get-Out 'tenantId' '00')"
& $add "resourceGroup: $rg   region: $($Config.Location)"
& $add "generated   : $now  by infra/scripts/Write-OutputSheet.ps1$($FromTemplates ? ' (-FromTemplates: declared wiring only)' : '')"
& $add
& $add 'THIS FILE CONTAINS NO SECRET VALUES. It maps each environment variable to the Key Vault secret that'
& $add 'supplies it. Section 6 shows how to read a value when you genuinely need one. Do not commit this file.'

& $section '1. SECRET WIRING  -  env var <- Key Vault secret'
& $add
& $add 'Container Apps resolves each keyvaultref with the user-assigned managed identity, which holds Key Vault'
& $add 'Secrets User on the vault. The value is never stored in the app, in the YAML, or here.'
& $add
(Format-Table2 -Headers @('WORKLOAD', 'ENVIRONMENT VARIABLE', 'KEY VAULT SECRET', 'KEYVAULTREF URI', 'STATE') -Rows $secretRows) | ForEach-Object { & $add $_ }
& $add
& $add "  identity: $(Get-Out 'identityName' '02')  (clientId $(Get-Out 'identityClientId' '02'))"
& $add "  vault   : $(Get-Out 'keyVaultName' '02')  (RBAC mode, purge protection on)"
if ($secretRows | Where-Object { $_[4] -eq 'DRIFT' }) {
    & $add
    & $add '  DRIFT means the deployed app does not reference the secret its template declares. Re-run step 07.'
}
& $add
& $add '  Not wired as container secrets, by design:'
& $add '    kv://sharepoint-client-secret, kv://partner-sftp-key  - named in config/sources/sources.yaml and'
& $add '      resolved at runtime through KEY_VAULT_URL, so adding a source needs no redeploy.'
& $add '    foundry-api-key  - not created. Foundry and Azure OpenAI are called with managed-identity tokens.'
& $add '    rag-chat-ui, rag-embed-query, rag-embed-ingest  - hold no secrets at all (09-smoke.ps1 asserts this).'

& $section '2. RESOURCES'
& $add
$resourceRows = @(
    @('Resource group', $rg, ''),
    @('Log Analytics', (Get-Out 'logAnalyticsName' '01'), (Get-Out 'logAnalyticsCustomerId' '01')),
    @('Application Insights', (Get-Out 'appInsightsName' '01'), 'connection string -> Key Vault'),
    @('Managed identity', (Get-Out 'identityName' '02'), (Get-Out 'identityClientId' '02')),
    @('Key Vault', (Get-Out 'keyVaultName' '02'), $vaultUri),
    @('Storage', (Get-Out 'storageName' '03'), (Get-Out 'blobEndpoint' '03')),
    @('PostgreSQL', (Get-Out 'postgresName' '03'), (Get-Out 'postgresFqdn' '03')),
    @('Service Bus', (Get-Out 'serviceBusName' '03'), (Get-Out 'serviceBusFqdn' '03')),
    @('AI Search', (Get-Out 'searchName' '04'), (Get-Out 'searchEndpoint' '04')),
    @('Foundry', (Get-Out 'foundryName' '05'), (Get-Out 'foundryEndpoint' '05')),
    @('Foundry project', (Get-Out 'foundryProject' '05'), (Get-Out 'aoaiEndpoint' '05')),
    @('Container registry', (Get-Out 'acrName' '06'), (Get-Out 'acrLoginServer' '06')),
    @('Container Apps env', (Get-Out 'containerEnvName' '07'), (Get-Out 'containerEnvDomain' '07'))
)
(Format-Table2 -Headers @('RESOURCE', 'NAME', 'ENDPOINT / ID') -Rows $resourceRows) | ForEach-Object { & $add $_ }
& $add
& $add "  None of these accept a key or a password: storage shared keys, Service Bus local auth, AI Search API keys,"
& $add "  the ACR admin user and PostgreSQL password auth are all disabled. Access is by managed identity only."

& $section '3. URLS'
& $add
& $add "  Chat UI          $chatUrl"
& $add "  Admin console    $chatUrl/admin"
& $add "  OpenAPI          $chatUrl/api/docs"
& $add "  Health           $chatUrl/api/healthz"
& $add "  Readiness        $chatUrl/api/readyz"
if ($Config.DevAuthEnabled) {
    & $add "  Dev embed host   $chatUrl/dev/embed-host      (DevAuthEnabled = `$true - turn this off for production)"
}
& $add "  rag-api is INTERNAL: http://rag-api inside $(Get-Out 'containerEnvName' '07') and nowhere else."

& $section '4. SIGN-IN (Microsoft Entra ID)'
& $add
$authRows = @(
    @('ENTRA_TENANT_ID', "$($Config.EntraTenantId)", 'directory that issues tokens'),
    @('ENTRA_CLIENT_ID', "$($Config.EntraClientId)", 'app registration used by the chat UI (MSAL)'),
    @('ENTRA_AUDIENCE', "$($Config.EntraAudience)", 'what the token must carry in `aud` - see the note below'),
    @('ENTRA_API_SCOPE', "$($Config.EntraApiScope)", 'scope MSAL requests, e.g. api://<client-id>/access_as_user'),
    @('DEV_AUTH_ENABLED', "$([bool]$Config.DevAuthEnabled)", 'demo principals + dev token endpoint; false in production')
)
(Format-Table2 -Headers @('SETTING', 'VALUE', 'MEANING') -Rows $authRows) | ForEach-Object { & $add $_ }
& $add
& $add "  The app registration must expose the scope above, or sign-in fails with AADSTS65005. Reconcile it with:"
& $add "    ./infra/scripts/Set-EntraAppRegistration.ps1 -Env $($Config.Env)"
& $add
& $add '  ENTRA_AUDIENCE depends on the registration, because api.requestedAccessTokenVersion decides the token'
& $add '  format: version 2 stamps aud = the bare app id, version 1 (which is what null means) stamps the'
& $add '  api://<app-id> URI that was requested. rag-api accepts both spellings.'
& $add
& $add "  The app registration's SPA redirect URI must be exactly:"
& $add "    $chatUrl/auth/callback"
& $add
& $add '  These are identifiers, not credentials - the client id and scope are published to the browser by'
& $add "  $chatUrl/api/public-config so MSAL can start. Sign-in itself is code + PKCE, with no client secret."
if (-not $Config.EntraTenantId -and -not $Config.DevAuthEnabled) {
    & $add
    & $add '  WARNING: neither Entra nor dev auth is configured. Nobody can sign in. See Deployment.md section 9 - Signing people in with Microsoft Entra ID.'
}

if ($envRows.Count -gt 0) {
    & $section '5. ENVIRONMENT VARIABLES PER WORKLOAD  (non-secret, as deployed)'
    & $add
    (Format-Table2 -Headers @('WORKLOAD', 'VARIABLE', 'VALUE') -Rows $envRows) | ForEach-Object { & $add $_ }
    if ($inlineSecretWarnings.Count -gt 0) {
        & $add
        & $add '  WARNING - inline values on credential-shaped variables (they belong in Key Vault):'
        foreach ($w in $inlineSecretWarnings) { & $add "    $w" }
    }
}
elseif ($FromTemplates) {
    & $section '5. ENVIRONMENT VARIABLES PER WORKLOAD'
    & $add
    & $add '  Not available with -FromTemplates: the templates carry a {{COMMON_ENV}} token that only'
    & $add '  07-container-apps.ps1 expands. Re-run this script against the deployment to list them.'
}

& $section '6. READING AND ROTATING A SECRET'
& $add
& $add '  Read one value (you must hold Key Vault Secrets Officer/User; the read is logged):'
& $add "    az keyvault secret show --vault-name $(Get-Out 'keyVaultName' '02') --name <secret> --query value -o tsv"
& $add
& $add '  Rotate the generated ones, then restart revisions so the new version is picked up:'
& $add "    ./infra/scripts/02-identity-keyvault.ps1 -Env $Env -RotateSecrets"
& $add "    az containerapp revision restart -g $rg -n rag-api --revision <active>"
& $add '  Container Apps caches a Key Vault reference for up to ~30 minutes, so a rotation is not live until the'
& $add '  revision restarts. Deployment.md section 4 (Key Vault secrets) has the full procedure.'
& $add
& $add "  Regenerate this sheet at any time:  ./infra/scripts/Write-OutputSheet.ps1 -Env $Env"
& $add

$text = ($L -join [Environment]::NewLine)
Assert-NoSecretValues -Text $text
Set-Content -LiteralPath $Path -Value $text -Encoding utf8NoBOM
Write-Ok "Wiring sheet written: $Path"
Write-Info "$($secretRows.Count) secret mapping(s), $($envRows.Count) environment variable(s), no secret values."
