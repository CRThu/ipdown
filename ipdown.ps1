param(
    [Parameter(Position=0)]
    [string]$Url,
    [Alias("o")][string]$Output,
    [Alias("i")][string[]]$Interface,
    [Alias("n")][string]$Adapter,
    [Alias("p")][int]$Parts = 0,
    [Alias("h")][switch]$Help
)

if ($Help -or -not $Url) {
    Write-Host @'

  Parallel IP Download - curl-like interface

  Usage: pdown <url> [options]

  Options:
    -o, -Output <file>       Output filename (default: from URL)
    -i, -Interface <ip>      IPs to use, comma-separated or multiple -i flags
    -n, -Adapter <name>      Adapter name (default: auto-detect)
    -p, -Parts <n>           Number of parts (default: number of IPs)
    -h, -Help                Show this help

  Features:
    - Auto-detect all IPs on adapter
    - Resume interrupted downloads (re-run same URL)
    - Manifest-based part validation
    - Network error auto-retry (3x)
    - Ctrl+C cleanup

  Examples:
    pdown "https://example.com/file.zip"
    pdown "https://example.com/file.zip" -o myfile.zip
    pdown "https://example.com/file.zip" -i 192.168.132.118,192.168.132.200
    pdown "https://example.com/file.zip" -n "Ethernet" -p 4

'@ -ForegroundColor Cyan
    return
}

$Ips = @()
if ($Interface) {
    foreach ($item in $Interface) {
        $Ips += $item -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne '' }
    }
}

if ($Ips.Count -eq 0) {
    if ($Adapter) {
        $Nic = Get-NetAdapter -Name $Adapter -ErrorAction Stop
    } else {
        $Route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
            Where-Object { $_.NextHop -ne '0.0.0.0' } |
            Sort-Object RouteMetric | Select-Object -First 1
        $Nic = Get-NetAdapter -InterfaceIndex $Route.InterfaceIndex
    }

    $Ips = (Get-NetIPAddress -InterfaceIndex $Nic.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue).IPAddress |
        Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' }

    if ($Ips.Count -eq 0) {
        Write-Host "[ERROR] No IPs found on adapter $($Nic.Name)." -ForegroundColor Red
        return
    }

    Write-Host "Adapter: $($Nic.Name) | IPs: $($Ips -join ', ')" -ForegroundColor Gray
}

if (-not $Output) {
    $Output = [System.IO.Path]::GetFileName(([System.Uri]$Url).AbsolutePath)
    if (-not $Output) { $Output = "download" }
}

if ($Parts -le 0) { $Parts = $Ips.Count }

Write-Host ""
Write-Host "=== Parallel IP Download ===" -ForegroundColor Cyan
Write-Host "URL:    $Url"
Write-Host "Output: $Output"
Write-Host "Parts:  $Parts"
Write-Host "IPs:    $($Ips -join ', ')"
Write-Host ""

Write-Host "Checking IPs..." -ForegroundColor Yellow
$aliveIps = @()
foreach ($ip in $Ips) {
    $test = curl.exe -sI --insecure --noproxy "*" --connect-timeout 3 --max-time 5 --interface $ip $Url -o /dev/null -w "%{http_code}" 2>$null
    if ($test -and $test -ne "000") {
        $aliveIps += $ip
        Write-Host "  $ip OK ($test)" -ForegroundColor Green
    } else {
        Write-Host "  $ip UNREACHABLE - skipped" -ForegroundColor Red
    }
}

if ($aliveIps.Count -eq 0) {
    Write-Host "[ERROR] No reachable IPs. Check Sangfor auth." -ForegroundColor Red
    return
}
$Ips = $aliveIps
if ($Parts -gt $Ips.Count) { $Parts = $Ips.Count }

Write-Host "Getting file size..." -ForegroundColor Yellow
try {
    $headResult = curl.exe -sIL --insecure --noproxy "*" --interface $Ips[0] $Url 2>$null
    $contentLength = 0
    foreach ($line in $headResult) {
        if ($line -match "Content-Length:\s*(\d+)") {
            $contentLength = [long]$Matches[1]
        }
    }
    if ($contentLength -eq 0) {
        Write-Host "[ERROR] Cannot get file size." -ForegroundColor Red
        return
    }
    $sizeMB = [math]::Round($contentLength / 1MB, 1)
    Write-Host "File size: $sizeMB MB" -ForegroundColor Green
} catch {
    Write-Host "[ERROR] Failed to get file size: $_" -ForegroundColor Red
    return
}

$finalPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location).Path $Output))
$finalDir = [System.IO.Path]::GetDirectoryName($finalPath)
$hash = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash([System.Text.Encoding]::UTF8.GetBytes($Url))).Replace('-','').Substring(0,12)
$tmpDir = Join-Path $finalDir ".dl_$hash"
New-Item -ItemType Directory -Path $tmpDir -Force | Out-Null

$manifestPath = Join-Path $tmpDir "manifest.json"
$startTime = Get-Date
$partFiles = @()
$jobs = @()

function Read-Manifest {
    if (-not (Test-Path $manifestPath)) { return $null }
    try {
        return Get-Content $manifestPath -Raw | ConvertFrom-Json
    } catch {
        return $null
    }
}

function Write-Manifest {
    param([object]$manifest)
    $manifest | ConvertTo-Json -Depth 5 | Set-Content $manifestPath -Encoding UTF8
}

try {
    $existingManifest = Read-Manifest
    $resumeAll = $false
    $chunkSize = [math]::Ceiling($contentLength / $Parts)
    $partList = @()

    for ($i = 0; $i -lt $Parts; $i++) {
        $partList += @{
            index = $i
            start = $i * $chunkSize
            end = [math]::Min(($i + 1) * $chunkSize - 1, $contentLength - 1)
            size = [math]::Min($chunkSize, $contentLength - $i * $chunkSize)
            ip = $Ips[$i % $Ips.Count]
        }
    }

    if ($existingManifest -and $existingManifest.url -eq $Url -and $existingManifest.totalSize -eq $contentLength -and $existingManifest.parts -eq $Parts) {
        $resumeAll = $true
        $allValid = $true
        foreach ($p in $existingManifest.partList) {
            $pf = Join-Path $tmpDir "part_$($p.index)"
            if (-not (Test-Path $pf) -or (Get-Item $pf).Length -ne $p.size) {
                $allValid = $false
                break
            }
        }
        if ($allValid) {
            Write-Host "All $Parts parts validated from cache, skipping download..." -ForegroundColor Green
        } else {
            $resumeAll = $false
        }
    }

    if (-not $resumeAll) {
        Write-Host ""
        Write-Host "Starting $Parts parallel downloads..." -ForegroundColor Yellow

        for ($i = 0; $i -lt $Parts; $i++) {
            $start = $partList[$i].start
            $end = $partList[$i].end
            $ip = $partList[$i].ip
            $partFile = Join-Path $tmpDir "part_$i"
            $partFiles += $partFile
            $expectedSize = $partList[$i].size

            $range = "$start-$end"
            $partMB = [math]::Round($expectedSize / 1MB, 1)

            if ((Test-Path $partFile) -and (Get-Item $partFile).Length -eq $expectedSize) {
                Write-Host "  Part $($i+1): $partMB MB via $ip [cached]" -ForegroundColor DarkGray
                continue
            }

            if (Test-Path $partFile) { Remove-Item $partFile -Force }

            Write-Host "  Part $($i+1): range $range ($partMB MB) via $ip" -ForegroundColor Gray

            $job = Start-Job -ScriptBlock {
                param($url, $range, $ip, $partFile)
                $result = curl.exe -sL --insecure --noproxy "*" --interface $ip -r $range -o $partFile --retry 3 --retry-delay 5 --retry-max-time 60 $url 2>&1
                if ($LASTEXITCODE -ne 0) { throw "curl failed: $result" }
                $size = (Get-Item $partFile).Length
                return $size
            } -ArgumentList $Url, $range, $ip, $partFile
            $jobs += $job
        }

        Write-Host ""
        Write-Host "Downloading..." -ForegroundColor Yellow

        $completed = 0
        $failed = 0
        $totalJobs = $jobs.Count
        $lastProgress = ""

        while ($completed + $failed -lt $totalJobs) {
            $elapsed = ((Get-Date) - $startTime).TotalSeconds
            $totalDownloaded = 0
            foreach ($f in $partFiles) {
                if (Test-Path $f) { $totalDownloaded += (Get-Item $f).Length }
            }
            $speed = if ($elapsed -gt 0) { [math]::Round($totalDownloaded / $elapsed / 1KB) } else { 0 }
            $progress = if ($contentLength -gt 0) { [math]::Round($totalDownloaded / $contentLength * 100, 1) } else { 0 }
            $dlMB = [math]::Round($totalDownloaded / 1MB, 1)
            $line = "  $dlMB/$([math]::Round($contentLength / 1MB, 1)) MB  $progress%  ${speed} KB/s"

            if ($line -ne $lastProgress) {
                Write-Host "`r$line" -NoNewline -ForegroundColor Green
                $lastProgress = $line
            }

            for ($i = 0; $i -lt $jobs.Count; $i++) {
                if ($jobs[$i] -and $jobs[$i].State -eq "Completed") {
                    Receive-Job -Job $jobs[$i] | Out-Null
                    $completed++
                    $jobs[$i] = $null
                }
                if ($jobs[$i] -and $jobs[$i].State -eq "Failed") {
                    $failed++
                    Write-Host ""
                    Write-Host "  Part $($i+1) FAILED" -ForegroundColor Red
                    Receive-Job -Job $jobs[$i] -ErrorAction SilentlyContinue
                    $jobs[$i] = $null
                }
            }
            Start-Sleep -Milliseconds 500
        }
        Write-Host ""

        $jobs | Where-Object { $_ -ne $null } | Remove-Job -Force -ErrorAction SilentlyContinue

        if ($failed -gt 0) {
            Write-Host ""
            Write-Host "[ERROR] $failed parts failed. Re-run to resume." -ForegroundColor Red
            return
        }

        Write-Manifest @{
            url = $Url
            totalSize = $contentLength
            parts = $Parts
            partList = $partList
            created = (Get-Date).ToString("o")
        }
    } else {
        for ($i = 0; $i -lt $Parts; $i++) {
            $partFiles += Join-Path $tmpDir "part_$i"
        }
    }

    Write-Host ""
    Write-Host "Verifying parts..." -ForegroundColor Yellow
    $verifyFailed = $false

    for ($i = 0; $i -lt $Parts; $i++) {
        $pf = $partFiles[$i]
        if (-not (Test-Path $pf)) {
            Write-Host "  Part $($i+1): MISSING" -ForegroundColor Red
            $verifyFailed = $true
            continue
        }
        $actualSize = (Get-Item $pf).Length
        $expectedSize = $partList[$i].size
        if ($actualSize -ne $expectedSize) {
            Write-Host "  Part $($i+1): SIZE MISMATCH (expected $expectedSize, got $actualSize)" -ForegroundColor Red
            $verifyFailed = $true
        } else {
            Write-Host "  Part $($i+1): $([math]::Round($actualSize / 1MB, 1)) MB OK" -ForegroundColor Green
        }
    }

    if ($verifyFailed) {
        Write-Host ""
        Write-Host "[ERROR] Part verification failed. Re-run to retry." -ForegroundColor Red
        return
    }

    Write-Host ""
    Write-Host "Merging parts..." -ForegroundColor Yellow

    $tmpFinal = $finalPath + ".tmp"
    $merged = $null
    try {
        $merged = [System.IO.File]::Create($tmpFinal)
        $buffer = New-Object byte[] 4MB
        for ($i = 0; $i -lt $Parts; $i++) {
            $pf = $partFiles[$i]
            $fs = [System.IO.File]::OpenRead($pf)
            $bytesRead = $fs.Read($buffer, 0, $buffer.Length)
            while ($bytesRead -gt 0) {
                $merged.Write($buffer, 0, $bytesRead)
                $bytesRead = $fs.Read($buffer, 0, $buffer.Length)
            }
            $fs.Close()
        }
        $merged.Close()
        $merged = $null

        $actualSize = (Get-Item $tmpFinal).Length
        if ($actualSize -ne $contentLength) {
            throw "Final file size mismatch: expected $contentLength, got $actualSize"
        }

        if (Test-Path $finalPath) { Remove-Item $finalPath -Force }
        Rename-Item $tmpFinal $finalPath

    } catch {
        if ($merged) { $merged.Close(); $merged = $null }
        if (Test-Path $tmpFinal) { Remove-Item $tmpFinal -Force }
        Write-Host ""
        Write-Host "[ERROR] Merge failed: $_" -ForegroundColor Red
        Write-Host "Parts preserved at: $tmpDir" -ForegroundColor Yellow
        return
    }

    $finalSizeMB = [math]::Round((Get-Item $finalPath).Length / 1MB, 1)
    $totalTime = ((Get-Date) - $startTime).TotalSeconds
    $avgSpeed = if ($totalTime -gt 0) { [math]::Round($finalSizeMB * 1024 / $totalTime, 1) } else { 0 }

    Write-Host ""
    Write-Host "=== Download Complete ===" -ForegroundColor Green
    Write-Host "File:     $finalPath"
    Write-Host "Size:     $finalSizeMB MB"
    Write-Host "Time:     $([math]::Round($totalTime, 1))s"
    Write-Host "Avg speed: ${avgSpeed} KB/s"

    Remove-Item -Path $tmpDir -Recurse -Force -ErrorAction SilentlyContinue

} finally {
    $jobs | Where-Object { $_ -ne $null } | Remove-Job -Force -ErrorAction SilentlyContinue
}
