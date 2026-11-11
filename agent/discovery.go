package main

import (
	"fmt"
	"log"
	"net"
	"strconv"
	"strings"
	"time"
)

// Порт, на который хаб отвечает на broadcast-поиск. Фиксирован намеренно:
// это не настройка, а адрес, по которому агент находит хаб в локальной сети.
const discoveryPort = 8797

// discoverHub ищет хаб в локальной сети и возвращает адрес WebSocket.
//
// Адрес хаба не хранится на колонке: DHCP может выдать другой IP, а
// прописанный вручную адрес после этого молча ломает всю связь. mDNS здесь
// не годится — на колонке нет avahi/nss-mdns, а бинарь собран с
// CGO_ENABLED=0, и его резолвер умеет только обычный DNS.
func discoverHub(timeout time.Duration) (string, error) {
	conn, err := net.ListenUDP("udp4", &net.UDPAddr{IP: net.IPv4zero, Port: 0})
	if err != nil {
		return "", fmt.Errorf("сокет поиска: %w", err)
	}
	defer conn.Close()

	targets := broadcastTargets()
	if len(targets) == 0 {
		return "", fmt.Errorf("нет интерфейса с адресом в локальной сети")
	}
	for _, target := range targets {
		if _, err := conn.WriteToUDP([]byte("HERMES-HUB?"),
			&net.UDPAddr{IP: target, Port: discoveryPort}); err != nil {
			log.Printf("поиск хаба: отправка на %s: %v", target, err)
		}
	}

	deadline := time.Now().Add(timeout)
	buf := make([]byte, 2048)
	for {
		if err := conn.SetReadDeadline(deadline); err != nil {
			return "", err
		}
		n, addr, err := conn.ReadFromUDP(buf)
		if err != nil {
			return "", fmt.Errorf("хаб не ответил за %v", timeout)
		}
		// Ожидаем «HERMES-HUB <ip> <port>»: чужой ответ на тот же порт
		// (например, от другой программы) не должен приниматься за хаб.
		wsURL, ok := parseHubReply(string(buf[:n]))
		if !ok {
			log.Printf("поиск хаба: чужой ответ от %s: %q", addr, string(buf[:n]))
			continue
		}
		return wsURL, nil
	}
}

// parseHubReply разбирает ответ хаба «HERMES-HUB <ip> <port>».
func parseHubReply(reply string) (string, bool) {
	fields := strings.Fields(reply)
	if len(fields) != 3 || fields[0] != "HERMES-HUB" {
		return "", false
	}
	ip := net.ParseIP(fields[1])
	if ip == nil || ip.To4() == nil {
		return "", false
	}
	port, err := strconv.Atoi(fields[2])
	if err != nil || port <= 0 || port > 65535 {
		return "", false
	}
	return fmt.Sprintf("ws://%s:%s/ws", fields[1], fields[2]), true
}

// broadcastTargets — широковещательные адреса всех интерфейсов с IPv4.
// 255.255.255.255 не проходит через некоторые точки доступа, поэтому
// направленный broadcast подсети нужен отдельно.
func broadcastTargets() []net.IP {
	targets := []net.IP{net.IPv4bcast}
	ifaces, err := net.Interfaces()
	if err != nil {
		return targets
	}
	for _, iface := range ifaces {
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ipnet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipnet.IP.To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
				continue
			}
			b := make(net.IP, 4)
			for i := 0; i < 4; i++ {
				b[i] = ip[i] | ^ipnet.Mask[i]
			}
			targets = append(targets, b)
		}
	}
	return targets
}

// primaryMAC — MAC интерфейса, у которого есть обычный IPv4 (не loopback и
// не link-local 169.254). На Лайт 2 рядом висит p2p0 с 169.254 — его брать
// нельзя: он есть всегда, даже когда Wi-Fi не поднят.
func primaryMAC() string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return ""
	}
	for _, iface := range ifaces {
		if iface.Flags&net.FlagLoopback != 0 || len(iface.HardwareAddr) == 0 {
			continue
		}
		addrs, err := iface.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ipnet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip := ipnet.IP.To4()
			if ip == nil || ip.IsLoopback() || ip.IsLinkLocalUnicast() {
				continue
			}
			return iface.HardwareAddr.String()
		}
	}
	return ""
}
