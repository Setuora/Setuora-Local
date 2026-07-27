# Production Release Checklist

Run this only on the target Windows server after completing `scripts\setup.bat`, Caddy
setup, and Windows service installation.

```powershell
.\deployment\windows\production_preflight.ps1 `
  -ProjectDir "C:\Setuora" `
  -Address "setuora.local"
```

The preflight fails if the source checkout is dirty, either service is not
running as `LocalService`, the Caddy configuration or TLS health endpoint is
invalid, the app is missing browser security headers, tests fail, or a verified
SQLite backup cannot be created. It does not print secrets.

Before declaring the system live, install the exported Caddy root certificate on
each staff phone and complete one real Tally import with a non-production test
voucher. Keep the generated backup outside the server before live use.
