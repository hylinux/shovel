#requires -Version 5.1
<#
.SYNOPSIS
Creates a ZIP of project files and Git progress reports.
.EXAMPLE
.\scripts\generate-project-snapshot.ps1
.EXAMPLE
.\scripts\generate-project-snapshot.ps1 -OutputPath .\shovel_snapshot.zip
.NOTES
Requires Git and a repository with at least one commit. Excludes .env* paths,
but does not detect secrets in other files, commit messages, or diffs.
#>
[CmdletBinding()]
param(
    [string] $ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string] $OutputPath = (Join-Path ([Environment]::GetFolderPath('UserProfile')) 'shovel_snapshot.zip')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$utf8 = [System.Text.UTF8Encoding]::new($false)
$git = (Get-Command git -CommandType Application -ErrorAction Stop).Source
$ProjectRoot = (Get-Item -LiteralPath $ProjectRoot -ErrorAction Stop).FullName

function Invoke-SnapshotGit {
    param([string] $Arguments)

    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $git
    $start.Arguments = "--no-pager -c core.quotepath=false $Arguments"
    $start.WorkingDirectory = $ProjectRoot
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.StandardOutputEncoding = $utf8
    $start.StandardErrorEncoding = $utf8
    $start.EnvironmentVariables['GIT_OPTIONAL_LOCKS'] = '0'
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    try {
        [void] $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        $process.WaitForExit()
        $text = $stdout.GetAwaiter().GetResult()
        $errorText = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            throw "git $Arguments failed (exit $($process.ExitCode)): $errorText"
        }
        return $text
    }
    finally {
        $process.Dispose()
    }
}

$ProjectRoot = (Invoke-SnapshotGit 'rev-parse --show-toplevel').TrimEnd("`r", "`n")
$OutputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputPath)
if (Test-Path -LiteralPath $OutputPath) {
    throw "Output already exists; choose a different -OutputPath: $OutputPath"
}
if (-not (Test-Path -LiteralPath (Split-Path -Parent $OutputPath) -PathType Container)) {
    throw "Output directory does not exist: $OutputPath"
}

$metadata = [ordered]@{
    '_gitlog.txt' = Invoke-SnapshotGit 'log --oneline --graph -100'
    '_gitstatus.txt' = Invoke-SnapshotGit 'status'
}
$paths = '-- . ":(exclude,glob,icase).env*" ":(exclude,glob,icase)**/.env*" ":(exclude,glob,icase)**/.env*/**"'
$unstaged = Invoke-SnapshotGit "diff --no-ext-diff --no-textconv $paths"
$staged = Invoke-SnapshotGit "diff --cached --no-ext-diff --no-textconv $paths"
$metadata['_uncommitted.diff'] = "# Unstaged changes`n$unstaged`n# Staged changes`n$staged"
$files = @(
    (Invoke-SnapshotGit 'ls-files -co --exclude-standard -z').Split([char] 0) |
        Where-Object { $_ -and $_ -notmatch '(^|/)\.env[^/]*(/|$)' } |
        Sort-Object -Unique
)

Add-Type -AssemblyName System.IO.Compression
$temporaryPath = Join-Path (Split-Path -Parent $OutputPath) ('.shovel-snapshot-' + [guid]::NewGuid() + '.tmp')
$stream = $null
$archive = $null
$count = 0
try {
    $stream = [System.IO.File]::Open($temporaryPath, [System.IO.FileMode]::CreateNew)
    $archive = [System.IO.Compression.ZipArchive]::new(
        $stream, [System.IO.Compression.ZipArchiveMode]::Create, $true
    )
    foreach ($relativePath in $files) {
        $source = Join-Path $ProjectRoot $relativePath.Replace('/', '\')
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            continue
        }
        if ($metadata.Contains($relativePath)) {
            throw "Project file conflicts with a generated report: $relativePath"
        }
        $item = Get-Item -LiteralPath $source -Force
        if ($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
            throw "Symbolic links are not supported in snapshots: $relativePath"
        }
        $entry = $archive.CreateEntry($relativePath, [System.IO.Compression.CompressionLevel]::Optimal)
        $inputStream = [System.IO.File]::OpenRead($source)
        $outputStream = $null
        try {
            $outputStream = $entry.Open()
            $inputStream.CopyTo($outputStream)
        }
        finally {
            if ($null -ne $outputStream) { $outputStream.Dispose() }
            $inputStream.Dispose()
        }
        $count++
    }
    foreach ($name in $metadata.Keys) {
        $entry = $archive.CreateEntry($name)
        $outputStream = $entry.Open()
        try {
            $bytes = $utf8.GetBytes($metadata[$name])
            $outputStream.Write($bytes, 0, $bytes.Length)
        }
        finally {
            $outputStream.Dispose()
        }
    }
    $archive.Dispose()
    $archive = $null
    $stream.Dispose()
    $stream = $null
    [System.IO.File]::Move($temporaryPath, $OutputPath)
}
finally {
    if ($null -ne $archive) { $archive.Dispose() }
    if ($null -ne $stream) { $stream.Dispose() }
    if (Test-Path -LiteralPath $temporaryPath) {
        Remove-Item -LiteralPath $temporaryPath -Force
    }
}

Write-Output "Snapshot created: $OutputPath ($count project files + $($metadata.Count) Git reports)"
