package main

// Sendspin-клиент для Music Assistant.
//
// Подключение инициирует клиент (ws://MA:8927/sendspin), mDNS не нужен.
// Роль только player@v1, формат только PCM 48/16/2 — поэтому нет ни FLAC,
// ни Opus, ни декодера: MA отдаёт готовый PCM, агент пишет его в aplay.
//
// Арбитраж с голосом: на время TTS/ответа sendspinMuteStream() глушит
// PCM (duck=0), но aplay продолжает работать. Раньше поток ставился на
// паузу, и после ответа MA уезжал вперёд: слышался рывок и терялись
// куски. Полная пауза (sendspinPause) остаётся для кнопки и остановки.

import (
	"encoding/binary"
	"encoding/json"
	"fmt"
	"log"
	"sync"
	"time"
)

type ssConfig struct {
	URL      string
	Name     string
	StateDir string
	Output   string
}

type ssServerTime struct {
	ClientTransmitted int64 `json:"client_transmitted"`
	ServerReceived    int64 `json:"server_received"`
	ServerTransmitted int64 `json:"server_transmitted"`
}

type ssChunk struct {
	pcm    []byte
	playAt int64 // локальное время, мкс; 0 — играть сразу
	gen    uint64
}

type ssClient struct {
	cfg ssConfig

	conn *wsConn
	send interface {
		Encrypt(out, ad, plaintext []byte) ([]byte, error)
	}
	recv interface {
		Decrypt(out, ad, ciphertext []byte) ([]byte, error)
	}
	encMu sync.Mutex

	mu          sync.Mutex
	volume      int
	volumeSet   bool
	muted       bool
	available   bool
	localPaused bool
	stateSent   bool
	active      bool
	audioGen    uint64
	track       string

	audio   *ssAudio
	filter  *ssTimeFilter
	audioCh chan ssChunk

	timeCh chan ssServerTime
	done   chan struct{}
	once   sync.Once
}

var ssEpoch = time.Now()

func ssNowUs() int64 { return time.Since(ssEpoch).Microseconds() }

var (
	ssGlobalMu sync.Mutex
	ssGlobal   *ssClient
	ssStarted  bool
)

// startSendspin запускает клиента по конфигу, который пришёл от хаба.
// Настройки больше не читаются из env: единственный источник — hub.json.
func startSendspin(cfg ssConfig) {
	if cfg.URL == "" {
		log.Printf("sendspin: хаб не передал URL — плеер отключён")
		return
	}
	if cfg.Name == "" {
		cfg.Name = "Колонка (Hermes)"
	}
	if cfg.StateDir == "" {
		cfg.StateDir = "/data/opt/hermes-station/sendspin"
	}
	if cfg.Output == "" {
		cfg.Output = "default"
	}

	ssGlobalMu.Lock()
	if ssStarted {
		ssGlobalMu.Unlock()
		return
	}
	ssStarted = true
	ssGlobalMu.Unlock()

	go runSendspinLoop(cfg)
}

func stationVolumeMA() int {
	if _, pct, _, err := readVolume(); err == nil {
		return pct
	}
	return 100
}

func runSendspinLoop(cfg ssConfig) {
	delay := time.Second
	for {
		started := time.Now()
		c := &ssClient{
			cfg:       cfg,
			volume:    stationVolumeMA(),
			volumeSet: true,
			available: true,
			audio:     newSSAudio(cfg.Output),
			filter:    newSSTimeFilter(),
			audioCh:   make(chan ssChunk, 96),
			timeCh:    make(chan ssServerTime, 8),
			done:      make(chan struct{}),
		}

		err := c.run()
		c.stop()
		ssGlobalMu.Lock()
		if ssGlobal == c {
			ssGlobal = nil
		}
		ssGlobalMu.Unlock()
		if time.Since(started) > time.Minute {
			delay = time.Second
		}
		log.Printf("sendspin: %v — переподключение через %s", err, delay)
		time.Sleep(delay)
		if delay < 30*time.Second {
			delay *= 2
		}
	}
}

