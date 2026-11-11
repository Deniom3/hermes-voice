package main

// Состояние музыки по логу mediad.
//
// ALSA для этого не годится: устройство постоянно держит mediad (dmix), поэтому
// /proc/asound/card0/pcm0p/sub0/status показывает PREPARED и во время музыки, и
// после её выключения, а owner_pid — это последний процесс, делавший hw_params
// (в том числе наша озвучка), а не источник звука. Проверено на живой колонке
// 2026-11-01: музыка играла, а status показывал PREPARED с мёртвым owner_pid.
//
// Единственный достоверный источник — события плеера mediad: track_start,
// track_finished, Notification: Paused/Stopped, switchUserState.

import (
	"context"
	"io"
	"log"
	"os"
	"regexp"
	"strings"
	"sync"
	"time"
)

const mediaLogPath = "/data/quasar/daemons_logs/mediad.log"

// ALSA-выход: закрыт — значит звука нет. На Лайт 1 годится как достоверный
// признак (mediad не держит устройство между треками); на Лайт 2 dmix держит
// его постоянно, статус не бывает closed, и проверка не вмешивается.
const (
	alsaCheckEvery  = 2 * time.Second
	alsaClosedGrace = 20 * time.Second
)

// Переменная, а не константа: тест подкладывает свой файл статуса.
var alsaStatusPath = "/proc/asound/card0/pcm0p/sub0/status"

var (
	alsaNextCheck time.Time
	alsaClosedAt  time.Time
)

var (
	mediaMu      sync.RWMutex
	mediaPlaying bool
	mediaTrack   string
	mediaSince   = time.Now()
	// mediaStream — поток, который играет сейчас. Нужен из-за кроссфейда:
	// при переходе на следующий трек mediad пишет новому потоку PLAYING,
	// а через ~1.5 с старому — STOPPED/End. Без сравнения потоков состояние
	// сбрасывалось в «не играет» при играющей музыке.
	mediaStream string
)

var trackIDRe = regexp.MustCompile(`"id":"([^"]+)"`)
var streamRe = regexp.MustCompile(`stream=(\d+)`)

// streamOf достаёт номер потока из строки mediad (пусто — строка без потока).
func streamOf(line string) string {
	if m := streamRe.FindStringSubmatch(line); m != nil {
		return m[1]
	}
	return ""
}

// mediaState возвращает «играет ли музыка» и последний известный id трека.
func mediaState() (bool, string) {
	mediaMu.RLock()
	defer mediaMu.RUnlock()
	return mediaPlaying, mediaTrack
}

func setMedia(playing bool, reason, line string) {
	mediaMu.Lock()
	// Событие конца старого потока при кроссфейде не должно гасить музыку:
	// новый поток уже играет. Сравниваем номера потоков.
	if s := streamOf(line); s != "" {
		if playing {
			mediaStream = s
		} else if mediaStream != "" && mediaStream != s {
			mediaMu.Unlock()
			return
		} else {
			mediaStream = ""
		}
	}
	changed := mediaPlaying != playing
	mediaPlaying = playing
	if m := trackIDRe.FindStringSubmatch(line); m != nil {
		mediaTrack = m[1]
	}
	if changed {
		mediaSince = time.Now()
	}
	mediaMu.Unlock()
	if changed {
		log.Printf("музыка: %v (%s)", playing, reason)
	}
}

