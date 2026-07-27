from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = PROJECT_ROOT / "scripts"


def test_windows_workflows_are_grouped_under_scripts_directory():
    for filename in ("setup.bat", "start_setuora.bat", "stop_setuora.bat", "update.bat"):
        assert (WORKFLOWS_DIR / filename).is_file()
        assert not (PROJECT_ROOT / filename).exists()


def test_setup_skips_stop_helper_for_fresh_install():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    stop_section = setup_script.split('Write-Section "Stop Existing Server"', 1)[1]
    stop_section = stop_section.split('Write-Section "Python"', 1)[0]

    assert "if ((Test-Path $VenvPython) -or $Repair)" in stop_section
    assert "Fresh installation; there is no existing Setuora server to stop." in stop_section


def test_requirements_pin_direct_starlette_import():
    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert "starlette==1.3.1" in requirements


def test_lockfile_excludes_uvloop_on_windows_and_pins_windows_dependencies():
    lockfile = (PROJECT_ROOT / "requirements.lock").read_text(encoding="utf-8")

    uvloop_line = next(line for line in lockfile.splitlines() if line.startswith("uvloop=="))
    assert "sys_platform != 'win32'" in uvloop_line
    assert "colorama==0.4.6 ; sys_platform == 'win32'" in lockfile


def test_setup_repairs_pip_and_checks_dependencies():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    assert "function Ensure-Pip" in setup_script
    assert "$RequirementsLock = Join-Path $ProjectRoot \"requirements.lock\"" in setup_script
    assert "pip install --require-hashes -r $RequirementsLock" in setup_script
    assert "& $VenvPython -m ensurepip --upgrade" in setup_script
    assert "& $VenvPython -m pip check" in setup_script
    assert 'import uvicorn; from app.main import app; print(\'App import OK\')' in setup_script


def test_setup_requires_the_verified_python_311_runtime():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    version_check = "sys.version_info[:2] == (3, 11)"
    assert setup_script.count(version_check) == 2
    assert "sys.version_info >= (3, 11)" not in setup_script
    assert 'Read-YesNo "Python 3.11 is required. Install it now with winget?"' in setup_script
    assert 'throw "Python 3.11 was not found.' in setup_script


def test_setup_has_a_data_preserving_repair_mode():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    assert "[switch]$Repair" in setup_script
    assert 'Write-Host "Existing .env settings preserved."' in setup_script
    assert "Existing Caddy HTTPS service repaired and configured for automatic startup." in setup_script
    assert "The virtual environment is damaged or incompatible. Rebuilding it" in setup_script
    assert "& $VenvPython -m pytest -q" in setup_script
    assert "Get-SetuoraServerLaunchInfo" in setup_script
    assert 'Start-ManagedService -Name $AppServiceName -DisplayName "Setuora"' in setup_script


def test_updater_preserves_clean_diverged_history_before_realigning():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    assert "& git pull " not in update_script
    assert "& git rebase " not in update_script
    assert "& git fetch --no-tags origin $branch" in update_script
    assert "& git merge-base --is-ancestor $previousHead $fetchedHead" in update_script
    assert "& git merge --ff-only FETCH_HEAD" in update_script
    assert "& git branch $backupBranch $previousHead" in update_script
    assert "& git reset --hard FETCH_HEAD" in update_script
    assert update_script.index("& git branch $backupBranch $previousHead") < update_script.index(
        "& git reset --hard FETCH_HEAD"
    )
    assert "Refusing to update because local source changes are present." in update_script
    assert "$worktreeChanges = @(& git status --porcelain)" in update_script


def test_updater_starts_services_without_changing_current_source():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    assert 'if ("$fetchedHead" -eq "$previousHead")' not in update_script
    assert "if ($fetchedHead -eq $previousHead)" in update_script
    assert "Setuora source is already up to date." in update_script
    current_block = update_script.split("if ($fetchedHead -eq $previousHead)", 1)[1].split(
        "# Prefer a normal fast-forward", 1
    )[0]
    assert "Start-SetuoraServer | Out-Null" in current_block
    assert "return" in current_block
    assert update_script.index("if ($fetchedHead -eq $previousHead)") < update_script.index(
        '& git merge --ff-only FETCH_HEAD'
    )


