"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const labels = { burned: "已刻录", pending: "待刻录", empty: "未登记", replaced: "留存索引" };
const integer = new Intl.NumberFormat("zh-CN");
const decimal = new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 });
const discName = (number) => `BD-${number}`;
const state = {
  route: "home", overview: null, mode: "name", search: null, searchRun: 0,
  folder: null, folderRun: 0, selection: new Set(), clipboard: null, locationRun: 0,
};
const dialog = $("#dialog");
let toastTimer;

function el(tag, className = "", content = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (content !== null) node.textContent = content;
  return node;
}

function icon(name) {
  const node = el("i", `icon i-${name}`);
  node.setAttribute("aria-hidden", "true");
  return node;
}

function btn(text, className, action, graphic = "") {
  const node = el("button", className);
  node.type = "button";
  if (graphic) node.append(icon(graphic));
  if (text) node.append(text);
  node.addEventListener("click", action);
  return node;
}

function iconBtn(label, graphic, action) {
  const node = btn("", "icon-button", action, graphic);
  node.title = label;
  node.setAttribute("aria-label", label);
  return node;
}

function notice(text, error = false) {
  return el("p", error ? "empty-message error" : "empty-message", text);
}

function toast(message) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 3200);
}

async function api(method, route, data) {
  const query = new URLSearchParams();
  if (method === "GET") {
    for (const [key, value] of Object.entries(data || {})) {
      if (value !== null && value !== undefined && value !== "") query.set(key, value);
    }
  }
  const options = method === "POST"
    ? { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) }
    : { cache: "no-store" };
  const response = await fetch(`/api/${route}${query.size ? `?${query}` : ""}`, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "操作未完成，请重试。");
  return result;
}

const get = (route, params) => api("GET", route, params);
const post = (route, data) => api("POST", route, data);

function size(bytes, exact = false) {
  if (exact) return `${integer.format(bytes)} 字节`;
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const power = Math.min(units.length - 1, Math.max(0, Math.floor(Math.log(bytes) / Math.log(1000))));
  return `${decimal.format(bytes / 1000 ** power)} ${units[power]}`;
}

function badge(status) {
  return el("span", `badge ${status}`, labels[status] || status);
}

function renderOverview() {
  const data = state.overview;
  $("#burned-count").textContent = integer.format(data.burned_count);
  $("#burned-capacity").textContent = decimal.format(data.burned_bytes / 1e9);
  $("#burned-capacity").title = size(data.burned_bytes, true);
  $("#burned-detail").textContent = `${integer.format(data.burned_file_count)} 个已刻录文件`;
  $("#indexed-count").textContent = integer.format(data.file_count);
  $("#connection-status").textContent = "SQLite 已连接";
  $(".side-footer").classList.remove("offline");

  const bar = $("#status-bar");
  bar.classList.add("has-data");
  bar.replaceChildren(...data.discs.map((disc) => {
    const segment = el("span", disc.status);
    segment.title = `${discName(disc.number)} · ${labels[disc.status]}`;
    return segment;
  }));
  const legend = $("#status-legend");
  const counts = new Map([["burned", data.burned_count], ["pending", data.pending_count],
    ["empty", data.discs.length - data.burned_count - data.pending_count]]);
  legend.replaceChildren(...[...counts].map(([status, count]) => {
    const node = el("span", status);
    node.append(el("b"), `${labels[status]} ${count} 张`);
    return node;
  }));
}

async function refreshOverview() {
  state.overview = await get("overview");
  renderOverview();
  renderDiscTree();
}

function route(hash) {
  return ["discs", "folders"].includes(hash) ? hash : "home";
}

