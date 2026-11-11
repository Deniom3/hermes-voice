// Громкость станции через нативный канал Quasar.
//
// maind хранит логическую громкость, которую используют кнопки, кольцо и
// Алиса, и зеркалит её в ALSA. Запись только в ALSA это состояние не меняет:
// следующее нажатие кнопки возвращает старый уровень. Директива VolumeSet
// через iohub_clients (9910) обновляет состояние maind, и он сам применяет
// его к микшеру.
//
// Формат кадра и директивы перенесён из Luna (MIT,
// Copyright (c) 2026 Luna contributors), src/quasar_volume.rs.
package main

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"net"
	"os"
	"strings"
	"time"
)

const quasarVolumeTimeout = time.Second

// Пути — переменные, а не константы: тесты подменяют их временными файлами.
var (
	quasarConfigPath     = "/system/vendor/quasar/quasar.cfg"
	quasarVersionPath    = "/system/vendor/quasar/version_full"
	quasarCompatiblePath = "/proc/device-tree/compatible"
)

// quasarVolumeBuilds — прошивки, на которых путь проверен вживую.
// Ключ — version_full, значение — обязательный токен в /proc/device-tree/compatible.
var quasarVolumeBuilds = map[string]string{
	"0.369.1.46.4778262442.20261013.297": "yandex,station-micro", // Лайт 1
	"0.369.1.50.4794654278.20261021.133": "yandex,prune-evt2",    // Лайт 2
}

// quasarVolumeVerified проверяет, что прошивка и плата входят в белый список.
func quasarVolumeVerified() bool {
	build, err := os.ReadFile(quasarVersionPath)
	if err != nil {
		return false
	}
	compatible, err := os.ReadFile(quasarCompatiblePath)
	if err != nil {
		return false
	}
	return volumeBuildVerified(strings.TrimSpace(string(build)), compatible)
}

// volumeBuildVerified отделена от файлов, чтобы проверяться тестом.
func volumeBuildVerified(build string, compatible []byte) bool {
	token, ok := quasarVolumeBuilds[strings.TrimSpace(build)]
	if !ok {
		return false
	}
	for _, entry := range strings.Split(string(compatible), "\x00") {
		if entry == token {
			return true
		}
	}
	return false
}

// quasarPort читает порт сервиса из quasar.cfg.
//
// В конфиге соседние сервисы лежат строками, поэтому сначала разбираем
// верхний уровень в RawMessage: иначе json.Unmarshal падает на первом же
// строковом значении и порт не читается вовсе.
func quasarPort(service string) (int, error) {
	data, err := os.ReadFile(quasarConfigPath)
	if err != nil {
		return 0, fmt.Errorf("quasar.cfg: %w", err)
	}
	var cfg map[string]json.RawMessage
	if err := json.Unmarshal(data, &cfg); err != nil {
		return 0, fmt.Errorf("quasar.cfg: %w", err)
	}
	raw, ok := cfg[service]
	if !ok {
		return 0, fmt.Errorf("quasar.cfg: сервис %s не найден", service)
	}
	var entry struct {
		Port int `json:"port"`
	}
	if err := json.Unmarshal(raw, &entry); err != nil {
		return 0, fmt.Errorf("quasar.cfg: %s: %w", service, err)
	}
	if entry.Port <= 0 || entry.Port > 65535 {
		return 0, fmt.Errorf("quasar.cfg: у сервиса %s нет корректного port", service)
	}
	return entry.Port, nil
}

func pushVarint(out []byte, v uint64) []byte {
	for v >= 0x80 {
		out = append(out, byte(v)|0x80)
		v >>= 7
	}
	return append(out, byte(v))
}

// quasarSetFrame собирает IOControl88 с одной директивой VolumeSet (254):
// PercentLevel — float32, Source=Button (кольцо показывает уровень без звука).
func quasarSetFrame(percent uint8) []byte {
	typed := []byte{13}
	var level [4]byte
	binary.LittleEndian.PutUint32(level[:], math.Float32bits(float32(percent)))
	typed = append(typed, level[:]...)
	typed = append(typed, 24, 1) // поле 3: Source = Button

	var directive []byte
	directive = append(directive, 10, 9)
	directive = append(directive, "directive"...)
	directive = append(directive, 18, 10)
	directive = append(directive, "volume_set"...)
	directive = append(directive, 32, 1, 122, byte(len(typed)))
	directive = append(directive, typed...)
	directive = pushVarint(directive, 20<<3)
	directive = pushVarint(directive, 254)

	packet := make([]byte, 4)
	packet = pushVarint(packet, 252<<3|2)
	packet = pushVarint(packet, uint64(7+len(directive)))
	packet = pushVarint(packet, 95<<3|2)
	packet = pushVarint(packet, uint64(4+len(directive)))
	packet = append(packet, 10, byte(2+len(directive)), 10, byte(len(directive)))
	packet = append(packet, directive...)
	packet = pushVarint(packet, 253<<3)
	packet = pushVarint(packet, 88)
	binary.LittleEndian.PutUint32(packet[:4], uint32(len(packet)-4))
	return packet
}

// quasarVolumeSet отправляет VolumeSet в maind. Уровень — проценты 0..100,
// прошивка квантует его сеткой 5%.
func quasarVolumeSet(percent int) error {
	if percent < 0 || percent > 100 {
		return fmt.Errorf("громкость %d%% вне 0..100", percent)
	}
	if !quasarVolumeVerified() {
		return fmt.Errorf("прошивка не проверена для нативного VolumeSet")
	}
	port, err := quasarPort("iohub_clients")
	if err != nil {
		return err
	}
	addr := fmt.Sprintf("127.0.0.1:%d", port)
	conn, err := net.DialTimeout("tcp", addr, quasarVolumeTimeout)
	if err != nil {
		return fmt.Errorf("iohub_clients %s: %w", addr, err)
	}
	defer conn.Close()
	_ = conn.SetWriteDeadline(time.Now().Add(quasarVolumeTimeout))
	if _, err := conn.Write(quasarSetFrame(uint8(percent))); err != nil {
		return fmt.Errorf("iohub_clients запись: %w", err)
	}
	return nil
}
