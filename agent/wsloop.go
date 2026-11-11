// Транспорт агента: одно WebSocket-соединение вместо TCP-аудио + TCP-управления.
//
// Вверх идут бинарные кадры с PCM (микрофон), вниз — бинарные кадры со звуком
// ответа и текстовые JSON с командами устройства. Один канал вместо двух:
// меньше состояний, есть ping/pong и понятный close.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"sync"
	"sync/atomic"
	"time"
)

var (
	serverURL   = flag.String("server", "", "адрес хаба ws://…; пусто — поиск в локальной сети")
	audioVolume = flag.Float64("volume", 4.0, "громкость ответов")
)

// discoveryMode включается, когда адрес хаба не задан вручную.
var discoveryMode bool

// hubAddr возвращает адрес хаба: найденный по broadcast или заданный флагом.
// Ошибка поиска — не повод стартовать вслепую: без адреса агент не работает.
func hubAddr() (string, error) {
	if !discoveryMode {
		return *serverURL, nil
	}
	addr, err := discoverHub(2 * time.Second)
	if err != nil {
		return "", err
	}
	if addr != *serverURL {
		log.Printf("поиск хаба: %s", addr)
		*serverURL = addr
	}
	return addr, nil
}

type audioItem struct {
	data   []byte
	volume float64
}

var (
	playQ     = make(chan audioItem, 8)
	volumeMu  sync.Mutex
	lastKnown = *audioVolume
	// playDoneNote — сигнал от плеера к сессии: звук доиграл, можно сообщить.
	playDoneNote = make(chan struct{}, 1)
	// interruptNote — кнопка паузы → цикл WebSocket-сессии. Отправляем
	// interrupt по уже открытому соединению: хаб не должен угадывать колонку
	// по имени или IP.
	interruptNote = make(chan bool, 1)
)

// audioPlayer проигрывает куски по очереди: команды и ping при этом не блокируются.
// Когда очередь опустела и последний кусок доиграл, сообщаем серверу play_done:
// только он знает момент конца звука, сокет уходит вперёд.
func audioPlayer() {
	for it := range playQ {
		id := fmt.Sprintf("ws-%d", time.Now().UnixNano())
		if err := playAudio(id, it.data, it.volume); err != nil {
			log.Printf("ws: звук не проигран: %v", err)
		}
		if len(playQ) > 0 {
			continue // следом уже лежит следующий кусок — конец ещё не наступил
		}
		notifyPlayDone()
	}
}

// notifyPlayDone неблокирующе просит цикл сессии отправить событие.
func notifyPlayDone() {
	select {
	case playDoneNote <- struct{}{}:
	default:
	}
}

func wsHello() []byte {
	_, pct, db := currentVolume()
	if pct < 0 {
		// Ещё не было опроса микшера: без этого hello уходил с уровнем 0,
		// и хаб на старте показывал «звук выключен».
		if r, p, d, err := readVolume(); err == nil {
			pct, db = p, d
			volMu.Lock()
			volRaw, volPercent, volDB = r, p, d
			volMu.Unlock()
		}
	}
	msg := map[string]any{
		"t":           "hello",
		"role":        "station",
		"mac":         primaryMAC(),
		"agc":         true,
		"write_audio": true,
		"volume":      pct,
		"volume_db":   db,
		"level":       levelOf(pct, db),
	}
	b, _ := json.Marshal(msg)
	return b
}

// hubOnline — есть ли живое соединение с хабом. Если хаба нет, кнопку
// обслуживаем локально: иначе нажатие уходит в никуда и колонка молчит.
var hubOnline atomic.Bool