function navigate(next, updateHash = true, locating = false) {
  const changed = next !== state.route;
  if (!locating) state.locationRun += 1;
  state.route = next;
  if (updateHash) {
    const hash = next === "home" ? "" : `#${next}`;
    if (location.hash !== hash) history.pushState(null, "", location.pathname + hash);
  }
  $("#home").hidden = next !== "home";
  $("#details").hidden = next === "home";
  $("#disc-tree").hidden = next !== "discs";
  $("#disc-filters").hidden = next !== "discs";
  $("#explorer").hidden = next !== "folders";
  $("#detail-description").textContent = next === "folders"
    ? "按归档文件夹查找光盘，整理文件夹和光盘快捷方式。"
    : "按光盘编号浏览备份内容和备注。";
  $$(".side-link[data-route], .browser-tab").forEach((node) => {
    const active = node.dataset.route === next ||
      (node.classList.contains("browser-tab") && node.dataset.route === "discs" && next === "folders");
    node.classList.toggle("active", active);
    if (active) node.setAttribute("aria-current", "page");
    else node.removeAttribute("aria-current");
  });
  $$("[data-view]").forEach((node) => node.setAttribute("aria-pressed", String(node.dataset.view === next)));
  if (next === "folders" && !state.folder) loadFolder();
  if (changed) window.scrollTo(0, 0);
}

function setSearchMode(mode) {
  state.mode = mode;
  state.search = null;
  state.searchRun += 1;
  $("#search-results").replaceChildren();
  $("#search-by-name").hidden = mode !== "name";
  $("#search-by-date").hidden = mode !== "date";
  $$("[data-search-mode]").forEach((node) =>
    node.setAttribute("aria-pressed", String(node.dataset.searchMode === mode)));
}

function pagination(result, loadPage) {
  if (result.pages < 2) return null;
  const bar = el("nav", "pagination");
  bar.setAttribute("aria-label", "结果分页");
  const previous = btn("上一页", "btn secondary", () => loadPage(result.page - 1));
  const next = btn("下一页", "btn secondary", () => loadPage(result.page + 1));
  previous.disabled = result.page <= 1;
  next.disabled = result.page >= result.pages;
  bar.append(previous, el("span", "", `${result.page} / ${result.pages}`), next);
  return bar;
}

function tableHead(columns) {
  const head = el("thead");
  const row = el("tr");
  for (const column of columns) {
    const cell = el("th", "", column);
    cell.scope = "col";
    row.append(cell);
  }
  head.append(row);
  return head;
}

async function search(page = 1) {
  const run = ++state.searchRun;
  const output = $("#search-results");
  output.replaceChildren(notice("正在搜索…"));
  try {
    const result = await get("search", { ...state.search, page });
    if (run !== state.searchRun) return;
    if (!result.total) {
      output.replaceChildren(notice("没有找到符合条件的文件。"));
      return;
    }
    const wrap = el("div", "table-wrap");
    const table = el("table", "result-table");
    const rows = el("tbody");
    table.append(tableHead(["文件名和路径", "修改日期", "光盘"]), rows);
    for (const item of result.items) {
      const row = el("tr");
      const file = el("td");
      file.append(el("strong", "", item.name), el("small", "", item.path));
      const disc = el("td");
      disc.append(btn(discName(item.disc), "btn secondary",
        () => openDisc(item.disc, item.id, item.content_id), "disc"));
      row.append(file, el("td", "", item.modified_date || "—"), disc);
      rows.append(row);
    }
    wrap.append(table);
    output.replaceChildren(notice(`找到 ${integer.format(result.total)} 个文件`), wrap);
    const pages = pagination(result, search);
    if (pages) output.append(pages);
  } catch (error) {
    if (run === state.searchRun) output.replaceChildren(notice(error.message, true));
  }
}

function noteButton(kind, id, title, note, update) {
  const button = iconBtn(`编辑备注 · ${title}`, "edit", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openNote(kind, id, title, note, (value) => {
      note = value;
      button.classList.toggle("has-note", Boolean(value));
      update(value);
    });
  });
  button.classList.toggle("has-note", Boolean(note));
  return button;
}

