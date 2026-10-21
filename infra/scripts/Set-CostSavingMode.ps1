#Requires -Version 7.3

<#
.SYNOPSIS
    Cut the idle Azure bill (ScaleDown or Hibernate) and put everything back exactly as it was (Resume).
.DESCRIPTION
    What keeps billing while nobody uses RAG-OS, and what each mode does about it:

      Component                          ScaleDown (still reachable)   Hibernate (unavailable)
      Dedicated profiles query, ingest   min nodes -> 0                DELETED - nodes and the Dedicated plan management
                                                                       fee (~$0.10/h) both stop; Resume re-adds them
                                                                       (-KeepDedicatedProfiles: min nodes -> 0 only)
      Container apps                     min replicas -> 0             stopped: no replicas, traffic and KEDA cannot wake them
      Scheduled jobs (rag-scheduler)     hourly (-ScaleDownCron)       once a year - a job cannot be stopped, only re-scheduled
      AI Search                          1 replica                     1 replica           (-KeepSearchReplicas: unchanged)
      PostgreSQL Flexible Server         running                       stopped             (-KeepPostgres: running)

    Deleting a profile needs it empty, stopped apps included, so Hibernate first moves the stopped apps and the jobs
    on it to the built-in Consumption profile - each only if its size is a Consumption pair (<= 4 vCPU, memory =
    2 x cpu). A workload that cannot move keeps its profile, and is named. Resume re-adds each deleted profile with
    its recorded type and node counts, moves every app and job back, and only then starts them: allow 5-10 minutes
    more for that than for a Resume that kept the profiles.

    A tier running on the Consumption plan (QueryPlan / IngestPlan = 'Consumption') has no Dedicated profile and no
    node floor to lower; ScaleDown and Hibernate still remove its minimum replicas, which bill at the idle rate.

    Not touched, because there is no idle compute to remove: the Foundry model deployments (billed per token),
    Service Bus, the registry, Key Vault, Storage and Log Analytics. Their base fees continue, as does PostgreSQL
    storage.

    Before the first change, the ACTUAL state - not the psd1 - is written to infra/env/<env>.costsaver.json. It is
    never overwritten while cost saving is on: re-running a mode, or switching from one to the other, keeps the
    original. So Resume brings back what was really running, including a temporary change such as extra Search
    replicas for a backfill. Resume archives the file as <env>.costsaver.<timestamp>.resumed.json.

    Caveats:
      - Azure starts a stopped PostgreSQL server again after 7 days. Re-run -Mode Hibernate to stop it again; it
        is safe to repeat and the snapshot is kept.
      - 07-container-apps.ps1 re-applies the psd1 replicas, nodes and cron, and re-adds deleted Dedicated profiles
        (with them, the management fee). After a deploy during cost saving, re-run the mode. -Mode Status names
        anything that has been undone.
      - ScaleDown: the first request after an idle spell waits for a node and the TEI pool to start (minutes).
.PARAMETER Mode
    Status (default, read-only), ScaleDown, Hibernate or Resume.
.PARAMETER Force
    Skip the confirmation prompt (scheduled runs). It does NOT override the running-migration guard.
.PARAMETER AllowDuringBootstrap
    Hibernate even while a rag-bootstrap execution (database migration) is running. Never put this in a schedule.
.PARAMETER KeepDedicatedProfiles
    Hibernate without deleting the Dedicated profiles: their min nodes go to 0, the management fee keeps billing,
    and Resume is a few minutes quicker.
.PARAMETER UseConfigDefaults
    Resume without a snapshot: restore the replicas, nodes, cron, profile assignments and Search replicas declared
    in the psd1 (QueryPlan / IngestPlan decide the profiles), and start every app.
.EXAMPLE
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev                           # what is running and what it costs
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev -Mode ScaleDown           # evenings: reachable, with cold starts
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev -Mode Hibernate -WhatIf   # show what would change
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev -Mode Hibernate           # weekends: everything stopped
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev -Mode Resume              # back to exactly what it was
    ./infra/scripts/Set-CostSavingMode.ps1 -Env dev -Mode Resume -UseConfigDefaults   # no snapshot: psd1 values
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$Env = 'dev',
    [ValidateSet('Status', 'ScaleDown', 'Hibernate', 'Resume')][string]$Mode = 'Status',
    [switch]$Force,
    [switch]$AllowDuringBootstrap,
    [switch]$KeepSearchReplicas,
    [switch]$KeepPostgres,
    [switch]$KeepDedicatedProfiles,
    [switch]$NoWait,
    [switch]$UseConfigDefaults,
    [ValidatePattern('^\S+( \S+){4}$')][string]$ScaleDownCron = '0 * * * *'
)
# -WhatIf is honoured by this script's own guard (Invoke-Change), not by PowerShell's. Left on, it would also reach
# the Remove-Item that cleans up after every Invoke-Az call and leak a temp file per read.
$script:DryRun = [bool]$WhatIfPreference
$WhatIfPreference = $false
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title "Cost saving ($Mode)"
$n = $Config.Names
$rg = $n.ResourceGroup
# Start and stop are ARM actions on the app; the Azure CLI has no 'containerapp stop'.
$script:AppsUrl = "https://management.azure.com$($Config.ResourceGroupId)/providers/Microsoft.App/containerApps"
$script:AppsApiVersion = '2025-01-01'
$SnapshotPath = Join-Path (Split-Path -Parent $Config.OutputsPath) "$($Config.EnvName).costsaver.json"
$HibernateCron = '0 0 1 1 *'
# Front door first, so nothing is left calling a backend that has already gone. Resume starts them in reverse.
$StopOrder = @('rag-chat-ui', 'rag-api', 'rag-ingest-worker', 'rag-embed-query', 'rag-embed-ingest')

# ================================================================================================ helpers
function Invoke-Change {
    <# .SYNOPSIS  Runs one change and reports it, or only describes it under -WhatIf. -Quiet: the action reports its own outcome. #>
    param([Parameter(Mandatory)][string]$Description, [Parameter(Mandatory)][scriptblock]$Action, [switch]$Quiet)
    if ($script:DryRun) { Write-Info "[what-if] $Description"; return }
    $null = & $Action
    if (-not $Quiet) { Write-Ok $Description }
}

function Get-Keys {
    <# .SYNOPSIS  The keys of a dictionary that may be absent - an older snapshot lacks the newer sections. #>
    param([AllowNull()]$Dictionary)
    if ($Dictionary -isnot [System.Collections.IDictionary]) { return @() }
    return @($Dictionary.Keys)
}

function Test-AppRunning {
    <# .SYNOPSIS  Anything but Stopped/Stopping counts as running: Progressing and Ready are on their way up. #>
    param([AllowNull()][AllowEmptyString()][string]$Status)
    return ($Status -notin @('Stopped', 'Stopping'))
}

function Sort-AppNames {
    <# .SYNOPSIS  Known apps in stop order, then any app this script does not know about - never silently skipped. #>
    param([AllowEmptyCollection()][string[]]$Names = @(), [switch]$Reverse)
    $known = @($StopOrder | Where-Object { $Names -contains $_ })
    $other = @($Names | Where-Object { $StopOrder -notcontains $_ } | Sort-Object)
    $all = @($known + $other)
    if ($Reverse) { [array]::Reverse($all) }
    return $all
}

