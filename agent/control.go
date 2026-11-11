package main

import (
	"context"
	"encoding/base64"
	"encoding/binary"
	"fmt"
	"log"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"
)

// ---------- управляющий канал ----------

type command struct {
	Cmd     string  `json:"cmd"`
	ID      string  `json:"id"`
	Name    string  `json:"name"`
	Audio   string  `json:"audio"`   // base64 mp3
	Volume  float64 `json:"volume"`  // множитель для gst volume
	Level   int     `json:"level"`   // громкость станции: уровень 0..10 (0 — мьют)
	Timeout int     `json:"timeout"` // сек для LED; 0 — обычный, <0 — без автосброса
}

type reply struct {
	OK              bool     `json:"ok"`
	ID              string   `json:"id"`
	Err             string   `json:"err,omitempty"`
	WasPlaying      bool     `json:"was_playing,omitempty"`
	Sendspin        *bool    `json:"sendspin,omitempty"`
	Playing         *bool    `json:"playing,omitempty"`
	NativePlaying   *bool    `json:"native_playing,omitempty"`
	SendspinPlaying *bool    `json:"sendspin_playing,omitempty"`
	SendspinEngaged *bool    `json:"sendspin_engaged,omitempty"`
	Track           string   `json:"track,omitempty"`
	Volume          *int     `json:"volume,omitempty"` // проценты ALSA (диагностика)
	VolumeDB        *float64 `json:"volume_db,omitempty"`
	Level           *int     `json:"level,omitempty"` // уровень 0..10, единый контракт
}

const (
	maindPath = "/system/vendor/quasar/maind"
	ledUtil   = "/system/vendor/quasar/led_util"
	// ledDir — свои .led-анимации (есть только на micro; на plum формата .led нет)
	ledDir     = "/data/quasar/ledpatterns"
	alicedAddr = "127.0.0.1:9875"
	capName    = "PlaybackControlCapability"
	rpcMember  = 210
)

const ledTimeout = 180 * time.Second

// plat выставляется в main() по флагу -platform (auto по умолчанию).
var plat = microProfile()

// ledTake снимает штатный триггер кольца. На plum активен led_ring_fb:
// пока он включён, запись brightness не даёт картинки, а запись нуля может
// сбить триггер и убрать анимации Алисы. На micro триггера нет.
func ledTake() {
	if plat.LEDTrigger == "" {
		return
	}
	for _, color := range []string{"blue", "green", "red"} {
		for i := 1; i <= 3; i++ {
			_ = os.WriteFile(ledTriggerPath(plat.LEDPrefix, color, i), []byte("none"), 0o644)
		}
	}
}

func ledRelease() {
	if plat.LEDTrigger == "" {
		return
	}
	for _, color := range []string{"blue", "green", "red"} {
		for i := 1; i <= 3; i++ {
			_ = os.WriteFile(ledTriggerPath(plat.LEDPrefix, color, i), []byte(plat.LEDTrigger), 0o644)
		}
	}
}

func ledWrite(color string, ring, value int) {
	p := ledBrightnessPath(plat.LEDPrefix, color, ring)
	_ = os.WriteFile(p, []byte(strconv.Itoa(value)), 0o644)
}

// resetLED гасит все зоны кольца.
func resetLED() {
	ledTake()
	for _, color := range []string{"blue", "green", "red"} {
		for i := 1; i <= 3; i++ {
			ledWrite(color, i, 0)
		}
	}
	ledRelease()
}

