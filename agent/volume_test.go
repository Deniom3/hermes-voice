package main

import "testing"

// Живой формат amixer на этой прошивке (проверено 03.11.2026)
const amixerSample = `Simple mixer control 'Master',0
  Capabilities: pvolume
  Playback channels: Front Left - Front Right
  Limits: Playback 0 - 255
  Mono:
  Front Left: Playback 128 [50%] [-25.40dB]
  Front Right: Playback 128 [50%] [-25.40dB]`

func TestParseVolumeReadsRawPercentAndDB(t *testing.T) {
	raw, p, db, err := parseVolume([]byte(amixerSample))
	if err != nil {
		t.Fatalf("ошибка разбора: %v", err)
	}
	if raw != 128 {
		t.Errorf("raw: получили %d, ждали 128", raw)
	}
	if p != 50 {
		t.Errorf("процент: получили %d, ждали 50", p)
	}
	if db != -25.40 {
		t.Errorf("dB: получили %.2f, ждали -25.40", db)
	}
}

func TestParseVolumeHandlesMutedAndFullLevels(t *testing.T) {
	cases := []struct {
		name string
		line string
		want int
	}{
		{"ноль", "Front Left: Playback 0 [0%] [-51.00dB]", 0},
		{"максимум", "Front Left: Playback 255 [100%] [0.00dB]", 100},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			_, p, _, err := parseVolume([]byte(c.line))
			if err != nil {
				t.Fatalf("ошибка: %v", err)
			}
			if p != c.want {
				t.Errorf("получили %d, ждали %d", p, c.want)
			}
		})
	}
}

func TestParseVolumeWithoutRawFails(t *testing.T) {
	if _, _, _, err := parseVolume([]byte("amixer: Unable to find simple control")); err == nil {
		t.Fatal("ждали ошибку на выводе без raw")
	}
}

// Уровень считается по дБ: шкала общая для моделей, проценты — нет.
// Значения сняты с обеих колонок.
func TestLevelOfByDB(t *testing.T) {
	cases := []struct {
		name  string
		pct   int
		db    float64
		level int
	}{
		{"Лайт 1, уровень 3", 30, -35.60, 3},
		{"Лайт 1, уровень 5", 50, -25.40, 5},
		{"Лайт 1, уровень 7", 70, -15.20, 7},
		{"Лайт 1, максимум", 100, 0, 10},
		{"Лайт 1, тишина", 0, -51, 0},
		{"Лайт 2, уровень 1", 24, -45.89, 1},
		{"Лайт 2, уровень 2", 32, -40.71, 2},
		{"Лайт 2, уровень 3", 40, -35.77, 3},
		{"Лайт 2, уровень 5", 58, -25.42, 5},
		{"Лайт 2, уровень 6", 66, -20.48, 6},
		{"Лайт 2, уровень 9", 91, -5.18, 9},
		{"Лайт 2, максимум", 100, 0, 10},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := levelOf(c.pct, c.db); got != c.level {
				t.Errorf("levelOf(%d, %.2f) = %d, ждали %d", c.pct, c.db, got, c.level)
			}
		})
	}
}
