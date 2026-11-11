# Мониторинг голосового контура

Стек мониторинга: **Nov Monitoring** (192.168.1.30), проект `monitoring-nov`
в Komodo, каталог `/home/user/docker/monitoring`.
Prometheus `:9090`, Grafana `:3000`, Loki, Alloy, VictoriaMetrics.

## Что отдаёт хаб

`GET http://hub.example.com:8796/metrics` — формат Prometheus,
без внешних зависимостей (`hub/metrics.py`).

Основные метрики:

- `hermes_voice_station_online{station}` — колонка подключена к хабу;
- `hermes_voice_station_last_audio_timestamp_seconds{station}` — когда был
  последний чанк; по нему ловится «подключена, но молчит»;
- `hermes_voice_wake_activations_total`, `..._wake_misses_total`,
  `..._wake_no_name_total`, `..._wake_arbitration_lost_total`;
- `hermes_voice_wake_score` — гистограмма пиков детектора;
- `hermes_voice_asr_seconds`, `..._asr_audio_seconds`, `..._asr_empty_total`,
  `..._asr_errors_total`;
- `hermes_voice_phrases_total{route="ha|intent|llm"}`, `..._intents_total{phrase}`;
- `hermes_voice_answer_seconds`, `..._answer_chars` — скорость и краткость;
- `hermes_voice_dataset_files{kind="positives|negatives|misses"}`, `..._dataset_bytes`;
- `hermes_voice_errors_total{stage}`.

## Установка

1. Добавить `prometheus/hermes-voice-job.yml` в `scrape_configs`
   `prometheus.yml` на Nov Monitoring.
2. Положить `prometheus/hermes-voice-alerts.yml` рядом и прописать в `rule_files`.
3. Перечитать конфиг без рестарта:
   `curl -X POST http://192.168.1.30:9090/-/reload`
   (Prometheus запущен с `--web.enable-lifecycle`).
4. Скопировать `grafana/hermes-voice.json` в
   `/home/user/docker/monitoring/grafana/provisioning/dashboards/`
   и указать источник Prometheus в переменной `DS_PROMETHEUS`.

Проверка: в Prometheus `/targets` job `hermes-voice` должен быть `UP`,
в Grafana появиться дашборд «Голосовой контур Hermes».

## Алерты

- `HermesVoiceHubDown` — хаб не отвечает 2 минуты.
- `HermesVoiceStationOffline` — колонка отвалилась.
- `HermesVoiceStationSilent` — колонка подключена, но аудио нет 2 минуты.
- `HermesVoiceNoNameRate` — >15% активаций без имени (ASR или ложные).
- `HermesVoiceMissRate` — >25% промахов детектора.
- `HermesVoiceAsrErrors`, `HermesVoiceHubErrors`.

## Ограничение

Prometheus на Nov Monitoring хранит 7 дней (`--storage.tsdb.retention.time=7d`).
Для долгой истории датасета надо либо увеличить retention, либо писать в
VictoriaMetrics (контейнер уже стоит на том же хосте, `:8428`).
