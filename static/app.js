/* ==========================================================================
   德语智读 · German AI Reader
   原生 JavaScript 单文件应用：书架 / 阅读器 / 划词私教 / 设置
   ========================================================================== */
(() => {
  "use strict";

  const API = "/api";
  const PREFIX = "gar:";
  const MAX_SELECTION = 500;
  const CONTENT_WIDTHS = { narrow: 640, comfortable: 780, wide: 920 };
  const THINKING_EFFORT_LABELS = { low: "低", medium: "中", high: "高", max: "最高" };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  const state = {
    view: "library",
    health: null,
    settings: null,
    system: null,
    books: [],
    stats: { books: 0, words: 0, annotations: 0, chat_messages: 0 },
    book: null,
    bookId: null,
    chapter: null,
    chapterIndex: 0,
    annotations: [],
    activeAnnotationId: null,

    theme: "light",
    fontSize: 21,
    mode: "scroll",
    lineWidth: "comfortable",
    guideCollapsed: false,

    page: { index: 0, total: 1, width: 0, gap: 64 },
    requestToken: 0,
    searchToken: 0,
    readerPoll: null,
    libraryPoll: null,
    guidePollCount: 0,
    bgPollCount: 0,

    selection: null,
    autoTimer: null,
    inFlight: new Set(),
    pendingSelection: null,
    annotationStream: null,
    chatStream: null,
    toolbarVisible: false,
    lastMultiParaWarn: 0,

    recognition: null,
    recording: false,
    recognitionHold: false,
    spaceHeld: false,
    voiceLang: "zh-CN",
    chatSending: false,
    guideBusy: false,
    bgBusy: false,

    settingsSaving: false,
    clearKeys: new Set(),
    settingsDirty: false,

    importing: false,
    importFile: null,
    importTimers: [],
    modalStack: [],
    confirmResolver: null,
    lastFocus: null,

    opened: {},
    lastBookId: null,
    initialized: false,
    dragDepth: 0,
  };

  /* ====================================================================== */
  /* Utilities                                                              */
  /* ====================================================================== */
  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[char]));
  }

  function icon(name, className = "icon") {
    return `<svg class="${className}" aria-hidden="true"><use href="#i-${name}"></use></svg>`;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("zh-CN").format(Number(value) || 0);
  }

  function formatWords(value) {
    const words = Number(value) || 0;
    if (words >= 10000) return `${(words / 10000).toFixed(words >= 100000 ? 0 : 1)} 万词`;
    return `${formatNumber(words)} 词`;
  }

  function formatMinutes(words) {
    return `${Math.max(1, Math.round((Number(words) || 0) / 160))} 分钟`;
  }

  function formatBookDate(value) {
    if (!value) return "";
    const date = new Date(String(value).replace(" ", "T") + (String(value).includes("Z") ? "" : "Z"));
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
  }

  function parseServerDate(value) {
    if (!value) return null;
    const text = String(value);
    const date = new Date(text.replace(" ", "T") + (text.includes("Z") || text.includes("+") ? "" : "Z"));
    return Number.isNaN(date.getTime()) ? null : date;
  }

  function formatClock(value) {
    const date = parseServerDate(value);
    if (!date) return "";
    return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
  }

  function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
      clearTimeout(timer);
      timer = setTimeout(() => fn(...args), wait);
    };
  }

  function hashString(text) {
    let hash = 0;
    for (let i = 0; i < text.length; i += 1) {
      hash = (hash * 31 + text.charCodeAt(i)) >>> 0;
    }
    return hash;
  }

  function prefGet(key, fallback = null) {
    try {
      const value = localStorage.getItem(PREFIX + key);
      return value === null ? fallback : value;
    } catch {
      return fallback;
    }
  }

  function prefSet(key, value) {
    try {
      localStorage.setItem(PREFIX + key, String(value));
    } catch {
      /* localStorage 被禁用时静默降级 */
    }
  }

  function readOpened() {
    try {
      const parsed = JSON.parse(prefGet("opened", "{}"));
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch {
      return {};
    }
  }

  function markOpened(bookId) {
    state.opened[String(bookId)] = Date.now();
    prefSet("opened", JSON.stringify(state.opened));
    state.lastBookId = Number(bookId);
    prefSet("lastBook", bookId);
  }

  function isOpened(bookId) {
    return Boolean(state.opened[String(bookId)]);
  }

  function posKey(bookId, chapterIndex) {
    return `pos.${bookId}.${chapterIndex}`;
  }

  function readSavedRatio(bookId, chapterIndex) {
    const raw = prefGet(posKey(bookId, chapterIndex), null);
    const value = raw === null ? NaN : Number.parseFloat(raw);
    return Number.isFinite(value) ? clamp(value, 0, 1) : null;
  }

  async function copyText(text) {
    const value = String(text ?? "");
    if (!value) return false;
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      try {
        const textarea = document.createElement("textarea");
        textarea.value = value;
        textarea.style.cssText = "position:fixed;opacity:0;left:-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        const ok = document.execCommand("copy");
        textarea.remove();
        return ok;
      } catch {
        return false;
      }
    }
  }

  /* ====================================================================== */
  /* API                                                                    */
  /* ====================================================================== */
  async function api(path, options = {}) {
    const opts = { cache: "no-store", ...options, headers: { ...(options.headers || {}) } };
    if (opts.body && !(opts.body instanceof FormData)) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.body);
    }
    let response;
    try {
      response = await fetch(API + path, opts);
    } catch {
      const error = new Error("无法连接本地服务，请确认终端中的服务仍在运行。");
      error.network = true;
      throw error;
    }
    const text = await response.text();
    let payload = null;
    if (text) {
      try {
        payload = JSON.parse(text);
      } catch {
        payload = null;
      }
    }
    if (!response.ok) {
      const error = new Error((payload && payload.detail) || `请求失败（HTTP ${response.status}）`);
      error.status = response.status;
      error.code = payload && payload.code;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function parseSSEEvent(raw) {
    let event = "message";
    const dataLines = [];
    for (const line of raw.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return null;
    const rawData = dataLines.join("\n");
    let data = rawData;
    try {
      data = JSON.parse(rawData);
    } catch {
      /* 保留纯文本 */
    }
    return { event, data };
  }

  /**
   * POST + SSE 读取器。返回 { promise, abort }。
   * handlers: { meta, delta, done, error }，可返回 false 提前结束。
   */
  function streamSSE(path, body, handlers = {}) {
    const controller = new AbortController();
    const promise = (async () => {
      let response;
      try {
        response = await fetch(API + path, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Accept: "text/event-stream",
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
      } catch (error) {
        if (error.name === "AbortError") throw error;
        const wrapped = new Error("无法连接本地服务，请确认服务仍在运行。");
        wrapped.network = true;
        throw wrapped;
      }
      if (!response.ok) {
        const text = await response.text();
        let payload = null;
        try {
          payload = JSON.parse(text);
        } catch {
          payload = null;
        }
        const error = new Error((payload && payload.detail) || `请求失败（HTTP ${response.status}）`);
        error.status = response.status;
        error.code = payload && payload.code;
        throw error;
      }
      if (!response.body) {
        throw new Error("当前浏览器不支持流式响应。");
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let buffer = "";
      let stop = false;
      try {
        while (!stop) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary = buffer.indexOf("\n\n");
          while (boundary >= 0) {
            const raw = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            const parsed = parseSSEEvent(raw);
            if (parsed) {
              const handler = handlers[parsed.event];
              if (handler) {
                const result = await handler(parsed.data);
                if (result === false) {
                  stop = true;
                  break;
                }
              }
            }
            boundary = buffer.indexOf("\n\n");
          }
        }
      } finally {
        try {
          await reader.cancel();
        } catch {
          /* ignore */
        }
      }
    })();
    return { promise, abort: () => controller.abort() };
  }

  /* ====================================================================== */
  /* Toasts & modals                                                        */
  /* ====================================================================== */
  function toast(message, type = "info", timeout = 4300) {
    const container = $("#toast-container");
    if (!container || !message) return;
    const icons = { success: "check", error: "info", info: "info" };
    const node = document.createElement("div");
    node.className = `toast ${type}`;
    node.innerHTML = `${icon(icons[type] || "info")}<div class="toast-text"></div><button class="toast-close" type="button" aria-label="关闭提示">${icon("close")}</button>`;
    node.querySelector(".toast-text").textContent = String(message);
    const close = () => {
      if (!node.isConnected) return;
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 200);
    };
    node.querySelector(".toast-close").addEventListener("click", close);
    container.appendChild(node);
    while (container.children.length > 5) container.firstElementChild.remove();
    if (timeout > 0) setTimeout(close, timeout);
  }

  function openModal(id) {
    const modal = document.getElementById(id);
    if (!modal || !modal.hidden) return;
    state.lastFocus = document.activeElement;
    modal.hidden = false;
    state.modalStack = state.modalStack.filter((item) => item !== id);
    state.modalStack.push(id);
    document.body.style.overflow = "hidden";
    requestAnimationFrame(() => {
      const focusable = modal.querySelector(
        "input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled]), [tabindex]:not([tabindex='-1'])"
      );
      if (focusable) focusable.focus({ preventScroll: true });
    });
  }

  function closeModal(id, options = {}) {
    const modal = document.getElementById(id);
    if (!modal || modal.hidden) return;
    if (id === "modal-import" && state.importing && !options.force) {
      toast("正在导入，请稍候片刻…", "info");
      return;
    }
    modal.hidden = true;
    state.modalStack = state.modalStack.filter((item) => item !== id);
    if (!state.modalStack.length) {
      document.body.style.overflow = "";
      if (state.lastFocus && document.contains(state.lastFocus)) {
        try {
          state.lastFocus.focus({ preventScroll: true });
        } catch {
          /* ignore */
        }
      }
    }
    if (id === "modal-confirm" && state.confirmResolver) {
      const resolve = state.confirmResolver;
      state.confirmResolver = null;
      resolve(false);
    }
  }

  function closeTopModal() {
    const id = state.modalStack[state.modalStack.length - 1];
    if (id) closeModal(id);
  }

  function confirmAction(options = {}) {
    return new Promise((resolve) => {
      if (state.confirmResolver) {
        state.confirmResolver(false);
        state.confirmResolver = null;
      }
      state.confirmResolver = resolve;
      $("#confirm-title").textContent = options.title || "确认操作";
      $("#confirm-message").textContent = options.message || "";
      const ok = $("#confirm-ok");
      ok.textContent = options.confirmText || "确认";
      ok.className = `btn ${options.danger === false ? "primary" : "danger"}`;
      ok.onclick = () => {
        state.confirmResolver = null;
        closeModal("modal-confirm", { silent: true, force: true });
        resolve(true);
      };
      openModal("modal-confirm");
    });
  }

  function trapFocus(event) {
    if (!state.modalStack.length || event.key !== "Tab") return;
    const modal = document.getElementById(state.modalStack[state.modalStack.length - 1]);
    if (!modal) return;
    const nodes = $$(
      "button:not([disabled]), input:not([type=hidden]):not([disabled]), textarea:not([disabled]), select:not([disabled]), a[href], [tabindex]:not([tabindex='-1'])",
      modal
    ).filter((node) => node.offsetParent !== null);
    if (!nodes.length) return;
    const first = nodes[0];
    const last = nodes[nodes.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  /* ====================================================================== */
  /* Preferences / theme                                                    */
  /* ====================================================================== */
  function loadPreferences() {
    state.theme = prefGet("theme", "light") === "dark" ? "dark" : "light";
    const font = Number(prefGet("font", "0"));
    state.fontSize = Number.isFinite(font) && font >= 16 && font <= 32 ? font : 21;
    const mode = prefGet("mode", "scroll");
    state.mode = mode === "paginated" ? "paginated" : "scroll";
    const width = prefGet("lineWidth", "comfortable");
    state.lineWidth = ["narrow", "comfortable", "wide"].includes(width) ? width : "comfortable";
    state.voiceLang = prefGet("voiceLang", "zh-CN") === "de-DE" ? "de-DE" : "zh-CN";
    state.guideCollapsed = prefGet("guide", "0") === "1";
    state.opened = readOpened();
    const lastBook = Number(prefGet("lastBook", "0"));
    state.lastBookId = Number.isFinite(lastBook) && lastBook > 0 ? lastBook : null;
  }

  function applyServerDefaults() {
    if (!state.settings) return;
    if (prefGet("theme", null) === null && ["light", "dark"].includes(state.settings.theme)) {
      state.theme = state.settings.theme;
    }
    if (prefGet("font", null) === null && Number(state.settings.font_size) >= 16) {
      state.fontSize = clamp(Number(state.settings.font_size), 16, 32);
    }
    if (prefGet("mode", null) === null && ["scroll", "paginated"].includes(state.settings.reading_mode)) {
      state.mode = state.settings.reading_mode;
    }
    if (prefGet("lineWidth", null) === null && CONTENT_WIDTHS[state.settings.line_width]) {
      state.lineWidth = state.settings.line_width;
    }
  }

  function applyThemeIcons() {
    const light = state.theme === "light";
    $$("#library-theme-toggle use, #reader-theme-toggle use").forEach((use) => {
      use.setAttribute("href", light ? "#i-moon" : "#i-sun");
    });
    $$("#library-theme-toggle, #reader-theme-toggle").forEach((button) => {
      button.title = light ? "切换到夜间墨色" : "切换到羊皮纸";
    });
    const meta = document.querySelector('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", light ? "#f3ecdd" : "#171613");
  }

  function applyPreferences() {
    const root = document.documentElement;
    root.dataset.theme = state.theme;
    root.style.setProperty("--reader-font-size", `${state.fontSize}px`);
    root.style.setProperty("--content-width", `${CONTENT_WIDTHS[state.lineWidth] || 780}px`);
    $("#font-size-label").textContent = String(state.fontSize);
    applyThemeIcons();
    updateModeButtons();
    if ($("#settings-font-size")) {
      $("#settings-font-size").value = String(state.fontSize);
      $("#settings-font-label").textContent = `${state.fontSize} px`;
    }
    const settingsWidth = $("#settings-line-width");
    if (settingsWidth) settingsWidth.value = state.lineWidth;
  }

  function updateModeButtons() {
    $$("#mode-switch [data-mode]").forEach((button) => {
      button.classList.toggle("active", button.dataset.mode === state.mode);
      button.setAttribute("aria-pressed", String(button.dataset.mode === state.mode));
    });
    $$("#settings-mode [data-mode-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.modeChoice === state.mode);
    });
    const surface = $("#reading-surface");
    if (surface) {
      surface.classList.toggle("scroll-mode", state.mode === "scroll");
      surface.classList.toggle("paginated", state.mode === "paginated");
    }
  }

  const syncPreferences = debounce(async () => {
    if (!state.initialized || !state.settings) return;
    try {
      const settings = await api("/settings", {
        method: "PUT",
        body: {
          theme: state.theme,
          font_size: state.fontSize,
          reading_mode: state.mode,
          line_width: state.lineWidth,
        },
      });
      state.settings = settings;
    } catch {
      /* 偏好同步失败不影响本地阅读 */
    }
  }, 1000);

  function setTheme(theme, options = {}) {
    const next = theme === "dark" ? "dark" : "light";
    if (state.theme === next) return;
    state.theme = next;
    prefSet("theme", next);
    applyPreferences();
    if (state.settings) state.settings.theme = next;
    if (options.sync !== false) syncPreferences();
  }

  function toggleTheme() {
    setTheme(state.theme === "light" ? "dark" : "light");
    toast(state.theme === "dark" ? "已切换到夜间墨色" : "已切换到羊皮纸", "info", 1800);
  }

  function setMode(mode, options = {}) {
    const next = mode === "paginated" ? "paginated" : "scroll";
    if (state.mode === next) return;
    const ratio = state.chapter ? getReadingRatio() : 0;
    state.mode = next;
    prefSet("mode", next);
    updateModeButtons();
    updateFooter();
    hideSelectionToolbar();
    if (state.view === "reader" && state.chapter) {
      const surface = $("#reading-surface");
      if (next === "paginated") {
        if (surface) surface.scrollTop = 0;
        requestAnimationFrame(() => {
          measurePages();
          applyPageByRatio(ratio);
        });
      } else {
        clearPaginationStyles();
        requestAnimationFrame(() => {
          setScrollRatio(ratio);
          updateFooter();
        });
      }
    }
    if (state.settings) state.settings.reading_mode = next;
    if (options.sync !== false) syncPreferences();
  }

  function setFontSize(size, options = {}) {
    const next = clamp(Math.round(Number(size) || 21), 16, 32);
    if (state.fontSize === next) return;
    const ratio = state.view === "reader" && state.chapter ? getReadingRatio() : null;
    state.fontSize = next;
    prefSet("font", next);
    document.documentElement.style.setProperty("--reader-font-size", `${next}px`);
    $("#font-size-label").textContent = String(next);
    if (state.settings) state.settings.font_size = next;
    if (ratio !== null) {
      requestAnimationFrame(() => {
        if (state.mode === "paginated") {
          measurePages();
          applyPageByRatio(ratio);
        } else {
          setScrollRatio(ratio);
        }
        updateFooter();
      });
    }
    if (options.sync !== false) syncPreferences();
  }

  /* ====================================================================== */
  /* Library                                                                */
  /* ====================================================================== */
  function updateConnection(online, text) {
    const pill = $("#library-connection");
    const label = $("#library-connection-text");
    if (!pill || !label) return;
    pill.classList.toggle("online", Boolean(online));
    pill.classList.toggle("offline", !online);
    label.textContent = text || (online ? "本地服务在线" : "本地服务离线");
  }

  function renderLibraryLoading() {
    $("#library-loading").hidden = false;
    $("#book-grid").hidden = true;
    $("#library-empty").hidden = true;
  }

  async function loadLibrary(options = {}) {
    if (!options.quiet) renderLibraryLoading();
    try {
      const data = await api("/books");
      state.books = data.books || [];
      state.stats = data.stats || state.stats;
      renderLibrary();
      scheduleLibraryPoll();
      return true;
    } catch (error) {
      if (!options.quiet) {
        showScreenError(error.message);
      }
      return false;
    }
  }

  async function refreshLibrary(options = {}) {
    if (state.view !== "library") return;
    try {
      const data = await api("/books");
      state.books = data.books || [];
      state.stats = data.stats || state.stats;
      renderLibrary();
      scheduleLibraryPoll();
    } catch (error) {
      if (!options.quiet) toast(error.message, "error");
    }
  }

  function scheduleLibraryPoll() {
    clearTimeout(state.libraryPoll);
    if (state.view !== "library") return;
    if (!state.books.some((book) => book.background_job && book.background_job.status === "running")) return;
    state.libraryPoll = setTimeout(async () => {
      await refreshLibrary({ quiet: true });
      scheduleLibraryPoll();
    }, 4000);
  }

  function renderLibrary() {
    const books = state.books;
    $("#library-loading").hidden = true;
    $("#library-stats").textContent = books.length
      ? `${books.length} 本书 · ${formatWords(state.stats.words)} · ${formatNumber(state.stats.annotations)} 条划词笔记`
      : "还没有书，先导入一本吧";
    $("#library-footer-version").textContent = state.health ? `v${state.health.version || "1.2.2"}` : "v1.2.2";

    const grid = $("#book-grid");
    grid.innerHTML = books.map(bookCardHtml).join("");
    grid.hidden = books.length === 0;
    $("#library-empty").hidden = books.length > 0;

    const continueButton = $("#hero-continue");
    if (books.length) {
      const target =
        books.find((book) => book.id === state.lastBookId) ||
        books.find((book) => book.current_chapter > 0) ||
        books[0];
      continueButton.hidden = false;
      $("#hero-continue-label").textContent = target.current_chapter > 0
        ? `继续读《${target.title}》`
        : `打开《${target.title}》`;
      continueButton.title = `打开《${target.title}》`;
      continueButton.dataset.bookId = String(target.id);
    } else {
      continueButton.hidden = true;
    }
    $("#library-stats").title = `${formatNumber(state.stats.words)} 词 · ${formatNumber(
      state.stats.chat_messages
    )} 条追问`;
  }

  function bookCardHtml(book) {
    const hue = [22, 30, 12, 198, 158, 268, 342][hashString(book.title || "Buch") % 7];
    const title = book.title || "未命名书籍";
    const initial = (title.replace(/^[«„"'\s]+/, "").trim()[0] || "B").toUpperCase();
    const started = isOpened(book.id) || Number(book.current_chapter) > 0;
    const chapterCount = Math.max(1, Number(book.chapter_count) || 1);
    const current = clamp(Number(book.current_chapter) || 0, 0, chapterCount - 1);
    const percent = started ? Math.round(((current + 1) / chapterCount) * 100) : 0;
    const status = started ? `继续 · 第 ${current + 1} 章` : "尚未开始";
    const jobRunning = book.background_job && book.background_job.status === "running";
    const jobBlock = jobRunning
      ? `<span class="cover-job"><span class="spinner"></span>生成背景</span>`
      : book.has_background
        ? `<span class="cover-job">AI 背景就绪</span>`
        : `<span class="cover-job">${book.file_type ? book.file_type.toUpperCase() : ""}</span>`;
    return `
      <article class="book-card" data-book-id="${book.id}" tabindex="0" role="button" aria-label="打开《${escapeHtml(title)}》">
        <div class="book-cover" style="--cover-h:${hue}">
          <div class="cover-top">
            <span class="cover-type">${escapeHtml((book.file_type || "txt").toUpperCase())}</span>
            ${jobBlock}
          </div>
          <span class="cover-initial">${escapeHtml(initial)}</span>
          <span class="cover-rule"></span>
          <div class="book-actions">
            <a class="icon-btn" href="/api/books/${book.id}/file" data-download title="下载原文件" aria-label="下载原文件">${icon("download")}</a>
            <button class="icon-btn" type="button" data-delete-book="${book.id}" title="删除书籍" aria-label="删除书籍">${icon("trash")}</button>
          </div>
        </div>
        <div class="book-body">
          <h3 class="book-title">${escapeHtml(title)}</h3>
          <p class="book-author">${escapeHtml(book.author || "作者未知")}${book.created_at ? ` · ${formatBookDate(book.created_at)}` : ""}</p>
          <div class="book-meta">
            <span>${chapterCount} 章 · ${formatWords(book.word_count)}</span>
            <span>${book.guides_ready || 0}/${chapterCount} 导读</span>
          </div>
          <div class="book-progress"><i style="width:${percent}%"></i></div>
          <div class="book-progress-label"><span>${status}</span><span>${percent}%</span></div>
        </div>
      </article>`;
  }

  async function deleteBook(bookId) {
    const book = state.books.find((item) => item.id === Number(bookId)) || (state.book && state.book.id === Number(bookId) ? state.book : null);
    const title = book ? book.title : "这本书";
    const confirmed = await confirmAction({
      title: "删除书籍",
      message: `确定删除《${title}》吗？章节、所有划词笔记、追问记录与原文件都会被永久清理，无法恢复。`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api(`/books/${bookId}`, { method: "DELETE" });
      toast("书籍已删除", "success");
      if (state.bookId === Number(bookId)) {
        showLibraryView({ refresh: false });
      }
      await loadLibrary({ quiet: true });
    } catch (error) {
      toast(error.message, "error");
    }
  }

  /* ====================================================================== */
  /* Reader view / chapter loading                                          */
  /* ====================================================================== */
  function showReaderView() {
    $("#library-view").hidden = true;
    $("#reader-view").hidden = false;
    state.view = "reader";
    document.body.classList.add("reader-open");
    closeMobileSidebars();
    updateAiBadge();
    window.scrollTo(0, 0);
  }

  function showLibraryView(options = {}) {
    state.requestToken += 1;
    clearTimeout(state.readerPoll);
    state.readerPoll = null;
    hideSelectionToolbar();
    closeMobileSidebars();
    if (state.view === "reader") savePosition();
    state.view = "library";
    $("#reader-view").hidden = true;
    $("#library-view").hidden = false;
    document.body.classList.remove("reader-open");
    document.title = "德语智读 · German AI Reader";
    resetAiPanel();
    if (!options.fromRoute) {
      history.replaceState({}, "", location.pathname);
    }
    window.scrollTo(0, 0);
    if (options.refresh !== false) refreshLibrary({ quiet: true });
  }

  async function openBook(bookId, chapterIndex = null, options = {}) {
    const id = Number(bookId);
    if (!id) return;
    if (
      !options.force &&
      state.view === "reader" &&
      state.bookId === id &&
      state.chapter &&
      (chapterIndex === null || Number(chapterIndex) === state.chapterIndex)
    ) {
      return;
    }
    const token = ++state.requestToken;
    state.bookId = id;
    state.book = null;
    state.chapter = null;
    state.chapterIndex = 0;
    state.annotations = [];
    state.activeAnnotationId = null;
    state.guidePollCount = 0;
    state.bgPollCount = 0;
    resetAiPanel();
    showReaderView();
    $("#reader-book-title").textContent = "正在载入…";
    $("#reader-chapter-label").textContent = "";
    $("#toc-list").innerHTML = "";
    $("#toc-count").textContent = "—";
    renderReaderSkeleton();
    setReaderLoading(true);

    try {
      const book = await api(`/books/${id}`);
      if (token !== state.requestToken) return;
      state.book = book;
      let index = chapterIndex === null ? Number(book.current_chapter) || 0 : Number(chapterIndex);
      index = clamp(index, 0, Math.max(0, (book.chapter_count || 1) - 1));
      updateReaderMeta();
      renderToc();
      renderBackground();
      updateAiBadge();
      $("#search-input").value = "";
      $("#search-results").innerHTML = `<p class="panel-hint">输入至少 2 个字符，可搜索全书正文并跳转。</p>`;
      switchSidebarTab("toc");
      markOpened(id);
      if (options.updateHistory === false) {
        history.replaceState({ book: id, chapter: index }, "", `#/book/${id}/${index}`);
      } else {
        history.pushState({ book: id, chapter: index }, "", `#/book/${id}/${index}`);
      }
      await loadChapter(index, { token, updateHistory: false, initial: true });
      document.title = `${book.title} · 德语智读`;
    } catch (error) {
      if (token !== state.requestToken) return;
      if (error.status === 404) {
        showLibraryView({ refresh: true });
        toast("这本书不存在或已被删除", "error");
        return;
      }
      renderReaderError(error.message, () => openBook(id, chapterIndex, { ...options, force: true }));
    }
  }

  async function loadChapter(index, options = {}) {
    if (!state.book) return;
    const myToken = options.token ?? ++state.requestToken;
    if (options.token === undefined) state.requestToken = myToken;
    const target = Number(index);
    if (!Number.isFinite(target)) return;
    hideSelectionToolbar();
    closeMobileSidebars();
    setReaderLoading(true);
    state.guidePollCount = 0;
    state.activeAnnotationId = null;
    resetAiPanel();
    try {
      const chapter = await api(`/books/${state.bookId}/chapters/${target}`);
      if (myToken !== state.requestToken) return;
      state.chapter = chapter;
      state.chapterIndex = Number(chapter.chapter_index ?? target);
      state.annotations = Array.isArray(chapter.annotations) ? chapter.annotations : [];
      state.guidePollCount = 0;
      syncChapterInBook();
      updateReaderMeta();
      renderToc();
      const saved = options.ratioOverride !== undefined
        ? options.ratioOverride
        : readSavedRatio(state.bookId, state.chapterIndex);
      renderChapterView({ ratio: saved === null ? 0 : saved, initial: Boolean(options.initial) });
      if (options.updateHistory !== false) {
        history.replaceState(
          { book: state.bookId, chapter: state.chapterIndex },
          "",
          `#/book/${state.bookId}/${state.chapterIndex}`
        );
      }
      markOpened(state.bookId);
      setReaderLoading(false);
      scheduleReaderPoll(2200);
      requestAnimationFrame(() => scrollActiveTocIntoView());
    } catch (error) {
      if (myToken !== state.requestToken) return;
      setReaderLoading(false);
      if (error.status === 404) {
        toast("章节不存在", "error");
        return;
      }
      renderReaderError(error.message, () => loadChapter(target));
    }
  }

  function renderReaderSkeleton() {
    const content = $("#reading-content");
    if (!content) return;
    content.style.transform = "none";
    content.innerHTML = `
      <div class="reader-skeleton">
        <div class="skeleton line w30"></div>
        <div class="skeleton line w80 tall"></div>
        <div class="skeleton line w100"></div>
        <div class="skeleton line w95"></div>
        <div class="skeleton line w90"></div>
        <div class="skeleton line w40"></div>
      </div>`;
    $("#footer-label").textContent = "正在载入…";
    $("#footer-progress").style.width = "0%";
    $("#footer-prev").disabled = true;
    $("#footer-next").disabled = true;
  }

  function renderReaderError(message, retry) {
    setReaderLoading(false);
    const content = $("#reading-content");
    if (!content) return;
    content.style.transform = "none";
    content.innerHTML = `
      <div class="reader-error">
        ${icon("info")}
        <h2>这一页没有打开</h2>
        <p>${escapeHtml(message)}</p>
        <button class="btn primary" type="button" id="reader-error-retry">${icon("refresh")}重试</button>
      </div>`;
    const button = $("#reader-error-retry");
    if (button) {
      button.addEventListener("click", () => {
        if (typeof retry === "function") retry();
        else if (state.chapterIndex !== null) loadChapter(state.chapterIndex);
        else if (state.bookId) openBook(state.bookId);
      });
    }
    $("#footer-label").textContent = "载入失败";
    $("#footer-prev").disabled = true;
    $("#footer-next").disabled = true;
  }

  function setReaderLoading(active) {
    const bar = $("#reader-loading-bar");
    if (bar) bar.hidden = !active;
    const surface = $("#reading-surface");
    if (surface) surface.classList.toggle("is-loading", Boolean(active) && Boolean(state.chapter));
  }

  function updateReaderMeta() {
    const book = state.book;
    const chapter = state.chapter;
    if (!book) return;
    $("#reader-book-title").textContent = book.title || "未命名书籍";
    if (chapter) {
      const total = Number(book.chapter_count) || Number(chapter.total_chapters) || 1;
      $("#reader-chapter-label").textContent = `第 ${chapter.chapter_index + 1} / ${total} 章 · ${formatNumber(
        chapter.word_count
      )} 词`;
      document.title = `${chapter.title} · ${book.title}`;
    } else {
      $("#reader-chapter-label").textContent = `${book.chapter_count || 0} 章 · ${formatWords(book.word_count)}`;
    }
    updateFooter();
  }

  function syncChapterInBook() {
    if (!state.book || !state.chapter) return;
    const list = state.book.chapters || [];
    const item = list.find(
      (chapter) => chapter.id === state.chapter.id || chapter.chapter_index === state.chapterIndex
    );
    if (item) {
      item.is_guide_ready = Boolean(state.chapter.is_guide_ready);
      item.chapter_summary = state.chapter.chapter_summary;
      item.questions = state.chapter.questions || state.chapter.guiding_questions;
      item.guiding_questions = item.questions;
      item.guide_job = state.chapter.guide_job || item.guide_job;
      item.word_count = state.chapter.word_count;
    }
  }

  /* ====================================================================== */
  /* Chapter rendering                                                      */
  /* ====================================================================== */
  function renderChapterView(options = {}) {
    const chapter = state.chapter;
    const book = state.book;
    if (!chapter || !book) return;
    if (state.mode === "scroll") clearPaginationStyles();
    const ratio = options.ratio !== undefined ? options.ratio : getReadingRatio();
    const content = $("#reading-content");
    if (!content) return;
    content.classList.remove("no-anim");
    content.style.transform = state.mode === "scroll" ? "none" : "translate3d(0,0,0)";
    const total = Number(book.chapter_count) || Number(chapter.total_chapters) || 1;
    const paragraphs = (chapter.paragraphs || [])
      .map((text, index) => renderParagraph(text, index))
      .join("");
    const nextTitle = chapter.next_chapter !== null && book.chapters
      ? (book.chapters.find((item) => item.chapter_index === chapter.next_chapter) || {}).title
      : null;
    content.innerHTML = `
      <header class="chapter-heading">
        <h1 class="chapter-title" lang="de">${escapeHtml(chapter.title)}</h1>
        <p class="chapter-meta">
          <span>${formatNumber(chapter.word_count)} 词</span><span class="dot-sep"></span>
          <span>约 ${formatMinutes(chapter.word_count)}</span><span class="dot-sep"></span>
          <span>${chapter.paragraphs.length} 个自然段</span><span class="dot-sep"></span>
          <span>第 ${chapter.chapter_index + 1} / ${total} 章</span>
        </p>
      </header>
      ${guideCardHtml()}
      <article class="chapter-text" id="chapter-text" lang="de">${paragraphs}</article>
      <div class="chapter-end">
        <div class="chapter-end-card">
          <div class="chapter-end-copy">
            <span>${chapter.next_chapter !== null ? "WEITERLESEN" : "ENDE"}</span>
            <strong>${chapter.next_chapter !== null ? `下一章${nextTitle ? `：${escapeHtml(nextTitle)}` : ""}` : "全书读完"}</strong>
            <small>${chapter.next_chapter !== null ? "保持节奏，或者回到目录挑选章节。" : "可以回到书架，开始另一本德语书。"}</small>
          </div>
          ${
            chapter.next_chapter !== null
              ? `<button class="btn primary" type="button" data-goto-chapter="${chapter.next_chapter}">下一章${icon("arrow-right")}</button>`
              : `<button class="btn" type="button" data-back-library>${icon("book")}返回书架</button>`
          }
        </div>
      </div>`;
    applyActiveMark();
    updateFooter();
    requestAnimationFrame(() => {
      if (state.mode === "paginated") {
        measurePages();
        applyPageByRatio(ratio);
      } else {
        setScrollRatio(ratio);
      }
      updateFooter();
      setReaderLoading(false);
    });
  }

  function renderParagraph(text, index) {
    const safeText = String(text ?? "");
    const annotations = state.annotations
      .filter(
        (annotation) =>
          Number(annotation.paragraph_index) === index &&
          Number(annotation.start_offset) < Number(annotation.end_offset)
      )
      .sort((a, b) => Number(a.start_offset) - Number(b.start_offset));
    let html = "";
    let cursor = 0;
    for (const annotation of annotations) {
      const start = clamp(Number(annotation.start_offset) || 0, 0, safeText.length);
      const end = clamp(Number(annotation.end_offset) || 0, start, safeText.length);
      if (end <= start || start < cursor) continue;
      html += escapeHtml(safeText.slice(cursor, start));
      html += `<mark class="hl" data-annotation-id="${annotation.id}" tabindex="0" role="button" title="点击查看已保存的解析与追问">${escapeHtml(
        safeText.slice(start, end)
      )}</mark>`;
      cursor = end;
    }
    html += escapeHtml(safeText.slice(cursor));
    if (!html) html = escapeHtml(safeText);
    return `<p class="para" id="p-${index}" data-p-idx="${index}">${html}</p>`;
  }

  function guideCardHtml() {
    const chapter = state.chapter;
    const ready = Boolean(chapter.is_guide_ready);
    const job = chapter.guide_job || null;
    const running = job && job.status === "running";
    const error = job && job.status === "error" ? job.error : null;
    const collapsed = ready && state.guideCollapsed;
    let body = "";
    if (ready) {
      const questions = chapter.questions || chapter.guiding_questions || [];
      body = `
        <div class="guide-body">
          <p class="guide-summary">${escapeHtml(chapter.chapter_summary || "本章导读生成中。")}</p>
          ${
            questions.length
              ? `<ol class="guide-questions">${questions
                  .slice(0, 3)
                  .map((question, index) => `<li><span class="q-num">${index + 1}</span><span>${escapeHtml(question)}</span></li>`)
                  .join("")}</ol>`
              : ""
          }
        </div>`;
    } else if (error) {
      body = `
        <div class="guide-body">
          <p class="guide-error">${escapeHtml(error)}</p>
          <div class="guide-status">
            <button class="btn small" type="button" data-guide-regen>${icon("refresh")}重试生成</button>
            ${
              /key|密钥|配置/i.test(error)
                ? `<button class="link-btn" type="button" data-open-settings>打开设置</button>`
                : ""
            }
          </div>
        </div>`;
    } else if (running) {
      body = `
        <div class="guide-body">
          <div class="guide-skeleton"><div class="skeleton line w90"></div><div class="skeleton line w75"></div><div class="skeleton line w85"></div></div>
          <p class="guide-status"><span class="spinner"></span>AI 正在准备本章导读与探究问题…</p>
        </div>`;
    } else {
      body = `
        <div class="guide-body">
          <p class="guide-status">本章还没有导读。生成后会显示情节概要，以及 3 个关注德语细节的探究问题。</p>
          <button class="btn small" type="button" data-guide-regen>${icon("spark")}生成本章导读</button>
        </div>`;
    }
    return `
      <section class="guide-card ${collapsed ? "collapsed" : ""}" id="guide-card">
        <div class="guide-head">
          <div class="guide-title"><span class="spark">💡</span>本章导读 &amp; 探究问题</div>
          <div class="guide-head-actions">
            ${
              ready
                ? `<button class="icon-btn guide-toggle" type="button" data-guide-toggle title="${collapsed ? "展开导读" : "收起导读"}" aria-label="${collapsed ? "展开导读" : "收起导读"}">${icon("arrow-up")}</button>
                   <button class="icon-btn" type="button" data-guide-regen title="重新生成导读" aria-label="重新生成导读">${icon("refresh")}</button>`
                : ""
            }
          </div>
        </div>
        ${body}
      </section>`;
  }

  async function generateGuide(force = true) {
    if (!state.chapter || state.guideBusy) return;
    state.guideBusy = true;
    const card = $("#guide-card");
    if (card) {
      const body = card.querySelector(".guide-body");
      if (body) {
        body.innerHTML = `
          <div class="guide-skeleton"><div class="skeleton line w90"></div><div class="skeleton line w75"></div><div class="skeleton line w85"></div></div>
          <p class="guide-status"><span class="spinner"></span>正在生成导读…</p>`;
      }
    }
    try {
      const result = await api(
        `/books/${state.bookId}/chapters/${state.chapterIndex}/guide?force=${force ? "true" : "false"}`,
        { method: "POST" }
      );
      state.chapter.is_guide_ready = true;
      state.chapter.chapter_summary = result.summary || "";
      state.chapter.questions = result.questions || [];
      state.chapter.guiding_questions = state.chapter.questions;
      state.chapter.guide_job = { status: "done" };
      syncChapterInBook();
      renderToc();
      renderChapterView({ ratio: getReadingRatio() });
      toast("本章导读已生成", "success", 2600);
    } catch (error) {
      state.chapter.guide_job = { status: "error", error: error.message };
      syncChapterInBook();
      renderChapterView({ ratio: getReadingRatio() });
      toast(error.message, "error");
    } finally {
      state.guideBusy = false;
      scheduleReaderPoll(1600);
    }
  }

  function toggleGuide() {
    if (!state.chapter || !state.chapter.is_guide_ready) return;
    state.guideCollapsed = !state.guideCollapsed;
    prefSet("guide", state.guideCollapsed ? "1" : "0");
    const card = $("#guide-card");
    if (!card) return;
    card.classList.toggle("collapsed", state.guideCollapsed);
    const toggle = card.querySelector("[data-guide-toggle]");
    if (toggle) {
      toggle.title = state.guideCollapsed ? "展开导读" : "收起导读";
      toggle.setAttribute("aria-label", toggle.title);
    }
    if (state.mode === "paginated") {
      const ratio = getReadingRatio();
      requestAnimationFrame(() => {
        measurePages();
        applyPageByRatio(ratio);
        updateFooter();
      });
    }
  }

  function applyActiveMark() {
    $$("#reading-content mark.hl").forEach((mark) => {
      const active = Number(mark.dataset.annotationId) === Number(state.activeAnnotationId);
      mark.classList.toggle("active", active);
    });
  }

  /* ====================================================================== */
  /* Pagination                                                             */
  /* ====================================================================== */
  function isPaginated() {
    return state.mode === "paginated";
  }

  function clearPaginationStyles() {
    const content = $("#reading-content");
    if (!content) return;
    content.style.columnWidth = "";
    content.style.columnGap = "";
    content.style.height = "";
    content.style.transform = "none";
  }

  function measurePages() {
    if (!isPaginated() || !state.chapter) return;
    const surface = $("#reading-surface");
    const content = $("#reading-content");
    if (!surface || !content) return;
    const style = getComputedStyle(surface);
    const paddingLeft = Number.parseFloat(style.paddingLeft) || 0;
    const paddingRight = Number.parseFloat(style.paddingRight) || 0;
    const width = Math.max(140, surface.clientWidth - paddingLeft - paddingRight);
    // 列间距必须大于左右内边距，否则相邻页会从阅读区的 padding 中露出一条边。
    const gap = Math.max(56, Math.ceil(Math.max(paddingLeft, paddingRight) + 36));
    content.style.columnWidth = `${width}px`;
    content.style.columnGap = `${gap}px`;
    content.style.height = "100%";
    content.style.width = "100%";
    const scrollWidth = content.scrollWidth;
    const last = content.lastElementChild;
    let total = Math.max(1, Math.round((scrollWidth + gap) / (width + gap)));
    if (last) {
      const end = last.offsetLeft + last.offsetWidth;
      total = Math.max(total, Math.ceil((end + gap) / (width + gap)));
    }
    state.page = {
      width,
      gap,
      total,
      index: clamp(state.page.index, 0, total - 1),
    };
  }

  function applyPage(index, options = {}) {
    if (!isPaginated()) return;
    hideSelectionToolbar();
    const total = Math.max(1, state.page.total);
    const target = clamp(index, 0, total - 1);
    state.page.index = target;
    const content = $("#reading-content");
    if (content) {
      const animate = options.animate !== false;
      content.classList.toggle("no-anim", !animate);
      content.style.transform = `translate3d(${-target * (state.page.width + state.page.gap)}px,0,0)`;
      if (!animate) {
        requestAnimationFrame(() => content.classList.remove("no-anim"));
      }
    }
    updateFooter();
    savePosition();
  }

  function applyPageByRatio(ratio) {
    const total = Math.max(1, state.page.total);
    const value = clamp(Number(ratio) || 0, 0, 1);
    applyPage(total <= 1 ? 0 : Math.round(value * (total - 1)), { animate: false });
  }

  function setScrollRatio(ratio) {
    const surface = $("#reading-surface");
    if (!surface) return;
    const max = Math.max(0, surface.scrollHeight - surface.clientHeight);
    if (!max) {
      surface.scrollTop = 0;
      return;
    }
    surface.scrollTop = clamp(Number(ratio) || 0, 0, 1) * max;
  }

  function getScrollRatio() {
    const surface = $("#reading-surface");
    if (!surface) return 0;
    const max = Math.max(0, surface.scrollHeight - surface.clientHeight);
    return max <= 0 ? 1 : clamp(surface.scrollTop / max, 0, 1);
  }

  function getReadingRatio() {
    if (!state.chapter) return 0;
    if (isPaginated()) {
      const total = Math.max(1, state.page.total);
      return total <= 1 ? 1 : clamp(state.page.index / (total - 1), 0, 1);
    }
    return getScrollRatio();
  }

  const savePosition = debounce(() => {
    if (!state.bookId || !state.chapter) return;
    prefSet(posKey(state.bookId, state.chapterIndex), getReadingRatio().toFixed(4));
  }, 380);

  function updateFooter() {
    const label = $("#footer-label");
    const fill = $("#footer-progress");
    const prev = $("#footer-prev");
    const next = $("#footer-next");
    if (!label || !fill || !prev || !next) return;
    if (!state.chapter || !state.book) {
      label.textContent = "正在载入…";
      fill.style.width = "0%";
      prev.disabled = true;
      next.disabled = true;
      return;
    }
    const chapter = state.chapter;
    const totalChapters = Number(state.book.chapter_count) || 1;
    if (isPaginated()) {
      const totalPages = Math.max(1, state.page.total);
      label.textContent = `第 ${state.page.index + 1} / ${totalPages} 页 · 第 ${chapter.chapter_index + 1} 章`;
      fill.style.width = `${((state.page.index + 1) / totalPages) * 100}%`;
      prev.disabled = state.page.index <= 0 && chapter.prev_chapter === null;
      next.disabled = state.page.index >= totalPages - 1 && chapter.next_chapter === null;
      prev.setAttribute("aria-label", state.page.index > 0 ? "上一页" : "上一章");
      next.setAttribute("aria-label", state.page.index < totalPages - 1 ? "下一页" : "下一章");
    } else {
      const ratio = getScrollRatio();
      label.textContent = `第 ${chapter.chapter_index + 1} / ${totalChapters} 章 · 已读 ${Math.round(
        ratio * 100
      )}%`;
      fill.style.width = `${((chapter.chapter_index + ratio) / totalChapters) * 100}%`;
      prev.disabled = chapter.prev_chapter === null;
      next.disabled = chapter.next_chapter === null;
      prev.setAttribute("aria-label", "上一章");
      next.setAttribute("aria-label", "下一章");
    }
  }

  function turnPage(direction) {
    if (!state.chapter || !state.book) return;
    if (!isPaginated()) {
      if (direction < 0) {
        if (state.chapter.prev_chapter !== null) loadChapter(state.chapter.prev_chapter, { ratioOverride: 1 });
      } else if (state.chapter.next_chapter !== null) {
        loadChapter(state.chapter.next_chapter, { ratioOverride: 0 });
      }
      return;
    }
    if (direction < 0) {
      if (state.page.index > 0) {
        applyPage(state.page.index - 1);
      } else if (state.chapter.prev_chapter !== null) {
        loadChapter(state.chapter.prev_chapter, { ratioOverride: 1 });
      }
    } else if (state.page.index < state.page.total - 1) {
      applyPage(state.page.index + 1);
    } else if (state.chapter.next_chapter !== null) {
      loadChapter(state.chapter.next_chapter, { ratioOverride: 0 });
    }
  }

  /* ====================================================================== */
  /* TOC / background / search                                              */
  /* ====================================================================== */
  function renderToc() {
    const book = state.book;
    const list = $("#toc-list");
    if (!book || !list) return;
    const chapters = book.chapters || [];
    const scrollTop = list.scrollTop;
    $("#toc-count").textContent = `${chapters.length} 章`;
    list.innerHTML = chapters
      .map((chapter) => {
        const active = chapter.chapter_index === state.chapterIndex;
        const job = chapter.guide_job;
        const dotClass = chapter.is_guide_ready
          ? "ready"
          : job && job.status === "running"
            ? "running"
            : "";
        return `
          <button class="toc-item ${active ? "active" : ""}" type="button" data-chapter="${chapter.chapter_index}" aria-current="${active ? "page" : "false"}">
            <span class="toc-num">${String(chapter.chapter_index + 1).padStart(2, "0")}</span>
            <span class="toc-text">
              <span class="toc-title">${escapeHtml(chapter.title)}</span>
              <span class="toc-sub"><span class="toc-dot ${dotClass}"></span>${formatNumber(
                chapter.word_count
              )} 词${chapter.is_guide_ready ? " · 已导读" : job && job.status === "running" ? " · 生成中" : ""}</span>
            </span>
          </button>`;
      })
      .join("");
    list.scrollTop = scrollTop;
  }

  function scrollActiveTocIntoView() {
    const active = $("#toc-list .toc-item.active");
    if (active && active.scrollIntoView) {
      active.scrollIntoView({ block: "nearest" });
    }
  }

  function renderBackground() {
    const book = state.book;
    if (!book) return;
    const job = book.background_job || null;
    const running = job && job.status === "running";
    const error = job && job.status === "error" ? job.error : null;
    const hasContent = Boolean(book.book_summary || book.author_bio || book.cultural_context);
    const regenerate = $("#background-regenerate");
    if (regenerate) regenerate.hidden = !hasContent || running;
    const container = $("#background-content");
    if (!container) return;
    if (hasContent) {
      container.innerHTML = `
        ${book.book_summary ? `<section class="bg-section"><h4>${icon("book")}全书概要</h4><div class="markdown">${renderMarkdown(book.book_summary)}</div></section>` : ""}
        ${book.author_bio ? `<section class="bg-section"><h4>${icon("edit")}作者生平</h4><div class="markdown">${renderMarkdown(book.author_bio)}</div></section>` : ""}
        ${book.cultural_context ? `<section class="bg-section"><h4>${icon("info")}历史文化背景</h4><div class="markdown">${renderMarkdown(book.cultural_context)}</div></section>` : ""}
        ${running ? `<p class="bg-source"><span class="spinner"></span> AI 正在更新背景…</p>` : ""}
        ${error ? `<p class="bg-error">${escapeHtml(error)}<br><button class="btn small" type="button" data-bg-generate>重试</button></p>` : ""}`;
      return;
    }
    if (running) {
      container.innerHTML = `
        <div class="bg-loading">
          <div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>
          <p class="guide-status"><span class="spinner"></span>AI 正在检索作者、时代与作品背景…</p>
        </div>`;
      return;
    }
    if (error) {
      container.innerHTML = `
        <div class="bg-error">${escapeHtml(error)}
          <br><button class="btn small" type="button" data-bg-generate>重新生成</button>
        </div>`;
      return;
    }
    container.innerHTML = `
      <div class="bg-empty">
        还没有生成全书背景。<br>AI 会结合书名、作者与正文开头，整理全书概要、作者生平与历史文化背景。
        <br><button class="btn small" type="button" data-bg-generate>${icon("spark")}生成全书背景</button>
      </div>`;
  }

  async function generateBackground() {
    if (!state.book || state.bgBusy) return;
    state.bgBusy = true;
    if (state.book) state.book.background_job = { status: "running" };
    renderBackground();
    try {
      const result = await api(`/books/${state.book.id}/background`, { method: "POST" });
      state.book = { ...state.book, ...result.book, background_job: result.job || { status: "done" } };
      renderBackground();
      renderToc();
      toast("全书背景已生成", "success");
    } catch (error) {
      state.book.background_job = { status: "error", error: error.message };
      renderBackground();
      toast(error.message, "error");
    } finally {
      state.bgBusy = false;
    }
  }

  async function runSearch(query) {
    const value = query.trim();
    const results = $("#search-results");
    if (!state.book || !results) return;
    if (value.length < 2) {
      results.innerHTML = `<p class="panel-hint">输入至少 2 个字符，可搜索全书正文并跳转。</p>`;
      return;
    }
    const token = ++state.searchToken;
    results.innerHTML = `<p class="panel-hint"><span class="spinner"></span> 正在搜索全书…</p>`;
    try {
      const data = await api(`/books/${state.book.id}/search?q=${encodeURIComponent(value)}&limit=60`);
      if (token !== state.searchToken) return;
      if (!data.results || !data.results.length) {
        results.innerHTML = `<p class="panel-hint">没有找到“${escapeHtml(value)}”。试试更短的词或词根。</p>`;
        return;
      }
      results.innerHTML = data.results
        .map(
          (item) => `
          <button class="search-result" type="button" data-search-chapter="${item.chapter_index}" data-search-paragraph="${item.paragraph_index}">
            <span class="search-result-title"><span>${escapeHtml(item.chapter_title)}</span><span>第 ${item.chapter_index + 1} 章</span></span>
            <span class="search-snippet">${highlightSnippet(item.snippet || "", value)}</span>
          </button>`
        )
        .join("");
    } catch (error) {
      if (token !== state.searchToken) return;
      results.innerHTML = `<p class="panel-hint">搜索失败：${escapeHtml(error.message)}</p>`;
    }
  }

  function highlightSnippet(snippet, query) {
    const text = String(snippet || "");
    const needle = query.trim();
    if (!needle) return escapeHtml(text);
    const lower = text.toLocaleLowerCase("de");
    const target = needle.toLocaleLowerCase("de");
    let index = 0;
    let html = "";
    let found = false;
    while (index < text.length) {
      const hit = lower.indexOf(target, index);
      if (hit < 0) break;
      found = true;
      html += escapeHtml(text.slice(index, hit));
      html += `<mark>${escapeHtml(text.slice(hit, hit + needle.length))}</mark>`;
      index = hit + needle.length;
    }
    html += escapeHtml(text.slice(index));
    return found ? html : escapeHtml(text);
  }

  async function jumpToParagraph(chapterIndex, paragraphIndex) {
    closeMobileSidebars();
    if (Number(chapterIndex) !== state.chapterIndex) {
      await loadChapter(Number(chapterIndex), { ratioOverride: 0 });
    }
    requestAnimationFrame(() => {
      setTimeout(() => scrollToParagraph(Number(paragraphIndex)), 80);
    });
  }

  function scrollToParagraph(index) {
    const paragraph = document.getElementById(`p-${index}`);
    if (!paragraph) return;
    if (isPaginated()) {
      measurePages();
      const page = Math.floor(
        (paragraph.offsetLeft || 0) / Math.max(1, state.page.width + state.page.gap)
      );
      applyPage(page);
    } else {
      paragraph.scrollIntoView({ behavior: "smooth", block: "center" });
    }
    paragraph.classList.remove("flash");
    void paragraph.offsetWidth;
    paragraph.classList.add("flash");
    setTimeout(() => paragraph.classList.remove("flash"), 1900);
  }

  /* ====================================================================== */
  /* AI panel                                                               */
  /* ====================================================================== */
  function hasActiveKey() {
    if (!state.settings) return false;
    return state.settings.active_model === "gemini"
      ? Boolean(state.settings.has_gemini_key)
      : Boolean(state.settings.has_deepseek_key);
  }

  function updateAiBadge() {
    const warning = $("#ai-key-warning");
    if (warning) warning.hidden = !state.settings || hasActiveKey() || state.view !== "reader";
  }

  function resetAiPanel() {
    if (state.annotationStream) {
      state.annotationStream.abort();
      state.annotationStream = null;
    }
    if (state.chatStream) {
      state.chatStream.abort();
      state.chatStream = null;
    }
    state.activeAnnotationId = null;
    state.pendingSelection = null;
    const empty = $("#ai-empty");
    const content = $("#ai-content");
    if (empty) empty.hidden = false;
    if (content) content.hidden = true;
    const input = $("#chat-input");
    if (input) {
      input.value = "";
      input.style.height = "auto";
    }
    const messages = $("#chat-messages");
    if (messages) {
      messages.innerHTML = `<p class="panel-hint" id="chat-empty">还没有追问。可以问「为什么这里用 hätte？」或「这个介词能否换成 für？」</p>`;
    }
    const explanation = $("#explanation");
    if (explanation) explanation.innerHTML = "";
    updateAiBadge();
  }

  function openAiSidebar() {
    if (window.innerWidth > 1180) return;
    document.body.classList.add("right-open");
    document.body.classList.remove("left-open");
    updateSidebarBackdrop();
  }

  function setExplanationCollapsed(collapsed) {
    const block = $(".explanation-block");
    const button = $("#toggle-explanation");
    const content = $("#ai-content");
    if (!block) return;
    block.classList.toggle("collapsed", Boolean(collapsed));
    if (content) content.classList.toggle("explanation-collapsed", Boolean(collapsed));
    if (button) button.textContent = collapsed ? "展开解析" : "收起解析";
    if (!collapsed) block.scrollTop = 0;
    requestAnimationFrame(updateExplanationOverflowHint);
  }

  function updateExplanationOverflowHint() {
    const block = $(".explanation-block");
    const hint = $("#explanation-overflow-hint");
    if (!block || !hint) return;
    const collapsed = block.classList.contains("collapsed");
    hint.hidden = collapsed || block.scrollHeight <= block.clientHeight + 8;
  }

  function showPanelLoading(selectedText, context) {
    state.activeAnnotationId = null;
    $("#ai-empty").hidden = true;
    $("#ai-content").hidden = false;
    $("#chat-section").hidden = true;
    setExplanationCollapsed(false);
    $("#selection-word").textContent = selectedText;
    $("#annotation-cache-badge").hidden = true;
    $("#explanation").innerHTML = "";
    $("#explanation-loading").hidden = false;
    $("#explanation-error").hidden = true;
    $("#explanation").hidden = true;
    $("#explanation-overflow-hint").hidden = true;
    $$("#selection-actions button").forEach((button) => {
      button.disabled = true;
    });
    openAiSidebar();
    applyActiveMark();
  }

  function showPanelError(message) {
    $("#ai-empty").hidden = true;
    $("#ai-content").hidden = false;
    $("#chat-section").hidden = true;
    setExplanationCollapsed(false);
    $("#explanation-loading").hidden = true;
    $("#explanation").hidden = true;
    $("#explanation-error").hidden = false;
    $("#explanation-error-text").textContent = message;
    $("#explanation-overflow-hint").hidden = true;
    $("#retry-annotation").hidden = !state.pendingSelection;
    $$("#selection-actions button").forEach((button) => {
      button.disabled = true;
    });
    openAiSidebar();
  }

  function openAnnotation(annotationId, options = {}) {
    const id = Number(annotationId);
    if (state.annotationStream) {
      state.annotationStream.abort();
      state.annotationStream = null;
    }
    if (state.chatStream) {
      state.chatStream.abort();
      state.chatStream = null;
    }
    const annotation = state.annotations.find((item) => Number(item.id) === id);
    if (!annotation) {
      toast("标注不存在或已被删除", "error");
      return;
    }
    state.activeAnnotationId = id;
    state.pendingSelection = null;
    $("#ai-empty").hidden = true;
    $("#ai-content").hidden = false;
    $("#chat-section").hidden = false;
    setExplanationCollapsed(false);
    $("#explanation-loading").hidden = true;
    $("#explanation-error").hidden = true;
    $("#explanation").hidden = false;
    $("#explanation").innerHTML = renderMarkdown(annotation.ai_explanation || "");
    $("#selection-word").textContent = annotation.selected_text || "";
    $("#annotation-cache-badge").hidden = false;
    $("#annotation-cache-badge").textContent = "已缓存";
    $$("#selection-actions button").forEach((button) => {
      button.disabled = false;
    });
    renderChatMessages(annotation.messages || []);
    applyActiveMark();
    const block = $(".explanation-block");
    if (block) block.scrollTop = 0;
    requestAnimationFrame(updateExplanationOverflowHint);
    openAiSidebar();
    if (!options.keepFocus) {
      const word = $("#selection-word");
      if (window.innerWidth > 1180 && word) word.scrollIntoView({ block: "nearest" });
    }
  }

  async function requestAnnotation(selection, options = {}) {
    if (!state.chapter) return;
    const chapterId = state.chapter.id;
    const key = `${chapterId}:${selection.paragraphIndex}:${selection.start}:${selection.end}`;
    if (state.inFlight.has(key)) return;
    if (state.annotationStream) state.annotationStream.abort();
    if (state.chatStream) state.chatStream.abort();
    state.inFlight.add(key);
    state.pendingSelection = selection;
    hideSelectionToolbar();
    showPanelLoading(selection.text, selection.context);
    let accumulated = "";
    let renderScheduled = false;
    let firstDelta = true;
    let donePayload = null;
    let handle = null;
    const paint = () => {
      if (renderScheduled) return;
      renderScheduled = true;
      requestAnimationFrame(() => {
        renderScheduled = false;
        const explanation = $("#explanation");
        if (!explanation) return;
        explanation.innerHTML = renderMarkdown(accumulated);
        const block = $(".explanation-block");
        if (block) block.scrollTop = block.scrollHeight;
        updateExplanationOverflowHint();
      });
    };
    try {
      handle = streamSSE(
        "/annotate/stream",
        {
          chapter_id: chapterId,
          paragraph_index: selection.paragraphIndex,
          start_offset: selection.start,
          end_offset: selection.end,
          selected_text: selection.text,
          context_sentence: selection.context,
          force_regenerate: Boolean(options.force),
        },
        {
          meta: (data) => {
            if (data && data.cached) {
              $("#annotation-cache-badge").hidden = false;
              $("#annotation-cache-badge").textContent = "缓存复现";
            }
          },
          delta: (data) => {
            const text = data && data.text ? String(data.text) : "";
            if (!text) return;
            if (firstDelta) {
              firstDelta = false;
              $("#explanation-loading").hidden = true;
              $("#explanation").hidden = false;
              $("#annotation-cache-badge").hidden = false;
              $("#annotation-cache-badge").textContent = "生成中";
            }
            accumulated += text;
            paint();
          },
          done: (data) => {
            donePayload = data;
          },
          error: (data) => {
            const error = new Error((data && data.detail) || "AI 生成失败，请重试。");
            error.code = data && data.code;
            throw error;
          },
        }
      );
      state.annotationStream = handle;
      await handle.promise;
      if (!donePayload || !donePayload.annotation) {
        throw new Error("AI 流式解析未完成，请重试。");
      }
      if (!state.chapter || state.chapter.id !== chapterId) return;
      const annotation = donePayload.annotation;
      state.annotations = state.annotations
        .filter((item) => Number(item.id) !== Number(annotation.id))
        .concat(annotation)
        .sort((a, b) =>
          Number(a.paragraph_index) - Number(b.paragraph_index) || Number(a.start_offset) - Number(b.start_offset)
        );
      state.pendingSelection = null;
      renderChapterView({ ratio: getReadingRatio() });
      openAnnotation(annotation.id);
      $("#annotation-cache-badge").textContent = donePayload.cached ? "缓存复现" : "已缓存";
      if (donePayload.regenerated || options.force) toast("解析已重新生成", "success", 2200);
    } catch (error) {
      if (error.name === "AbortError") return;
      showPanelError(error.message);
      toast(error.message, "error");
      if (error.code === "missing_key" || error.code === "unauthorized") {
        updateAiBadge();
      }
    } finally {
      state.inFlight.delete(key);
      if (state.annotationStream === handle) state.annotationStream = null;
    }
  }

  function regenerateExplanation() {
    const annotation = state.annotations.find(
      (item) => Number(item.id) === Number(state.activeAnnotationId)
    );
    if (!annotation) return;
    requestAnnotation(
      {
        paragraphIndex: annotation.paragraph_index,
        start: annotation.start_offset,
        end: annotation.end_offset,
        text: annotation.selected_text,
        context: annotation.context_sentence,
      },
      { force: true }
    );
  }

  async function deleteAnnotation() {
    const annotation = state.annotations.find(
      (item) => Number(item.id) === Number(state.activeAnnotationId)
    );
    if (!annotation) return;
    const confirmed = await confirmAction({
      title: "删除这条划词笔记",
      message: `删除“${annotation.selected_text}”的高亮、解析与全部追问记录？此操作无法撤销。`,
      confirmText: "删除",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api(`/annotations/${annotation.id}`, { method: "DELETE" });
      state.annotations = state.annotations.filter((item) => Number(item.id) !== Number(annotation.id));
      state.activeAnnotationId = null;
      renderChapterView({ ratio: getReadingRatio() });
      resetAiPanel();
      toast("划词笔记已删除", "success");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function renderChatMessages(messages) {
    const container = $("#chat-messages");
    if (!container) return;
    const content = $("#ai-content");
    if (content) content.classList.toggle("chat-empty-state", !messages || !messages.length);
    if (!messages || !messages.length) {
      container.innerHTML = `<p class="panel-hint" id="chat-empty">还没有追问。可以问「为什么这里用 hätte？」或「这个介词能否换成 für？」</p>`;
      const clear = $("#clear-chat");
      if (clear) clear.hidden = true;
      return;
    }
    const clear = $("#clear-chat");
    if (clear) clear.hidden = false;
    container.innerHTML = messages
      .map((message) => {
        const isUser = message.role === "user";
        const body = isUser
          ? `<span class="chat-text">${escapeHtml(message.content)}</span>`
          : `<div class="markdown">${renderMarkdown(message.content)}</div>`;
        return `<div class="chat-msg ${isUser ? "user" : "assistant"}"><span class="chat-avatar">${isUser ? "你" : "AI"}</span><div class="chat-bubble ${isUser ? "user" : "assistant"}">${body}<span class="chat-time">${formatClock(message.created_at)}</span></div></div>`;
      })
      .join("");
    requestAnimationFrame(() => {
      container.scrollTop = container.scrollHeight;
      updateExplanationOverflowHint();
    });
  }

  async function sendChat() {
    const annotation = state.annotations.find(
      (item) => Number(item.id) === Number(state.activeAnnotationId)
    );
    if (!annotation || state.chatSending) return;
    const input = $("#chat-input");
    const message = input.value.trim();
    if (!message) return;
    if (state.chatStream) state.chatStream.abort();
    state.chatSending = true;
    $("#chat-send").disabled = true;
    setExplanationCollapsed(true);
    const container = $("#chat-messages");
    const empty = $("#chat-empty");
    if (empty) empty.remove();
    const userNode = document.createElement("div");
    userNode.className = "chat-msg user";
    userNode.innerHTML = `<span class="chat-avatar">你</span><div class="chat-bubble user"><span class="chat-text">${escapeHtml(message)}</span></div>`;
    const typing = document.createElement("div");
    typing.className = "chat-msg assistant typing";
    typing.innerHTML = `<span class="chat-avatar">AI</span><div class="chat-bubble assistant"><span class="typing-dots"><i></i><i></i><i></i></span></div>`;
    container.appendChild(userNode);
    container.appendChild(typing);
    container.scrollTop = container.scrollHeight;
    input.value = "";
    input.style.height = "auto";
    const clear = $("#clear-chat");
    if (clear) clear.hidden = false;

    let streamed = "";
    let renderScheduled = false;
    let markdownElement = null;
    let donePayload = null;
    let handle = null;
    const bubble = typing.querySelector(".chat-bubble");
    const paint = () => {
      if (renderScheduled || !markdownElement) return;
      renderScheduled = true;
      requestAnimationFrame(() => {
        renderScheduled = false;
        if (!markdownElement) return;
        markdownElement.innerHTML = renderMarkdown(streamed);
        container.scrollTop = container.scrollHeight;
      });
    };
    try {
      handle = streamSSE(
        `/annotations/${annotation.id}/chat/stream`,
        { message },
        {
          delta: (data) => {
            const text = data && data.text ? String(data.text) : "";
            if (!text) return;
            if (!markdownElement) {
              typing.classList.remove("typing");
              bubble.innerHTML = '<div class="markdown stream-markdown"></div><span class="stream-cursor">▍</span>';
              markdownElement = bubble.querySelector(".stream-markdown");
            }
            streamed += text;
            paint();
          },
          done: (data) => {
            donePayload = data;
          },
          error: (data) => {
            const error = new Error((data && data.detail) || "AI 回答失败，请重试。");
            error.code = data && data.code;
            throw error;
          },
        }
      );
      state.chatStream = handle;
      await handle.promise;
      if (!donePayload) throw new Error("AI 流式回答未完成，请重试。");
      if (Number(state.activeAnnotationId) !== Number(annotation.id)) return;
      annotation.messages = donePayload.messages || [];
      renderChatMessages(annotation.messages);
    } catch (error) {
      if (error.name === "AbortError") return;
      input.value = message;
      autoGrowTextarea(input);
      renderChatMessages(annotation.messages || []);
      toast(error.message, "error");
    } finally {
      state.chatSending = false;
      $("#chat-send").disabled = false;
      if (state.chatStream === handle) state.chatStream = null;
      // 发送后释放输入框焦点，避免在阅读 AI 讲解时按空格被输入法/文本框截获。
      if (state.view === "reader" && Number(state.activeAnnotationId) === Number(annotation.id)) {
        input.blur();
      }
    }
  }

  async function clearChat() {
    const annotation = state.annotations.find(
      (item) => Number(item.id) === Number(state.activeAnnotationId)
    );
    if (!annotation) return;
    const confirmed = await confirmAction({
      title: "清空追问",
      message: "清空这条划词下的所有追问记录？已有的解析会保留。",
      confirmText: "清空",
      danger: true,
    });
    if (!confirmed) return;
    try {
      await api(`/annotations/${annotation.id}/chat`, { method: "DELETE" });
      annotation.messages = [];
      renderChatMessages([]);
      toast("追问记录已清空", "success");
    } catch (error) {
      toast(error.message, "error");
    }
  }

  function autoGrowTextarea(textarea) {
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(96, textarea.scrollHeight)}px`;
  }

  /* ====================================================================== */
  /* Selection & toolbar                                                    */
  /* ====================================================================== */
  function paragraphFromNode(node) {
    if (!node) return null;
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element ? element.closest("p.para[data-p-idx]") : null;
  }

  function handleSelection() {
    if (state.view !== "reader" || !state.chapter) return;
    const activeTag = document.activeElement ? document.activeElement.tagName : "";
    if (["INPUT", "TEXTAREA", "SELECT"].includes(activeTag)) return;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0 || selection.isCollapsed) {
      hideSelectionToolbar();
      return;
    }
    const range = selection.getRangeAt(0);
    const startParagraph = paragraphFromNode(range.startContainer);
    const endParagraph = paragraphFromNode(range.endContainer);
    if (!startParagraph || !endParagraph) {
      hideSelectionToolbar();
      return;
    }
    if (startParagraph !== endParagraph) {
      hideSelectionToolbar();
      const now = Date.now();
      if (now - state.lastMultiParaWarn > 1800) {
        state.lastMultiParaWarn = now;
        toast("目前只支持在同一个自然段内划词。", "info", 2600);
      }
      return;
    }
    const raw = range.toString();
    if (!raw.trim()) {
      hideSelectionToolbar();
      return;
    }
    const paragraphText = startParagraph.textContent || "";
    const before = range.cloneRange();
    before.selectNodeContents(startParagraph);
    before.setEnd(range.startContainer, range.startOffset);
    let start = before.toString().length;
    const leading = raw.length - raw.trimStart().length;
    start += leading;
    const selectedText = raw.trim();
    if (!selectedText) {
      hideSelectionToolbar();
      return;
    }
    if (selectedText.length > MAX_SELECTION) {
      hideSelectionToolbar();
      toast(`选中内容过长（上限 ${MAX_SELECTION} 字），请缩短范围。`, "error");
      return;
    }
    if (!/[A-Za-zÄÖÜäöüß]/.test(selectedText)) {
      hideSelectionToolbar();
      return;
    }
    let end = start + selectedText.length;
    if (paragraphText.slice(start, end) !== selectedText) {
      const found = paragraphText.indexOf(selectedText);
      if (found >= 0) {
        start = found;
        end = found + selectedText.length;
      } else {
        hideSelectionToolbar();
        return;
      }
    }
    const paragraphIndex = Number(startParagraph.dataset.pIdx);
    const overlap = state.annotations.find(
      (annotation) =>
        Number(annotation.paragraph_index) === paragraphIndex &&
        start < Number(annotation.end_offset) &&
        end > Number(annotation.start_offset)
    );
    if (overlap) {
      hideSelectionToolbar();
      openAnnotation(overlap.id);
      selection.removeAllRanges();
      return;
    }
    const rect = range.getBoundingClientRect();
    state.selection = {
      paragraphIndex,
      start,
      end,
      text: selectedText,
      context: paragraphText.slice(0, 4000),
      rect,
    };
    showSelectionToolbar(rect);
    clearTimeout(state.autoTimer);
    if (state.settings && state.settings.auto_annotate) {
      const captured = state.selection;
      state.autoTimer = setTimeout(() => {
        if (state.selection === captured && state.toolbarVisible) {
          requestAnnotation(captured);
        }
      }, 620);
    }
  }

  function showSelectionToolbar(rect) {
    const toolbar = $("#selection-toolbar");
    if (!toolbar) return;
    toolbar.hidden = false;
    state.toolbarVisible = true;
    const width = toolbar.offsetWidth || 240;
    const height = toolbar.offsetHeight || 40;
    let left = rect.left + rect.width / 2 - width / 2;
    left = clamp(left, 8, window.innerWidth - width - 8);
    let top = rect.top - height - 10;
    if (top < 72) top = Math.min(window.innerHeight - height - 10, rect.bottom + 10);
    toolbar.style.left = `${Math.round(left)}px`;
    toolbar.style.top = `${Math.round(top)}px`;
  }

  function hideSelectionToolbar() {
    clearTimeout(state.autoTimer);
    state.autoTimer = null;
    const toolbar = $("#selection-toolbar");
    if (toolbar) toolbar.hidden = true;
    state.toolbarVisible = false;
  }

  function handleSelectionAction(action) {
    const selection = state.selection;
    if (!selection) return;
    if (action === "explain") {
      requestAnnotation(selection);
    } else if (action === "speak") {
      speakGerman(selection.text);
    } else if (action === "copy") {
      copyText(selection.text).then((ok) => toast(ok ? "已复制选中内容" : "复制失败", ok ? "success" : "error"));
      hideSelectionToolbar();
    } else if (action === "dismiss") {
      hideSelectionToolbar();
      const current = window.getSelection();
      if (current) current.removeAllRanges();
    }
  }

  /* ====================================================================== */
  /* Speech                                                                 */
  /* ====================================================================== */
  function speakGerman(text) {
    if (!("speechSynthesis" in window)) {
      toast("当前浏览器不支持语音朗读。", "error");
      return;
    }
    const content = String(text || "").trim();
    if (!content) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(content.slice(0, 600));
    utterance.lang = "de-DE";
    utterance.rate = 0.94;
    utterance.pitch = 1;
    const voices = window.speechSynthesis.getVoices();
    const german = voices.find((voice) => /^de(-|_)/i.test(voice.lang));
    if (german) utterance.voice = german;
    window.speechSynthesis.speak(utterance);
  }

  function getSpeechRecognition() {
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) return null;
    const recognition = new Recognition();
    recognition.lang = state.voiceLang;
    recognition.interimResults = true;
    recognition.continuous = false;
    recognition.maxAlternatives = 1;
    return recognition;
  }

  function updateVoiceLangButton() {
    const button = $("#voice-lang");
    if (button) {
      button.textContent = state.voiceLang === "de-DE" ? "德" : "中";
      button.title = `语音识别语言：${state.voiceLang === "de-DE" ? "德语" : "中文"}（点击切换）`;
    }
    if (state.recognition) state.recognition.lang = state.voiceLang;
  }

  function speechSupported() {
    return Boolean(window.SpeechRecognition || window.webkitSpeechRecognition);
  }

  function stopRecognition() {
    if (!state.recognition) return;
    try {
      state.recognition.stop();
    } catch {
      /* ignore */
    }
  }

  function startRecognition(options = {}) {
    const hold = Boolean(options.hold);
    if (!speechSupported()) {
      toast("当前浏览器不支持语音识别，请使用 Chrome / Edge。", "error");
      return false;
    }
    if (state.recording) return false;
    const input = $("#chat-input");
    const chatSection = $("#chat-section");
    if (!input || !state.activeAnnotationId || !chatSection || chatSection.hidden) {
      if (!hold) toast("请先在正文中划选词句，再开始语音追问。", "info");
      return false;
    }
    const recognition = getSpeechRecognition();
    if (!recognition) return false;
    state.recognition = recognition;
    state.recognitionHold = hold;
    const base = input.value;
    const button = $("#mic-button");
    recognition.onstart = () => {
      state.recording = true;
      button.classList.add("recording");
      if (hold) {
        toast(`正在听 ${state.voiceLang === "de-DE" ? "德语" : "中文"}…松开空格结束`, "info", 1500);
      } else {
        toast(`正在听 ${state.voiceLang === "de-DE" ? "德语" : "中文"}…`, "info", 1600);
      }
    };
    recognition.onresult = (event) => {
      let transcript = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        transcript += event.results[i][0].transcript;
      }
      input.value = `${base}${transcript}`;
      autoGrowTextarea(input);
      if (!hold) input.focus();
    };
    recognition.onerror = (event) => {
      state.recording = false;
      if (hold) state.spaceHeld = false;
      button.classList.remove("recording");
      if (event.error === "aborted") return;
      const messages = {
        "not-allowed": "没有麦克风权限。局域网 HTTP 访问时，浏览器可能要求 HTTPS。",
        "service-not-allowed": "浏览器禁止了语音识别服务。",
        "no-speech": "没有听到声音，请再试一次。",
        network: "语音识别网络错误。",
      };
      toast(messages[event.error] || `语音识别失败：${event.error}`, "error", 5200);
    };
    recognition.onend = () => {
      state.recording = false;
      state.recognition = null;
      state.recognitionHold = false;
      button.classList.remove("recording");
      if (!state.spaceHeld) input.focus({ preventScroll: true });
    };
    try {
      recognition.start();
      return true;
    } catch {
      state.recognition = null;
      state.recording = false;
      toast("无法启动语音识别，请稍后再试。", "error");
      return false;
    }
  }

  function toggleRecording() {
    if (state.recording) {
      stopRecognition();
      return;
    }
    startRecognition({ hold: false });
  }

  function handleSpaceDown(event, options = {}) {
    if (event.code !== "Space" && event.key !== " ") return false;
    if (event.repeat || state.spaceHeld || state.recording) return false;
    const active = document.activeElement;
    const tag = active ? active.tagName : "";
    const allowChatInput = Boolean(options.allowChatInput && active && active.id === "chat-input");
    if (
      ["INPUT", "TEXTAREA", "SELECT", "BUTTON", "A"].includes(tag) &&
      !allowChatInput
    ) {
      return false;
    }
    if (active && active.isContentEditable) return false;
    if (!state.activeAnnotationId) return false;
    const chatSection = $("#chat-section");
    const aiContent = $("#ai-content");
    if (!chatSection || chatSection.hidden || !aiContent || aiContent.hidden) return false;
    if (window.innerWidth <= 1180 && !document.body.classList.contains("right-open")) return false;
    if (!speechSupported()) return false;
    event.preventDefault();
    state.spaceHeld = true;
    if (!startRecognition({ hold: true })) {
      state.spaceHeld = false;
      return false;
    }
    return true;
  }

  function handleSpaceUp(event) {
    if (!state.spaceHeld) return false;
    event.preventDefault();
    state.spaceHeld = false;
    stopRecognition();
    setTimeout(() => {
      const input = $("#chat-input");
      if (input) input.focus({ preventScroll: true });
    }, 60);
    return true;
  }

  /* ====================================================================== */
  /* Markdown renderer                                                      */
  /* ====================================================================== */
  function renderMarkdown(source) {
    const raw = String(source || "").replace(/\r\n?/g, "\n");
    if (!raw.trim()) return "";
    const codeBlocks = [];
    let text = raw.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_match, language, code) => {
      const token = `\u0000CODEBLOCK${codeBlocks.length}\u0000`;
      codeBlocks.push(`<pre><code class="language-${escapeHtml(language)}">${escapeHtml(code.replace(/\n$/, ""))}</code></pre>`);
      return token;
    });
    const inline = (value) => {
      const codes = [];
      let result = escapeHtml(value);
      result = result.replace(/`([^`]+)`/g, (_match, code) => {
        const token = `\u0001INLINECODE${codes.length}\u0001`;
        codes.push(`<code>${code}</code>`);
        return token;
      });
      result = result.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      result = result.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      result = result.replace(/~~([^~]+)~~/g, "<del>$1</del>");
      result = result.replace(
        /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );
      result = result.replace(/\u0001INLINECODE(\d+)\u0001/g, (_match, index) => codes[Number(index)] || "");
      return result;
    };
    const lines = text.split("\n");
    const output = [];
    let paragraph = [];
    let listType = null;
    let listItems = [];
    let quote = [];
    const flushParagraph = () => {
      if (paragraph.length) {
        output.push(`<p>${inline(paragraph.join(" "))}</p>`);
        paragraph = [];
      }
    };
    const flushList = () => {
      if (listType && listItems.length) {
        output.push(`<${listType}>${listItems.map((item) => `<li>${inline(item)}</li>`).join("")}</${listType}>`);
      }
      listType = null;
      listItems = [];
    };
    const flushQuote = () => {
      if (quote.length) output.push(`<blockquote>${inline(quote.join(" "))}</blockquote>`);
      quote = [];
    };
    const flushAll = () => {
      flushParagraph();
      flushList();
      flushQuote();
    };
    for (const line of lines) {
      const trimmed = line.trim();
      const blockMatch = trimmed.match(/^\u0000CODEBLOCK(\d+)\u0000$/);
      if (blockMatch) {
        flushAll();
        output.push(codeBlocks[Number(blockMatch[1])] || "");
        continue;
      }
      if (!trimmed) {
        flushAll();
        continue;
      }
      const heading = trimmed.match(/^(#{1,4})\s+(.*)$/);
      if (heading) {
        flushAll();
        const level = heading[1].length;
        const tag = level <= 2 ? "h3" : "h4";
        output.push(`<${tag}>${inline(heading[2])}</${tag}>`);
        continue;
      }
      if (/^([-*_])\1{2,}$/.test(trimmed)) {
        flushAll();
        output.push("<hr>");
        continue;
      }
      const bullet = trimmed.match(/^[-*+]\s+(.*)$/);
      if (bullet) {
        flushParagraph();
        flushQuote();
        if (listType !== "ul") flushList();
        listType = "ul";
        listItems.push(bullet[1]);
        continue;
      }
      const numbered = trimmed.match(/^\d+[.)]\s+(.*)$/);
      if (numbered) {
        flushParagraph();
        flushQuote();
        if (listType !== "ol") flushList();
        listType = "ol";
        listItems.push(numbered[1]);
        continue;
      }
      const quoted = trimmed.match(/^>\s?(.*)$/);
      if (quoted) {
        flushParagraph();
        flushList();
        quote.push(quoted[1]);
        continue;
      }
      flushList();
      flushQuote();
      paragraph.push(trimmed);
    }
    flushAll();
    return output.join("\n").replace(/\u0000CODEBLOCK\d+\u0000/g, "");
  }

  /* ====================================================================== */
  /* Reader polling                                                         */
  /* ====================================================================== */
  function scheduleReaderPoll(delay = 2400) {
    clearTimeout(state.readerPoll);
    state.readerPoll = null;
    if (state.view !== "reader" || !state.chapter || !state.book) return;
    const guideRunning =
      !state.chapter.is_guide_ready &&
      state.chapter.guide_job &&
      state.chapter.guide_job.status === "running";
    const guidePendingAuto =
      !state.chapter.is_guide_ready &&
      !state.chapter.guide_job &&
      state.settings &&
      state.settings.auto_guide &&
      state.guidePollCount < 4;
    const bgPending =
      state.book &&
      !state.book.book_summary &&
      state.bgPollCount < 45 &&
      (!state.book.background_job || state.book.background_job.status !== "error");
    if (!guideRunning && !guidePendingAuto && !bgPending) return;
    state.readerPoll = setTimeout(pollReader, delay);
  }

  async function pollReader() {
    if (state.view !== "reader" || !state.chapter || !state.book) return;
    let pending = false;
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed) {
      scheduleReaderPoll(1600);
      return;
    }
    try {
      if (!state.chapter.is_guide_ready) {
        state.guidePollCount += 1;
        const data = await api(`/books/${state.bookId}/chapters/${state.chapterIndex}`);
        if (state.view !== "reader" || !state.chapter) return;
        state.chapter.guide_job = data.guide_job;
        state.chapter.is_guide_ready = Boolean(data.is_guide_ready);
        if (data.is_guide_ready) {
          state.chapter.chapter_summary = data.chapter_summary;
          state.chapter.questions = data.questions || [];
          state.chapter.guiding_questions = state.chapter.questions;
          syncChapterInBook();
          renderToc();
          renderChapterView({ ratio: getReadingRatio() });
        } else if (data.guide_job && data.guide_job.status === "error") {
          syncChapterInBook();
          renderToc();
          renderChapterView({ ratio: getReadingRatio() });
          state.guidePollCount = 99;
        } else {
          syncChapterInBook();
          pending = true;
        }
      }
      if (!state.book.book_summary && state.bgPollCount < 45) {
        state.bgPollCount += 1;
        const book = await api(`/books/${state.bookId}`);
        if (state.view !== "reader") return;
        const hadSummary = Boolean(state.book.book_summary);
        state.book = { ...state.book, ...book };
        if (!hadSummary && book.book_summary) {
          toast("全书背景已生成", "success", 2600);
        }
        renderBackground();
        if (!book.book_summary && book.background_job && book.background_job.status === "error") {
          state.bgPollCount = 99;
        } else if (!book.book_summary) {
          pending = true;
        }
      }
      if (pending) {
        try {
          const chapters = await api(`/books/${state.bookId}/chapters`);
          if (state.view === "reader" && state.book) {
            state.book.chapters = chapters.chapters || [];
            renderToc();
          }
        } catch {
          /* ignore */
        }
      }
    } catch {
      /* 静默重试 */
      pending = true;
    }
    if (state.view === "reader" && pending) scheduleReaderPoll(2600);
  }

  /* ====================================================================== */
  /* Sidebars                                                               */
  /* ====================================================================== */
  function updateSidebarBackdrop() {
    const backdrop = $("#sidebar-backdrop");
    if (!backdrop) return;
    const open = document.body.classList.contains("left-open") || document.body.classList.contains("right-open");
    if (open) {
      backdrop.hidden = false;
      requestAnimationFrame(() => backdrop.classList.add("visible"));
    } else {
      backdrop.classList.remove("visible");
      setTimeout(() => {
        if (!document.body.classList.contains("left-open") && !document.body.classList.contains("right-open")) {
          backdrop.hidden = true;
        }
      }, 200);
    }
  }

  function closeMobileSidebars() {
    document.body.classList.remove("left-open", "right-open");
    updateSidebarBackdrop();
  }

  function toggleLeftSidebar() {
    const open = !document.body.classList.contains("left-open");
    document.body.classList.toggle("left-open", open);
    document.body.classList.remove("right-open");
    updateSidebarBackdrop();
  }

  function toggleRightSidebar() {
    const open = !document.body.classList.contains("right-open");
    document.body.classList.toggle("right-open", open);
    document.body.classList.remove("left-open");
    updateSidebarBackdrop();
    if (open) updateAiBadge();
  }

  function switchSidebarTab(tab) {
    $$(".sidebar-tabs button").forEach((button) => {
      button.classList.toggle("active", button.dataset.sidebarTab === tab);
      button.setAttribute("aria-selected", String(button.dataset.sidebarTab === tab));
    });
    $$(".sidebar-panel").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.sidebarPanel === tab);
    });
    if (tab === "search") {
      setTimeout(() => $("#search-input").focus(), 50);
    }
    if (tab === "background") renderBackground();
  }

  /* ====================================================================== */
  /* Import                                                                 */
  /* ====================================================================== */
  function openImportModal(file = null) {
    if (!state.importing) {
      resetImportModal();
    }
    openModal("modal-import");
    if (file) setImportFile(file);
    setTimeout(() => $("#dropzone").focus(), 80);
  }

  function resetImportModal() {
    state.importFile = null;
    state.importing = false;
    state.importTimers.forEach((timer) => clearTimeout(timer));
    state.importTimers = [];
    $("#file-input").value = "";
    $("#file-chosen").hidden = true;
    $("#file-name").textContent = "";
    $("#file-size").textContent = "";
    $("#file-type").textContent = "FILE";
    $("#import-title-input").value = "";
    $("#import-author-input").value = "";
    $("#import-error").hidden = true;
    $("#import-error").textContent = "";
    $("#import-progress").hidden = true;
    $("#import-submit").disabled = true;
    $("#import-submit").textContent = "开始导入";
    $("#import-foot-hint").textContent = "文件只保存在本机 uploads/ 目录";
    $$("#import-progress .import-stages li").forEach((item) => item.classList.remove("active", "done"));
    $("#import-progress-title").textContent = "正在上传…";
    const dropzone = $("#dropzone");
    if (dropzone) dropzone.classList.remove("dragover");
  }

  function setImportFile(file) {
    if (!file) return;
    const suffix = (file.name.split(".").pop() || "").toLowerCase();
    if (!["pdf", "txt", "epub", "md"].includes(suffix)) {
      showImportError("只支持 PDF、TXT、EPUB 或 Markdown 文件。");
      return;
    }
    if (file.size > 200 * 1024 * 1024) {
      showImportError("文件超过 200 MB 限制。");
      return;
    }
    state.importFile = file;
    $("#import-error").hidden = true;
    $("#file-chosen").hidden = false;
    $("#file-name").textContent = file.name;
    $("#file-size").textContent = formatFileSize(file.size);
    $("#file-type").textContent = suffix.toUpperCase();
    $("#import-submit").disabled = false;
  }

  function formatFileSize(bytes) {
    if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
    if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${bytes} B`;
  }

  function showImportError(message) {
    $("#import-error").hidden = false;
    $("#import-error").textContent = message;
  }

  function setImportStage(index) {
    $$("#import-progress .import-stages li").forEach((item, position) => {
      item.classList.toggle("active", position === index);
      item.classList.toggle("done", position < index);
    });
    const titles = ["正在上传原始文件…", "正在解析德语结构与章节…", "正在建立段落与高亮索引…", "正在排队生成 AI 导读…"];
    $("#import-progress-title").textContent = titles[index] || "正在处理…";
  }

  async function submitImport() {
    if (state.importing) return;
    const file = state.importFile;
    if (!file) {
      showImportError("请先选择一本德语书。");
      return;
    }
    state.importing = true;
    $("#import-error").hidden = true;
    $("#import-progress").hidden = false;
    $("#import-submit").disabled = true;
    $("#import-submit").textContent = "导入中…";
    $("#import-foot-hint").textContent = "解析在本机完成，请不要关闭窗口";
    setImportStage(0);
    state.importTimers.push(setTimeout(() => setImportStage(1), 500));
    state.importTimers.push(setTimeout(() => setImportStage(2), 1800));
    state.importTimers.push(setTimeout(() => setImportStage(3), 3200));
    const form = new FormData();
    form.append("file", file, file.name);
    const title = $("#import-title-input").value.trim();
    const author = $("#import-author-input").value.trim();
    if (title) form.append("title", title);
    if (author) form.append("author", author);
    try {
      const result = await api("/books/import", { method: "POST", body: form });
      state.importTimers.forEach((timer) => clearTimeout(timer));
      $$("#import-progress .import-stages li").forEach((item) => item.classList.add("done"));
      $("#import-progress-title").textContent = "导入成功，正在打开…";
      const book = result.book;
      await loadLibrary({ quiet: true });
      setTimeout(() => {
        state.importing = false;
        closeModal("modal-import", { force: true });
        toast(`《${book.title}》已导入，AI 正在后台生成导读`, "success", 4200);
        openBook(book.id, 0, { force: true });
      }, 420);
    } catch (error) {
      state.importTimers.forEach((timer) => clearTimeout(timer));
      state.importing = false;
      $("#import-submit").disabled = false;
      $("#import-submit").textContent = "开始导入";
      $("#import-progress").hidden = true;
      $("#import-foot-hint").textContent = "文件只保存在本机 uploads/ 目录";
      showImportError(error.message);
      toast(error.message, "error");
    }
  }

  /* ====================================================================== */
  /* Settings                                                               */
  /* ====================================================================== */
  function openSettings() {
    if (!state.settings) {
      toast("设置尚未载入，请稍后重试", "error");
      return;
    }
    populateSettings();
    openModal("modal-settings");
    loadSystemInfoForSettings();
  }

  function populateSettings() {
    const settings = state.settings;
    if (!settings) return;
    state.clearKeys.clear();
    state.settingsDirty = false;
    $("#settings-status").textContent = "";
    $$('input[name="active-model"]').forEach((input) => {
      input.checked = input.value === settings.active_model;
    });
    updateProviderCards();

    const hasDeepseek = Boolean(settings.has_deepseek_key);
    const hasGemini = Boolean(settings.has_gemini_key);
    $("#deepseek-key").value = "";
    $("#gemini-key").value = "";
    $("#deepseek-key").type = "password";
    $("#gemini-key").type = "password";
    $$("[data-toggle-key]").forEach((button) => (button.textContent = "显示"));
    $("#deepseek-key").placeholder = hasDeepseek
      ? `已保存：${settings.deepseek_key_hint}（留空保持）`
      : "sk-…";
    $("#gemini-key").placeholder = hasGemini
      ? `已保存：${settings.gemini_key_hint}（留空保持）`
      : "AIza…";
    $("#deepseek-key-hint").textContent = hasDeepseek ? `已保存 ${settings.deepseek_key_hint}` : "未配置";
    $("#gemini-key-hint").textContent = hasGemini ? `已保存 ${settings.gemini_key_hint}` : "未配置";
    $("#settings-deepseek-dot").className = `status-dot ${hasDeepseek ? "ok" : "warn"}`;
    $("#settings-gemini-dot").className = `status-dot ${hasGemini ? "ok" : "warn"}`;
    $$('[data-clear-key="deepseek"]').forEach((button) => (button.hidden = !hasDeepseek));
    $$('[data-clear-key="gemini"]').forEach((button) => (button.hidden = !hasGemini));
    $("#deepseek-model").value = settings.deepseek_model || "deepseek-chat";
    $("#gemini-model").value = settings.gemini_model || "gemini-2.5-flash";
    $("#toggle-thinking").checked = settings.thinking_enabled !== false;
    const effort = settings.thinking_effort || "medium";
    $$("#thinking-effort [data-thinking-effort]").forEach((button) => {
      button.classList.toggle("active", button.dataset.thinkingEffort === effort);
    });
    updateThinkingControls();
    $("#selection-prompt").value = settings.selection_prompt || settings.default_selection_prompt || "";
    $("#settings-font-size").value = String(state.fontSize);
    $("#settings-font-label").textContent = `${state.fontSize} px`;
    $("#settings-line-width").value = state.lineWidth;
    $("#toggle-auto-annotate").checked = Boolean(settings.auto_annotate);
    $("#toggle-auto-guide").checked = Boolean(settings.auto_guide);
    $("#toggle-auto-metadata").checked = Boolean(settings.auto_metadata);
    $$("#settings-theme-options [data-theme-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.themeChoice === state.theme);
    });
    $$("#settings-mode [data-mode-choice]").forEach((button) => {
      button.classList.toggle("active", button.dataset.modeChoice === state.mode);
    });
    $("#test-result").className = "test-result";
    $("#test-result").textContent = "";
    $("#test-connection .btn").disabled = false;
    switchSettingsTab("ai");
  }

  function switchSettingsTab(tab) {
    $$("[data-settings-tab]").forEach((button) => {
      button.classList.toggle("active", button.dataset.settingsTab === tab);
    });
    $$("[data-settings-panel]").forEach((panel) => {
      panel.classList.toggle("active", panel.dataset.settingsPanel === tab);
    });
    if (tab === "about") loadSystemInfoForSettings();
  }

  function updateProviderCards() {
    $$(".provider-card").forEach((card) => {
      const input = card.querySelector('input[name="active-model"]');
      card.classList.toggle("selected", Boolean(input && input.checked));
    });
  }

  function updateThinkingControls() {
    const toggle = $("#toggle-thinking");
    const field = $("#thinking-effort-field");
    const hint = $("#thinking-hint");
    if (!toggle || !field || !hint) return;
    const enabled = toggle.checked;
    field.classList.toggle("disabled", !enabled);
    $$("#thinking-effort [data-thinking-effort]").forEach((button) => {
      button.disabled = !enabled;
    });
    hint.textContent = enabled
      ? "中等：日常划词与导读的平衡点；思考越深越慢，但分析更充分。"
      : "已关闭思考：模型直接生成最终回答，响应更快、Token 更少。";
  }

  function markSettingsDirty() {
    state.settingsDirty = true;
    $("#settings-status").textContent = "有未保存的更改";
  }

  function buildSettingsPayload() {
    const selectedProvider = document.querySelector('input[name="active-model"]:checked');
    const provider = selectedProvider ? selectedProvider.value : state.settings.active_model;
    const prompt = $("#selection-prompt").value.trim();
    if (prompt.length < 20) {
      toast("自定义 Prompt 至少需要 20 个字符", "error");
      return null;
    }
    const payload = {
      active_model: provider,
      deepseek_model: $("#deepseek-model").value.trim() || "deepseek-chat",
      gemini_model: $("#gemini-model").value.trim() || "gemini-2.5-flash",
      selection_prompt: prompt,
      auto_annotate: $("#toggle-auto-annotate").checked,
      auto_guide: $("#toggle-auto-guide").checked,
      auto_metadata: $("#toggle-auto-metadata").checked,
      thinking_enabled: $("#toggle-thinking").checked,
      thinking_effort:
        document.querySelector("#thinking-effort [data-thinking-effort].active")?.dataset
          .thinkingEffort || "medium",
      theme: state.theme,
      font_size: Number($("#settings-font-size").value) || state.fontSize,
      reading_mode: state.mode,
      line_width: $("#settings-line-width").value || state.lineWidth,
    };
    const themeChoice = document.querySelector("#settings-theme-options [data-theme-choice].active");
    if (themeChoice) payload.theme = themeChoice.dataset.themeChoice;
    const modeChoice = document.querySelector("#settings-mode [data-mode-choice].active");
    if (modeChoice) payload.reading_mode = modeChoice.dataset.modeChoice;
    const deepseekKey = $("#deepseek-key").value.trim();
    const geminiKey = $("#gemini-key").value.trim();
    if (deepseekKey) payload.deepseek_key = deepseekKey;
    else if (state.clearKeys.has("deepseek")) payload.deepseek_key = "";
    if (geminiKey) payload.gemini_key = geminiKey;
    else if (state.clearKeys.has("gemini")) payload.gemini_key = "";
    return payload;
  }

  async function saveSettings(options = {}) {
    if (state.settingsSaving) return false;
    const payload = buildSettingsPayload();
    if (!payload) return false;
    state.settingsSaving = true;
    const saveButton = $("#settings-save");
    const testButton = $("#settings-test");
    saveButton.disabled = true;
    testButton.disabled = true;
    $("#settings-status").textContent = "正在保存…";
    try {
      const settings = await api("/settings", { method: "PUT", body: payload });
      state.settings = settings;
      state.clearKeys.clear();
      state.settingsDirty = false;
      const nextTheme = payload.theme === "dark" ? "dark" : "light";
      state.theme = nextTheme;
      prefSet("theme", nextTheme);
      state.fontSize = clamp(Number(payload.font_size) || state.fontSize, 16, 32);
      prefSet("font", state.fontSize);
      state.lineWidth = payload.line_width;
      prefSet("lineWidth", state.lineWidth);
      if (payload.reading_mode !== state.mode) {
        setMode(payload.reading_mode, { sync: false });
      }
      applyPreferences();
      updateAiBadge();
      if (state.view === "reader" && state.chapter) {
        const ratio = getReadingRatio();
        requestAnimationFrame(() => {
          if (isPaginated()) {
            measurePages();
            applyPageByRatio(ratio);
          } else {
            setScrollRatio(ratio);
          }
          updateFooter();
        });
        setTimeout(() => scheduleReaderPoll(1200), 600);
      }
      $("#settings-status").textContent = "已保存";
      toast("设置已保存", "success", 2200);
      if (options.test) {
        populateSettings();
        await testConnection();
      } else {
        closeModal("modal-settings");
      }
      return true;
    } catch (error) {
      $("#settings-status").textContent = "";
      toast(error.message, "error");
      return false;
    } finally {
      state.settingsSaving = false;
      saveButton.disabled = false;
      testButton.disabled = false;
    }
  }

  async function testConnection() {
    const resultSpan = $("#test-result");
    resultSpan.className = "test-result loading";
    resultSpan.innerHTML = `<span class="spinner"></span> 正在连接模型…`;
    const selected = document.querySelector('input[name="active-model"]:checked');
    const provider = selected ? selected.value : state.settings.active_model;
    try {
      const result = await api("/settings/test", {
        method: "POST",
        body: { provider },
      });
      if (result.ok) {
        resultSpan.className = "test-result ok";
        const thinking = result.thinking_enabled === false
          ? "直答"
          : `思考·${THINKING_EFFORT_LABELS[result.thinking_effort] || "中"}`;
        resultSpan.textContent = `连接成功 · ${result.model} · ${thinking} · ${result.latency_ms} ms`;
      } else {
        resultSpan.className = "test-result error";
        resultSpan.textContent = result.error || "连接失败";
      }
      return result;
    } catch (error) {
      resultSpan.className = "test-result error";
      resultSpan.textContent = error.message;
      return null;
    }
  }

  async function loadSystemInfoForSettings() {
    try {
      const info = await api("/system/info");
      state.system = info;
      state.stats = info.stats || state.stats;
      $("#lan-url").textContent = info.primary_url || "—";
      const qr = $("#lan-qr");
      if (qr) {
        qr.src = `/api/system/qr?url=${encodeURIComponent(info.primary_url || info.local_url || "")}`;
      }
      $("#data-books").textContent = formatNumber(info.stats && info.stats.books);
      $("#data-words").textContent = formatWords(info.stats && info.stats.words);
      $("#data-notes").textContent = formatNumber(info.stats && info.stats.annotations);
      $("#data-chats").textContent = formatNumber(info.stats && info.stats.chat_messages);
      $("#data-path").textContent = info.database || "";
      $("#about-version").textContent = `v${info.version || "1.2.2"}`;
      $("#about-engine").textContent = info.pdf_engine || "PyMuPDF · EbookLib";
    } catch (error) {
      $("#lan-url").textContent = "无法读取网络信息";
      $("#data-path").textContent = error.message;
    }
  }

  /* ====================================================================== */
  /* Health / boot                                                          */
  /* ====================================================================== */
  async function checkHealth() {
    try {
      const health = await api("/health");
      state.health = health;
      state.stats = health.stats || state.stats;
      updateConnection(true);
      if (health.ai) {
        if (!state.settings) state.settings = {};
        Object.assign(state.settings, health.ai);
        updateAiBadge();
      }
      return true;
    } catch {
      updateConnection(false);
      return false;
    }
  }

  function showScreenError(message) {
    const overlay = $("#screen-error");
    if (!overlay) return;
    $("#screen-error-message").textContent = message || "请确认终端中的服务仍在运行。";
    overlay.hidden = false;
    updateConnection(false);
  }

  async function boot() {
    loadPreferences();
    applyPreferences();
    updateVoiceLangButton();
    renderLibraryLoading();
    const healthy = await checkHealth();
    if (!healthy) {
      showScreenError("无法连接本地服务。请确认 Python 服务仍在运行，然后点击重试。");
      return;
    }
    try {
      state.settings = await api("/settings");
      applyServerDefaults();
      applyPreferences();
      updateAiBadge();
    } catch (error) {
      toast(`读取设置失败：${error.message}`, "error");
    }
    const loaded = await loadLibrary();
    if (!loaded) return;
    state.initialized = true;
    routeFromLocation();
    setInterval(() => {
      if (!document.hidden) checkHealth();
    }, 30000);
  }

  function routeFromLocation() {
    const match = location.hash.match(/^#\/book\/(\d+)(?:\/(\d+))?/);
    if (!match) {
      if (state.view === "reader") showLibraryView({ fromRoute: true });
      return;
    }
    const bookId = Number(match[1]);
    const chapterIndex = match[2] === undefined ? null : Number(match[2]);
    if (
      state.view === "reader" &&
      state.bookId === bookId &&
      (chapterIndex === null || chapterIndex === state.chapterIndex)
    ) {
      return;
    }
    openBook(bookId, chapterIndex, { updateHistory: false });
  }

  /* ====================================================================== */
  /* Event binding                                                          */
  /* ====================================================================== */
  function bindCoreEvents() {
    $("#library-theme-toggle").addEventListener("click", toggleTheme);
    $("#reader-theme-toggle").addEventListener("click", toggleTheme);
    $("#library-settings").addEventListener("click", openSettings);
    $("#reader-settings").addEventListener("click", openSettings);
    $("#ai-open-settings").addEventListener("click", openSettings);

    ["#import-open", "#hero-import", "#empty-import"].forEach((selector) => {
      const button = $(selector);
      if (button) button.addEventListener("click", () => openImportModal());
    });
    $("#brand-home").addEventListener("click", (event) => {
      event.preventDefault();
      showLibraryView({ refresh: true });
    });
    $("#back-to-library").addEventListener("click", () => showLibraryView({ refresh: true }));
    $("#hero-continue").addEventListener("click", () => {
      const id = Number($("#hero-continue").dataset.bookId);
      if (id) openBook(id, null);
    });

    $("#book-grid").addEventListener("click", (event) => {
      const deleteButton = event.target.closest("[data-delete-book]");
      if (deleteButton) {
        event.preventDefault();
        event.stopPropagation();
        deleteBook(deleteButton.dataset.deleteBook);
        return;
      }
      if (event.target.closest("[data-download]")) {
        event.stopPropagation();
        return;
      }
      const card = event.target.closest("[data-book-id]");
      if (card) openBook(Number(card.dataset.bookId), null);
    });
    $("#book-grid").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      if (event.target.closest("a, button")) return;
      const card = event.target.closest("[data-book-id]");
      if (!card) return;
      event.preventDefault();
      openBook(Number(card.dataset.bookId), null);
    });

    $$("#mode-switch [data-mode]").forEach((button) => {
      button.addEventListener("click", () => setMode(button.dataset.mode));
    });
    $("#font-decrease").addEventListener("click", () => setFontSize(state.fontSize - 1));
    $("#font-increase").addEventListener("click", () => setFontSize(state.fontSize + 1));

    $("#left-toggle").addEventListener("click", toggleLeftSidebar);
    $("#right-toggle").addEventListener("click", toggleRightSidebar);
    $("#sidebar-right").addEventListener("pointerdown", (event) => {
      if (event.target.closest("input, textarea, select, button, a, label")) return;
      const explanation = event.target.closest("#explanation");
      if (explanation) explanation.focus({ preventScroll: true });
      const active = document.activeElement;
      if (active && ["INPUT", "TEXTAREA"].includes(active.tagName)) active.blur();
    });
    $("#sidebar-backdrop").addEventListener("click", closeMobileSidebars);

    $$(".sidebar-tabs button").forEach((button) => {
      button.addEventListener("click", () => switchSidebarTab(button.dataset.sidebarTab));
    });

    $("#footer-prev").addEventListener("click", () => turnPage(-1));
    $("#footer-next").addEventListener("click", () => turnPage(1));

    $("#search-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runSearch($("#search-input").value);
    });
    const searchInput = $("#search-input");
    let searchTimer = null;
    searchInput.addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => runSearch(searchInput.value), 380);
    });

    $("#toc-list").addEventListener("click", (event) => {
      const button = event.target.closest("[data-chapter]");
      if (!button) return;
      loadChapter(Number(button.dataset.chapter));
    });

    $("#background-regenerate").addEventListener("click", generateBackground);
    $("#background-content").addEventListener("click", (event) => {
      if (event.target.closest("[data-bg-generate]")) generateBackground();
      if (event.target.closest("[data-open-settings]")) openSettings();
    });

    $("#search-results").addEventListener("click", (event) => {
      const button = event.target.closest("[data-search-chapter]");
      if (!button) return;
      jumpToParagraph(
        Number(button.dataset.searchChapter),
        Number(button.dataset.searchParagraph)
      );
    });

    $("#reading-content").addEventListener("click", (event) => {
      const mark = event.target.closest("mark.hl");
      if (mark) {
        openAnnotation(Number(mark.dataset.annotationId));
        return;
      }
      if (event.target.closest("[data-guide-toggle]")) {
        toggleGuide();
        return;
      }
      if (event.target.closest("[data-guide-regen]")) {
        generateGuide(true);
        return;
      }
      if (event.target.closest("[data-open-settings]")) {
        openSettings();
        return;
      }
      const chapterButton = event.target.closest("[data-goto-chapter]");
      if (chapterButton) {
        loadChapter(Number(chapterButton.dataset.gotoChapter), { ratioOverride: 0 });
        return;
      }
      if (event.target.closest("[data-back-library]")) {
        showLibraryView({ refresh: true });
      }
    });
    $("#reading-content").addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      const mark = event.target.closest("mark.hl");
      if (mark) {
        event.preventDefault();
        openAnnotation(Number(mark.dataset.annotationId));
      }
    });

    const surface = $("#reading-surface");
    surface.addEventListener("scroll", () => {
      if (isPaginated()) return;
      if (state.scrollRaf) return;
      state.scrollRaf = requestAnimationFrame(() => {
        state.scrollRaf = 0;
        if (state.view !== "reader" || !state.chapter) return;
        updateFooter();
        savePosition();
      });
    }, { passive: true });

    // 翻页模式：鼠标滚轮 / 触控板上下滑动翻页。
    let wheelAccumulator = 0;
    let wheelResetTimer = null;
    let wheelLockedUntil = 0;
    surface.addEventListener("wheel", (event) => {
      if (!isPaginated() || state.modalStack.length) return;
      if (event.ctrlKey) return; // 保留浏览器缩放
      event.preventDefault();
      const now = Date.now();
      if (now < wheelLockedUntil) return;
      const multiplier = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? 420 : 1;
      wheelAccumulator += event.deltaY * multiplier;
      clearTimeout(wheelResetTimer);
      wheelResetTimer = setTimeout(() => {
        wheelAccumulator = 0;
      }, 220);
      if (Math.abs(wheelAccumulator) < 32) return;
      const direction = wheelAccumulator > 0 ? 1 : -1;
      wheelAccumulator = 0;
      wheelLockedUntil = now + 360;
      turnPage(direction);
    }, { passive: false });

    bindSelectionEvents();
    bindAiEvents();
    bindChatEvents();
    bindImportEvents();
    bindSettingsEvents();
    bindModalEvents();
    bindKeyboardAndResize();
  }

  function bindSelectionEvents() {
    ["mouseup", "touchend", "keyup"].forEach((eventName) => {
      document.addEventListener(
        eventName,
        (event) => {
          if (state.view !== "reader") return;
          const target = event.target;
          if (target instanceof Element && target.closest("#selection-toolbar")) return;
          if (eventName === "touchend") {
            setTimeout(handleSelection, 60);
          } else if (eventName === "keyup" && !(event.shiftKey || event.key === "Shift")) {
            return;
          } else {
            setTimeout(handleSelection, 10);
          }
        },
        { passive: true }
      );
    });

    document.addEventListener("mousedown", (event) => {
      if (!(event.target instanceof Element) || !event.target.closest("p.para, #selection-toolbar")) {
        hideSelectionToolbar();
      }
    });
    document.addEventListener("touchstart", (event) => {
      if (!(event.target instanceof Element) || !event.target.closest("p.para, #selection-toolbar")) {
        hideSelectionToolbar();
      }
    }, { passive: true });

    $("#selection-toolbar").addEventListener("click", (event) => {
      const button = event.target.closest("[data-selection-action]");
      if (!button) return;
      event.preventDefault();
      event.stopPropagation();
      handleSelectionAction(button.dataset.selectionAction);
    });

    $("#reading-surface").addEventListener("scroll", hideSelectionToolbar, { passive: true });

    const surface = $("#reading-surface");
    let touchStart = null;
    surface.addEventListener("touchstart", (event) => {
      if (!isPaginated() || event.touches.length !== 1) return;
      touchStart = { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }, { passive: true });
    surface.addEventListener("touchend", (event) => {
      if (!touchStart || !isPaginated() || !event.changedTouches.length) return;
      const dx = event.changedTouches[0].clientX - touchStart.x;
      const dy = event.changedTouches[0].clientY - touchStart.y;
      touchStart = null;
      if (Math.abs(dx) < 55 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
      turnPage(dx < 0 ? 1 : -1);
    }, { passive: true });
  }

  function bindAiEvents() {
    $("#speak-selection").addEventListener("click", () => {
      const annotation = state.annotations.find(
        (item) => Number(item.id) === Number(state.activeAnnotationId)
      );
      if (annotation) speakGerman(annotation.selected_text);
    });
    $("#regenerate-explanation").addEventListener("click", regenerateExplanation);
    $("#delete-annotation").addEventListener("click", deleteAnnotation);
    $("#toggle-explanation").addEventListener("click", () => {
      const block = $(".explanation-block");
      setExplanationCollapsed(!(block && block.classList.contains("collapsed")));
    });
    $("#retry-annotation").addEventListener("click", () => {
      if (state.pendingSelection) requestAnnotation(state.pendingSelection, { force: true });
    });
  }

  function bindChatEvents() {
    const input = $("#chat-input");
    $("#chat-form").addEventListener("submit", (event) => {
      event.preventDefault();
      sendChat();
    });
    input.addEventListener("input", () => autoGrowTextarea(input));
    input.addEventListener("focus", () => {
      const block = $(".explanation-block");
      if (block && !block.classList.contains("collapsed")) {
        setExplanationCollapsed(true);
      }
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        sendChat();
      }
    });
    $("#clear-chat").addEventListener("click", clearChat);
    $("#mic-button").addEventListener("click", toggleRecording);
    $("#voice-lang").addEventListener("click", () => {
      state.voiceLang = state.voiceLang === "de-DE" ? "zh-CN" : "de-DE";
      prefSet("voiceLang", state.voiceLang);
      if (state.recording && state.recognition) {
        try {
          state.recognition.stop();
        } catch {
          /* ignore */
        }
      }
      updateVoiceLangButton();
      toast(`语音识别语言：${state.voiceLang === "de-DE" ? "德语" : "中文"}`, "info", 1800);
    });
  }

  function bindImportEvents() {
    const dropzone = $("#dropzone");
    const input = $("#file-input");
    dropzone.addEventListener("click", () => {
      if (!state.importing) input.click();
    });
    dropzone.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && !state.importing) {
        event.preventDefault();
        input.click();
      }
    });
    input.addEventListener("change", () => {
      if (input.files && input.files[0]) setImportFile(input.files[0]);
    });
    ["dragenter", "dragover"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        if (!state.importing) dropzone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach((eventName) => {
      dropzone.addEventListener(eventName, (event) => {
        event.preventDefault();
        dropzone.classList.remove("dragover");
      });
    });
    dropzone.addEventListener("drop", (event) => {
      if (state.importing) return;
      event.stopPropagation();
      const file = event.dataTransfer && event.dataTransfer.files[0];
      if (file) setImportFile(file);
    });
    $("#file-remove").addEventListener("click", (event) => {
      event.stopPropagation();
      state.importFile = null;
      input.value = "";
      $("#file-chosen").hidden = true;
      $("#import-submit").disabled = true;
    });
    $("#import-submit").addEventListener("click", submitImport);

    const libraryView = $("#library-view");
    libraryView.addEventListener("dragover", (event) => {
      if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files")) {
        event.preventDefault();
      }
    });
    libraryView.addEventListener("drop", (event) => {
      if (!event.dataTransfer || !event.dataTransfer.files.length) return;
      event.preventDefault();
      openImportModal(event.dataTransfer.files[0]);
    });

    // 防止把文件拖到阅读器/其他区域时浏览器直接打开文件。
    document.addEventListener("dragover", (event) => {
      if (event.dataTransfer && Array.from(event.dataTransfer.types || []).includes("Files")) {
        event.preventDefault();
      }
    });
    document.addEventListener("drop", (event) => {
      if (event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files.length) {
        event.preventDefault();
      }
    });
  }

  function bindSettingsEvents() {
    $$("[data-settings-tab]").forEach((button) => {
      button.addEventListener("click", () => switchSettingsTab(button.dataset.settingsTab));
    });
    $$('input[name="active-model"]').forEach((input) => {
      input.addEventListener("change", () => {
        updateProviderCards();
        markSettingsDirty();
      });
    });
    $("#toggle-thinking").addEventListener("change", () => {
      updateThinkingControls();
      markSettingsDirty();
    });
    $$("#thinking-effort [data-thinking-effort]").forEach((button) => {
      button.addEventListener("click", () => {
        if (button.disabled) return;
        $$("#thinking-effort [data-thinking-effort]").forEach((item) => {
          item.classList.toggle("active", item === button);
        });
        markSettingsDirty();
      });
    });
    $$("[data-toggle-key]").forEach((button) => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.toggleKey);
        if (!input) return;
        const show = input.type === "password";
        input.type = show ? "text" : "password";
        button.textContent = show ? "隐藏" : "显示";
      });
    });
    $$("[data-clear-key]").forEach((button) => {
      button.addEventListener("click", () => {
        const provider = button.dataset.clearKey;
        const input = document.getElementById(`${provider}-key`);
        if (input) input.value = "";
        state.clearKeys.add(provider);
        const hint = document.getElementById(`${provider}-key-hint`);
        if (hint) hint.textContent = "保存后清除";
        markSettingsDirty();
      });
    });
    $("#reset-prompt").addEventListener("click", () => {
      $("#selection-prompt").value =
        state.settings.default_selection_prompt || $("#selection-prompt").value;
      markSettingsDirty();
    });
    $$("[data-insert-placeholder]").forEach((button) => {
      button.addEventListener("click", () => {
        const textarea = $("#selection-prompt");
        const placeholder = button.dataset.insertPlaceholder;
        const start = textarea.selectionStart || 0;
        const end = textarea.selectionEnd || 0;
        textarea.setRangeText(placeholder, start, end, "end");
        textarea.focus();
        markSettingsDirty();
      });
    });
    $("#selection-prompt").addEventListener("input", markSettingsDirty);
    $$("#settings-theme-options [data-theme-choice]").forEach((button) => {
      button.addEventListener("click", () => {
        $$("#settings-theme-options [data-theme-choice]").forEach((item) => {
          item.classList.toggle("active", item === button);
        });
        markSettingsDirty();
      });
    });
    $("#settings-font-size").addEventListener("input", () => {
      $("#settings-font-label").textContent = `${$("#settings-font-size").value} px`;
      markSettingsDirty();
    });
    $$("#settings-mode [data-mode-choice]").forEach((button) => {
      button.addEventListener("click", () => {
        $$("#settings-mode [data-mode-choice]").forEach((item) => {
          item.classList.toggle("active", item === button);
        });
        markSettingsDirty();
      });
    });
    $("#settings-line-width").addEventListener("change", markSettingsDirty);
    ["#toggle-auto-annotate", "#toggle-auto-guide", "#toggle-auto-metadata"].forEach((selector) => {
      $(selector).addEventListener("change", markSettingsDirty);
    });
    ["#deepseek-key", "#gemini-key", "#deepseek-model", "#gemini-model"].forEach((selector) => {
      $(selector).addEventListener("input", markSettingsDirty);
    });
    $("#deepseek-key").addEventListener("input", () => {
      if ($("#deepseek-key").value.trim()) state.clearKeys.delete("deepseek");
    });
    $("#gemini-key").addEventListener("input", () => {
      if ($("#gemini-key").value.trim()) state.clearKeys.delete("gemini");
    });
    $("#settings-save").addEventListener("click", () => saveSettings());
    $("#settings-test").addEventListener("click", () => saveSettings({ test: true }));
    $("#copy-lan-url").addEventListener("click", async () => {
      const url = $("#lan-url").textContent.trim();
      if (!url || url === "—") return;
      const ok = await copyText(url);
      toast(ok ? "局域网地址已复制" : "复制失败", ok ? "success" : "error");
    });
    $("#lan-qr").addEventListener("error", () => {
      $("#lan-qr").style.opacity = "0.2";
    });
  }

  function bindModalEvents() {
    $$("[data-close-modal]").forEach((element) => {
      element.addEventListener("click", () => {
        const modal = element.closest(".modal");
        if (modal) closeModal(modal.id);
      });
    });
    $$("[data-confirm-cancel]").forEach((element) => {
      element.addEventListener("click", () => {
        if (state.confirmResolver) {
          const resolve = state.confirmResolver;
          state.confirmResolver = null;
          resolve(false);
        }
        closeModal("modal-confirm", { force: true, silent: true });
      });
    });
    $("#screen-error-retry").addEventListener("click", () => location.reload());
  }

  function bindKeyboardAndResize() {
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        if (state.modalStack.length) {
          closeTopModal();
          return;
        }
        if (state.toolbarVisible) {
          hideSelectionToolbar();
          return;
        }
        if (document.body.classList.contains("left-open") || document.body.classList.contains("right-open")) {
          closeMobileSidebars();
          return;
        }
      }
      if (state.modalStack.length) {
        trapFocus(event);
        return;
      }
      if (state.view !== "reader" || !state.chapter) return;
      const active = document.activeElement;
      const tag = active ? active.tagName : "";
      const canHoldFromEmptyChat =
        active &&
        active.id === "chat-input" &&
        active.value.trim() === "" &&
        (event.code === "Space" || event.key === " ");
      if (["INPUT", "TEXTAREA", "SELECT"].includes(tag) && !canHoldFromEmptyChat) return;
      if (event.altKey || event.ctrlKey || event.metaKey) return;
      if (handleSpaceDown(event, { allowChatInput: canHoldFromEmptyChat })) return;
      if (!isPaginated()) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        turnPage(-1);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        turnPage(1);
      }
    });

    const onResize = debounce(() => {
      hideSelectionToolbar();
      updateExplanationOverflowHint();
      if (state.view !== "reader" || !state.chapter) return;
      if (isPaginated()) {
        const ratio = getReadingRatio();
        measurePages();
        applyPageByRatio(ratio);
      }
      updateFooter();
    }, 180);
    window.addEventListener("resize", onResize);
    window.addEventListener("orientationchange", onResize);
    window.addEventListener("popstate", routeFromLocation);
    document.addEventListener("keyup", (event) => {
      if (event.code === "Space" || event.key === " ") handleSpaceUp(event);
    });
    window.addEventListener("blur", () => {
      if (state.spaceHeld) {
        state.spaceHeld = false;
        stopRecognition();
      }
    });

    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) checkHealth();
    });
  }

  /* ====================================================================== */
  /* Start                                                                  */
  /* ====================================================================== */
  document.addEventListener("DOMContentLoaded", () => {
    bindCoreEvents();
    boot().catch((error) => {
      console.error(error);
      showScreenError(error.message);
    });
  });
})();
