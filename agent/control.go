package main

import (
	"syscall"
	"net/http"
	"context"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ---------- управляющий канал ----------

type command struct {
	Cmd     string  `json:"cmd"`
	ID      string  `json:"id"`
	Name    string  `json:"name"`
	Audio   string  `json:"audio"`   // base64 mp3
	Volume  float64 `json:"volume"`  // множитель для gst volume
	Timeout int     `json:"timeout"` // сек для LED; 0 — обычный, <0 — без автосброса
}

type reply struct {
	OK         bool   `json:"ok"`
	ID         string `json:"id"`
	Err        string `json:"err,omitempty"`
	WasPlaying bool   `json:"was_playing,omitempty"`
	Playing    *bool  `json:"playing,omitempty"`
	Track      string `json:"track,omitempty"`
}

const (
	maindPath  = "/system/vendor/quasar/maind"
	ledUtil    = "/system/vendor/quasar/led_util"
	ledDir     = "/data/quasar/ledpatterns"
	alicedAddr = "127.0.0.1:9875"
	capName    = "PlaybackControlCapability"
	rpcMember  = 210
)

const ledTimeout = 15 * time.Second

// resetLED гасит все зоны кольца.
func resetLED() {
	for _, color := range []string{"blue", "green", "red"} {
		for i := 1; i <= 3; i++ {
			p := fmt.Sprintf("/sys/class/leds/micro:%s:ring%d/brightness", color, i)
			_ = os.WriteFile(p, []byte("0"), 0o644)
		}
	}
}

// pausedByUs — музыка была поставлена на паузу именно нами, её и возвращаем.
var pausedByUs bool

var (
	ledMu   sync.Mutex
	ledProc *os.Process
	aliceMu sync.Mutex
	aliceCn net.Conn
)

// ---------- LED ----------

// killLEDProcesses добивает все висящие анимации: led_util — это maind,
// он держит LED-контроллер до прерывания и конфликтует с Алисой.
func killLEDProcesses() {
	entries, err := os.ReadDir("/proc")
	if err != nil {
		return
	}
	self := os.Getpid()
	for _, e := range entries {
		pid, err := strconv.Atoi(e.Name())
		if err != nil || pid == self {
			continue
		}
		b, err := os.ReadFile("/proc/" + e.Name() + "/cmdline")
		if err != nil {
			continue
		}
		line := strings.ReplaceAll(string(b), "\x00", " ")
		if strings.Contains(line, "led_util") && strings.Contains(line, ".led") {
			if p, err := os.FindProcess(pid); err == nil {
				_ = p.Kill()
			}
		}
	}
}

func stopLED() {
	ledMu.Lock()
	if ledProc != nil {
		_ = ledProc.Kill()
		_, _ = ledProc.Wait()
		ledProc = nil
	}
	ledMu.Unlock()
	killLEDProcesses()
}

// playerMu/playerProc: ALSA-устройство одно, поэтому одновременно может
// играть только один процесс. Висевший gst держал карту в PREPARED,
// из-за чего следующий звук молчал, а цикл растягивался на минуту.
var (
	playerMu   sync.Mutex
	playerProc *os.Process
)

func setPlayer(p *os.Process) {
	playerMu.Lock()
	playerProc = p
	playerMu.Unlock()
}

func killPlayer() {
	playerMu.Lock()
	p := playerProc
	playerProc = nil
	playerMu.Unlock()
	if p != nil {
		// процесс мог уже завершиться сам: тогда pid мёртв, а его номер мог
		// достаться другому процессу — бить по нему нельзя
		if p.Signal(syscall.Signal(0)) != nil {
			return
		}
		// maind запускает gst-launch дочерним процессом: убивать нужно всю группу,
		// иначе ребёнок продолжает держать ALSA и следующий звук молчит.
		// Сначала SIGTERM — после SIGKILL карта оставалась в PREPARED и звук
		// не играл до перезагрузки колонки.
		_ = syscall.Kill(-p.Pid, syscall.SIGTERM)
		time.Sleep(250 * time.Millisecond)
		_ = syscall.Kill(-p.Pid, syscall.SIGKILL)
		_ = p.Kill()
		log.Printf("аудио: процесс %d остановлен", p.Pid)
	}
}

func audioCmd(pipeline string) *exec.Cmd {
	cmd := exec.Command(maindPath, "--gst-launch", pipeline)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return cmd
}

// currentLED — что сейчас горит: агент сам гасит индикацию,
// если сервер пропал посреди ответа
var currentLED string

func setLED(name string, timeoutSec int) error {
	stopLED()
	currentLED = name
	resetLED()
	if name == "" {
		return nil
	}
	limit := ledTimeout
	switch {
	case timeoutSec > 0:
		limit = time.Duration(timeoutSec) * time.Second
	case timeoutSec < 0:
		limit = 0 // без автосброса
	}
	path := fmt.Sprintf("%s/%s.led", ledDir, name)
	if _, err := os.Stat(path); err != nil {
		return fmt.Errorf("нет файла %s", path)
	}
	cmd := exec.Command(ledUtil, path)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("led_util: %w", err)
	}
	ledMu.Lock()
	ledProc = cmd.Process
	ledMu.Unlock()
	go func() {
		_ = cmd.Wait()
		ledMu.Lock()
		if ledProc == cmd.Process {
			ledProc = nil
		}
		ledMu.Unlock()
	}()
	// страховка: анимация не должна висеть дольше своего таймаута
	if limit > 0 {
		go func(p *os.Process, name string) {
			time.Sleep(limit)
		ledMu.Lock()
		alive := ledProc == p
		ledMu.Unlock()
		if alive {
			log.Printf("led: %s висела дольше %v — снимаю", name, ledTimeout)
			stopLED()
			resetLED()
		}
		}(cmd.Process, name)
	}
	return nil
}

