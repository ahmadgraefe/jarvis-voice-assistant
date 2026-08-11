// Ahmads Cockpit (2026-08-11) — holt echte Daten von /api/cockpit und
// befuellt die statische Vorlage in cockpit.html. Kein Demo-Wert hier drin,
// jeder Leerzustand ist ehrlich als "noch keine Daten" markiert statt
// irgendetwas zu erfinden.

const fmt = (n) => (n === null || n === undefined ? "–" : n.toLocaleString("de-DE"));
const fmtEur = (n) => (n === null || n === undefined ? "–" : n.toLocaleString("de-DE", { minimumFractionDigits: 2, maximumFractionDigits: 2 }));

function kpiCard(icon, label, val, unit, footText, footClass, footIcon) {
  return `<div class="card kpi">
    <div class="kpi-icon"><i data-lucide="${icon}"></i></div>
    <span class="eyebrow">${label}</span>
    <div class="kpi-val">${val}${unit ? `<span class="kpi-unit">${unit}</span>` : ""}</div>
    ${footText ? `<div class="kpi-foot ${footClass || "flat"}"><i data-lucide="${footIcon || "minus"}"></i>${footText}</div>` : ""}
  </div>`;
}

function emptyState(icon, text) {
  return `<div class="empty-state"><i data-lucide="${icon}"></i><div>${text}</div></div>`;
}

function rowHtml(dotColor, title, sub, val, unit, badge) {
  return `<div class="row">
    <div class="row-l">${dotColor ? `<span class="dot" style="background:${dotColor}"></span>` : ""}
      <div><div class="row-title">${title}</div>${sub ? `<div class="row-sub">${sub}</div>` : ""}</div>
    </div>
    <div class="row-r">${val !== undefined ? `<div class="row-val">${val}${unit ? ` <span class="row-unit">${unit}</span>` : ""}</div>` : ""}${badge || ""}</div>
  </div>`;
}

function trendMeta(delta) {
  if (delta === null || delta === undefined) return { cls: "flat", icon: "minus", text: "keine Vergleichsdaten" };
  if (delta > 0) return { cls: "up", icon: "arrow-up-right", text: `+${fmt(delta)} seit letztem Check` };
  if (delta < 0) return { cls: "down", icon: "arrow-down-right", text: `${fmt(delta)} seit letztem Check` };
  return { cls: "flat", icon: "minus", text: "unveraendert" };
}

function renderOverview(data) {
  const heroLine = document.getElementById("heroLine");
  heroLine.textContent = data.nudge || "Kein akutes To-do erkannt — alles Wichtige ist auf dem aktuellen Stand.";

  const doneHabits = data.habits.filter((h) => h.done_today).length;
  const bestIg = data.instagram.reduce((best, cur) => {
    if (cur.delta === null || cur.delta === undefined) return best;
    if (!best || Math.abs(cur.delta) > Math.abs(best.delta)) return cur;
    return best;
  }, null);
  const financeTrend = data.finance && data.finance.current_total_cost !== undefined && data.finance.previous_total_cost !== undefined
    ? data.finance.current_total_cost - data.finance.previous_total_cost
    : null;

  document.getElementById("overviewKpis").innerHTML = [
    data.finance
      ? kpiCard("wallet", "Kosten " + (data.finance.current_month || "aktueller Monat"), fmtEur(data.finance.current_total_cost), "€",
          financeTrend === null ? null : (financeTrend <= 0 ? `${fmtEur(Math.abs(financeTrend))} € weniger` : `${fmtEur(financeTrend)} € mehr`),
          financeTrend === null ? "flat" : (financeTrend <= 0 ? "up" : "down"),
          financeTrend === null ? "minus" : (financeTrend <= 0 ? "arrow-down-right" : "arrow-up-right"))
      : kpiCard("wallet", "Kosten", "–", "", "keine Daten", "flat", "minus"),
    kpiCard("target", "Aktive Ziele", fmt(data.goals.length), "", data.goals.some((g) => g.due) ? "eines faellig" : "alle im Plan", data.goals.some((g) => g.due) ? "warn" : "ok", data.goals.some((g) => g.due) ? "alert-circle" : "check"),
    kpiCard("check-circle", "Gewohnheiten", `${doneHabits}<span class="kpi-unit">/${data.habits.length}</span>`, "", data.habits.length ? "heute erledigt" : "noch keine angelegt", "flat", "minus"),
    bestIg
      ? kpiCard("trending-up", "Instagram", `@${bestIg.handle}`, "", trendMeta(bestIg.delta).text, trendMeta(bestIg.delta).cls, trendMeta(bestIg.delta).icon)
      : kpiCard("trending-up", "Instagram", "–", "", "keine Snapshot-Daten", "flat", "minus"),
  ].join("");

  document.getElementById("overviewHabits").innerHTML = data.habits.length
    ? data.habits.map((hh) => rowHtml(hh.done_today ? "var(--color-ok)" : "var(--color-warn)", hh.name, `Streak ${hh.streak} Tage`, undefined, undefined,
        `<span class="badge ${hh.done_today ? "badge-ok" : "badge-warn"}">${hh.done_today ? "erledigt" : "offen"}</span>`)).join("")
    : emptyState("check-circle", "Noch keine Gewohnheiten angelegt. Sag Jarvis einfach, was du ab jetzt regelmäßig tracken willst.");

  document.getElementById("overviewGoals").innerHTML = data.goals.length
    ? data.goals.slice(0, 5).map((g) => rowHtml(g.due ? "var(--color-warn)" : "var(--color-ok)", g.description, g.last_note || "noch kein Update", undefined, undefined,
        g.due ? `<span class="badge badge-warn">faellig</span>` : `<span class="badge badge-ok">im Plan</span>`)).join("")
    : emptyState("target", "Noch keine Ziele angelegt. Sag Jarvis einfach, was du dir vornimmst.");
}

