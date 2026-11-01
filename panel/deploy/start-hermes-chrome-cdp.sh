#!/bin/bash
# Живой браузер панели: Chrome на VNC-дисплее :1 с CDP на 9223.
# Профиль — постоянный, чтобы входы на сайты не терялись между запусками.
export DISPLAY=:1
export XAUTHORITY="$HOME/.Xauthority"
exec /opt/google/chrome/chrome \
  --remote-debugging-port=9223 \
  --remote-debugging-address=127.0.0.1 \
  --user-data-dir="$HOME/.config/google-chrome-cdp" \
  --no-first-run \
  --no-default-browser-check \
  --no-sandbox \
  --test-type \
  --hide-crash-restore-bubble \
  --simulate-outdated-no-au="Tue, 31 Dec 2099 23:59:59 GMT" \
  --disable-session-crashed-bubble \
  --start-maximized \
  about:blank
