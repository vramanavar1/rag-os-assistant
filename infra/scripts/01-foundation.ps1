#Requires -Version 7.3
<#
.SYNOPSIS
    Step 01 - resource group, Log Analytics workspace, workspace-based Application Insights, budget alert.
.EXAMPLE
    ./infra/scripts/01-foundation.ps1 -Env dev
#>
[CmdletBinding()]
param([string]$Env = 'dev')
. (Join-Path $PSScriptRoot 'common.ps1')
$Config = Initialize-RagOsScript -Env $Env -Title '01 foundation'
$n = $Config.Names
$rg = $n.ResourceGroup
$loc = $Config.Location
$tags = Get-TagArgs -Config $Config

Write-Step "Resource group $rg"
$null = Ensure-AzResource -Description "resource group $rg" -Config $Config -SyncTags `
    -Show @('group', 'show', '-n', $rg) `
    -Create (@('group', 'create', '-n', $rg, '-l', $loc) + $tags) `
    -Update @('group', 'update', '-n', $rg) -Desired @(
    (New-DesiredProperty -Path 'location' -Desired $loc -Label 'location' -Class 'immutable' `
            -Remediation "A resource group cannot move. Either set Location back to its current value, or deploy a second environment with a different Prefix/NameSuffix.")
)

Write-Step "Log Analytics workspace $($n.LogAnalytics)"
$law = Ensure-AzResource -Description "Log Analytics $($n.LogAnalytics)" -Config $Config -SyncTags `
    -Show @('monitor', 'log-analytics', 'workspace', 'show', '-g', $rg, '-n', $n.LogAnalytics) `
    -Create (@('monitor', 'log-analytics', 'workspace', 'create', '-g', $rg, '-n', $n.LogAnalytics, '-l', $loc,
        '--sku', 'PerGB2018', '--retention-time', [string]$Config.LogRetentionDays) + $tags) `
    -Update @('monitor', 'log-analytics', 'workspace', 'update', '-g', $rg, '-n', $n.LogAnalytics) -Desired @(
    # Retention is billed, so a change here is deliberate - but it neither restarts anything nor moves data.
    (New-DesiredProperty -Path 'retentionInDays' -Desired $Config.LogRetentionDays -Arg '--retention-time' -Label 'retention days')
)

Write-Step "Application Insights $($n.AppInsights) (workspace-based)"
$appi = Ensure-AzResource -Description "Application Insights $($n.AppInsights)" -Config $Config -SyncTags `
    -Show @('monitor', 'app-insights', 'component', 'show', '--app', $n.AppInsights, '-g', $rg) `
    -Create (@('monitor', 'app-insights', 'component', 'create', '--app', $n.AppInsights, '-g', $rg, '-l', $loc,
        '--workspace', $law.id, '--kind', 'web', '--application-type', 'web') + $tags) `
    -Update @('monitor', 'app-insights', 'component', 'update', '--app', $n.AppInsights, '-g', $rg) -Desired @(
    # A component keeps pointing at whatever workspace it was created against. If the workspace name ever changes
    # (a new Prefix or NameSuffix), telemetry keeps landing in the old one and nothing else would say so.
    (New-DesiredProperty -Path 'workspaceResourceId' -Desired $law.id -Arg '--workspace' -Label 'workspace')
)

