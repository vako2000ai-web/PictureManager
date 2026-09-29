"use strict";

const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (n) => {
  n = Number(n || 0);
  const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const res = await fetch("/api" + path, init);
  let data = null;
  try { data = await res.json(); } catch { /* пустой ответ */ }
  if (!res.ok) throw new Error((data && (data.detail || data.error)) || `Ошибка ${res.status}`);
  return data;
}
const fail = (e) => alert(e.message || e);

/* ---------- вкладки ---------- */
const TABS = ["sources", "catalog", "dups", "move", "heic", "journal"];
const loaders = {};
function showTab() {
  const tab = TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : "sources";
  TABS.forEach((t) => { $("#tab-" + t).hidden = t !== tab; });
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
  if (loaders[tab]) loaders[tab]().catch(fail);
}
window.addEventListener("hashchange", showTab);

/* ---------- фоновые задачи ---------- */
let currentJob = null;
const KIND = { scan: "Сканирование", duplicates: "Поиск дублей", move: "Перемещение", rollback: "Откат", convert: "Конвертация HEIC" };
const fmtTime = (sec) => `${String(Math.floor(sec / 60)).padStart(2, "0")}:${String(Math.floor(sec % 60)).padStart(2, "0")}`;
let jobTimer = null;
function trackJob(jobId, onDone) {
  currentJob = jobId;
  const bar = $("#jobbar"), text = $("#jobtext"), track = $("#jobtrack"), fill = $(".fill", track);
  const started = Date.now();
  clearInterval(jobTimer);
  // Время тикает независимо от сервера: если оно идёт, интерфейс не завис.
  jobTimer = setInterval(() => { $("#jobtime").textContent = "прошло " + fmtTime((Date.now() - started) / 1000); }, 500);
  $("#jobtime").textContent = "прошло 00:00";
  bar.hidden = false;
  bar.classList.remove("finished");
  track.classList.add("indeterminate");
  fill.style.width = "";
  $("#jobcancel").hidden = false;
  $("#jobcur").textContent = "";
  text.textContent = "Запуск…";
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = (ev) => {
    const j = JSON.parse(ev.data);
    const name = KIND[j.kind] || j.kind;
    const known = j.kind !== "scan" && j.total_bytes > 0;          // у скана общий объём растёт по ходу обхода
    track.classList.toggle("indeterminate", !known && !TERMINAL.includes(j.status));
    if (known) {
      const pct = Math.min(100, (100 * j.done_bytes) / j.total_bytes);
      fill.style.width = pct + "%";
      track.setAttribute("aria-valuenow", Math.round(pct));
    } else if (TERMINAL.includes(j.status)) {
      fill.style.width = "100%";
    }
    if (j.kind === "scan" && !TERMINAL.includes(j.status)) {
      text.textContent = `${name}: найдено файлов ${j.done_files}, ${fmtBytes(j.done_bytes)}`;
    } else {
      text.textContent = `${name}: ${STATUS[j.status] || j.status} — ${j.done_files}/${j.total_files} файлов, ${fmtBytes(j.done_bytes)} из ${fmtBytes(j.total_bytes)}`;
    }
    if (j.current) $("#jobcur").textContent = j.current;
    if (TERMINAL.includes(j.status)) {
      es.close();
      clearInterval(jobTimer);
      currentJob = null;
      bar.classList.add("finished");
      track.classList.remove("indeterminate");
      $("#jobcancel").hidden = true;
      $("#jobcur").textContent = "";
      if (j.error) text.textContent += ` — ${j.error}`;
      setTimeout(() => { if (!currentJob) bar.hidden = true; }, 5000);
      if (onDone) onDone(j);
    }
  };
  es.onerror = () => es.close();
}
const TERMINAL = ["done", "cancelled", "error", "interrupted"];
const STATUS = { queued: "в очереди", running: "выполняется", done: "готово", cancelled: "отменено", error: "ошибка", interrupted: "прервано" };
$("#jobcancel").onclick = () => currentJob && api(`/jobs/${currentJob}/cancel`, { method: "POST" }).catch(fail);

function resultSummary(j) {
  const r = j.result || {};
  const errs = (r.errors || []).length;
  const parts = Object.entries(r).filter(([k, v]) => typeof v === "number" || typeof v === "boolean").map(([k, v]) => `${k}: ${v}`);
  return parts.join(", ") + (errs ? `, ошибок: ${errs}` : "");
}

