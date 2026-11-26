"use strict";
// Pagina unica: accesso, caricamento o registrazione, elenco dei job con stato, progetti e glossari.

const $ = (id) => document.getElementById(id);
let io = null;            // utente corrente, con il token CSRF
let timerAggiorna = null;

const STATI = {
  in_coda: "in coda", in_elaborazione: "in elaborazione", completato: "completato",
  errore: "errore", annullato: "annullato",
};

async function api(metodo, percorso, corpo) {
  const opzioni = { method: metodo, headers: {}, credentials: "same-origin" };
  if (io) opzioni.headers["X-CSRF"] = io.csrf;
  if (corpo !== undefined) {
    opzioni.headers["Content-Type"] = "application/json";
    opzioni.body = JSON.stringify(corpo);
  }
  const r = await fetch(percorso, opzioni);
  const dati = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (r.status === 401 && percorso !== "/api/accesso") { mostraAccesso(); throw new Error("Accesso richiesto."); }
  if (!r.ok) throw new Error(dati?.detail || `Errore ${r.status}`);
  return dati;
}

function el(tag, attributi = {}, ...figli) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attributi)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  e.append(...figli.filter((f) => f !== null && f !== undefined));
  return e;
}

const minuti = (s) => (s == null ? "" : s < 60 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
const data = (iso) => new Date(iso).toLocaleString("it-IT", { dateStyle: "short", timeStyle: "short" });

// --- accesso ---------------------------------------------------------------

function mostraAccesso() {
  io = null;
  clearTimeout(timerAggiorna);
  $("vista-app").hidden = true;
  $("vista-accesso").hidden = false;
  $("esci").hidden = true;
  $("chi").textContent = "";
}

async function avvia() {
  try { io = await api("GET", "/api/io"); } catch { return; }
  $("vista-accesso").hidden = true;
  $("vista-app").hidden = false;
  $("esci").hidden = false;
  $("chi").textContent = io.nome || io.email;
  $("lingua").value = io.lingua_predefinita;
  $("giorni").textContent = io.conservazione_giorni;
  const l = io.limiti;
  $("limiti").textContent = `Formati: opus/ogg, mp3, wav, m4a, webm. Fino a ${l.max_mb} MB e ` +
    `${l.max_minuti} minuti per file, ${l.max_job_giorno} file al giorno.`;
  await caricaProgetti();
  await aggiornaJob();
}

$("form-accesso").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.target);
  $("errore-accesso").textContent = "";
  try {
    await api("POST", "/api/accesso", { email: f.get("email"), password: f.get("password") });
    ev.target.reset();
    await avvia();
  } catch (e) { $("errore-accesso").textContent = e.message; }
});

$("esci").addEventListener("click", async () => {
  try { await api("POST", "/api/uscita"); } finally { mostraAccesso(); }
});

// --- job -------------------------------------------------------------------

function barraAvanzamento(perc) {
  const riempimento = el("div");
  riempimento.style.width = `${perc}%`;   // proprietà, non attributo: compatibile con una CSP senza inline
  return el("div", { class: "barra" }, riempimento);
}

function rigaJob(j) {
  const perc = j.durata_s ? Math.min(100, Math.round((100 * j.avanzamento_s) / j.durata_s)) : 0;
  let dettaglio = `${data(j.creato_il)} · ${minuti(j.durata_s)}`;
  if (j.stato === "in_coda") dettaglio += j.davanti ? ` · ${j.davanti} prima di te` : " · sei il prossimo";
  if (j.stato === "in_elaborazione") dettaglio += ` · ${perc}%`;
  if (j.stato === "completato" && j.testo_scade_il) dettaglio += ` · disponibile fino al ${data(j.testo_scade_il)}`;
  if (j.errore) dettaglio += ` · ${j.errore}`;

  const azioni = el("div", { class: "azioni" });
  if (j.stato === "completato") {
    azioni.append(el("a", { class: "btn primario piccolo", href: `/api/job/${j.id}/testo`, download: "" }, "Scarica .txt"));
  }
  if (j.stato === "in_coda" || j.stato === "in_elaborazione") {
    azioni.append(el("button", { class: "btn piccolo", onclick: () => azione("POST", `/api/job/${j.id}/annulla`, "Annullare questa trascrizione?") }, "Annulla"));
  } else {
    azioni.append(el("button", { class: "btn pericolo piccolo", onclick: () => azione("DELETE", `/api/job/${j.id}`, "Eliminare subito testo e dati di questa trascrizione?") }, "Elimina"));
  }
  return el("li", {},
    el("div", { class: "info" },
      el("div", { class: "titolo" }, j.progetto || "Senza progetto", " ",
        el("span", { class: `etichetta ${j.stato}` }, STATI[j.stato])),
      el("div", { class: "tenue piccolo" }, dettaglio),
      j.stato === "in_elaborazione" ? barraAvanzamento(perc) : null),
    azioni);
}

async function azione(metodo, percorso, domanda) {
  if (!confirm(domanda)) return;
  try { await api(metodo, percorso); } catch (e) { alert(e.message); }
  aggiornaJob();
}

async function aggiornaJob() {
  clearTimeout(timerAggiorna);
  let elenco;
  try { elenco = await api("GET", "/api/job"); } catch { return; }
  $("job").replaceChildren(...elenco.map(rigaJob));
  $("nessun-job").hidden = elenco.length > 0;
  const attivi = elenco.some((j) => j.stato === "in_coda" || j.stato === "in_elaborazione");
  timerAggiorna = setTimeout(aggiornaJob, attivi ? 3000 : 30000);
}