function renderBusiness(data) {
  document.getElementById("businessAccounts").innerHTML = data.instagram.length
    ? data.instagram.map((acc) => {
        const t = trendMeta(acc.delta);
        return kpiCard("at-sign", `@${acc.handle}`, fmt(acc.followers), "Follower", t.text, t.cls, t.icon);
      }).join("")
    : emptyState("trending-up", "Noch keine Instagram-Snapshots vorhanden. Der Hintergrund-Tracker füllt das automatisch, sobald er gelaufen ist.");

  document.getElementById("businessWinners").innerHTML = data.winners.length
    ? data.winners.map((w) => rowHtml("var(--color-accent)", w.account || "–", (w.video_link || "").slice(0, 46), fmt(w.views), "Views",
        w.decision ? `<span class="badge ${String(w.decision).toUpperCase() === "KEEP" ? "badge-ok" : String(w.decision).toUpperCase() === "DELETE" ? "badge-warn" : "badge-info"}">${w.decision}</span>` : "")).join("")
    : emptyState("trophy", "Keine Winner-Tracking-Einträge geladen (oder das Sheet war gerade nicht erreichbar).");
}

function renderFinance(data) {
  const f = data.finance;
  const sub = document.getElementById("financeSub");
  if (!f) {
    sub.textContent = "Keine Finanzdaten verfügbar — Sheet gerade nicht erreichbar oder aktueller Monat nicht gefunden.";
    document.getElementById("financeKpis").innerHTML = emptyState("wallet", "Keine Finanzdaten geladen.");
    return;
  }
  sub.textContent = `${f.current_month} im Vergleich zu ${f.previous_month}`;
  document.getElementById("financeKpis").innerHTML = [
    kpiCard("wallet", "Gesamtkosten " + f.current_month, fmtEur(f.current_total_cost), "€"),
    kpiCard("wallet", "Gesamtkosten " + f.previous_month, fmtEur(f.previous_total_cost), "€"),
    f.current_netto !== null ? kpiCard("trending-up", "Netto " + f.current_month, fmtEur(f.current_netto), "€") : kpiCard("trending-up", "Netto", "–", "", "keine Daten", "flat", "minus"),
    f.previous_netto !== null ? kpiCard("trending-up", "Netto " + f.previous_month, fmtEur(f.previous_netto), "€") : kpiCard("trending-up", "Netto", "–", "", "keine Daten", "flat", "minus"),
  ].join("");

  const card = document.getElementById("financeChartCard");
  card.style.display = "";
  const g = card.querySelector(".financebars");
  g.innerHTML = "";
  const max = Math.max(f.current_total_cost, f.previous_total_cost, 1);
  const bars = [
    { label: f.previous_month, val: f.previous_total_cost, x: 70, color: "var(--color-text-2)" },
    { label: f.current_month, val: f.current_total_cost, x: 200, color: "var(--color-accent)" },
  ];
  bars.forEach((b) => {
    const bh = (b.val / max) * 110;
    const y = 130 - bh;
    g.insertAdjacentHTML("beforeend", `<rect class="anim-bar" x="${b.x}" y="${y}" width="60" height="${bh}" rx="4" fill="${b.color}"></rect>
      <text x="${b.x + 30}" y="148" text-anchor="middle" class="mono" fill="var(--color-text-muted)" font-size="11">${b.label}</text>
      <text x="${b.x + 30}" y="${y - 8}" text-anchor="middle" class="mono num" fill="var(--color-text)" font-size="12">${fmtEur(b.val)} €</text>`);
  });
}