/* ---------- источники и настройки ---------- */
async function loadRoots() {
  const roots = await api("/roots");
  $("#roots").innerHTML = "<tr><th>Путь</th><th>Файлов</th><th>Последний скан</th><th></th></tr>" +
    roots.map((r) => `<tr><td>${esc(r.path)}</td><td>${r.files}</td><td>${esc(r.last_scan || "—")}</td>
      <td><button data-scan="${r.id}">Сканировать</button>
      <button class="secondary" data-del="${r.id}">Удалить из каталога</button></td></tr>`).join("");
  for (const sel of ["#f-root", "#m-root"]) {
    const cur = $(sel).value;
    $(sel).innerHTML = `<option value="">${sel === "#f-root" ? "все источники" : "все файлы каталога"}</option>` +
      roots.map((r) => `<option value="${r.id}">${esc(r.path)}</option>`).join("");
    $(sel).value = cur;
  }
}
loaders.sources = async () => {
  await loadRoots();
  const s = await api("/settings");
  const f = $("#settingsform");
  for (const k of ["path_template", "day_boundary_hour", "jpeg_quality", "video_time_policy", "ffprobe_path", "ffmpeg_path"]) f.elements[k].value = s[k] ?? "";
};
$("#rootform").onsubmit = async (e) => {
  e.preventDefault();
  try { await api("/roots", { method: "POST", body: { path: $("#rootpath").value } }); $("#rootpath").value = ""; await loadRoots(); } catch (err) { fail(err); }
};
$("#roots").onclick = async (e) => {
  const scan = e.target.dataset.scan, del = e.target.dataset.del;
  try {
    if (scan) { const { job_id } = await api(`/roots/${scan}/scan`, { method: "POST" }); trackJob(job_id, (j) => { loadRoots(); alert("Сканирование: " + resultSummary(j)); }); }
    if (del && confirm("Убрать источник из каталога? Файлы на диске не удаляются.")) { await api(`/roots/${del}`, { method: "DELETE" }); await loadRoots(); }
  } catch (err) { fail(err); }
};
$("#settingsform").onsubmit = async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {};
  for (const el of f.elements) if (el.name) body[el.name] = el.value === "" ? null : (el.type === "number" ? Number(el.value) : el.value);
  try { await api("/settings", { method: "PUT", body }); alert("Настройки сохранены"); checkHealth(); } catch (err) { fail(err); }
};

/* ---------- каталог ---------- */
const selected = new Set();
const updateSel = () => { $("#selcount").textContent = `выбрано: ${selected.size}`; };
function filterQuery() {
  const q = new URLSearchParams({ sort: $("#f-sort").value });
  if ($("#f-root").value) q.set("root_id", $("#f-root").value);
  if ($("#f-kind").value) q.set("kind", $("#f-kind").value);
  if ($("#f-undated").checked) q.set("undated", "true");
  return q;
}
async function loadDays() {
  const days = await api("/catalog/days?" + filterQuery());
  selected.clear(); updateSel();
  $("#days").innerHTML = days.length ? days.map((d) => `<details class="day" data-day="${esc(d.day || "")}">
      <summary>${esc(d.day || "Без даты")} <span class="muted">— ${d.count} файл., ${fmtBytes(d.bytes)}</span></summary>
      <div class="thumbs"></div></details>`).join("") : '<p class="muted">Каталог пуст. Добавьте источник и запустите сканирование.</p>';
}
loaders.catalog = async () => { await loadRoots(); await loadDays(); };
$("#f-apply").onclick = () => loadDays().catch(fail);
$("#days").addEventListener("toggle", async (e) => {
  const det = e.target;
  if (!det.open || det.dataset.loaded) return;
  det.dataset.loaded = "1";
  const q = filterQuery();
  if (det.dataset.day) q.set("day", det.dataset.day); else q.set("undated", "true");
  q.set("limit", "1000");
  const files = await api("/catalog/files?" + q);
  $(".thumbs", det).innerHTML = files.map((f) => `<label class="thumb">
      <input type="checkbox" data-id="${f.id}"><img loading="lazy" src="/api/thumb/${f.id}" alt="">
      <div class="cap">${esc(f.rel_path.split("/").pop())}<br>${esc(f.date_source || "нет даты")}${f.manual_taken_at ? " (вручную)" : ""}</div></label>`).join("");
}, true);
$("#days").onchange = (e) => {
  const id = Number(e.target.dataset.id);
  if (!id) return;
  e.target.checked ? selected.add(id) : selected.delete(id);
  updateSel();
};
async function changeDate(iso) {
  if (!selected.size) return alert("Выберите файлы");
  try { await api("/catalog/date", { method: "POST", body: { file_ids: [...selected], date: iso } }); await loadDays(); } catch (e) { fail(e); }
}
$("#setdate").onclick = () => {
  const v = $("#manualdate").value;
  if (!v) return alert("Укажите дату");
  changeDate(v.length === 16 ? v + ":00" : v);
};
$("#cleardate").onclick = () => changeDate(null);

