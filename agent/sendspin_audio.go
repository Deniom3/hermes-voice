package main

// Вывод PCM в ALSA через штатный aplay.
//
// Почему aplay, а не cgo: на колонке уже есть /usr/bin/aplay, он открывает
// default (softvol → dmix) и не тянет в агент libasound. Формат фиксирован:
// 48 кГц, 16 бит, стерео — ровно то, что просит MA в client/hello.

import (
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"math"
	"os"
	"os/exec"
	"sync"
	"time"
)

type ssAudio struct {
	mu     sync.Mutex
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	output string
	volume int
	muted  bool
	// paused — пауза под ответ. Write обязан её уважать: иначе поток
	// чанков от MA перезапускает aplay после остановки, и музыка звучит
	// поверх ответа.
	paused bool
}

func newSSAudio(output string) *ssAudio {
	return &ssAudio{output: output, volume: 100}
}

func (a *ssAudio) Start() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.cmd != nil {
		return nil
	}
	cmd := exec.Command("/usr/bin/aplay", "-q", "-D", a.output,
		"-t", "raw", "-f", "S16_LE", "-r", "48000", "-c", "2")
	// Без этого ошибка ALSA (например, занятое устройство) уходит в никуда,
	// и в журнале остаётся только следствие — закрытый stdin.
	cmd.Stderr = os.Stderr
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return err
	}
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("aplay: %w", err)
	}
	a.cmd = cmd
	a.stdin = stdin
	log.Printf("sendspin: aplay запущен (%s)", a.output)
	return nil
}

func (a *ssAudio) Stop() {
	a.mu.Lock()
	cmd, stdin := a.cmd, a.stdin
	a.cmd, a.stdin = nil, nil
	a.mu.Unlock()
	if cmd == nil {
		return
	}
	// Закрытый stdin — единственный сигнал aplay: допеть остаток буфера и
	// выйти самому. Никаких kill: убийство посреди записи в ALSA даёт
	// щелчок и рвёт PCM. Ждём до 500 мс; если aplay не вышел — не трогаем
	// процесс, он завершится сам, когда отпустит устройство.
	if stdin != nil {
		_ = stdin.Close()
	}
	done := make(chan struct{})
	go func() {
		_ = cmd.Wait()
		close(done)
	}()
	select {
	case <-done:
		log.Printf("sendspin: aplay остановлен")
	case <-time.After(500 * time.Millisecond):
		log.Printf("sendspin: aplay ещё завершается (ждём без kill)")
	}
}

// SetPaused включает паузу и, если нужно, сразу гасит поток.
func (a *ssAudio) SetPaused(v bool) {
	a.mu.Lock()
	a.paused = v
	a.mu.Unlock()
	if v {
		a.Stop()
	}
}

func (a *ssAudio) Running() bool {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.cmd != nil
}

func (a *ssAudio) SetVolume(v int, muted bool) {
	if v < 0 {
		v = 0
	}
	if v > 100 {
		v = 100
	}
	a.mu.Lock()
	a.volume, a.muted = v, muted
	a.mu.Unlock()
}

// Write масштабирует S16_LE и отдаёт aplay. Копия нужна, потому что буфер
// приходит из расшифрованного сообщения и может использоваться дальше.
//
// Если aplay уже завершился (например, устройство было занято в момент старта),
// запись в его stdin даёт "file already closed". Раньше агент только
// останавливал поток: музыка молчала до следующего server/activate, а MA
// продолжал гнать чанки в переполненную очередь. Теперь поток поднимается сам.
func (a *ssAudio) Write(pcm []byte) {
	if len(pcm) < 2 {
		return
	}
	a.mu.Lock()
	if a.paused {
		a.mu.Unlock()
		return
	}
	gain := math.Pow(float64(a.volume)/100, 1.5)
	if a.muted {
		gain = 0
	}
	a.mu.Unlock()

	buf := scaleS16(pcm, gain)
	for attempt := 1; attempt <= 3; attempt++ {
		a.mu.Lock()
		if a.paused {
			a.mu.Unlock()
			return
		}
		stdin := a.stdin
		a.mu.Unlock()
		if stdin == nil {
			return
		}
		_, err := stdin.Write(buf)
		if err == nil {
			return
		}
		log.Printf("sendspin: запись в aplay: %v (попытка %d из 3)", err, attempt)
		a.Stop()
		if attempt == 3 {
			return
		}
		time.Sleep(time.Duration(attempt) * 150 * time.Millisecond)
		if err := a.Start(); err != nil {
			log.Printf("sendspin: перезапуск aplay: %v", err)
			return
		}
	}
}

// scaleS16 возвращает копию PCM с усилением gain (0..1). Нечётный хвостовой
// байт отбрасывается: кадр S16_LE — два байта.
func scaleS16(pcm []byte, gain float64) []byte {
	buf := make([]byte, len(pcm)&^1)
	copy(buf, pcm)
	if gain == 1 {
		return buf
	}
	for i := 0; i+1 < len(buf); i += 2 {
		s := int16(binary.LittleEndian.Uint16(buf[i:]))
		binary.LittleEndian.PutUint16(buf[i:], uint16(int16(float64(s)*gain)))
	}
	return buf
}
