// Jarvis — Mobile-Companion
// Gleiches Gehirn wie main.js (derselbe /ws-Endpunkt, dieselbe
// process_message_native()-Pipeline, dasselbe Gedaechtnis) — nur eine
// andere Oberflaeche: Text + Push-to-Talk statt Dauer-Zuhoeren (iOS Safari
// unterstuetzt Dauer-Spracherkennung nicht zuverlaessig, und Dauer-Zuhoeren
// waere auf einem mobilen Akku ohnehin die falsche Wahl).

const canvas = document.getElementById('orb-canvas');
const status = document.getElementById('status');
const logEl = document.getElementById('mobile-log');
const textInput = document.getElementById('mobile-text');
const sendBtn = document.getElementById('mobile-send');
const talkBtn = document.getElementById('mobile-talk');
const pushBtn = document.getElementById('mobile-enable-push');

let ws;
let audioQueue = [];
let isPlaying = false;
let currentAudio = null;
let audioCtx = null;
let analyser = null;
let audioLevelData = null;

// EIN wiederverwendetes <audio>-Element fuer die eigentliche Sprachausgabe,
// nie fuers Primen angefasst (siehe unten warum das wichtig ist).
const responseAudio = new Audio();

// Separates, IMMER gueltiges stilles Audio-Element NUR zum "Primen" der
// Abspiel-Erlaubnis. Erster Fix (primen direkt auf responseAudio) hat nur
// beim ALLERERSTEN Mal zuverlaessig funktioniert: nach der ersten echten
// Antwort zeigt responseAudio.src auf eine bereits verbrauchte/freigegebene
// Blob-URL, ein erneutes play() darauf schlaegt aus einem ANDEREN Grund
// fehl (ungueltige Quelle) und registriert bei iOS offenbar keine gueltige
// Geste mehr — genau das Muster das Ahmad live gemeldet hat (erste
// Nachricht Ton, danach stumm). Ein separates Element mit einer festen,
// immer abspielbaren stillen Audiospur hat dieses Problem nicht.
const unlockAudioEl = new Audio('data:audio/mp3;base64,SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4Ljc2LjEwMAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA//tQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWGluZwAAAA8AAAACAAABhgC7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7u7//////////////////////////////////////////////////////////////////8AAAAATGF2YzU4LjEzAAAAAAAAAAAAAAAAJAAAAAAAAAAAAYZNIGPkAAAAAAAAAAAAAAAAAAAA');

function primeResponseAudio() {
    try {
        unlockAudioEl.currentTime = 0;
        const p = unlockAudioEl.play();
        if (p && typeof p.catch === 'function') p.catch(() => {});
    } catch (e) { /* non-fatal */ }
}

function ensureAudioContext() {
    if (audioCtx) return;
    try {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        analyser = audioCtx.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.75;
        audioLevelData = new Uint8Array(analyser.frequencyBinCount);
        analyser.connect(audioCtx.destination);
    } catch (e) { /* non-fatal */ }
}

// iOS spielt programmatisch gestartetes Audio (z.B. Jarvis' Antwort NACH
// einem WebSocket-Rundlauf) nur zuverlaessig ab, wenn MOEGLICHST KURZ vorher
// ein Audio-Element innerhalb einer echten Beruehrung/eines Klicks gestartet
// wurde — darum bei JEDER Beruehrung neu primen (siehe primeResponseAudio),
// nicht nur einmalig, die Erlaubnis kann zwischendurch wieder ablaufen.
document.addEventListener('touchstart', primeResponseAudio, { once: false });
document.addEventListener('click', primeResponseAudio, { once: false });

function getAudioLevel() {
    if (!analyser || !audioLevelData) return 0;
    analyser.getByteFrequencyData(audioLevelData);
    let sum = 0;
    for (let i = 0; i < audioLevelData.length; i++) sum += audioLevelData[i];
    return Math.min(1, (sum / audioLevelData.length) / 110);
}

