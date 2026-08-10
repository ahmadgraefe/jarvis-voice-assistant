// Jarvis V2 — Frontend
// WebGL-Orb (orb.js, Three.js) als visuelles Interface, audio-reaktiv waehrend
// Jarvis spricht. No on-screen transcript by design — der Orb ist die
// gesamte Oberflaeche.

const canvas = document.getElementById('orb-canvas');
const status = document.getElementById('status');

let ws;
let audioQueue = [];
let isPlaying = false;
let currentAudio = null;
let audioUnlocked = false;
let pausedByUser = false;

// Ahmad (2026-08-07): "er soll wirklich nur aufwachen wenn ich doppeltklatsche"
// — the mic used to just listen forever once a session started (continuous
// recognition auto-restarts itself), so ambient talk near an already-open-
// but-unfocused Jarvis tab could trigger a response with no clap at all.
// After SLEEP_TIMEOUT_MS of no real interaction, the mic goes fully quiet;
// only the double-clap (which brings this window to front, see
// launch-session.sh) wakes it again, via the focus/visibilitychange
// listeners below — not a timer, not ambient noise.
let sleeping = false;
let lastActivityTime = Date.now();
const SLEEP_TIMEOUT_MS = 90 * 1000;

// ---------------------------------------------------------------------------
// Audio analysis — feeds real playback amplitude into the orb while speaking
// ---------------------------------------------------------------------------
let audioCtx = null;
let analyser = null;
let audioLevelData = null;

function ensureAudioContext() {
    if (audioCtx) return;
    try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.75;
        audioLevelData = new Uint8Array(analyser.frequencyBinCount);
        analyser.connect(audioCtx.destination);
        window.__jarvisAudioAnalyser = analyser; // orb.js liest hier mit (kein zweiter Tap moeglich, siehe orb.js)
    } catch (e) {
        console.warn('[jarvis] AudioContext unavailable', e);
    }
}

function getAudioLevel() {
    if (!analyser || !audioLevelData) return 0;
    analyser.getByteFrequencyData(audioLevelData);
    let sum = 0;
    for (let i = 0; i < audioLevelData.length; i++) sum += audioLevelData[i];
    return Math.min(1, (sum / audioLevelData.length) / 110);
}

// Unlock audio + init AudioContext on ANY user interaction
function unlockAudio() {
    ensureAudioContext();
    if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume();
    if (!audioUnlocked) {
        const silent = new Audio('data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA');
        silent.play().then(() => {
            audioUnlocked = true;
            console.log('[jarvis] Audio unlocked');
        }).catch(() => {});
    }
}
document.addEventListener('click', unlockAudio, { once: false });
document.addEventListener('touchstart', unlockAudio, { once: false });
document.addEventListener('keydown', unlockAudio, { once: false });

// ---------------------------------------------------------------------------
// Orb renderer — jetzt in orb.js (WebGL/Three.js), als <script type="module">
// vor diesem Skript geladen (siehe index.html) und global als window.Orb
// verfuegbar, exakt gleiche Schnittstelle (setState/start/resize) wie vorher.
// ---------------------------------------------------------------------------
Orb.start();

// ---------------------------------------------------------------------------
// WebSocket + speech
// ---------------------------------------------------------------------------
let hasGreeted = false; // only auto-greet on the TRUE first connection — a
// dropped/reconnected WebSocket (server restart, brief wifi hiccup, laptop
// sleep/wake) must NOT silently re-trigger a full unprompted greeting.