function treeBox(locator, parent = null) {
  const node = el("div", "tree-box");
  node.branch = { locator, parent, page: 0, request: null, run: 0 };
  return node;
}

function discGroup(disc, locator = { disc: disc.number }) {
  const group = el("details", "disc-group");
  group.dataset.disc = disc.number;
  const summary = el("summary", "disc-summary");
  const note = el("span", "row-note", disc.note || "");
  note.title = disc.note || "";
  summary.append(el("span", "chevron"), icon("disc"), el("strong", "disc-name", discName(disc.number)),
    badge(disc.status), note);
  if (disc.status !== "empty" && disc.status !== "replaced") {
    summary.append(el("small", "row-meta", `${integer.format(disc.file_count)} 个文件 · ${size(disc.bytes)}`));
  }
  if (disc.status !== "replaced") {
    summary.append(noteButton("disc", disc.number, discName(disc.number), disc.note || "", (value) => {
      disc.note = value;
      note.textContent = value;
      note.title = value;
    }));
  }
  const box = treeBox(locator);
  group.append(summary, box);
  group.addEventListener("toggle", () => {
    if (group.open && !box.branch.page && !box.branch.request) loadTree(box);
  });
  return group;
}

function renderDiscTree() {
  const output = $("#disc-tree");
  if (!state.overview) {
    output.replaceChildren(notice("正在读取光盘编号…"));
    return;
  }
  const filter = $("#status-filter").value;
  const discs = state.overview.discs.filter((disc) => filter === "all" || filter === disc.status);
  output.replaceChildren(...discs.map((disc) => discGroup(disc)));
  if (!discs.length) output.append(notice("此状态下暂无光盘。"));
}

function treeItem(item, locator) {
  const isFolder = item.kind === "folder";
  const row = el(isFolder ? "summary" : "div", isFolder ? "tree-row folder" : "tree-row file");
  row.dataset.node = item.id;
  if (isFolder) row.append(el("span", "chevron"));
  const name = isFolder ? el("span", "tree-name", item.name)
    : btn(item.name, "tree-name", () => openFileInfo(item, locator));
  const note = el("span", "row-note", item.note || "");
  note.title = item.note || "";
  row.append(icon(isFolder ? "folder" : "file"), name, note);
  if (!isFolder) row.append(el("span", "row-meta", size(item.bytes)));
  row.append(noteButton("node", item.id, item.name, item.note || "", (value) => {
    item.note = value;
    note.textContent = value;
    note.title = value;
  }));
  if (!isFolder) return row;
  const group = el("details", "tree-folder");
  const box = treeBox(locator, item.id);
  group.append(row, box);
  group.addEventListener("toggle", () => {
    if (group.open && !box.branch.page && !box.branch.request) loadTree(box);
  });
  return group;
}

function loadTree(box, page = 1) {
  const branch = box.branch;
  if (branch.request?.page === page) return branch.request.promise;
  if (branch.page === page && !branch.request) return Promise.resolve(true);
  const run = ++branch.run;
  box.replaceChildren(notice("正在读取文件树…"));
  const promise = get("tree", { ...branch.locator, parent: branch.parent, page }).then((result) => {
    if (run !== branch.run) return false;
    branch.page = result.page;
    box.replaceChildren(...result.items.map((item) => treeItem(item, branch.locator)));
    if (!result.items.length) box.append(notice(branch.parent ? "此文件夹为空。" : "这张光盘暂无文件条目。"));
    const pages = pagination(result, (newPage) => loadTree(box, newPage));
    if (pages) box.append(pages);
    return true;
  }).catch((error) => {
    if (run === branch.run) {
      branch.page = 0;
      box.replaceChildren(notice(error.message, true), btn("重试", "btn secondary", () => loadTree(box, page)));
    }
    return false;
  }).finally(() => { if (run === branch.run) branch.request = null; });
  branch.request = { page, promise };
  return promise;
}

