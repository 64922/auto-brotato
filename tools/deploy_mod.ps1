#Requires -Version 5.1
<#
.SYNOPSIS
    AutoBrotato mod 部署脚本（票据 01）。

.DESCRIPTION
    将仓库 mod/ 源码打包为 <modId>-<version>.zip 并部署到 Brotato workshop 目录：
      1. 打包 mod/ —— zip 内结构为 mods-unpacked/<modId>/...；
      2. 备份既有 zip（含同 mod 旧版本）与 %APPDATA%\Brotato\ 相关配置到
         backups\<yyyyMMdd-HHmmss>\，并写入 sha256 清单 manifest.json；
      3. 写入 workshop 目录，替换同名 zip，删除同 mod 旧版本 zip（避免 ModLoader 重复加载）；
      4. 更新 mod_user_profiles.json（is_active / zip_path）与 auto_brotato_deploy.json
         （沿用既有字段：mod_id/item_id/item_dir/zip_path/profile_path/deployed_at）。
    脚本幂等：可重复执行，每次覆盖前都会新建时间戳备份。

.PARAMETER ModDir
    仓库 mod 源码目录（含 manifest.json），默认 <仓库根>\mod。

.PARAMETER WorkshopDir
    Brotato 创意工坊物品目录，默认
    E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307。

.PARAMETER AppDataDir
    Brotato 用户配置目录，默认 %APPDATA%\Brotato。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File tools\deploy_mod.ps1
#>
[CmdletBinding()]
param(
    [string]$ModDir = (Join-Path $PSScriptRoot '..\mod'),
    [string]$WorkshopDir = 'E:\SteamLibrary\steamapps\workshop\content\1942280\3814007307',
    [string]$AppDataDir = (Join-Path $env:APPDATA 'Brotato')
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.IO.Compression | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem | Out-Null

# 以制表符 + LF 写出 JSON（与 ModLoader / 既有配置文件风格一致，无 BOM）。
function Write-JsonFile {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][AllowNull()]$Object
    )
    $json = ConvertTo-TabJson -Value $Object
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $json + "`n", $utf8NoBom)
}

function ConvertTo-TabJson {
    param(
        [Parameter(Mandatory)][AllowNull()]$Value,
        [int]$Level = 0
    )
    $indent = "`t" * $Level
    $inner = "`t" * ($Level + 1)
    if ($null -eq $Value) {
        return 'null'
    }
    if ($Value -is [bool]) {
        return $(if ($Value) { 'true' } else { 'false' })
    }
    if ($Value -is [string] -or $Value -is [ValueType]) {
        return (ConvertTo-Json -InputObject $Value -Compress)
    }
    if ($Value -is [System.Management.Automation.PSCustomObject]) {
        $pairs = @()
        foreach ($prop in $Value.PSObject.Properties) {
            $pairs += "$inner$(ConvertTo-Json -InputObject ([string]$prop.Name) -Compress): $(ConvertTo-TabJson -Value $prop.Value -Level ($Level + 1))"
        }
        if ($pairs.Count -eq 0) { return '{}' }
        return "{`n" + ($pairs -join ",`n") + "`n$indent}"
    }
    if ($Value -is [System.Collections.IDictionary]) {
        $pairs = @()
        foreach ($key in $Value.Keys) {
            $pairs += "$inner$(ConvertTo-Json -InputObject ([string]$key) -Compress): $(ConvertTo-TabJson -Value $Value[$key] -Level ($Level + 1))"
        }
        if ($pairs.Count -eq 0) { return '{}' }
        return "{`n" + ($pairs -join ",`n") + "`n$indent}"
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $items = @($Value)
        if ($items.Count -eq 0) { return '[]' }
        $pairs = foreach ($item in $items) { "$inner$(ConvertTo-TabJson -Value $item -Level ($Level + 1))" }
        return "[`n" + ($pairs -join ",`n") + "`n$indent]"
    }
    return (ConvertTo-Json -InputObject $Value -Compress)
}