// wsSession — одно соединение: досыл буфера, запись микрофона и чтение команд.
// Возвращает ошибку, по которой вызывающий решает, когда переподключаться.
func wsSession() error {
	addr, err := hubAddr()
	if err != nil {
		return fmt.Errorf("поиск хаба: %w", err)
	}
	c, err := wsDial(addr, 5*time.Second)
	if err != nil {
		return err
	}
	defer c.Close()

	if err := c.WriteMessage(opText, wsHello()); err != nil {
		return fmt.Errorf("hello: %w", err)
	}
	// Нажатие, сделанное до подключения, не должно оборвать следующий ответ:
	// старый сигнал сбрасываем при входе в новую сессию.
drainInterrupt:
	for {
		select {
		case <-interruptNote:
		default:
			break drainInterrupt
		}
	}
	log.Printf("ws: подключено к %s, имя — по MAC", addr)
	hubOnline.Store(true)
	defer hubOnline.Store(false)

	stop := make(chan struct{})
	go c.PingLoop(20*time.Second, stop)
	defer close(stop)

	readErr := make(chan error, 1)
	go func() {
		for {
			op, payload, err := c.ReadMessage()
			if err != nil {
				readErr <- err
				return
			}
			switch op {
			case opBin:
				volumeMu.Lock()
				vol := lastKnown
				volumeMu.Unlock()
				select {
				case playQ <- audioItem{payload, vol}:
				default:
					log.Printf("ws: очередь звука переполнена, кусок %d Б отброшен", len(payload))
				}
			case opText:
				handleTextFrame(c, payload)
			}
		}
	}()

	// после обрыва досылаем то, что накопилось в кольцевом буфере
	if buf := pendingHolder.take(); len(buf) > 0 {
		if err := c.WriteMessage(opBin, buf); err != nil {
			pendingHolder.push(buf)
			return fmt.Errorf("досыл буфера: %w", err)
		}
		log.Printf("ws: дослано из буфера %d КБ", len(buf)/1024)
	}

	for {
		select {
		case <-playDoneNote:
			// Последний кусок доиграл: сервер по этому событию гасит кольцо
			// и возвращает музыку. Пишем из цикла сессии — запись в сокет
			// должна идти из одного места, иначе кадры перемешиваются.
			b, _ := json.Marshal(map[string]any{"t": "play_done"})
			if err := c.WriteMessage(opText, b); err != nil {
				return fmt.Errorf("play_done: %w", err)
			}
		case ev := <-volumeNote:
			// Громкость поменяли голосом Алисы, кнопкой или нашей командой:
			// сообщаем хабу, чтобы HA видел настоящее значение. `from_ma`
			// говорит хабу не отправлять это же значение обратно в MA.
			_, pct, db := currentVolume()
			b, _ := json.Marshal(map[string]any{
				"t": "volume", "percent": pct, "db": db, "level": ev.level,
				"from_ma": ev.fromMA,
			})
			if err := c.WriteMessage(opText, b); err != nil {
				return fmt.Errorf("volume: %w", err)
			}
		case long := <-interruptNote:
			// Кнопка паузы: прерываем ход LLM в том же соединении. Никаких
			// HTTP-запросов, имени или IP — хаб уже знает этот Conn.
			// long=true — длинное нажатие: хаб переключит на Алису.
			msg := map[string]any{"t": "interrupt"}
			if long {
				msg["long"] = true
			}
			b, _ := json.Marshal(msg)
			if err := c.WriteMessage(opText, b); err != nil {
				return fmt.Errorf("interrupt: %w", err)
			}
			log.Printf("interrupt: отправлен по WebSocket (длинное=%v)", long)
		case err := <-readErr:
			return fmt.Errorf("чтение: %w", err)
		default:
		}
		b := pendingHolder.take()
		if b == nil {
			time.Sleep(10 * time.Millisecond)
			continue
		}
		st.chunk(len(b))
		if err := c.WriteMessage(opBin, b); err != nil {
			pendingHolder.push(b)
			st.mu.Lock()
			st.sendErr++
			st.mu.Unlock()
			return fmt.Errorf("запись: %w", err)
		}
	}
}

func handleTextFrame(c *wsConn, payload []byte) {
	var msg map[string]any
	if err := json.Unmarshal(payload, &msg); err != nil {
		return
	}
	switch msg["t"] {
	case "cmd":
		raw, _ := json.Marshal(msg)
		var cmd command
		if err := json.Unmarshal(raw, &cmd); err != nil {
			log.Printf("ws cmd: разбор: %v", err)
			return
		}
		// В WebSocket-режиме звук приходит отдельным бинарным кадром сразу
		// после команды say: здесь только запоминаем громкость и подтверждаем.
		if cmd.Cmd == "say" && cmd.Audio == "" {
			if cmd.Volume > 0 {
				volumeMu.Lock()
				lastKnown = cmd.Volume
				volumeMu.Unlock()
			}
			sendReply(c, reply{OK: true, ID: cmd.ID})
			return
		}
		if *verbose {
			log.Printf("ws cmd: %s id=%s name=%s", cmd.Cmd, cmd.ID, cmd.Name)
		}
		r := handleCommand(cmd)
		if r.Err != "" {
			log.Printf("ws cmd: %s -> %s", cmd.Cmd, r.Err)
		}
		sendReply(c, r)
	case "ping":
		_ = c.WriteMessage(opText, []byte(`{"t":"pong"}`))
	case "hello_ok":
		log.Printf("ws: сервер принял hello (%v)", msg["name"])
	case "sendspin":
		var m struct {
			Config struct {
				URL      string `json:"url"`
				Name     string `json:"name"`
				StateDir string `json:"state_dir"`
				Output   string `json:"output"`
			} `json:"config"`
		}
		if err := json.Unmarshal(payload, &m); err != nil {
			log.Printf("ws: sendspin config: %v", err)
			break
		}
		startSendspin(ssConfig{
			URL:      m.Config.URL,
			Name:     m.Config.Name,
			StateDir: m.Config.StateDir,
			Output:   m.Config.Output,
		})
		log.Printf("ws: sendspin config получен: url=%s name=%s", m.Config.URL, m.Config.Name)
	}
}

func sendReply(c *wsConn, r reply) {
	m := map[string]any{
		"t": "cmd_result", "id": r.ID, "ok": r.OK, "err": r.Err,
		"was_playing": r.WasPlaying, "sendspin": r.Sendspin,
		"playing": r.Playing, "native_playing": r.NativePlaying,
		"sendspin_playing": r.SendspinPlaying, "sendspin_engaged": r.SendspinEngaged,
		"track": r.Track,
	}
	if r.Volume != nil {
		m["volume"] = *r.Volume
	}
	if r.VolumeDB != nil {
		m["volume_db"] = *r.VolumeDB
	}
	if r.Level != nil {
		m["level"] = *r.Level
	}
	b, _ := json.Marshal(m)
	_ = c.WriteMessage(opText, b)
}

func wsLoop() {
	backoff := backoffMin
	for {
		err := wsSession()
		if err != nil {
			log.Printf("ws: %v — повтор через %v", err, backoff)
			clearLedOnDisconnect()
		} else {
			log.Printf("ws: соединение закрыто — повтор через %v", backoff)
			clearLedOnDisconnect()
		}
		bumpReconnect()
		time.Sleep(backoff)
		backoff = nextBackoff(backoff)
	}
}