def test_unified_updater_releases_the_executable_before_git_merge():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")
    installer_source = (PROJECT_ROOT / "installer" / "main.go").read_text(encoding="utf-8")

    assert 'if "%SETUORA_LAUNCHED_UPDATE%"=="1"' in update_script
    assert '"SETUORA_LAUNCHED_UPDATE=1"' in installer_source
    assert "command.Start()" in installer_source
    assert 'runGitVisible(gitPath, installDir, "branch", backupBranch, currentHead)' in installer_source
    assert 'runGitVisible(gitPath, installDir, "reset", "--hard", "FETCH_HEAD")' in installer_source
    assert "local source changes are present; setup will not overwrite them" in installer_source


def test_updater_checks_service_permissions_before_fetching():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    permission_check = update_script.index("if (($service -or $caddyService) -and -not (Test-AdminShell))")
    fetch = update_script.index("& git fetch --no-tags origin $branch")

    assert permission_check < fetch


def test_updater_repairs_pip_and_checks_dependencies():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    assert "function Ensure-Pip" in update_script
    assert "& $VenvPython -m ensurepip --upgrade" in update_script
    assert "& $VenvPython -m pip install --upgrade pip" in update_script
    assert "& $VenvPython -m pip check" in update_script
    assert 'import uvicorn; from app.main import app; print(\'App import OK\')' in update_script


def test_updater_starts_setuora_by_default_after_update():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    assert "deployment\\windows\\server_processes.ps1" in update_script
    assert "$restartAsService = [bool]$service" in update_script
    assert "$runningSetuoraProcesses = @(Get-SetuoraServerProcesses" in update_script
    assert "$restartAsConsole = -not $restartAsService" in update_script
    assert 'Start-Process -FilePath $StartScript -ArgumentList @("-HostAddress", "$restartHostAddress", "-Port", "$restartPort", "--console-only")' in update_script
    assert "Setuora was not running before the update; leaving it stopped." not in update_script


def test_start_script_uses_state_aware_windows_helper():
    start_script = (WORKFLOWS_DIR / "start_setuora.bat").read_text(encoding="utf-8")
    helper = (PROJECT_ROOT / "deployment" / "windows" / "start_setuora.ps1").read_text(
        encoding="utf-8"
    )

    assert "deployment\\windows\\start_setuora.ps1" in start_script
    assert "-HostAddress \"%HOST_ADDRESS%\" -Port \"%PORT%\"" in start_script
    assert "Get-Service -Name $ServiceName" in helper
    assert "[switch]$ConsoleOnly" in helper
    assert "if ($service -and -not $ConsoleOnly)" in helper
    assert 'if "%CONSOLE_ONLY%"=="1" set "CONSOLE_ONLY_ARG=-ConsoleOnly"' in start_script
    assert "Get-SetuoraServerProcesses" in helper
    assert "Setuora is already running in another window or background process." in helper
    assert "function Start-CaddyProxy" in helper
    assert "sc.exe config $ServiceName start= auto" in helper
    assert "sc.exe config $CaddyServiceName start= auto depend= $ServiceName" in helper
    assert "Caddy HTTPS started and then stopped" in helper
    assert 'Wait-HealthEndpoint -Uri "http://127.0.0.1:$Port/health"' in helper
    assert 'Wait-HealthEndpoint -Uri "https://$caddyAddress/health"' in helper
    assert helper.index('Start-Service -Name $ServiceName') < helper.index(
        "Start-CaddyProxy | Out-Null"
    )


def test_unified_start_and_stop_request_administrator_access():
    installer_source = (PROJECT_ROOT / "installer" / "main.go").read_text(encoding="utf-8")

    elevation_condition = installer_source.split('if options.command == "setup"', 1)[1].split("{", 1)[0]
    assert 'options.command == "start"' in elevation_condition
    assert 'options.command == "stop"' in elevation_condition


