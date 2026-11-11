// Громкость станции без облака.
//
// Логическое состояние громкости хранит maind: его используют кнопки, кольцо
// и Алиса, и он же применяет уровень к ALSA. Записываем только через нативный
// VolumeSet (см. quasar_volume.go) — прямого sset в микшер больше нет: он
// менял только ALSA, и следующее нажатие кнопки возвращало старый уровень.
//
// amixer здесь остаётся только на чтение: диагностика, health и наблюдение
// за изменениями от кнопок. Уровень 1..10 считается по дБ, а не по процентам:
// проценты у моделей разные (Лайт 2 при уровне 5 показывает 58%, Лайт 1 — 50%),
// а шкала дБ общая — шаг 5 дБ от −50 дБ (тишина) до 0 дБ (максимум).
package main

import (
	"context"
	"fmt"
	"log"
	"math"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	volumePoll   = 2 * time.Second
	volumeMaster = "Master"
)

// amixerPath — переменная, а не константа: тесты подменяют её фейковым
// скриптом, чтобы проверять команды без живого железа.
var amixerPath = "/usr/bin/amixer"

// volumeApplyWait — сколько ждать, пока maind применит директиву к микшеру.
// Переменная, а не константа: тесты выключают ожидание.
var volumeApplyWait = 2 * time.Second

var (
	volMu      sync.Mutex
	volRaw     = -1                // последнее прочитанное raw-значение микшера
	volPercent = -1                // процент ALSA (диагностика)
	volDB      float64             // dB по ALSA
	volumeNote = make(chan volumeEvent, 1) // сигнал сессии: уровень изменился
	maVolumeAt time.Time           // когда громкость пришла из MA (не echo)
)

type volumeEvent struct {
	level  int
	fromMA bool
}

var (
	reRaw     = regexp.MustCompile(`Playback (\d+) \[`)
	rePercent = regexp.MustCompile(`\[(\d+)%\]`)
	reDB      = regexp.MustCompile(`\[(-?[\d.]+)dB\]`)
)

// parseVolume разбирает вывод `amixer sget Master`: raw, процент и dB.
func parseVolume(out []byte) (int, int, float64, error) {
	rm := reRaw.FindSubmatch(out)
	if rm == nil {
		return 0, 0, 0, fmt.Errorf("amixer: не нашёл raw в выводе")
	}
	raw, err := strconv.Atoi(string(rm[1]))
	if err != nil {
		return 0, 0, 0, fmt.Errorf("amixer: raw %q: %w", rm[1], err)
	}
	pct := 0
	if m := rePercent.FindSubmatch(out); m != nil {
		if v, err := strconv.Atoi(string(m[1])); err == nil {
			pct = v
		}
	}
	db := 0.0
	if d := reDB.FindSubmatch(out); d != nil {
		if v, err := strconv.ParseFloat(string(d[1]), 64); err == nil {
			db = v
		}
	}
	return raw, pct, db, nil
}

// readVolume читает Master.
func readVolume() (int, int, float64, error) {
	out, err := exec.Command(amixerPath, "-D", "default", "sget", volumeMaster).Output()
	if err != nil {
		return 0, 0, 0, fmt.Errorf("amixer sget: %w", err)
	}
	return parseVolume(out)
}

// setAmixerPercent — аварийный путь, когда maind принял VolumeSet, но
// фактический микшер не изменился.
func setAmixerPercent(percent int) error {
	if percent < 0 {
		percent = 0
	}
	if percent > 100 {
		percent = 100
	}
	out, err := exec.Command(amixerPath, "-D", "default", "sset",
		volumeMaster, fmt.Sprintf("%d%%", percent)).CombinedOutput()
	if err != nil {
		return fmt.Errorf("amixer sset: %w (%s)", err, strings.TrimSpace(string(out)))
	}
	return nil
}

// currentVolume отдаёт последнее прочитанное значение (без вызова amixer).
func currentVolume() (int, int, float64) {
	volMu.Lock()
	defer volMu.Unlock()
	return volRaw, volPercent, volDB
}

// notifyVolume неблокирующе просит цикл сессии отправить событие.
func notifyVolume(level int) {
	notifyVolumeFrom(level, false)
}

// notifyVolumeFromMA — изменение пришло от Music Assistant. Хаб обновит
// состояние, но не отправит это значение обратно в MA: иначе получается
// петля «MA → ALSA → MA» и громкость мигает между двумя уровнями.
func notifyVolumeFromMA(level int) {
	notifyVolumeFrom(level, true)
}

func notifyVolumeFrom(level int, fromMA bool) {
	select {
	case volumeNote <- volumeEvent{level: level, fromMA: fromMA}:
	default:
	}
}