function renderGoals(data) {
  document.getElementById("goalsList").innerHTML = data.goals.length
    ? data.goals.map((g) => rowHtml(g.due ? "var(--color-warn)" : "var(--color-ok)", g.description,
        g.last_note ? g.last_note : `alle ${g.check_in_days} Tage Check-in`,
        g.days_since_check_in !== null ? fmt(g.days_since_check_in) : "–", "Tage her",
        g.due ? `<span class="badge badge-warn">faellig</span>` : `<span class="badge badge-ok">im Plan</span>`)).join("")
    : emptyState("target", "Noch keine Ziele angelegt. Sag Jarvis einfach 'ich will X schaffen', dann trackt er es von selbst.");
}

function renderHabits(data) {
  document.getElementById("habitsList").innerHTML = data.habits.length
    ? data.habits.map((hh) => {
        const dots = hh.history.map((d) => `<span class="dot" style="background:${d ? "var(--color-ok)" : "rgba(242,232,220,0.12)"};margin-right:3px"></span>`).join("");
        return rowHtml(null, hh.name, `Streak ${hh.streak} Tage`, undefined, undefined,
          `<div style="display:flex;align-items:center;gap:10px">${dots}<span class="badge ${hh.done_today ? "badge-ok" : "badge-warn"}">${hh.done_today ? "heute erledigt" : "heute offen"}</span></div>`);
      }).join("")
    : emptyState("check-circle", "Noch keine Gewohnheiten angelegt. Sag Jarvis 'ich will ab jetzt täglich X machen', dann legt er es an.");
}

function renderMeals(data) {
  document.getElementById("mealsList").innerHTML = data.meals.length
    ? data.meals.slice().reverse().map((m) => rowHtml("var(--color-accent)", m.text, m.timestamp.split(" ")[1] || m.timestamp)).join("")
    : emptyState("utensils", "Noch nichts eingetragen. Sag Jarvis einfach, was du gegessen oder getrunken hast.");
}

function renderHealth(data) {
  const health = data.health || {};
  const metrics = Object.keys(health).filter((k) => k !== "_updated_at");
  const kpiEl = document.getElementById("healthKpis");
  const emptyEl = document.getElementById("healthEmpty");
  if (!metrics.length) {
    kpiEl.innerHTML = "";
    emptyEl.innerHTML = emptyState("moon", "Noch nicht verbunden. Auf dem iPhone die App 'Health Auto Export' einrichten und eine REST-Automation auf diese Adresse zeigen lassen — Jarvis fragt dann direkt danach.");
    return;
  }
  emptyEl.innerHTML = "";
  const iconFor = (name) => ({ sleep_analysis: "moon", step_count: "footprints", heart_rate: "heart-pulse", resting_heart_rate: "heart", active_energy: "flame" }[name] || "activity");
  kpiEl.innerHTML = metrics.map((name) => {
    const m = health[name];
    return kpiCard(iconFor(name), name.replace(/_/g, " "), fmt(m.value), m.units || "", m.date ? `Stand ${m.date}` : null, "flat", "clock");
  }).join("");
}

async function loadCockpit() {
  const dot = document.getElementById("syncDot");
  const label = document.getElementById("syncLabel");
  try {
    const res = await fetch("/api/cockpit");
    if (!res.ok) throw new Error("HTTP " + res.status);
    const data = await res.json();

    renderOverview(data);
    renderBusiness(data);
    renderFinance(data);
    renderGoals(data);
    renderHabits(data);
    renderMeals(data);
    renderHealth(data);

    dot.style.background = "var(--color-ok)";
    label.textContent = "live · " + data.generated_at;
    document.getElementById("footerNote").textContent = `Ahmads Cockpit · live aus Jarvis · zuletzt aktualisiert ${data.generated_at}`;
  } catch (e) {
    dot.style.background = "var(--color-err)";
    label.textContent = "Verbindung fehlgeschlagen";
    console.error("Cockpit-Ladefehler:", e);
  }
  if (window.lucide) lucide.createIcons();
  animateFillsAndRings();
}

function animateFillsAndRings() {
  document.querySelectorAll(".fill").forEach((f) => {
    const w = f.dataset.w;
    if (!w) return;
    f.style.width = "0";
    requestAnimationFrame(() => { f.style.width = w + "%"; });
  });
}

// ===== Tabs =====
const navItems = document.querySelectorAll(".nav-item");
const pages = document.querySelectorAll(".tab-page");
navItems.forEach((it) => {
  it.addEventListener("click", () => {
    const tab = it.dataset.tab;
    navItems.forEach((n) => n.classList.remove("active"));
    it.classList.add("active");
    pages.forEach((p) => p.classList.toggle("active", p.id === tab));
    document.querySelector(".main").scrollTop = 0;
  });
});

if (window.lucide) lucide.createIcons();
loadCockpit();
setInterval(loadCockpit, 5 * 60 * 1000);
