package main

import "testing"

// Кольцо «говорит» гасит сервер по концу всей очереди кусков. Если агент
// погасит его сам после первого куска, лампа потухнет посреди ответа —
// именно этот дефект здесь и закреплён.
func TestClearLedOnDisconnectIsSafeWhenNothingLit(t *testing.T) {
	old := currentLED
	defer func() { currentLED = old }()

	currentLED = ""
	clearLedOnDisconnect() // не должен трогать файлы и паниковать
	if currentLED != "" {
		t.Fatalf("currentLED изменился: %q", currentLED)
	}
}

func TestLedTimeoutLongEnoughForLongAnswers(t *testing.T) {
	// Ответ может готовиться и звучать десятки секунд; автосброс не должен
	// срабатывать раньше, чем сервер пришлёт свою команду.
	if ledTimeout < 120*1e9 {
		t.Fatalf("ledTimeout=%v — слишком короткий, кольцо снимется посреди ответа", ledTimeout)
	}
}
