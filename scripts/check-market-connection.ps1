$ErrorActionPreference = 'Stop'
if (-not $env:FINANCIAL_DATASETS_API_KEY) {
    $env:FINANCIAL_DATASETS_API_KEY = [Environment]::GetEnvironmentVariable('FINANCIAL_DATASETS_API_KEY', 'User')
}
if (-not $env:FINANCIAL_DATASETS_API_KEY) {
    throw '请先在用户环境变量中设置 FINANCIAL_DATASETS_API_KEY；不要在聊天或命令行填写密钥。'
}
Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    python -m mercury mcp-tools
    if ($LASTEXITCODE -ne 0) { throw 'MCP 连接检查未通过，详见已脱敏错误。' }
}
finally {
    Pop-Location
}