function connect() {
    // wss:// wenn die Seite selbst ueber https laeuft (Hetzner-Server via
    // tailscale serve), sonst ws:// — sonst blockt der Browser das als
    // Mixed Content auf einer https-Seite.
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);
    ws.onopen = () => {
        console.log('[jarvis] WebSocket connected');
        if (!hasGreeted) {
            hasGreeted = true;
            status.textContent = 'Klicke einmal irgendwo, dann spricht Jarvis.';
            Orb.setState('thinking');
            ws.send(JSON.stringify({ text: 'Jarvis, lets go' }));
        } else {
            console.log('[jarvis] Reconnect — kein erneutes automatisches Update.');
            status.textContent = '';
            Orb.setState('idle');
            if (!isListening) startListening();
        }
    };
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'response') {
            console.log('[jarvis]', data.text);
            markActivity();
            if (data.audio && data.audio.length > 0) {
                queueAudio(data.audio, !!data.proactive);
            } else {
                Orb.setState('idle');
                if (!isListening) startListening();
            }
        } else if (data.type === 'status') {
            status.textContent = data.text;
        }
    };
    ws.onclose = () => {
        status.textContent = 'Verbindung verloren...';
        setTimeout(connect, 3000);
    };
}

function queueAudio(base64Audio, proactive = false) {
    audioQueue.push({ audio: base64Audio, proactive });
    if (!isPlaying) playNext();
}

// Stops whatever Jarvis is currently saying. Only ever called from a real user
// action (clicking the orb) — never from the mic — so it can't misfire on echo.
function interruptSpeech() {
    if (currentAudio) {
        currentAudio.pause();
        currentAudio = null;
    }
    audioQueue = [];
    isPlaying = false;
    stopBargeInWatch();
}

function playNext() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        currentAudio = null;
        stopBargeInWatch();
        Orb.setState('idle');
        status.textContent = '';
        // Brief cooldown before the mic reopens — catching the tail end of
        // Jarvis's own audio/room echo right as playback ends is exactly how
        // a hot mic talks to itself.
        setTimeout(() => {
            if (!isListening && !isPlaying && !pausedByUser) startListening();
        }, 500);
        return;
    }
    isPlaying = true;
    startBargeInWatch();
    const next = audioQueue.shift();
    const b64 = next.audio;
    if (next.proactive) {
        Orb.setState('announcing');
        status.textContent = 'Jarvis meldet sich...';
    } else {
        Orb.setState('speaking');
        status.textContent = '';
    }
    // Mic stays OFF while Jarvis talks — the Web Speech API's echo cancellation
    // isn't reliable enough to keep it open without him hearing himself. Click
    // the orb to interrupt him instead (see the click handler below).
    if (isListening) {
        recognition.stop();
        isListening = false;
    }

    const bytes = Uint8Array.from(atob(b64), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;

    // Route through the analyser so the orb can react to real playback
    // amplitude — falls back silently if AudioContext isn't ready yet.
    try {
        ensureAudioContext();
        if (audioCtx) {
            const src = audioCtx.createMediaElementSource(audio);
            src.connect(analyser);
        }
    } catch (e) { /* non-fatal — orb just won't be audio-reactive this line */ }

    audio.onended = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        playNext();
    };
    audio.onerror = () => {
        URL.revokeObjectURL(url);
        if (currentAudio === audio) currentAudio = null;
        playNext();
    };
    audio.play().catch(err => {
        console.warn('[jarvis] Autoplay blocked, waiting for click...');
        status.textContent = 'Klicke irgendwo damit Jarvis sprechen kann.';
        Orb.setState('idle');
        // Wait for click then retry
        document.addEventListener('click', function retry() {
            document.removeEventListener('click', retry);
            audio.play().then(() => {
                Orb.setState('speaking');
                status.textContent = '';
            }).catch(() => playNext());
        });
    });
}

