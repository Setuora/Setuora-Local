# LAN HTTPS Guide

Phone cameras usually require HTTPS unless the site is opened as `localhost`. For staff phones on the factory LAN, run a local reverse proxy in front of FastAPI.

## Recommended Shape

```text
Phone browser
  -> https://setuora.local
  -> Caddy
  -> http://127.0.0.1:8000
  -> FastAPI
```

## Caddy

The easiest Windows path is:

1. Run `Setuora.exe setup` as Administrator, or right-click `scripts\setup.bat`
   and choose **Run as administrator**.
2. Confirm the detected LAN IP address, or enter a local DNS name.
3. Install `deployment\caddy\setuora-caddy-root.crt` as a trusted CA certificate
   on every staff phone and laptop.

Setup installs Caddy through WinGet, generates and validates the Caddyfile,
registers the auto-start `SetuoraCaddy` service, and permits LAN traffic on ports
80 and 443. Pass `-SkipCaddy` when running setup if HTTPS is managed separately.

For a manual installation, use `deployment/caddy/Caddyfile.example` as the
starting point and replace `setuora.local` with the real LAN hostname or static IP.

Keep FastAPI bound to localhost behind the proxy:

```text
scripts\start_setuora.bat
```

or:

```powershell
.\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

## Certificate

The automated setup uses Caddy's internal certificate authority and exports its
public root certificate to `deployment\caddy\setuora-caddy-root.crt`. Install that
certificate as a trusted CA on each phone. Back up `deployment\caddy\state`, but
never distribute it because it contains private keys.

For a manual deployment, Caddy's `tls internal` can provide the same local CA,
or you can configure certificates from another trusted certificate tool.

The deployment should remain LAN-only unless the client explicitly asks for remote access.

Automated setup sets the following value in `.env` and restarts an existing
Setuora service. For manual setup, set it yourself and restart Setuora:

```text
SESSION_COOKIE_SECURE=true
```

Leave it `false` while testing over plain `http://127.0.0.1:8000`, otherwise the browser will not send the login cookie over HTTP.