var (
	ledMu   sync.Mutex
	ledProc *os.Process
	ledGen  int
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
	if plat.Name == "plum" {
		// на plum кольцом владеет led_ring_fb: погасить и вернуть триггер,
		// иначе штатные анимации Алисы пропадут
		resetLED()
	}
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

// terminateGracefully завершает группу процессов аудио: сначала SIGTERM,
// затем, если группа не ушла за grace, SIGKILL. Возвращает true, если
// понадобился SIGKILL.
//
// Почему не убивать сразу: SIGKILL оставляет сегмент dmix неосвобождённым, и
// следующий клиент не может открыть устройство — колонка молчит до перезагрузки.
// Почему важен лог: если SIGKILL понадобился, процесс не реагирует на SIGTERM,
// и это стоит разобрать, а не считать нормой.
func terminateGracefully(pid int, grace time.Duration) bool {
	if pid <= 0 {
		return false
	}
	if err := syscall.Kill(-pid, syscall.SIGTERM); err != nil {
		return false // группы уже нет
	}
	deadline := time.Now().Add(grace)
	for time.Now().Before(deadline) {
		// kill(-pid, 0) возвращает ESRCH, когда в группе не осталось процессов
		// (зомби считается живым до Wait — его снимает горутина cmd.Wait)
		if syscall.Kill(-pid, 0) != nil {
			return false
		}
		time.Sleep(20 * time.Millisecond)
	}
	_ = syscall.Kill(-pid, syscall.SIGKILL)
	return true
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
		if terminateGracefully(p.Pid, time.Second) {
			log.Printf("аудио: процесс %d не ответил на SIGTERM — SIGKILL", p.Pid)
		} else {
			log.Printf("аудио: процесс %d остановлен по SIGTERM", p.Pid)
		}
		_ = p.Kill()
	}
}

// stopPlayback глушит текущий звук и выбрасывает очередь. Перебивание
// приходит, когда сервер уже успел отправить несколько кусков: доигрывать
// их после начала нового хода нельзя.
func stopPlayback() int {
	drained := 0
	for {
		select {
		case <-playQ:
			drained++
		default:
			killPlayer()
			return drained
		}
	}
}

func audioCmd(pipeline string) *exec.Cmd {
	cmd := exec.Command(maindPath, "--gst-launch", pipeline)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return cmd
}

// aplayCmd — прямое проигрывание WAV через ALSA. Нужно на plum: у её maind
// нет опции --gst-launch, а декодера mp3/opus на борту нет.
func aplayCmd(path string) *exec.Cmd {
	cmd := exec.Command("/usr/bin/aplay", "-q", path)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setpgid: true}
	return cmd
}

// currentLED — что сейчас горит: агент сам гасит индикацию,
// если сервер пропал посреди ответа
var currentLED string

// ledStaticColors — на plum нет .led-анимаций (в ledpatterns только clock/oknix),
// поэтому индикация статическая. Цвета и разбивка по трём зонам кольца взяты
// из .led-файлов Лайт 1: на micro первая строка кадра задаёт цвет каждой зоны,
// и именно из-за этого «голубой» Лайт 1 на plum выглядел синим, если писать
// {0,0,160} во все зоны.
var ledStaticColors = map[string][3][3]int{
	// 0fffc3 в трёх зонах — голубой Лайт 1
	"hermes_listening": {{0x0f, 0xff, 0xc3}, {0x0f, 0xff, 0xc3}, {0x0f, 0xff, 0xc3}},
	// speaking: старт голубой, затем 0dd7a4 и 0aab83
	"hermes_speaking": {{0x0f, 0xff, 0xc3}, {0x0d, 0xd7, 0xa4}, {0x0a, 0xab, 0x83}},
	// thinking: 6a4906 / 7d5607 / 0d0901 — тёплый оранжевый
	"hermes_thinking": {{0x6a, 0x49, 0x06}, {0x7d, 0x56, 0x07}, {0x0d, 0x09, 0x01}},
	// tool: 376a05 / 427d07 / 070d00 — зелёный
	"hermes_tool": {{0x37, 0x6a, 0x05}, {0x42, 0x7d, 0x07}, {0x07, 0x0d, 0x00}},
	// error: 490000 / 3b0000 / 3b0000 — красный
	"hermes_error": {{0x49, 0x00, 0x00}, {0x3b, 0x00, 0x00}, {0x3b, 0x00, 0x00}},
	// pause: e800f3 — штатная вспышка Алисы (player_pause.led): яркий
	// розово-фиолетовый и плавное затухание. На plum анимаций нет, поэтому
	// статический цвет того же оттенка, гаснет по таймауту команды.
	"hermes_pause": {{0xe8, 0x00, 0xf3}, {0xe8, 0x00, 0xf3}, {0xe8, 0x00, 0xf3}},
	// Режим на время объявления: local — зелёный, cloud — фиолетовый Алисы.
	// Гаснет по команде хаба после фразы.
	"hermes_mode_local": {{0x37, 0xd6, 0x7a}, {0x37, 0xd6, 0x7a}, {0x37, 0xd6, 0x7a}},
	"hermes_mode_cloud": {{0xe8, 0x00, 0xf3}, {0xe8, 0x00, 0xf3}, {0xe8, 0x00, 0xf3}},
}

