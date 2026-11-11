package main

import (
	"encoding/binary"
	"os"
	"testing"
)

func TestClientHelloPayload(t *testing.T) {
	c := &ssClient{cfg: ssConfig{Name: "test"}}
	p := c.clientHelloPayload(ssIdentity{pub: make([]byte, 32)})

	roles, _ := p["supported_roles"].([]string)
	if len(roles) != 1 || roles[0] != "player@v1" {
		t.Fatalf("supported_roles=%v", p["supported_roles"])
	}
	methods, ok := p["supported_pair_methods"].(map[string]any)
	if !ok {
		t.Fatalf("supported_pair_methods нет или не объект: %T", p["supported_pair_methods"])
	}
	if _, ok := methods["pairing_psk"]; !ok {
		t.Fatalf("pairing_psk не предложен: %v", methods)
	}
	support, ok := p["player@v1_support"].(map[string]any)
	if !ok {
		t.Fatalf("player@v1_support нет: %T", p["player@v1_support"])
	}
	formats, ok := support["supported_formats"].([]map[string]any)
	if !ok || len(formats) != 1 {
		t.Fatalf("supported_formats=%v", support["supported_formats"])
	}
	f := formats[0]
	if f["codec"] != "pcm" || f["sample_rate"] != ssSampleRate ||
		f["channels"] != ssChannels || f["bit_depth"] != ssBitDepth {
		t.Fatalf("формат=%v", f)
	}
	if up, ok := p["unpaired_access"].(map[string]any); !ok || up["enabled"] != true {
		t.Fatalf("unpaired_access=%v", p["unpaired_access"])
	}
}

func TestTimeMeasurement(t *testing.T) {
	m, e := ssTimeMeasurement(1000, 2000, 2100, 2200)
	if m != 450 || e != 550 {
		t.Fatalf("measurement=%d maxError=%d, ожидалось 450/550", m, e)
	}
}

func TestScaleS16(t *testing.T) {
	pcm := make([]byte, 4)
	pos := int16(1000)
	neg := int16(-1000)
	binary.LittleEndian.PutUint16(pcm[0:], uint16(pos))
	binary.LittleEndian.PutUint16(pcm[2:], uint16(neg))

	got := scaleS16(pcm, 0.5)
	if v := int16(binary.LittleEndian.Uint16(got[0:])); v != 500 {
		t.Fatalf("первый сэмпл %d, ожидалось 500", v)
	}
	if v := int16(binary.LittleEndian.Uint16(got[2:])); v != -500 {
		t.Fatalf("второй сэмпл %d, ожидалось -500", v)
	}
	if v := int16(binary.LittleEndian.Uint16(pcm[0:])); v != 1000 {
		t.Fatalf("исходный буфер изменён: %d", v)
	}

	muted := scaleS16(pcm, 0)
	if muted[0] != 0 || muted[1] != 0 || muted[2] != 0 || muted[3] != 0 {
		t.Fatalf("mute не обнулил PCM: %v", muted)
	}
}

func TestTimeFilterConverges(t *testing.T) {
	f := newSSTimeFilter()
	base := int64(1_000_000)
	for i := 0; i < 20; i++ {
		// Сервер на 1000 мкс впереди; сетевая ошибка ±250 мкс.
		f.Update(1000, 250, base+int64(i)*500_000)
	}
	if !f.Converged() {
		t.Fatalf("фильтр не сошёлся: count=%d", f.count)
	}
	got := f.ClientTime(10_000_000)
	want := int64(9_999_000)
	if got < want-500 || got > want+500 {
		t.Fatalf("ClientTime=%d, ожидалось около %d", got, want)
	}
}

func TestIdentityPersists(t *testing.T) {
	dir := t.TempDir()
	a, err := ssLoadIdentity(dir)
	if err != nil {
		t.Fatal(err)
	}
	b, err := ssLoadIdentity(dir)
	if err != nil {
		t.Fatal(err)
	}
	if a.clientID() != b.clientID() {
		t.Fatalf("client_id изменился: %s != %s", a.clientID(), b.clientID())
	}
	if len(a.clientID()) != 43 {
		t.Fatalf("client_id длиной %d, ожидалось 43", len(a.clientID()))
	}
	st, err := os.Stat(dir + "/identity.key")
	if err != nil {
		t.Fatal(err)
	}
	if st.Mode().Perm() != 0o600 {
		t.Fatalf("права identity.key = %o, ожидалось 600", st.Mode().Perm())
	}
}