// ---------------------------------------------------------------------------
// Orb renderer — jetzt in orb.js (WebGL/Three.js), als <script type="module">
// vor diesem Skript geladen (siehe mobile.html) und global als window.Orb
// verfuegbar, exakt gleiche Schnittstelle (setState/start/resize) wie vorher.
// Echte Audioreaktivitaet (echte Lautstaerke statt nur Zustand) bleibt hier
// bewusst aus, siehe orb.js-Kommentar: responseAudio darf aus Zuverlaessigkeits-
// Gruenden nicht an einen Web-Audio-Analyser gehaengt werden.
// ---------------------------------------------------------------------------
Orb.start();
// ---------------------------------------------------------------------------
// WebSocket — dieselbe Pipeline wie main.js, kein Dauer-Zuhoeren, kein
// automatischer Begruessungs-Trigger (Ahmad oeffnet die App bewusst, das
// ersetzt das Doppelklatschen).
// ---------------------------------------------------------------------------
function addLog(who, text) {
    const line = document.createElement('div');
    line.className = who === 'you' ? 'you' : 'jarvis';
    line.textContent = (who === 'you' ? 'Du: ' : 'Jarvis: ') + text;
    logEl.appendChild(line);
    logEl.scrollTop = logEl.scrollHeight;
}

function connect() {
    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    ws = new WebSocket(`${wsProtocol}//${location.host}/ws`);
    ws.onopen = () => {
        status.textContent = '';
        Orb.setState('idle');
    };
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'response') {
            if (data.text) addLog('jarvis', data.text);
            if (data.audio && data.audio.length > 0) {
                queueAudio(data.audio, !!data.proactive);
            } else {
                Orb.setState('idle');
                status.textContent = '';
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

function sendText(text) {
    if (!text.trim() || !ws || ws.readyState !== WebSocket.OPEN) return;
    addLog('you', text);
    Orb.setState('thinking');
    status.textContent = 'Jarvis denkt nach...';
    ws.send(JSON.stringify({ text }));
}

sendBtn.addEventListener('click', () => {
    primeResponseAudio();
    sendText(textInput.value);
    textInput.value = '';
});
textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
        primeResponseAudio();
        sendText(textInput.value);
        textInput.value = '';
    }
});

function queueAudio(base64Audio, proactive = false) {
    audioQueue.push({ audio: base64Audio, proactive });
    if (!isPlaying) playNext();
}

function playNext() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        currentAudio = null;
        stopBargeInWatch();
        Orb.setState('idle');
        status.textContent = '';
        return;
    }
    isPlaying = true;
    startBargeInWatch();
    const next = audioQueue.shift();
    Orb.setState(next.proactive ? 'announcing' : 'speaking');
    status.textContent = next.proactive ? 'Jarvis meldet sich...' : '';

    const bytes = Uint8Array.from(atob(next.audio), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    currentAudio = responseAudio;

    // Bewusst OHNE Web-Audio-Graph (kein createMediaElementSource/Analyser)
    // — das hier ist die tatsaechliche Sprachausgabe, die MUSS zuverlaessig
    // hoerbar sein, unabhaengig vom AudioContext-Status. Ausserdem bewusst
    // dasselbe wiederverwendete <audio>-Element (responseAudio) statt bei
    // jeder Antwort ein frisches Audio()-Objekt — siehe primeResponseAudio,
    // deutlich zuverlaessiger bei Push-to-Talk (langer Rundlauf: Aufnahme
    // + Hochladen + Transkription + LLM-Antwort, das Zeitfenster in dem
    // WebKit programmatisches Abspielen noch erlaubt kann dabei ablaufen).
    responseAudio.onended = () => { URL.revokeObjectURL(url); playNext(); };
    responseAudio.onerror = () => { URL.revokeObjectURL(url); playNext(); };
    responseAudio.src = url;
    responseAudio.play().catch((err) => {
        status.textContent = 'Kein Ton (' + (err && err.name ? err.name : 'unbekannt') + '), bitte antippen.';
        document.addEventListener('click', function retry() {
            document.removeEventListener('click', retry);
            responseAudio.play().catch(() => playNext());
        }, { once: true });
    });
}

canvas.addEventListener('click', () => {
    if (isPlaying) {
        stopBargeInWatch();
        if (currentAudio) currentAudio.pause();
        audioQueue = [];
        isPlaying = false;
        Orb.setState('idle');
        status.textContent = '';
    }
});

connect();

