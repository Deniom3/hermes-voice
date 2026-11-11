package main

import "testing"

// Строки сняты с Лайт 1 08.11 в 02:43:44–02:43:47 при переходе на следующий
// трек: новому потоку пришёл PLAYING, старому через 1.4 с — STOPPED и End.
const (
	lineNewPlaying = "[2083:2243] [2026-11-08 02:43:46.372] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=130404613 mark=CjSELd] switchUserState from UNSET to PLAYING"
	lineNewState   = "[2083:2243] [2026-11-08 02:43:46.377] [media] [info] [gstreamer_audio_player2.cpp:1170] [audio stream=130404613 mark=CjSELd] State changed from GST_STATE_PAUSED to GST_STATE_PLAYING"
	lineOldStopped = "[2083:2074] [2026-11-08 02:43:47.817] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=106025528 mark=ZR1wNP] switchUserState from PLAYING to STOPPED"
	lineOldEnd     = "[2083:2072] [2026-11-08 02:43:47.831] [media] [info] [audio_client.cpp:890] [audio id=fb07e9a2 stream=106025528 mark=ZR1wNP] handleEnd: Rss memory 36744/49450"
	lineNewStopped = "[2083:2074] [2026-11-08 02:44:10.000] [media] [info] [gstreamer_audio_player2.cpp:1915] [audio stream=130404613 mark=CjSELd] switchUserState from PLAYING to STOPPED"
)

func resetMediaStream(t *testing.T) {
	t.Helper()
	mediaMu.Lock()
	mediaPlaying, mediaStream, mediaTrack = false, "", ""
	mediaMu.Unlock()
}

func TestCrossfadeEndOfOldStreamKeepsPlaying(t *testing.T) {
	resetMediaStream(t)

	applyMediaLine(lineNewPlaying)
	if playing, _ := mediaState(); !playing {
		t.Fatal("после PLAYING нового потока музыка должна считаться играющей")
	}

	// Старый поток закончился — музыка при этом играет.
	applyMediaLine(lineOldStopped)
	applyMediaLine(lineOldEnd)
	if playing, _ := mediaState(); !playing {
		t.Fatal("конец старого потока при кроссфейде погасил состояние")
	}

	// Новый поток действительно остановился — теперь состояние снимается.
	applyMediaLine(lineNewStopped)
	if playing, _ := mediaState(); playing {
		t.Fatal("остановка играющего потока не сняла состояние")
	}
}

func TestEndOfUnknownStreamStillStops(t *testing.T) {
	resetMediaStream(t)
	applyMediaLine(lineNewState)
	if playing, _ := mediaState(); !playing {
		t.Fatal("State changed ... to PLAYING не включил состояние")
	}
	// Конец того же потока без предварительного PLAYING другого.
	applyMediaLine(lineNewStopped)
	if playing, _ := mediaState(); playing {
		t.Fatal("остановка потока не сняла состояние")
	}
}
