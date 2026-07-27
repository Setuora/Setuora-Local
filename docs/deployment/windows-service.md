# Windows Service Guide

Use NSSM to run Setuora automatically after reboot. The easier path is to run
`Setuora.exe setup` as Administrator; automatic Setuora and Caddy services are
the default. Use this guide when installing or repairing the service manually.

## Files

- Example install script: `deployment/windows/install_service.ps1`
- Default service name: `SetuoraQrTallyBridge`
- App command: `.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000`
- Logs: `logs\setuora-out.log` and `logs\setuora-err.log`

The installer runs the service as `NT AUTHORITY\LocalService`, not LocalSystem.
It grants that account read access to the application and write access only to
`data\` and `logs\`. If an off-machine backup needs an authenticated network
share, use a separately reviewed service-account deployment instead of granting
the default account broader rights.

## Steps

1. Run `scripts\setup.bat` first so `.venv`, `.env`, `data\`, and `logs\` exist.
2. Download `nssm.exe` and place it somewhere stable, for example `C:\Tools\nssm\nssm.exe`.
3. Open PowerShell as Administrator.
4. Run the install script with the correct paths.

```powershell
.\deployment\windows\install_service.ps1 `
  -ProjectDir "C:\Setuora" `
  -NssmPath "C:\Tools\nssm\nssm.exe"
```

## Service Controls

```powershell
nssm status SetuoraQrTallyBridge
nssm restart SetuoraQrTallyBridge
nssm stop SetuoraQrTallyBridge
```

Logs should be written to:

```text
logs\setuora-out.log
logs\setuora-err.log
```

Restart the service after editing `.env`:

```powershell
nssm restart SetuoraQrTallyBridge
```

If Caddy is used for LAN HTTPS, keep the Setuora service on `127.0.0.1:8000` and let Caddy expose the local HTTPS hostname.