/* ---------- дубли ---------- */
async function loadDups() {
  const groups = await api("/duplicates");
  $("#dupgroups").innerHTML = groups.length ? groups.map((g) => `<div class="card" data-g="${g.id}">
      <div class="muted">SHA-256 ${esc(g.sha256.slice(0, 16))}… · ${g.members.length} копий по ${fmtBytes(g.members[0].size)}</div>
      ${g.members.map((m) => `<div class="member"><label><input type="radio" name="k${g.id}" data-file="${m.id}" ${m.is_keeper ? "checked" : ""}>
        оставить</label> <span>${esc(m.root_path)}/${esc(m.rel_path)}</span></div>`).join("")}
      <button class="danger" data-q="${g.id}">Остальные — в карантин</button></div>`).join("") : '<p class="muted">Групп дублей нет (или поиск ещё не запускался).</p>';
  const q = await api("/quarantine");
  $("#quarantine").innerHTML = q.length ? "<table>" + q.map((f) => `<tr><td>${esc(f.root_path)}/${esc(f.rel_path)}</td><td>${fmtBytes(f.size)}</td>
      <td><button class="secondary" data-restore="${f.id}">Восстановить</button></td></tr>`).join("") + "</table>" : '<p class="muted">Карантин пуст.</p>';
}
loaders.dups = loadDups;
$("#dupsearch").onclick = async () => {
  try { const { job_id } = await api("/duplicates/search", { method: "POST" }); trackJob(job_id, () => loadDups().catch(fail)); } catch (e) { fail(e); }
};
$("#dupgroups").onclick = async (e) => {
  try {
    const gid = e.target.closest("[data-g]")?.dataset.g;
    if (e.target.dataset.file) { await api(`/duplicates/${gid}/keeper`, { method: "POST", body: { file_id: Number(e.target.dataset.file) } }); }
    if (e.target.dataset.q && confirm("Перенести все копии, кроме отмеченной, в карантин?")) {
      const r = await api(`/duplicates/${e.target.dataset.q}/quarantine`, { method: "POST", body: { confirm: true } });
      if (r.errors.length) alert("Ошибки: " + r.errors.map((x) => x.error).join("; "));
      await loadDups();
    }
  } catch (err) { fail(err); }
};
$("#quarantine").onclick = async (e) => {
  if (!e.target.dataset.restore) return;
  try { await api(`/files/${e.target.dataset.restore}/restore`, { method: "POST" }); await loadDups(); } catch (err) { fail(err); }
};

/* ---------- перемещение ---------- */
let currentPlan = null;
function renderPlan(p) {
  currentPlan = p;
  const s = p.summary;
  $("#plan").innerHTML = `<div class="card"><b>План №${p.id}</b> → ${esc(p.dest_path)} · статус: ${esc(p.status)}<br>
    Файлов к перемещению: <b>${s.files}</b> (${fmtBytes(s.bytes)}), уже на месте: ${s.skipped}, конфликтов имён: ${s.conflicts},
    в карантин как дубли: ${s.quarantine}, без даты: ${s.undated}<br>
    Нужно места на другом томе: ${fmtBytes(s.need_bytes)}, свободно: ${fmtBytes(s.free_bytes)}
    ${s.enough_space ? "" : '<b class="bad"> — места не хватает</b>'}
    <div class="row" style="margin-top:8px"><button id="runplan" ${p.status !== "draft" || !s.enough_space ? "disabled" : ""}>Выполнить перемещение</button></div></div>
    <table><tr><th>Откуда</th><th>Куда</th><th>Действие</th></tr>${(p.items || []).map((i) => `<tr><td>${esc(i.src)}</td><td>${esc(i.dst)}</td>
    <td>${esc(i.action)} ${esc(i.note || "")}</td></tr>`).join("")}</table>
    ${p.items && p.items.length >= 200 ? '<p class="muted">Показаны первые 200 строк плана.</p>' : ""}`;
}
async function loadPlans() {
  const plans = await api("/plans");
  $("#plans").innerHTML = plans.length ? "<table>" + plans.map((p) => `<tr><td>№${p.id}</td><td>${esc(p.dest_path)}</td><td>${esc(p.status)}</td>
    <td>${esc(p.created)}</td><td>${p.summary.files || 0} файл.</td>
    <td><button class="secondary" data-open="${p.id}">Открыть</button>
    ${["done", "partial"].includes(p.status) ? `<button class="danger" data-rollback="${p.id}">Откатить</button>` : ""}</td></tr>`).join("") + "</table>" : '<p class="muted">Планов пока нет.</p>';
}
loaders.move = async () => { await loadRoots(); await loadPlans(); };
$("#buildplan").onclick = async () => {
  const dest = $("#destpath").value.trim();
  if (!dest) return alert("Укажите корень назначения");
  const body = { dest_path: dest, undated: $("#m-undated").checked };
  if ($("#m-root").value) body.root_id = Number($("#m-root").value);
  try { const made = await api("/plans", { method: "POST", body }); renderPlan(await api(`/plans/${made.id}`)); await loadPlans(); } catch (e) { fail(e); }
};
$("#plan").onclick = async (e) => {
  if (e.target.id !== "runplan" || !currentPlan) return;
  const s = currentPlan.summary;
  if (!confirm(`Переместить ${s.files} файлов (${fmtBytes(s.bytes)}) в ${currentPlan.dest_path}?\nОперация записывается в журнал и может быть откачена.`)) return;
  try {
    const { job_id } = await api(`/plans/${currentPlan.id}/execute`, { method: "POST", body: { confirm: true } });
    trackJob(job_id, async (j) => { alert("Перемещение: " + resultSummary(j)); renderPlan(await api(`/plans/${currentPlan.id}`)); loadPlans(); });
  } catch (err) { fail(err); }
};
$("#plans").onclick = async (e) => {
  try {
    if (e.target.dataset.open) renderPlan(await api(`/plans/${e.target.dataset.open}`));
    if (e.target.dataset.rollback && confirm("Вернуть файлы плана на прежние места? Изменённые после перемещения файлы будут пропущены.")) {
      const { job_id } = await api(`/plans/${e.target.dataset.rollback}/rollback`, { method: "POST", body: { confirm: true } });
      trackJob(job_id, (j) => { alert("Откат: " + resultSummary(j)); loadPlans(); });
    }
  } catch (err) { fail(err); }
};