function Format-AppState {
    param([AllowNull()]$App)
    if (-not $App) { return '(not deployed)' }
    $max = Get-Value $App 'maxReplicas'
    $profileName = Get-Value $App 'workloadProfileName'
    return "$(Get-Value $App 'runningStatus'), min $(Get-Value $App 'minReplicas')$(if ($null -ne $max) { ", max $max" })$(if ($profileName) { ", on $profileName" })"
}

function Get-ReplicaSize {
    <# .SYNOPSIS  Total CPU and memory (Gi) one replica of an app or job requests, summed over its containers. #>
    param([Parameter(Mandatory)]$Resource)
    $cpu = 0.0
    $memoryGi = 0.0
    foreach ($container in @(Get-Value $Resource 'properties.template.containers')) {
        if (-not $container) { continue }
        $cpu += [double](Get-Value $container 'resources.cpu')
        if ("$(Get-Value $container 'resources.memory')" -match '^(\d+(\.\d+)?)Gi$') { $memoryGi += [double]$Matches[1] }
    }
    return @{ cpu = $cpu; memoryGi = $memoryGi }
}

# ================================================================================================ reading state
function Get-AppStates {
    $body = Invoke-AzRest -Method get -Url "$($script:AppsUrl)?api-version=$($script:AppsApiVersion)"
    $apps = [ordered]@{}
    foreach ($app in @(Get-Value $body 'value')) {
        if (-not $app) { continue }
        $max = Get-Value $app 'properties.template.scale.maxReplicas'
        $size = Get-ReplicaSize -Resource $app
        $apps[(Get-Value $app 'name')] = [ordered]@{
            runningStatus       = "$(Get-Value $app 'properties.runningStatus')"
            minReplicas         = [int](Get-Value $app 'properties.template.scale.minReplicas')   # unset means 0
            maxReplicas         = ($null -eq $max) ? $null : [int]$max
            workloadProfileName = "$(Get-Value $app 'properties.workloadProfileName')"
            cpu                 = $size.cpu
            memoryGi            = $size.memoryGi
        }
    }
    return $apps
}

function Get-JobStates {
    <# .SYNOPSIS  Cron of each SCHEDULED job - the only kind that runs (and bills) by itself. #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    $jobs = [ordered]@{}
    foreach ($row in $Rows) {
        if ((Get-Value $row 'properties.configuration.triggerType') -ne 'Schedule') { continue }
        $jobs[(Get-Value $row 'name')] = "$(Get-Value $row 'properties.configuration.scheduleTriggerConfig.cronExpression')"
    }
    return $jobs
}

function Get-JobProfiles {
    <#
    .SYNOPSIS  Profile and size of EVERY job, manual ones included.
    .DESCRIPTION
        rag-bootstrap is a manual job and has no cron to pause, but it is assigned to the 'ingest' profile all the
        same - and a profile cannot be deleted while anything is assigned to it.
    #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][object[]]$Rows)
    $jobs = [ordered]@{}
    foreach ($row in $Rows) {
        $size = Get-ReplicaSize -Resource $row
        $jobs[(Get-Value $row 'name')] = [ordered]@{
            workloadProfileName = "$(Get-Value $row 'properties.workloadProfileName')"
            cpu                 = $size.cpu
            memoryGi            = $size.memoryGi
        }
    }
    return $jobs
}

function Get-ProfileStates {
    $profiles = [ordered]@{}
    $list = Invoke-Az @('containerapp', 'env', 'show', '-g', $rg, '-n', $n.ContainerEnv, '--query', 'properties.workloadProfiles')
    foreach ($p in @($list)) {
        if (-not $p) { continue }
        # Consumption profiles, the serverless GPU one included, have no node floor: they bill per running replica.
        $type = "$(Get-Value $p 'workloadProfileType')"
        if ($type -like 'Consumption*') { continue }
        $profiles[(Get-Value $p 'name')] = [ordered]@{
            type         = $type
            minimumCount = [int](Get-Value $p 'minimumCount')
            maximumCount = [int](Get-Value $p 'maximumCount')
        }
    }
    return $profiles
}

function Get-SearchReplicas {
    $value = Invoke-Az @('search', 'service', 'show', '-g', $rg, '-n', $n.Search, '--query', 'replicaCount', '-o', 'tsv') -AllowNotFound
    if ("$value" -eq '') { return $null }
    return [int]$value
}

function Get-PostgresState {
    return "$(Invoke-Az @('postgres', 'flexible-server', 'show', '-g', $rg, '-n', $n.Postgres, '--query', 'state', '-o', 'tsv') -AllowNotFound)"
}

function Get-CurrentState {
    $jobRows = @(@(Invoke-Az @('containerapp', 'job', 'list', '-g', $rg)) | Where-Object { $_ })
    return [ordered]@{
        apps           = Get-AppStates
        jobs           = Get-JobStates -Rows $jobRows
        jobProfiles    = Get-JobProfiles -Rows $jobRows
        profiles       = Get-ProfileStates
        searchReplicas = Get-SearchReplicas
        postgresState  = Get-PostgresState
    }
}

function Get-ConfigTarget {
    <# .SYNOPSIS  The steady state the psd1 declares, in the same shape as a snapshot. Mirrors the tokens step 07 renders. #>
    param([Parameter(Mandatory)]$Current)
    $outputs = Get-Outputs -Config $Config
    $gpu = [bool]$outputs['gpuEnabled']
    $declared = @{
        'rag-api'           = @($Config.ApiMinReplicas, $Config.ApiMaxReplicas)
        'rag-chat-ui'       = @($Config.ChatUiMinReplicas, $Config.ChatUiMaxReplicas)
        'rag-embed-query'   = @($Config.EmbedQueryMinReplicas, $Config.EmbedQueryMaxReplicas)
        'rag-embed-ingest'  = @($Config.GpuMinReplicas, ($gpu ? $Config.GpuMaxReplicas : $Config.EmbedIngestCpuMaxReplicas))
        'rag-ingest-worker' = @(0, $Config.WorkerMaxReplicas)
    }
    # The same tier -> profile mapping step 07 renders into the templates.
    $queryProfile = ($Config.QueryPlan -eq 'Consumption') ? 'Consumption' : 'query'
    $ingestProfile = ($Config.IngestPlan -eq 'Consumption') ? 'Consumption' : 'ingest'
    $assigned = @{
        'rag-chat-ui' = $queryProfile; 'rag-api' = $queryProfile; 'rag-embed-query' = $queryProfile
        'rag-ingest-worker' = $ingestProfile; 'rag-scheduler' = $ingestProfile; 'rag-bootstrap' = $ingestProfile
        'rag-embed-ingest' = ($gpu ? 'gpu-t4' : $ingestProfile)
    }
    $apps = [ordered]@{}
    foreach ($name in $Current.apps.Keys) {
        $now = $Current.apps[$name]
        $want = $declared[$name]
        $apps[$name] = [ordered]@{
            runningStatus       = 'Running'
            minReplicas         = $want ? [int]$want[0] : (Get-Value $now 'minReplicas')
            maxReplicas         = $want ? [int]$want[1] : (Get-Value $now 'maxReplicas')
            workloadProfileName = $assigned[$name] ?? (Get-Value $now 'workloadProfileName')
        }
    }
    $jobs = [ordered]@{}
    foreach ($name in $Current.jobs.Keys) { $jobs[$name] = ($name -eq 'rag-scheduler') ? $Config.SchedulerCron : $Current.jobs[$name] }
    $jobProfiles = [ordered]@{}
    foreach ($name in (Get-Keys (Get-Value $Current 'jobProfiles'))) {
        $jobProfiles[$name] = [ordered]@{ workloadProfileName = $assigned[$name] ?? (Get-Value $Current.jobProfiles[$name] 'workloadProfileName') }
    }
    # A tier on Dedicated wants its profile whether or not it exists right now - Hibernate may have deleted it.
    $declaredProfiles = [ordered]@{}
    if ($queryProfile -eq 'query') { $declaredProfiles.query = @($Config.QueryProfileType, $Config.QueryMinNodes, $Config.QueryMaxNodes) }
    if ($ingestProfile -eq 'ingest') { $declaredProfiles.ingest = @($Config.IngestProfileType, $Config.IngestMinNodes, $Config.IngestMaxNodes) }
    $profiles = [ordered]@{}
    foreach ($name in $declaredProfiles.Keys) {
        $want = $declaredProfiles[$name]
        $profiles[$name] = [ordered]@{ type = $want[0]; minimumCount = [int]$want[1]; maximumCount = [int]$want[2] }
    }
    foreach ($name in $Current.profiles.Keys) {
        if ($name -in @('query', 'ingest')) { continue }   # the psd1 decides these two; anything else is not ours
        $profiles[$name] = $Current.profiles[$name]
    }
    return [ordered]@{ apps = $apps; jobs = $jobs; jobProfiles = $jobProfiles; profiles = $profiles
        searchReplicas = [int]$Config.SearchReplicas; postgresState = 'Ready' }
}

