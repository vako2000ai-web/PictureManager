"use strict";
const $ = (sel, el = document) => el.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = n => { n = n || 0; const u = ["Б", "КБ", "МБ", "ГБ", "ТБ"]; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + " " + u[i]; };

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) { init.body = JSON.stringify(opts.body); init.headers["Content-Type"] = "application/json"; }
  const r = await fetch("/api" + path, init);
  if (!r.ok) { let m = r.statusText; try { m = (await r.json()).detail || m; } catch {} throw new Error(m); }
  return r.json();
}
const post = (p, body = {}) => api(p, { method: "POST", body });

/* подтверждение опасных действий со сводкой последствий */
function confirmDialog(title, summaryHtml, okLabel = "Выполнить") {
  return new Promise(resolve => {
    const d = document.createElement("div"); d.className = "dlg";
    d.innerHTML = `<div class="box"><h2>${esc(title)}</h2><div>${summaryHtml}</div>
      <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn secondary" id="no">Отмена</button><button class="btn danger" id="ok">${esc(okLabel)}</button></div></div>`;
    document.body.append(d);
    $("#no", d).onclick = () => { d.remove(); resolve(false); };
    $("#ok", d).onclick = () => { d.remove(); resolve(true); };
  });
}
const alertErr = e => alert("Ошибка: " + e.message);

/* ---- фоновые задачи и прогресс (SSE) ---- */
const tracked = new Set();
const jobEls = {};
const jobNames = { scan: "Сканирование", dupes: "Поиск дублей", move: "Перемещение", rollback: "Откат", heic: "Конвертация HEIC" };
const finished = ["done", "cancelled", "failed", "interrupted"];
const jobDone = {};

function renderJob(j) {
  let el = jobEls[j.id];
  if (!el) { el = jobEls[j.id] = document.createElement("div"); el.className = "job"; $("#jobs").append(el); }
  const pct = j.bytes_total ? j.bytes_done / j.bytes_total : (j.files_total ? j.files_done / j.files_total : 0);
  const fin = finished.includes(j.status);
  el.innerHTML = `<b>${esc(j.message || "Задача " + j.id)}</b><span class="badge ${j.status === "failed" ? "bad" : ""}">${esc(j.status)}</span>
    <progress max="1" value="${fin && j.status === "done" ? 1 : pct}"></progress>
    <span class="muted">${j.files_done}/${j.files_total} файлов · ${fmtBytes(j.bytes_done)} / ${fmtBytes(j.bytes_total)}</span>
    ${fin ? `<button class="btn secondary" data-close>✕</button>` : `<button class="btn secondary" data-cancel>Отмена</button>`}
    ${j.error ? `<span style="color:var(--danger)">${esc(j.error)}</span>` : ""}`;
  const c = $("[data-cancel]", el); if (c) c.onclick = () => post(`/jobs/${j.id}/cancel`);
  const x = $("[data-close]", el); if (x) x.onclick = () => { el.remove(); delete jobEls[j.id]; };
}

function trackJob(id, onFinish) {
  if (onFinish) jobDone[id] = onFinish;
  if (tracked.has(id)) return;
  tracked.add(id);
  const es = new EventSource(`/api/jobs/${id}/events`);
  es.onmessage = ev => {
    const j = JSON.parse(ev.data); renderJob(j);
    if (finished.includes(j.status)) { es.close(); tracked.delete(id); (jobDone[id] || (() => {}))(j); if (current.onJobFinished) current.onJobFinished(j); }
  };
  es.onerror = () => { es.close(); tracked.delete(id); };
}
const startJob = async (path, body, onFinish) => { try { const { job_id } = await post(path, body); trackJob(job_id, onFinish); } catch (e) { alertErr(e); } };

/* после повторного открытия страницы подхватываем активные задачи */
async function resumeJobs() { for (const j of await api("/jobs")) if (!finished.includes(j.status)) trackJob(j.id); }

/* ---- навигация ---- */
let current = { name: null };
const screens = {};
function show(name) {
  current = { name };
  document.querySelectorAll("#nav button").forEach(b => b.classList.toggle("active", b.dataset.screen === name));
  $("#main").innerHTML = "";
  screens[name]().catch(alertErr);
}
$("#nav").onclick = e => { if (e.target.dataset.screen) show(e.target.dataset.screen); };

