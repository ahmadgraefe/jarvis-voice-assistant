// Jarvis — WebGL-Orb (ersetzt den alten Canvas2D-Orb aus main.js/mobile.js)
//
// ES-Modul (import von three.module.js direkt im Browser, kein Bundler noetig
// -- das Projekt hat bewusst keinen, siehe frontend/README-artige Struktur).
// Exponiert global `window.Orb` mit GENAU derselben Schnittstelle wie der
// alte Canvas2D-Orb (`setState`, `start`, `resize`), damit main.js/mobile.js
// unveraendert bleiben koennen.
//
// Audio-Reaktivitaet: liest `window.__jarvisAudioAnalyser` (ein bestehender
// Web-Audio AnalyserNode, den main.js/mobile.js selbst erzeugen und hier nur
// REFERENZIEREN -- ein zweiter eigener Tap wuerde bei einem <audio>-Element,
// das schon per createMediaElementSource an einen Analyser haengt, mit einem
// Browser-Fehler ("already connected") scheitern, das laesst sich technisch
// nicht umgehen). Gelesen wird nur waehrend state 'speaking'/'announcing' --
// das ersetzt das alte `isPlaying`-Flag, ohne dass dieses Modul eine fremde
// Variable aus main.js/mobile.js braucht.
//
// WICHTIGER UNTERSCHIED zum urspruenglichen Plan: auf dem Handy bleibt
// responseAudio bewusst UNVERBUNDEN (siehe mobile.js-Kommentar zur
// Zuverlaessigkeit der Wiedergabe) -- ein "nur lesender" Tap auf ein
// <audio>-Element existiert in der Web-Audio-API strukturell nicht, jedes
// createMediaElementSource() reisst die Wiedergabe zwingend in den Web-Audio-
// Graphen. Der Orb bekommt auf dem Handy darum weiterhin nur zustandsbasierte
// (nicht echte amplituden-reaktive) Energie beim Sprechen -- kein Rueckschritt
// gegenueber heute, aber (noch) keine echte Lautstaerke-Reaktion dort.

import * as THREE from '/static/three.module.js';

