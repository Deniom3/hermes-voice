package main

import "testing"

// Кнопка паузы не должна ходить отдельным HTTP-запросом: она кладёт сигнал
// в очередь WebSocket-сессии, а хаб отменяет ход именно этого соединения.
func TestNotifyHubInterruptQueuesWSMessage(t *testing.T) {
	for {
		select {
		case <-interruptNote:
		default:
			goto drained
		}
	}
drained:
	notifyHubInterrupt(false)

	select {
	case long := <-interruptNote:
		if long {
			t.Fatal("короткое нажатие помечено длинным")
		}
	default:
		t.Fatal("interruptNote пуст: кнопка не поставила сообщение в очередь")
	}
}

// Длинное нажатие должно доезжать до хаба отдельным признаком: по нему
// хаб переключает источник на Алису, а не ставит паузу.
func TestNotifyHubInterruptMarksLongPress(t *testing.T) {
	for {
		select {
		case <-interruptNote:
		default:
			goto drained
		}
	}
drained:
	notifyHubInterrupt(true)

	select {
	case long := <-interruptNote:
		if !long {
			t.Fatal("длинное нажатие не помечено")
		}
	default:
		t.Fatal("interruptNote пуст")
	}
}