// --- caricamento -----------------------------------------------------------

function carica(blob) {
  const parametri = new URLSearchParams({ lingua: $("lingua").value });
  if ($("progetto").value) parametri.set("progetto", $("progetto").value);
  const barra = $("avanzamento-upload");
  $("errore-upload").textContent = "";
  barra.hidden = false;
  return new Promise((risolvi) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/job?${parametri}`);
    xhr.setRequestHeader("X-CSRF", io.csrf);
    xhr.setRequestHeader("Content-Type", blob.type || "application/octet-stream");
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) barra.firstElementChild.style.width = `${(100 * e.loaded) / e.total}%`;
    };
    xhr.onloadend = () => {
      barra.hidden = true;
      barra.firstElementChild.style.width = "0";
      if (xhr.status === 401) mostraAccesso();
      else if (xhr.status < 200 || xhr.status >= 300) {
        let msg = `Caricamento non riuscito (${xhr.status || "rete"}).`;
        try { msg = JSON.parse(xhr.responseText).detail || msg; } catch { /* risposta non JSON */ }
        $("errore-upload").textContent = msg;
      }
      aggiornaJob();
      risolvi();
    };
    xhr.send(blob);
  });
}

$("file").addEventListener("change", async (ev) => {
  const file = ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  if (file.size > io.limiti.max_mb * 1048576) {
    $("errore-upload").textContent = `Il file supera ${io.limiti.max_mb} MB.`;
    return;
  }
  await carica(file);
});

// --- registrazione dal microfono -------------------------------------------

let registratore = null;
let inizioRegistrazione = 0;
let timerRegistrazione = null;

function formatoRegistrazione() {
  for (const t of ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"]) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(t)) return t;
  }
  return "";
}

$("registra").addEventListener("click", async () => {
  const bottone = $("registra");
  if (registratore) { registratore.stop(); return; }
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
    $("errore-upload").textContent = "Questo browser non permette di registrare dal microfono.";
    return;
  }
  let flusso;
  try {
    flusso = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true } });
  } catch {
    $("errore-upload").textContent = "Accesso al microfono negato.";
    return;
  }
  const tipo = formatoRegistrazione();
  const pezzi = [];
  registratore = new MediaRecorder(flusso, { mimeType: tipo, audioBitsPerSecond: 32000 });
  registratore.ondataavailable = (e) => e.data.size && pezzi.push(e.data);
  registratore.onstop = async () => {
    flusso.getTracks().forEach((t) => t.stop());
    clearInterval(timerRegistrazione);
    registratore = null;
    bottone.classList.remove("registrando");
    bottone.textContent = "● Registra dal microfono";
    $("stato-registrazione").textContent = "Caricamento della registrazione…";
    await carica(new Blob(pezzi, { type: tipo.split(";")[0] || "audio/webm" }));
    $("stato-registrazione").textContent = "";
  };
  registratore.start(10000);            // un pezzo ogni 10 s: niente picchi di memoria
  inizioRegistrazione = Date.now();
  bottone.classList.add("registrando");
  bottone.textContent = "■ Ferma e trascrivi";
  timerRegistrazione = setInterval(() => {
    const s = Math.floor((Date.now() - inizioRegistrazione) / 1000);
    $("stato-registrazione").textContent = `Registrazione: ${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
    if (s >= io.limiti.max_minuti * 60) registratore?.stop();
  }, 1000);
});

window.addEventListener("beforeunload", (e) => { if (registratore) e.preventDefault(); });

// --- progetti --------------------------------------------------------------

async function caricaProgetti() {
  const progetti = await api("GET", "/api/progetti");
  const scelta = $("progetto").value;
  $("progetto").replaceChildren(el("option", { value: "" }, "Nessun progetto"),
    ...progetti.map((p) => el("option", { value: p.id }, p.nome)));
  $("progetto").value = progetti.some((p) => String(p.id) === scelta) ? scelta : "";
  $("elenco-progetti").replaceChildren(...progetti.map((p) => {
    const nome = el("input", { value: p.nome, maxlength: "100", "aria-label": "Nome del progetto" });
    const glossario = el("textarea", { rows: "3", "aria-label": "Glossario" });
    glossario.value = p.glossario;
    return el("li", {},
      el("div", { class: "info" }, nome, glossario),
      el("div", { class: "azioni" },
        el("button", { class: "btn piccolo", type: "button", onclick: async () => {
          try { await api("PUT", `/api/progetti/${p.id}`, { nome: nome.value, glossario: glossario.value }); await caricaProgetti(); }
          catch (e) { alert(e.message); }
        } }, "Salva"),
        el("button", { class: "btn pericolo piccolo", type: "button", onclick: async () => {
          if (!confirm(`Eliminare il progetto «${p.nome}»? Le trascrizioni restano, senza progetto.`)) return;
          try { await api("DELETE", `/api/progetti/${p.id}`); await caricaProgetti(); aggiornaJob(); }
          catch (e) { alert(e.message); }
        } }, "Elimina")));
  }));
}

$("gestisci-progetti").addEventListener("click", () => $("dialogo-progetti").showModal());
$("crea-progetto").addEventListener("click", async () => {
  try {
    const p = await api("POST", "/api/progetti", { nome: $("nuovo-nome").value, glossario: $("nuovo-glossario").value });
    $("nuovo-nome").value = "";
    $("nuovo-glossario").value = "";
    await caricaProgetti();
    $("progetto").value = p.id;
  } catch (e) { alert(e.message); }
});

avvia().then(() => { if (!io) mostraAccesso(); });