// setStaticLED — индикация для plum: снять led_ring_fb, выставить цвет,
// а по таймауту (или команде) вернуть триггер через stopLED.
func setStaticLED(name string, timeoutSec int) error {
	stopLED()
	currentLED = name
	if name == "" {
		return nil
	}
	color, ok := ledStaticColors[name]
	if !ok {
		color = [3][3]int{{0x0f, 0xff, 0xc3}, {0x0f, 0xff, 0xc3}, {0x0f, 0xff, 0xc3}}
	}
	ledTake()
	for _, c := range []string{"blue", "green", "red"} {
		for i := 1; i <= 3; i++ {
			ledWrite(c, i, 0)
		}
	}
	for i := 1; i <= 3; i++ {
		zone := color[i-1]
		ledWrite("red", i, zone[0])
		ledWrite("green", i, zone[1])
		ledWrite("blue", i, zone[2])
	}
	limit := ledTimeout
	switch {
	case timeoutSec > 0:
		limit = time.Duration(timeoutSec) * time.Second
	case timeoutSec < 0:
		limit = 0
	}
	ledMu.Lock()
	ledGen++
	gen := ledGen
	ledMu.Unlock()
	if limit > 0 {
		go func(n string, g int) {
			time.Sleep(limit)
			ledMu.Lock()
			alive := ledGen == g
			ledMu.Unlock()
			if alive {
				log.Printf("led: %s висела дольше %v — снимаю", n, limit)
				stopLED()
			}
		}(name, gen)
	}
	return nil
}

func setLED(name string, timeoutSec int) error {
	if plat.Name == "plum" {
		return setStaticLED(name, timeoutSec)
	}
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
	return aliceFrameCap(capName, field, payload)
}

func aliceFrameCap(name string, field int, payload []byte) []byte {
	remote := append(fBytes(7, []byte(name)), fBytes(field, payload)...)
	env := append(fBytes(252, remote), fVarint(253, uint64(rpcMember))...)
	out := make([]byte, 4, 4+len(env))
	binary.LittleEndian.PutUint32(out, uint32(len(env)))
	return append(out, env...)
}

func aliceConnect() (net.Conn, error) {
	return aliceConnectCap(capName)
}

func aliceConnectCap(name string) (net.Conn, error) {
	conn, err := net.DialTimeout("tcp", alicedAddr, 3*time.Second)
	if err != nil {
		return nil, err
	}
	sub := aliceFrameCap(name, 20, fBytes(1, []byte(name)))
	if _, err := conn.Write(sub); err != nil {
		conn.Close()
		return nil, err
	}
	return conn, nil
}

// Методы плеера. Проверено на обеих колонках 08.11: 0 — PLAY, 1 — PAUSE.
// Раньше 1 называли TOGGLE_PLAY_PAUSE, и это порождало «спорное» поведение:
// будто бы повторная пауза включает музыку. Фактически повторный pause
// оставляет паузу, повторный play продолжает трек.
const (
	playerPlay  uint64 = 0
	playerPause uint64 = 1
)

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
	payload := fVarint(1, method)
	// Прошивка Лайт 2 (0.369.1.50) требует второй аргумент (can_request_music):
	// без него вызов падает с "Missing can_request_music argument". На Лайт 1
	// поле лишнее, поэтому добавляем его только профилю plum.
	if plat.Name == "plum" {
		payload = append(payload, fVarint(2, 1)...)
	}
	frame := aliceFrame(13, payload)
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
	return nil
}