/* ---------- HEIC ---------- */
async function loadHeic() {
  const files = await api("/catalog/files?ext=.heic&limit=1000");
  const more = await api("/catalog/files?ext=.heif&limit=1000");
  const all = files.concat(more);
  $("#heiclist").innerHTML = all.length ? `<p class="muted">Найдено HEIC: ${all.length}</p><div class="thumbs">` + all.map((f) => `<div class="thumb">
    <img loading="lazy" src="/api/thumb/${f.id}" alt=""><div class="cap">${esc(f.rel_path)}</div></div>`).join("") + "</div>" : '<p class="muted">HEIC-файлов в каталоге нет.</p>';
}
loaders.heic = loadHeic;
$("#convertall").onclick = async () => {
  try { const { job_id } = await api("/convert/heic", { method: "POST", body: {} }); trackJob(job_id, (j) => { alert("Конвертация: " + resultSummary(j)); loadHeic(); }); } catch (e) { fail(e); }
};
$("#quarantineorig").onclick = async () => {
  if (!confirm("Перенести в карантин HEIC-оригиналы, у которых есть JPEG-копия?")) return;
  try {
    const files = (await api("/catalog/files?ext=.heic&limit=1000")).concat(await api("/catalog/files?ext=.heif&limit=1000"));
    const r = await api("/convert/quarantine-originals", { method: "POST", body: { file_ids: files.map((f) => f.id) } });
    alert(`В карантин: ${r.quarantined}, пропущено: ${r.errors.length}`); loadHeic();
  } catch (e) { fail(e); }
};

/* ---------- журнал ---------- */
async function loadJournal() {
  const ops = await api("/operations?limit=300");
  $("#journal").innerHTML = "<tr><th>№</th><th>Время</th><th>Тип</th><th>Статус</th><th>Откуда</th><th>Куда</th></tr>" +
    ops.map((o) => `<tr><td>${o.id}</td><td>${esc(o.ts)}</td><td>${esc(o.kind)}</td><td class="${o.status === "error" ? "bad" : ""}">${esc(o.status)}</td>
    <td>${esc(o.src)}</td><td>${esc(o.dst)}</td></tr>`).join("");
}
loaders.journal = loadJournal;
$("#reloadjournal").onclick = () => loadJournal().catch(fail);

/* ---------- предупреждение об ffmpeg ---------- */
async function checkHealth() {
  const h = await api("/health");
  const missing = [!h.ffprobe && "ffprobe", !h.ffmpeg && "ffmpeg"].filter(Boolean);
  const w = $("#warn");
  w.hidden = !missing.length;
  if (missing.length) w.textContent = `Не найден ${missing.join(" и ")}: даты видео берутся из имени файла или времени изменения, миниатюры видео заменены заглушкой. Установите FFmpeg или укажите путь в настройках.`;
}
checkHealth().catch(() => {});
showTab();