async function openDisc(number, id = null, content = null, archived = false, oldNote = "") {
  const run = ++state.locationRun;
  try {
    const locator = content ? { content } : { disc: number };
    const found = id ? await get("locate", { ...locator, id }) : null;
    if (run !== state.locationRun) return;
    archived = archived || found?.status === "replaced";
    number = found?.disc || number;
    navigate("discs", true, true);
    const filter = $("#status-filter");
    if (filter.value !== "all" && !archived && !$(`[data-disc="${number}"]`, $("#disc-tree"))) {
      filter.value = "all";
      renderDiscTree();
    }
    $(".archived-tree", $("#disc-tree"))?.remove();
    let group;
    if (archived) {
      group = discGroup({ number, status: "replaced", note: oldNote }, locator);
      group.classList.add("archived-tree");
      $("#disc-tree").prepend(group);
    } else {
      group = $(`.disc-group[data-disc="${number}"]`, $("#disc-tree"));
    }
    if (!group) throw new Error("未找到此光盘。");
    $$(".highlight", $("#disc-tree")).forEach((row) => row.classList.remove("highlight"));
    group.open = true;
    let box = $(":scope > .tree-box", group);
    let focus = group;
    for (const step of found?.folders || []) {
      if (!await loadTree(box, step.page) || run !== state.locationRun) return;
      const row = $(`:scope > .tree-folder > [data-node="${step.id}"]`, box);
      if (!row) throw new Error("文件路径已变更，请重新检索。");
      row.parentElement.open = true;
      box = $(":scope > .tree-box", row.parentElement);
    }
    if (!await loadTree(box, found?.page || 1) || run !== state.locationRun) return;
    if (found) {
      focus = $(`:scope > [data-node="${id}"], :scope > .tree-folder > [data-node="${id}"]`, box) || group;
      if (focus !== group) {
        focus.classList.add("highlight");
        if (found.item.kind === "folder") focus.parentElement.open = true;
      }
    }
    focus.scrollIntoView({ block: "nearest" });
  } catch (error) {
    if (run === state.locationRun) toast(error.message);
  }
}

function openFileInfo(item, locator) {
  const disc = locator.disc ? discName(locator.disc) : "留存索引";
  const fields = [
    `光盘　${disc}`, `路径　${item.path}`, `修改日期　${item.modified_date || "—"}`,
    `大小　${size(item.bytes, true)}`, item.note ? `备注　${item.note}` : "",
  ].filter(Boolean);
  openDialog(item.name, fields.join("\n"));
}

async function loadFolder(id = null, keepSelection = false) {
  const run = ++state.folderRun;
  const root = $("#explorer");
  if (!keepSelection) state.selection.clear();
  if (!state.folder) root.replaceChildren(notice("正在读取文件夹…"));
  root.setAttribute("aria-busy", "true");
  root.inert = true;
  try {
    const result = await get("explorer", { folder: id });
    if (run !== state.folderRun) return;
    state.folder = result;
    const validIds = new Set(result.items.map((item) => item.id));
    state.selection = new Set([...state.selection].filter((itemId) => validIds.has(itemId)));
    renderExplorer();
  } catch (error) {
    if (run !== state.folderRun) return;
    if (state.folder) toast(error.message);
    else root.replaceChildren(notice(error.message, true), btn("重试", "btn secondary", () => loadFolder(id)));
  } finally {
    if (run === state.folderRun) {
      root.removeAttribute("aria-busy");
      root.inert = false;
    }
  }
}

function setSelection(ids) {
  state.selection = ids;
  const root = $("#explorer");
  for (const row of $$("tbody tr", root)) {
    const selected = ids.has(Number(row.dataset.id));
    row.classList.toggle("selected", selected);
    $("input", row).checked = selected;
  }
  const all = $("#explorer-all");
  if (all) {
    const total = state.folder.items.length;
    all.checked = total > 0 && total === ids.size;
    all.indeterminate = ids.size > 0 && ids.size < total;
  }
  for (const control of $$("[data-needs-selection]", root)) control.disabled = !ids.size;
  $("#selection-count").textContent = ids.size ? `已选择 ${ids.size} 项` : "";
}

