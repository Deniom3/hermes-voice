package main

// Чтение физической кнопки Алисы напрямую из evdev.
//
// Почему не лог maind: в лог попадает одна строка «Button Event: play_pause
// Event: 0» — без отпускания и длительности, поэтому длинное нажатие
// отличить нечем, а формат строки и ротация файла добавляют хрупкости.
// В evdev приходят отдельные события: value=1 нажатие, 0 отпускание,
// 2 повтор. Путь и проверка бита KEY_ALICE повторяют приём из Luna
// (src/alice_input.rs, MIT), которая работает на той же прошивке.

import (
	"context"
	"encoding/binary"
	"fmt"
	"log"
	"os"
	"strings"
	"syscall"
	"time"
	"unsafe"
)

// KEY_ALICE — код кнопки Алисы в evdev.
const keyAlice = 582

// longPressAfter — порог длинного нажатия. Короткое — toggle владельца,
// длинное — явное «Алиса». Автоповторов прошивка не шлёт, поэтому
// длительность считается между нажатием и отпусканием.
const longPressAfter = 700 * time.Millisecond

// eviocgbitKey — EVIOCGBIT(EV_KEY, 96): какие коды клавиш умеет устройство.
const eviocgbitKey = (2 << 30) | (96 << 16) | ('E' << 8) | 0x21

// eviocgrab — EVIOCGRAB: забрать устройство у остальных читателей.
// Без него то же нажатие видит maind и ставит свою паузу, а мы свою —
// два toggle на одно нажатие дают «первый раз не гаснет, второй гасит».
const eviocgrab = 0x40044590

// inputEventSize — размер struct input_event: timeval + type + code + value.
// На 32-битном ARM timeval занимает 8 байт (16 всего), на 64-битном — 16
// (24 всего). Считаем от платформы, а не константой: на amd64 тесты должны
// разбирать тот же формат, что и на колонке.
var inputEventSize = int(unsafe.Sizeof(syscall.Timeval{})) + 8

// inputDevicesPath — переменная, чтобы тест подкладывал свой список.
var inputDevicesPath = "/proc/bus/input/devices"

// evdevDevice — найденное устройство с KEY_ALICE.
type evdevDevice struct {
	Path string
	Name string
}

// parseInputDevices достаёт из /proc/bus/input/devices пути event-устройств.
func parseInputDevices(listing string) []evdevDevice {
	var out []evdevDevice
	for _, block := range strings.Split(listing, "\n\n") {
		var name string
		var handlers string
		for _, line := range strings.Split(block, "\n") {
			switch {
			case strings.HasPrefix(line, "N: Name="):
				name = strings.Trim(strings.TrimPrefix(line, "N: Name="), "\"")
			case strings.HasPrefix(line, "H: Handlers="):
				handlers = strings.TrimPrefix(line, "H: Handlers=")
			}
		}
		for _, h := range strings.Fields(handlers) {
			if strings.HasPrefix(h, "event") {
				out = append(out, evdevDevice{Path: "/dev/input/" + h, Name: name})
			}
		}
	}
	return out
}

// hasKeyAlice проверяет бит KEY_ALICE через EVIOCGBIT(EV_KEY).
func hasKeyAlice(f *os.File) bool {
	bits := make([]byte, 96)
	_, _, errno := syscall.Syscall(
		syscall.SYS_IOCTL, f.Fd(), uintptr(eviocgbitKey), uintptr(unsafe.Pointer(&bits[0])))
	if errno != 0 {
		return false
	}
	idx := int(keyAlice / 8)
	return idx < len(bits) && bits[idx]&(1<<(keyAlice%8)) != 0
}

