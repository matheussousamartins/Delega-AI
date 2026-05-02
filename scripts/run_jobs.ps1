param(
    [string]$BaseUrl = "http://localhost:8010",
    [string]$Secret = $env:REMINDER_JOB_SECRET,
    [int]$IntervalSeconds = 60,
    [switch]$Loop,
    [switch]$RemindersOnly,
    [switch]$NotificationsOnly
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($Secret)) {
    Write-Error "Informe o segredo com -Secret ou defina REMINDER_JOB_SECRET no PowerShell."
}

$headers = @{
    "Content-Type" = "application/json"
    "x-job-secret" = $Secret
}

function Invoke-DelegaJob {
    param(
        [string]$Name,
        [string]$Path,
        [string]$Body = "{}"
    )

    $url = "$BaseUrl$Path"
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] $Name -> $url"

    try {
        $response = Invoke-RestMethod -Method Post -Uri $url -Headers $headers -Body $Body
        $response | ConvertTo-Json -Depth 8
    }
    catch {
        Write-Warning "$Name falhou: $($_.Exception.Message)"
    }
}

function Invoke-DelegaJobsOnce {
    if (-not $NotificationsOnly) {
        Invoke-DelegaJob -Name "reminders" -Path "/jobs/reminders" -Body "{}"
    }
    if (-not $RemindersOnly) {
        Invoke-DelegaJob -Name "notifications" -Path "/jobs/notifications" -Body "{}"
    }
}

do {
    Invoke-DelegaJobsOnce
    if ($Loop) {
        Start-Sleep -Seconds $IntervalSeconds
    }
} while ($Loop)
