package main

import (
	"os"
	"path/filepath"
	"testing"
)

func TestSetuoraRemoteVariants(t *testing.T) {
	valid := []string{
		"https://github.com/Setuora/Setuora-Local.git",
		"https://github.com/Setuora/Setuora-Local/",
		"git@github.com:Setuora/Setuora-Local.git",
		"ssh://git@github.com/Setuora/Setuora-Local.git",
	}
	for _, remote := range valid {
		if !isSetuoraRemote(remote) {
			t.Errorf("expected valid Setuora remote: %s", remote)
		}
	}
	if isSetuoraRemote("https://github.com/example/Setuora-Local.git") {
		t.Fatal("accepted an unrelated repository")
	}
	if isSetuoraRemote("http://github.com/Setuora/Setuora-Local.git") {
		t.Fatal("accepted an insecure HTTP repository")
	}
	if isSetuoraRemote("https://github.com/Dijo-404/Proj_Setu.git") {
		t.Fatal("accepted the retired Setuora repository")
	}
}

func TestLegacySetuoraRemoteIsRecognizedOnlyForMigration(t *testing.T) {
	valid := []string{
		"https://github.com/Dijo-404/Proj_Setu.git",
		"git@github.com:Dijo-404/Proj_Setu.git",
	}
	for _, remote := range valid {
		if !isLegacySetuoraRemote(remote) {
			t.Errorf("expected legacy Setuora remote: %s", remote)
		}
	}
	if isLegacySetuoraRemote("http://github.com/Dijo-404/Proj_Setu.git") {
		t.Fatal("accepted an insecure legacy repository URL")
	}
	if isLegacySetuoraRemote("https://github.com/example/Proj_Setu.git") {
		t.Fatal("accepted an unrelated legacy repository")
	}
}

func TestPowerShellQuote(t *testing.T) {
	if got, want := powershellQuote(`C:\User's Apps\Setuora.exe`), `'C:\User''s Apps\Setuora.exe'`; got != want {
		t.Fatalf("powershellQuote() = %q, want %q", got, want)
	}
}

func TestWindowsQuoteArgument(t *testing.T) {
	tests := []struct {
		input string
		want  string
	}{
		{"main", "main"},
		{"", `""`},
		{`C:\Setuora`, `C:\Setuora`},
		{`C:\Program Files\Setuora`, `"C:\Program Files\Setuora"`},
		{`C:\folder with spaces\`, `"C:\folder with spaces\\"`},
	}
	for _, test := range tests {
		if got := windowsQuoteArgument(test.input); got != test.want {
			t.Errorf("windowsQuoteArgument(%q) = %q, want %q", test.input, got, test.want)
		}
	}
}

func TestValidBranch(t *testing.T) {
	valid := []string{"main", "release/2026.07", "hotfix-1"}
	for _, branch := range valid {
		if !validBranch.MatchString(branch) {
			t.Errorf("rejected valid branch %q", branch)
		}
	}
	invalid := []string{"-main", "main branch", "main;calc.exe"}
	for _, branch := range invalid {
		if validBranch.MatchString(branch) {
			t.Errorf("accepted invalid branch %q", branch)
		}
	}
}

func TestParseOptionsSupportsUnifiedCommands(t *testing.T) {
	options, err := parseOptions([]string{"start", "--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions() returned %v", err)
	}
	if options.command != "start" {
		t.Fatalf("command = %q, want start", options.command)
	}
	if !options.withCaddy {
		t.Fatal("Caddy should be enabled by default")
	}

	options, err = parseOptions([]string{"repair", "--install-dir", `C:\Setuora`, "--port", "8123"})
	if err != nil {
		t.Fatalf("parseOptions(repair) returned %v", err)
	}
	if options.command != "repair" || options.port != 8123 {
		t.Fatalf("repair options = command %q, port %d", options.command, options.port)
	}

	options, err = parseOptions([]string{"--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions() returned %v", err)
	}
	if options.command != "setup" {
		t.Fatalf("legacy flags command = %q, want setup", options.command)
	}

	options, err = parseOptions([]string{"setup", "--with-caddy=false", "--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions(Caddy opt-out) returned %v", err)
	}
	if options.withCaddy {
		t.Fatal("--with-caddy=false did not disable Caddy")
	}
}

func TestMenuOffersStartStopAndUninstall(t *testing.T) {
	cases := map[string]string{"1\r\n": "start", " 2\n": "stop", "3": "uninstall", "0": "", "4": "", "start": ""}
	for choice, want := range cases {
		if got := menuCommand(choice); got != want {
			t.Errorf("menuCommand(%q) = %q, want %q", choice, got, want)
		}
	}
}

func TestParseOptionsAcceptsUninstall(t *testing.T) {
	options, err := parseOptions([]string{"uninstall", "--install-dir", `C:\Setuora`})
	if err != nil {
		t.Fatalf("parseOptions(uninstall) returned %v", err)
	}
	if options.command != "uninstall" {
		t.Fatalf("command = %q, want uninstall", options.command)
	}
}

func TestBrowserURLPrefersCaddyHTTPSAddress(t *testing.T) {
	installDir := t.TempDir()
	options := installerOptions{installDir: installDir, port: 8123}
	if got := browserURL(options); got != "http://127.0.0.1:8123" {
		t.Fatalf("browserURL without Caddy = %q", got)
	}

	caddyDir := filepath.Join(installDir, "deployment", "caddy")
	if err := os.MkdirAll(caddyDir, 0o755); err != nil {
		t.Fatal(err)
	}
	caddyfile := "https://192.168.1.20 {\r\n\ttls internal\r\n\treverse_proxy 127.0.0.1:8000\r\n}\r\n"
	if err := os.WriteFile(filepath.Join(caddyDir, "Caddyfile"), []byte(caddyfile), 0o644); err != nil {
		t.Fatal(err)
	}
	if got := browserURL(options); got != "https://192.168.1.20" {
		t.Fatalf("browserURL with Caddy = %q, want https://192.168.1.20", got)
	}
}