// waitMedia ждёт, пока состояние музыки станет ожидаемым. Ответ ok без
// подтверждения скрывал случай, когда команда ушла, а плеер её не применил:
// хаб считал музыку поставленной на паузу и потом не возвращал её.
func waitMedia(want bool, timeout time.Duration) bool {
	deadline := time.Now().Add(timeout)
	for {
		if playing, _ := mediaState(); playing == want {
			return true
		}
		if time.Now().After(deadline) {
			return false
		}
		time.Sleep(100 * time.Millisecond)
	}
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
	ext := ".mp3"
	if plat.AudioMode == "aplay" {
		ext = ".wav" // aplay выбирает демультиплексор по расширению
	}
	path := fmt.Sprintf("/tmp/hermes-say-%s%s", sanitize(id), ext)
	if err := os.WriteFile(path, audio, 0o644); err != nil {
		return fmt.Errorf("запись файла: %w", err)
	}
	defer os.Remove(path)

	killPlayer()
	var cmd *exec.Cmd
	if plat.AudioMode == "aplay" {
		// на plum нет gst: сервер должен присылать WAV, иначе декодировать нечем
		if len(audio) < 12 || string(audio[:4]) != "RIFF" {
			return fmt.Errorf("платформа %s: ожидается WAV (mp3 на борту не декодируется)", plat.Name)
		}
		cmd = aplayCmd(path)
	} else {
		pipeline := fmt.Sprintf(
			"filesrc location=%s ! decodebin ! audioconvert ! audioresample ! volume volume=%.2f ! alsasink",
			path, volume,
		)
		cmd = audioCmd(pipeline)
	}
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
			// Убийство процесса командой `stop` или кнопкой — не сбой
			// воспроизведения: перебивание не должно выглядеть ошибкой.
			if strings.Contains(err.Error(), "signal:") {
				return nil
			}
			return fmt.Errorf("gst-launch: %w (%s)", err, strings.TrimSpace(stderr.String()))
		}
	case <-interruptCh:
		if terminateGracefully(cmd.Process.Pid, 500*time.Millisecond) {
			log.Printf("say: процесс не ответил на SIGTERM — SIGKILL")
		}
		<-done
		setPlayer(nil)
		log.Printf("say: прервано кнопкой")
	}
	// Кольцо «говорит» здесь НЕ гасим: ответ приходит несколькими кусками,
	// и раньше после первого же куска кольцо тухло, хотя колонка говорила
	// ещё десятки секунд. Гасит сервер по концу всей очереди (play_done);
	// страховка на случай обрыва связи — clearSpeakingOnDisconnect().
	return nil
}

// clearLedOnDisconnect гасит индикацию, если сервер пропал посреди хода:
// иначе анимация осталась бы горящей навсегда.
func clearLedOnDisconnect() {
	if currentLED == "" {
		return
	}
	if err := setLED("", 0); err != nil {
		log.Printf("led off после обрыва связи: %v", err)
	} else {
		log.Printf("led off: соединение с сервером потеряно")
	}
}

// interruptCh — нажатия физической кнопки, пока идёт озвучка.
var interruptCh = make(chan struct{}, 4)

// notifyHubInterrupt просит цикл WebSocket-сессии отправить interrupt.
// Кнопка живёт в том же соединении, что микрофон и ответ, поэтому хабу
// не нужно угадывать колонку по имени или IP. long — длинное нажатие:
// это явное «переключи на Алису», а не пауза текущего источника.
// localButtonToggle — кнопка без хаба: свой toggle на нативном плеере.
// EVIOCGRAB у нас, поэтому без этого fallback нажатие уходит в пустоту.
func localButtonToggle() {
	playing, _ := mediaState()
	if playing {
		if err := playerCommand(playerPause); err != nil {
			log.Printf("кнопка без хаба: пауза не встала: %v", err)
		} else {
			log.Printf("кнопка без хаба: натив на паузе")
		}
	} else {
		if err := playerCommand(playerPlay); err != nil {
			log.Printf("кнопка без хаба: play не прошёл: %v", err)
		} else {
			log.Printf("кнопка без хаба: натив продолжен")
		}
	}
	if sendspinEngaged() {
		sendspinPause()
	}
}