async function loadStatus() {
  const s = await api("/status");
  $("#warnings").innerHTML = s.warnings.map(w => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
}

/* ---- Источники ---- */
screens.roots = async () => {
  const main = $("#main");
  const roots = await api("/roots");
  main.innerHTML = `<div class="card"><h2>Источники сканирования</h2>
    <div class="row"><input id="rpath" size="50" placeholder="Путь: D:\\Фото или /home/user/photos"><button class="btn" id="radd">Добавить</button><button class="btn secondary" id="rbrowse">Обзор…</button></div>
    <div id="browser"></div>
    <table><tr><th>Путь</th><th>Том</th><th>Файлов</th><th>Объём</th><th>Последний скан</th><th></th></tr>
    ${roots.map(r => `<tr><td class="path">${esc(r.path)}</td><td>${esc(r.volume_id)}</td><td>${r.files}</td><td>${fmtBytes(r.bytes)}</td><td>${esc(r.last_scan || "—")}</td>
      <td><button class="btn" data-scan="${r.id}">Сканировать</button> <button class="btn secondary" data-del="${r.id}">Удалить</button></td></tr>`).join("") || `<tr><td colspan="6" class="muted">Добавьте каталог или диск</td></tr>`}</table></div>`;
  $("#radd").onclick = async () => { try { await post("/roots", { path: $("#rpath").value.trim() }); show("roots"); } catch (e) { alertErr(e); } };
  $("#rbrowse").onclick = () => browse("");
  main.querySelectorAll("[data-scan]").forEach(b => b.onclick = () => startJob(`/roots/${b.dataset.scan}/scan`, {}, j => {
    if (j.result) alert(`Скан завершён: новых ${j.result.new}, изменённых ${j.result.updated}, пропало ${j.result.missing}, пропущено неподдерживаемых ${j.result.skipped_unsupported}, ошибок доступа ${j.result.errors.length}`);
    loadStatus(); if (current.name === "roots") show("roots");
  }));
  main.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (await confirmDialog("Удалить источник?", "Записи каталога для него будут удалены. Файлы на диске не затрагиваются.", "Удалить")) { await api(`/roots/${b.dataset.del}`, { method: "DELETE" }); show("roots"); }
  });
};
async function browse(path) {
  const r = await api("/fs/list?path=" + encodeURIComponent(path));
  const el = $("#browser");
  el.innerHTML = `<div class="card"><div class="path">${esc(r.path || "Диски")}</div>
    ${r.parent !== null ? `<div><a href="#" data-p="${esc(r.parent)}">⬆ вверх</a></div>` : ""}
    ${r.dirs.map(d => `<div><a href="#" data-p="${esc(d)}">📁 ${esc(d)}</a></div>`).join("")}
    ${r.path ? `<button class="btn" id="pick">Выбрать этот каталог</button>` : ""}</div>`;
  el.querySelectorAll("[data-p]").forEach(a => a.onclick = e => { e.preventDefault(); browse(a.dataset.p); });
  const pick = $("#pick", el); if (pick) pick.onclick = () => { $("#rpath").value = r.path; el.innerHTML = ""; };
}

/* ---- Каталог ---- */
const sel = new Set();
let shown = [];

/* удаление: в карантин (восстановимо) либо навсегда */
function deleteDialog(n, bytes) {
  return new Promise(resolve => {
    const d = document.createElement("div"); d.className = "dlg";
    d.innerHTML = `<div class="box"><h2>Удалить выбранные файлы?</h2><div>Файлов: <b>${n}</b>, объём <b>${fmtBytes(bytes)}</b>.</div>
      <label style="display:block;margin-top:10px"><input type="radio" name="dm" value="quarantine" checked> В карантин (папка <code>_duplicates</code>, можно восстановить)</label>
      <label style="display:block"><input type="radio" name="dm" value="permanent"> Навсегда (без возможности восстановления)</label>
      <div class="row" style="margin-top:16px;justify-content:flex-end"><button class="btn secondary" id="no">Отмена</button><button class="btn danger" id="ok">Удалить</button></div></div>`;
    document.body.append(d);
    $("#no", d).onclick = () => { d.remove(); resolve(null); };
    $("#ok", d).onclick = () => { const m = d.querySelector("input[name=dm]:checked").value; d.remove(); resolve(m); };
  });
}

