package main

import (
	"testing"
	"time"
)

func TestWaitMediaDetectsChange(t *testing.T) {
	resetMediaStream(t)
	setMedia(false, "тест", "")

	go func() {
		time.Sleep(150 * time.Millisecond)
		setMedia(true, "тест", "")
	}()

	started := time.Now()
	if !waitMedia(true, 2*time.Second) {
		t.Fatal("waitMedia не увидел переход в playing")
	}
	if elapsed := time.Since(started); elapsed > time.Second {
		t.Fatalf("подтверждение заняло %v — опрос не работает", elapsed)
	}
}

func TestWaitMediaTimesOut(t *testing.T) {
	resetMediaStream(t)
	setMedia(false, "тест", "")

	if waitMedia(true, 200*time.Millisecond) {
		t.Fatal("waitMedia не должен подтверждать состояние, которого нет")
	}
}

func TestWaitMediaAlreadyInState(t *testing.T) {
	resetMediaStream(t)
	setMedia(true, "тест", "")

	started := time.Now()
	if !waitMedia(true, 2*time.Second) {
		t.Fatal("waitMedia не подтвердил уже достигнутое состояние")
	}
	if elapsed := time.Since(started); elapsed > 50*time.Millisecond {
		t.Fatalf("ожидание уже достигнутого состояния заняло %v", elapsed)
	}
}