// ---------------------------------------------------------------------------
// Barge-in (Roadmap Punkt 20) — gleiches Prinzip wie main.js (Mac): waehrend
// Jarvis spricht laeuft ein leichtgewichtiger Lautstaerke-Waechter mit (KEIN
// echtes Zuhoeren/Transkribieren), erkennt er ein deutlich lauteres Signal
// als Jarvis' eigene Ausgabe gerade produziert, unterbricht er SOFORT — das
// ist rein lokale Audio-Pegel-Messung, kein Server-Umweg, also genauso
// schnell wie am Mac. Danach unterscheidet sich der Ablauf: iOS hat keine
// eingebaute Dauer-Spracherkennung (siehe Datei-Kopf), darum startet nach
// dem Unterbrechen automatisch dieselbe Aufnahme wie beim Push-to-Talk-
// Knopf, stoppt selbststaendig nach einer kurzen Sprechpause, und geht dann
// denselben Weg wie sonst (Hochladen, Transkription, Antwort) — das kostet
// dort ein bis zwei Sekunden extra, das Unterbrechen selbst aber nicht.
// ---------------------------------------------------------------------------
const VAD_MIN_ABSOLUTE = 0.05;
const VAD_BLEED_FACTOR = 1.4;
const VAD_SUSTAIN_MS = 280;
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
    if (vadStream) return;
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
        console.warn('[jarvis] Barge-in: Mikrofon nicht verfuegbar', e);
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
    const speakerLevel = getAudioLevel();
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
    stopBargeInWatch();
    if (currentAudio) currentAudio.pause();
    audioQueue = [];
    isPlaying = false;
    Orb.setState('listening');
    status.textContent = 'Ich hoere...';
    primeResponseAudio();
    startRecording().then(() => {
        talkBtn.textContent = '🔴 Ich hoere zu...'; // kein Knopf gedrueckt, "Loslassen" waere hier irrefuehrend
        watchForSilenceAndAutoStop();
    });
}

// Stoppt die per triggerBargeIn gestartete Aufnahme selbststaendig nach
// einer kurzen Sprechpause, statt auf Loslassen eines Knopfs zu warten (den
// gibt es hier ja nicht) — sonst wuerde MAX_RECORDING_MS als einzige Grenze
// gelten, spuerbar traege.
function watchForSilenceAndAutoStop() {
    setTimeout(() => {
        if (!mediaStream || !mediaRecorder || mediaRecorder.state !== 'recording') return;
        const src = audioCtx.createMediaStreamSource(mediaStream);
        const a = audioCtx.createAnalyser();
        a.fftSize = 256;
        const data = new Uint8Array(a.frequencyBinCount);
        src.connect(a);

        const SILENCE_THRESHOLD = 0.04;
        const SILENCE_HOLD_MS = 1200;
        const MAX_RECORDING_MS = 12000;
        const startedAt = performance.now();
        let heardSpeech = false;
        let silenceSince = null;

        function check() {
            if (!mediaRecorder || mediaRecorder.state !== 'recording') return;
            a.getByteFrequencyData(data);
            let sum = 0;
            for (let i = 0; i < data.length; i++) sum += data[i];
            const level = Math.min(1, (sum / data.length) / 110);
            const now = performance.now();
            if (level > SILENCE_THRESHOLD) {
                heardSpeech = true;
                silenceSince = null;
            } else if (heardSpeech) {
                if (silenceSince === null) silenceSince = now;
                if (now - silenceSince >= SILENCE_HOLD_MS) {
                    stopRecording();
                    return;
                }
            }
            if (now - startedAt >= MAX_RECORDING_MS) {
                stopRecording();
                return;
            }
            requestAnimationFrame(check);
        }
        check();
    }, 50);
}

// ---------------------------------------------------------------------------
// Push-to-Talk — Aufnahme per MediaRecorder, Transkription server-seitig
// (ElevenLabs), Text danach ganz normal ueber denselben WebSocket-Pfad wie
// Tipptext. iOS Safari hat keine zuverlaessige eigene SpeechRecognition-API,
// darum dieser Umweg statt Dauer-Zuhoeren wie am Mac.
// ---------------------------------------------------------------------------
let mediaRecorder = null;
let recordedChunks = [];
let mediaStream = null;

async function startRecording() {
    try {
        ensureAudioContext();
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
        status.textContent = 'Mikrofon-Zugriff verweigert.';
        return;
    }
    recordedChunks = [];
    const mimeType = MediaRecorder.isTypeSupported('audio/webm') ? 'audio/webm' : '';
    mediaRecorder = mimeType ? new MediaRecorder(mediaStream, { mimeType }) : new MediaRecorder(mediaStream);
    mediaRecorder.ondataavailable = (e) => { if (e.data.size > 0) recordedChunks.push(e.data); };
    mediaRecorder.onstop = onRecordingStop;
    mediaRecorder.start();
    talkBtn.classList.add('recording');
    talkBtn.textContent = '🔴 Loslassen zum Senden';
    Orb.setState('listening');
}

function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop();
    if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
    talkBtn.classList.remove('recording');
    talkBtn.textContent = '🎤 Halten zum Sprechen';
}