/* просмотр фото/видео на весь экран, навигация стрелками */
function openViewer(index) {
  if (index < 0) return;
  const v = document.createElement("div"); v.className = "viewer";
  document.body.append(v);
  const render = () => {
    const f = shown[index];
    const media = f.kind === "video" ? `<video src="/api/files/${f.id}/view" controls autoplay></video>` : `<img src="/api/files/${f.id}/view" alt="">`;
    v.innerHTML = `<button class="vbtn vclose" title="Закрыть (Esc)">✕</button><button class="vbtn vprev" title="Назад (←)">‹</button><button class="vbtn vnext" title="Вперёд (→)">›</button>
      <div class="vmedia">${media}</div><div class="vcap">${esc(f.root_path)}/${esc(f.rel_path)} · ${esc(f.eff_date || "без даты")} · ${fmtBytes(f.size)} · ${index + 1}/${shown.length}</div>`;
    $(".vclose", v).onclick = close; $(".vprev", v).onclick = () => go(-1); $(".vnext", v).onclick = () => go(1);
  };
  const go = d => { const i = index + d; if (i >= 0 && i < shown.length) { index = i; render(); } };
  const key = e => { if (e.key === "Escape") close(); else if (e.key === "ArrowLeft") go(-1); else if (e.key === "ArrowRight") go(1); };
  function close() { document.removeEventListener("keydown", key); v.remove(); }
  v.onclick = e => { if (e.target === v || e.target.classList.contains("vmedia")) close(); };
  document.addEventListener("keydown", key);
  render();
}
screens.catalog = async () => {
  const main = $("#main");
  const roots = await api("/roots");
  main.innerHTML = `<div class="card"><div class="row">
    <select id="fsort"><option value="desc">Новые сверху</option><option value="asc">Старые сверху</option></select>
    <select id="fkind"><option value="">Все типы</option><option value="photo">Фото</option><option value="raw">RAW</option><option value="video">Видео</option></select>
    <select id="froot"><option value="">Все источники</option>${roots.map(r => `<option value="${r.id}">${esc(r.path)}</option>`).join("")}</select>
    <select id="fstatus"><option value="">Любой статус</option><option value="present">present</option><option value="missing">missing</option><option value="quarantined">quarantined</option></select>
    <label><input type="checkbox" id="fnodate"> без даты</label>
    <button class="btn secondary" id="fgo">Показать</button></div>
    <div class="row"><input type="datetime-local" id="mdate"><button class="btn" id="mset">Задать дату выбранным</button><button class="btn secondary" id="mreset">Сбросить ручную дату</button><button class="btn danger" id="mdel">Удалить выбранные</button><span id="selcount" class="muted"></span></div></div>
    <div id="feed"></div>`;
  const load = async () => {
    const q = new URLSearchParams({ sort: $("#fsort").value, limit: 500 });
    for (const [k, id] of [["kind", "fkind"], ["root_id", "froot"], ["status", "fstatus"]]) if ($("#" + id).value) q.set(k, $("#" + id).value);
    if ($("#fnodate").checked) q.set("no_date", "true");
    const data = await api("/catalog?" + q);
    shown = data.days.flatMap(d => d.files);
    for (const id of [...sel]) if (!shown.some(f => f.id === id)) sel.delete(id);
    $("#selcount").textContent = sel.size ? `выбрано: ${sel.size}` : "";
    $("#feed").innerHTML = `<div class="muted">Всего: ${data.total}${data.total > 500 ? " (показаны первые 500)" : ""}</div>` + data.days.map(d => `<div class="card"><h3>${esc(d.day || "Без даты")} <span class="muted">· ${d.count}</span></h3><div class="grid">
      ${d.files.map(f => `<div class="thumb"><input type="checkbox" data-id="${f.id}" ${sel.has(f.id) ? "checked" : ""}>
        <img loading="lazy" style="cursor:zoom-in" data-open="${f.id}" src="/api/files/${f.id}/thumb" alt="">
        <div class="cap" title="${esc(f.rel_path)}">${esc(f.rel_path.split("/").pop())}</div>
        <div class="cap muted">${esc(f.date_source || "—")} ${f.is_derived ? '<span class="badge">производный</span>' : ""} ${f.status !== "present" ? `<span class="badge warn">${esc(f.status)}</span>` : ""}</div></div>`).join("")}</div></div>`).join("");
    $("#feed").querySelectorAll("input[data-id]").forEach(c => c.onchange = () => { c.checked ? sel.add(+c.dataset.id) : sel.delete(+c.dataset.id); $("#selcount").textContent = sel.size ? `выбрано: ${sel.size}` : ""; });
    $("#feed").querySelectorAll("img[data-open]").forEach(i => i.onclick = () => openViewer(shown.findIndex(f => f.id === +i.dataset.open)));
  };
  $("#fgo").onclick = load;
  $("#mdel").onclick = async () => {
    if (!sel.size) return alert("Выберите файлы");
    const files = shown.filter(f => sel.has(f.id));
    const mode = await deleteDialog(files.length, files.reduce((a, f) => a + f.size, 0));
    if (!mode) return;
    try {
      const r = await post("/files/delete", { file_ids: files.map(f => f.id), permanent: mode === "permanent", confirm: true });
      sel.clear(); if (r.failed.length) alert(`Не удалено: ${r.failed.length}\n` + r.failed.map(x => x.reason).join("\n"));
      load(); loadStatus();
    } catch (e) { alertErr(e); }
  };
  $("#mset").onclick = async () => {
    if (!sel.size || !$("#mdate").value) return alert("Выберите файлы и дату");
    await api("/files/date", { method: "PATCH", body: { file_ids: [...sel], taken_at: $("#mdate").value.length === 16 ? $("#mdate").value + ":00" : $("#mdate").value } }); load();
  };
  $("#mreset").onclick = async () => { if (sel.size) { await api("/files/date", { method: "PATCH", body: { file_ids: [...sel], taken_at: null } }); load(); } };
  await load();
};