// ---------------------------------------------------------------------------
// Barge-in (Roadmap Punkt 20) — waehrend Jarvis spricht bleibt die normale
// SpeechRecognition AUS (Echo-Risiko, siehe playNext), aber ein separater,
// leichtgewichtiger Lautstaerke-Waechter laeuft parallel mit: KEINE
// Spracherkennung, nur "ist da gerade ein echtes, deutlich lauteres Signal
// als das was Jarvis selbst gerade ausgibt". Erkennt er das sustained ueber
// VAD_SUSTAIN_MS, wird sofort unterbrochen und danach erst die echte
// Spracherkennung gestartet (dann in Stille, kein Selbst-Trigger-Risiko
// mehr). Schwelle ist relativ zur eigenen Ausgabelautstaerke (getAudioLevel,
// bereits fuer die Orb-Visualisierung vorhanden) statt eines festen Werts —
// laut sprechende Jarvis-Antworten brauchen dadurch automatisch einen
// lauteren Interrupt als leise, statt bei fester Schwelle staendig falsch
// auszuloesen. Muss live an Ahmads Mikro/Lautsprecher-Setup nachjustiert
// werden, darum alle Zahlen als Konstanten ganz oben in diesem Block.
const VAD_MIN_ABSOLUTE = 0.05;   // Mic-Level muss IMMER mindestens das ueberschreiten
const VAD_BLEED_FACTOR = 1.4;    // wie viel lauter als die erwartete Lautsprecher-Ruecklaufmenge
const VAD_SUSTAIN_MS = 280;      // so lange am Stueck ueber der Schwelle (filtert kurze Stoergeraeusche)
const VAD_POLL_MS = 40;

let vadStream = null;
let vadAnalyser = null;
let vadData = null;
let vadTimer = null;
let vadAboveSince = null;

function vadLevel() {
    if (!vadAnalyser || !vadData) return 0;
    vadAnalyser.getByteFrequencyData(vadData);
    let sum = 0;
    for (let i = 0; i < vadData.length; i++) sum += vadData[i];
    return Math.min(1, (sum / vadData.length) / 110);
}