async function onRecordingStop() {
    if (recordedChunks.length === 0) { Orb.setState('idle'); return; }
    const blob = new Blob(recordedChunks, { type: recordedChunks[0].type || 'audio/webm' });
    status.textContent = 'Transkribiere...';
    Orb.setState('thinking');
    try {
        const form = new FormData();
        form.append('audio', blob, 'recording.webm');
        const res = await fetch('/transcribe', { method: 'POST', body: form });
        const data = await res.json();
        if (data.text && data.text.trim()) {
            sendText(data.text);
        } else {
            status.textContent = 'Nichts verstanden, versuch es nochmal.';
            Orb.setState('idle');
        }
    } catch (e) {
        status.textContent = 'Transkription fehlgeschlagen.';
        Orb.setState('idle');
    }
}

talkBtn.addEventListener('touchstart', (e) => { e.preventDefault(); primeResponseAudio(); startRecording(); }, { passive: false });
talkBtn.addEventListener('touchend', (e) => { e.preventDefault(); primeResponseAudio(); stopRecording(); }, { passive: false });
talkBtn.addEventListener('mousedown', () => { primeResponseAudio(); startRecording(); });
talkBtn.addEventListener('mouseup', () => { primeResponseAudio(); stopRecording(); });
talkBtn.addEventListener('mouseleave', () => { if (mediaRecorder && mediaRecorder.state === 'recording') stopRecording(); });

// ---------------------------------------------------------------------------
// Push-Benachrichtigungen — Service Worker registrieren, Abo anlegen, an den
// Server schicken. Auf iOS nur wirksam wenn die Seite ueber "Zum Home-
// Bildschirm hinzufuegen" installiert wurde (Safari-Tabs bekommen keine
// Push-Benachrichtigungen, iOS 16.4+).
// ---------------------------------------------------------------------------
function urlBase64ToUint8Array(base64String) {
    const padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    const base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    const rawData = atob(base64);
    return Uint8Array.from([...rawData].map((c) => c.charCodeAt(0)));
}

async function subscribeAndSend(reg) {
    const keyRes = await fetch('/push/vapid-public-key');
    const { key } = await keyRes.json();
    const sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(key),
    });
    await fetch('/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sub),
    });
    return sub;
}

async function enablePush() {
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
        status.textContent = 'Push wird von diesem Browser nicht unterstuetzt.';
        return;
    }
    try {
        const reg = await navigator.serviceWorker.register('/static/sw.js');
        const perm = await Notification.requestPermission();
        if (perm !== 'granted') {
            status.textContent = 'Benachrichtigungen wurden nicht erlaubt.';
            return;
        }
        await subscribeAndSend(reg);
        pushBtn.classList.add('granted');
        status.textContent = 'Benachrichtigungen aktiv.';
    } catch (e) {
        status.textContent = 'Konnte Benachrichtigungen nicht aktivieren.';
    }
}

pushBtn.addEventListener('click', enablePush);

// Beim Start pruefen ob bereits alles eingerichtet ist (Erlaubnis erteilt
// UND ein Abo existiert bereits) — sonst musste Ahmad nach JEDEM
// Neu-Oeffnen der App wieder auf "Benachrichtigungen aktivieren" tippen,
// obwohl die Erlaubnis vom Betriebssystem her laengst besteht. Erlaubnis
// selbst NUR bei einem echten Tap auf den Button abfragen (siehe
// enablePush) — requestPermission() OHNE Geste wuerde iOS ohnehin
// ignorieren/verweigern.
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').then(async (reg) => {
        if (Notification.permission !== 'granted') return; // noch nie erlaubt, Button bleibt sichtbar
        try {
            let sub = await reg.pushManager.getSubscription();
            if (!sub) {
                // Erlaubnis besteht, aber (noch) kein Abo bei uns hinterlegt
                // (z.B. nach Server-Neustart/Datenverlust) — im Hintergrund
                // still nachholen, ohne Ahmad nochmal zu fragen.
                sub = await subscribeAndSend(reg);
            } else {
                // Abo existiert bereits lokal, sicherheitshalber erneut an
                // den Server melden (dedupliziert dort ueber den endpoint,
                // kein Duplikat) — faengt einen Fall ab wo der Server das
                // Abo verloren hat, obwohl der Browser es noch kennt.
                await fetch('/push/subscribe', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(sub),
                }).catch(() => {});
            }
            pushBtn.classList.add('granted');
        } catch (e) { /* still lassen, Button bleibt als Fallback sichtbar */ }
    }).catch(() => {});
}