// ---------- плеер (aliced:9875) ----------

func aliceFrame(field int, payload []byte) []byte {
	remote := append(fBytes(7, []byte(capName)), fBytes(field, payload)...)
	env := append(fBytes(252, remote), fVarint(253, uint64(rpcMember))...)
	out := make([]byte, 4, 4+len(env))
	binary.LittleEndian.PutUint32(out, uint32(len(env)))
	return append(out, env...)
}

func aliceConnect() (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", alicedAddr, 3*time.Second)
	if err != nil {
		return nil, err
	}
	sub := aliceFrame(20, fBytes(1, []byte(capName)))
	if _, err := conn.Write(sub); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}

func playerCommand(method uint64) error {
	aliceMu.Lock()
	defer aliceMu.Unlock()
	if aliceCn == nil {
		conn, err := aliceConnect()
		if err != nil {
			return fmt.Errorf("aliced: %w", err)
		}
		aliceCn = conn
	}
	frame := aliceFrame(13, fVarint(1, method))
	_ = aliceCn.SetWriteDeadline(time.Now().Add(3 * time.Second))
	if _, err := aliceCn.Write(frame); err != nil {
		_ = aliceCn.Close()
		aliceCn = nil
		// одна повторная попытка с новым соединением
		conn, err2 := aliceConnect()
		if err2 != nil {
			return fmt.Errorf("aliced reconnect: %w", err2)
		}
		aliceCn = conn
		_ = aliceCn.SetWriteDeadline(time.Now().Add(3 * time.Second))
		if _, err3 := aliceCn.Write(frame); err3 != nil {
			_ = aliceCn.Close()
			aliceCn = nil
			return fmt.Errorf("aliced write: %w", err3)
		}
	}
	time.Sleep(120 * time.Millisecond)
	return nil
}

// ---------- воспроизведение ----------

