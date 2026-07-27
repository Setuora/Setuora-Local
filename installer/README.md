# Setuora Windows Installer

`Setuora.exe` is the single Windows control executable for a Windows 11 server.
It provides setup, repair, update, start, and stop commands. Setup requests administrator
access, installs Git for Windows when needed, clones or updates the official
Setuora repository, and launches the complete `scripts\setup.bat` workflow.

The setup workflow installs Python when needed, creates the virtual environment,
installs Python dependencies, creates or preserves `.env` and application data,
runs an import smoke test, configures Caddy HTTPS by default, and installs and
starts automatic Setuora and Caddy Windows services. Repair rebuilds a damaged
virtual environment, reinstalls verified dependencies, repairs service startup,
preserves settings and data, runs the full tests, and starts the services.

## Build

From this directory with Go installed:

```text
go test ./...
set GOOS=windows
set GOARCH=amd64
go build -trimpath -ldflags="-s -w" -o ..\Setuora.exe .
```

Linux/macOS can cross-compile it with:

```bash
GOOS=windows GOARCH=amd64 go build -trimpath -ldflags="-s -w" -o ../Setuora.exe .
```

The installer requires internet access. The generated executable is unsigned;
Windows SmartScreen may therefore ask the operator to confirm that it should run.

## Options

```text
Setuora.exe setup --install-dir C:\Setuora --branch main
Setuora.exe setup --with-caddy=false
Setuora.exe repair
Setuora.exe update
Setuora.exe start
Setuora.exe stop
```

Updates refuse uncommitted source changes. If a clean installed checkout has
diverged from official release history, its prior commit is preserved on a
timestamped `setuora-backup/...` branch before the verified release is applied.
Application data, settings, and backups are not changed.