func notifyHubInterrupt(long bool) {
	select {
	case interruptNote <- long:
	default:
	}
}

// pressPauseButton — реакция на физическое нажатие кнопки паузы.
// long — нажатие держали ≥ longPressAfter: это явная команда «Алиса».
func pressPauseButton(long bool) {
	log.Printf("кнопка: нажата (длинное=%v)", long)
	if !hubOnline.Load() {
		// Хаба нет — обслуживаем кнопку сами, иначе она не делает ничего.
		localButtonToggle()
		return
	}
	if long {
		// Длинное нажатие — переключение на Алису. Натив не паузим: хаб
		// снимет MA и даст команду native_play. Свой поток гасим, чтобы
		// не играл буфер поверх.
		if sendspinEngaged() {
			sendspinPause()
		}
		go notifyHubInterrupt(true)
		go killPlayer()
		select {
		case interruptCh <- struct{}{}:
		default:
		}
		return
	}
	// Короткое нажатие при живом хабе — только сигнал. Паузу ставит хаб,
	// когда снимет факты: если паузить здесь, хаб через полсекунды увидит
	// уже тишину, решит «играть нечему» и включит источник обратно —
	// ровно «пауза со второго нажатия». Натив и Sendspin — зона хаба.
	// killPlayer гасит только наш aplay/TTS: это не музыка, хаб о нём
	// всё равно не знает и буфер доиграет поверх ответа.
	go notifyHubInterrupt(false)
	go killPlayer()
	select {
	case interruptCh <- struct{}{}:
	default:
	}
}

// watchButtons читает кнопку из evdev, а при недоступности устройства — из
// лога maind. evdev даёт отдельные нажатие и отпускание (нужно для длинного
// нажатия), лог — только факт нажатия.
func watchButtons(ctx context.Context) {
	for {
		err := watchAliceKey(ctx, pressPauseButton, changeVolume)
		if ctx.Err() != nil {
			return
		}
		// Второго источника нет намеренно: разбор лога давал только факт
		// нажатия и ломался на ротации. Здесь одна ветка — ждём и пробуем
		// снова, чтобы пережить перезапуск maind.
		log.Printf("кнопка: evdev недоступен (%v) — повтор через 10 с", err)
		select {
		case <-ctx.Done():
			return
		case <-time.After(10 * time.Second):
		}
	}
}

// soundPath ищет штатный звук. На micro это <sounds/common>/<name>.opus
// (играет gst через maind). На plum каталога common нет, а aplay умеет
// только WAV, поэтому ищем <sounds>/<name>.wav.
func soundPath(name string) (string, error) {
	base := sanitize(name)
	exts := []string{".opus"}
	if plat.AudioMode == "aplay" {
		exts = []string{".wav"}
	}
	dirs := []string{"/data/opt/hermes-agent/sounds", plat.SoundDir}
	if plat.Name == "plum" {
		// На Лайт 2 штатные звуки разложены по common/ и <locale>/, а часть
		// файлов с расширением .wav на самом деле Ogg или MP3 — aplay их не
		// проиграет, поэтому для aplay берём только настоящий RIFF/WAVE.
		dirs = append(dirs, plat.SoundDir+"/common", plat.SoundDir+"/ru-RU")
	}
	for _, dir := range dirs {
		for _, ext := range exts {
			p := fmt.Sprintf("%s/%s%s", dir, base, ext)
			fi, err := os.Stat(p)
			if err != nil || fi.IsDir() {
				continue
			}
			if plat.AudioMode == "aplay" {
				f, err := os.Open(p)
				if err != nil {
					continue
				}
				var hdr [4]byte
				n, _ := f.Read(hdr[:])
				f.Close()
				if n != 4 || string(hdr[:]) != "RIFF" {
					continue
				}
			}
			return p, nil
		}
	}
	return "", fmt.Errorf("нет звука %q для %s", name, plat.Name)
}