func playAudio(id string, audio []byte, volume float64) error {
	if volume <= 0 {
		volume = 4.0
	}
	// нажатие, сделанное вне озвучки, не должно обрывать следующий ответ
	for {
		select {
		case <-interruptCh:
			continue
		default:
		}
		break
	}
	path := fmt.Sprintf("/tmp/hermes-say-%s.mp3", sanitize(id))
	if err := os.WriteFile(path, audio, 0o644); err != nil {
		return fmt.Errorf("запись файла: %w", err)
	}
	defer os.Remove(path)

	pipeline := fmt.Sprintf(
		"filesrc location=%s ! decodebin ! audioconvert ! audioresample ! volume volume=%.2f ! alsasink",
		path, volume,
	)
	killPlayer()
	cmd := audioCmd(pipeline)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("gst-launch: %w", err)
	}
	setPlayer(cmd.Process)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		// сбрасываем обязательно: иначе следующий killPlayer() бьёт по уже
		// мёртвому pid, а он может быть переиспользован чужим процессом
		setPlayer(nil)
		if err != nil {
			return fmt.Errorf("gst-launch: %w (%s)", err, strings.TrimSpace(stderr.String()))
		}
	case <-interruptCh:
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		time.Sleep(150 * time.Millisecond)
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		_ = cmd.Process.Kill()
		<-done
		setPlayer(nil)
		log.Printf("say: прервано кнопкой")
	}
	// страховка: если сервер умер или не успел прислать led off,
	// кольцо «говорит» не должно остаться горящим
	if currentLED == "hermes_speaking" {
		if err := setLED("", 0); err != nil {
			log.Printf("led off после say: %v", err)
		}
	}
	return nil
}

// interruptCh — нажатия физической кнопки, пока идёт озвучка.
var interruptCh = make(chan struct{}, 4)

// notifyInterrupt сообщает серверу, что нажата кнопка паузы: он обрывает
// ответ, гасит кольцо, возвращает музыку и выходит из ожидания фразы.
func notifyInterrupt() {
	req, err := http.NewRequest("POST", hubHTTP("/interrupt"), nil)
	if err != nil {
		return
	}
	c := &http.Client{Timeout: 3 * time.Second}
	resp, err := c.Do(req)
	if err != nil {
		log.Printf("interrupt: %v", err)
		return
	}
	resp.Body.Close()
	log.Printf("interrupt: сервер уведомлён (%d)", resp.StatusCode)
}

// watchButtonsLog следит за логом maind: кнопки физической панели обрабатывает
// сам maind и пишет их как "Button Event: play_pause Event: 0". evdev захвачен
// им эксклюзивно, поэтому единственный доступный источник — этот лог.
func watchButtonsLog(ctx context.Context) {
	const path = "/data/quasar/daemons_logs/maind.log"
	var f *os.File
	var offset int64
	var tail string
	buf := make([]byte, 8192)

	for {
		select {
		case <-ctx.Done():
			return
		default:
		}

		if f == nil {
			ff, err := os.Open(path)
			if err != nil {
				time.Sleep(2 * time.Second)
				continue
			}
			st, err := ff.Stat()
			if err != nil {
				ff.Close()
				time.Sleep(2 * time.Second)
				continue
			}
			f = ff
			offset = st.Size() // начинаем с конца: старые нажатия не считаем
			_, _ = f.Seek(offset, io.SeekStart)
			log.Printf("кнопки: слежу за %s", path)
		}

		// Сравниваем с файлом ПО ПУТИ, а не с открытым дескриптором: после
		// ротации f.Stat() показывает старый inode, размер не меняется — и
		// агент навсегда застревает на переименованном файле, не видя кнопок.
		pathSt, err := os.Stat(path)
		if err != nil {
			f.Close()
			f = nil
			continue
		}
		curSt, err := f.Stat()
		if err != nil || !os.SameFile(pathSt, curSt) || pathSt.Size() < offset {
			f.Close()
			f = nil
			tail = ""
			continue
		}

		n, err := f.Read(buf)
		if n > 0 {
			offset += int64(n)
			chunk := tail + string(buf[:n])
			lines := strings.Split(chunk, "\n")
			tail = lines[len(lines)-1]
			if len(tail) > 4096 {
				tail = ""
			}
			for _, line := range lines[:len(lines)-1] {
				if strings.Contains(line, "Button Event: play_pause") && strings.Contains(line, "Event: 0") {
					log.Printf("кнопка паузы: нажата")
					go notifyInterrupt()
					go killPlayer()
					select {
					case interruptCh <- struct{}{}:
					default:
					}
				}
			}
		}
		if err != nil {
			time.Sleep(200 * time.Millisecond)
		}
	}
}