function Get-Sha256Lower {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

# 备份既有文件到 <BackupRoot>\<yyyyMMdd-HHmmss>（同秒重名自动加序号），
# 写入 sha256 清单 manifest.json；无文件可备份时返回 $null。
function New-DeployBackup {
    param(
        [Parameter(Mandatory)][string]$BackupRoot,
        [Parameter(Mandatory)][string]$ModId,
        [string[]]$Files = @()
    )
    $existing = @($Files | Where-Object { $_ -and (Test-Path -LiteralPath $_) })
    if ($existing.Count -eq 0) {
        Write-Host '  [备份] 无既有文件（首次部署），跳过。'
        return $null
    }
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backupDir = Join-Path $BackupRoot $stamp
    $suffix = 2
    while (Test-Path -LiteralPath $backupDir) {
        $backupDir = Join-Path $BackupRoot "$stamp-$suffix"
        $suffix++
    }
    New-Item -ItemType Directory -Path $backupDir | Out-Null
    $entries = [ordered]@{}
    foreach ($file in $existing) {
        $name = Split-Path -Leaf $file
        if ($entries.Contains($name)) {
            # 同名文件（理论上不会出现）：加序号避免覆盖
            $i = 2
            while ($entries.Contains("$i-$name")) { $i++ }
            $name = "$i-$name"
        }
        Copy-Item -LiteralPath $file -Destination (Join-Path $backupDir $name) -Force
        $entries[$name] = [ordered]@{
            sha256 = Get-Sha256Lower -Path $file
            size   = (Get-Item -LiteralPath $file).Length
        }
    }
    Write-JsonFile -Path (Join-Path $backupDir 'manifest.json') -Object ([ordered]@{
        operation  = "deploy-$ModId"
        created_at = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'
        files      = $entries
    })
    return $backupDir
}

function Invoke-Deploy {
    $modRoot = (Resolve-Path -LiteralPath $ModDir).Path
    $manifestPath = Join-Path $modRoot 'manifest.json'
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "mod manifest 不存在：$manifestPath"
    }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $namespace = [string]$manifest.namespace
    $name = [string]$manifest.name
    $version = [string]$manifest.version_number
    if ([string]::IsNullOrWhiteSpace($version)) { throw 'manifest.json 缺少 version_number。' }
    $modId = @($namespace, $name) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | Select-Object -First 1
    if (-not [string]::IsNullOrWhiteSpace($namespace) -and -not [string]::IsNullOrWhiteSpace($name)) {
        $modId = "$namespace-$name"
    }
    if ([string]::IsNullOrWhiteSpace($modId)) { throw 'manifest.json 缺少 namespace/name，无法确定 mod id。' }

    $workshopRoot = [System.IO.Path]::GetFullPath($WorkshopDir)
    $appDataRoot = [System.IO.Path]::GetFullPath($AppDataDir)
    $backupRoot = Join-Path $appDataRoot 'backups'
    $zipName = "$modId-$version.zip"
    $targetZip = Join-Path $workshopRoot $zipName
    $tmpZip = Join-Path $workshopRoot ".deploy-tmp-$zipName"
    $profilePath = Join-Path $appDataRoot 'mod_user_profiles.json'
    $deployPath = Join-Path $appDataRoot 'auto_brotato_deploy.json'
    $itemId = Split-Path -Leaf $workshopRoot

    Write-Host "== AutoBrotato 部署 =="
    Write-Host "  源码:   $modRoot"
    Write-Host "  版本:   $version ($modId)"
    Write-Host "  目标:   $targetZip"

    if (Get-Process -Name 'Brotato' -ErrorAction SilentlyContinue) {
        Write-Warning '检测到 Brotato.exe 正在运行：文件已写入磁盘，但需重启游戏后才会加载新 zip。'
    }

    New-Item -ItemType Directory -Path $workshopRoot -Force | Out-Null
    New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null

    # 1) 打包 zip（结构与既有包一致：mods-unpacked/<modId>/...）
    $staleZips = @(Get-ChildItem -LiteralPath $workshopRoot -Filter "$modId-*.zip" -File |
        Where-Object { $_.Name -ne $zipName })
    $staleZipPaths = @($staleZips | Select-Object -ExpandProperty FullName)
    if (Test-Path -LiteralPath $tmpZip) { Remove-Item -LiteralPath $tmpZip -Force }
    $archive = [System.IO.Compression.ZipFile]::Open($tmpZip, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        $fileCount = 0
        foreach ($file in (Get-ChildItem -LiteralPath $modRoot -Recurse -File | Sort-Object FullName)) {
            $relative = $file.FullName.Substring($modRoot.Length + 1).Replace('\', '/')
            $entryName = "mods-unpacked/$modId/$relative"
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile(
                $archive, $file.FullName, $entryName, [System.IO.Compression.CompressionLevel]::Optimal)
            $fileCount++
        }
    }
    finally {
        $archive.Dispose()
    }
    Write-Host "  [打包] $fileCount 个文件 -> $zipName"

    # 2) 备份既有 zip（当前 + 旧版本）与配置（修改前）
    $backupFiles = @($targetZip) + $staleZipPaths + @($profilePath, $deployPath)
    $backupDir = New-DeployBackup -BackupRoot $backupRoot -ModId $modId -Files $backupFiles
    if ($backupDir) { Write-Host "  [备份] $backupDir" }

    # 3) 写入 workshop 目录（同卷 Move 覆盖），并清理旧版本 zip
    Move-Item -LiteralPath $tmpZip -Destination $targetZip -Force
    Write-Host "  [写入] $targetZip"
    foreach ($stale in $staleZips) {
        Remove-Item -LiteralPath $stale.FullName -Force
        Write-Host "  [清理] 旧版本 $($stale.Name)"
    }

    # 4) 更新 mod_user_profiles.json：确保当前 profile 激活指向新 zip
    if (Test-Path -LiteralPath $profilePath) {
        $profile = Get-Content -LiteralPath $profilePath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    else {
        $profile = [PSCustomObject]@{}
    }
    $currentProfile = [string]$profile.current_profile
    if ([string]::IsNullOrWhiteSpace($currentProfile)) {
        $currentProfile = 'default'
        $profile | Add-Member -NotePropertyName current_profile -NotePropertyValue $currentProfile -Force
    }
    if ($null -eq $profile.profiles) {
        $profile | Add-Member -NotePropertyName profiles -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    if ($null -eq $profile.profiles.$currentProfile) {
        $profile.profiles | Add-Member -NotePropertyName $currentProfile -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    $activeProfile = $profile.profiles.$currentProfile
    if ($null -eq $activeProfile.mod_list) {
        $activeProfile | Add-Member -NotePropertyName mod_list -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    $modEntry = $activeProfile.mod_list.$modId
    if ($null -eq $modEntry) {
        $modEntry = [PSCustomObject]@{}
        $activeProfile.mod_list | Add-Member -NotePropertyName $modId -NotePropertyValue $modEntry -Force
    }
    $modEntry | Add-Member -NotePropertyName is_active -NotePropertyValue $true -Force
    $modEntry | Add-Member -NotePropertyName zip_path -NotePropertyValue $targetZip -Force
    Write-JsonFile -Path $profilePath -Object $profile
    Write-Host "  [配置] mod_user_profiles.json -> $targetZip"

    # 5) 更新 auto_brotato_deploy.json（沿用既有字段）
    Write-JsonFile -Path $deployPath -Object ([ordered]@{
        mod_id      = $modId
        item_id     = $itemId
        item_dir    = $workshopRoot
        zip_path    = $targetZip
        profile_path = $profilePath
        deployed_at = Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz'
    })
    Write-Host "  [配置] auto_brotato_deploy.json"

    Write-Host '== 部署完成：重启 Brotato 后由 ModLoader 加载。 =='
}

try {
    Invoke-Deploy
}
catch {
    Write-Error ("部署失败：" + $_.Exception.Message)
    exit 1
}
