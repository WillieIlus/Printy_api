# Create and activate virtualenv for printy_API
# Run: .\setup_venv.ps1

$venvPath = "venv"
if (-not (Test-Path $venvPath)) {
    python -m venv $venvPath
    Write-Host "Created virtualenv at $venvPath"
}
& "$venvPath\Scripts\Activate.ps1"
pip install -r requirements.txt
Write-Host "Virtualenv ready. Activate with: .\venv\Scripts\Activate.ps1"