def test_start_helper_repairs_missing_dependencies_before_launch():
    helper = (PROJECT_ROOT / "deployment" / "windows" / "start_setuora.ps1").read_text(
        encoding="utf-8"
    )

    assert "function Ensure-AppDependencies" in helper
    assert "import uvicorn; from app.main import app" in helper
    assert "Set-Location $projectRoot" in helper
    assert "Test-AppDependencies -PythonExe $PythonExe -Quiet" in helper
    assert "& $PythonExe -m ensurepip --upgrade" in helper
    assert "$requirementsPath = Join-Path $projectRoot \"requirements.lock\"" in helper
    assert "& $PythonExe -m pip install --require-hashes -r $RequirementsPath" in helper
    assert "Ensure-AppDependencies -PythonExe $pythonExe -RequirementsPath $requirementsPath" in helper
    assert helper.index("Ensure-AppDependencies -PythonExe $pythonExe") < helper.index(
        "$service = Get-Service -Name $ServiceName"
    )


def test_windows_services_use_the_least_privilege_localservice_account():
    installer = (PROJECT_ROOT / "deployment" / "windows" / "install_service.ps1").read_text(
        encoding="utf-8"
    )
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    assert 'ObjectName "NT AUTHORITY\\LocalService" ""' in installer
    assert '$CaddyServiceStartName = "NT AUTHORITY\\LocalService"' in setup_script
    assert "StartName = $CaddyServiceStartName" in setup_script
    assert "StartPassword = $null" in setup_script
    assert "sc.exe config $CaddyServiceName obj=" not in setup_script
    assert 'StartMode = "Automatic"' in setup_script
    assert "Grant-LocalServiceAccess -Path $dataDir -Access \"M\"" in installer
    assert "Grant-LocalServiceAccess -Path $stateDir -Access \"M\"" in setup_script
    assert "Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START" in installer
    assert "Invoke-Nssm start $ServiceName" not in installer
    assert "sc.exe config $CaddyServiceName start= auto" in setup_script
    assert "sc.exe config $CaddyServiceName depend= $AppServiceName" in setup_script
    assert "sc.exe failureflag $ServiceName 1" in installer


def test_service_install_grants_localservice_access_to_base_python():
    installer = (PROJECT_ROOT / "deployment" / "windows" / "install_service.ps1").read_text(
        encoding="utf-8"
    )

    # The venv python.exe is a redirect stub that launches the base interpreter
    # recorded in .venv\pyvenv.cfg. A per-user (winget) Python lives under a
    # profile LocalService cannot read, so the service fails to start with
    # "No Python at '...'". LocalService must be granted read/execute on the
    # base interpreter home.
    assert "function Get-VenvBasePythonHome" in installer
    assert "pyvenv.cfg" in installer
    assert 'Grant-LocalServiceAccess -Path $basePythonHome -Access "RX"' in installer


def test_service_start_surfaces_the_service_error_log():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    # A failed service start must show the tail of setuora-err.log instead of
    # only pointing at Event Viewer, so the real cause is visible immediately.
    assert "function Get-RecentServiceError" in setup_script
    assert "Recent service log:" in setup_script
    assert (
        'Start-ManagedService -Name $AppServiceName -DisplayName "Setuora" -IncludeSetuoraLog'
        in setup_script
    )


def test_repair_fixes_an_unsafe_bootstrap_password_instead_of_preserving_it():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    config_block = setup_script.split('Write-Section "Configuration"', 1)[1].split(
        'Write-Section "Smoke Test"', 1
    )[0]

    # Repair must not preserve a .env whose bootstrap password would make the
    # service refuse to start on a fresh database; it repairs it instead.
    assert "Test-EnvFileHasSafeBootstrapPassword" in config_block
    assert "Existing .env settings preserved." in config_block
    assert "$credentials = Ensure-EnvFile" in config_block


def test_caddy_service_creation_uses_the_cim_servicetype_parameter_type():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    # Win32_Service.Create expects a CIM UInt8; Invoke-CimMethod does not coerce
    # PowerShell's default Int32 literal and fails before the service is created.
    assert "ServiceType = [byte]16" in setup_script
    assert "ServiceType = 16" not in setup_script


def test_windows_workflows_close_when_their_work_finishes():
    for filename in ("setup.bat", "start_setuora.bat", "stop_setuora.bat", "update.bat"):
        script = (WORKFLOWS_DIR / filename).read_text(encoding="utf-8")

        assert 'if /I "%~1"=="--no-pause"' in script
        assert "\npause" not in script.lower()
        assert "exit /b" in script


