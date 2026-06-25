# Network IP Manager - Auto find free IP and bind
# Usage:
#   .\manage_ip.ps1 list                              # Show current IPs
#   .\manage_ip.ps1 add [count|full-ip]               # Auto find N free IPs, or bind a specific IP
#   .\manage_ip.ps1 remove 192.168.132.200            # Remove IP
#   .\manage_ip.ps1 scan                              # Scan subnet for used IPs

param(
    [Parameter(Position=0, Mandatory=$true)]
    [ValidateSet("list", "add", "remove", "scan", "help")]
    [string]$Action,
    [Parameter(Position=1)]
    [string[]]$CountOrIP = @("1"),
    [Alias("h")]
    [switch]$Help,
    [string]$AdapterName
)

if ($Help -or $Action -eq "help") {
    Write-Host @'

  Network IP Manager
  Usage: .\manage_ip.ps1 <command> [args]

  Commands:
    list                          Show current IPs
    scan                          Scan subnet for used/free IPs
    add <count|full-ip>           Add free IPs or a specific IP
    remove <full-ip>              Remove an IP
    help                          Show this help

  Options:
    -h, -Help                    Show this help
    -AdapterName <name>          Manually specify network adapter

  Examples:
    .\manage_ip.ps1 -h
    .\manage_ip.ps1 list
    .\manage_ip.ps1 scan
    .\manage_ip.ps1 add 192.168.132.203
    .\manage_ip.ps1 add 192.168.132.201-205
    .\manage_ip.ps1 add 192.168.132.201,192.168.132.202
    .\manage_ip.ps1 add 3
    .\manage_ip.ps1 remove 192.168.132.203
    .\manage_ip.ps1 remove 192.168.132.201-205
    .\manage_ip.ps1 list -AdapterName "Ethernet"

'@ -ForegroundColor Cyan
    exit
}

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "[ERROR] Need Administrator! Right-click PowerShell -> Run as Administrator" -ForegroundColor Red
    pause
    exit 1
}

$CountOrIP = $CountOrIP -join ','

if ($AdapterName) {
    $Adapter = Get-NetAdapter -Name $AdapterName -ErrorAction Stop
    $Prefix = (Get-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -AddressFamily IPv4 | Select-Object -First 1).IPAddress -replace '\.\d+$', ''
    $Gateway = (Get-NetRoute -InterfaceIndex $Adapter.InterfaceIndex -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue).NextHop
} else {
    $Route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Where-Object { $_.NextHop -ne '0.0.0.0' } |
        Sort-Object RouteMetric | Select-Object -First 1
    $Adapter = Get-NetAdapter -InterfaceIndex $Route.InterfaceIndex
    $Prefix = (Get-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -AddressFamily IPv4 | Select-Object -First 1).IPAddress -replace '\.\d+$', ''
    $Gateway = $Route.NextHop
}

function Get-UsedIPs {
    $arp = arp -a | Select-String "$Prefix" | ForEach-Object { ($_ -split "\s+")[0] } |
        Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' }
    $netIPs = Get-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty IPAddress
    return ($arp + $netIPs) | Sort-Object -Unique
}

function Format-Column($text, $width) {
    $displayWidth = ($text.ToCharArray() | ForEach-Object { if ([char]::IsHighSurrogate($_)) { 2 } else { if ([int]$_ -gt 0x7F) { 2 } else { 1 } } } | Measure-Object -Sum).Sum
    $pad = $width - $displayWidth
    if ($pad -lt 0) { $pad = 0 }
    return $text + (" " * $pad)
}

function Expand-IPList($ipInput) {
    $raw = $ipInput -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
    $result = @()
    foreach ($item in $raw) {
        if ($item -match '^(\d+\.\d+\.\d+\.)(\d+)-(\d+)$') {
            $base = $Matches[1]
            $start = [int]$Matches[2]
            $end = [int]$Matches[3]
            for ($i = $start; $i -le $end; $i++) { $result += "$base$i" }
        } else {
            $result += $item
        }
    }
    return $result
}

function Find-FreeIP($used, [int]$count) {
    $free = @()
    for ($i = 201; $i -le 250; $i++) {
        $ip = "$Prefix.$i"
        if ($ip -notin $used -and $ip -ne $Gateway -and $Gateway) {
            $free += $ip
            if ($free.Count -ge $count) { break }
        }
    }
    return $free
}

switch ($Action) {
    "list" {
        $showDefault = if ($AdapterName) { $Adapter.InterfaceIndex } else {
            (Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
                Where-Object { $_.NextHop -ne '0.0.0.0' } |
                Sort-Object RouteMetric | Select-Object -First 1).InterfaceIndex
        }

        Get-NetAdapter | Where-Object { $_.Status -eq 'Up' } | ForEach-Object {
            $ips = (Get-NetIPAddress -InterfaceIndex $_.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress -join ', '
            $isDefault = $_.InterfaceIndex -eq $showDefault
            $prefix = if ($isDefault) { "* " } else { "  " }
            $line = "  {0}{1} {2}" -f $prefix, (Format-Column $_.Name 28), $ips
            if ($isDefault) { Write-Host $line -ForegroundColor Green } else { Write-Host $line }
        }
        Write-Host ""
    }
    "scan" {
        Write-Host "Scanning $Prefix.0/24 ..." -ForegroundColor Cyan
        $used = Get-UsedIPs
        $free = Find-FreeIP $used 250

        Write-Host "`nUsed ($($used.Count)):" -ForegroundColor Yellow
        $row = 0
        foreach ($ip in $used) {
            if ($row % 5 -eq 0) { Write-Host "  " -NoNewline }
            Write-Host ("{0,-20}" -f $ip) -NoNewline
            $row++
            if ($row % 5 -eq 0) { Write-Host "" }
        }
        if ($row % 5 -ne 0) { Write-Host "" }

        Write-Host "Free ($($free.Count)):" -ForegroundColor Green
        $row = 0
        foreach ($ip in $free) {
            if ($row % 5 -eq 0) { Write-Host "  " -NoNewline }
            Write-Host ("{0,-20}" -f $ip) -NoNewline
            $row++
            if ($row % 5 -eq 0) { Write-Host "" }
        }
        if ($row % 5 -ne 0) { Write-Host "" }
    }
    "add" {
        $ips = Expand-IPList $CountOrIP
        if ($ips.Count -eq 1 -and $ips[0] -match '^\d+$') {
            [int]$count = [int]$ips[0]
            $used = Get-UsedIPs
            $free = Find-FreeIP $used $count
            if ($free.Count -eq 0) {
                Write-Host "No free IPs found in $Prefix.201-250" -ForegroundColor Red
                return
            }
            foreach ($ip in $free) {
                New-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -IPAddress $ip -PrefixLength 24 | Out-Null
                Write-Host "[added] $ip" -ForegroundColor Green
            }
        } else {
            $used = Get-UsedIPs
            foreach ($ip in $ips) {
                if ($ip -notin $used) {
                    New-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -IPAddress $ip -PrefixLength 24 | Out-Null
                    Write-Host "[added] $ip" -ForegroundColor Green
                } else {
                    Write-Host "[skip] $ip already in use" -ForegroundColor Yellow
                }
            }
        }
    }
    "remove" {
        $ips = Expand-IPList $CountOrIP
        foreach ($ip in $ips) {
            Remove-NetIPAddress -InterfaceIndex $Adapter.InterfaceIndex -IPAddress $ip -Confirm:$false -ErrorAction SilentlyContinue
            Write-Host "[removed] $ip" -ForegroundColor Red
        }
    }
}
