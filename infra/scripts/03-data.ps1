#Requires -Version 7.3
<#
.SYNOPSIS
    Step 03 - Storage (keyless), PostgreSQL Flexible Server (Entra-only), Service Bus (ingest-priority / ingest-bulk).
.DESCRIPTION
    Storage       shared-key access disabled, TLS 1.2, no public blobs; containers raw-docs, config, exports (--auth-mode login).
                  Roles: managed identity + deployer -> Storage Blob Data Contributor.
    PostgreSQL    v16, Entra-only auth (password auth disabled). Entra admins: the managed identity (ServicePrincipal,
                  principal name = identity name - the apps log in as that name) and the deployer. Database 'ragos'.
                  Firewall: Azure services (0.0.0.0) + optionally the deployer's client IP.
    Service Bus   Standard, local (SAS) auth disabled. Queues with dead-lettering, duplicate detection (creation-time only),
                  max delivery count, lock duration.
                  Roles: managed identity -> Azure Service Bus Data Owner (the KEDA azure-servicebus scaler reads queue
                  runtime properties, which requires the Manage right that only Data Owner grants; the same identity sends
                  and receives). Deployer -> Azure Service Bus Data Sender (local discovery CLI).
.EXAMPLE
    ./infra/scripts/03-data.ps1 -Env dev
#>
[CmdletBinding()]
param(
    [string]$Env = 'dev',
    # Applies drift that restarts or resizes the server (Postgres tier/SKU/storage, storage redundancy).
    [switch]$ApplyChanges
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '03 data (storage, PostgreSQL, Service Bus)'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location
$tags = Get-TagArgs -Config $Config
$deployer = Get-DeployerPrincipal
$miName = Get-Output -Config $Config -Name 'identityName' -ProducedBy '02-identity-keyvault.ps1'
$miPrincipalId = Get-Output -Config $Config -Name 'identityPrincipalId' -ProducedBy '02-identity-keyvault.ps1'
$deployerLabel = "deployer $($deployer.Name)"

# ============================================================================================== Storage
Write-Step "Storage account $($n.Storage)"
$st = Ensure-AzResource -Description "storage $($n.Storage)" -Config $Config -SyncTags -ApplyChanges:$ApplyChanges `
    -Show @('storage', 'account', 'show', '-g', $rg, '-n', $n.Storage) `
    -Create (@('storage', 'account', 'create', '-g', $rg, '-n', $n.Storage, '-l', $loc, '--sku', $Config.StorageSku, '--kind', 'StorageV2',
        '--allow-shared-key-access', 'false', '--min-tls-version', 'TLS1_2', '--allow-blob-public-access', 'false', '--https-only', 'true') + $tags) `
    -Update @('storage', 'account', 'update', '-g', $rg, '-n', $n.Storage) -Desired @(
    # All four of these were create-time only, so an account created before a setting was tightened - or one
    # imported from elsewhere - kept the weaker posture and still reported '(exists)'. Only shared-key access
    # was ever brought back, and it was announced with a bare Write-Info that is easy to miss in a long run.
    (New-DesiredProperty -Path 'allowSharedKeyAccess' -Desired $false -Arg '--allow-shared-key-access' -Label 'shared-key access'),
    (New-DesiredProperty -Path 'allowBlobPublicAccess' -Desired $false -Arg '--allow-blob-public-access' -Label 'public blob access'),
    (New-DesiredProperty -Path 'enableHttpsTrafficOnly' -Desired $true -Arg '--https-only' -Label 'https only'),
    (New-DesiredProperty -Path 'minimumTlsVersion' -Desired 'TLS1_2' -Arg '--min-tls-version' -Label 'min TLS'),
    # Redundancy is a billing and durability decision, and LRS<-GRS moves data, so it waits to be asked for.
    (New-DesiredProperty -Path 'sku.name' -Desired $Config.StorageSku -Arg '--sku' -Label 'sku' -Class 'gated')
)
Grant-Role -PrincipalId $miPrincipalId -PrincipalType 'ServicePrincipal' -Role 'Storage Blob Data Contributor' -Scope $st.id -PrincipalLabel $miName
Grant-Role -PrincipalId $deployer.ObjectId -PrincipalType $deployer.PrincipalType -Role 'Storage Blob Data Contributor' -Scope $st.id -PrincipalLabel $deployerLabel

foreach ($container in @('raw-docs', 'config', 'exports')) {
    # 'create' is idempotent; retries cover data-plane role propagation (403 AuthorizationPermissionMismatch).
    # It also reports whether it had to do anything, which is the only way to tell the two cases apart here.
    $made = Invoke-WithRetry -Activity "Create container $container" -MaxAttempts 12 -DelaySeconds 15 `
        -RetryOn '(?i)(AuthorizationPermissionMismatch|AuthorizationFailure|not authorized|403)' -ScriptBlock {
        Invoke-Az @('storage', 'container', 'create', '--account-name', $n.Storage, '--name', $container, '--auth-mode', 'login', '--query', 'created', '-o', 'tsv')
    }
    Write-Ok "container $container ($(("$made".Trim() -eq 'true') ? 'created' : 'exists'))"
}

