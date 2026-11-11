package main

// Дефект 2026-11-02: в mediad рядом с музыкой живут голосовые сессии
// (`[voice mark=...]`). Их switchUserState PLAYING/STOPPED перебивали состояние
// музыкального потока, агент отвечал was_playing=false, и после озвучки музыка
// не возвращалась. Тесты закрепляют различие «музыка» / «голос».

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func resetMedia(t *testing.T) {
	t.Helper()
	mediaMu.Lock()
	mediaPlaying = false
	mediaTrack = ""
	mediaMu.Unlock()
}

func musicPlaying(t *testing.T) bool {
	t.Helper()
	playing, _ := mediaState()
	return playing
}

func TestTrackStartTurnsMusicOn(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[2026-11-02 00:26:00.246] [media] [info] [media_stats.cpp:190] event=track_start, payload={"id":"113683787"}`)
	if !musicPlaying(t) {
		t.Fatal("track_start должен включать музыку")
	}
	if _, track := mediaState(); track != "113683787" {
		t.Fatalf("id трека не разобран: %q", track)
	}
}

func TestVoicePlayingDoesNotCountAsMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:26967] [2026-11-02 00:24:13.092] [media] [info] [gstreamer_audio_player2.cpp:1915] [voice mark=6LcI1u live] switchUserState from UNSET to PLAYING`)
	if musicPlaying(t) {
		t.Fatal("голосовая сессия Алисы не должна выглядеть как музыка")
	}
}

func TestVoiceStopDoesNotStopMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:26986] [2026-11-02 00:24:14.581] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=113683787 mark=K0Knw] switchUserState from UNSET to PLAYING`)
	applyMediaLine(`[13198:26967] [2026-11-02 00:24:14.151] [media] [info] [gstreamer_audio_player2.cpp:1915] [voice mark=6LcI1u live] switchUserState from PLAYING to STOPPED`)
	if !musicPlaying(t) {
		t.Fatal("остановка голосовой сессии погасила музыку")
	}
}

func TestAudioPauseStopsMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:26986] [2026-11-02 00:26:00.355] [media] [info] [gstreamer_audio_player2.cpp:346] [audio stream=113683787 mark=K0Knw] pause (position 12345)`)
	if musicPlaying(t) {
		t.Fatal("pause музыкального потока должен выключать музыку")
	}
}

func TestTrackFinishedStopsMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[1771:1800] [2026-11-02 00:14:12.484] [media] [info] [media_stats.cpp:257] event=track_finished, payload={"altUrlCount":0}`)
	if musicPlaying(t) {
		t.Fatal("track_finished должен выключать музыку")
	}
}

func TestVoiceLineWithAudioStreamIsMusic(t *testing.T) {
	// Строка содержит и voice, и audio stream — считаем музыкой: так выглядит
	// при переключении пользователя, когда голос и трек идут одним сообщением.
	resetMedia(t)
	applyMediaLine(`[voice mark=abc live] [audio stream=42 mark=x] switchUserState from PAUSED to PLAYING`)
	if !musicPlaying(t) {
		t.Fatal("audio stream в строке должен включать музыку")
	}
}

// Дефект 2026-11-02 17:58: короткий файл (`file_player`) закончился за 8 с до
// активации, состояние сбросилось в false, pause вернул was_playing=false —
// и музыка, игравшая на 171-й секунде трека, после команды не вернулась.
func TestFilePlayerEndDoesNotStopMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:12327] [2026-11-02 14:58:51.870] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=155802860 mark=hpnViY] switchUserState from PLAYING to PAUSED`)
	applyMediaLine(`[13198:12327] [2026-11-02 14:58:51.870] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=155802860 mark=hpnViY] switchUserState from UNSET to PLAYING`)
	if !musicPlaying(t) {
		t.Fatal("музыка должна играть")
	}
	applyMediaLine(`[13198:12351] [2026-11-02 14:58:42.948] [media] [info] [gstreamer_audio_player2.cpp:2126] [file_player mark=8WQEmA] Notification: End`)
	applyMediaLine(`[13198:12351] [2026-11-02 14:58:42.949] [media] [info] [gstreamer_audio_player2.cpp:1170] [file_player mark=8WQEmA] State changed from GST_STATE_PLAYING to GST_STATE_PAUSED`)
	applyMediaLine(`[13198:12351] [2026-11-02 14:58:42.960] [media] [info] [gstreamer_audio_player2.cpp:1875] [file_player mark=8WQEmA] switchInternalState transition GST_STATE_PAUSED -> GST_STATE_NULL`)
	if !musicPlaying(t) {
		t.Fatal("конец короткого файла не должен выключать музыку")
	}
}

func TestFilePlayerStartDoesNotFakeMusic(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:12351] [2026-11-02 14:58:45.646] [media] [info] [gstreamer_audio_player2.cpp:2107] [file_player mark=8WQEmA] Notification: Start`)
	if musicPlaying(t) {
		t.Fatal("короткий файл не должен выглядеть как музыка")
	}
}

// Дефект 2026-11-02 19:54: трек играл уже 6 секунд, но в журнале было только
// `handleStart` (без switchUserState) — фильтр по «audio stream=» отбрасывал
// эту строку, состояние оставалось «не играет», pause отвечал was_playing=false
// и музыка не возвращалась.
func TestAudioHandleStartTurnsMusicOn(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:15933] [2026-11-02 16:54:45.769] [media] [info] [audio_client.cpp:870] [audio id=90031d93-fc32-4bfd-abca-4d554b3b922e stream=150665628 mark=I6VFs3] handleStart: Rss memory 36352/49450`)
	if !musicPlaying(t) {
		t.Fatal("handleStart аудиоплеера должен включать музыку")
	}
}

