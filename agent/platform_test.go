package main

import "testing"

// Кольцо на разных платформах называется по-разному: micro:*:ring1 на Лайт 1
// и plum:*:ring1 на Лайт 2. Ошибка в префиксе = агент молча не управляет LED.
func TestPlatformLEDPaths(t *testing.T) {
	cases := []struct {
		prof   platformProfile
		color  string
		ring   int
		wantBr string
		wantTr string
	}{
		{microProfile(), "blue", 1, "/sys/class/leds/micro:blue:ring1/brightness", "/sys/class/leds/micro:blue:ring1/trigger"},
		{plumProfile(), "red", 3, "/sys/class/leds/plum:red:ring3/brightness", "/sys/class/leds/plum:red:ring3/trigger"},
	}
	for _, c := range cases {
		if got := ledBrightnessPath(c.prof.LEDPrefix, c.color, c.ring); got != c.wantBr {
			t.Errorf("brightness %s: got %q want %q", c.prof.Name, got, c.wantBr)
		}
		if got := ledTriggerPath(c.prof.LEDPrefix, c.color, c.ring); got != c.wantTr {
			t.Errorf("trigger %s: got %q want %q", c.prof.Name, got, c.wantTr)
		}
	}
}

// На plum активен led_ring_fb: без снятия триггера запись яркости не работает,
// а возврат обязателен, иначе пропадают штатные анимации Алисы.
func TestPlumNeedsTriggerHandling(t *testing.T) {
	if plumProfile().LEDTrigger != "led_ring_fb" {
		t.Fatalf("plum: ожидался триггер led_ring_fb, получено %q", plumProfile().LEDTrigger)
	}
	if microProfile().LEDTrigger != "" {
		t.Fatalf("micro: триггер должен быть пустым, получено %q", microProfile().LEDTrigger)
	}
}

func TestResolvePlatformByName(t *testing.T) {
	for _, name := range []string{"lite1", "micro", "lite-1"} {
		p, ok := profileByName(name)
		if !ok || p.Name != "micro" {
			t.Errorf("%s: got %q ok=%v", name, p.Name, ok)
		}
	}
	for _, name := range []string{"lite2", "plum", "lite-2"} {
		p, ok := profileByName(name)
		if !ok || p.Name != "plum" {
			t.Errorf("%s: got %q ok=%v", name, p.Name, ok)
		}
	}
	if _, ok := profileByName("mini"); ok {
		t.Error("mini не должен определяться как поддерживаемая платформа")
	}
}

// На plum нет .led-анимаций, поэтому индикация статическая: у каждого имени
// сервера должен быть цвет, иначе кольцо не покажет состояние.
func TestPlumStaticColorsCoverServerNames(t *testing.T) {
	for _, name := range []string{"hermes_listening", "hermes_speaking", "hermes_thinking", "hermes_tool", "hermes_error"} {
		if _, ok := ledStaticColors[name]; !ok {
			t.Errorf("нет цвета для %q", name)
		}
	}
}