function renderExplorer() {
  const { folder, breadcrumbs, items } = state.folder;
  const toolbar = el("div", "explorer-toolbar");
  const newFolder = btn("新建文件夹", "btn secondary", () => openNewFolder(folder), "plus");
  const copy = btn("复制", "btn secondary", () => copySelection("copy"));
  const cut = btn("剪切", "btn secondary", () => copySelection("cut"));
  const paste = btn("粘贴", "btn secondary", pasteSelection);
  paste.id = "explorer-paste";
  paste.disabled = !state.clipboard;
  copy.dataset.needsSelection = "";
  cut.dataset.needsSelection = "";
  const annotate = noteButton("explorer", folder.id, folder.name, folder.note || "", (value) => {
    folder.note = value;
    renderExplorer();
  });
  annotate.append("备注");
  annotate.classList.add("btn", "secondary", "explorer-annotate");
  toolbar.append(newFolder, cut, copy, paste, annotate);
  const address = el("nav", "explorer-address");
  address.setAttribute("aria-label", "文件夹路径");
  const up = btn("↑ 上一级", "crumb", () => loadFolder(folder.parent_id));
  up.disabled = folder.parent_id === null;
  address.append(up);
  breadcrumbs.forEach((entry) => {
    address.append(el("span", "crumb-divider", "/"),
      btn(entry.name, "crumb", () => loadFolder(entry.id), entry.parent_id === null ? "folder" : ""));
  });
  const root = $("#explorer");
  root.replaceChildren(toolbar, address);
  if (folder.note) root.append(el("p", "explorer-folder-note", folder.note));
  if (!items.length) root.append(notice("此文件夹为空。"));
  else {
    const table = el("table", "explorer-table");
    const header = tableHead(["", "名称", "状态", "备注", ""]);
    const all = el("input");
    all.type = "checkbox";
    all.id = "explorer-all";
    all.setAttribute("aria-label", "选择全部项目");
    all.addEventListener("change", () => setSelection(all.checked ? new Set(items.map((item) => item.id)) : new Set()));
    $("th", header).append(all);
    const body = el("tbody");
    for (const item of items) {
      const row = el("tr");
      row.dataset.id = item.id;
      const check = el("input");
      check.type = "checkbox";
      check.setAttribute("aria-label", `选择 ${item.name}`);
      check.addEventListener("change", () => {
        if (check.checked) state.selection.add(item.id);
        else state.selection.delete(item.id);
        setSelection(state.selection);
      });
      const checked = el("td");
      checked.append(check);
      const isFolder = item.kind === "folder";
      const name = el("td");
      name.append(btn(item.name, "explorer-name", () => {
        if (isFolder) loadFolder(item.id);
        else openDisc(item.disc, item.node_id, item.content_id, item.status === "replaced");
      }, isFolder ? "folder" : "disc"));
      if (item.path) name.append(el("small", "", item.path));
      const status = el("td");
      if (isFolder) status.textContent = "文件夹";
      else status.append(badge(item.status));
      const note = el("td", "explorer-note", item.note || "—");
      const action = el("td");
      action.append(noteButton("explorer", item.id, item.name, item.note || "", (value) => {
        item.note = value;
        note.textContent = value || "—";
      }));
      row.append(checked, name, status, note, action);
      body.append(row);
    }
    table.append(header, body);
    const wrap = el("div", "table-wrap");
    wrap.append(table);
    root.append(wrap);
  }
  const foot = el("div", "explorer-foot");
  const selected = el("span");
  selected.id = "selection-count";
  foot.append(el("span", "", `${integer.format(items.length)} 个项目`), selected);
  root.append(foot);
  setSelection(state.selection);
}

