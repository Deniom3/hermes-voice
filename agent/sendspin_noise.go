package main

// Noise KKpsk2 для Sendspin: клиент — responder, сервер — initiator.
//
// Оба статических ключа известны заранее (client/init и server/init), поэтому
// в KK всего два сообщения. PSK вставляется в сообщение 2 (psk2). Для
// незапаренной колонки используется публичный Sentinel PSK — он ничего не
// аутентифицирует, но именно так MA разрешает unpaired-клиентов.

import (
	"crypto/rand"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/flynn/noise"
)

type ssIdentity struct {
	priv []byte
	pub  []byte
}

func (id ssIdentity) clientID() string { return ssB64(id.pub) }

func ssLoadIdentity(dir string) (ssIdentity, error) {
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return ssIdentity{}, err
	}
	path := filepath.Join(dir, "identity.key")
	raw, err := os.ReadFile(path)
	if err == nil {
		lines := strings.Split(strings.TrimSpace(string(raw)), "\n")
		if len(lines) >= 2 {
			priv, err1 := ssB64Decode(strings.TrimSpace(lines[0]))
			pub, err2 := ssB64Decode(strings.TrimSpace(lines[1]))
			if err1 == nil && err2 == nil && len(priv) == 32 && len(pub) == 32 {
				return ssIdentity{priv: priv, pub: pub}, nil
			}
		}
		return ssIdentity{}, fmt.Errorf("identity.key повреждён: %s", path)
	}
	if !os.IsNotExist(err) {
		// Права/IO: не генерируем новый ключ, иначе затрём существующий
		// client_id и MA потеряет плеер.
		return ssIdentity{}, fmt.Errorf("identity.key: %w", err)
	}

	cs := noise.NewCipherSuite(noise.DH25519, noise.CipherAESGCM, noise.HashSHA256)
	kp, err := cs.GenerateKeypair(rand.Reader)
	if err != nil {
		return ssIdentity{}, err
	}
	id := ssIdentity{priv: kp.Private, pub: kp.Public}
	data := ssB64(id.priv) + "\n" + ssB64(id.pub) + "\n"
	if err := os.WriteFile(path, []byte(data), 0o600); err != nil {
		return ssIdentity{}, err
	}
	return id, nil
}

type ssServerInit struct {
	ServerID string `json:"server_id"`
	Version  int    `json:"version"`
}

// ssHandshake выполняет client/init → server/init → Noise msg1 → msg2.
// Возвращает шифры для исходящих и входящих сообщений и server_id.
func ssHandshake(conn *wsConn, id ssIdentity) (send, recv *noise.CipherState, serverID string, err error) {
	clientID := id.clientID()
	initText, err := ssEncode("client/init", map[string]any{
		"client_id": clientID,
		"version":   ssProtocolVersion,
		"suite":     ssSuite,
	})
	if err != nil {
		return nil, nil, "", err
	}
	if err := conn.WriteMessage(opText, initText); err != nil {
		return nil, nil, "", fmt.Errorf("client/init: %w", err)
	}

	op, data, err := conn.ReadMessage()
	if err != nil {
		return nil, nil, "", fmt.Errorf("server/init: %w", err)
	}
	if op != opText {
		return nil, nil, "", fmt.Errorf("server/init: ожидался текст, получен opcode %d", op)
	}
	var env ssEnvelope
	if err := json.Unmarshal(data, &env); err != nil {
		return nil, nil, "", fmt.Errorf("server/init JSON: %w", err)
	}
	if env.Type == "server/error" {
		return nil, nil, "", fmt.Errorf("server/error: %s", string(env.Payload))
	}
	if env.Type != "server/init" {
		return nil, nil, "", fmt.Errorf("server/init: неожиданный тип %q", env.Type)
	}
	var si ssServerInit
	if err := json.Unmarshal(env.Payload, &si); err != nil {
		return nil, nil, "", fmt.Errorf("server/init payload: %w", err)
	}
	if si.Version != ssProtocolVersion {
		return nil, nil, "", fmt.Errorf("server/init: версия %d, ожидалась %d", si.Version, ssProtocolVersion)
	}
	serverPub, err := ssB64Decode(si.ServerID)
	if err != nil || len(serverPub) != 32 {
		return nil, nil, "", fmt.Errorf("server/init: некорректный server_id")
	}

	cs := noise.NewCipherSuite(noise.DH25519, noise.CipherAESGCM, noise.HashSHA256)
	hs, err := noise.NewHandshakeState(noise.Config{
		CipherSuite:           cs,
		Pattern:               noise.HandshakeKK,
		Initiator:             false,
		StaticKeypair:         noise.DHKey{Private: id.priv, Public: id.pub},
		PeerStatic:            serverPub,
		Prologue:              append(append([]byte{}, initText...), data...),
		PresharedKey:          ssSentinelPSK[:],
		PresharedKeyPlacement: 2,
	})
	if err != nil {
		return nil, nil, "", fmt.Errorf("noise init: %w", err)
	}

	// Сообщение 1: сервер → клиент.
	op, data, err = conn.ReadMessage()
	if err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1: %w", err)
	}
	if op != opText {
		return nil, nil, "", fmt.Errorf("noise msg1: ожидался текст, получен opcode %d", op)
	}
	var hsEnv ssEnvelope
	if err := json.Unmarshal(data, &hsEnv); err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1 JSON: %w", err)
	}
	if hsEnv.Type != "noise/handshake" {
		return nil, nil, "", fmt.Errorf("noise msg1: неожиданный тип %q", hsEnv.Type)
	}
	var hsPayload struct {
		Data string `json:"data"`
	}
	if err := json.Unmarshal(hsEnv.Payload, &hsPayload); err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1 payload: %w", err)
	}
	ct1, err := ssB64Decode(hsPayload.Data)
	if err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1 base64: %w", err)
	}
	pt1, _, _, err := hs.ReadMessage(nil, ct1)
	if err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1 read: %w", err)
	}
	var msg1 struct {
		PSKID       string          `json:"psk_id"`
		PSKCategory json.RawMessage `json:"psk_category"`
	}
	if err := json.Unmarshal(pt1, &msg1); err != nil {
		return nil, nil, "", fmt.Errorf("noise msg1 payload: %w", err)
	}
	if len(msg1.PSKCategory) > 0 {
		var cat string
		if err := json.Unmarshal(msg1.PSKCategory, &cat); err != nil {
			return nil, nil, "", fmt.Errorf("noise msg1 psk_category: %w", err)
		}
		if cat != "sn" {
			return nil, nil, "", fmt.Errorf("noise msg1: сервер ждёт PSK категории %q, поддерживается только sentinel (sn)", cat)
		}
	}

	// Сообщение 2: клиент → сервер, payload строго "{}".
	ct2, cs1, cs2, err := hs.WriteMessage(nil, []byte("{}"))
	if err != nil {
		return nil, nil, "", fmt.Errorf("noise msg2 write: %w", err)
	}
	hsOut, err := ssEncode("noise/handshake", map[string]string{"data": ssB64(ct2)})
	if err != nil {
		return nil, nil, "", err
	}
	if err := conn.WriteMessage(opText, hsOut); err != nil {
		return nil, nil, "", fmt.Errorf("noise msg2 send: %w", err)
	}

	// Для responder: Split() отдаёт (initiator→responder, responder→initiator),
	// значит отправляем вторым, принимаем первым.
	return cs2, cs1, si.ServerID, nil
}
