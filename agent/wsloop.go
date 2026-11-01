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
	"net/url"
	"os"
	"strings"
	"sync"
	"time"
)

var (
	serverURL   = flag.String("server", "ws://127.0.0.1:8796/ws", "адрес голосового хаба (ws://)")
	deviceName  = flag.String("name", defaultDeviceName(), "имя устройства в хабе")
	threshold   = flag.Float64("threshold", 0.95, "порог ключевого слова на сервере")
	detGain     = flag.Float64("gain", 10.0, "усиление перед детектором")
	audioVolume = flag.Float64("volume", 4.0, "громкость ответов")
)

// hubHTTP превращает адрес хаба ws://host:port/ws в http://host:port/<path>.
func hubHTTP(path string) string {
	u, err := url.Parse(*serverURL)
	if err != nil {
		return "http://127.0.0.1:8796" + path
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += ":80"
	}
	return "http://" + host + path
}

func defaultDeviceName() string {
	h, err := os.Hostname()
	if err != nil || h == "" {
		return "station"
	}
	return h
}

type audioItem struct {
	data   []byte
	volume float64
}

var (
	playQ     = make(chan audioItem, 8)
	volumeMu  sync.Mutex
	lastKnown = *audioVolume
)

// audioPlayer проигрывает куски по очереди: команды и ping при этом не блокируются.
func audioPlayer() {
	for it := range playQ {
		id := fmt.Sprintf("ws-%d", time.Now().UnixNano())
		if err := playAudio(id, it.data, it.volume); err != nil {
			log.Printf("ws: звук не проигран: %v", err)
		}
	}
}

func wsHello() []byte {
	msg := map[string]any{
		"t":     "hello",
		"name":  *deviceName,
		"role":  "station",
		"threshold": *threshold,
		"gain":      *detGain,
		"agc":       true,
		"write_audio": true,
	}
	b, _ := json.Marshal(msg)
	return b
}

// wsSession — одно соединение: досыл буфера, запись микрофона и чтение команд.
// Возвращает ошибку, по которой вызывающий решает, когда переподключаться.
func wsSession() error {
	c, err := wsDial(*serverURL, 5*time.Second)
	if err != nil {
		return err
	}
	defer c.Close()

	if err := c.WriteMessage(opText, wsHello()); err != nil {
		return fmt.Errorf("hello: %w", err)
	}
	log.Printf("ws: подключено к %s как %s", *serverURL, *deviceName)

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
	}
}

func sendReply(c *wsConn, r reply) {
	b, _ := json.Marshal(map[string]any{
		"t": "cmd_result", "id": r.ID, "ok": r.OK, "err": r.Err,
		"was_playing": r.WasPlaying, "playing": r.Playing, "track": r.Track,
	})
	_ = c.WriteMessage(opText, b)
}

func wsLoop() {
	backoff := backoffMin
	for {
		err := wsSession()
		if err != nil {
			log.Printf("ws: %v — повтор через %v", err, backoff)
		} else {
			log.Printf("ws: соединение закрыто — повтор через %v", backoff)
		}
		bumpReconnect()
		time.Sleep(backoff)
		backoff = nextBackoff(backoff)
	}
}
