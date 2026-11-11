import { useEffect, useRef, useState } from "react";
import {
  LuMic, LuMicOff, LuGlobe, LuTerminal, LuMinus, LuMaximize2, LuMinimize2,
  LuMessageSquarePlus, LuVolume2, LuVolume1, LuVolumeX,
} from "react-icons/lu";
import { MicVAD } from "@ricky0123/vad-web";
import * as ort from "onnxruntime-web";

// Firefox падает на многопоточной сборке onnxruntime («failed to allocate
// executable memory»): ей нужен SharedArrayBuffer. Одного потока достаточно —
// VAD лёгкий, а так он запускается в любом браузере.
ort.env.wasm.numThreads = 1;
ort.env.wasm.simd = true;
import { VoiceOrb, type VoiceLevels } from "./VoiceOrb";
import { DEFAULT_ORB } from "./orb-style";

type Mode = "idle" | "input" | "output" | "muted" | "thinking" | "tool";

// Громкость, при которой срабатывание VAD считаем твоим перебиванием, а не эхом
// собственной озвучки из колонок (~ -26 дБ на шкале -1..1).
const BARGE_RMS = 0.05;
// Сколько кадров (~32 мс каждый) копим, пока идёт озвучка: ~1.5 с — чтобы начало
// перебивающей фразы не потерялось.
const IGNORED_FRAMES = 46;