// markMAVolume отделяет эхо от MA от локального изменения (кнопка/Алиса/HA).
func markMAVolume() {
	volMu.Lock()
	maVolumeAt = time.Now()
	volMu.Unlock()
}

func markLocalVolume() {
	volMu.Lock()
	maVolumeAt = time.Time{}
	volMu.Unlock()
}

func recentMAVolume() bool {
	volMu.Lock()
	defer volMu.Unlock()
	return !maVolumeAt.IsZero() && time.Since(maVolumeAt) < 3*time.Second
}

// watchVolume следит за микшером: голос Алисы и кнопки меняют Master
// без нашего участия, и хаб должен об этом узнать.
func watchVolume(ctx context.Context) {
	for {
		if raw, pct, db, err := readVolume(); err == nil {
			volMu.Lock()
			changed := raw != volRaw
			volRaw, volPercent, volDB = raw, pct, db
			volMu.Unlock()
			if changed {
				level := levelOf(pct, db)
				log.Printf("громкость: raw=%d (%d%%, %.2f dB) — уровень %d", raw, pct, db, level)
				if recentMAVolume() {
					// applyMAVolume уже отправит событие с fromMA=true.
					log.Printf("громкость: изменение пришло из MA — без echo")
				} else {
					notifyVolume(level)
				}
			}
		} else {
			log.Printf("громкость: чтение не удалось: %v", err)
		}
		select {
		case <-ctx.Done():
			return
		case <-time.After(volumePoll):
		}
	}
}

// levelOf переводит состояние микшера в уровень станции 0..10.
// Шкала общая для всех моделей: 0 дБ — 10, −5 дБ — 9, …, −50 дБ — 0.
func levelOf(pct int, db float64) int {
	if pct <= 0 {
		return 0
	}
	lv := int(math.Round((db + 50) / 5))
	if lv < 1 {
		return 1
	}
	if lv > 10 {
		return 10
	}
	return lv
}

// setVolumeLevel ставит уровень станции 0..10.
//
// Единственный путь — нативный VolumeSet в maind: он хранит логическую
// громкость кнопок и Алисы и сам применяет её к микшеру. Если директива не
// ушла, громкость не меняется и вызывающий получает ошибку — молча писать
// в amixer нельзя, иначе maind и микшер разойдутся.
func setVolumeLevel(level int) error {
	if level < 0 {
		level = 0
	}
	if level > 10 {
		level = 10
	}
	return setVolumePercent(level*10, false)
}

// setVolumePercentFromMA применяет громкость, пришедшую из Music Assistant.
// Идём через maind, а не прямым amixer: иначе maind возвращает свой старый
// логический уровень, и ползунок MA на колонке не держится.
func setVolumePercentFromMA(percent int) error {
	return setVolumePercent(percent, true)
}

func setVolumePercent(percent int, fromMA bool) error {
	if fromMA {
		markMAVolume()
	} else {
		markLocalVolume()
	}
	if percent < 0 {
		percent = 0
	}
	if percent > 100 {
		percent = 100
	}
	if err := quasarVolumeSet(percent); err != nil {
		log.Printf("громкость: IPC не отправился: %v", err)
		return err
	}
	// maind применяет директиву не мгновенно (живьём — до 1.7 с), поэтому
	// читаем микшер с ретраем, пока процент не совпадёт с запрошенным.
	deadline := time.Now().Add(volumeApplyWait)
	var raw, rp int
	var rd float64
	applied := false
	for {
		var err error
		raw, rp, rd, err = readVolume()
		if err == nil {
			volMu.Lock()
			volRaw, volPercent, volDB = raw, rp, rd
			volMu.Unlock()
			if rp == percent {
				applied = true
				break
			}
		}
		if time.Now().After(deadline) {
			break
		}
		time.Sleep(100 * time.Millisecond)
	}
	if !applied {
		// На части прошивок maind подтверждает VolumeSet, но микшер не
		// меняет. Для ползунка MA нужен фактический уровень динамика,
		// поэтому добиваем напрямую ALSA.
		if err := setAmixerPercent(percent); err != nil {
			log.Printf("громкость: ALSA fallback не сработал: %v", err)
			return err
		}
		if _, pct, db, err := readVolume(); err == nil {
			volMu.Lock()
			volPercent, volDB = pct, db
			volMu.Unlock()
		}
	}
	if volPercent >= 0 {
		log.Printf("громкость: IPC ok, %d%% (raw=%d, %d%%, %.2f dB)",
			percent, volRaw, volPercent, volDB)
	} else {
		log.Printf("громкость: IPC ok, %d%%, микшер не прочитался", percent)
	}
	lvl := levelOf(volPercent, volDB)
	if fromMA {
		notifyVolumeFromMA(lvl)
	} else {
		notifyVolume(lvl)
	}
	return nil
}