function Compare-CostState {
    <# .SYNOPSIS  One line per difference between two states. Used for psd1 drift before Resume and to verify after it. #>
    param([Parameter(Mandatory)]$Expected, [Parameter(Mandatory)]$Actual, [string]$ExpectedLabel = 'expected', [string]$ActualLabel = 'actual')
    $diffs = @()
    $expectedApps = Get-Value $Expected 'apps'
    foreach ($name in @($expectedApps.Keys)) {
        $e = $expectedApps[$name]
        $a = (Get-Value $Actual 'apps')[$name]
        if (-not $a) { $diffs += "app ${name}: in $ExpectedLabel, but not deployed"; continue }
        foreach ($field in 'minReplicas', 'maxReplicas') {
            $ev = Get-Value $e $field
            if ($null -ne $ev -and $ev -ne (Get-Value $a $field)) { $diffs += "app $name ${field}: $ExpectedLabel $ev, $ActualLabel $(Get-Value $a $field)" }
        }
        if ((Test-AppRunning (Get-Value $e 'runningStatus')) -ne (Test-AppRunning (Get-Value $a 'runningStatus'))) {
            $diffs += "app ${name}: $ExpectedLabel $(Get-Value $e 'runningStatus'), $ActualLabel $(Get-Value $a 'runningStatus')"
        }
        # An old snapshot has no assignment recorded; that is "unknown", not "must be empty".
        $ep = "$(Get-Value $e 'workloadProfileName')"
        if ($ep -and $ep -ne "$(Get-Value $a 'workloadProfileName')") { $diffs += "app $name profile: $ExpectedLabel $ep, $ActualLabel $(Get-Value $a 'workloadProfileName')" }
    }
    $expectedJobProfiles = Get-Value $Expected 'jobProfiles'
    foreach ($name in (Get-Keys $expectedJobProfiles)) {
        $ep = "$(Get-Value $expectedJobProfiles[$name] 'workloadProfileName')"
        $a = (Get-Value $Actual 'jobProfiles')[$name]
        if ($ep -and $a -and $ep -ne "$(Get-Value $a 'workloadProfileName')") { $diffs += "job $name profile: $ExpectedLabel $ep, $ActualLabel $(Get-Value $a 'workloadProfileName')" }
    }
    $expectedJobs = Get-Value $Expected 'jobs'
    foreach ($name in @($expectedJobs.Keys)) {
        $a = (Get-Value $Actual 'jobs')[$name]
        if ("$a" -ne "$($expectedJobs[$name])") { $diffs += "job $name cron: $ExpectedLabel '$($expectedJobs[$name])', $ActualLabel '$a'" }
    }
    $expectedProfiles = Get-Value $Expected 'profiles'
    foreach ($name in @($expectedProfiles.Keys)) {
        $e = $expectedProfiles[$name]
        $a = (Get-Value $Actual 'profiles')[$name]
        if (-not $a) { $diffs += "profile ${name}: in $ExpectedLabel, but not in the environment"; continue }
        $en = "$(Get-Value $e 'minimumCount')-$(Get-Value $e 'maximumCount')"
        $an = "$(Get-Value $a 'minimumCount')-$(Get-Value $a 'maximumCount')"
        if ($en -ne $an) { $diffs += "profile $name nodes: $ExpectedLabel $en, $ActualLabel $an" }
    }
    $es = Get-Value $Expected 'searchReplicas'
    if ($null -ne $es -and $es -ne (Get-Value $Actual 'searchReplicas')) { $diffs += "Search replicas: $ExpectedLabel $es, $ActualLabel $(Get-Value $Actual 'searchReplicas')" }
    $ep = "$(Get-Value $Expected 'postgresState')"
    $ap = "$(Get-Value $Actual 'postgresState')"
    if ($ep -and $ap -and (($ep -eq 'Stopped') -ne ($ap -eq 'Stopped'))) { $diffs += "PostgreSQL: $ExpectedLabel $ep, $ActualLabel $ap" }
    return $diffs
}

function Get-SavingGaps {
    <# .SYNOPSIS  What is billing although the recorded mode says it should not be - a deploy, a manual start, or Azure's 7-day restart. #>
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)]$Current)
    $mode = "$(Get-Value $Snapshot 'mode')"
    $cron = "$(Get-Value $Snapshot 'options.schedulerCron')"
    $gaps = @()
    foreach ($name in @($Current.apps.Keys)) {
        $app = $Current.apps[$name]
        if ($mode -eq 'Hibernate' -and (Test-AppRunning (Get-Value $app 'runningStatus'))) { $gaps += "app $name is $(Get-Value $app 'runningStatus')" }
        if ($mode -eq 'ScaleDown' -and (Get-Value $app 'minReplicas') -gt 0) { $gaps += "app $name has min replicas $(Get-Value $app 'minReplicas')" }
    }
    foreach ($name in @($Current.jobs.Keys)) {
        if ($cron -and $Current.jobs[$name] -ne $cron) { $gaps += "job $name runs on '$($Current.jobs[$name])', not '$cron'" }
    }
    foreach ($name in @($Current.profiles.Keys)) {
        $min = Get-Value $Current.profiles[$name] 'minimumCount'
        if ($min -gt 0) { $gaps += "profile $name keeps $min node(s) running" }
    }
    # A profile Hibernate had to keep (a workload that cannot run on Consumption) was reported then; anything else
    # that exists is one a deploy re-added, or one an older snapshot never removed.
    if ($mode -eq 'Hibernate' -and -not (Get-Value $Snapshot 'options.keepDedicatedProfiles')) {
        $kept = @(Get-Value $Snapshot 'options.keptProfiles')
        foreach ($name in @($Current.profiles.Keys | Where-Object { $kept -notcontains $_ })) {
            $gaps += "Dedicated profile $name exists, so the Dedicated plan management fee is billing (a step 07 deploy re-adds it)"
        }
    }
    if (-not (Get-Value $Snapshot 'options.keepSearchReplicas') -and $Current.searchReplicas -gt 1) { $gaps += "Search has $($Current.searchReplicas) replicas" }
    if ($mode -eq 'Hibernate' -and -not (Get-Value $Snapshot 'options.keepPostgres') -and $Current.postgresState -eq 'Ready') {
        $gaps += 'PostgreSQL is running again - most likely Azure''s automatic start after 7 days'
    }
    return $gaps
}