// applyMediaLine разбирает одну строку лога mediad.
func applyMediaLine(line string) {
	// В mediad рядом живут три вида плееров:
	//   [audio stream=NNN ...] — сам трек (музыка, радио, книга);
	//   [voice mark=...]      — голосовые ответы Алисы;
	//   [file_player mark=...]— короткие файлы: звуки, уведомления, подсказки.
	// Музыка — только audio-плеер, и опознаётся он по `audio stream=`.
	//
	// Проверено 02.11 17:58: `[file_player] Notification: End` (короткий звук)
	// сбросил состояние в «музыка не играет» за 8 с до активации. Хаб поверил,
	// `pause` вернул was_playing=false, и музыка, игравшая на 171-й секунде
	// трека, не вернулась после команды.
	if !strings.Contains(line, "[audio ") &&
		!strings.Contains(line, "event=track_start") &&
		!strings.Contains(line, "event=track_finished") {
		return
	}
	switch {
	case strings.Contains(line, "handleStart"):
		// Единственное событие начала трека, когда плеер уже был в PLAYING:
		// switchUserState в этом случае не пишется. Проверено 02.11 19:54 —
		// трек играл 6 с, а состояние оставалось «не играет», потому что
		// handleStart отбрасывался фильтром по «audio stream=».
		setMedia(true, "handleStart", line)
	case strings.Contains(line, "handleEnd"), strings.Contains(line, "switchToEnd"):
		setMedia(false, "handleEnd", line)
	case strings.Contains(line, "event=track_start"):
		setMedia(true, "track_start", line)
	case strings.Contains(line, "Notification: Playing"):
		setMedia(true, "notification", line)
	case strings.Contains(line, "switchUserState") &&
		(strings.Contains(line, " to PLAYING") ||
			strings.Contains(line, "to GST_STATE_PLAYING")):
		setMedia(true, "switchUserState", line)
	case strings.Contains(line, "State changed") &&
		strings.Contains(line, "to GST_STATE_PLAYING"):
		// Живой mediad пишет возврат из паузы так: «State changed from
		// GST_STATE_PAUSED to GST_STATE_PLAYING». Без этого правила после
		// `play` состояние оставалось «не играет», страховка в команде play
		// делала TOGGLE и ставила музыку на паузу (07.11, 00:49).
		setMedia(true, "state", line)
	case strings.Contains(line, "State changed") &&
		strings.Contains(line, "from GST_STATE_PLAYING to GST_STATE_PAUSED"):
		setMedia(false, "state", line)
	case strings.Contains(line, "Notification: Paused"),
		strings.Contains(line, "Notification: Stopped"),
		strings.Contains(line, "Notification: End"):
		setMedia(false, "notification", line)
	case strings.Contains(line, "switchUserState") &&
		(strings.Contains(line, " to PAUSED") || strings.Contains(line, " to STOPPED")):
		setMedia(false, "switchUserState", line)
	case strings.Contains(line, "pause (position"):
		setMedia(false, "pause", line)
	case strings.Contains(line, "event=track_finished"):
		setMedia(false, "track_finished", line)
	}
}

// outputClosed сообщает, что ALSA-выход никем не открыт.
func outputClosed(path string) bool {
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	// Ядро на разных платформах пишет "closed" в нижнем или верхнем регистре.
	return strings.Contains(strings.ToLower(string(data)), "closed")
}

// playingIsStale — «играет» держится, хотя выход закрыт дольше grace.
func playingIsStale(playing bool, closedSince, now time.Time, grace time.Duration) bool {
	if !playing || closedSince.IsZero() {
		return false
	}
	return now.Sub(closedSince) >= grace
}

// checkAlsaIdle сбрасывает залипшее «играет».
//
// Событие окончания трека приходит не всегда: после ротации mediad.log,
// при коротком звуке или обрыве состояния «играет» висит часами — и
// switch.hermes_<станция>_music в HA врёт, ломая автоматизации. Если выход
// закрыт дольше grace, состояние снимаем.
func checkAlsaIdle(now time.Time) {
	if !alsaNextCheck.IsZero() && now.Before(alsaNextCheck) {
		return
	}
	alsaNextCheck = now.Add(alsaCheckEvery)
	playing, _ := mediaState()
	if !playing || !outputClosed(alsaStatusPath) {
		alsaClosedAt = time.Time{}
		return
	}
	if alsaClosedAt.IsZero() {
		alsaClosedAt = now
	}
	if playingIsStale(playing, alsaClosedAt, now, alsaClosedGrace) {
		setMedia(false, "alsa closed", "")
		alsaClosedAt = time.Time{}
	}
}

// watchMediaLog следит за mediad.log. При старте прогоняет уже накопленный
// хвост файла, чтобы состояние было верным сразу, а не только после следующего
// события. Ротацию отслеживает по пути, а не по открытому дескриптору.
func watchMediaLog(ctx context.Context) {
	var f *os.File
	var tail string
	buf := make([]byte, 8192)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		checkAlsaIdle(time.Now())

		if f == nil {
			ff, err := os.Open(mediaLogPath)
			if err != nil {
				time.Sleep(2 * time.Second)
				continue
			}
			// Читаем накопленное с начала: файл небольшой, зато состояние
			// восстанавливается сразу после перезапуска агента.
			data, _ := io.ReadAll(ff)
			for _, line := range strings.Split(string(data), "\n") {
				applyMediaLine(line)
			}
			f = ff
			log.Printf("музыка: слежу за %s", mediaLogPath)
		}

		pathSt, err := os.Stat(mediaLogPath)
		if err != nil {
			f.Close()
			f = nil
			tail = ""
			continue
		}
		curSt, err := f.Stat()
		if err != nil || !os.SameFile(pathSt, curSt) {
			f.Close()
			f = nil
			tail = ""
			continue
		}

		n, err := f.Read(buf)
		if n > 0 {
			chunk := tail + string(buf[:n])
			lines := strings.Split(chunk, "\n")
			tail = lines[len(lines)-1]
			if len(tail) > 4096 {
				tail = ""
			}
			for _, line := range lines[:len(lines)-1] {
				applyMediaLine(line)
			}
			continue
		}
		if err != nil && err != io.EOF {
			f.Close()
			f = nil
			continue
		}
		time.Sleep(300 * time.Millisecond)
	}
}