if ([double]$Config.BudgetAmount -le 0) {
    # The catch below has always advertised this as the way to turn budgets off, but there was no guard, so a
    # zero amount was PUT, rejected by the API, and warned about on every single run.
    Write-Step 'Budget (disabled)'
    Write-Ok "BudgetAmount is $($Config.BudgetAmount) - no budget is created or updated."
    Write-Info 'Nothing will alert you on spend. Set BudgetAmount in the psd1 to enable it.'
}
else {
    Write-Step "Budget $($n.Budget) ($($Config.BudgetAmount) / month)"
    # az consumption budget create cannot add notifications, so the Consumption REST API is used (pinned api-version).
    try {
        $budgetUrl = "https://management.azure.com$($Config.ResourceGroupId)/providers/Microsoft.Consumption/budgets/$($n.Budget)?api-version=2023-11-01"
        $existing = Invoke-AzRest -Method get -Url $budgetUrl -AllowNotFound
        # Comparing the amount alone meant an edit to BudgetContactEmails never took effect - a silent alerting
        # gap. Both are compared now, and the emails are order-insensitive so a reorder is not a change.
        $desiredEmails = @($Config.BudgetContactEmails | Where-Object { $_ } | Sort-Object)
        $currentEmails = @(Get-Value $existing 'properties.notifications.Actual_GreaterThan_80_Percent.contactEmails' | Where-Object { $_ } | Sort-Object)
        $amountMatches = $existing -and [double](Get-Value $existing 'properties.amount') -eq [double]$Config.BudgetAmount
        $emailsMatch = ($desiredEmails -join '|') -eq ($currentEmails -join '|')
        if ($amountMatches -and $emailsMatch) {
            Write-Ok "budget $($n.Budget) (exists, amount and contacts unchanged)"
        }
        else {
            # Keep the window the budget already has. Rebasing startDate to the current month on every amount
            # edit quietly discards the period the spend has been accumulating against.
            $start = Get-Value $existing 'properties.timePeriod.startDate'
            if (-not $start) { $start = (Get-Date -Day 1).ToString('yyyy-MM-01T00:00:00Z') }
            $end = Get-Value $existing 'properties.timePeriod.endDate'
            if (-not $end) { $end = (Get-Date -Day 1).AddYears(3).ToString('yyyy-MM-01T00:00:00Z') }
            $notification = {
                param($threshold, $type)
                $entry = @{ enabled = $true; operator = 'GreaterThan'; threshold = $threshold; thresholdType = $type; contactRoles = @('Owner') }
                if (@($Config.BudgetContactEmails).Count -gt 0) { $entry.contactEmails = @($Config.BudgetContactEmails) }
                $entry
            }
            $body = @{
                properties = @{
                    category      = 'Cost'
                    amount        = $Config.BudgetAmount
                    timeGrain     = 'Monthly'
                    timePeriod    = @{ startDate = $start; endDate = $end }
                    notifications = @{
                        Actual_GreaterThan_80_Percent      = & $notification 80 'Actual'
                        Forecasted_GreaterThan_100_Percent = & $notification 100 'Forecasted'
                    }
                }
            }
            $etag = Get-Value $existing 'eTag'
            if ($etag) { $body.eTag = $etag }
            $null = Invoke-AzRest -Method put -Url $budgetUrl -Body $body
            $what = if (-not $existing) { 'created' } elseif (-not $amountMatches) { "updated: amount -> $($Config.BudgetAmount)" } else { 'updated: contacts' }
            Write-Ok "budget $($n.Budget) ($what)"
            Write-Info 'Note: the PUT replaces the whole notification block, so portal-side edits to it are overwritten.'
        }
    }
    catch {
        Write-Warn "Budget not created: $($_.Exception.Message.Split("`n")[0])"
        Write-Info 'Non-fatal - provisioning continues, but nothing will alert you on spend. Some subscription offers'
        Write-Info '(CSP, sponsored, some EA scopes) do not support budgets at all. Either create it in the portal under'
        Write-Info 'Cost Management > Budgets, or set BudgetAmount = 0 in the psd1 to stop attempting it.'
    }
}

Save-Outputs -Config $Config -Values @{
    resourceGroup          = $rg
    location               = $loc
    logAnalyticsId         = $law.id
    logAnalyticsName       = $n.LogAnalytics
    logAnalyticsCustomerId = $law.customerId
    appInsightsId          = $appi.id
    appInsightsName        = $n.AppInsights
}
Write-Ok "Foundation ready. Verify: az group show -n $rg --query properties.provisioningState -o tsv"