# ================================================================================================ snapshot
function Read-Snapshot {
    if (-not (Test-Path -LiteralPath $SnapshotPath)) { return $null }
    return (Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json -AsHashtable -Depth 20)
}

function Write-Snapshot {
    param([Parameter(Mandatory)]$Snapshot)
    if ($script:DryRun) { Write-Info "[what-if] write $SnapshotPath"; return }
    $Snapshot | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $SnapshotPath -Encoding utf8NoBOM
}

function Update-SnapshotAssignments {
    <#
    .SYNOPSIS  Fills in what an older snapshot did not record: profile assignments, sizes, and workloads added since.
    .DESCRIPTION
        Snapshots written before Hibernate could delete profiles have no workloadProfileName per app and no
        jobProfiles at all. Taking them from the current state is exact, not a guess: nothing before this feature
        ever moved a workload between profiles, so where it is now is where it was. The same goes for an app or job
        deployed after the snapshot was taken - it was never touched by cost saving, so its current state is its
        original one. Existing recorded values are never replaced. Returns one line per addition.
    #>
    param([Parameter(Mandatory)]$Snapshot, [Parameter(Mandatory)]$Current)
    $state = Get-Value $Snapshot 'state'
    $notes = @()
    $apps = Get-Value $state 'apps'
    foreach ($name in @($Current.apps.Keys)) {
        $now = $Current.apps[$name]
        $recorded = $apps[$name]
        if (-not $recorded) { $apps[$name] = $now; $notes += "app $name (deployed after the snapshot was taken)"; continue }
        foreach ($field in 'workloadProfileName', 'cpu', 'memoryGi') {
            if ("$(Get-Value $recorded $field)" -eq '') { $recorded[$field] = $now[$field]; if ($field -eq 'workloadProfileName') { $notes += "app $name is on $($now[$field])" } }
        }
    }
    if (-not (Get-Value $state 'jobProfiles')) { $state['jobProfiles'] = [ordered]@{} }
    $jobProfiles = Get-Value $state 'jobProfiles'
    foreach ($name in @($Current.jobProfiles.Keys)) {
        if ($jobProfiles[$name]) { continue }
        $jobProfiles[$name] = $Current.jobProfiles[$name]
        $notes += "job $name is on $(Get-Value $Current.jobProfiles[$name] 'workloadProfileName')"
    }
    $jobs = Get-Value $state 'jobs'
    foreach ($name in @($Current.jobs.Keys)) {
        if ($null -ne $jobs[$name]) { continue }
        $jobs[$name] = $Current.jobs[$name]
        $notes += "job $name cron '$($Current.jobs[$name])' (deployed after the snapshot was taken)"
    }
    return $notes
}

# ================================================================================================ changes
function Set-JobCron {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Cron, [AllowEmptyString()][string]$Current)
    if ($Current -eq $Cron) { Write-Ok "job $Name cron '$Cron' (unchanged)"; return }
    Invoke-Change "job $Name cron '$Current' -> '$Cron'" {
        Invoke-Az @('containerapp', 'job', 'update', '-g', $rg, '-n', $Name, '--cron-expression', $Cron, '-o', 'none')
    }
}

function Set-AppScale {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][int]$Min, [AllowNull()]$Max, [Parameter(Mandatory)]$Current)
    $updateArgs = @()
    $shown = @()
    if ((Get-Value $Current 'minReplicas') -ne $Min) { $updateArgs += @('--min-replicas', [string]$Min); $shown += "min $(Get-Value $Current 'minReplicas')->$Min" }
    if ($null -ne $Max -and (Get-Value $Current 'maxReplicas') -ne $Max) { $updateArgs += @('--max-replicas', [string]$Max); $shown += "max $(Get-Value $Current 'maxReplicas')->$Max" }
    if ($updateArgs.Count -eq 0) { Write-Ok "app $Name replicas (unchanged: $(Format-AppState $Current))"; return }
    Invoke-Change "app $Name replicas $($shown -join ', ')" {
        Invoke-Az (@('containerapp', 'update', '-g', $rg, '-n', $Name) + $updateArgs + @('-o', 'none'))
    }
}

function Wait-AppStatus {
    param([Parameter(Mandatory)][string]$Name, [switch]$Stopped)
    $label = $Stopped ? 'stopped' : 'started'
    $result = Wait-Until -Activity "app $Name" -ReadyLabel $label -TimeoutMinutes 10 -IntervalSeconds 10 -Condition {
        $app = Invoke-AzRest -Method get -Url "$($script:AppsUrl)/$($Name)?api-version=$($script:AppsApiVersion)"
        $status = "$(Get-Value $app 'properties.runningStatus')"
        @{ Ok = ($Stopped ? ($status -eq 'Stopped') : ($status -notin @('Stopped', 'Stopping', 'Progressing'))); Detail = "runningStatus=$status" }
    }
    if (-not $result.Ok) { Write-Warn "app $Name was not $label after 10 minutes ($($result.Detail)). Check it in the portal, then re-run this mode." }
}

function Stop-RagApp {
    param([Parameter(Mandatory)][string]$Name)
    Invoke-Change "stop app $Name" -Quiet {
        Invoke-AzRest -Method post -Url "$($script:AppsUrl)/$($Name)/stop?api-version=$($script:AppsApiVersion)"
        Wait-AppStatus -Name $Name -Stopped
    }
}

function Start-RagApp {
    param([Parameter(Mandatory)][string]$Name)
    Invoke-Change "start app $Name" -Quiet {
        Invoke-AzRest -Method post -Url "$($script:AppsUrl)/$($Name)/start?api-version=$($script:AppsApiVersion)"
        Wait-AppStatus -Name $Name
    }
}

function Set-ProfileNodes {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][int]$Min, [Parameter(Mandatory)][int]$Max, [Parameter(Mandatory)]$Current)
    $now = "$(Get-Value $Current 'minimumCount')-$(Get-Value $Current 'maximumCount')"
    if ($now -eq "$Min-$Max") { Write-Ok "profile $Name nodes $now (unchanged)"; return }
    Invoke-Change "profile $Name nodes $now -> $Min-$Max" {
        Invoke-Az @('containerapp', 'env', 'workload-profile', 'update', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', $Name,
            '--min-nodes', [string]$Min, '--max-nodes', [string]$Max, '-o', 'none')
    }
}