/** Аргументы инструмента как их понятно читать: команда — строкой, прочее — по полям. */
function humanArgs(s: Step): string {
  if (s.context) {
    const a = s.args ? JSON.parse(s.args) as Record<string, unknown> : null;
    const cmd = a && typeof a.command === "string" ? a.command : null;
    // «$» только у настоящей команды: у чтения файла это подпись, а не строка оболочки.
    const head = cmd ? `$ ${cmd}` : (a ? "" : s.context);
    const extra = a ? Object.entries(a).filter(([k]) => k !== "command")
                              .map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join("\n") : "";
    return [head, extra].filter(Boolean).join("\n");
  }
  if (!s.args) return "";
  try {
    const a = JSON.parse(s.args) as Record<string, unknown>;
    const lines = Object.entries(a).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`);
    return lines.join("\n");
  } catch { return s.args; }
}

type Step = {
  id?: string;
  name: string;
  context?: string;
  args?: string;
  result?: string;
  exit?: number | null;
  error?: string | null;
  duration?: number;
  done: boolean;
};

// Тот же origin, что и страница: голосовой сокет живёт на /voice-ws
// (через Traefik наружу открыт ОДИН порт, отдельные порты не пробрасываются).
const WS_URL =
  (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/voice-ws";

// Сколько окна работают держатся: после последнего инструмента и после конца ответа.
const WINDOW_LINGER = 20000;      // сколько держим окна после конца работы
const WINDOW_AFTER_TURN = 12000;   // и после конца ответа

export default function App() {
  const levels = useRef<VoiceLevels>({ input: 0, output: 0, speaking: 0 });
  const [mode, setMode] = useState<Mode>("idle");
  const [transcript, setTranscript] = useState("");
  const [reply, setReply] = useState("");
  const [busy, setBusy] = useState(false);

  // «Наглядность»: окна, которые выезжают сами, когда я работаю.
  const [browserOpen, setBrowserOpen] = useState(false);
  const [browserMax, setBrowserMax] = useState(false);
  const [workOpen, setWorkOpen] = useState(false);
  const [steps, setSteps] = useState<Step[]>([]);
  const lastMsgRef = useRef(0);        // когда сервер последний раз что-то сказал
  const workLogRef = useRef<HTMLDivElement | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const micRef = useRef<any>(null);
  const micCtxRef = useRef<AudioContext | null>(null);   // наш граф усиления микрофона
  const micRawRef = useRef<MediaStream | null>(null);
  const speakingRef = useRef(false);
  // Перебили: доиграть фразу, которая уже звучит, и замолчать — остаток ответа в мусор.
  const stopAfterPhraseRef = useRef(false);
  const probedRef = useRef(false);
  const speechActiveRef = useRef(false);      // идёт ли сейчас реплика (для предбуфера)
  const preRollRef = useRef<Int16Array[]>([]); // последние ~300 мс до начала речи
  const ignoredRef = useRef(false);            // срабатывание от эха — игнорируем
  const loudAtRef = useRef(0);                 // когда микрофон давал громкий звук
  const audioCtxRef = useRef<AudioContext | null>(null);
  const pendingRef = useRef<Uint8Array[]>([]);
  const sourceRef = useRef<AudioBufferSourceNode | null>(null);
  const modeRef = useRef<Mode>("idle");

  const setModeBoth = (m: Mode) => { modeRef.current = m; setMode(m); (window as any).__voiceMode = m; };

  // Окна работы убираются сами: пока инструмент идёт — показываем, закончился — прячем.
  const [micOn, setMicOn] = useState(false);
  const [screenMode, setScreenMode] = useState<"browser" | "desktop">("browser");
  const [volumeMenu, setVolumeMenu] = useState(false);
  const [hint, setHint] = useState("");
  const [volume, setVolume] = useState(() => {
    const raw = localStorage.getItem("voice-volume");
    if (raw === null) return 1;                 // ничего не сохранено — полная громкость.
    const saved = Number(raw);                  // Number(null) даёт 0: так панель молчала!
    return Number.isFinite(saved) && saved >= 0 && saved <= 1 ? saved : 1;
  });
  const volumeRef = useRef(volume);
  const masterGainRef = useRef<GainNode | null>(null);

  // Регулятор стоит между анализатором и выходом: шкала орба показывает саму речь,
  // а не выставленную громкость.
  useEffect(() => {
    volumeRef.current = volume;
    localStorage.setItem("voice-volume", String(volume));
    const g = masterGainRef.current;
    const ctx = audioCtxRef.current;
    if (g && ctx) {
      g.gain.cancelScheduledValues(ctx.currentTime);
      g.gain.setTargetAtTime(volume, ctx.currentTime, 0.02);   // без щелчка
    }
  }, [volume]);

  function ensureMaster(ctx: AudioContext): GainNode {
    if (!masterGainRef.current) {
      const g = ctx.createGain();
      g.gain.value = volumeRef.current;
      g.connect(ctx.destination);
      masterGainRef.current = g;
      (window as any).__voiceGainNode = g;          // для замеров громкости
      (window as any).__voiceVolume = volumeRef.current;
    }
    return masterGainRef.current;
  }
  const micOnRef = useRef(false);          // то же для обработчиков сокета
  const closingRef = useRef(false);        // сами закрыли микрофон — не ругаться «соединение закрыто»
  const restartingRef = useRef(false);     // идёт переподнятие захвата после обрыва
  const micRestoreTriesRef = useRef(0);    // попытки подряд, чтобы не зациклиться
  const hideTimerRef = useRef(0);
  function cancelHide() { window.clearTimeout(hideTimerRef.current); hideTimerRef.current = 0; }
  function scheduleHide(ms: number) {
    cancelHide();
    hideTimerRef.current = window.setTimeout(() => { setWorkOpen(false); setBrowserOpen(false); }, ms);
  }

  /** Отправка в сокет, который может быть ещё в состоянии открытия. */
  function wsSend(obj: object) {
    const ws = ensureWs();
    const data = JSON.stringify(obj);
    if (ws.readyState === 1) ws.send(data);
    else ws.addEventListener("open", () => { try { ws.send(data); } catch { /* уже закрыт */ } }, { once: true });
  }

  function ensureWs(): WebSocket {
    if (wsRef.current && wsRef.current.readyState <= 1) return wsRef.current;
    const ws = new WebSocket(WS_URL);
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;
    ws.onopen = () => {
      // Сервер нумерует ходы заново на каждое соединение, поэтому порог отменённых ходов
      // здесь обязательно сбрасывается: иначе после переподключения панель глухая столько
      // ходов, сколько успела нажать «Новая сессия» до обрыва.
      turnFloorRef.current = 0;
      activeTurnRef.current = null;
      // Микрофон включён — просим сервер заранее поднять модели распознавания и синтеза.
      ws.send(JSON.stringify({ type: "warm" }));
    };
    ws.onclose = () => {
      if (closingRef.current) { closingRef.current = false; return; }   // выключили мы, а не сеть
      setModeBoth(micOnRef.current ? "idle" : "muted");
      // Сокет умер (перезапуск сервера, сеть, засыпание вкладки), а микрофон в браузере
      // остался жить: кадры уходили в мёртвый сокет и панель становилась глухой — кнопка
      // горит, орб синий, на слова не реагирует. Переподнимаем захват на новом сокете.
      if (micOnRef.current) void restoreMicCapture();
    };
    ws.onmessage = (ev) => {
      lastMsgRef.current = performance.now();
      if (ev.data instanceof ArrayBuffer) {
        // Аудио принимаем только для хода, который сейчас ждём: хвост отменённого
        // ответа (он доходит уже после перебивания) играть нельзя.
        if (activeTurnRef.current === null || activeTurnRef.current < turnFloorRef.current) return;
        pendingRef.current.push(new Uint8Array(ev.data));
        return;
      }
      const m = JSON.parse(ev.data);
      switch (m.type) {
        case "final":
          if (!micOnRef.current) break;
          setTranscript(m.text || ""); setBusy(true); setModeBoth("thinking");
          setHint("");
          setSteps([]);
          break;
        case "agent_delta": setReply(r => r + (m.text || "")); break;
        case "hint":
          // Сервер говорит, что происходит (например, модели на 139 поднимаются ~10 с).
          if (m.text) { cancelHide(); setHint(String(m.text)); } else setHint("");
          break;
        case "tool": {
          const name = String(m.name || "инструмент");
          const id = typeof m.id === "string" ? m.id : undefined;
          if (m.state === "start") {
            cancelHide();
            setSteps(s => [...s, { id, name, context: m.context || "", args: m.args || "", done: false }]);
            setWorkOpen(true);
            // Один и тот же цвет, что у кольца колонки в этот момент — лайм.
            setModeBoth("tool");
            // Что именно делает — видно и без раскрытия панели действий.
            setHint(`выполняю: ${name}`);
            if (/browser|chrome|web/i.test(name)) setBrowserOpen(true);
          } else {
            // Результат вешаем на свой шаг: по id от шлюза, иначе на последний такой же
            // без результата — иначе при двух вызовах подряд выводы путаются.
            setSteps(s => {
              const at = id ? s.findIndex(x => x.id === id)
                            : s.map(x => x.name === name && !x.done).lastIndexOf(true);
              const put = (x: Step): Step => ({ ...x, done: true, context: m.context || x.context,
                                                args: m.args || x.args, result: m.result || "",
                                                exit: m.exit_code ?? null, error: m.error ?? null,
                                                duration: typeof m.duration === "number" ? m.duration : x.duration });
              if (at < 0) return [...s, put({ id, name, done: true })];
              return s.map((x, i) => (i === at ? put(x) : x));
            });
            setModeBoth("thinking");
            scheduleHide(WINDOW_LINGER);          // работа кончилась — окна уходят сами
          }
          break;
        }
        case "speaking":
          if (m.mime) mimeRef.current = m.mime;
          if (typeof m.turn === "number" && m.turn >= turnFloorRef.current) {
            if (activeTurnRef.current !== null && m.turn > activeTurnRef.current) {
              stopAfterPhraseRef.current = false;   // новый ход: мягкий стоп снят
            }
            activeTurnRef.current = m.turn;         // это ход, который сейчас играем
          }
          if (m.filler) {
            // Заглушка вместо тишины: показываем её словами под орбом и оставляем
            // «работаю». Раньше панель молча крутила жёлтым — пользователю это читалось
            // как «ничего не происходит».
            cancelHide();
            setHint(String(m.text || ""));
            setModeBoth("thinking");
          } else {
            setHint("");
            setModeBoth("output");
          }
          break;
        case "audio_end":
          if (activeTurnRef.current === null || m.turn !== activeTurnRef.current) break;
          (window as any).__voiceRecv = ((window as any).__voiceRecv || 0) + 1;
          enqueueAudio();
          break;
        case "stop":
          // Сервер решил, что перебивают (услышал ключевое слово): доигрываем текущую
          // фразу и замолкаем. Никакого обрыва на полуслове.
          stopAfterPhrase();
          break;
        case "session_new":
          setReply(""); setTranscript(""); setSteps([]);
          break;
        case "listening":
          pendingRef.current = [];
          setModeBoth("input");
          break;
        case "thinking":
          // Сервер понял, что фраза кончилась, и ушёл распознавать/думать.
          setModeBoth("thinking");
          break;
        case "turn_done":
          // Конец отменённого хода прилетает уже во время нового: чужой конец не должен
          // сбрасывать состояние нового хода (иначе после перебивания панель глохла).
          if (typeof m.turn === "number" && activeTurnRef.current !== null
              && m.turn < activeTurnRef.current) break;
          setBusy(false); setReply(""); setTranscript("");
          setHint("");
          scheduleHide(WINDOW_AFTER_TURN);
          ignoredRef.current = false;      // ответ закончился — микрофон снова главный
          break;
        // Сервер вернулся в «слушаю» (реплику отменили или отвечать не на что) —
        // прошлый распознанный текст под орбом больше не актуален.
        case "idle": setBusy(false); setTranscript(""); setModeBoth("input"); break;
        case "error":
          // Ход сорвался (например, шлюз перезапустили) — панель должна вернуться
          // в «слушаю», а не крутить «думаю» до перезагрузки страницы.
          setBusy(false); setReply("");
          setModeBoth(micOnRef.current ? "input" : "idle");
          break;
      }
    };
    return ws;
  }

  // Очередь озвучки: куски планируются встык по времени аудиоконтекста, поэтому
  // между предложениями нет паузы на декодирование следующего куска.
  const queueRef = useRef<Uint8Array[][]>([]);
  const playingRef = useRef(false);
  const nextStartRef = useRef(0);              // когда должен зазвучать следующий кусок
  const sourcesRef = useRef<AudioBufferSourceNode[]>([]);
  const activeRef = useRef(0);                 // сколько кусков сейчас играет
  const mimeRef = useRef("audio/wav");
  const runIdRef = useRef(0);          // номер прогона озвучки: перебивание его увеличивает
  const fadeRef = useRef(0);           // кадр плавного затухания уровня
  const activeTurnRef = useRef<number | null>(null);  // ход, чьё аудио сейчас принимаем
  const turnFloorRef = useRef(0);                     // ходы ниже этого — уже отменённые

  /** Отменяем приём аудио: хвост ответа, который уже летит к нам, играть нельзя.
   *  Порог поднимаем ТОЛЬКО когда ход реально в полёте. Иначе он уезжает вперёд нумерации
   *  сервера (счётчик ходов живёт на соединение и не сбрасывается), следующий ответ
   *  оказывается ниже порога — и панель молча выбрасывает и аудио, и audio_end: «нет звука
   *  до перезагрузки страницы». */
  function dropLateAudio() {
    if (activeTurnRef.current === null) return;    // нечего отменять — порог не трогаем
    turnFloorRef.current = activeTurnRef.current + 1;
    activeTurnRef.current = null;
  }
  const analyserRef = useRef<AnalyserNode | null>(null);
  const levelLoopRef = useRef(0);

  /** Общий анализатор громкости для всей озвучки. */
  function ensureAnalyser(ctx: AudioContext): AnalyserNode {
    if (!analyserRef.current) {
      const a = ctx.createAnalyser();
      a.fftSize = 512;
      a.connect(ensureMaster(ctx));
      analyserRef.current = a;
    }
    return analyserRef.current;
  }

  /** Одна петля чтения уровня (раньше их было по одной на кусок — они мешали друг другу). */
  function startLevels(ctx: AudioContext) {
    if (levelLoopRef.current) return;
    const analyser = ensureAnalyser(ctx);
    const data = new Float32Array(analyser.fftSize);
    let smooth = 0;                    // сглаживание: быстрый подъём, плавный спад
    const tick = () => {
      if (!speakingRef.current) {
        levelLoopRef.current = 0;
        fadeOutput();                  // гасим плавно, иначе облачко дёргается на перебивании
        return;
      }
      if (fadeRef.current) {           // пошла речь — отменяем затухание
        cancelAnimationFrame(fadeRef.current);
        fadeRef.current = 0;
      }
      analyser.getFloatTimeDomainData(data);
      let sum = 0;
      for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
      const rms = Math.sqrt(sum / data.length);
      (window as any).__voiceRmsRaw = rms;        // сырая громкость, для подбора усиления
      // Громкость речи доходит примерно до 0.16, поэтому одной линейной шкалы мало:
      // тихие места тонули в нуле и облачко почти не шевелилось. Корень растягивает
      // тихое и поджимает громкое — движение идёт за голосом continuously.
      const raw = Math.sqrt(Math.min(1, rms * 6));
      smooth = raw;                                // сглаживает уже сам орб (attack/release)
      levels.current.output = smooth;
      (window as any).__voiceLevel = smooth;   // для проверки, что уровень живой
      levelLoopRef.current = requestAnimationFrame(tick);
    };
    levelLoopRef.current = requestAnimationFrame(tick);
  }

  /** Плавное затухание уровня: резкий ноль на перебивании выглядит рывком. */
  function fadeOutput() {
    if (fadeRef.current) return;
    const step = () => {
      levels.current.output *= 0.82;
      if (levels.current.output > 0.012) {
        fadeRef.current = requestAnimationFrame(step);
      } else {
        levels.current.output = 0;
        fadeRef.current = 0;
      }
    };
    fadeRef.current = requestAnimationFrame(step);
  }

  /** Ход отзвучал целиком: сообщаем серверу «агент договорил». От этого момента сервер
   *  считает окно диалога, поэтому продолжать разговор можно сразу, без ключевого слова. */
  function turnAudioDone() {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "turn_audio_done" })); } catch { /* сокет закрылся */ }
    }
  }

  function enqueueAudio() {
    const chunks = pendingRef.current;
    pendingRef.current = [];
    if (!chunks.length) return;
    if (stopAfterPhraseRef.current) return;   // перебили: остаток ответа не играем
    queueRef.current.push(chunks);
    void drainQueue();
  }

  async function drainQueue() {
    if (playingRef.current) return;
    const runId = ++runIdRef.current;      // прогон: перебивание увеличивает его и отменяет этот
    playingRef.current = true;
    const ctx = audioCtxRef.current || (audioCtxRef.current = new AudioContext());
    // Цикл живёт, пока играет хоть один кусок: аудио приходит пачками уже во время
    // воспроизведения, и если выйти из цикла на пустой очереди — остаток ответа
    // молча теряется (именно так ломалось воспроизведение).
    for (;;) {
      if (!queueRef.current.length) {
        if (activeRef.current === 0) break;
        await new Promise((r) => setTimeout(r, 40));
        continue;
      }
      const chunks = queueRef.current.shift()!;
      try {
        const blob = new Blob(chunks as BlobPart[], { type: mimeRef.current });
        const buf = await blob.arrayBuffer();
        if (runIdRef.current !== runId) break;     // перебили, пока шло декодирование
        const decoded = await ctx.decodeAudioData(buf);
        if (runIdRef.current !== runId) break;     // ещё раз проверили после декодирования
        if (fadeRef.current) {                     // начинаем говорить — затухание не нужно
          cancelAnimationFrame(fadeRef.current);
          fadeRef.current = 0;
        }
        const src = ctx.createBufferSource();
        src.buffer = decoded;
        // Один общий анализатор на всю озвучку: отдельный на каждый кусок затирал
        // уровень нулём, когда предыдущий кусок отзвучал (облачко замирало).
        src.connect(ensureAnalyser(ctx));
        // Встык: начало = конец предыдущего куска (если он ещё не отзвучал),
        // иначе — сразу. Так стык не зависит от скорости декодирования.
        const start = Math.max(ctx.currentTime + 0.03, nextStartRef.current);
        nextStartRef.current = start + decoded.duration;
        activeRef.current += 1;
        (window as any).__voicePlayed = ((window as any).__voicePlayed || 0) + 1;
        src.onended = () => {
          activeRef.current = Math.max(0, activeRef.current - 1);
          if (activeRef.current === 0) {
            speakingRef.current = false;
            levels.current.speaking = 0;
            fadeOutput();                          // плавно гасим, без рывка на стыке фраз
          }
        };
        speakingRef.current = true;
        levels.current.speaking = 1;           // анимация говорит ровно пока идёт звук
        setModeBoth("output");
        setModeBoth("output");
        sourcesRef.current.push(src);
        src.start(start);
        startLevels(ctx);
      } catch {
        speakingRef.current = false;
        levels.current.speaking = 0;
        (window as any).__voiceFailed = ((window as any).__voiceFailed || 0) + 1;
      }
    }
    if (runIdRef.current === runId) {
      playingRef.current = false;
      nextStartRef.current = 0;
      sourcesRef.current = [];
      ignoredRef.current = false;          // озвучка кончилась — снимаем защиту от эха
      levels.current.speaking = 0;
      if (modeRef.current === "output") setModeBoth("input");
      // Ход закончился: возвращаем отсчёт окна диалога в ноль — «агент работает» это
      // живой диалог, ключевое слово тут требовать нельзя. Сервер продлевает окно с
      // этого момента, поэтому продолжать можно сразу после ответа без обращения.
      turnAudioDone();
    }
  }

  function stopSpeaking() {
    turnAudioDone();                       // перебили — но диалог жив: окно считаем с этого момента
    runIdRef.current += 1;                 // отменяем всё, что ещё в полёте (перебивание)
    speakingRef.current = false;
    levels.current.speaking = 0;
    queueRef.current = [];
    for (const s of sourcesRef.current) {
      try { s.stop(); } catch { /* уже остановлен */ }
    }
    sourcesRef.current = [];
    activeRef.current = 0;
    nextStartRef.current = 0;
    playingRef.current = false;
    fadeOutput();                          // облачко гаснет плавно, а не прыгает в ноль
  }

  /** Перебили (по слову, решает сервер): доиграть текущую фразу и замолчать.
   *  Мгновенное глушение на полуслове звучало как сбой — пользователь просил давать агенту
   *  договорить фразу. Поэтому не сбрасываем звук, а только обрезаем остаток ответа. */
  function stopAfterPhrase() {
    stopAfterPhraseRef.current = true;
    queueRef.current = [];                 // ещё не начатые куски не играем
    pendingRef.current = [];
    turnAudioDone();                       // диалог жив: окно считаем с этого момента
    if (activeRef.current === 0) {         // ничего не звучит — гасим сразу
      stopAfterPhraseRef.current = false;
      speakingRef.current = false;
      levels.current.speaking = 0;
      fadeOutput();
    }
  }

  /** VAD с ограничением по времени: инициализация onnxruntime иногда не доходит до конца. */
  /** Непрерывный поток микрофона 16 кГц. Границы реплики решает сервер. */
  async function openMic(): Promise<any> {
    const stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        // Дальний микрофон: AEC режет тихую речь как «эхо», шумодав — согласные.
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: true,
      },
    });
    const ctx = audioCtxRef.current
      || (audioCtxRef.current = new AudioContext({ sampleRate: 16000 }));
    await ctx.resume();
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(1024, 1, 1);
    proc.onaudioprocess = (ev) => {
      const w = wsRef.current;
      if (!w || w.readyState !== 1 || !micOnRef.current) return;
      const f = ev.inputBuffer.getChannelData(0);
      const pcm = new Int16Array(f.length);
      for (let i = 0; i < f.length; i++) {
        const v = Math.max(-1, Math.min(1, f[i]));
        pcm[i] = v < 0 ? (v * 0x8000) | 0 : (v * 0x7fff) | 0;
      }
      w.send(pcm.buffer);
    };
    src.connect(proc);
    proc.connect(ctx.destination);   // ScriptProcessor работает только с выходом
    return {
      start() {},
      destroy() {
        try { proc.disconnect(); } catch { /* уже */ }
        try { src.disconnect(); } catch { /* уже */ }
        stream.getTracks().forEach((t) => t.stop());
      },
    };
  }

  async function startMic() {
    cancelHide();
    setHint("поднимаю микрофон…");
    const ws = ensureWs();
    let mic: any = null;
    try {
      mic = await openMic();
    } catch (e) {
      console.warn("[mic] микрофон не поднялся:", e);
    }
    if (!mic) {
      setHint("микрофон не поднялся — нажми ещё раз");
      window.setTimeout(() => setHint(""), 5000);
      setModeBoth("idle");
      return;
    }
    micRef.current = mic;
    micOnRef.current = true;
    setMicOn(true);
    mic.start();
    setHint(h => (h === "поднимаю микрофон…" ? "" : h));
    const sendStart = () => {
      if (ws.readyState === 1) {
        ws.send(JSON.stringify({ type: "start" }));
        ws.send(JSON.stringify({ type: "warm" }));
      }
    };
    if (ws.readyState === 1) sendStart();
    else ws.addEventListener("open", sendStart, { once: true });
    setModeBoth("input");
  }

  async function stopMic() {
    setMicOn(false); micOnRef.current = false;
    micRef.current?.destroy?.();
    micRef.current = null;
    stopSpeaking();
    closingRef.current = true;
    wsRef.current?.close();
    wsRef.current = null;
    // Распознанный текст живёт под орбом ровно столько, сколько слушает микрофон.
    // Раньше после выключения он оставался висеть до перезагрузки страницы: финал хода
    // не приходит (соединение уже закрыто), а чистить его было некому.
    setTranscript(""); setHint("");
    setModeBoth("idle");
  }

  /** Возвращает микрофон к жизни после обрыва сокета: иначе панель глохнет навсегда
   *  (кнопка горит, орб синий, на фразы не реагирует). */
  async function restoreMicCapture() {
    if (restartingRef.current || !micOnRef.current) return;
    restartingRef.current = true;
    try {
      micRef.current?.destroy?.();     // старый поток держит мёртвый сокет
      micRef.current = null;
      speechActiveRef.current = false;
      ignoredRef.current = false;
      preRollRef.current = [];
      await new Promise((r) => window.setTimeout(r, 600));
      if (!micOnRef.current) return;
      ensureWs();                      // поднимем новый сокет, если старый мёртв
      await startMic();                // VAD переподнимется уже на живом сокете
      micRestoreTriesRef.current = 0;
      console.log("[микрофон] захват переподнят после обрыва сокета");
    } catch (e) {
      micRestoreTriesRef.current += 1;
      console.warn(`[микрофон] переподнять не удалось (попытка ${micRestoreTriesRef.current}):`, e);
      if (micRestoreTriesRef.current <= 4 && micOnRef.current) {
        window.setTimeout(() => void restoreMicCapture(), 1200 * micRestoreTriesRef.current);
      } else {
        setHint("микрофон отвалился — выключи и включи кнопку");
        window.setTimeout(() => setHint(""), 6000);
        setModeBoth("idle");
      }
    } finally {
      restartingRef.current = false;
    }
  }

  useEffect(() => {
    // Сторож сокета: полуоткрытый сокет (сервер ушёл, а TCP ещё держится) событием
    // onclose не поймать — панель молча глохнет. Пингуем и переподключаемся первыми.
    const t = window.setInterval(() => {
      if (!micOnRef.current) return;
      const ws = wsRef.current;
      if (!ws || ws.readyState !== 1) return;
      if (performance.now() - lastMsgRef.current > 45_000) {
        console.warn("[сокет] сервер молчит 45 с — переподключаемся");
        try { ws.close(); } catch { /* уже закрыт */ }
        return;
      }
      try { ws.send(JSON.stringify({ type: "ping" })); } catch { /* закрыт */ }
    }, 15_000);
    return () => window.clearInterval(t);
  }, []);

  useEffect(() => {
    // Страницу открыли — сообщаем серверу: пора поднять модели распознавания и синтеза
    // (до этого не конкурирует с первой репликой).
    void fetch("/warm").catch(() => {});
    ensureWs();                        // сразу знаем список моделей, подпись не пустая
    // Заготовки для VAD: предзагрузка в кэш браузера срезает часть задержки на клик по орбу.
    void fetch("/voice-assets/silero_vad_v5.onnx", { cache: "force-cache" }).catch(() => {});
  }, []);

  useEffect(() => () => { void stopMic(); }, []);

  // Сторож тишины: сервер во время работы подкидывает фразы, поэтому долгое
  // молчание в «думаю» — это потерянный ход, а не долгая работа.
  useEffect(() => {
    if (!busy) return;
    const t = window.setInterval(() => {
      if (performance.now() - lastMsgRef.current > 180_000) {
        lastMsgRef.current = performance.now();
        setBusy(false); setReply("");
        setModeBoth(micOnRef.current ? "input" : "idle");
      }
    }, 10_000);
    return () => window.clearInterval(t);
  }, [busy]);

  // Журнал действий: новый вывод всегда на виду.
  useEffect(() => {
    const el = workLogRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [steps]);

  const shownMode: Mode = mode;
  const running = micOn;
  const anyPanel = browserOpen || workOpen;

  return (
    <div className="app">
      <section
        className={`voice-stage ${browserOpen ? "is-browsing" : ""} ${workOpen ? "is-terminal" : ""} ${anyPanel ? "is-docked" : ""}`}
        data-mode={shownMode}
        aria-label="Голосовой разговор"
      >
        <header className="voice-stage-header">
          <span className="voice-stage-session" />
          <div className="voice-utilities">
            <button
              type="button"
              className={`voice-mic ${running ? "is-on" : ""}`}
              title={running ? "Выключить микрофон" : "Включить микрофон"}
              aria-pressed={running}
              onClick={() => (running ? void stopMic() : void startMic())}
            >
              {running ? <LuMic /> : <LuMicOff />}
            </button>
            <div className="voice-volume">
              <button type="button" title="Громкость ответов"
                      onClick={() => setVolumeMenu(v => !v)}>
                {volume === 0 ? <LuVolumeX /> : volume < 0.5 ? <LuVolume1 /> : <LuVolume2 />}
              </button>
              {volumeMenu && (
                <>
                  <div className="voice-pop-backdrop" onClick={() => setVolumeMenu(false)} />
                  <div className="voice-volume-panel">
                    <input type="range" min={0} max={100} step={5}
                           value={Math.round(volume * 100)}
                           onChange={(e) => setVolume(Number(e.target.value) / 100)} />
                    <span>{Math.round(volume * 100)}%</span>
                  </div>
                </>
              )}
            </div>
            <button type="button" title="Новая сессия" aria-label="Новая сессия"
                    onClick={() => {
                      stopSpeaking(); dropLateAudio();
                      wsSend({ type: "new_session" });
                      setReply(""); setTranscript(""); setSteps([]);
                    }}><LuMessageSquarePlus /></button>
            <button type="button" title="Показать браузер" aria-label="Показать браузер"
                    onClick={() => setBrowserOpen(v => !v)}><LuGlobe /></button>
            <button type="button" title="Показать работу" aria-label="Показать работу"
                    onClick={() => setWorkOpen(v => !v)}><LuTerminal /></button>
          </div>
        </header>

        <section className={`voice-browser-window ${browserOpen ? "is-open" : ""} ${browserMax ? "is-maximized" : ""}`}
                 aria-label="Живой браузер" aria-hidden={!browserOpen}>
          <header>
            <span><i />Живой браузер</span>
            <div>
              <span className="voice-screen-mode">
                {(["browser", "desktop"] as const).map((m) => (
                  <button key={m} type="button" className={screenMode === m ? "is-current" : ""}
                          title={m === "browser" ? "Показать браузер" : "Показать рабочий стол"}
                          onClick={() => { setScreenMode(m);
                            void fetch(`/browser-mode?mode=${m}`).catch(() => {}); }}>
                    {m === "browser" ? "браузер" : "стол"}
                  </button>
                ))}
              </span>
              {browserMax
                ? <button type="button" title="Обычный размер" onClick={() => setBrowserMax(false)}><LuMinimize2 /></button>
                : <button type="button" title="Развернуть" onClick={() => setBrowserMax(true)}><LuMaximize2 /></button>}
              <button type="button" title="Свернуть" onClick={() => setBrowserOpen(false)}><LuMinus /></button>
            </div>
          </header>
          {browserOpen && (
            <iframe
              src="/vnc/vnc.html?autoconnect=1&resize=remote&path=vnc-ws&show_dot=1&quality=7&compression=2"
              title="Экран браузера, в котором я работаю"
              allow="clipboard-read; clipboard-write"
            />
          )}
        </section>

        <section className={`voice-terminal-window ${workOpen ? "is-open" : ""}`}
                 aria-label="Что я делаю" aria-hidden={!workOpen}>
          <header>
            <span><LuTerminal />Что я делаю</span>
            <div><button type="button" title="Свернуть" onClick={() => setWorkOpen(false)}><LuMinus /></button></div>
          </header>
          {workOpen && (
            <div className="work-log" ref={workLogRef}>
              {steps.length === 0 && <p className="work-empty">Пока ничего не запускал.</p>}
              {steps.map((s, i) => (
                <div key={s.id || i} className={`work-step ${s.done ? "is-done" : "is-running"}`}>
                  <span className="work-name">
                    {s.done ? "✓" : "▸"} {s.name}
                    {s.done && typeof s.duration === "number" && <em> · {s.duration.toFixed(1)} с</em>}
                    {s.done && typeof s.exit === "number" && <em> · выход {s.exit}</em>}
                    {s.done && s.error && <em className="work-bad"> · ошибка</em>}
                  </span>
                  {(s.context || s.args) && <pre>{humanArgs(s)}</pre>}
                  {s.error && <pre className="work-error">{s.error}</pre>}
                  {s.result && <pre className="work-result">{s.result}</pre>}
                </div>
              ))}
            </div>
          )}
        </section>

        <div className="voice-presence">
          {/* Орб — это и есть кнопка микрофона: нажал на шар — начал слушать, нажал ещё раз — замолчал. */}
          <div className="voice-avatar" role="button" tabIndex={0} aria-pressed={running}
               title={running ? "Выключить микрофон" : "Включить микрофон"}
               onClick={() => (running ? void stopMic() : void startMic())}
               onKeyDown={(e) => {
                 if (e.key === "Enter" || e.key === " ") {
                   e.preventDefault();
                   running ? void stopMic() : void startMic();
                 }
               }}>
            <VoiceOrb mode={shownMode} levels={levels} look={{ ...DEFAULT_ORB, reactivity: 1.25 }} />
          </div>
          {/* В свёрнутой плашке своя реплика уходит: там мало места, и важно, что сказал агент. */}
          {transcript && (!anyPanel || !reply) && <p className="voice-live-transcript voice-live-user">{transcript}</p>}
          {hint && !reply && <p className="voice-live-transcript voice-live-reply" style={{ opacity: .75, fontStyle: "italic" }}>{hint}</p>}
          {reply && <p className="voice-live-transcript voice-live-reply" style={{ opacity: .55 }}>{reply}</p>}

        </div>
      </section>
    </div>
  );
}