/* ---- Дубли ---- */
screens.dupes = async () => {
  const main = $("#main");
  const [groups, quarantine] = await Promise.all([api("/dupes"), api("/quarantine")]);
  main.innerHTML = `<div class="card"><h2>Точные дубли</h2><div class="row"><button class="btn" id="dscan">Найти дубли</button><span class="muted">Группы: ${groups.length}</span></div></div>
    ${groups.map(g => `<div class="card"><div class="row"><b>${fmtBytes(g.size)}</b><span class="muted path">${esc(g.sha256.slice(0, 16))}…</span>
      <button class="btn danger" data-q="${g.id}">Лишние копии в карантин</button></div>
      <table>${g.members.map(m => `<tr class="${m.is_keeper ? "keeper" : ""}"><td><input type="radio" name="k${g.id}" data-g="${g.id}" data-f="${m.id}" ${m.is_keeper ? "checked" : ""}> оригинал</td>
        <td class="path">${esc(m.root_path)}/${esc(m.rel_path)}</td><td>${esc(m.eff_date || "—")}</td></tr>`).join("")}</table></div>`).join("")}
    <div class="card"><h2>Карантин</h2>${quarantine.length ? `<table>${quarantine.map(f => `<tr><td class="path">${esc(f.root_path)}/${esc(f.rel_path)}</td><td>${fmtBytes(f.size)}</td><td><button class="btn secondary" data-r="${f.id}">Восстановить</button></td></tr>`).join("")}</table>` : `<span class="muted">Пусто</span>`}</div>`;
  $("#dscan").onclick = () => startJob("/dupes/scan", {}, () => current.name === "dupes" && show("dupes"));
  main.querySelectorAll("input[data-g]").forEach(r => r.onchange = async () => { await post(`/dupes/${r.dataset.g}/keeper`, { file_id: +r.dataset.f }); show("dupes"); });
  main.querySelectorAll("[data-q]").forEach(b => b.onclick = async () => {
    const g = groups.find(x => x.id == b.dataset.q); const n = g.members.filter(m => !m.is_keeper).length;
    if (await confirmDialog("Перенести копии в карантин?", `Будет перенесено файлов: <b>${n}</b>, объём <b>${fmtBytes(n * g.size)}</b>. Файлы не удаляются, их можно восстановить.`, "В карантин")) {
      try { await post(`/dupes/${g.id}/quarantine`, { confirm: true }); show("dupes"); } catch (e) { alertErr(e); }
    }
  });
  main.querySelectorAll("[data-r]").forEach(b => b.onclick = async () => {
    const [res] = await post("/quarantine/restore", { file_ids: [+b.dataset.r] });
    if (!res.restored) alert("Не восстановлено: " + res.reason); show("dupes");
  });
};

