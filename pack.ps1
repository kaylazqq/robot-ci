# Build a clean zip for sharing (no logs/workspaces/caches)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSCommandPath
$outDir = Join-Path $root "dist"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$zip = Join-Path $outDir ("swr-push-helper-{0}.zip" -f (Get-Date -Format "yyyyMMdd-HHmmss"))
$stage = Join-Path $env:TEMP ("swr-pack-" + [guid]::NewGuid().ToString("n"))
$dest = Join-Path $stage "swr-push-helper"
New-Item -ItemType Directory -Force -Path $dest | Out-Null
try {
  foreach ($name in @("start.bat", "stop.bat", "server.py", "services.json", "test-plans.json", "test-suites", "test_runner.py", "README.md", "web", "deploy-linux.sh", "config.example.json")) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination (Join-Path $dest $name) -Recurse -Force
  }
  Compress-Archive -Path $dest -DestinationPath $zip -Force
  Write-Host "packed: $zip"
} finally {
  Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
}
