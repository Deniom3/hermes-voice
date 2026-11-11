package main

import (
	"net"
	"testing"
)

func TestParseHubReply(t *testing.T) {
	cases := []struct {
		reply string
		want  string
		ok    bool
	}{
		{"HERMES-HUB 192.168.1.12 8796", "ws://192.168.1.12:8796/ws", true},
		{"HERMES-HUB 192.168.1.12 8796\n", "ws://192.168.1.12:8796/ws", true},
		{"HERMES-HUB 10.0.0.5 9000", "ws://10.0.0.5:9000/ws", true},
		// Чужой ответ на тот же порт не должен считаться хабом.
		{"SOME-SERVICE 192.168.1.12 8796", "", false},
		{"HERMES-HUB", "", false},
		{"HERMES-HUB 192.168.1.12", "", false},
		{"HERMES-HUB not-an-ip 8796", "", false},
		{"HERMES-HUB 192.168.1.12 not-a-port", "", false},
		{"HERMES-HUB 192.168.1.12 70000", "", false},
		{"", "", false},
	}
	for _, c := range cases {
		got, ok := parseHubReply(c.reply)
		if ok != c.ok || got != c.want {
			t.Errorf("parseHubReply(%q) = %q,%v; ожидалось %q,%v",
				c.reply, got, ok, c.want, c.ok)
		}
	}
}

func TestBroadcastTargetsHasSubnetBroadcast(t *testing.T) {
	targets := broadcastTargets()
	if len(targets) == 0 {
		t.Fatal("нет целей broadcast")
	}
	if !targets[0].Equal(net.IPv4bcast) {
		t.Errorf("первая цель %v, ожидался 255.255.255.255", targets[0])
	}
}