func TestFilePlayerHandleStartStaysSilent(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[13198:15933] [2026-11-02 16:54:45.769] [media] [info] [audio_client.cpp:870] [file_player id=1 stream=2 mark=x] handleStart: Rss memory`)
	if musicPlaying(t) {
		t.Fatal("file_player не должен считаться музыкой")
	}
}

// Дефект 2026-11-07 00:48: после возврата музыки (`play`) mediad wrote
// «State changed from GST_STATE_PAUSED to GST_STATE_PLAYING», агент это
// игнорировал, страховка в play делала TOGGLE и ставила трек на паузу.
func TestStateChangedToPlayingTurnsMusicOn(t *testing.T) {
	resetMedia(t)
	applyMediaLine(`[2038:5152] [2026-11-06 21:48:56.251] [media] [info] [gstreamer_audio_player2.cpp:1170] [audio stream=34678972 mark=sk9nPE] State changed from GST_STATE_PAUSED to GST_STATE_PLAYING`)
	if !musicPlaying(t) {
		t.Fatal("State changed ... to GST_STATE_PLAYING должен включать музыку")
	}
}

func TestStateChangedPreparationDoesNotStopMusic(t *testing.T) {
	// Подготовка следующего трека: READY → PAUSED — это не пауза музыки.
	resetMedia(t)
	setMedia(true, "test", "")
	applyMediaLine(`[2038:5453] [2026-11-06 21:51:18.974] [media] [info] [gstreamer_audio_player2.cpp:1170] [audio stream=29025590 mark=zBVPH3] State changed from GST_STATE_READY to GST_STATE_PAUSED`)
	if !musicPlaying(t) {
		t.Fatal("подготовка нового трека не должна выключать музыку")
	}
}

func TestStateChangedPlayingToPausedStopsMusic(t *testing.T) {
	resetMedia(t)
	setMedia(true, "test", "")
	applyMediaLine(`[2038:5152] [2026-11-06 21:51:21.394] [media] [info] [gstreamer_audio_player2.cpp:1170] [audio stream=34678972 mark=sk9nPE] State changed from GST_STATE_PLAYING to GST_STATE_PAUSED`)
	if musicPlaying(t) {
		t.Fatal("переход PLAYING → PAUSED должен выключать музыку")
	}
}

// Дефект 2026-11-06: медиад ротировал лог в 11:11, событие окончания трека
// не дошло, и «играет» висело 3.5 часа — switch.hermes_lite_1_music в HA
// показывал on при закрытом ALSA-выходе. Проверяем сброс по ALSA.
func TestPlayingIsStale(t *testing.T) {
	now := time.Now()
	if playingIsStale(false, now.Add(-time.Minute), now, 20*time.Second) {
		t.Fatal("не играет — сбрасывать нечего")
	}
	if playingIsStale(true, time.Time{}, now, 20*time.Second) {
		t.Fatal("без отметки закрытия сбрасывать нельзя")
	}
	if playingIsStale(true, now.Add(-5*time.Second), now, 20*time.Second) {
		t.Fatal("5 с меньше grace — рано")
	}
	if !playingIsStale(true, now.Add(-25*time.Second), now, 20*time.Second) {
		t.Fatal("25 с больше grace — пора сбрасывать")
	}
}

func TestOutputClosedReadsStatus(t *testing.T) {
	dir := t.TempDir()
	closed := filepath.Join(dir, "closed")
	if err := os.WriteFile(closed, []byte("state: CLOSED\nowner_pid: 123\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !outputClosed(closed) {
		t.Fatal("CLOSED должен читаться как закрытый выход")
	}
	running := filepath.Join(dir, "running")
	if err := os.WriteFile(running, []byte("state: RUNNING\nowner_pid: 42\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if outputClosed(running) {
		t.Fatal("RUNNING — не закрытый выход")
	}
	if outputClosed(filepath.Join(dir, "missing")) {
		t.Fatal("нет файла — не считаем закрытым")
	}
}

func TestCheckAlsaIdleClearsStuckPlaying(t *testing.T) {
	dir := t.TempDir()
	status := filepath.Join(dir, "status")
	if err := os.WriteFile(status, []byte("state: CLOSED\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	oldPath := alsaStatusPath
	alsaStatusPath = status
	alsaNextCheck = time.Time{}
	alsaClosedAt = time.Time{}
	defer func() {
		alsaStatusPath = oldPath
		alsaNextCheck = time.Time{}
		alsaClosedAt = time.Time{}
	}()

	resetMedia(t)
	setMedia(true, "test", "")
	now := time.Now()
	checkAlsaIdle(now)
	if !musicPlaying(t) {
		t.Fatal("первый проход только отмечает закрытие, состояние не трогает")
	}
	checkAlsaIdle(now.Add(alsaClosedGrace + time.Second))
	if musicPlaying(t) {
		t.Fatal("выход закрыт дольше grace — состояние должно сброситься")
	}
}
