package main

import (
	"net"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// fakeAmixer подменяет amixer: держит raw в файле и умеет sget/sset.
// Позволяет проверять команды агента без ALSA и без колонки.
func fakeAmixer(t *testing.T, start int) (dir string, restore func()) {
	t.Helper()
	dir = t.TempDir()
	state := filepath.Join(dir, "raw")
	if err := os.WriteFile(state, []byte(strconv.Itoa(start)), 0o644); err != nil {
		t.Fatalf("состояние: %v", err)
	}
	script := filepath.Join(dir, "amixer")
	body := `#!/bin/sh
S="` + state + `"
if [ "$3" = "sget" ]; then
  R=$(cat "$S")
  P=$((R * 100 / 255))
  echo "Simple mixer control 'Master',0"
  case "$R" in
    60) DB=-45.89;; 82) DB=-40.71;; 103) DB=-35.77;; 125) DB=-30.59;;
    147) DB=-25.42;; 168) DB=-20.48;; 190) DB=-15.40;; 212) DB=-10.30;;
    233) DB=-5.18;; 255) DB=0.00;; *) DB=-50.00;;
  esac
  echo "  Front Left: Playback $R [$P%] [$DB""dB]"
elif [ "$3" = "sset" ]; then
  echo "$5" > "$S"
fi
`
	if err := os.WriteFile(script, []byte(body), 0o755); err != nil {
		t.Fatalf("скрипт: %v", err)
	}
	old := amixerPath
	amixerPath = script
	// Сбрасываем кэш, чтобы тест не зависел от предыдущего
	volMu.Lock()
	volRaw, volPercent, volDB = -1, -1, 0
	volMu.Unlock()
	return dir, func() { amixerPath = old }
}

func TestPauseWhenNothingPlaysDoesNotStartNativeMusic(t *testing.T) {
	resetMedia(t)
	r := handleCommand(command{Cmd: "pause", ID: "c1"})
	if !r.OK {
		t.Fatalf("пауза в простое вернула ошибку: %q", r.Err)
	}
	if r.WasPlaying {
		t.Fatal("в простое was_playing должен быть false")
	}
	if r.Sendspin == nil || *r.Sendspin {
		t.Fatal("в простое sendspin должен быть false")
	}
}


func readFakeRaw(t *testing.T, dir string) int {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, "raw"))
	if err != nil {
		t.Fatalf("чтение состояния: %v", err)
	}
	v, err := strconv.Atoi(strings.TrimSpace(string(b)))
	if err != nil {
		t.Fatalf("состояние не число: %q", b)
	}
	return v
}

// startQuasar поднимает слушатель вместо iohub_clients и подменяет пути
// quasar, чтобы команды агента уходили в тест, а не в живой maind.
func startQuasar(t *testing.T) (<-chan []byte, func()) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("слушатель: %v", err)
	}
	dir := t.TempDir()
	port := ln.Addr().(*net.TCPAddr).Port
	write := func(name, body string) string {
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
		return path
	}
	cfg := write("quasar.cfg", `{"iohub_clients":{"port":`+strconv.Itoa(port)+`}}`)
	ver := write("version_full", "0.369.1.46.4778262442.20261013.297\n")
	comp := write("compatible", "yandex,station-micro\x00amlogic,axg\x00")

	oldCfg, oldVer, oldComp := quasarConfigPath, quasarVersionPath, quasarCompatiblePath
	quasarConfigPath, quasarVersionPath, quasarCompatiblePath = cfg, ver, comp

	frames := make(chan []byte, 4)
	go func() {
		for {
			conn, err := ln.Accept()
			if err != nil {
				return
			}
			buf := make([]byte, 512)
			n, _ := conn.Read(buf)
			if n > 0 {
				frames <- buf[:n]
			}
			_ = conn.Close()
		}
	}()
	return frames, func() {
		quasarConfigPath, quasarVersionPath, quasarCompatiblePath = oldCfg, oldVer, oldComp
		_ = ln.Close()
	}
}

func TestVolumeSetSendsDirective(t *testing.T) {
	oldWait := volumeApplyWait
	volumeApplyWait = 0
	defer func() { volumeApplyWait = oldWait }()

	cases := []struct{ level, percent int }{{1, 10}, {5, 50}, {10, 100}, {42, 100}, {-3, 0}}
	for _, c := range cases {
		frames, restore := startQuasar(t)
		_, restoreAmixer := fakeAmixer(t, 0)
		r := handleCommand(command{Cmd: "volume_set", ID: "c1", Level: c.level})
		if r.Err != "" {
			t.Fatalf("уровень %d: ошибка %q", c.level, r.Err)
		}
		want := c.level
		if want < 0 {
			want = 0
		}
		if want > 10 {
			want = 10
		}
		if r.Level == nil || *r.Level != want {
			t.Errorf("уровень %d: ответ %v, ждали %d", c.level, r.Level, want)
		}
		select {
		case got := <-frames:
			exp := quasarSetFrame(uint8(c.percent))
			if len(got) != len(exp) {
				t.Fatalf("уровень %d: кадр %d байт, ждали %d", c.level, len(got), len(exp))
			}
			for i := range exp {
				if got[i] != exp[i] {
					t.Fatalf("уровень %d: байт %d = %02x, ждали %02x", c.level, i, got[i], exp[i])
				}
			}
		case <-time.After(2 * time.Second):
			t.Fatalf("уровень %d: директива не пришла", c.level)
		}
		restoreAmixer()
		restore()
	}
}

func TestVolumeCommandReadsMixerNotCache(t *testing.T) {
	dir, restore := fakeAmixer(t, 147)
	defer restore()

	// Первое чтение заполняет кэш.
	if r := handleCommand(command{Cmd: "volume", ID: "c1"}); r.Level == nil || *r.Level != 5 {
		t.Fatalf("первое чтение: %v", r.Level)
	}

	// Алиса убавила голосом мимо нас — кэш должен быть проигнорирован.
	if err := os.WriteFile(filepath.Join(dir, "raw"), []byte("103"), 0o644); err != nil {
		t.Fatalf("запись: %v", err)
	}
	r := handleCommand(command{Cmd: "volume", ID: "c2"})
	if r.Level == nil || *r.Level != 3 {
		t.Errorf("после изменения микшера ждали 3, получили %v", r.Level)
	}
	if r.Volume == nil || *r.Volume == 0 {
		t.Errorf("проценты не вернулись: %v", r.Volume)
	}
}

func TestVolumeCommandFailsWithoutAmixer(t *testing.T) {
	old := amixerPath
	amixerPath = filepath.Join(t.TempDir(), "нет-такого")
	defer func() { amixerPath = old }()

	r := handleCommand(command{Cmd: "volume", ID: "c1"})
	if r.Err == "" {
		t.Fatal("ждали ошибку при недоступном amixer")
	}
}
