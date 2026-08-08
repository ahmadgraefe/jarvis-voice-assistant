// Jarvis — Mobile-Companion
// Gleiches Gehirn wie main.js (derselbe /ws-Endpunkt, dieselbe
// process_message_native()-Pipeline, dasselbe Gedaechtnis) — nur eine
// andere Oberflaeche: Text + Push-to-Talk statt Dauer-Zuhoeren (iOS Safari
// unterstuetzt Dauer-Spracherkennung nicht zuverlaessig, und Dauer-Zuhoeren
// waere auf einem mobilen Akku ohnehin die falsche Wahl).

const canvas = document.getElementById('orb-canvas');
const ctx = canvas.getContext('2d');
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

function getAudioLevel() {
    if (!analyser || !audioLevelData) return 0;
    analyser.getByteFrequencyData(audioLevelData);
    let sum = 0;
    for (let i = 0; i < audioLevelData.length; i++) sum += audioLevelData[i];
    return Math.min(1, (sum / audioLevelData.length) / 110);
}

document.addEventListener('touchstart', ensureAudioContext, { once: true });
document.addEventListener('click', ensureAudioContext, { once: true });

// ---------------------------------------------------------------------------
// Orb renderer — identisch zu main.js (reiner Canvas-Code, keine Kopplung an
// Dauer-Zuhoeren) mit der genau gleichen Optik wie am Mac.
// ---------------------------------------------------------------------------
const Orb = (() => {
    let W = 0, H = 0, CX = 0, CY = 0, DPR = 1;
    let R = 1;

    const STATES = {
        idle:       { speed: 0.05, energy: 0.32, core: 0.85, jitter: 0.10 },
        listening:  { speed: 0.09, energy: 0.55, core: 1.00, jitter: 0.22 },
        thinking:   { speed: 0.22, energy: 0.75, core: 1.05, jitter: 0.45 },
        speaking:   { speed: 0.14, energy: 0.65, core: 1.10, jitter: 0.30 },
        announcing: { speed: 0.10, energy: 0.70, core: 1.15, jitter: 0.20 },
    };
    let target = STATES.idle;
    let cur = { speed: 0.05, energy: 0.32, core: 0.85, jitter: 0.10 };

    let yaw = 0, pitch = 0.35;
    let t = 0;

    const PARTICLE_COUNT = 260;
    const particles = [];
    (function initParticles() {
        const offset = 2 / PARTICLE_COUNT;
        const increment = Math.PI * (3 - Math.sqrt(5));
        for (let i = 0; i < PARTICLE_COUNT; i++) {
            const y = (i * offset) - 1 + offset / 2;
            const r = Math.sqrt(Math.max(0, 1 - y * y));
            const phi = i * increment;
            particles.push({ x: Math.cos(phi) * r, y, z: Math.sin(phi) * r, tw: Math.random() * Math.PI * 2 });
        }
    })();

    const MERIDIANS = 10, MERIDIAN_STEPS = 40;
    const PARALLELS = 5, PARALLEL_STEPS = 48;
    const meridianLines = [];
    const parallelLines = [];
    (function initGrid() {
        for (let m = 0; m < MERIDIANS; m++) {
            const lon = (m / MERIDIANS) * Math.PI * 2;
            const line = [];
            for (let s = 0; s <= MERIDIAN_STEPS; s++) {
                const lat = (s / MERIDIAN_STEPS) * Math.PI - Math.PI / 2;
                line.push({
                    x: Math.cos(lat) * Math.cos(lon),
                    y: Math.sin(lat),
                    z: Math.cos(lat) * Math.sin(lon),
                });
            }
            meridianLines.push(line);
        }
        for (let p = 1; p < PARALLELS; p++) {
            const lat = (p / PARALLELS) * Math.PI - Math.PI / 2;
            const line = [];
            for (let s = 0; s <= PARALLEL_STEPS; s++) {
                const lon = (s / PARALLEL_STEPS) * Math.PI * 2;
                line.push({
                    x: Math.cos(lat) * Math.cos(lon),
                    y: Math.sin(lat),
                    z: Math.cos(lat) * Math.sin(lon),
                });
            }
            parallelLines.push(line);
        }
    })();

    const RING_RADIUS = 1.32;
    const RING_STEPS = 64;
    const ringDefs = [
        { tiltX: 1.05, tiltZ: 0.35, speedMul: 1.0 },
        { tiltX: -0.85, tiltZ: -0.6, speedMul: -0.8 },
    ];
    function ringPoints() {
        const pts = [];
        for (let i = 0; i <= RING_STEPS; i++) {
            const a = (i / RING_STEPS) * Math.PI * 2;
            pts.push({ x: Math.cos(a) * RING_RADIUS, y: Math.sin(a) * RING_RADIUS, z: 0 });
        }
        return pts;
    }
    const baseRing = ringPoints();

    function rotX(p, a) {
        const c = Math.cos(a), s = Math.sin(a);
        return { x: p.x, y: p.y * c - p.z * s, z: p.y * s + p.z * c };
    }
    function rotY(p, a) {
        const c = Math.cos(a), s = Math.sin(a);
        return { x: p.x * c + p.z * s, y: p.y, z: -p.x * s + p.z * c };
    }
    function rotZ(p, a) {
        const c = Math.cos(a), s = Math.sin(a);
        return { x: p.x * c - p.y * s, y: p.x * s + p.y * c, z: p.z };
    }

    const CAM_DIST = 3.4;
    function project(p) {
        const factor = CAM_DIST / (CAM_DIST + p.z);
        return { x: CX + p.x * factor * R, y: CY + p.y * factor * R, f: factor };
    }

    function resize() {
        const rect = canvas.getBoundingClientRect();
        DPR = Math.min(2, window.devicePixelRatio || 1);
        canvas.width = rect.width * DPR;
        canvas.height = rect.height * DPR;
        ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
        W = rect.width; H = rect.height;
        CX = W / 2; CY = H / 2;
        R = Math.min(W, H) * 0.30;
    }
    window.addEventListener('resize', resize);

    function setState(name) {
        target = STATES[name] || STATES.idle;
    }

    function drawLine(points, color, width) {
        ctx.beginPath();
        let started = false;
        for (const p of points) {
            const rp = rotZ(rotX(rotY(p, yaw), pitch), 0);
            const proj = project(rp);
            if (!started) { ctx.moveTo(proj.x, proj.y); started = true; }
            else ctx.lineTo(proj.x, proj.y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = width;
        ctx.stroke();
    }

    function frame() {
        t += 1;
        for (const k in cur) cur[k] += (target[k] - cur[k]) * 0.05;

        const audioLevel = isPlaying ? getAudioLevel() : 0;
        const energy = cur.energy + audioLevel * 0.5;
        const coreScale = cur.core * (1 + audioLevel * 0.55);

        yaw += 0.006 * (1 + cur.speed * 6) + audioLevel * 0.01;
        pitch = 0.35 + Math.sin(t * 0.004) * 0.06;

        ctx.clearRect(0, 0, W, H);

        const glow = ctx.createRadialGradient(CX, CY, R * 0.1, CX, CY, R * 2.2);
        glow.addColorStop(0, `rgba(255,150,40,${0.16 * energy})`);
        glow.addColorStop(1, 'rgba(255,150,40,0)');
        ctx.fillStyle = glow;
        ctx.fillRect(0, 0, W, H);

        const rayCount = 14;
        for (let i = 0; i < rayCount; i++) {
            const a = (i / rayCount) * Math.PI * 2 + yaw * 0.6;
            const flicker = 0.5 + 0.5 * Math.sin(t * 0.05 + i * 1.7);
            const len = R * (1.6 + flicker * 1.4 + energy * 1.2 + audioLevel * 1.8);
            const x2 = CX + Math.cos(a) * len;
            const y2 = CY + Math.sin(a) * len * 0.62;
            const grad = ctx.createLinearGradient(CX, CY, x2, y2);
            const alpha = 0.22 * energy * flicker;
            grad.addColorStop(0, `rgba(255,210,140,${alpha})`);
            grad.addColorStop(1, 'rgba(255,150,40,0)');
            ctx.strokeStyle = grad;
            ctx.lineWidth = 1.2;
            ctx.beginPath();
            ctx.moveTo(CX, CY);
            ctx.lineTo(x2, y2);
            ctx.stroke();
        }

        const gridAlpha = 0.16 + energy * 0.12;
        for (const line of meridianLines) drawLine(line, `rgba(255,178,90,${gridAlpha})`, 1);
        for (const line of parallelLines) drawLine(line, `rgba(255,178,90,${gridAlpha * 0.8})`, 1);

        ringDefs.forEach((def, idx) => {
            const ringYaw = yaw * def.speedMul + idx * 1.4;
            const pts = baseRing.map(p => rotZ(rotX(rotY(p, ringYaw), def.tiltX), def.tiltZ));
            drawLine(pts, `rgba(255,196,110,${0.35 + energy * 0.25})`, 1.4);
        });

        for (const p of particles) {
            const rp = rotX(rotY(p, yaw), pitch);
            const proj = project(rp);
            const depth = (rp.z + 1) / 2;
            const tw = 0.5 + 0.5 * Math.sin(t * 0.03 + p.tw);
            const size = (0.6 + depth * 1.4) * (0.7 + cur.jitter * tw);
            const alpha = (0.15 + depth * 0.55) * (0.5 + energy * 0.5);
            ctx.beginPath();
            ctx.arc(proj.x, proj.y, size, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(255,214,160,${alpha})`;
            ctx.fill();
        }

        const coreR = R * 0.34 * coreScale;
        const core = ctx.createRadialGradient(CX, CY, 0, CX, CY, coreR);
        core.addColorStop(0, 'rgba(255,250,235,0.95)');
        core.addColorStop(0.35, `rgba(255,205,120,${0.85})`);
        core.addColorStop(1, 'rgba(255,150,40,0)');
        ctx.fillStyle = core;
        ctx.beginPath();
        ctx.arc(CX, CY, coreR, 0, Math.PI * 2);
        ctx.fill();

        requestAnimationFrame(frame);
    }

    return { resize, setState, start: () => { resize(); requestAnimationFrame(frame); } };
})();

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
    sendText(textInput.value);
    textInput.value = '';
});
textInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
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
        Orb.setState('idle');
        status.textContent = '';
        return;
    }
    isPlaying = true;
    const next = audioQueue.shift();
    Orb.setState(next.proactive ? 'announcing' : 'speaking');
    status.textContent = next.proactive ? 'Jarvis meldet sich...' : '';

    const bytes = Uint8Array.from(atob(next.audio), c => c.charCodeAt(0));
    const blob = new Blob([bytes], { type: 'audio/mpeg' });
    const url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;

    try {
        ensureAudioContext();
        if (audioCtx) {
            const src = audioCtx.createMediaElementSource(audio);
            src.connect(analyser);
        }
    } catch (e) { /* non-fatal */ }

    audio.onended = () => { URL.revokeObjectURL(url); if (currentAudio === audio) currentAudio = null; playNext(); };
    audio.onerror = () => { URL.revokeObjectURL(url); if (currentAudio === audio) currentAudio = null; playNext(); };
    audio.play().catch(() => {
        status.textContent = 'Tippe irgendwo damit Jarvis sprechen kann.';
        document.addEventListener('click', function retry() {
            document.removeEventListener('click', retry);
            audio.play().catch(() => playNext());
        }, { once: true });
    });
}

canvas.addEventListener('click', () => {
    if (isPlaying) {
        if (currentAudio) currentAudio.pause();
        audioQueue = [];
        isPlaying = false;
        Orb.setState('idle');
        status.textContent = '';
    }
});

connect();

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

talkBtn.addEventListener('touchstart', (e) => { e.preventDefault(); startRecording(); }, { passive: false });
talkBtn.addEventListener('touchend', (e) => { e.preventDefault(); stopRecording(); }, { passive: false });
talkBtn.addEventListener('mousedown', startRecording);
talkBtn.addEventListener('mouseup', stopRecording);
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
        pushBtn.classList.add('granted');
        status.textContent = 'Benachrichtigungen aktiv.';
    } catch (e) {
        status.textContent = 'Konnte Benachrichtigungen nicht aktivieren.';
    }
}

pushBtn.addEventListener('click', enablePush);

if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/static/sw.js').catch(() => {});
}
