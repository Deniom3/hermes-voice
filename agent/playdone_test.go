package main

import "testing"

// Сервер гасит кольцо и возвращает музыку по play_done, поэтому событие
// обязано уходить ровно один раз после последнего куска: раньше — кольцо
// тухнет посреди речи, на каждый кусок — музыка возвращается между кусками.
func TestPlayDoneSignaledOncePerBurst(t *testing.T) {
	drainPlayDone()
	notifyPlayDone()
	notifyPlayDone() // очередь сигналов вмещает один: второй не копится
	if n := len(playDoneNote); n != 1 {
		t.Fatalf("в очереди сигналов %d, ожидался 1", n)
	}
	drainPlayDone()
}

func TestPlayDoneDrainedIsSilent(t *testing.T) {
	drainPlayDone()
	select {
	case <-playDoneNote:
		t.Fatal("пустая очередь не должна давать сигнал")
	default:
	}
}

func drainPlayDone() {
	for {
		select {
		case <-playDoneNote:
		default:
			return
		}
	}
}
