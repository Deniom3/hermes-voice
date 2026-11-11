package main

// Протокол Sendspin: константы, JSON-конверты и фильтр времени.
//
// Клиент подключается к Music Assistant по ws://, проходит Noise KKpsk2
// (Sentinel PSK, без pairing) и получает PCM 48/16/2. Всё, что нужно плееру,
// умещается в client/hello, client/state и разборе бинарных чанков типа 4.

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"math"
)

const (
	ssProtocolVersion = 1
	ssSuite           = "25519_AESGCM_SHA256"

	ssMsgJSON  = 0
	ssMsgAudio = 4

	ssSampleRate = 48000
	ssChannels   = 2
	ssBitDepth   = 16
)

var ssSentinelPSK = sha256.Sum256([]byte("sendspin-sentinel-psk-v1"))

func ssB64(b []byte) string {
	return base64.RawURLEncoding.EncodeToString(b)
}

func ssB64Decode(s string) ([]byte, error) {
	return base64.RawURLEncoding.DecodeString(s)
}

// ssPSKID — base64url(SHA256("sendspin-psk-id-v1" || psk)), как в aiosendspin.
func ssPSKID(psk []byte) string {
	h := sha256.New()
	h.Write([]byte("sendspin-psk-id-v1"))
	h.Write(psk)
	return ssB64(h.Sum(nil))
}

type ssEnvelope struct {
	Type    string          `json:"type"`
	Payload json.RawMessage `json:"payload"`
}

func ssEncode(typ string, payload any) ([]byte, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, err
	}
	return json.Marshal(ssEnvelope{Type: typ, Payload: raw})
}

// ssTimeFilter — порт SendspinTimeFilter из aiosendspin (двумерный фильтр
// Калмана: смещение часов и дрейф). Нужен, чтобы client/state уходил только
// после сходимости, как требует спецификация.
type ssTimeFilter struct {
	lastUpdate int64
	count      int

	offset float64
	drift  float64

	poo float64 // offset covariance
	pod float64 // offset/drift covariance
	pdd float64 // drift covariance

	useDrift bool
}

func newSSTimeFilter() *ssTimeFilter {
	return &ssTimeFilter{poo: math.Inf(1)}
}

func (f *ssTimeFilter) Update(measurement, maxError, timeAdded int64) {
	if timeAdded <= f.lastUpdate || maxError < 0 {
		return
	}
	dt := float64(timeAdded - f.lastUpdate)
	f.lastUpdate = timeAdded

	updateStd := float64(maxError) * 0.5
	r := updateStd * updateStd

	if f.count == 0 {
		f.count = 1
		f.offset = float64(measurement)
		f.poo = r
		f.drift = 0
		return
	}
	if f.count == 1 {
		f.count = 2
		f.drift = (float64(measurement) - f.offset) / dt
		f.offset = float64(measurement)
		f.pdd = (f.poo + r) / (dt * dt)
		f.poo = r
		return
	}

	const (
		processStdDev      = 0.0
		driftProcessStdDev = 1e-11
		forgetFactor       = 2.0
		adaptiveCutoff     = 3.0
		minSamples         = 100
		driftSignificance  = 2.0
	)

	offset := f.offset + f.drift*dt
	dt2 := dt * dt

	driftProcessVar := dt * driftProcessStdDev * driftProcessStdDev
	newPdd := f.pdd + driftProcessVar
	newPod := f.pod + f.pdd*dt
	newPoo := f.poo + 2*f.pod*dt + f.pdd*dt2 + dt*processStdDev*processStdDev

	residual := float64(measurement) - offset
	if f.count < minSamples {
		f.count++
	} else if math.Abs(residual) > adaptiveCutoff*float64(maxError) {
		k := forgetFactor * forgetFactor
		newPoo *= k
		newPod *= k
		newPdd *= k
	}

	s := newPoo + r
	if s < 1e-9 {
		s = 1e-9
	}
	ko := newPoo / s
	kd := newPod / s

	f.offset = offset + ko*residual
	f.drift += kd * residual
	f.poo = newPoo - ko*newPoo
	f.pod = newPod - kd*newPoo
	f.pdd = newPdd - kd*newPod

	f.useDrift = f.drift*f.drift > driftSignificance*driftSignificance*f.pdd
}

// ClientTime переводит серверную метку в локальную (микросекунды).
func (f *ssTimeFilter) ClientTime(serverTS int64) int64 {
	d := 0.0
	if f.useDrift {
		d = f.drift
	}
	v := (float64(serverTS) - f.offset + d*float64(f.lastUpdate)) / (1 + d)
	return int64(math.Round(v))
}

func (f *ssTimeFilter) Converged() bool {
	return f.count >= 8
}

// ssTimeMeasurement считает смещение и максимальную ошибку по четырём меткам
// NTP-обмена (T1 клиент отправил, T2 сервер принял, T3 сервер отправил,
// T4 клиент принял). Целочисленное деление — как в спецификации.
func ssTimeMeasurement(t1, t2, t3, t4 int64) (measurement, maxError int64) {
	return ((t2 - t1) + (t3 - t4)) / 2, ((t4 - t1) - (t3 - t2)) / 2
}