function copySelection(operation) {
  if ($("#explorer").inert || !state.selection.size) return;
  state.clipboard = { operation, ids: [...state.selection] };
  $("#explorer-paste").disabled = false;
  toast(`${operation === "cut" ? "已剪切" : "已复制"} ${state.selection.size} 项`);
}

async function pasteSelection() {
  const clip = state.clipboard;
  if ($("#explorer").inert || !clip || !state.folder) return;
  const folder = state.folder.folder.id;
  const run = state.folderRun;
  const control = $("#explorer-paste");
  if (control.disabled) return;
  control.disabled = true;
  try {
    await post("explorer/paste", { parent: folder, ...clip });
    if (clip.operation === "cut" && state.clipboard === clip) state.clipboard = null;
    if (run === state.folderRun) await loadFolder(folder);
    toast("已粘贴");
  } catch (error) {
    toast(error.message);
  } finally {
    const paste = $("#explorer-paste");
    if (paste) paste.disabled = !state.clipboard;
  }
}

function openDialog(title, description = "", fields = null, submit = null, submitText = "保存") {
  if (dialog.open) dialog.close();
  const head = el("div", "dialog-head");
  const heading = el("h2", "", title);
  heading.id = "dialog-title";
  head.append(heading, iconBtn("关闭", "close", () => dialog.close()));
  const body = el(submit ? "form" : "div");
  if (description) body.append(el("p", "dialog-description", description));
  if (fields) body.append(fields);
  const errors = el("p", "dialog-error error");
  errors.setAttribute("role", "alert");
  errors.hidden = true;
  const actions = el("div", "dialog-actions");
  actions.append(btn(submit ? "取消" : "关闭", "btn secondary", () => dialog.close()));
  if (submit) {
    const save = el("button", "btn primary", submitText);
    save.type = "submit";
    actions.append(save);
    body.addEventListener("submit", async (event) => {
      event.preventDefault();
      save.disabled = true;
      errors.hidden = true;
      try {
        const message = await submit();
        if (dialog.contains(body)) dialog.close();
        if (message) toast(message);
      } catch (error) {
        errors.textContent = error.message;
        errors.hidden = false;
      } finally {
        save.disabled = false;
      }
    });
  }
  body.append(errors, actions);
  dialog.replaceChildren(head, body);
  dialog.showModal();
  $("input:not([type=checkbox]), textarea, select", body)?.focus();
}

function field(label, input) {
  const node = el("label");
  node.append(label, input);
  return node;
}

function openNote(kind, id, title, note, update) {
  const text = el("textarea");
  text.maxLength = 20000;
  text.value = note;
  text.placeholder = "写下你的备注";
  const fields = el("div", "dialog-fields");
  fields.append(field("备注", text));
  openDialog(title, "", fields, async () => {
    await post("note", { kind, id, note: text.value });
    update(text.value);
    return "备注已保存";
  });
}

function openNewFolder(parent) {
  const name = el("input");
  name.type = "text";
  name.placeholder = "例如 照片归档";
  name.required = true;
  name.maxLength = 120;
  const fields = el("div", "dialog-fields");
  fields.append(field("文件夹名称", name));
  openDialog("新建文件夹", "", fields, async () => {
    await post("explorer/folder", { parent: parent.id, name: name.value });
    await loadFolder(parent.id);
    return "文件夹已创建";
  }, "创建");
}

function discSelect(discs) {
  const select = el("select");
  select.required = true;
  const initial = el("option", "", "选择光盘编号");
  initial.value = "";
  initial.disabled = true;
  initial.selected = true;
  select.append(initial);
  for (const disc of discs) {
    const option = el("option", "", `${discName(disc.number)} · ${labels[disc.status]}`);
    option.value = disc.number;
    select.append(option);
  }
  return select;
}

