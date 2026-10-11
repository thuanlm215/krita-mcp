<#
.SYNOPSIS
    Installs the Krita MCP bridge plugin and enables it.

.DESCRIPTION
    Copies the bundled plugin into Krita's resource folder and enables it in
    kritarc. Safe to re-run. Close Krita first: it rewrites kritarc on exit.

.PARAMETER Force
    Enable the plugin even if Krita is currently running.
#>
[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$arguments = @("$here\mcp_server.py", "install")
if ($Force) {
    $arguments += "--force"
}
& python @arguments
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