func (c *ssClient) stop() {
	c.once.Do(func() { close(c.done) })
	c.audio.Stop()
}

func (c *ssClient) run() error {
	id, err := ssLoadIdentity(c.cfg.StateDir)
	if err != nil {
		return err
	}
	conn, err := wsDial(c.cfg.URL, 10*time.Second)
	if err != nil {
		return fmt.Errorf("подключение: %w", err)
	}
	c.conn = conn
	defer conn.Close()

	send, recv, serverID, err := ssHandshake(conn, id)
	if err != nil {
		return fmt.Errorf("рукопожатие: %w", err)
	}
	c.send, c.recv = send, recv
	log.Printf("sendspin: Noise OK, server=%s, client=%s", serverID, id.clientID())

	// server/hello → client/hello → server/activate.
	env, err := c.readEnvelope()
	if err != nil {
		return fmt.Errorf("server/hello: %w", err)
	}
	if env.Type != "server/hello" {
		return fmt.Errorf("ожидался server/hello, получен %q", env.Type)
	}
	log.Printf("sendspin: server/hello получен")
	if err := c.sendClientHello(id); err != nil {
		return err
	}
	log.Printf("sendspin: client/hello отправлен")
	ssGlobalMu.Lock()
	ssGlobal = c
	ssGlobalMu.Unlock()
	go c.conn.PingLoop(15*time.Second, c.done)
	go c.audioPump()
	for {
		env, err = c.readEnvelope()
		if err != nil {
			return fmt.Errorf("server/activate: %w", err)
		}
		if env.Type == "server/activate" {
			break
		}
		// Поток может прийти раньше activate при переподключении во время
		// воспроизведения: обрабатываем, иначе первые чанки теряются.
		c.handleEnvelope(env)
	}

	go c.timeSync()
	return c.readLoop()
}

func (c *ssClient) sendClientHello(id ssIdentity) error {
	return c.sendJSON("client/hello", c.clientHelloPayload(id))
}

func (c *ssClient) clientHelloPayload(id ssIdentity) map[string]any {
	return map[string]any{
		"name":            c.cfg.Name,
		"supported_roles": []string{"player@v1"},
		"client_id":       id.clientID(),
		"version":         ssProtocolVersion,
		"supported_pair_methods": map[string]any{
			"pairing_psk": map[string]any{"locations": []string{"device"}},
		},
		"player@v1_support": map[string]any{
			"supported_formats": []map[string]any{{
				"codec": "pcm", "channels": ssChannels,
				"sample_rate": ssSampleRate, "bit_depth": ssBitDepth,
			}},
			"buffer_capacity": 1 << 20,
		},
		"unpaired_access": map[string]any{"enabled": true},
	}
}

