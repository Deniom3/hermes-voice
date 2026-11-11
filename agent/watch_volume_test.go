package main

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestWatchVolumeNotifiesOnChange(t *testing.T) {
	dir, restore := fakeAmixer(t, 147) // уровень 5
	defer restore()

	// Чистим канал от предыдущих событий
	for len(volumeNote) > 0 {
		<-volumeNote
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	go watchVolume(ctx)

	// Первое чтение: кэш был пустой, значит должно прийти событие.
	if !waitLevel(t, 5, 5*time.Second) {
		t.Fatal("не пришло событие с уровнем 5")
	}

	// Алиса убавила мимо нас — следующий опрос обязан это заметить.
	if err := os.WriteFile(filepath.Join(dir, "raw"), []byte("103"), 0o644); err != nil {
		t.Fatalf("запись микшера: %v", err)
	}
	if !waitLevel(t, 3, 6*time.Second) {
		t.Fatal("изменение микшера не попало в событие")
	}
}

func waitLevel(t *testing.T, want int, timeout time.Duration) bool {
	t.Helper()
	deadline := time.After(timeout)
	for {
		select {
		case lv := <-volumeNote:
			if lv.level == want {
				return true
			}
		case <-deadline:
			return false
		}
	}
}