function Move-Workload {
    <# .SYNOPSIS  Reassigns one app or job to another workload profile. Throws on failure; the caller decides what that costs. #>
    param([Parameter(Mandatory)][ValidateSet('app', 'job')][string]$Kind, [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$From, [Parameter(Mandatory)][string]$To)
    $group = ($Kind -eq 'job') ? @('containerapp', 'job') : @('containerapp')
    Invoke-Change "$Kind $Name profile $From -> $To" {
        Invoke-Az ($group + @('update', '-g', $rg, '-n', $Name, '--workload-profile-name', $To, '-o', 'none'))
    }
}

function Get-AppRunningStatus {
    param([Parameter(Mandatory)][string]$Name)
    return "$(Get-Value (Invoke-AzRest -Method get -Url "$($script:AppsUrl)/$($Name)?api-version=$($script:AppsApiVersion)") 'properties.runningStatus')"
}

function Remove-DedicatedProfiles {
    <#
    .SYNOPSIS  Hibernate's last compute step: empty each Dedicated profile onto Consumption, then delete it.
    .DESCRIPTION
        The Dedicated plan management fee is charged for as long as any Dedicated profile exists, whatever its node
        count, so min-nodes 0 was never enough. Each profile is handled on its own: a workload that cannot run on
        Consumption, or a move Azure refuses, keeps THAT profile (and the fee) and is named, while the others are
        still removed. Returns @{ Removed = names; Kept = @{ name = reason } }.
    #>
    $state = Get-CurrentState
    $removed = @()
    $kept = [ordered]@{}
    foreach ($profileName in @($state.profiles.Keys)) {
        $workloads = @()
        foreach ($name in @($state.apps.Keys)) {
            $a = $state.apps[$name]
            if ((Get-Value $a 'workloadProfileName') -eq $profileName) { $workloads += @{ Kind = 'app'; Name = $name; Cpu = $a.cpu; MemoryGi = $a.memoryGi } }
        }
        foreach ($name in @($state.jobProfiles.Keys)) {
            $j = $state.jobProfiles[$name]
            if ((Get-Value $j 'workloadProfileName') -eq $profileName) { $workloads += @{ Kind = 'job'; Name = $name; Cpu = $j.cpu; MemoryGi = $j.memoryGi } }
        }
        $misfits = @($workloads | Where-Object { -not (Test-ConsumptionSize -Cpu $_.Cpu -MemoryGi $_.MemoryGi) })
        if ($misfits.Count -gt 0) {
            $why = ($misfits | ForEach-Object { "$($_.Name) $($_.Cpu) vCPU / $($_.MemoryGi)Gi" }) -join ', '
            Write-Warn "profile $profileName kept, so the management fee continues: $why cannot run on the Consumption profile (<= 4 vCPU, memory = 2 x cpu)."
            $kept[$profileName] = "cannot run on Consumption: $why"
            continue
        }
        $failed = @()
        foreach ($w in $workloads) {
            try { Move-Workload -Kind $w.Kind -Name $w.Name -From $profileName -To 'Consumption' }
            catch {
                $failed += $w.Name
                Write-Warn "could not move $($w.Kind) $($w.Name) off profile ${profileName}:"
                foreach ($line in @($_.Exception.Message -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -Last 4)) { Write-Info "    $line" }
            }
        }
        if ($failed.Count -gt 0) {
            Write-Warn "profile $profileName kept, so the management fee continues, because $($failed -join ', ') could not be moved."
            $kept[$profileName] = "could not move $($failed -join ', ')"
            continue
        }
        if (-not $script:DryRun) {
            # An update may start a stopped app; this is a hibernation, so anything it started is stopped again.
            foreach ($w in @($workloads | Where-Object { $_.Kind -eq 'app' })) {
                if (Test-AppRunning (Get-AppRunningStatus -Name $w.Name)) { Stop-RagApp -Name $w.Name }
            }
            $users = @(Get-WorkloadProfileUsers -ResourceGroup $rg -ProfileName $profileName)
            if ($users.Count -gt 0) {
                Write-Warn "profile $profileName kept: $($users -join ', ') is still assigned to it."
                $kept[$profileName] = "still assigned: $($users -join ', ')"
                continue
            }
        }
        Invoke-Change "profile $profileName deleted (Resume re-adds it)" {
            Invoke-Az @('containerapp', 'env', 'workload-profile', 'delete', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', $profileName, '-o', 'none')
        }
        $removed += $profileName
    }
    return @{ Removed = $removed; Kept = $kept }
}

function Add-DedicatedProfile {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Type, [Parameter(Mandatory)][int]$Min, [Parameter(Mandatory)][int]$Max)
    Invoke-Change "profile $Name re-added ($Type, $Min-$Max nodes)" {
        Write-Info "Adding profile $Name; its nodes take a few minutes to come up ..."
        Invoke-Az @('containerapp', 'env', 'workload-profile', 'add', '-g', $rg, '-n', $n.ContainerEnv, '--workload-profile-name', $Name,
            '--workload-profile-type', $Type, '--min-nodes', [string]$Min, '--max-nodes', [string]$Max, '-o', 'none')
    }
}

function Set-SearchReplicas {
    param([Parameter(Mandatory)][int]$Replicas, [AllowNull()]$Current)
    if ($null -eq $Current) { Write-Warn "Search service $($n.Search) not found; skipped."; return }
    if ([int]$Current -eq $Replicas) { Write-Ok "Search $($n.Search) $Replicas replica(s) (unchanged)"; return }
    Invoke-Change "Search $($n.Search) replicas $Current -> $Replicas" {
        Write-Info "Scaling Search to $Replicas replica(s); this takes several minutes ..."
        Invoke-Az @('search', 'service', 'update', '-g', $rg, '-n', $n.Search, '--replica-count', [string]$Replicas, '-o', 'none')
    }
}

function Set-PostgresRunning {
    param([Parameter(Mandatory)][bool]$Running, [AllowEmptyString()][string]$Current)
    if (-not $Current) { Write-Warn "PostgreSQL server $($n.Postgres) not found; skipped."; return }
    if ($Current -notin @('Ready', 'Stopped') -and -not $script:DryRun) {
        # start/stop are refused while the server is mid-transition, so wait for it to settle first.
        $settled = Wait-Until -Activity "PostgreSQL $($n.Postgres)" -ReadyLabel 'settled' -TimeoutMinutes 15 -IntervalSeconds 20 -Condition {
            $state = Get-PostgresState
            @{ Ok = ($state -in @('Ready', 'Stopped')); Detail = "state=$state" }
        }
        $Current = Get-PostgresState
        if (-not $settled.Ok) { throw "PostgreSQL $($n.Postgres) is still '$Current' after 15 minutes; re-run once it is Ready or Stopped." }
    }
    if ($Running) {
        if ($Current -eq 'Ready') { Write-Ok "PostgreSQL $($n.Postgres) Ready (unchanged)"; return }
        Invoke-Change "PostgreSQL $($n.Postgres) started" {
            Write-Info 'Starting PostgreSQL; this takes a few minutes ...'
            Invoke-Az @('postgres', 'flexible-server', 'start', '-g', $rg, '-n', $n.Postgres, '-o', 'none')
        }
    }
    else {
        if ($Current -eq 'Stopped') { Write-Ok "PostgreSQL $($n.Postgres) Stopped (unchanged)"; return }
        Invoke-Change "PostgreSQL $($n.Postgres) stopped" {
            Write-Info 'Stopping PostgreSQL; this takes a few minutes ...'
            Invoke-Az @('postgres', 'flexible-server', 'stop', '-g', $rg, '-n', $n.Postgres, '-o', 'none')
        }
        Write-Info '  Azure starts a stopped server again after 7 days. Re-run -Mode Hibernate then to stop it again.'
    }
}

# ================================================================================================ modes
function Show-Status {
    param([Parameter(Mandatory)]$Current, [AllowNull()]$Snapshot)
    $before = $Snapshot ? (Get-Value $Snapshot 'state') : $null
    $rows = [System.Collections.Generic.List[object]]::new()
    foreach ($name in (Sort-AppNames -Names @($Current.apps.Keys))) {
        $rows.Add([pscustomobject]@{ Component = "app $name"; Now = Format-AppState $Current.apps[$name]
                Before = $before ? (Format-AppState (Get-Value $before 'apps')[$name]) : ''; 'Bills while idle' = 'per replica (Consumption) / holds its nodes up (Dedicated)' })
    }
    $formatJob = {
        param($Jobs, $JobProfiles, $Name)
        $cron = ($Jobs -is [System.Collections.IDictionary]) ? $Jobs[$Name] : $null
        $jobProfile = ($JobProfiles -is [System.Collections.IDictionary]) ? (Get-Value $JobProfiles[$Name] 'workloadProfileName') : $null
        if ($null -eq $cron -and -not $jobProfile) { return '' }
        return "$(if ($null -ne $cron) { "cron '$cron'" } else { 'manual' })$(if ($jobProfile) { ", on $jobProfile" })"
    }
    foreach ($name in @(@($Current.jobs.Keys) + @($Current.jobProfiles.Keys) | Select-Object -Unique)) {
        $rows.Add([pscustomobject]@{ Component = "job $name"; Now = (& $formatJob $Current.jobs $Current.jobProfiles $name)
                Before = $before ? (& $formatJob (Get-Value $before 'jobs') (Get-Value $before 'jobProfiles') $name) : ''
                'Bills while idle' = ($Current.jobs.Contains($name) ? 'each run wakes a node' : 'only when started') })
    }
    $beforeProfiles = Get-Value $before 'profiles'
    foreach ($name in @(@($Current.profiles.Keys) + (Get-Keys $beforeProfiles) | Select-Object -Unique)) {
        $p = $Current.profiles[$name]
        $b = ($beforeProfiles -is [System.Collections.IDictionary]) ? $beforeProfiles[$name] : $null
        $type = Get-Value ($p ?? $b) 'type'
        $rows.Add([pscustomobject]@{ Component = "profile $name ($type)"
                Now = $p ? "nodes $(Get-Value $p 'minimumCount')-$(Get-Value $p 'maximumCount')" : '(deleted - no node or management fee)'
                Before = $b ? "nodes $(Get-Value $b 'minimumCount')-$(Get-Value $b 'maximumCount')" : ''
                'Bills while idle' = 'per node, plus the Dedicated plan management fee while any exists' })
    }
    $rows.Add([pscustomobject]@{ Component = "Search $($n.Search)"; Now = "$($Current.searchReplicas) replica(s)"
            Before = $before ? "$(Get-Value $before 'searchReplicas') replica(s)" : ''; 'Bills while idle' = 'per replica, around the clock (minimum 1)' })
    $rows.Add([pscustomobject]@{ Component = "PostgreSQL $($n.Postgres)"; Now = $Current.postgresState
            Before = $before ? "$(Get-Value $before 'postgresState')" : ''; 'Bills while idle' = 'compute while Ready; storage always' })
    Write-Host ($rows | Format-Table -AutoSize -Wrap | Out-String).TrimEnd()
    Write-Host ''
    if (-not $Snapshot) {
        Write-Info 'Cost saving is OFF (no snapshot): the environment runs at its full steady-state cost.'
        Write-Info "Switch on with: ./infra/scripts/Set-CostSavingMode.ps1 -Env $Env -Mode ScaleDown   (or -Mode Hibernate)"
        return
    }
    Write-Info "Cost saving is ON: $(Get-Value $Snapshot 'mode') since $(Get-Value $Snapshot 'updatedAt') (snapshot taken $(Get-Value $Snapshot 'takenAt'))."
    $gaps = @(Get-SavingGaps -Snapshot $Snapshot -Current $Current)
    if ($gaps.Count -eq 0) { Write-Ok 'Everything is in the recorded cost-saving state.' }
    else {
        foreach ($gap in $gaps) { Write-Warn "$gap - billing although $(Get-Value $Snapshot 'mode') is recorded." }
        Write-Info "Re-apply with: ./infra/scripts/Set-CostSavingMode.ps1 -Env $Env -Mode $(Get-Value $Snapshot 'mode')"
    }
    Write-Info "Restore with:  ./infra/scripts/Set-CostSavingMode.ps1 -Env $Env -Mode Resume"
}

function Invoke-Saving {
    param([Parameter(Mandatory)]$Current, [AllowNull()]$Snapshot)
    $hibernate = ($Mode -eq 'Hibernate')
    $cron = $hibernate ? $HibernateCron : $ScaleDownCron

    if ($hibernate) {
        Write-Step 'Pre-flight'
        $bootstrapRuns = @(Get-AzTsvValues @('containerapp', 'job', 'execution', 'list', '-g', $rg, '-n', 'rag-bootstrap', '--query', "[?properties.status=='Running'].name") -AllowNotFound)
        if ($bootstrapRuns.Count -gt 0) {
            # Deliberately not -Force: a scheduled run passes -Force to skip the prompt, and must still never stop
            # the database under a migration.
            if (-not $AllowDuringBootstrap) { throw "rag-bootstrap is running ($($bootstrapRuns -join ', ')). Let it finish - stopping PostgreSQL under a migration is how a schema ends up half-applied. -AllowDuringBootstrap overrides." }
            Write-Warn "rag-bootstrap is running ($($bootstrapRuns -join ', ')); continuing because of -AllowDuringBootstrap."
        }
        foreach ($queue in @('ingest-priority', 'ingest-bulk')) {
            try {
                $count = Invoke-Az @('servicebus', 'queue', 'show', '-g', $rg, '--namespace-name', $n.ServiceBus, '-n', $queue, '--query', 'countDetails.activeMessageCount', '-o', 'tsv') -AllowNotFound
                if ([int]"0$count" -gt 0) { Write-Warn "$queue has $count message(s) waiting. They stay queued and are processed after Resume." }
                else { Write-Ok "$queue is empty" }
            }
            catch { Write-Warn "Could not read the $queue message count: $(($_.Exception.Message -split "`n")[0])" }
        }
    }

    if (-not $Force -and -not $script:DryRun) {
        Write-Host ''
        $impact = $hibernate ? 'Every app is STOPPED: RAG-OS is unavailable until Resume.' : 'Apps scale to zero: the first request after an idle spell waits minutes for a cold start.'
        Write-Host $impact -ForegroundColor Yellow
        $answer = Read-Host "Type '$Mode' to continue"
        if ($answer -ne $Mode) { Write-Info 'Cancelled.'; return }
    }

    Write-Step 'Snapshot'
    # Not ISO 8601 on purpose: ConvertFrom-Json turns that back into a local-culture DateTime on the next read.
    $now = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd HH:mm') + ' UTC'
    if (-not $Snapshot) {
        $Snapshot = [ordered]@{ takenAt = $now; env = $Config.EnvName; resourceGroup = $rg; state = $Current }
        Write-Ok "recorded the current state -> $(Split-Path -Leaf $SnapshotPath)"
    }
    else {
        # Never overwrite it: what is running now is the cost-saving state, and recording that would make Resume
        # restore the scaled-down environment as if it were the original.
        Write-Ok "keeping the snapshot taken $(Get-Value $Snapshot 'takenAt') - Resume restores that state"
        foreach ($note in @(Update-SnapshotAssignments -Snapshot $Snapshot -Current $Current)) { Write-Ok "recorded in the snapshot: $note" }
    }
    # Which profiles an earlier run deleted must survive this run, or Resume would not know to re-add them.
    $previouslyRemoved = @(Get-Value $Snapshot 'options.removedProfiles' | Where-Object { $_ })
    $Snapshot['mode'] = $Mode
    $Snapshot['updatedAt'] = $now
    $Snapshot['options'] = [ordered]@{
        schedulerCron         = $cron
        keepSearchReplicas    = [bool]$KeepSearchReplicas
        keepPostgres          = ($hibernate ? [bool]$KeepPostgres : $true)
        keepDedicatedProfiles = ($hibernate ? [bool]$KeepDedicatedProfiles : $true)
        removedProfiles       = $previouslyRemoved
        keptProfiles          = @()
    }
    # Written before the first change, so a run that fails half-way can still be resumed.
    Write-Snapshot $Snapshot
    $original = Get-Value $Snapshot 'state'

    if (-not $hibernate) {
        # Coming back from Hibernate: the database and the apps that were running must be up again.
        if ((Get-Value $original 'postgresState') -ne 'Stopped' -and $Current.postgresState -eq 'Stopped') {
            Write-Step 'PostgreSQL'
            Set-PostgresRunning -Running $true -Current $Current.postgresState
        }
    }

    Write-Step 'Scheduled jobs'
    if ($Current.jobs.Count -eq 0) { Write-Info 'No scheduled jobs.' }
    foreach ($name in @($Current.jobs.Keys)) { Set-JobCron -Name $name -Cron $cron -Current $Current.jobs[$name] }

    Write-Step 'Container apps'
    foreach ($name in (Sort-AppNames -Names @($Current.apps.Keys))) {
        $app = $Current.apps[$name]
        if ($hibernate) {
            if (Test-AppRunning (Get-Value $app 'runningStatus')) { Stop-RagApp -Name $name }
            else { Write-Ok "app $name already stopped" }
            continue
        }
        Set-AppScale -Name $name -Min 0 -Max $null -Current $app
        $wasRunning = Test-AppRunning (Get-Value (Get-Value $original 'apps')[$name] 'runningStatus')
        if ($wasRunning -and -not (Test-AppRunning (Get-Value $app 'runningStatus'))) { Start-RagApp -Name $name }
    }

    Write-Step 'Workload profiles'
    if ($Current.profiles.Count -eq 0) { Write-Ok 'no Dedicated profile in the environment' }
    foreach ($name in @($Current.profiles.Keys)) {
        $p = $Current.profiles[$name]
        Set-ProfileNodes -Name $name -Min 0 -Max ([int](Get-Value $p 'maximumCount')) -Current $p
    }
    if (-not $hibernate -and $previouslyRemoved.Count -gt 0) {
        Write-Info "Profiles $($previouslyRemoved -join ', ') were deleted by Hibernate: the apps run on the Consumption profile until Resume re-adds them."
    }

    Write-Step 'AI Search'
    if ($KeepSearchReplicas) { Write-Info "Left at $($Current.searchReplicas) replica(s) (-KeepSearchReplicas)." }
    else { Set-SearchReplicas -Replicas 1 -Current $Current.searchReplicas }

    if ($hibernate) {
        Write-Step 'Dedicated plan'
        if ($KeepDedicatedProfiles) { Write-Info 'Profiles kept at 0 min nodes (-KeepDedicatedProfiles); the management fee continues.' }
        elseif ($Current.profiles.Count -eq 0) { Write-Ok 'no Dedicated profile to remove' }
        else {
            $result = Remove-DedicatedProfiles
            $Snapshot.options.removedProfiles = @(@($previouslyRemoved) + @($result.Removed) | Sort-Object -Unique)
            $Snapshot.options.keptProfiles = @($result.Kept.Keys)
            Write-Snapshot $Snapshot
        }
    }

    if ($hibernate) {
        Write-Step 'PostgreSQL'
        if ($KeepPostgres) { Write-Info 'Left running (-KeepPostgres).' }
        else { Set-PostgresRunning -Running $false -Current $Current.postgresState }
    }

    Write-Step "$Mode finished"
    if ($script:DryRun) { Write-Info 'Dry run (-WhatIf): nothing was changed.'; return }
    $after = Get-CurrentState
    $gaps = @(Get-SavingGaps -Snapshot $Snapshot -Current $after)
    if ($gaps.Count -eq 0) { Write-Ok "Verified: everything is in the $Mode state." }
    else { foreach ($gap in $gaps) { Write-Warn "$gap - re-run this mode once it settles." } }
    $stillBilling = 'Service Bus, registry, storage (including PostgreSQL storage), Key Vault, Log Analytics'
    if ($after.profiles.Count -gt 0) {
        # Single-quoted: '$0' inside double quotes would be read as a variable and print as nothing.
        Write-Info ('Still billing: the Dedicated plan management fee (~$0.10/h list price, while ' + (@($after.profiles.Keys) -join ', ') + " exist), $stillBilling.")
    }
    else {
        Write-Ok 'No Dedicated profile left: the Dedicated plan management fee has stopped.'
        Write-Info "Still billing: $stillBilling."
    }
    Write-Info "Restore with: ./infra/scripts/Set-CostSavingMode.ps1 -Env $Env -Mode Resume"
}

function Invoke-Resume {
    param([Parameter(Mandatory)]$Current, [AllowNull()]$Snapshot)
    Write-Step 'What to restore'
    $configTarget = Get-ConfigTarget -Current $Current
    if ($Snapshot) {
        $target = Get-Value $Snapshot 'state'
        Write-Ok "snapshot taken $(Get-Value $Snapshot 'takenAt') (mode $(Get-Value $Snapshot 'mode'))"
        # The snapshot wins - it is what was really running - but a difference from the psd1 is named, not restored silently.
        $drift = @(Compare-CostState -Expected $target -Actual $configTarget -ExpectedLabel 'snapshot' -ActualLabel 'psd1')
        foreach ($line in $drift) { Write-Warn "$line - restoring the snapshot value." }
        if ($drift.Count -gt 0) { Write-Info "  To return to the psd1 values afterwards: ./infra/scripts/07-container-apps.ps1 -Env $Env (Search: ./infra/scripts/Scale-SearchReplicas.ps1)" }
    }
    elseif ($UseConfigDefaults) {
        $target = $configTarget
        Write-Warn "No snapshot at $SnapshotPath - restoring the values declared in $($Config.EnvName).psd1 and starting every app."
    }
    else {
        throw "No snapshot at $SnapshotPath, so there is no recorded state to restore (cost saving was never switched on, or the file was removed). Re-run with -UseConfigDefaults to restore the values in infra/env/$($Config.EnvName).psd1."
    }

    Write-Step 'PostgreSQL'
    if ((Get-Value $target 'postgresState') -eq 'Stopped') { Write-Info 'It was stopped before cost saving; left as it is.' }
    else { Set-PostgresRunning -Running $true -Current $Current.postgresState }

    Write-Step 'AI Search'
    $searchTarget = Get-Value $target 'searchReplicas'
    if ($null -ne $searchTarget) { Set-SearchReplicas -Replicas ([int]$searchTarget) -Current $Current.searchReplicas }

    Write-Step 'Workload profiles'
    $targetProfiles = Get-Value $target 'profiles'
    foreach ($name in (Get-Keys $targetProfiles)) {
        $want = $targetProfiles[$name]
        $min = [int](Get-Value $want 'minimumCount')
        $max = [int](Get-Value $want 'maximumCount')
        $now = $Current.profiles[$name]
        # Absent because Hibernate deleted it (or it was removed by hand): re-add it exactly as it was recorded.
        if (-not $now) { Add-DedicatedProfile -Name $name -Type "$(Get-Value $want 'type')" -Min $min -Max $max; continue }
        Set-ProfileNodes -Name $name -Min $min -Max $max -Current $now
    }

    # Before any app is scaled or started: an app started on Consumption would run there, and moving it afterwards
    # is a restart of a running app rather than a reassignment of a stopped one.
    Write-Step 'Workload profile assignments'
    $moves = 0
    $assignments = @()
    foreach ($name in (Get-Keys (Get-Value $target 'apps'))) {
        $assignments += @{ Kind = 'app'; Name = $name; Want = "$(Get-Value (Get-Value $target 'apps')[$name] 'workloadProfileName')"; Now = $Current.apps[$name] }
    }
    foreach ($name in (Get-Keys (Get-Value $target 'jobProfiles'))) {
        $assignments += @{ Kind = 'job'; Name = $name; Want = "$(Get-Value (Get-Value $target 'jobProfiles')[$name] 'workloadProfileName')"; Now = $Current.jobProfiles[$name] }
    }
    foreach ($a in $assignments) {
        $have = "$(Get-Value $a.Now 'workloadProfileName')"
        if (-not $a.Want -or -not $a.Now -or $a.Want -eq $have) { continue }
        $moves++
        try { Move-Workload -Kind $a.Kind -Name $a.Name -From $have -To $a.Want }
        catch {
            # Not fatal: on Consumption it still works, it just is not where it was. Resume can be re-run.
            Write-Warn "$($a.Kind) $($a.Name) stays on $have (it still runs there): $(($_.Exception.Message -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1))"
            Write-Info '  Re-run -Mode Resume to retry the move.'
        }
    }
    if ($moves -eq 0) { Write-Ok 'every app and job is on its recorded profile' }
    elseif (-not $script:DryRun) { $Current.apps = Get-AppStates }   # so the lines below describe where they are now

    Write-Step 'Container apps'
    $targetApps = Get-Value $target 'apps'
    foreach ($name in (Sort-AppNames -Names @($targetApps.Keys) -Reverse)) {
        $now = $Current.apps[$name]
        if (-not $now) { Write-Warn "app $name is no longer deployed; skipped."; continue }
        $want = $targetApps[$name]
        Set-AppScale -Name $name -Min ([int](Get-Value $want 'minReplicas')) -Max (Get-Value $want 'maxReplicas') -Current $now
        $shouldRun = Test-AppRunning (Get-Value $want 'runningStatus')
        $isRunning = Test-AppRunning (Get-Value $now 'runningStatus')
        if ($shouldRun -and -not $isRunning) { Start-RagApp -Name $name }
        elseif (-not $shouldRun) { Write-Info "app $name was stopped before cost saving; left as it is." }
    }

    Write-Step 'Scheduled jobs'
    $targetJobs = Get-Value $target 'jobs'
    foreach ($name in @($targetJobs.Keys)) {
        if (-not $Current.jobs.Contains($name)) { Write-Warn "job $name is no longer deployed; skipped."; continue }
        Set-JobCron -Name $name -Cron "$($targetJobs[$name])" -Current $Current.jobs[$name]
    }

    Write-Step 'Resume finished'
    if ($script:DryRun) { Write-Info 'Dry run (-WhatIf): nothing was changed.'; return }
    $after = Get-CurrentState
    $left = @(Compare-CostState -Expected $target -Actual $after -ExpectedLabel 'wanted' -ActualLabel 'now')
    if ($left.Count -eq 0) { Write-Ok 'Verified: every component matches the state being restored.' }
    else { foreach ($line in $left) { Write-Warn $line } }

    if (-not $NoWait) {
        Write-Step 'Waiting for the query path'
        foreach ($name in @('rag-embed-query', 'rag-api')) {
            $want = $targetApps[$name]
            if ($want -and (Test-AppRunning (Get-Value $want 'runningStatus')) -and [int](Get-Value $want 'minReplicas') -gt 0) {
                $ready = Wait-ContainerAppReady -ResourceGroup $rg -Name $name -MinReplicas 1 -TimeoutMinutes 15
                if (-not $ready.Ok) { Write-Warn "$name is not ready after 15 minutes ($($ready.Detail))." }
            }
        }
        $chat = $targetApps['rag-chat-ui']
        if ($chat -and (Test-AppRunning (Get-Value $chat 'runningStatus'))) {
            $readyzUrl = "$(Get-ChatUiUrl -Config $Config)/api/readyz"
            $readyz = Wait-Until -Activity 'GET /api/readyz' -TimeoutMinutes 10 -Condition {
                $response = Invoke-WebRequest -Uri $readyzUrl -TimeoutSec 30 -SkipHttpErrorCheck
                @{ Ok = ($response.StatusCode -eq 200); Detail = ((Format-ReadyzReasons -Content "$($response.Content)" -StatusCode $response.StatusCode) -join '; ') }
            }
            if (-not $readyz.Ok) { Write-Warn "/api/readyz is not 200 yet: $($readyz.Detail). See Deployment.md 'Diagnosing a 503 from /api/readyz'." }
        }
    }

    if ($Snapshot) {
        $stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
        $archive = Join-Path (Split-Path -Parent $SnapshotPath) "$($Config.EnvName).costsaver.$stamp.resumed.json"
        Move-Item -LiteralPath $SnapshotPath -Destination $archive
        Write-Ok "snapshot archived as $(Split-Path -Leaf $archive) - cost saving is OFF"
    }
}

# ================================================================================================ main
$snapshot = Read-Snapshot
Write-Step 'Reading the current state'
$current = Get-CurrentState
Write-Ok "$($current.apps.Count) app(s), $($current.jobProfiles.Count) job(s) ($($current.jobs.Count) scheduled), $($current.profiles.Count) Dedicated profile(s)"
if ($script:DryRun) { Write-Info 'Dry run (-WhatIf): changes are listed, not made.' }

switch ($Mode) {
    'Status' { Show-Status -Current $current -Snapshot $snapshot }
    'Resume' { Invoke-Resume -Current $current -Snapshot $snapshot }
    default { Invoke-Saving -Current $current -Snapshot $snapshot }
}