def test_windows_workflows_do_not_forward_no_pause_to_powershell_helpers():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")
    start_script = (WORKFLOWS_DIR / "start_setuora.bat").read_text(encoding="utf-8")
    stop_script = (WORKFLOWS_DIR / "stop_setuora.bat").read_text(encoding="utf-8")

    assert setup_script.index('set "SETUORA_SETUP_BAT=%~f0"') < setup_script.index("shift")
    assert update_script.index('set "SETUORA_UPDATE_BAT=%~f0"') < update_script.index("shift")
    assert '" %*' not in setup_script
    assert '" %*' not in update_script
    assert '" %*' not in stop_script
    assert "%1 %2 %3 %4 %5 %6 %7 %8 %9" in setup_script
    assert "%1 %2 %3 %4 %5 %6 %7 %8 %9" in update_script
    assert "%1 %2 %3 %4 %5 %6 %7 %8 %9" in stop_script
    assert "%~dp0.venv" not in start_script
    assert '"%PROJECT_DIR%\\.venv\\Scripts\\python.exe"' in start_script


def test_setup_configures_and_starts_services_and_caddy_by_default():
    setup_script = (WORKFLOWS_DIR / "setup.bat").read_text(encoding="utf-8")

    assert '[switch]$ConfigureCaddy' in setup_script
    assert 'Read-YesNo "Install and configure Caddy for HTTPS access from phones and laptops?" $true' in setup_script
    assert 'Read-YesNo "Install Setuora as an automatic Windows service?" $true' in setup_script
    assert 'Start-ManagedService -Name $AppServiceName -DisplayName "Setuora"' in setup_script
    assert 'Start-ManagedService -Name $CaddyServiceName -DisplayName "Setuora Caddy HTTPS proxy"' in setup_script
    assert 'Read-YesNo "Start Setuora and Caddy now in a new window?" $true' in setup_script
    assert 'Wait-HealthEndpoint -Uri "http://127.0.0.1:$Port/health"' in setup_script
    assert 'Wait-HealthEndpoint -Uri "https://$startupCaddyAddress/health"' in setup_script
    assert setup_script.index('Set-EnvSetting -Name "TRUSTED_HOSTS"') < setup_script.index(
        "$rootCertificate = Install-CaddyService"
    )


def test_updater_restarts_the_optional_https_proxy_with_the_app_service():
    update_script = (WORKFLOWS_DIR / "update.bat").read_text(encoding="utf-8")

    assert '$CaddyServiceName = "SetuoraCaddy"' in update_script
    assert "Start-Service -Name $CaddyServiceName" in update_script
    assert "sc.exe config $ServiceName start= auto" in update_script
    assert "sc.exe config $CaddyServiceName start= auto depend= $ServiceName" in update_script
    assert 'Wait-HealthEndpoint -Uri "https://$caddyAddress/health"' in update_script
    assert update_script.index("Start-Service -Name $ServiceName") < update_script.index(
        "Start-Service -Name $CaddyServiceName"
    )


def test_target_server_preflight_is_available():
    preflight = (PROJECT_ROOT / "deployment" / "windows" / "production_preflight.ps1").read_text(
        encoding="utf-8"
    )

    assert "Assert-LocalServiceIdentity -ServiceName $AppServiceName" in preflight
    assert '$service.StartMode -eq "Auto"' in preflight
    assert "$caddyService.ServicesDependedOn" in preflight
    assert "https://$Address/health" in preflight
    assert "create_scheduled_backup" in preflight
    assert "git -C $projectRoot status --porcelain" in preflight


def test_stop_helper_detects_setuora_process_tree_not_loopback_socket():
    stop_helper = (
        PROJECT_ROOT / "deployment" / "windows" / "stop_setuora.ps1"
    ).read_text(encoding="utf-8")
    process_helper = (
        PROJECT_ROOT / "deployment" / "windows" / "server_processes.ps1"
    ).read_text(encoding="utf-8")

    assert "127.0.0.1" not in stop_helper
    assert "Get-SetuoraServerProcesses" in stop_helper
    assert "Get-SetuoraLauncherProcess" in stop_helper
    assert "start_setuora\\.bat" in process_helper
    assert "start_setuora\\.ps1" in process_helper
    assert "uvicorn.exe" in process_helper
    assert "Get-SetuoraServerLaunchInfo" in process_helper