# ============================================================================================== PostgreSQL
Write-Step "PostgreSQL Flexible Server $($n.Postgres)"
$pg = Get-AzResourceOrNull @('postgres', 'flexible-server', 'show', '-g', $rg, '-n', $n.Postgres)
if (-not $pg) {
    Write-Info 'Creating server (Entra-only auth; typically 5-10 minutes) ...'
    $adminType = ($deployer.PrincipalType -eq 'User') ? 'User' : 'ServicePrincipal'
    $null = Invoke-Az (@('postgres', 'flexible-server', 'create', '-g', $rg, '-n', $n.Postgres, '-l', $loc,
            '--version', [string]$Config.PostgresVersion, '--tier', $Config.PostgresTier, '--sku-name', $Config.PostgresSku,
            '--storage-size', [string]$Config.PostgresStorageGb,
            '--microsoft-entra-auth', 'Enabled', '--password-auth', 'Disabled',
            '--admin-object-id', $deployer.ObjectId, '--admin-display-name', $deployer.Name, '--admin-type', $adminType,
            # 'Enabled', not 'None'. The CLI help claims None "sets the server in public access mode but does
            # not create a firewall rule"; in practice it leaves publicNetworkAccess Disabled, and Azure then
            # refuses every firewall-rule call. Nothing here is VNet-integrated - the Container Apps environment
            # is created without a subnet - so the API, the workers and the bootstrap job all reach this server
            # over its public endpoint. Access is still closed by default: 'Enabled' creates no rules, and the
            # only ones that exist are the two added below.
            '--public-access', 'Enabled', '--yes') + $tags)
    $pg = Invoke-Az @('postgres', 'flexible-server', 'show', '-g', $rg, '-n', $n.Postgres)
    Write-Ok "server $($n.Postgres) (created)"
}
else {
    # Everything below this point - Entra admins, firewall rules, the database - needs a running server. Printing
    # the state and carrying on meant a stopped server failed several calls later with an az error that said
    # nothing about the cause.
    $state = "$(Get-Value $pg 'state')"
    if ($state -and $state -ne 'Ready') {
        throw ("PostgreSQL server $($n.Postgres) is '$state', not 'Ready'. Entra admins, firewall rules and the " +
            "database cannot be configured against it. Start it (az postgres flexible-server start -g $rg -n $($n.Postgres)) " +
            'or wait for the current operation to finish, then re-run this step.')
    }
    if ((Get-Value $pg 'authConfig.passwordAuth') -eq 'Enabled') {
        Write-Warn "Password authentication is enabled on $($n.Postgres); RAG-OS expects Entra-only (az postgres flexible-server update --password-auth Disabled)."
    }
    $null = Sync-AzTags -Description "server $($n.Postgres)" -Config $Config -Resource $pg
    # Sizing is the edit an operator makes because the disk is full or the tier is too small - exactly the change
    # that used to be ignored while the transcript said '(exists)'. Each one restarts the server, so each is gated.
    $drift = Sync-AzResource -Description "server $($n.Postgres)" -Resource $pg -ApplyChanges:$ApplyChanges `
        -Update @('postgres', 'flexible-server', 'update', '-g', $rg, '-n', $n.Postgres) -Desired @(
        # Heals a server created by an earlier run with --public-access None, which left the endpoint disabled
        # and made every firewall-rule call fail. Not gated: without it the workloads cannot reach the database
        # at all, so there is nothing to protect by deferring it.
        (New-DesiredProperty -Path 'network.publicNetworkAccess' -Desired 'Enabled' -Arg '--public-access' -Label 'public network access'),
        (New-DesiredProperty -Path 'storage.storageSizeGb' -Desired $Config.PostgresStorageGb -Arg '--storage-size' -Label 'storage GB' -Class 'gated'),
        (New-DesiredProperty -Path 'sku.tier' -Desired $Config.PostgresTier -Arg '--tier' -Label 'tier' -Class 'gated'),
        (New-DesiredProperty -Path 'sku.name' -Desired $Config.PostgresSku -Arg '--sku-name' -Label 'sku' -Class 'gated'),
        # A major version change is a separate, one-way operation and cannot go backwards at all.
        (New-DesiredProperty -Path 'version' -Desired $Config.PostgresVersion -Label 'major version' -Class 'immutable' `
                -Remediation "Upgrading is one-way and takes the server offline: az postgres flexible-server upgrade -g $rg -n $($n.Postgres) -v $($Config.PostgresVersion). Downgrading needs a new server and a dump/restore.")
    )
    if ($drift.Applied.Count -gt 0) { $pg = Invoke-Az @('postgres', 'flexible-server', 'show', '-g', $rg, '-n', $n.Postgres) }
}
$pgFqdn = Get-Value $pg 'fullyQualifiedDomainName'