func (c *ssClient) sendJSON(typ string, payload any) error {
	if c.send == nil || c.conn == nil {
		return nil
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	body, err := json.Marshal(ssEnvelope{Type: typ, Payload: raw})
	if err != nil {
		return err
	}
	pt := append([]byte{ssMsgJSON}, body...)
	// Encrypt и запись — под одним мьютексом: иначе два писателя могут
	// зашифровать seq N и N+1, а записать в обратном порядке, и сервер
	// оборвёт соединение на неверном nonce.
	c.encMu.Lock()
	defer c.encMu.Unlock()
	ct, err := c.send.Encrypt(nil, nil, pt)
	if err != nil {
		return err
	}
	return c.conn.WriteMessage(opBin, ct)
}

func (c *ssClient) readEnvelope() (ssEnvelope, error) {
	_ = c.conn.SetReadDeadline(time.Now().Add(30 * time.Second))
	op, data, err := c.conn.ReadMessage()
	if err != nil {
		return ssEnvelope{}, err
	}
	if op != opBin {
		return ssEnvelope{}, fmt.Errorf("ожидался бинарный кадр, получен opcode %d", op)
	}
	pt, err := c.recv.Decrypt(nil, nil, data)
	if err != nil {
		return ssEnvelope{}, fmt.Errorf("decrypt: %w", err)
	}
	if len(pt) == 0 || pt[0] != ssMsgJSON {
		return ssEnvelope{}, fmt.Errorf("ожидался JSON (0), получен тип %d", pt[0])
	}
	var env ssEnvelope
	if err := json.Unmarshal(pt[1:], &env); err != nil {
		return ssEnvelope{}, err
	}
	return env, nil
}

func (c *ssClient) readLoop() error {
	for {
		_ = c.conn.SetReadDeadline(time.Now().Add(60 * time.Second))
		op, data, err := c.conn.ReadMessage()
		if err != nil {
			return err
		}
		if op != opBin {
			continue
		}
		pt, err := c.recv.Decrypt(nil, nil, data)
		if err != nil {
			return fmt.Errorf("decrypt: %w", err)
		}
		if len(pt) == 0 {
			continue
		}
		switch pt[0] {
		case ssMsgJSON:
			var env ssEnvelope
			if err := json.Unmarshal(pt[1:], &env); err != nil {
				log.Printf("sendspin: JSON: %v", err)
				continue
			}
			c.handleEnvelope(env)
		case ssMsgAudio:
			c.handleAudio(pt)
		case 1:
			// Фрагментация: для PCM 48/16/2 чанк ≤ 150 мс (28.8 КБ),
			// это меньше лимита Noise (65 КБ), поэтому не встречается.
		}
	}
}

func (c *ssClient) handleEnvelope(env ssEnvelope) {
	switch env.Type {
	case "server/time":
		var st ssServerTime
		if err := json.Unmarshal(env.Payload, &st); err != nil {
			return
		}
		select {
		case c.timeCh <- st:
		default:
		}
	case "stream/start":
		var p struct {
			Player struct {
				Codec      string `json:"codec"`
				SampleRate int    `json:"sample_rate"`
				Channels   int    `json:"channels"`
				BitDepth   int    `json:"bit_depth"`
			} `json:"player"`
		}
		_ = json.Unmarshal(env.Payload, &p)
		if p.Player.Codec != "pcm" || p.Player.SampleRate != ssSampleRate ||
			p.Player.Channels != ssChannels || p.Player.BitDepth != ssBitDepth {
			log.Printf("sendspin: неожиданный формат %s %d/%d/%d — поток пропущен",
				p.Player.Codec, p.Player.SampleRate, p.Player.Channels, p.Player.BitDepth)
			return
		}
		c.mu.Lock()
		c.active = true
		paused := c.localPaused
		c.mu.Unlock()
		if paused {
			return
		}
		pauseNativeIfPlaying()
		if err := c.audio.Start(); err != nil {
			log.Printf("sendspin: aplay: %v", err)
		}
	case "stream/clear":
		c.flushAudio()
		c.mu.Lock()
		paused := c.localPaused
		c.mu.Unlock()
		c.audio.Stop()
		if !paused {
			if err := c.audio.Start(); err != nil {
				log.Printf("sendspin: aplay: %v", err)
			}
		}
	case "stream/end":
		c.mu.Lock()
		c.active = false
		c.mu.Unlock()
		c.audio.Stop()
		c.flushAudio()
	case "group/update":
		var p struct {
			PlaybackState string `json:"playback_state"`
		}
		_ = json.Unmarshal(env.Payload, &p)
		switch p.PlaybackState {
		case "playing":
			c.mu.Lock()
			paused := c.localPaused
			active := c.active
			c.mu.Unlock()
			if paused || !active {
				return
			}
			pauseNativeIfPlaying()
			if err := c.audio.Start(); err != nil {
				log.Printf("sendspin: aplay: %v", err)
			}
		case "paused", "stopped":
			c.mu.Lock()
			local := c.localPaused
			c.mu.Unlock()
			if !local {
				c.audio.Stop()
			}
		}
	case "server/command":
		var p struct {
			Player struct {
				Command string `json:"command"`
				Volume  *int   `json:"volume"`
				Mute    *bool  `json:"mute"`
			} `json:"player"`
		}
		_ = json.Unmarshal(env.Payload, &p)
		c.mu.Lock()
		switch p.Player.Command {
		case "volume":
			if p.Player.Volume != nil {
				c.volume = *p.Player.Volume
				c.volumeSet = true
			}
		case "mute":
			if p.Player.Mute != nil {
				c.muted = *p.Player.Mute
			}
		}
		vol, muted := c.volume, c.muted
		c.mu.Unlock()
		// Нативная громкость maind применяется до 1.7 с; не блокируем readLoop.
		go c.applyMAVolume(vol, muted)
		_ = c.sendState()
	case "server/state":
		var p struct {
			Metadata struct {
				Title  string `json:"title"`
				Artist string `json:"artist"`
			} `json:"metadata"`
		}
		_ = json.Unmarshal(env.Payload, &p)
		if p.Metadata.Title != "" {
			track := p.Metadata.Title
			if p.Metadata.Artist != "" {
				track = p.Metadata.Artist + " — " + track
			}
			c.mu.Lock()
			c.track = track
			c.mu.Unlock()
		}
	case "server/hello", "server/activate":
		log.Printf("sendspin: %s", env.Type)
	}
}

func (c *ssClient) handleAudio(pt []byte) {
	if len(pt) < 13 {
		return
	}
	c.mu.Lock()
	paused, active, gen := c.localPaused, c.active, c.audioGen
	c.mu.Unlock()
	if paused || !active {
		return
	}
	// timestamp_us (big-endian int64) + send_ahead (uint32) + PCM.
	serverTS := int64(binary.BigEndian.Uint64(pt[0:8]))
	playAt := int64(0)
	if c.filter.Converged() {
		playAt = c.filter.ClientTime(serverTS)
	}
	chunk := ssChunk{pcm: append([]byte(nil), pt[13:]...), playAt: playAt, gen: gen}
	select {
	case c.audioCh <- chunk:
	default:
		log.Printf("sendspin: очередь аудио переполнена, кусок отброшен")
	}
}

// audioPump пишет в aplay отдельно от readLoop: запись в pipe может
// блокироваться, а на это время не должны вставать time sync и команды.
func (c *ssClient) audioPump() {
	for {
		select {
		case <-c.done:
			return
		case ch := <-c.audioCh:
			c.mu.Lock()
			paused, active, gen := c.localPaused, c.active, c.audioGen
			c.mu.Unlock()
			if paused || !active || ch.gen != gen {
				continue
			}
			// Поток мог начаться заново без server/activate (MA продолжил
			// после нашей паузы), и тогда aplay не запущен: куски уходят
			// в никуда, очередь переполняется. Поднимаем его здесь же, но
			// расписание playAt не ломаем: иначе музыка стартует раньше
			// времени MA и слышен скачок на 1-2 с.
			if !c.audio.Running() {
				if err := c.audio.Start(); err != nil {
					log.Printf("sendspin: aplay: %v", err)
					continue
				}
			}
			if ch.playAt > 0 {
				delay := time.Duration(ch.playAt-ssNowUs())*time.Microsecond - 50*time.Millisecond
				if delay > 0 {
					t := time.NewTimer(delay)
					select {
					case <-t.C:
					case <-c.done:
						t.Stop()
						return
					}
				}
				c.mu.Lock()
				paused, active, gen = c.localPaused, c.active, c.audioGen
				c.mu.Unlock()
				if paused || !active || ch.gen != gen {
					continue
				}
				if ssNowUs()-ch.playAt > 250_000 {
					continue // опоздал больше чем на 250 мс
				}
			}
			c.audio.Write(ch.pcm)
		}
	}
}

func (c *ssClient) timeSync() {
	for {
		for i := 0; i < 8; i++ {
			t1 := ssNowUs()
			if err := c.sendJSON("client/time", map[string]any{"client_transmitted": t1}); err != nil {
				return
			}
			st, ok := c.waitMeasurement(t1)
			if !ok {
				i = 8
				continue
			}
			t4 := ssNowUs()
			measurement, maxError := ssTimeMeasurement(t1, st.ServerReceived, st.ServerTransmitted, t4)
			c.filter.Update(measurement, maxError, t4)
		}
		if c.filter.Converged() && !c.stateSent {
			_ = c.sendState()
		}
		select {
		case <-time.After(5 * time.Second):
		case <-c.done:
			return
		}
	}
}

// waitMeasurement ждёт ответ именно на свой client_transmitted: запоздавшие
// ответы от прошлых burst'ов иначе сдвигают фильтр на секунды.
func (c *ssClient) waitMeasurement(t1 int64) (ssServerTime, bool) {
	t := time.NewTimer(2 * time.Second)
	defer t.Stop()
	for {
		select {
		case st := <-c.timeCh:
			if st.ClientTransmitted != t1 {
				continue
			}
			return st, true
		case <-t.C:
			return ssServerTime{}, false
		case <-c.done:
			return ssServerTime{}, false
		}
	}
}

func (c *ssClient) sendState() error {
	c.mu.Lock()
	// Первый state — это фактическая громкость колонки, а не дефолтные 100:
	// иначе MA показывает 100, хотя на устройстве 4/10.
	if !c.volumeSet {
		if _, pct, _, err := readVolume(); err == nil {
			c.volume = pct
		}
		c.volumeSet = true
	}
	payload := map[string]any{
		"available": c.available,
		"player": map[string]any{
			"volume":                c.volume,
			"muted":                 c.muted,
			"output_delay_ms":       0,
			"required_lead_time_ms": 1000,
			"min_buffer_ms":         500,
			"supported_commands":    []string{"volume", "mute"},
			"format": map[string]any{
				"codec": "pcm", "channels": ssChannels,
				"sample_rate": ssSampleRate, "bit_depth": ssBitDepth,
			},
		},
	}
	c.stateSent = true
	c.mu.Unlock()
	return c.sendJSON("client/state", payload)
}

// applyMAVolume применяет громкость MA к самой колонке, а не только к PCM.
// Нативный VolumeSet на части прошивок квантует и не даёт точный процент,
// поэтому для ползунка MA пишем фактический процент в ALSA. Если это не
// удалось — остаётся программное масштабирование.
var ssVolApplyMu sync.Mutex

func (c *ssClient) applyMAVolume(volume int, muted bool) {
	ssVolApplyMu.Lock()
	defer ssVolApplyMu.Unlock()
	if !muted {
		if err := setVolumePercentFromMA(volume); err == nil {
			c.audio.SetVolume(100, false)
			return
		} else {
			log.Printf("sendspin: громкость колонки не применена: %v", err)
		}
	}
	c.audio.SetVolume(volume, muted)
}

// setVolumeFromStation обновляет MA, когда громкость изменили кнопкой или
// Алисой. PCM не трогаем: колонка уже на нужном уровне.
func (c *ssClient) setVolumeFromStation(percent int) {
	c.mu.Lock()
	c.volume = percent
	c.volumeSet = true
	muted := c.muted
	c.mu.Unlock()
	c.audio.SetVolume(100, muted)
	_ = c.sendState()
}

func (c *ssClient) pause() {
	c.mu.Lock()
	if c.localPaused || (!c.active && !c.audio.Running()) {
		c.mu.Unlock()
		return
	}
	// available остаётся true: это прерываемая пауза под TTS, а не уход
	// из группы. available:false заставил бы MA остановить поток и не
	// вернуть его автоматически.
	c.localPaused = true
	c.mu.Unlock()
	// Пауза должна быть мгновенной: Stop() доигрывал буфер до секунды,
	// и музыка звучала уже после сигнала «слушаю».
	c.audio.SetPaused(true)
	c.flushAudio()
}

func (c *ssClient) resume() {
	c.mu.Lock()
	wasPaused := c.localPaused
	active := c.active
	c.localPaused = false
	c.mu.Unlock()
	c.audio.SetPaused(false)
	// Start нужен и тогда, когда флаг паузы уже снят, но aplay не работает:
	// иначе поток идёт в пустоту, а очередь переполняется.
	if active && (wasPaused || !c.audio.Running()) {
		if err := c.audio.Start(); err != nil {
			log.Printf("sendspin: aplay: %v", err)
		}
	}
}

// flushAudio выбрасывает уже принятые, но ещё не записанные куски.
func (c *ssClient) flushAudio() {
	c.mu.Lock()
	c.audioGen++
	c.mu.Unlock()
	for {
		select {
		case <-c.audioCh:
		default:
			return
		}
	}
}

func sendspinPause() {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c != nil {
		c.pause()
	}
}

// sendspinVolumeFromStation — громкость изменили кнопкой или Алисой:
// сообщаем MA, чтобы его ползунок не расходился с колонкой.
func sendspinVolumeFromStation(percent int) {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c != nil {
		c.setVolumeFromStation(percent)
	}
}

// sendspinMuteStream/Unmute — тишина на время TTS без остановки потока.
// aplay продолжает писать нули, MA не уезжает вперёд, и после ответа
// музыка возвращается без рывка и перезапуска aplay.
func sendspinMuteStream() {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c != nil {
		c.audio.SetDuck(0)
	}
}

func sendspinUnmuteStream() {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c != nil {
		c.audio.SetDuck(1)
	}
}

func sendspinResume() {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c != nil {
		c.resume()
	}
}

// sendspinReady — рукопожатие прошло и соединение живое.
func sendspinReady() bool {
	ssGlobalMu.Lock()
	defer ssGlobalMu.Unlock()
	return ssGlobal != nil && ssGlobal.conn != nil && ssGlobal.send != nil
}

// sendspinEngaged — MA уже начал поток (или мы его локально приостановили).
// Только в этом случае голосовые pause/play должны управлять Sendspin, а не
// нативным mediad: иначе Яндекс-радио перестаёт слушаться при живом, но
// простаивающем соединении.
func sendspinEngaged() bool {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.active || c.localPaused
}

var (
	ssOverlayMu      sync.Mutex
	ssOverlayN       int
	nativePausedByUs bool
)

// sendspinOverlayBegin/End — общий счётчик для say и sound: музыка
// возвращается только после последнего оверлея, а не после первого.
func sendspinOverlayBegin() {
	ssOverlayMu.Lock()
	ssOverlayN++
	first := ssOverlayN == 1
	ssOverlayMu.Unlock()
	if !first {
		return
	}
	sendspinMuteStream()
	if playing, _ := mediaState(); playing {
		if err := playerCommand(playerPause); err == nil {
			ssOverlayMu.Lock()
			nativePausedByUs = true
			ssOverlayMu.Unlock()
		}
	}
}

func sendspinOverlayEnd() {
	ssOverlayMu.Lock()
	ssOverlayN--
	last := ssOverlayN == 0
	if ssOverlayN < 0 {
		ssOverlayN = 0
	}
	resumeNative := last && nativePausedByUs
	if last {
		nativePausedByUs = false
	}
	ssOverlayMu.Unlock()
	if !last {
		return
	}
	sendspinUnmuteStream()
	if resumeNative {
		_ = playerCommand(playerPlay)
	}
}

// sendspinPlaying — aplay запущен и поток не поставлен на паузу нами.
func sendspinPlaying() bool {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c == nil {
		return false
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.audio.Running() && !c.localPaused
}

func sendspinTrack() string {
	ssGlobalMu.Lock()
	c := ssGlobal
	ssGlobalMu.Unlock()
	if c == nil {
		return ""
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.track
}

// pauseNativeIfPlaying гасит штатный mediad, если он играет: два источника
// (Yandex и MA) в одном dmix дают наложение.
func pauseNativeIfPlaying() {
	if playing, _ := mediaState(); playing {
		if err := playerCommand(playerPause); err == nil {
			log.Printf("sendspin: нативный mediad поставлен на паузу")
		}
	}
}

// Используется в тестах и для диагностики.
func sendspinConnected() bool {
	ssGlobalMu.Lock()
	defer ssGlobalMu.Unlock()
	return ssGlobal != nil
}
