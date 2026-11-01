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

var (
	mediaMu      sync.RWMutex
	mediaPlaying bool
	mediaTrack   string
	mediaSince   = time.Now()
)

var trackIDRe = regexp.MustCompile(`"id":"([^"]+)"`)

// mediaState возвращает «играет ли музыка» и последний известный id трека.
func mediaState() (bool, string) {
	mediaMu.RLock()
	defer mediaMu.RUnlock()
	return mediaPlaying, mediaTrack
}

func setMedia(playing bool, reason, line string) {
	mediaMu.Lock()
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
	switch {
	case strings.Contains(line, "event=track_start"):
		setMedia(true, "track_start", line)
	case strings.Contains(line, "Notification: Playing"):
		setMedia(true, "notification", line)
	case strings.Contains(line, "switchUserState") && strings.Contains(line, " to PLAYING"):
		setMedia(true, "switchUserState", line)
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