Write-Step 'PostgreSQL Entra administrators'
# objectId is flattened in current CLI output; properties.objectId covers older shapes.
$admins = @(Get-AzTsvValues @('postgres', 'flexible-server', 'microsoft-entra-admin', 'list', '-g', $rg, '-s', $n.Postgres,
        '--query', '[].[objectId, properties.objectId][]') | Where-Object { $_ -ne 'None' })
$wanted = @(
    @{ Id = $miPrincipalId; Name = $miName; Type = 'ServicePrincipal' }
    @{ Id = $deployer.ObjectId; Name = $deployer.Name; Type = (($deployer.PrincipalType -eq 'User') ? 'User' : 'ServicePrincipal') }
)
foreach ($a in $wanted) {
    if ($admins -contains $a.Id) { Write-Ok "Entra admin $($a.Name) (exists)"; continue }
    Invoke-WithRetry -Activity "Add Entra admin $($a.Name)" -MaxAttempts 6 -DelaySeconds 20 -RetryOn '(?i)(not found|does not exist|conflict|another operation)' -ScriptBlock {
        $null = Invoke-Az @('postgres', 'flexible-server', 'microsoft-entra-admin', 'create', '-g', $rg, '-s', $n.Postgres,
            '-i', $a.Id, '-u', $a.Name, '-t', $a.Type, '-o', 'none')
    }
    Write-Ok "Entra admin $($a.Name) (created, $($a.Type))"
}

