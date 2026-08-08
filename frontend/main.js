// Jarvis V2 — Frontend
// Fully animated canvas orb (wireframe globe + crossing rings + radiating
// light + glowing core), audio-reactive while Jarvis speaks. No on-screen
// transcript by design — the orb itself is the whole interface.

const canvas = document.getElementById('orb-canvas');
const ctx = canvas.getContext('2d');
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
// Orb renderer — wireframe globe + two crossing tilted rings + radiating
// light beams + particle sparkle + a glowing core. Pure canvas 2D, no deps.
// ---------------------------------------------------------------------------
const Orb = (() => {
    let W = 0, H = 0, CX = 0, CY = 0, DPR = 1;
    let R = 1; // base render radius in px, set on resize

    // Per-state target parameters, smoothly interpolated toward each frame.
    const STATES = {
        idle:       { speed: 0.05, energy: 0.32, core: 0.85, jitter: 0.10 },
        listening:  { speed: 0.09, energy: 0.55, core: 1.00, jitter: 0.22 },
        thinking:   { speed: 0.22, energy: 0.75, core: 1.05, jitter: 0.45 },
        speaking:   { speed: 0.14, energy: 0.65, core: 1.10, jitter: 0.30 },
        // Jarvis meldet sich VON SICH AUS (background_brain-Fund, kein
        // Antwort auf eine Frage) — spuerbar praesenter als normales
        // Sprechen, damit der Unterschied auch ohne hinzuhoeren auffaellt.
        announcing: { speed: 0.10, energy: 0.70, core: 1.15, jitter: 0.20 },
    };
    let target = STATES.idle;
    let cur = { speed: 0.05, energy: 0.32, core: 0.85, jitter: 0.10 };

    let yaw = 0, pitch = 0.35;
    let t = 0;

    // Fibonacci-distributed sphere points (surface sparkle particles)
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

    // Wireframe globe: meridians (longitude) + parallels (latitude)
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

    // Two crossing "orbit" rings, tilted like the reference image
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
        // Smoothly ease current params toward the target state (fluid transitions)
        for (const k in cur) cur[k] += (target[k] - cur[k]) * 0.05;

        const audioLevel = isPlaying ? getAudioLevel() : 0;
        const energy = cur.energy + audioLevel * 0.5;
        const coreScale = cur.core * (1 + audioLevel * 0.55);

        yaw += 0.006 * (1 + cur.speed * 6) + audioLevel * 0.01;
        pitch = 0.35 + Math.sin(t * 0.004) * 0.06;

        ctx.clearRect(0, 0, W, H);

        // Outer ambient glow behind everything
        const glow = ctx.createRadialGradient(CX, CY, R * 0.1, CX, CY, R * 2.2);
        glow.addColorStop(0, `rgba(255,150,40,${0.16 * energy})`);
        glow.addColorStop(1, 'rgba(255,150,40,0)');
        ctx.fillStyle = glow;
        ctx.fillRect(0, 0, W, H);

        // Radiating light rays
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

        // Wireframe globe — meridians
        const gridAlpha = 0.16 + energy * 0.12;
        for (const line of meridianLines) drawLine(line, `rgba(255,178,90,${gridAlpha})`, 1);
        for (const line of parallelLines) drawLine(line, `rgba(255,178,90,${gridAlpha * 0.8})`, 1);

        // Crossing orbit rings
        ringDefs.forEach((def, idx) => {
            const ringYaw = yaw * def.speedMul + idx * 1.4;
            const pts = baseRing.map(p => rotZ(rotX(rotY(p, ringYaw), def.tiltX), def.tiltZ));
            drawLine(pts, `rgba(255,196,110,${0.35 + energy * 0.25})`, 1.4);
        });

        // Surface sparkle particles
        for (const p of particles) {
            const rp = rotX(rotY(p, yaw), pitch);
            const proj = project(rp);
            const depth = (rp.z + 1) / 2; // 0 back .. 1 front
            const tw = 0.5 + 0.5 * Math.sin(t * 0.03 + p.tw);
            const size = (0.6 + depth * 1.4) * (0.7 + cur.jitter * tw);
            const alpha = (0.15 + depth * 0.55) * (0.5 + energy * 0.5);
            ctx.beginPath();
            ctx.arc(proj.x, proj.y, size, 0, Math.PI * 2);
            ctx.fillStyle = `rgba(255,214,160,${alpha})`;
            ctx.fill();
        }

        // Glowing core
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
}

function playNext() {
    if (audioQueue.length === 0) {
        isPlaying = false;
        currentAudio = null;
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
