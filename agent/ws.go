// Минимальный WebSocket-клиент на стандартной библиотеке.
//
// Специально без внешних модулей: сборка идёт offline, а на устройстве
// (armhf, Go 1.22) нечего тянуть из сети. Поддерживается ровно то, что нужно
// мосту: текстовые и бинарные кадры, ping/pong, close, фрагментация на чтение.
package main

import (
	"bufio"
	"crypto/rand"
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"net/url"
	"strings"
	"sync"
	"time"
)

const (
	opCont  byte = 0x0
	opText  byte = 0x1
	opBin   byte = 0x2
	opClose byte = 0x8
	opPing  byte = 0x9
	opPong  byte = 0xA
)

type wsConn struct {
	conn net.Conn
	br   *bufio.Reader
	wmu  sync.Mutex
}

func wsDial(rawURL string, timeout time.Duration) (*wsConn, error) {
	u, err := url.Parse(rawURL)
	if err != nil {
		return nil, err
	}
	if u.Scheme != "ws" {
		return nil, fmt.Errorf("поддерживается только ws://, получено %q", u.Scheme)
	}
	host := u.Host
	if !strings.Contains(host, ":") {
		host += ":80"
	}
	conn, err := net.DialTimeout("tcp", host, timeout)
	if err != nil {
		return nil, err
	}
	if tc, ok := conn.(*net.TCPConn); ok {
		_ = tc.SetKeepAlive(true)
		_ = tc.SetKeepAlivePeriod(5 * time.Second)
		_ = tc.SetNoDelay(true)
	}

	path := u.RequestURI()
	if path == "" {
		path = "/"
	}
	var keyRaw [16]byte
	if _, err := rand.Read(keyRaw[:]); err != nil {
		conn.Close()
		return nil, err
	}
	key := base64.StdEncoding.EncodeToString(keyRaw[:])
	req := fmt.Sprintf("GET %s HTTP/1.1\r\nHost: %s\r\nUpgrade: websocket\r\n"+
		"Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n",
		path, u.Host, key)
	_ = conn.SetWriteDeadline(time.Now().Add(5 * time.Second))
	if _, err := conn.Write([]byte(req)); err != nil {
		conn.Close()
		return nil, err
	}

	br := bufio.NewReaderSize(conn, 64*1024)
	_ = conn.SetReadDeadline(time.Now().Add(5 * time.Second))
	status, err := br.ReadString('\n')
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("handshake: %w", err)
	}
	if !strings.Contains(status, "101") {
		conn.Close()
		return nil, fmt.Errorf("handshake: %s", strings.TrimSpace(status))
	}
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			conn.Close()
			return nil, fmt.Errorf("handshake headers: %w", err)
		}
		if line == "\r\n" || line == "\n" {
			break
		}
	}
	_ = conn.SetReadDeadline(time.Time{})
	_ = conn.SetWriteDeadline(time.Time{})
	return &wsConn{conn: conn, br: br}, nil
}

func (c *wsConn) WriteMessage(op byte, payload []byte) error {
	c.wmu.Lock()
	defer c.wmu.Unlock()

	n := len(payload)
	frame := make([]byte, 0, n+14)
	frame = append(frame, 0x80|op) // FIN + opcode
	switch {
	case n <= 125:
		frame = append(frame, byte(n)|0x80)
	case n <= 65535:
		frame = append(frame, 126|0x80, byte(n>>8), byte(n))
	default:
		frame = append(frame, 127|0x80, 0, 0, 0, 0,
			byte(n>>24), byte(n>>16), byte(n>>8), byte(n))
	}
	var mask [4]byte
	if _, err := rand.Read(mask[:]); err != nil {
		return err
	}
	frame = append(frame, mask[:]...)
	start := len(frame)
	frame = append(frame, payload...)
	for i := 0; i < n; i++ {
		frame[start+i] ^= mask[i%4]
	}

	_ = c.conn.SetWriteDeadline(time.Now().Add(10 * time.Second))
	_, err := c.conn.Write(frame)
	return err
}

func (c *wsConn) readFrame() (fin bool, op byte, payload []byte, err error) {
	var hdr [2]byte
	if _, err = io.ReadFull(c.br, hdr[:]); err != nil {
		return
	}
	fin = hdr[0]&0x80 != 0
	op = hdr[0] & 0x0f
	masked := hdr[1]&0x80 != 0
	n := uint64(hdr[1] & 0x7f)
	switch n {
	case 126:
		var ext [2]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return
		}
		n = uint64(binary.BigEndian.Uint16(ext[:]))
	case 127:
		var ext [8]byte
		if _, err = io.ReadFull(c.br, ext[:]); err != nil {
			return
		}
		n = binary.BigEndian.Uint64(ext[:])
	}
	if n > 16<<20 {
		err = fmt.Errorf("кадр слишком большой: %d", n)
		return
	}
	var mask [4]byte
	if masked {
		if _, err = io.ReadFull(c.br, mask[:]); err != nil {
			return
		}
	}
	payload = make([]byte, n)
	if n > 0 {
		if _, err = io.ReadFull(c.br, payload); err != nil {
			return
		}
	}
	if masked {
		for i := range payload {
			payload[i] ^= mask[i%4]
		}
	}
	return
}

// ReadMessage собирает кадры до FIN, отвечает на ping и close.
func (c *wsConn) ReadMessage() (byte, []byte, error) {
	var op byte
	var buf []byte
	for {
		fin, opcode, payload, err := c.readFrame()
		if err != nil {
			return 0, nil, err
		}
		switch opcode {
		case opPing:
			_ = c.WriteMessage(opPong, payload)
			continue
		case opPong:
			continue
		case opClose:
			_ = c.WriteMessage(opClose, nil)
			return 0, nil, io.EOF
		}
		if opcode != opCont {
			op = opcode
		}
		buf = append(buf, payload...)
		if fin {
			return op, buf, nil
		}
	}
}

// PingLoop держит соединение живым: без него мёртвый канал виден только
// при следующей записи.
func (c *wsConn) PingLoop(every time.Duration, stop <-chan struct{}) {
	t := time.NewTicker(every)
	defer t.Stop()
	for {
		select {
		case <-stop:
			return
		case <-t.C:
			if err := c.WriteMessage(opPing, nil); err != nil {
				return
			}
		}
	}
}

// SetReadDeadline ограничивает ожидание кадра: без него пропавший сервер
// оставляет readLoop в вечном ожидании.
func (c *wsConn) SetReadDeadline(t time.Time) error {
	return c.conn.SetReadDeadline(t)
}

func (c *wsConn) Close() error {
	_ = c.WriteMessage(opClose, nil)
	return c.conn.Close()
}

var errClosed = errors.New("соединение закрыто")