Write-Step 'PostgreSQL firewall + database'
# Rules can only exist on a server with a public endpoint. The reconciliation above asks for one, so reaching
# here without it means the request did not take - and this stops rather than warns, because nothing in this
# deployment is VNet-integrated: without the AllowAzureServices rule below, rag-api, the workers and the
# bootstrap job cannot reach the database at all. Carrying on would let step 03 report success and move the real
# failure to step 08, far from its cause.
# A VNet-integrated deployment is the one case where skipping this section would be correct instead. That does
# not exist yet; whoever adds it should branch here rather than soften this into a warning.
$publicAccess = "$(Get-Value $pg 'network.publicNetworkAccess')"
if ($publicAccess -eq 'Disabled') {
    throw (
        "PostgreSQL server $($n.Postgres) still reports publicNetworkAccess=Disabled after this step asked for " +
        "'Enabled', so no firewall rule can be created on it and nothing will be able to reach it.`n" +
        "  Try again first - this can be a transient control-plane refusal:`n" +
        "    az postgres flexible-server update -g $rg -n $($n.Postgres) --public-access Enabled`n" +
        "  If Azure refuses, the server was created in a networking mode that cannot be switched, and it has to " +
        "be recreated. That is cheap here: the '$($Config.PostgresDatabase)' database is created further down " +
        "this same script and migrations do not run until step 08, so THE SERVER HOLDS NO DATA YET.`n" +
        "    az postgres flexible-server delete -g $rg -n $($n.Postgres) --yes`n" +
        "    ./infra/scripts/provision-all.ps1 -Env $($Config.Env) -From 3"
    )
}
function Set-FirewallRule([string]$RuleName, [string]$Start, [string]$End, [string]$Why) {
    # Read first: 'create' is a PUT, so calling it blind worked, but it mutated the server on every run and
    # printed the same green line whether or not the rule was already exactly right.
    # The rule name is -n here; -s is the server. There is no -r on this command - guessing it is what made
    # step 03 fail six minutes in, on a probe that should never have been able to fail at all.
    $rule = Get-AzResourceOrNull @('postgres', 'flexible-server', 'firewall-rule', 'show', '-g', $rg, '-s', $n.Postgres, '-n', $RuleName)
    $range = "$Start-$End"
    if ($rule -and (Get-Value $rule 'startIpAddress') -eq $Start -and (Get-Value $rule 'endIpAddress') -eq $End) {
        Write-Ok "firewall rule $RuleName ($range) (exists)"
        return
    }
    $null = Invoke-Az @('postgres', 'flexible-server', 'firewall-rule', 'create', '-g', $rg, '-s', $n.Postgres, '-n', $RuleName,
        '--start-ip-address', $Start, '--end-ip-address', $End, '-o', 'none')
    $what = $rule ? "updated: $(Get-Value $rule 'startIpAddress')-$(Get-Value $rule 'endIpAddress')->$range" : "created: $range"
    Write-Ok "firewall rule $RuleName ($what)$(if ($Why) { " - $Why" })"
}
# 0.0.0.0 is Azure's sentinel for "resources deployed in Azure", not an address. It is what lets the Container
# Apps reach the server, so it is not optional while they live outside a VNet.
Set-FirewallRule 'AllowAzureServices' '0.0.0.0' '0.0.0.0' 'lets the Container Apps reach the server'
if ($Config.AllowClientIp) {
    # This rule is only ever for you: psql, the local rag-os CLI, a GUI client. Provisioning itself never
    # connects to the database - the migrations run inside the rag-bootstrap job - so a wrong address here
    # costs you local access and nothing else.
    $ip = $Config.ClientIpAddress
    $source = 'ClientIpAddress in the psd1'
    if (-not $ip) {
        $source = 'auto-detected via api.ipify.org'
        try { $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 10).ToString().Trim() }
        catch { Write-Warn 'Could not detect the public client IP; set ClientIpAddress in the psd1.' }
    }
    if ($ip) { Set-FirewallRule 'AllowDeployerClientIp' $ip $ip "from $source" }
}
elseif (Test-AzResource @('postgres', 'flexible-server', 'firewall-rule', 'show', '-g', $rg, '-s', $n.Postgres, '-n', 'AllowDeployerClientIp', '--query', 'id', '-o', 'tsv')) {
    # Turning AllowClientIp off did nothing to the rule it had created, so the hole stayed open and nothing said
    # so. Removing it is the operator's call - a provisioning script does not delete - but they have to know.
    Write-Warn "AllowClientIp is off, but the firewall rule 'AllowDeployerClientIp' still exists and still allows that address in."
    Write-Info "  Remove it: az postgres flexible-server firewall-rule delete -g $rg -s $($n.Postgres) -n AllowDeployerClientIp --yes"
}
$null = Ensure-AzResource -Description "database $($Config.PostgresDatabase)" `
    -Show @('postgres', 'flexible-server', 'db', 'show', '-g', $rg, '-s', $n.Postgres, '-n', $Config.PostgresDatabase) `
    -Create @('postgres', 'flexible-server', 'db', 'create', '-g', $rg, '-s', $n.Postgres, '-n', $Config.PostgresDatabase)

# The apps authenticate as the managed identity: user name = identity name, password = Entra token (PG_ENTRA_AUTH=true).
$stateDbUrl = "postgresql+psycopg://$($miName)@$($pgFqdn):5432/$($Config.PostgresDatabase)?sslmode=require"