async function startBargeInWatch() {
    if (vadStream) return; // laeuft schon
    try {
        ensureAudioContext();
        vadStream = await navigator.mediaDevices.getUserMedia({
            audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        const src = audioCtx.createMediaStreamSource(vadStream);
        vadAnalyser = audioCtx.createAnalyser();
        vadAnalyser.fftSize = 256;
        vadAnalyser.smoothingTimeConstant = 0.6;
        vadData = new Uint8Array(vadAnalyser.frequencyBinCount);
        src.connect(vadAnalyser);
        vadAboveSince = null;
        vadTimer = setInterval(pollVad, VAD_POLL_MS);
    } catch (e) {
        console.warn('[jarvis] Barge-in: Mikrofon fuer Lautstaerke-Waechter nicht verfuegbar', e);
    }
}

function stopBargeInWatch() {
    if (vadTimer) { clearInterval(vadTimer); vadTimer = null; }
    if (vadStream) { vadStream.getTracks().forEach(t => t.stop()); vadStream = null; }
    vadAnalyser = null;
    vadData = null;
    vadAboveSince = null;
}

function pollVad() {
    if (!isPlaying || !vadStream) return;
    const mic = vadLevel();
    const speakerLevel = getAudioLevel(); // Jarvis' eigene Ausgabe gerade jetzt
    const threshold = Math.max(VAD_MIN_ABSOLUTE, speakerLevel * VAD_BLEED_FACTOR);
    const now = performance.now();
    if (mic > threshold) {
        if (vadAboveSince === null) vadAboveSince = now;
        if (now - vadAboveSince >= VAD_SUSTAIN_MS) {
            console.log('[jarvis] Barge-in ausgeloest — mic:', mic.toFixed(3), 'schwelle:', threshold.toFixed(3));
            triggerBargeIn();
        }
    } else {
        vadAboveSince = null;
    }
}

function triggerBargeIn() {
    interruptSpeech(); // stoppt auch den VAD-Waechter selbst (siehe dort)
    pausedByUser = false;
    markActivity();
    startListening();
}

// Speech Recognition
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition;
let isListening = false;

if (SpeechRecognition) {
    recognition = new SpeechRecognition();
    recognition.lang = 'de-DE';
    recognition.continuous = true;
    recognition.interimResults = false;

    // Jarvis must only ever respond to things actually said to him — a hot
    // mic in a continuous-listening setup WILL occasionally mishear ambient
    // noise (a door, the Music app, a TV) as a stray short word. Filter those
    // out before they ever reach the server rather than trying to explain
    // them away after the fact.
    // 2, not higher — "Ja" is a real, common one-word reply and must not be
    // filtered out; this only catches single-character noise blips ("h", "äh").
    const MIN_TRANSCRIPT_LENGTH = 2;
    const MIN_CONFIDENCE = 0.5;

    recognition.onresult = (event) => {
        const last = event.results[event.results.length - 1];
        if (!last.isFinal) return;

        const text = last[0].transcript.trim();
        const confidence = last[0].confidence;

        if (!text || text.length < MIN_TRANSCRIPT_LENGTH) {
            console.log('[jarvis] Ignoriert (zu kurz, vermutlich Stoergeraeusch):', JSON.stringify(text));
            return;
        }
        // Some browsers never report a real confidence (always 0 or 1) — only
        // filter on it when it looks like an actual measured value.
        if (typeof confidence === 'number' && confidence > 0 && confidence < MIN_CONFIDENCE) {
            console.log('[jarvis] Ignoriert (niedrige Konfidenz):', text, confidence);
            return;
        }

        console.log('[du]', text);
        markActivity();
        Orb.setState('thinking');
        status.textContent = 'Jarvis denkt nach...';
        ws.send(JSON.stringify({ text }));
    };

    recognition.onend = () => {
        isListening = false;
        if (!pausedByUser && !isPlaying && !sleeping) setTimeout(startListening, 300);
    };

    recognition.onerror = (event) => {
        isListening = false;
        if (pausedByUser || isPlaying || sleeping) return;
        if (event.error === 'no-speech' || event.error === 'aborted') {
            setTimeout(startListening, 300);
        } else {
            setTimeout(startListening, 1000);
        }
    };
}

function startListening() {
    if (pausedByUser || isPlaying || sleeping) return;
    try {
        recognition.start();
        isListening = true;
        Orb.setState('listening');
        status.textContent = '';
    } catch (e) {}
}

function markActivity() {
    lastActivityTime = Date.now();
}

function goToSleep() {
    if (sleeping) return;
    sleeping = true;
    if (isListening) {
        recognition.stop();
        isListening = false;
    }
    Orb.setState('idle');
    status.textContent = '';
    console.log('[jarvis] Eingeschlafen — nur ein Doppelklatschen weckt ihn wieder.');
}

function wakeUp() {
    if (!sleeping) return;
    sleeping = false;
    markActivity();
    if (!pausedByUser && !isPlaying) startListening();
    console.log('[jarvis] Aufgewacht.');
}

setInterval(() => {
    if (!sleeping && !pausedByUser && !isPlaying && Date.now() - lastActivityTime > SLEEP_TIMEOUT_MS) {
        goToSleep();
    }
}, 5000);

// The double-clap (scripts/launch-session.sh) brings this Chrome window to
// the front — that's the ONLY intended way this fires outside a fresh page
// load, so treating focus/visibility as "wake up" matches Ahmad's ask
// without needing a separate signalling channel from clap-trigger.py.
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') wakeUp();
});
window.addEventListener('focus', wakeUp);

canvas.addEventListener('click', () => {
    // A direct click is unambiguous intent — wakes it up same as a clap would.
    if (sleeping) {
        wakeUp();
        return;
    }
    // Jarvis is talking — a click means "stop, I want to say something".
    if (isPlaying) {
        interruptSpeech();
        pausedByUser = false;
        markActivity();
        startListening();
        return;
    }
    if (isListening) {
        pausedByUser = true;
        recognition.stop();
        isListening = false;
        Orb.setState('idle');
        status.textContent = 'Pausiert. Klicke zum Fortsetzen.';
    } else {
        pausedByUser = false;
        markActivity();
        startListening();
    }
});

connect();
