// hermes-station-agent — мост между микрофоном Яндекс Станции и сервером Hermes.
//
// Пилотный режим: читает канал vqe_0 из локального IPC maind (127.0.0.1:9850),
// держит исходящее TCP-соединение к серверу и отправляет PCM 16 кГц моно s16le.
// Управление (звук, LED, пауза музыки) в этой версии НЕ выполняется.
//
// Сборка: GOOS=linux GOARCH=arm GOARM=7 CGO_ENABLED=0 go build -trimpath -ldflags="-s -w"
package main

import (
	"context"
	"bufio"
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"os"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	micAddrDefault = "127.0.0.1:9850"
	tokenDefault      = ""
	magic             = "HRMA1"
	frameBytes        = 1024 // чанк vqe_0
	backoffMin        = 500 * time.Millisecond
	backoffMax        = 10 * time.Second
)

var (
	micAddr    = flag.String("mic", micAddrDefault, "адрес IPC maind (yio_audio)")
	token      = flag.String("token", tokenDefault, "токен канала")
	bufferSec  = flag.Int("buffer", 10, "глубина буфера при обрыве, секунд")
	logPath    = flag.String("log", "/tmp/hermes-agent.log", "файл лога")
	verbose    = flag.Bool("v", false, "подробный лог чанков")
)

// ---------- protobuf ----------

func varint(n uint64) []byte {
	var out []byte
	for n >= 0x80 {
		out = append(out, byte(n)|0x80)
		n >>= 7
	}
	return append(out, byte(n))
}

func tag(field, wire int) []byte { return varint(uint64(field<<3 | wire)) }

func fBytes(field int, b []byte) []byte {
	out := tag(field, 2)
	out = append(out, varint(uint64(len(b)))...)
	return append(out, b...)
}

func fVarint(field int, v uint64) []byte {
	return append(tag(field, 0), varint(v)...)
}

// envelope: field 252 = body, field 253 = member
func envelope(member int, body []byte) []byte {
	env := fBytes(252, body)
	env = append(env, fVarint(253, uint64(member))...)
	out := make([]byte, 4, 4+len(env))
	binary.LittleEndian.PutUint32(out, uint32(len(env)))
	return append(out, env...)
}

type pbField struct {
	field int
	wire  int
	bytes []byte
	num   uint64
}

func parseFields(data []byte) ([]pbField, error) {
	var out []pbField
	i := 0
	for i < len(data) {
		key, n, err := readVarint(data[i:])
		if err != nil {
			return nil, err
		}
		i += n
		f := pbField{field: int(key >> 3), wire: int(key & 7)}
		switch f.wire {
		case 0:
			v, n, err := readVarint(data[i:])
			if err != nil {
				return nil, err
			}
			i += n
			f.num = v
		case 2:
			l, n, err := readVarint(data[i:])
			if err != nil {
				return nil, err
			}
			i += n
			if i+int(l) > len(data) {
				return nil, io.ErrUnexpectedEOF
			}
			f.bytes = data[i : i+int(l)]
			i += int(l)
		case 5:
			if i+4 > len(data) {
				return nil, io.ErrUnexpectedEOF
			}
			f.bytes = data[i : i+4]
			i += 4
		case 1:
			if i+8 > len(data) {
				return nil, io.ErrUnexpectedEOF
			}
			f.bytes = data[i : i+8]
			i += 8
		default:
			return nil, fmt.Errorf("protobuf wire %d", f.wire)
		}
		out = append(out, f)
	}
	return out, nil
}

func readVarint(b []byte) (uint64, int, error) {
	var v uint64
	var shift uint
	for i := 0; i < len(b); i++ {
		v |= uint64(b[i]&0x7f) << shift
		if b[i] < 0x80 {
			return v, i + 1, nil
		}
		shift += 7
		if shift > 63 {
			return 0, 0, fmt.Errorf("varint too long")
		}
	}
	return 0, 0, io.ErrUnexpectedEOF
}

// ---------- буфер ----------

type pendingBuffer struct {
	mu  sync.Mutex
	buf []byte
	max int
}

func newPendingBuffer(max int) *pendingBuffer { return &pendingBuffer{max: max} }

func (p *pendingBuffer) push(b []byte) {
	p.mu.Lock()
	p.buf = append(p.buf, b...)
	if len(p.buf) > p.max {
		p.buf = p.buf[len(p.buf)-p.max:]
	}
	p.mu.Unlock()
}

func (p *pendingBuffer) take() []byte {
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.buf) == 0 {
		return nil
	}
	out := p.buf
	p.buf = nil
	return out
}

func (p *pendingBuffer) size() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return len(p.buf)
}

// ---------- статистика ----------

type stats struct {
	mu        sync.Mutex
	chunks    uint64
	bytes     uint64
	reconnect uint64
	sendErr   uint64
	lastChunk time.Time
}

func (s *stats) chunk(n int) {
	s.mu.Lock()
	s.chunks++
	s.bytes += uint64(n)
	s.lastChunk = time.Now()
	s.mu.Unlock()
}

