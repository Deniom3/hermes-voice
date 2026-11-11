package main

import (
	"fmt"
	"os"
)

// platformProfile описывает различия платформ, от которых зависит агент.
//
// Проверено 2026-11-05 на Лайт 1 (192.168.1.20, micro) и Лайт 2
// (192.168.1.21, plum): общее — ядро aarch64, 32-бит userspace, maind,
// aliced:9875, Glagol:1961, amixer Master, ALSA AML-AUGESOUND.
type platformProfile struct {
	Name       string // micro (Лайт 1) или plum (Лайт 2)
	LEDPrefix  string // префикс имён в /sys/class/leds
	LEDTrigger string // активный триггер кольца; пусто — прямой sysfs без триггера
	SoundDir   string // каталог штатных звуков
	AudioMode  string // gst (maind --gst-launch) или aplay (прямой ALSA)
	MicMode    string // yio (IPC 9850) или vqe (LD_PRELOAD-хук)
}

const (
	ledsBase   = "/sys/class/leds"
	soundsBase = "/system/vendor/quasar/sounds"
)

func microProfile() platformProfile {
	return platformProfile{
		Name:       "micro",
		LEDPrefix:  "micro",
		LEDTrigger: "",
		SoundDir:   soundsBase + "/common",
		AudioMode:  "gst",
		MicMode:    "yio",
	}
}

func plumProfile() platformProfile {
	return platformProfile{
		Name:       "plum",
		LEDPrefix:  "plum",
		LEDTrigger: "led_ring_fb",
		// на plum каталога sounds/common нет: звуки лежат в sounds/ и sounds/<locale>/
		SoundDir:  soundsBase,
		AudioMode: "aplay", // maind на plum не имеет опции --gst-launch
		MicMode:   "vqe",   // yio_audio:9850 кадров не отдаёт
	}
}

// profileByName принимает и человекочитаемые имена, и кодовые.
func profileByName(name string) (platformProfile, bool) {
	switch name {
	case "micro", "lite1", "lite-1", "yandexmicro":
		return microProfile(), true
	case "plum", "lite2", "lite-2":
		return plumProfile(), true
	}
	return platformProfile{}, false
}

// detectPlatform определяет платформу по кольцу: на plum пути plum:*:ring1,
// на micro — micro:*:ring1.
func detectPlatform() (platformProfile, error) {
	for _, p := range []platformProfile{plumProfile(), microProfile()} {
		probe := fmt.Sprintf("%s/%s:red:ring1/brightness", ledsBase, p.LEDPrefix)
		if _, err := os.Stat(probe); err == nil {
			return p, nil
		}
	}
	return platformProfile{}, fmt.Errorf("не нашёл кольцо ни micro, ни plum в %s", ledsBase)
}

// resolvePlatform разбирает значение флага -platform: auto, имя платформы
// или пустая строка (тогда автоопределение).
func resolvePlatform(name string) (platformProfile, error) {
	if name == "" || name == "auto" {
		return detectPlatform()
	}
	if p, ok := profileByName(name); ok {
		return p, nil
	}
	return platformProfile{}, fmt.Errorf("неизвестная платформа %q (ожидается auto|lite1|lite2)", name)
}

func ledBrightnessPath(prefix, color string, ring int) string {
	return fmt.Sprintf("%s/%s:%s:ring%d/brightness", ledsBase, prefix, color, ring)
}

func ledTriggerPath(prefix, color string, ring int) string {
	return fmt.Sprintf("%s/%s:%s:ring%d/trigger", ledsBase, prefix, color, ring)
}