const soundDir = "/system/vendor/quasar/sounds/common"

// playSound проигрывает штатный звук колонки (opus) без сети.
func playSound(name string) error {
	if name == "" {
		return fmt.Errorf("пустое имя звука")
	}
	path := fmt.Sprintf("%s/%s.opus", soundDir, sanitize(name))
	if _, err := os.Stat(path); err != nil {
		return fmt.Errorf("нет файла %s", path)
	}
	pipeline := fmt.Sprintf(
		"filesrc location=%s ! decodebin ! audioconvert ! audioresample ! alsasink", path)
	killPlayer()
	cmd := audioCmd(pipeline)
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("sound: %w", err)
	}
	setPlayer(cmd.Process)
	done := make(chan error, 1)
	go func() { done <- cmd.Wait() }()
	select {
	case err := <-done:
		setPlayer(nil)
		if err != nil {
			return fmt.Errorf("sound: %w", err)
		}
	case <-time.After(5 * time.Second):
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGTERM)
		time.Sleep(150 * time.Millisecond)
		_ = syscall.Kill(-cmd.Process.Pid, syscall.SIGKILL)
		_ = cmd.Process.Kill()
		<-done
		setPlayer(nil)
		log.Printf("sound %s: таймаут, процесс убит", name)
	}
	return nil
}

func sanitize(s string) string {
	if s == "" {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	var b strings.Builder
	for _, r := range s {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') || r == '-' || r == '_' {
			b.WriteRune(r)
		}
	}
	if b.Len() == 0 {
		return fmt.Sprintf("%d", time.Now().UnixNano())
	}
	return b.String()
}

// ---------- обработчик команд ----------

func handleCommand(c command) reply {
	switch c.Cmd {
	case "ping":
		return reply{OK: true, ID: c.ID}
	case "state":
		playing, track := mediaState()
		return reply{OK: true, ID: c.ID, Playing: &playing, Track: track}
	case "led":
		if err := setLED(c.Name, c.Timeout); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		return reply{OK: true, ID: c.ID}
	case "pause":
		// Источник истины — события mediad, а не ALSA: при dmix status врёт.
		was, _ := mediaState()
		if err := playerCommand(1); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		pausedByUs = was
		log.Printf("pause: было воспроизведение=%v", was)
		return reply{OK: true, ID: c.ID, WasPlaying: was}
	case "play":
		if !pausedByUs {
			// музыку выключал не я — не включаю её обратно
			log.Printf("play: пропуск, паузу ставили не мы")
			return reply{OK: true, ID: c.ID}
		}
		if err := playerCommand(0); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		pausedByUs = false
		return reply{OK: true, ID: c.ID}
	case "sound":
		// играем в отдельной горутине: синхронный вызов держал ctrlLoop
		// до конца проигрывания, и следующая команда (кольцо) опаздывала
		name := c.Name
		go func() {
			if err := playSound(name); err != nil {
				log.Printf("sound %s: %v", name, err)
			}
		}()
		return reply{OK: true, ID: c.ID}
	case "say":
		data, err := base64.StdEncoding.DecodeString(c.Audio)
		if err != nil {
			return reply{ID: c.ID, Err: "base64: " + err.Error()}
		}
		if err := playAudio(c.ID, data, c.Volume); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		return reply{OK: true, ID: c.ID}
	default:
		return reply{ID: c.ID, Err: "неизвестная команда: " + c.Cmd}
	}
}