func (s *stats) snapshot() (uint64, uint64, uint64, uint64, time.Duration) {
	s.mu.Lock()
	defer s.mu.Unlock()
	silent := time.Since(s.lastChunk)
	if s.lastChunk.IsZero() {
		silent = -1
	}
	return s.chunks, s.bytes, s.reconnect, s.sendErr, silent
}

var st stats

func rssKB() int {
	b, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return -1
	}
	parts := strings.Fields(string(b))
	if len(parts) < 2 {
		return -1
	}
	pages, _ := strconv.Atoi(parts[1])
	return pages * (os.Getpagesize() / 1024)
}

func reportLoop() {
	for range time.Tick(60 * time.Second) {
		c, by, rc, se, silent := st.snapshot()
		log.Printf("stats: chunks=%d bytes=%d reconnect=%d sendErr=%d silent=%v rss=%dKB pending=%dKB",
			c, by, rc, se, silent.Round(time.Millisecond), rssKB(), pendingHolder.size()/1024)
	}
}

var pendingHolder *pendingBuffer

func buffered() *pendingBuffer { return pendingHolder }

// ---------- микрофон ----------

func subscribeBody() []byte {
	body := fBytes(2, nil)
	return append(body, fBytes(3, nil)...)
}

// readMic открывает подписку на vqe_0 и отдаёт PCM-чанки в out.
func readMic(stop <-chan struct{}) error {
	conn, err := net.DialTimeout("tcp", *micAddr, 5*time.Second)
	if err != nil {
		return fmt.Errorf("mic dial: %w", err)
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := conn.Write(envelope(159, subscribeBody())); err != nil {
		return fmt.Errorf("mic subscribe: %w", err)
	}
	log.Printf("mic: подписка на vqe_0 отправлена")

	br := bufio.NewReaderSize(conn, 64*1024)
	var hdr [4]byte
	for {
		select {
		case <-stop:
			return nil
		default:
		}
		_ = conn.SetReadDeadline(time.Now().Add(30 * time.Second))
		if _, err := io.ReadFull(br, hdr[:]); err != nil {
			if ne, ok := err.(net.Error); ok && ne.Timeout() {
				continue
			}
			return fmt.Errorf("mic read: %w", err)
		}
		n := binary.LittleEndian.Uint32(hdr[:])
		if n == 0 || n > 1<<20 {
			return fmt.Errorf("mic frame size %d", n)
		}
		payload := make([]byte, n)
		if _, err := io.ReadFull(br, payload); err != nil {
			return fmt.Errorf("mic body: %w", err)
		}
		chunks, err := extractAudio(payload, "vqe_0")
		if err != nil {
			continue
		}
		for _, c := range chunks {
			pendingHolder.push(c)
		}
	}
}

func extractAudio(payload []byte, want string) ([][]byte, error) {
	fields, err := parseFields(payload)
	if err != nil {
		return nil, err
	}
	var member uint64
	var body []byte
	for _, f := range fields {
		if f.field == 252 && f.wire == 2 {
			body = f.bytes
		} else if f.field == 253 && f.wire == 0 {
			member = f.num
		}
	}
	if member != 158 || body == nil {
		return nil, fmt.Errorf("not audio frame")
	}
	inner, err := parseFields(body)
	if err != nil {
		return nil, err
	}
	var out [][]byte
	for _, f := range inner {
		if f.field != 1 || f.wire != 2 {
			continue
		}
		name, data := "", []byte(nil)
		sub, err := parseFields(f.bytes)
		if err != nil {
			continue
		}
		for _, s := range sub {
			if s.field == 2 && s.wire == 2 {
				name = string(s.bytes)
			} else if s.field == 3 && s.wire == 2 {
				data = s.bytes
			}
		}
		if name == want && len(data) > 0 {
			out = append(out, data)
		}
	}
	return out, nil
}

// ---------- переподключение ----------

func nextBackoff(b time.Duration) time.Duration {
	b *= 2
	if b > backoffMax {
		return backoffMax
	}
	return b
}

func bumpReconnect() {
	st.mu.Lock()
	st.reconnect++
	st.mu.Unlock()
}

func main() {
	flag.Parse()
	logf, err := os.OpenFile(*logPath, os.O_CREATE|os.O_WRONLY|os.O_APPEND, 0o644)
	if err != nil {
		logf = os.Stderr
	}
	log.SetOutput(logf)
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Printf("=== hermes-station-agent старт: mic=%s server=%s name=%s buffer=%ds",
		*micAddr, *serverURL, *deviceName, *bufferSec)

	pendingHolder = newPendingBuffer(*bufferSec * 32000)
	stop := make(chan struct{})
	go reportLoop()
	go audioPlayer()
	go wsLoop()
	go watchButtonsLog(context.Background())
	go watchMediaLog(context.Background())

	backoff := backoffMin
	for {
		if err := readMic(stop); err != nil {
			log.Printf("mic: %v — переподключение через %v", err, backoff)
			time.Sleep(backoff)
			backoff *= 2
			if backoff > backoffMax {
				backoff = backoffMax
			}
			continue
		}
		backoff = backoffMin
	}
}