// findAliceDevice ищет ровно одно устройство с KEY_ALICE. Ноль и больше
// одного — ошибка: на двух устройствах непонятно, чьё нажатие слушать.
func findAliceDevice() (evdevDevice, error) {
	raw, err := os.ReadFile(inputDevicesPath)
	if err != nil {
		return evdevDevice{}, fmt.Errorf("список устройств: %w", err)
	}
	var found []evdevDevice
	for _, d := range parseInputDevices(string(raw)) {
		f, err := os.OpenFile(d.Path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
		if err != nil {
			continue
		}
		ok := hasKeyAlice(f)
		f.Close()
		if ok {
			found = append(found, d)
		}
	}
	if len(found) != 1 {
		return evdevDevice{}, fmt.Errorf("устройств с KEY_ALICE: %d (нужно одно)", len(found))
	}
	return found[0], nil
}

// decodeKeyEvent разбирает input_event: возвращает type, code, value.
func decodeKeyEvent(buf []byte) (uint16, uint16, int32) {
	if len(buf) < inputEventSize {
		return 0, 0, 0
	}
	base := len(buf) - 8
	typ := binary.LittleEndian.Uint16(buf[base : base+2])
	code := binary.LittleEndian.Uint16(buf[base+2 : base+4])
	value := int32(binary.LittleEndian.Uint32(buf[base+4 : base+8]))
	return typ, code, value
}

// waitReadable ждёт готовности fd к чтению не дольше timeout.
func waitReadable(fd int, timeout time.Duration) (bool, error) {
	var set syscall.FdSet
	set.Bits[fd/64] |= 1 << (uint(fd) % 64)
	tv := syscall.NsecToTimeval(timeout.Nanoseconds())
	n, err := syscall.Select(fd+1, &set, nil, nil, &tv)
	if err != nil {
		if err == syscall.EINTR {
			return false, nil
		}
		return false, err
	}
	return n > 0, nil
}

// aliceButtonPress — одно нажатие кнопки (value=1). Отпускание не возвращаем:
// обработчик реагирует на нажатие, как и прежний разбор лога.
type aliceButtonPress struct{}

// watchAliceKey читает evdev и вызывает onPress на отпускание KEY_ALICE.
// Признак long — нажатие держали не меньше longPressAfter.
// Возвращает ошибку, если устройство пропало: вызывающий переходит на лог.
func watchAliceKey(ctx context.Context, onPress func(long bool)) error {
	dev, err := findAliceDevice()
	if err != nil {
		return err
	}
	f, err := os.OpenFile(dev.Path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return fmt.Errorf("открыть %s: %w", dev.Path, err)
	}
	defer f.Close()
	// Забираем устройство себе: у maind на это же нажатие своя логика,
	// и вместе с нашей получаются два переключателя на одну кнопку.
	var grab int32 = 1
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, f.Fd(), eviocgrab,
		uintptr(unsafe.Pointer(&grab))); errno != 0 {
		log.Printf("кнопка: EVIOCGRAB %s не удался: %v (maind тоже увидит нажатие)", dev.Path, errno)
	} else {
		log.Printf("кнопка: evdev %s (%s), захвачено (EVIOCGRAB)", dev.Path, dev.Name)
	}

	buf := make([]byte, inputEventSize*16)
	// Длительность считаем между нажатием и отпусканием: события могут
	// прийти в разных read, поэтому метка живёт вне цикла чтения.
	var pressedAt time.Time
	for {
		select {
		case <-ctx.Done():
			return nil
		default:
		}
		ready, err := waitReadable(int(f.Fd()), 500*time.Millisecond)
		if err != nil {
			return fmt.Errorf("select: %w", err)
		}
		if !ready {
			continue
		}
		n, err := f.Read(buf)
		if err != nil {
			if err == syscall.EAGAIN || err == syscall.EWOULDBLOCK {
				continue
			}
			return fmt.Errorf("чтение %s: %w", dev.Path, err)
		}
		for off := 0; off+inputEventSize <= n; off += inputEventSize {
			typ, code, value := decodeKeyEvent(buf[off : off+inputEventSize])
			if typ != 1 || code != keyAlice {
				continue
			}
			if value == 1 {
				pressedAt = time.Now()
			} else if value == 0 && !pressedAt.IsZero() {
				held := time.Since(pressedAt)
				pressedAt = time.Time{}
				onPress(held >= longPressAfter)
			}
		}
	}
}