/* ---- Перемещение ---- */
let lastPlan = null;
screens.move = async () => {
  const main = $("#main");
  const [roots, settings] = await Promise.all([api("/roots"), api("/settings")]);
  const opts = roots.map(r => `<option value="${r.id}">${esc(r.path)}</option>`).join("");
  main.innerHTML = `<div class="card"><h2>План перемещения</h2>
    <div class="row">Источник: <select id="msrc"><option value="">все</option>${opts}</select> Корень назначения: <select id="mdst">${opts}</select> <button class="btn" id="mplan">Построить план</button></div>
    <details><summary>Настройки</summary><div class="row" style="margin-top:8px">Шаблон: <input id="stpl" value="${esc(settings.path_template)}"> Граница суток (час): <input id="sbound" type="number" min="0" max="23" value="${settings.day_boundary_hour}" style="width:60px"> <button class="btn secondary" id="ssave">Сохранить</button></div></details></div>
    <div id="plan"></div>`;
  $("#ssave").onclick = async () => { try { await api("/settings", { method: "PUT", body: { path_template: $("#stpl").value, day_boundary_hour: +$("#sbound").value } }); alert("Сохранено"); } catch (e) { alertErr(e); } };
  $("#mplan").onclick = async () => {
    try { lastPlan = await post("/plans", { dest_root_id: +$("#mdst").value, source_root_id: $("#msrc").value ? +$("#msrc").value : null }); renderPlan(); } catch (e) { alertErr(e); }
  };
  if (lastPlan) renderPlan();
};
function renderPlan() {
  const p = lastPlan, s = p.summary;
  $("#plan").innerHTML = `<div class="card"><h2>План #${p.id} <span class="badge">${esc(p.status)}</span></h2>
    <div>Файлов: <b>${s.files}</b> · Объём: <b>${fmtBytes(s.bytes)}</b> · Конфликты имён: <b>${s.conflicts}</b> · Без даты: <b>${s.no_date}</b> · Уже на месте: ${s.skipped}</div>
    <div>Свободно на целевом диске: ${fmtBytes(s.free_bytes)}, требуется для копирования между дисками: ${fmtBytes(s.cross_volume_bytes)} ${s.enough_space ? "✓" : '<span class="badge bad">не хватает места</span>'}</div>
    <div class="row" style="margin-top:10px"><button class="btn danger" id="pexec" ${p.status !== "draft" || !s.enough_space || !s.files ? "disabled" : ""}>Выполнить план</button>
      <button class="btn secondary" id="prollback" ${["done", "cancelled"].includes(p.status) ? "" : "disabled"}>Откатить</button></div>
    <table><tr><th>Откуда</th><th>Куда</th><th></th></tr>${p.items.slice(0, 200).map(i => `<tr><td class="path">${esc(i.src_rel)}</td><td class="path">${esc(i.dst_rel)}</td><td>${i.conflict ? '<span class="badge warn">конфликт</span>' : ""} ${i.status !== "planned" ? `<span class="badge">${esc(i.status)}</span>` : ""}</td></tr>`).join("")}</table>
    ${p.items.length > 200 ? `<div class="muted">Показаны первые 200 операций</div>` : ""}</div>`;
  const refresh = async () => { lastPlan = await api(`/plans/${p.id}`); if (current.name === "move") renderPlan(); };
  $("#pexec").onclick = async () => {
    if (await confirmDialog("Выполнить перемещение?", `Будет перемещено файлов: <b>${s.files}</b>, объём <b>${fmtBytes(s.bytes)}</b>. Исходные файлы переносятся (не копируются); при копировании между дисками оригинал удаляется только после сверки SHA-256. Операции можно откатить.`, "Переместить")) {
      try { const { job_id } = await post(`/plans/${p.id}/execute`, { confirm: true }); trackJob(job_id, refresh); } catch (e) { alertErr(e); }
    }
  };
  $("#prollback").onclick = async () => {
    if (await confirmDialog("Откатить перемещение?", "Файлы вернутся на прежние пути, если они не изменялись на новом месте.", "Откатить")) {
      try { const { job_id } = await post(`/plans/${p.id}/rollback`, { confirm: true }); trackJob(job_id, refresh); } catch (e) { alertErr(e); }
    }
  };
}

