package main

import (
	"encoding/binary"
	"os"
	"testing"
	"time"
)

const sampleDevices = `I: Bus=0019 Vendor=0001 Product=0001 Version=0100
N: Name="gpio-keys-filter"
P: Phys=gpio-keys-filter/input0
S: Sysfs=/devices/platform/gpio-keys-filter/input/input0
U: Uniq=
H: Handlers=kbd event0
B: PROP=0
B: EV=3
B: KEY=10000000 0 0 0

I: Bus=0018 Vendor=0000 Product=0000 Version=0000
N: Name="aml-keypad"
H: Handlers=event1 cpufreq
B: EV=3
`

func TestParseInputDevices(t *testing.T) {
	got := parseInputDevices(sampleDevices)
	if len(got) != 2 {
		t.Fatalf("устройств: %d, ожидалось 2", len(got))
	}
	if got[0].Path != "/dev/input/event0" || got[0].Name != "gpio-keys-filter" {
		t.Fatalf("первое устройство: %+v", got[0])
	}
	if got[1].Path != "/dev/input/event1" || got[1].Name != "aml-keypad" {
		t.Fatalf("второе устройство: %+v", got[1])
	}
}

func encodeEvent(typ, code uint16, value int32) []byte {
	buf := make([]byte, inputEventSize)
	base := inputEventSize - 8
	binary.LittleEndian.PutUint16(buf[base:base+2], typ)
	binary.LittleEndian.PutUint16(buf[base+2:base+4], code)
	binary.LittleEndian.PutUint32(buf[base+4:base+8], uint32(value))
	return buf
}

func TestDecodeKeyEventPress(t *testing.T) {
	buf := encodeEvent(1, keyAlice, 1)
	typ, code, value := decodeKeyEvent(buf)
	if typ != 1 || code != keyAlice || value != 1 {
		t.Fatalf("разобрано неверно: type=%d code=%d value=%d", typ, code, value)
	}
}

func TestDecodeKeyEventRelease(t *testing.T) {
	buf := encodeEvent(1, keyAlice, 0)
	typ, code, value := decodeKeyEvent(buf)
	if typ != 1 || code != keyAlice || value != 0 {
		t.Fatalf("отпускание разобрано неверно: type=%d code=%d value=%d", typ, code, value)
	}
}

func TestDecodeKeyEventShortBuffer(t *testing.T) {
	if typ, code, value := decodeKeyEvent(make([]byte, inputEventSize-1)); typ != 0 || code != 0 || value != 0 {
		t.Fatal("короткий буфер должен давать нулевое событие")
	}
}

func TestDecodeIgnoresOtherKeys(t *testing.T) {
	// Другая кнопка (например, громкость) не должна считаться нажатием Алисы
	buf := encodeEvent(1, 115, 1) // KEY_VOLUMEUP
	typ, code, value := decodeKeyEvent(buf)
	if typ != 1 || code == keyAlice || value != 1 {
		t.Fatalf("чужой код распознан как Алиса: code=%d", code)
	}
}

func TestWaitReadableTimeout(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()

	started := time.Now()
	ready, err := waitReadable(int(r.Fd()), 100*time.Millisecond)
	if err != nil {
		t.Fatalf("select: %v", err)
	}
	if ready {
		t.Fatal("на пустом pipe готовности быть не должно")
	}
	if elapsed := time.Since(started); elapsed < 50*time.Millisecond {
		t.Fatalf("ожидание слишком короткое: %v", elapsed)
	}
}

func TestWaitReadableReady(t *testing.T) {
	r, w, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	defer w.Close()
	if _, err := w.Write([]byte{1}); err != nil {
		t.Fatal(err)
	}

	ready, err := waitReadable(int(r.Fd()), time.Second)
	if err != nil {
		t.Fatalf("select: %v", err)
	}
	if !ready {
		t.Fatal("pipe с данными должен быть готов")
	}
}

func TestVolumeDelta(t *testing.T) {
	cases := []struct {
		code  uint16
		value int32
		want  int
	}{
		{keyVolumeUp, 1, 1},
		{keyVolumeDown, 1, -1},
		{keyVolumeUp, 2, 1},
		{keyVolumeDown, 2, -1},
		{keyVolumeUp, 0, 0},
		{keyVolumeDown, 0, 0},
		{keyAlice, 1, 0},
	}
	for _, c := range cases {
		if got := volumeDelta(c.code, c.value); got != c.want {
			t.Fatalf("volumeDelta(%d, %d) = %d, ожидалось %d",
				c.code, c.value, got, c.want)
		}
	}
}
