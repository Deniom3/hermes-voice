package main

import (
	"encoding/hex"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Кадр собран по схеме Luna и сверен с её эталоном для 50%.
func TestQuasarSetFrameMatchesLunaGolden(t *testing.T) {
	golden := "33000000e20f2dfa052a0a280a260a0964697265637469766512" +
		"0a766f6c756d655f73657420017a070d000048421801a001fe01e80f58"
	want, err := hex.DecodeString(golden)
	if err != nil {
		t.Fatal(err)
	}
	got := quasarSetFrame(50)
	if len(got) != len(want) {
		t.Fatalf("длина кадра %d, ожидалась %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Fatalf("байт %d: %02x, ожидался %02x", i, got[i], want[i])
		}
	}
}

func TestVolumeBuildVerified(t *testing.T) {
	good := "0.369.1.46.4778262442.20261013.297"
	if !volumeBuildVerified(good, []byte("yandex,station-micro\x00amlogic,axg\x00")) {
		t.Fatal("наша сборка и плата должны быть разрешены")
	}
	if volumeBuildVerified(good, []byte("amlogic,axg\x00yandex,station-mini2\x00")) {
		t.Fatal("чужая плата не должна проходить")
	}
	if volumeBuildVerified("0.369.1.46.4778262442.20261013.298", []byte("yandex,station-micro\x00")) {
		t.Fatal("другая сборка не должна проходить")
	}
	if !volumeBuildVerified("0.369.1.50.4794654278.20261021.133", []byte("yandex,prune-evt2\x00yandex,prune\x00")) {
		t.Fatal("Лайт 2 должна проходить по своей сборке и плате")
	}
}

func TestQuasarPortFromConfig(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "quasar.cfg")
	body := `{"iohub_clients":{"port":9910},"volume_manager":{"port":9922},` +
		`"some_string_service":"value","nested":{"port":"not a number"}}`
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	old := quasarConfigPath
	quasarConfigPath = path
	defer func() { quasarConfigPath = old }()

	port, err := quasarPort("iohub_clients")
	if err != nil {
		t.Fatal(err)
	}
	if port != 9910 {
		t.Fatalf("порт %d, ожидался 9910", port)
	}
	if _, err := quasarPort("нет_такого"); err == nil {
		t.Fatal("для отсутствующего сервиса должна быть ошибка")
	}
}

func TestQuasarVolumeSetSendsFrame(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()

	dir := t.TempDir()
	write := func(name, body string) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
		return path
	}
	port := listener.Addr().(*net.TCPAddr).Port
	cfg := write("quasar.cfg", `{"iohub_clients":{"port":`+itoa(port)+`}}`)
	version := write("version_full", "0.369.1.46.4778262442.20261013.297\n")
	compatible := write("compatible", "yandex,station-micro\x00amlogic,axg\x00")

	oldCfg, oldVer, oldComp := quasarConfigPath, quasarVersionPath, quasarCompatiblePath
	quasarConfigPath, quasarVersionPath, quasarCompatiblePath = cfg, version, compatible
	defer func() {
		quasarConfigPath, quasarVersionPath, quasarCompatiblePath = oldCfg, oldVer, oldComp
	}()

	received := make(chan []byte, 1)
	go func() {
		conn, err := listener.Accept()
		if err != nil {
			return
		}
		defer conn.Close()
		_ = conn.SetReadDeadline(time.Now().Add(2 * time.Second))
		buf := make([]byte, 256)
		n, _ := conn.Read(buf)
		received <- buf[:n]
	}()

	if err := quasarVolumeSet(30); err != nil {
		t.Fatal(err)
	}
	select {
	case got := <-received:
		want := quasarSetFrame(30)
		if len(got) != len(want) {
			t.Fatalf("получено %d байт, ожидалось %d", len(got), len(want))
		}
		for i := range want {
			if got[i] != want[i] {
				t.Fatalf("байт %d: %02x, ожидался %02x", i, got[i], want[i])
			}
		}
	case <-time.After(2 * time.Second):
		t.Fatal("кадр не пришёл")
	}
}

func TestQuasarVolumeSetRejectsUnverified(t *testing.T) {
	dir := t.TempDir()
	version := filepath.Join(dir, "version_full")
	if err := os.WriteFile(version, []byte("0.0.0"), 0o644); err != nil {
		t.Fatal(err)
	}
	oldVer := quasarVersionPath
	quasarVersionPath = version
	defer func() { quasarVersionPath = oldVer }()

	if err := quasarVolumeSet(30); err == nil {
		t.Fatal("для непроверенной прошивки должна быть ошибка")
	}
}

func itoa(v int) string {
	if v == 0 {
		return "0"
	}
	var digits []byte
	for v > 0 {
		digits = append([]byte{byte('0' + v%10)}, digits...)
		v /= 10
	}
	return string(digits)
}