// playSound проигрывает штатный звук колонки (opus/wav) без сети.
func playSound(name string) error {
	if name == "" {
		return fmt.Errorf("пустое имя звука")
	}
	path, err := soundPath(name)
	if err != nil {
		return err
	}
	killPlayer()
	var cmd *exec.Cmd
	if plat.AudioMode == "aplay" {
		cmd = aplayCmd(path)
	} else {
		pipeline := fmt.Sprintf(
			"filesrc location=%s ! decodebin ! audioconvert ! audioresample ! alsasink", path)
		cmd = audioCmd(pipeline)
	}
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
		native := playing
		spin := sendspinPlaying()
		engaged := sendspinEngaged()
		if spin {
			playing = true
			if t := sendspinTrack(); t != "" {
				track = t
			}
		}
		_, p, db := currentVolume()
		if p < 0 {
			_, p, db, _ = readVolume()
		}
		lv := levelOf(p, db)
		return reply{OK: true, ID: c.ID, Playing: &playing, NativePlaying: &native,
			SendspinPlaying: &spin, SendspinEngaged: &engaged, Track: track,
			Volume: &p, VolumeDB: &db, Level: &lv}
	case "volume":
		// Всегда читаем микшер заново: Алиса и кнопки меняют его без нас,
		// и кэш на вопрос «какая громкость» сразу после них врал.
		raw, p, db, err := readVolume()
		if err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		volMu.Lock()
		volRaw, volPercent, volDB = raw, p, db
		volMu.Unlock()
		// Level считаем по дБ: проценты у моделей разные (Лайт 2 при
		// уровне 5 показывает 58%), а шкала дБ общая.
		lv := levelOf(p, db)
		return reply{OK: true, ID: c.ID, Volume: &p, VolumeDB: &db, Level: &lv}
	case "volume_set":
		// Снаружи громкость — всегда уровень 0..10 (0 — мьют); уровень
		// переводит в проценты агент, а применяет maind.
		lvl := c.Level
		if lvl < 0 {
			lvl = 0
		}
		if lvl > 10 {
			lvl = 10
		}
		// Единственный путь — нативный VolumeSet в maind (см.
		// quasar_volume.go); при ошибке громкость не меняется.
		ssVolApplyMu.Lock()
		err := setVolumeLevel(lvl)
		ssVolApplyMu.Unlock()
		if err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		_, p, db := currentVolume()
		lv := lvl
		return reply{OK: true, ID: c.ID, Volume: &p, VolumeDB: &db, Level: &lv}
	case "led":
		if err := setLED(c.Name, c.Timeout); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		return reply{OK: true, ID: c.ID}
	case "pause":
		sp := false
		wasSpin := false
		if sendspinEngaged() {
			wasSpin = sendspinPlaying()
			sendspinPause()
			sp = true
		}
		// Натив гасим отдельно. Раньше здесь был return: если Sendspin
		// «занят» (active или localPaused от прошлой паузы), нативный плеер
		// не трогали — и Алиса играла поверх нашего ответа. engaged() не
		// означает «сейчас звучит MA»: localPaused остаётся true и после
		// нашей же паузы.
		wasNative, _ := mediaState()
		if wasNative {
			if err := playerCommand(playerPause); err != nil {
				return reply{ID: c.ID, Err: err.Error(), WasPlaying: wasSpin, Sendspin: &sp}
			}
			if !waitMedia(false, 2*time.Second) {
				return reply{ID: c.ID, Err: "плеер не подтвердил паузу",
					WasPlaying: true, Sendspin: &sp}
			}
			log.Printf("pause: натив остановлен, sendspin=%v", sp)
			return reply{OK: true, ID: c.ID, WasPlaying: true, Sendspin: &sp,
				SendspinPlaying: &wasSpin}
		}
		if !sp {
			// Не отправляем pause в простое: на Лайт 1 этот метод запускает
			// последний нативный источник, и обычная пауза перед ответом
			// включала радио Алисы.
			log.Printf("pause: музыки нет — команду не отправляю")
		}
		return reply{OK: true, ID: c.ID, WasPlaying: wasSpin, Sendspin: &sp,
			SendspinPlaying: &wasSpin}
	case "sendspin_pause":
		// Только наш поток: натив (Алису) не трогаем. Нужно, когда Алиса
		// заговорила сама, а музыка MA ещё доигрывает буфер в aplay.
		sp := false
		if sendspinEngaged() {
			sendspinPause()
			sp = true
		}
		log.Printf("sendspin_pause: музыка остановлена=%v", sp)
		return reply{OK: true, ID: c.ID, Sendspin: &sp}
	case "play":
		// Решение «возвращать ли музыку» принимает сервер: он знает
		// was_playing из ответа на pause. Локальный флаг pausedByUs этого
		// не переживал — после перезапуска агента он сбрасывался, и play
		// молча пропускался: в журнале «play: пропуск, паузу ставили не мы»,
		// а музыка оставалась на паузе (проверено 02.11 19:57).
		//
		// Страховку с TOGGLE_PLAY_PAUSE убрали 07.11. Раньше, если через
		// 2.5+2 с mediaState() показывал «не играет», агент переключал
		// плеер: на колонке с одним источником это поднимало тот же трек,
		// но с Music Assistant TOGGLE включал последний нативный источник —
		// радио вместо выбранного трека. PLAY источник не меняет; если он
		// не поднял поток, возвращать нечего, и вмешиваться нельзя.
		if sendspinEngaged() {
			sendspinResume()
			return reply{OK: true, ID: c.ID}
		}
		if err := playerCommand(playerPlay); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		if !waitMedia(true, 2*time.Second) {
			return reply{ID: c.ID, Err: "плеер не подтвердил воспроизведение"}
		}
		return reply{OK: true, ID: c.ID}
	case "native_pause":
		// Кнопка паузы: maind на то же нажатие сам делает Toggle play/pause
		// и может поднять последний источник Алисы. Глушим только натив;
		// Sendspin и очередь MA не трогаем.
		was, _ := mediaState()
		if was {
			if err := playerCommand(playerPause); err != nil {
				return reply{ID: c.ID, Err: err.Error()}
			}
		}
		return reply{OK: true, ID: c.ID, WasPlaying: was}
	case "native_play":
		// Продолжить нативный плеер, если он на паузе. На idle не трогаем:
		// иначе playerPlay поднимает последнее радио вместо паузы.
		was, _ := mediaState()
		if !was {
			if err := playerCommand(playerPlay); err != nil {
				return reply{ID: c.ID, Err: err.Error()}
			}
		}
		return reply{OK: true, ID: c.ID, WasPlaying: was}
	case "sound":
		// играем в отдельной горутине: синхронный вызов держал ctrlLoop
		// до конца проигрывания, и следующая команда (кольцо) опаздывала
		name := c.Name
		go func() {
			sendspinOverlayBegin()
			defer sendspinOverlayEnd()
			if err := playSound(name); err != nil {
				log.Printf("sound %s: %v", name, err)
			}
		}()
		return reply{OK: true, ID: c.ID}
	case "stop":
		// Перебивание голосом: сервер уже отправил несколько кусков, и без
		// очистки очереди ответ доигрывал бы после начала нового хода.
		if sendspinEngaged() {
			sendspinPause()
		}
		n := stopPlayback()
		log.Printf("stop: очередь очищена (%d кусков), звук остановлен", n)
		return reply{OK: true, ID: c.ID}
	case "say":
		data, err := base64.StdEncoding.DecodeString(c.Audio)
		if err != nil {
			return reply{ID: c.ID, Err: "base64: " + err.Error()}
		}
		sendspinOverlayBegin()
		defer sendspinOverlayEnd()
		if err := playAudio(c.ID, data, c.Volume); err != nil {
			return reply{ID: c.ID, Err: err.Error()}
		}
		return reply{OK: true, ID: c.ID}
	default:
		return reply{ID: c.ID, Err: "неизвестная команда: " + c.Cmd}
	}
}
