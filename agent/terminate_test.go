package main

import (
	"os/exec"
	"syscall"
	"testing"
	"time"
)

// startGroup запускает команду в отдельной группе процессов — как playAudio,
// иначе kill(-pid) попадёт по нашей же группе.
func startGroup(t *testing.T, script string) *exec.Cmd {
	t.Helper()
	cmd := exec.Command("sh", "-c", script)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	if err := cmd.Start(); err != nil {
		t.Fatalf("старт: %v", err)
	}
	// дать sh выполнить trap: SIGTERM, пришедший до него, завершит процесс
	time.Sleep(200 * time.Millisecond)
	return cmd
}

func TestTerminateGracefullyUsesSigterm(t *testing.T) {
	// `exec` важен: без него между нами и sleep стоит sh, и в контейнере
	// (где шаг может быть PID 1) SIGTERM достаётся оболочке, а не процессу.
	cmd := startGroup(t, "exec sleep 10")
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	started := time.Now()
	killed := terminateGracefully(cmd.Process.Pid, 2*time.Second)
	elapsed := time.Since(started)

	if killed {
		t.Fatal("процесс, завершившийся по SIGTERM, не должен получать SIGKILL")
	}
	if elapsed > time.Second {
		t.Fatalf("ожидание SIGTERM заняло %v — проверка группы не работает", elapsed)
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("процесс не завершился")
	}
}

func TestTerminateGracefullyEscalatesToSigkill(t *testing.T) {
	// sh с игнорированием SIGTERM: единственный способ остановить — SIGKILL
	// sh сам игнорирует SIGTERM и продолжает цикл: sleep внутри умирает, но
	// группа остаётся живой — единственный способ остановить — SIGKILL
	cmd := startGroup(t, `trap "" TERM; while :; do sleep 0.1; done`)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()

	if !terminateGracefully(cmd.Process.Pid, 300*time.Millisecond) {
		t.Fatal("процесс, игнорирующий SIGTERM, должен получить SIGKILL")
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("процесс не завершился после SIGKILL")
	}
}