const Orb = (() => {
    const STATES = {
        idle:       { rot: 0.05, energy: 0.16, core: 0.82, spread: 1.00, jitter: 0.05 },
        listening:  { rot: 0.09, energy: 0.34, core: 1.00, spread: 1.04, jitter: 0.10 },
        thinking:   { rot: 0.26, energy: 0.52, core: 1.04, spread: 1.14, jitter: 0.55 },
        speaking:   { rot: 0.14, energy: 0.68, core: 1.14, spread: 1.08, jitter: 0.20 },
        announcing: { rot: 0.18, energy: 0.85, core: 1.24, spread: 1.18, jitter: 0.30 },
    };

    const COLOR_CORE = new THREE.Color(0xffcf94);
    const COLOR_GLOW = new THREE.Color(0xff8a2e);
    const COLOR_RING = new THREE.Color(0xffa54d);
    const COLOR_PARTICLE = new THREE.Color(0xffb066);

    const reducedMotion = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let canvas, renderer, scene, camera;
    let coreMesh, glowMesh, rings, particles, particleGeom, particleBase;
    let running = false;
    let rafId = null;
    let W = 0, H = 0;

    let target = STATES.idle;
    let cur = { rot: STATES.idle.rot, energy: STATES.idle.energy, core: STATES.idle.core, spread: STATES.idle.spread, jitter: STATES.idle.jitter };
    let curState = 'idle';
    let t = 0;

    let pointerX = 0, pointerY = 0; // -1..1, fuer sanftes Kamera-Parallax (nur Maus, kein Touch-Zwang)

    function makeParticleSprite() {
        const size = 64;
        const c = document.createElement('canvas');
        c.width = c.height = size;
        const g = c.getContext('2d');
        const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
        grad.addColorStop(0, 'rgba(255,255,255,1)');
        grad.addColorStop(0.35, 'rgba(255,210,150,0.9)');
        grad.addColorStop(1, 'rgba(255,150,40,0)');
        g.fillStyle = grad;
        g.fillRect(0, 0, size, size);
        const tex = new THREE.CanvasTexture(c);
        tex.needsUpdate = true;
        return tex;
    }

    function fibonacciSphere(n, radius) {
        const pts = new Float32Array(n * 3);
        const golden = Math.PI * (3 - Math.sqrt(5));
        for (let i = 0; i < n; i++) {
            const y = 1 - (i / (n - 1)) * 2;
            const r = Math.sqrt(1 - y * y);
            const theta = golden * i;
            pts[i * 3] = Math.cos(theta) * r * radius;
            pts[i * 3 + 1] = y * radius;
            pts[i * 3 + 2] = Math.sin(theta) * r * radius;
        }
        return pts;
    }

    function buildScene() {
        scene = new THREE.Scene();
        camera = new THREE.PerspectiveCamera(42, 1, 0.1, 100);
        camera.position.set(0, 0, 6.2);

        renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'low-power' });
        renderer.setClearColor(0x000000, 0);

        // --- Kern: helles inneres Zentrum + zwei Fresnel-Gluthuellen (Bloom-
        // Ersatz ohne EffectComposer/UnrealBloomPass -- ein echter Shader mit
        // weichem Kanten-Falloff statt einer flach eingefaerbten Kugel, sonst
        // zeigt selbst eine fein unterteilte Geometrie sichtbare Facettenkanten
        // (live getestet: mit MeshBasicMaterial sah die Gluthuelle wie ein
        // hartkantiges Vieleck aus, nicht wie Licht). Guenstig genug fuer ein
        // Handy: nur 2 zusaetzliche Objekte, kein Postprocessing-Pass. ---
        function fresnelMaterial(color, power, baseOpacity) {
            return new THREE.ShaderMaterial({
                uniforms: {
                    glowColor: { value: color },
                    power: { value: power },
                    baseOpacity: { value: baseOpacity },
                },
                vertexShader: `
                    varying vec3 vNormal;
                    varying vec3 vViewDir;
                    void main() {
                        vNormal = normalize(normalMatrix * normal);
                        vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
                        vViewDir = normalize(-mvPosition.xyz);
                        gl_Position = projectionMatrix * mvPosition;
                    }
                `,
                fragmentShader: `
                    uniform vec3 glowColor;
                    uniform float power;
                    uniform float baseOpacity;
                    varying vec3 vNormal;
                    varying vec3 vViewDir;
                    void main() {
                        float fresnel = pow(1.0 - abs(dot(normalize(vNormal), normalize(vViewDir))), power);
                        gl_FragColor = vec4(glowColor, fresnel * baseOpacity);
                    }
                `,
                transparent: true,
                blending: THREE.AdditiveBlending,
                depthWrite: false,
                side: THREE.FrontSide,
            });
        }

        const coreGeom = new THREE.SphereGeometry(0.62, 48, 32);
        const coreMat = new THREE.MeshBasicMaterial({ color: COLOR_CORE, transparent: true, opacity: 0.98 });
        coreMesh = new THREE.Mesh(coreGeom, coreMat);
        scene.add(coreMesh);

        // Innere Gluthuelle: eng am Kern, blendet den harten Kern-Rand weich
        // in die Umgebung ein.
        const glowGeom = new THREE.SphereGeometry(0.90, 48, 32);
        glowMesh = new THREE.Mesh(glowGeom, fresnelMaterial(COLOR_GLOW, 2.2, 1.1));
        scene.add(glowMesh);

        // Aeussere, weitere Gluthuelle -- gibt dem Kern Tiefe/Ausstrahlung.
        const haloGeom = new THREE.SphereGeometry(1.6, 32, 24);
        const haloMesh = new THREE.Mesh(haloGeom, fresnelMaterial(COLOR_GLOW, 3.2, 0.55));
        scene.add(haloMesh);
        rings = { halo: haloMesh };

        // --- Ringe: mehrere unabhaengig geneigte/rotierende Torusse statt der
        // alten zwei Kreis-Umlaufbahnen. ---
        rings.list = [];
        const ringDefs = [
            { radius: 1.55, tube: 0.010, tiltX: 0.30, tiltZ: 0.10, dir: 1, speedMul: 1.0 },
            { radius: 1.85, tube: 0.008, tiltX: -0.55, tiltZ: 0.85, dir: -1, speedMul: 0.7 },
            { radius: 2.15, tube: 0.006, tiltX: 1.10, tiltZ: -0.35, dir: 1, speedMul: 0.45 },
            { radius: 2.45, tube: 0.005, tiltX: -1.35, tiltZ: 0.55, dir: -1, speedMul: 0.3 },
        ];
        for (const def of ringDefs) {
            const geom = new THREE.TorusGeometry(def.radius, def.tube, 8, 96);
            const mat = new THREE.MeshBasicMaterial({
                color: COLOR_RING, transparent: true, opacity: 0.5,
                blending: THREE.AdditiveBlending, depthWrite: false,
            });
            const mesh = new THREE.Mesh(geom, mat);
            mesh.rotation.x = def.tiltX;
            mesh.rotation.z = def.tiltZ;
            mesh.userData = def;
            scene.add(mesh);
            rings.list.push(mesh);
        }

        // --- Partikelfeld: eine einzige BufferGeometry/Points-Wolke statt
        // hunderter Einzel-Draws (100% GPU-instanziiert). ---
        const COUNT = 620;
        particleBase = fibonacciSphere(COUNT, 1.9);
        particleGeom = new THREE.BufferGeometry();
        particleGeom.setAttribute('position', new THREE.BufferAttribute(particleBase.slice(), 3));
        const particleMat = new THREE.PointsMaterial({
            color: COLOR_PARTICLE, size: 0.045, map: makeParticleSprite(),
            transparent: true, opacity: 0.85, blending: THREE.AdditiveBlending,
            depthWrite: false, sizeAttenuation: true,
        });
        particles = new THREE.Points(particleGeom, particleMat);
        scene.add(particles);
    }

    function resize() {
        if (!canvas || !renderer) return;
        const rect = canvas.getBoundingClientRect();
        W = Math.max(1, rect.width);
        H = Math.max(1, rect.height);
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        renderer.setPixelRatio(dpr);
        renderer.setSize(W, H, false);
        camera.aspect = W / H;
        camera.updateProjectionMatrix();
    }

    function readAudioLevel() {
        if (curState !== 'speaking' && curState !== 'announcing') return 0;
        const analyser = window.__jarvisAudioAnalyser;
        if (!analyser) return 0;
        if (!readAudioLevel._buf || readAudioLevel._buf.length !== analyser.frequencyBinCount) {
            readAudioLevel._buf = new Uint8Array(analyser.frequencyBinCount);
        }
        const buf = readAudioLevel._buf;
        analyser.getByteFrequencyData(buf);
        let sum = 0;
        for (let i = 0; i < buf.length; i++) sum += buf[i];
        return Math.min(1, (sum / buf.length) / 110);
    }

    function frame() {
        if (!running) return;
        rafId = requestAnimationFrame(frame);
        t += 0.016;

        const ease = reducedMotion ? 0.02 : 0.06;
        cur.rot += (target.rot - cur.rot) * ease;
        cur.energy += (target.energy - cur.energy) * ease;
        cur.core += (target.core - cur.core) * ease;
        cur.spread += (target.spread - cur.spread) * ease;
        cur.jitter += (target.jitter - cur.jitter) * ease;

        const audioLevel = readAudioLevel();
        const liveEnergy = Math.min(1.3, cur.energy + audioLevel * 0.6);
        const rotScale = reducedMotion ? 0.15 : 1;

        // Kern: Skalierung + leichte Amplituden-getriebene Pulse
        const corePulse = cur.core * (1 + audioLevel * 0.22 + Math.sin(t * 3.1) * 0.015);
        coreMesh.scale.setScalar(corePulse);
        glowMesh.scale.setScalar(corePulse * 1.02 + liveEnergy * 0.12);
        coreMesh.rotation.y += 0.003 * rotScale;
        coreMesh.rotation.x += 0.0017 * rotScale;
        glowMesh.material.uniforms.baseOpacity.value = 0.85 + liveEnergy * 0.5;

        rings.halo.scale.setScalar(1 + liveEnergy * 0.10 + Math.sin(t * 1.3) * 0.01);
        rings.halo.material.uniforms.baseOpacity.value = 0.35 + liveEnergy * 0.4;

        for (const ring of rings.list) {
            const def = ring.userData;
            ring.rotation.y += cur.rot * def.speedMul * def.dir * 0.02 * rotScale;
            ring.scale.setScalar(cur.spread * (1 + liveEnergy * 0.06));
            ring.material.opacity = 0.32 + liveEnergy * 0.35;
        }

        // Partikel: sanftes Atmen + Zittern (jitter) je nach Zustand, plus
        // eine langsame Gesamtrotation der Wolke.
        const pos = particleGeom.attributes.position.array;
        const jitterAmt = cur.jitter * 0.05;
        for (let i = 0; i < particleBase.length; i += 3) {
            const bx = particleBase[i], by = particleBase[i + 1], bz = particleBase[i + 2];
            const n = Math.sin(t * 1.7 + i) * jitterAmt;
            const s = cur.spread + liveEnergy * 0.08;
            pos[i] = bx * s + n;
            pos[i + 1] = by * s + n;
            pos[i + 2] = bz * s + n;
        }
        particleGeom.attributes.position.needsUpdate = true;
        particles.rotation.y += cur.rot * 0.01 * rotScale;
        particles.rotation.x = Math.sin(t * 0.15) * 0.08;
        particles.material.opacity = 0.55 + liveEnergy * 0.35;

        // Sanftes Kamera-Parallax (Desktop-Maus, harmlos wenn nie ausgeloest)
        camera.position.x += (pointerX * 0.35 - camera.position.x) * 0.03;
        camera.position.y += (-pointerY * 0.25 - camera.position.y) * 0.03;
        camera.lookAt(0, 0, 0);

        renderer.render(scene, camera);
    }

    function setState(name) {
        if (!STATES[name]) return;
        target = STATES[name];
        curState = name;
    }

    function start() {
        canvas = document.getElementById('orb-canvas');
        if (!canvas) return;
        buildScene();
        resize();
        window.addEventListener('resize', resize);
        window.addEventListener('mousemove', (e) => {
            pointerX = (e.clientX / window.innerWidth) * 2 - 1;
            pointerY = (e.clientY / window.innerHeight) * 2 - 1;
        });
        document.addEventListener('visibilitychange', () => {
            if (document.hidden) {
                running = false;
                if (rafId) cancelAnimationFrame(rafId);
            } else if (!running) {
                running = true;
                frame();
            }
        });
        running = true;
        frame();
    }

    return { setState, start, resize };
})();

window.Orb = Orb;