/* ---- HEIC ---- */
screens.heic = async () => {
  const main = $("#main");
  const [items, settings] = await Promise.all([api("/heic"), api("/settings")]);
  const converted = items.filter(i => i.jpeg_id && i.status === "present");
  main.innerHTML = `<div class="card"><h2>Конвертация HEIC → JPEG</h2>
    <div class="row">Качество JPEG: <input id="hq" type="number" min="1" max="100" value="${settings.jpeg_quality}" style="width:70px">
    <button class="btn" id="hgo">Конвертировать выбранные</button> <button class="btn secondary" id="hall">Выбрать все без JPEG</button></div>
    <p class="muted">JPEG сохраняется рядом с оригиналом; EXIF и цветовой профиль переносятся. Оригинал остаётся на месте.</p>
    <table><tr><th></th><th>Файл</th><th>Размер</th><th>JPEG</th></tr>${items.map(i => `<tr><td><input type="checkbox" data-h="${i.id}" ${i.status !== "present" ? "disabled" : ""}></td><td class="path">${esc(i.rel_path)}</td><td>${fmtBytes(i.size)}</td><td>${i.jpeg_id ? "✓" : "—"} ${i.status === "quarantined" ? '<span class="badge warn">в карантине</span>' : ""}</td></tr>`).join("") || `<tr><td colspan="4" class="muted">HEIC-файлов в каталоге нет</td></tr>`}</table></div>
    <div class="card"><h3>Оригиналы</h3><div>Сконвертированных HEIC, оригиналы которых ещё на месте: <b>${converted.length}</b></div>
    <button class="btn danger" id="hq2" ${converted.length ? "" : "disabled"}>Перенести оригиналы в карантин</button></div>`;
  $("#hall").onclick = () => main.querySelectorAll("[data-h]").forEach(c => { const it = items.find(i => i.id == c.dataset.h); c.checked = !it.jpeg_id && !c.disabled; });
  $("#hgo").onclick = () => {
    const ids = [...main.querySelectorAll("[data-h]:checked")].map(c => +c.dataset.h);
    if (!ids.length) return alert("Выберите файлы");
    startJob("/heic/convert", { file_ids: ids, quality: +$("#hq").value }, j => { if (j.result?.errors?.length) alert("Ошибки: " + j.result.errors.map(e => e.path).join(", ")); if (current.name === "heic") show("heic"); });
  };
  $("#hq2").onclick = async () => {
    if (await confirmDialog("Перенести оригиналы в карантин?", `Файлов HEIC: <b>${converted.length}</b>, объём <b>${fmtBytes(converted.reduce((a, i) => a + i.size, 0))}</b>. Их можно восстановить из карантина.`, "В карантин")) {
      try { const r = await post("/heic/quarantine-originals", { confirm: true }); alert(`Перенесено: ${r.quarantined}, ошибок: ${r.failed}`); show("heic"); } catch (e) { alertErr(e); }
    }
  };
};

/* ---- Журнал ---- */
screens.journal = async () => {
  const ops = await api("/operations?limit=300");
  $("#main").innerHTML = `<div class="card"><h2>Журнал операций</h2><table><tr><th>#</th><th>Время</th><th>Тип</th><th>Откуда</th><th>Куда</th><th>Статус</th><th>Примечание</th></tr>
    ${ops.map(o => `<tr><td>${o.id}</td><td>${esc(o.ts)}</td><td>${esc(o.kind)}</td><td class="path">${esc(o.src_rel)}</td><td class="path">${esc(o.dst_rel)}</td><td><span class="badge ${o.status === "failed" ? "bad" : o.status === "done" ? "" : "warn"}">${esc(o.status)}</span></td><td>${esc(o.note || "")}</td></tr>`).join("")}</table></div>`;
};

loadStatus(); resumeJobs(); show("roots");