# ============================================================================================== Service Bus
Write-Step "Service Bus namespace $($n.ServiceBus)"
$sb = Ensure-AzResource -Description "Service Bus $($n.ServiceBus)" -Config $Config -SyncTags -ApplyChanges:$ApplyChanges `
    -Show @('servicebus', 'namespace', 'show', '-g', $rg, '-n', $n.ServiceBus) `
    -Create (@('servicebus', 'namespace', 'create', '-g', $rg, '-n', $n.ServiceBus, '-l', $loc, '--sku', $Config.ServiceBusSku,
        '--disable-local-auth', 'true', '--min-tls', '1.2') + $tags) `
    -Update @('servicebus', 'namespace', 'update', '-g', $rg, '-n', $n.ServiceBus) -Desired @(
    (New-DesiredProperty -Path 'disableLocalAuth' -Desired $true -Arg '--disable-local-auth' -Label 'local (SAS) auth disabled'),
    # Standard -> Premium changes the billing model and provisions dedicated capacity; not something a routine
    # re-run should do on its own.
    (New-DesiredProperty -Path 'sku.name' -Desired $Config.ServiceBusSku -Arg '--sku' -Label 'sku' -Class 'gated')
)

foreach ($queue in @('ingest-priority', 'ingest-bulk')) {
    $q = Get-AzResourceOrNull @('servicebus', 'queue', 'show', '-g', $rg, '--namespace-name', $n.ServiceBus, '-n', $queue)
    if (-not $q) {
        $null = Invoke-Az @('servicebus', 'queue', 'create', '-g', $rg, '--namespace-name', $n.ServiceBus, '-n', $queue,
            '--max-delivery-count', [string]$Config.QueueMaxDeliveryCount, '--lock-duration', $Config.QueueLockDuration,
            '--enable-dead-lettering-on-message-expiration', 'true',
            '--enable-duplicate-detection', 'true', '--duplicate-detection-history-time-window', $Config.QueueDuplicateWindow, '-o', 'none')
        Write-Ok "queue $queue (created)"
        continue
    }
    # Compare before writing. This used to be an unconditional PUT on every run under a comment claiming it
    # brought mutable properties to the desired state - it did, but it also overwrote hand-tuning silently and
    # gave the transcript no way to show whether a psd1 edit had taken effect.
    $null = Sync-AzResource -Description "queue $queue" -Resource $q `
        -Update @('servicebus', 'queue', 'update', '-g', $rg, '--namespace-name', $n.ServiceBus, '-n', $queue) -Desired @(
        (New-DesiredProperty -Path 'maxDeliveryCount' -Desired $Config.QueueMaxDeliveryCount -Arg '--max-delivery-count' -Label 'max delivery count'),
        (New-DesiredProperty -Path 'lockDuration' -Desired $Config.QueueLockDuration -Arg '--lock-duration' -Label 'lock duration'),
        (New-DesiredProperty -Path 'deadLetteringOnMessageExpiration' -Desired $true -Arg '--enable-dead-lettering-on-message-expiration' -Label 'dead-letter on expiry'),
        # The window is mutable even though duplicate detection itself is not, but it was only ever passed on
        # create, so editing QueueDuplicateWindow was a no-op.
        (New-DesiredProperty -Path 'duplicateDetectionHistoryTimeWindow' -Desired $Config.QueueDuplicateWindow -Arg '--duplicate-detection-history-time-window' -Label 'duplicate window'),
        (New-DesiredProperty -Path 'requiresDuplicateDetection' -Desired $true -Label 'duplicate detection' -Class 'immutable' `
                -Remediation "Duplicate detection can only be set at creation. Drain the queue, delete it (az servicebus queue delete -g $rg --namespace-name $($n.ServiceBus) -n $queue) and re-run this step.")
    )
}
Grant-Role -PrincipalId $miPrincipalId -PrincipalType 'ServicePrincipal' -Role 'Azure Service Bus Data Owner' -Scope $sb.id -PrincipalLabel $miName
Grant-Role -PrincipalId $deployer.ObjectId -PrincipalType $deployer.PrincipalType -Role 'Azure Service Bus Data Sender' -Scope $sb.id -PrincipalLabel $deployerLabel

Save-Outputs -Config $Config -Values @{
    storageName        = $n.Storage
    storageId          = $st.id
    blobEndpoint       = (Get-Value $st 'primaryEndpoints.blob').TrimEnd('/')
    postgresName       = $n.Postgres
    postgresFqdn       = $pgFqdn
    postgresDatabase   = $Config.PostgresDatabase
    stateDbUrl         = $stateDbUrl
    serviceBusName     = $n.ServiceBus
    serviceBusId       = $sb.id
    serviceBusFqdn     = "$($n.ServiceBus).servicebus.windows.net"
}
Write-Ok 'Data services ready.'
Write-Info "Verify: az postgres flexible-server show -g $rg -n $($n.Postgres) --query '{state:state, auth:authConfig}'"
Write-Info "        az servicebus queue list -g $rg --namespace-name $($n.ServiceBus) --query '[].{name:name, dup:requiresDuplicateDetection}' -o table"
