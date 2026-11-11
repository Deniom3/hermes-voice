package main

import "testing"

// Перебивание приходит, когда сервер уже отправил несколько кусков звука:
// stop обязан выбросить очередь, иначе ответ доигрывает после нового вопроса.
func TestStopPlaybackDrainsQueue(t *testing.T) {
	for len(playQ) > 0 {
		<-playQ
	}
	playQ <- audioItem{data: []byte("a")}
	playQ <- audioItem{data: []byte("b")}

	if n := stopPlayback(); n != 2 {
		t.Fatalf("ожидалось 2 выброшенных куска, получено %d", n)
	}
	if len(playQ) != 0 {
		t.Fatalf("очередь не пуста: %d", len(playQ))
	}
}