function openRelabel() {
  if (!state.overview) {
    toast("光盘列表正在加载，请稍后。");
    return;
  }
  const discs = state.overview.discs;
  const source = discSelect(discs.filter((disc) => disc.content_id));
  const target = discSelect(discs);
  const info = el("p", "dialog-description");
  const fields = el("div", "dialog-fields");
  fields.append(field("原光盘编号", source), field("新光盘编号", target), info);
  function update() {
    const same = source.value && source.value === target.value;
    target.setCustomValidity(same ? "请选择不同的光盘编号。" : "");
    const disc = discs.find((entry) => entry.number === Number(target.value));
    info.textContent = disc?.content_id ? `${discName(disc.number)} 当前索引将被覆盖，旧索引可在补标记历史中查看。` : "";
  }
  source.addEventListener("change", update);
  target.addEventListener("change", update);
  openDialog("补标记光盘", "原编号的文件索引和备注将转到新编号，原编号腾空。", fields, async () => {
    await post("relabel", { source: Number(source.value), target: Number(target.value) });
    await refreshOverview();
    if (state.folder) await loadFolder(state.folder.folder.id);
    if (state.search) search();
    return "补标记已保存";
  }, "确认补标记");
}

async function openHistory() {
  try {
    const { items } = await get("relabels");
    if (!items.length) {
      openDialog("补标记历史", "暂无补标记记录。");
      return;
    }
    const wrap = el("div", "table-wrap");
    const table = el("table");
    const rows = el("tbody");
    table.append(tableHead(["编号变更", "登记时间", "覆盖记录"]), rows);
    for (const item of items) {
      const when = new Date(item.created_at);
      const formatted = Number.isNaN(when.valueOf()) ? item.created_at : when.toLocaleString("zh-CN", { hour12: false });
      const row = el("tr");
      const previous = el("td", "", item.overwrote ? "" : "空编号");
      if (item.overwrote) {
        previous.append(btn("查看旧索引", "text-button", () => {
          dialog.close();
          openDisc(item.target, null, item.target_content_id, true, item.target_note);
        }));
      }
      row.append(el("td", "", `${discName(item.source)} → ${discName(item.target)}`),
        el("td", "", formatted), previous);
      rows.append(row);
    }
    wrap.append(table);
    openDialog("补标记历史", "", wrap);
  } catch (error) {
    toast(error.message);
  }
}

$$("[data-route]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.route)));
$$("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
$$("[data-action=history]").forEach((button) => button.addEventListener("click", openHistory));
$("[data-action=relabel]").addEventListener("click", openRelabel);
$$("[data-search-mode]").forEach((button) => button.addEventListener("click", () => setSearchMode(button.dataset.searchMode)));
$("#status-filter").addEventListener("change", renderDiscTree);
$("#search-form").addEventListener("submit", (event) => {
  event.preventDefault();
  state.search = state.mode === "name"
    ? { mode: "name", q: $("#name-query").value }
    : { mode: "date", from: $("#date-from").value, to: $("#date-to").value };
  search();
});

document.addEventListener("keydown", (event) => {
  if (state.route !== "folders" || dialog.open || !(event.ctrlKey || event.metaKey) || event.altKey ||
    event.target.matches("textarea, input:not([type=checkbox]), select, [contenteditable]")) return;
  const key = event.key.toLowerCase();
  if (key === "v" && state.clipboard) {
    event.preventDefault();
    pasteSelection();
  } else if (["c", "x"].includes(key) && state.selection.size) {
    event.preventDefault();
    copySelection(key === "x" ? "cut" : "copy");
  }
});

window.addEventListener("popstate", () => navigate(route(location.hash.slice(1)), false));
navigate(route(location.hash.slice(1)), false);
refreshOverview().catch((error) => {
  $("#connection-status").textContent = "数据库连接失败";
  $(".side-footer").classList.add("offline");
  $("#disc-tree").replaceChildren(notice(error.message, true));
  toast(error.message);
});
