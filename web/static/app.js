const $ = (id) => document.getElementById(id);

/* --------------------------------------------------------------- icons -- */
const ICON_PATHS = {
  spark: '<path d="M12 3l1.6 4.8L18.4 9.4l-4.8 1.6L12 15.8l-1.6-4.8L5.6 9.4l4.8-1.6z"/><path d="M18.5 15.5l.7 2.1 2.1.7-2.1.7-.7 2.1-.7-2.1-2.1-.7 2.1-.7z"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  store: '<path d="M6 8h12l-1 12H7L6 8z"/><path d="M9 8V6a3 3 0 0 1 6 0v2"/>',
  wechat: '<path d="M21 11.5A8.4 8.4 0 0 1 12.5 20a9 9 0 0 1-3.2-.6L4 21l1.6-4.2A8.4 8.4 0 0 1 12.5 3 8.4 8.4 0 0 1 21 11.5z"/><path d="M8.5 10.5h.01M14 10.5h.01"/>',
  qq: '<path d="M22 2 11 13"/><path d="M22 2l-7 20-4-9-9-4z"/>',
  image: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8.5" cy="8.5" r="1.5"/><path d="M21 15l-5-5L5 21"/>',
  sticker: '<circle cx="12" cy="12" r="9"/><path d="M8.5 10h.01M15.5 10h.01"/><path d="M8 14.5a5 5 0 0 0 8 0"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  heart: '<path d="M12 20s-7-4.5-9.2-8.4A5 5 0 0 1 12 6a5 5 0 0 1 9.2 5.6C19 15.5 12 20 12 20z"/>',
  sliders: '<path d="M4 21v-6M4 11V3M12 21v-9M12 8V3M20 21v-4M20 13V3"/><circle cx="4" cy="13" r="2"/><circle cx="12" cy="10" r="2"/><circle cx="20" cy="15" r="2"/>',
  mic: '<rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/>',
  cpu: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  watch: '<rect x="7" y="6" width="10" height="12" rx="3"/><path d="M9 6V3h6v3M9 18v3h6v-3"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="3"/><path d="M3 7l9 6 9-6"/>',
  key: '<circle cx="8" cy="8" r="4"/><path d="M11 11l9 9M17 17l2-2M14 20l2-2"/>',
  download: '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/>',
  warning: '<path d="M12 3l9 16H3z"/><path d="M12 9v4M12 16.5h.01"/>',
  moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4 8 8 0 1 0 20 14.5z"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  logout: '<path d="M15 3h3a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2h-3"/><path d="M10 17l-5-5 5-5"/><path d="M5 12h12"/>',
  trash: '<path d="M4 7h16"/><path d="M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/><path d="M6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/><path d="M10 11v6M14 11v6"/>',
  brain: '<path d="M12 5a3 3 0 0 0-6 0 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1z"/><path d="M12 5a3 3 0 0 1 6 0 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1z"/>',
  home: '<path d="M3 9.5 12 3l9 6.5V20a1 1 0 0 1-1 1h-5v-7h-6v7H4a1 1 0 0 1-1-1Z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21v-1a6 6 0 0 1 16 0v1"/>',
  chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
  moments: '<rect x="3.5" y="3.5" width="12.5" height="12.5" rx="3.2"/><path d="M8.5 20.5H17a3.5 3.5 0 0 0 3.5-3.5V8.5"/><circle cx="9.8" cy="8.2" r="1.3"/>',
  bell: '<path d="M18 8a6 6 0 1 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  send: '<path d="M3 11 21 3l-8 18-2-8z"/>',
  search: '<circle cx="11" cy="11" r="7"/><line x1="21" y1="21" x2="16.5" y2="16.5"/>',
  chevron: '<path d="M9 6l6 6-6 6"/>',
  close: '<path d="M6 6l12 12M18 6L6 18"/>',
  refresh: '<path d="M21 12a9 9 0 1 1-2.6-6.4"/><path d="M21 4v5h-5"/>',
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  gift: '<rect x="3" y="8" width="18" height="4" rx="1"/><path d="M5 12v8h14v-8"/><path d="M12 8v12"/><path d="M12 8S11 4 8 4a2 2 0 0 0 0 4h4zM12 8s1-4 4-4a2 2 0 0 1 0 4h-4z"/>',
  eye: '<path d="M2 12s3.6-6.5 10-6.5S22 12 22 12s-3.6 6.5-10 6.5S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/>',
  "eye-off": '<path d="M3 3l18 18"/><path d="M10.6 6.1A9.7 9.7 0 0 1 12 6c6.4 0 10 6 10 6a17 17 0 0 1-2.6 3.3"/><path d="M6.4 6.5A17 17 0 0 0 2 12s3.6 6 10 6a9.4 9.4 0 0 0 3.6-.7"/><path d="M9.5 9.6a2.6 2.6 0 0 0 3.6 3.6"/>',
  shield: '<path d="M12 3l8 3v6c0 4.4-3.2 7.8-8 9-4.8-1.2-8-4.6-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
};
function icon(name, cls = "ic") {
  const path = ICON_PATHS[name] || "";
  return `<svg class="${cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
}

/* ------------------------------------------------------------- ambient -- */

function onScroll() {
  document.body.classList.toggle("scrolled", window.scrollY > 6);
}
window.addEventListener("scroll", onScroll, { passive: true });
onScroll();

/* --------------------------------------------------- segments + reveals -- */
function moveIndicator(seg) {
  const ind = seg.querySelector(":scope > .seg-ind");
  if (!ind) return;
  const active = seg.querySelector(".active");
  if (!active) { ind.style.opacity = "0"; return; }
  ind.style.opacity = "1";
  ind.style.width = active.offsetWidth + "px";
  ind.style.height = active.offsetHeight + "px";
  ind.style.transform = `translate(${active.offsetLeft}px, ${active.offsetTop}px)`;
}
const segRegistry = [];
window.addEventListener("resize", () => {
  for (let i = segRegistry.length - 1; i >= 0; i -= 1) {
    if (!segRegistry[i].isConnected) segRegistry.splice(i, 1);
    else moveIndicator(segRegistry[i]);
  }
});
function initSegments(root = document) {
  root.querySelectorAll(".tabs, .segment, .seg-nav").forEach((seg) => {
    if (!seg.dataset.segReady) {
      seg.dataset.segReady = "1";
      const ind = document.createElement("span");
      ind.className = "seg-ind";
      seg.prepend(ind);
      seg.addEventListener("click", () => requestAnimationFrame(() => moveIndicator(seg)));
      if (!segRegistry.includes(seg)) segRegistry.push(seg);
    }
    requestAnimationFrame(() => moveIndicator(seg));
  });
}
function refreshSegment(seg) {
  if (seg) requestAnimationFrame(() => moveIndicator(seg));
}
function revealInit(root = document) {
  const nodes = root.querySelectorAll(".reveal");
  if (!("IntersectionObserver" in window)) { nodes.forEach((n) => n.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (entry.isIntersecting) { entry.target.classList.add("in"); io.unobserve(entry.target); }
    });
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  nodes.forEach((n) => io.observe(n));
}
window.addEventListener("load", () => { initSegments(); revealInit(); mountStarfield(); });

/* -------------------------------------------------------------- motion -- */
function prefersReduced() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function rippleAt(button, event) {
  if (prefersReduced()) return;
  const rect = button.getBoundingClientRect();
  const size = Math.max(rect.width, rect.height);
  const span = document.createElement("span");
  span.className = "ripple";
  span.style.width = span.style.height = size + "px";
  const x = (event.clientX || rect.left + rect.width / 2) - rect.left - size / 2;
  const y = (event.clientY || rect.top + rect.height / 2) - rect.top - size / 2;
  span.style.left = x + "px";
  span.style.top = y + "px";
  button.appendChild(span);
  setTimeout(() => span.remove(), 560);
}
document.addEventListener("pointerdown", (event) => {
  const node = event.target.closest && event.target.closest(".btn, .icon-btn, .card-del");
  if (node && !node.disabled) rippleAt(node, event);
});

function animateCounts(root = document) {
  if (prefersReduced()) return;
  root.querySelectorAll("[data-count]").forEach((node) => {
    const target = Number(node.dataset.count);
    if (!Number.isFinite(target)) return;
    const start = performance.now();
    const duration = 680;
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      node.textContent = Math.round(target * (1 - Math.pow(1 - t, 3)));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  });
}

/* ----------------------------------------------------- starfield (环星) -- */
let ambientPaused = false;
function setAmbientPaused(paused) {
  const next = Boolean(paused);
  if (next === ambientPaused) return;
  ambientPaused = next;
  document.documentElement.classList.toggle("ambient-paused", ambientPaused);
}
function mountStarfield() {
  if (prefersReduced() || document.querySelector(".starfield")) return;
  const canvas = document.createElement("canvas");
  canvas.className = "starfield";
  canvas.setAttribute("aria-hidden", "true");
  document.body.prepend(canvas);
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const COLORS = [[124, 92, 255], [61, 139, 255], [240, 101, 149], [255, 255, 255]];
  const sprites = COLORS.map((color) => {
    const size = 32;
    const sprite = document.createElement("canvas");
    sprite.width = sprite.height = size;
    const g = sprite.getContext("2d");
    const grad = g.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
    grad.addColorStop(0, `rgba(${color[0]},${color[1]},${color[2]},1)`);
    grad.addColorStop(0.38, `rgba(${color[0]},${color[1]},${color[2]},0.42)`);
    grad.addColorStop(1, `rgba(${color[0]},${color[1]},${color[2]},0)`);
    g.fillStyle = grad;
    g.fillRect(0, 0, size, size);
    return sprite;
  });

  let w = 0, h = 0, raf = 0, last = 0, renderAt = 0;
  let particles = [];
  let centers = [];

  function build() {
    const count = Math.min(56, Math.max(20, Math.round((w * h) / 34000)));
    const span = Math.min(w, h);
    centers = [
      { x: w * 0.22, y: h * 0.16, r: span * 0.36, dir: 1 },
      { x: w * 0.84, y: h * 0.60, r: span * 0.44, dir: -1 },
    ];
    particles = Array.from({ length: count }, (_, i) => {
      const orbit = i % 3 === 0;
      const center = centers[i % centers.length];
      return {
        orbit,
        center,
        angle: Math.random() * Math.PI * 2,
        radius: center.r * (0.30 + Math.random() * 0.85),
        speed: (0.00005 + Math.random() * 0.00009) * center.dir,
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.10,
        vy: (Math.random() - 0.5) * 0.10,
        size: 1 + Math.random() * 2.2,
        alpha: 0.14 + Math.random() * 0.34,
        phase: Math.random() * Math.PI * 2,
        twinkle: 0.4 + Math.random() * 0.9,
        sprite: sprites[i % sprites.length],
      };
    });
  }

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    w = canvas.clientWidth || window.innerWidth;
    h = canvas.clientHeight || window.innerHeight;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    build();
  }

  function frame(now) {
    raf = requestAnimationFrame(frame);
    if (ambientPaused) { last = 0; return; }
    if (now - renderAt < 32) return;
    const dt = last ? Math.min(now - last, 64) : 32;
    last = now;
    renderAt = now;
    ctx.clearRect(0, 0, w, h);
    for (const p of particles) {
      if (p.orbit) {
        p.angle += p.speed * dt;
        const targetX = p.center.x + Math.cos(p.angle) * p.radius;
        const targetY = p.center.y + Math.sin(p.angle) * p.radius;
        p.x += (targetX - p.x) * 0.05;
        p.y += (targetY - p.y) * 0.05;
      } else {
        p.x += p.vx * (dt / 16);
        p.y += p.vy * (dt / 16);
        if (p.x < -12) p.x = w + 12; else if (p.x > w + 12) p.x = -12;
        if (p.y < -12) p.y = h + 12; else if (p.y > h + 12) p.y = -12;
      }
      const twinkle = 0.5 + 0.5 * Math.sin((now / 1500) * p.twinkle + p.phase);
      const radius = p.size * 3.2;
      ctx.globalAlpha = p.alpha * (0.35 + 0.65 * twinkle);
      ctx.drawImage(p.sprite, p.x - radius, p.y - radius, radius * 2, radius * 2);
    }
    ctx.globalAlpha = 1;
  }

  resize();
  let resizeTimer = 0;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 180); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { cancelAnimationFrame(raf); raf = 0; }
    else if (!raf) { last = 0; renderAt = 0; raf = requestAnimationFrame(frame); }
  });
  raf = requestAnimationFrame(frame);
}

/* ----------------------------------------------------------------- api -- */
function _errorMessage(data) {
  if (!data) return "请求失败";
  const detail = data.detail !== undefined ? data.detail : data.error;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail) && detail.length) {
    const parts = detail.map((item) => item && item.msg ? item.msg : JSON.stringify(item));
    return parts.join("；") || "请求参数有误";
  }
  if (detail) return String(detail);
  return "请求失败";
}
async function api(path, options = {}) {
  const opts = { credentials: "same-origin", ...options };
  if (opts.body && !(opts.body instanceof FormData)) {
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  }
  const method = (opts.method || "GET").toUpperCase();
  const maxAttempts = method === "GET" ? 4 : 1;
  for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
    if (attempt) await new Promise((resolve) => setTimeout(resolve, 260 * 2 ** (attempt - 1) + Math.random() * 220));
    let response;
    try {
      response = await fetch(path, opts);
    } catch (error) {
      if (navigator.onLine === false) throw new Error("网络已断开，请检查网络后重试");
      if (attempt < maxAttempts - 1) continue;
      throw new Error("网络不稳定，连接失败，请重试");
    }
    if (response.status === 401 && !path.startsWith("/api/auth")) {
      window.location.href = window.location.pathname.startsWith("/admin") ? "/admin/login" : "/login";
      throw new Error("未登录");
    }
    if ([502, 503, 504].includes(response.status) && attempt < maxAttempts - 1) continue;
    const type = response.headers.get("content-type") || "";
    const data = type.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) {
      const error = new Error(_errorMessage(data));
      error.status = response.status;
      throw error;
    }
    return data;
  }
  throw new Error("网络不稳定，请稍后重试");
}

/* --------------------------------------------------------------- toast -- */
function toast(message) {
  let host = document.querySelector(".toasts");
  if (!host) {
    host = document.createElement("div");
    host.className = "toasts";
    host.setAttribute("role", "status");
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  const node = document.createElement("div");
  node.className = "toast";
  node.textContent = message;
  host.appendChild(node);
  setTimeout(() => {
    node.style.transition = "opacity .3s var(--ease), transform .3s var(--ease)";
    node.style.opacity = "0";
    node.style.transform = "translateY(8px) scale(.96)";
    setTimeout(() => node.remove(), 320);
  }, 2400);
}

/* ----------------------------------------------------------------- nav -- */
const NAV_ITEMS = [
  { href: "/app", label: "首页", icon: "spark", match: /^\/app$/ },
  { href: "/app/moments", label: "朋友圈", icon: "moments", match: /^\/app\/moments/ },
  { href: "/app/create/distill", label: "创建", icon: "plus", match: /^\/app\/create/ },
  { href: "/app/memory", label: "记忆", icon: "brain", match: /^\/app\/memory/ },
  { href: "/app/settings", label: "我的", icon: "user", match: /^\/app\/settings/ },
];

function mountTabbar(active) {
  let bar = document.querySelector(".tabbar");
  if (!bar) { bar = document.createElement("nav"); bar.className = "tabbar"; document.body.appendChild(bar); }
  bar.innerHTML = NAV_ITEMS.map((item) => {
    const on = item.match ? item.match.test(active) : item.href === active;
    return `<a href="${item.href}" class="${on ? "active" : ""}">${icon(item.icon)}<span>${item.label}</span></a>`;
  }).join("");
}

async function me() { return api("/api/me"); }

async function mountNotifications() {
  const shell = document.querySelector(".shell");
  if (!shell || document.querySelector("#noticeBanner")) return;
  let data;
  try { data = await api("/api/notifications"); } catch (error) { return; }
  const items = (data.items || []).filter((item) => !item.read_at);
  if (!items.length) return;
  const node = document.createElement("section");
  node.id = "noticeBanner";
  node.className = "glass card notice reveal";
  const body = items.map((item) => `<div class="notice-item">${escapeHtml(item.message || "有一条新提醒")}</div>`).join("");
  node.innerHTML =
    `<div class="notice-head">${icon("warning")}<span>需要处理</span></div>${body}` +
    `<div class="notice-foot"><button class="btn small ghost" id="noticeRead">知道了</button></div>`;
  shell.insertBefore(node, shell.firstElementChild);
  node.querySelector("#noticeRead").onclick = async () => {
    node.remove();
    try { await api("/api/notifications/read", { method: "POST" }); } catch (error) { /* 忽略 */ }
  };
  revealInit(shell);
}

function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

function fmtDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/* --------------------------------------------------------- modal/sheet -- */
function focusFirst(mask) {
  const items = focusableItems(mask);
  if (items.length) items[0].focus();
}

function focusableItems(mask) {
  const selector = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
  return Array.prototype.filter.call(
    mask.querySelectorAll(selector),
    (el) => !el.disabled && el.offsetParent !== null && !el.hasAttribute("data-skip-focus"),
  );
}

function trapFocus(mask, event) {
  if (event.key !== "Tab") return;
  const items = focusableItems(mask);
  if (!items.length) return;
  const first = items[0], last = items[items.length - 1];
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
}

function closeModal(mask) {
  if (!mask || mask.dataset.closing) return;
  mask.dataset.closing = "1";
  mask.style.pointerEvents = "none";
  const restore = mask._lastFocus;
  const modal = mask.querySelector(".modal");
  if (mask.classList.contains("sheet")) {
    mask.style.transition = "background-color .32s var(--ease), backdrop-filter .32s var(--ease), -webkit-backdrop-filter .32s var(--ease)";
    mask.style.backgroundColor = "rgba(30, 26, 60, 0)";
    mask.style.backdropFilter = "blur(0px)";
    mask.style.webkitBackdropFilter = "blur(0px)";
    if (modal) {
      modal.style.transition = "transform .42s cubic-bezier(0.4, 0, 0.2, 1)";
      modal.style.transform = "translateY(102%)";
    }
    setTimeout(() => { mask.remove(); syncScrollLock(); if (restore && restore.focus) restore.focus(); }, 430);
  } else {
    mask.style.transition = "opacity .26s var(--ease)";
    mask.style.opacity = "0";
    if (modal) {
      modal.style.transition = "transform .3s var(--ease), opacity .3s var(--ease)";
      modal.style.transform = "scale(.96)";
      modal.style.opacity = "0";
    }
    setTimeout(() => { mask.remove(); syncScrollLock(); if (restore && restore.focus) restore.focus(); }, 300);
  }
}

function syncScrollLock() {
  const masks = Array.prototype.filter.call(document.querySelectorAll(".modal-mask"), (m) => !m.dataset.closing);
  document.body.style.overflow = masks.length ? "hidden" : "";
  // 只要还开着浮层就暂停背景动画（环星 + 极光漂移），否则 backdrop-filter 每帧
  // 都要对被动画改写的背景重新取景模糊，滚动时会明显掉帧。
  setAmbientPaused(masks.length > 0);
  // 只有最上层的浮层需要背景模糊；下层被完全遮住，关掉可省掉一层全屏合成。
  masks.forEach((mask, index) => mask.classList.toggle("under", index < masks.length - 1));
}

function enableSheetDrag(mask) {
  const modal = mask.querySelector(".modal");
  if (!modal) return;
  let startY = 0, startX = 0, dy = 0, active = false, lastY = 0, lastT = 0, vy = 0, startT = 0, moved = 0, fromHandle = false;
  const onMove = (e) => {
    if (!active) return;
    dy = e.clientY - startY;
    if (dy < 0) dy *= 0.18;
    moved = Math.max(moved, Math.hypot(e.clientX - startX, e.clientY - startY));
    const now = performance.now();
    if (now - lastT > 16) { vy = (e.clientY - lastY) / (now - lastT); lastY = e.clientY; lastT = now; }
    const span = modal.offsetHeight || 480;
    modal.style.transform = `translateY(${dy}px)`;
    mask.style.opacity = String(1 - Math.min(0.7, Math.max(0, dy) / span * 0.7));
  };
  const onUp = () => {
    if (!active) return;
    active = false;
    window.removeEventListener("pointermove", onMove, true);
    window.removeEventListener("pointerup", onUp, true);
    window.removeEventListener("pointercancel", onUp, true);
    modal.style.transition = "";
    modal.style.transform = "";
    mask.style.transition = "";
    mask.style.opacity = "";
    const tap = fromHandle && moved < 8 && performance.now() - startT < 320;
    if (dy > 90 || vy > 0.55 || tap) closeModal(mask);
  };
  modal.addEventListener("pointerdown", (e) => {
    if (e.button) return;
    if (!e.target.closest(".sheet-handle, h3, .sub")) return;
    active = true; dy = 0; moved = 0;
    startY = lastY = e.clientY; startX = e.clientX; startT = lastT = performance.now(); vy = 0;
    fromHandle = !!e.target.closest(".sheet-handle");
    modal.style.transition = "none";
    mask.style.transition = "none";
    window.addEventListener("pointermove", onMove, true);
    window.addEventListener("pointerup", onUp, true);
    window.addEventListener("pointercancel", onUp, true);
  });
}

function openModal(title, bodyHtml, onMount) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.setAttribute("role", "dialog");
  mask.setAttribute("aria-modal", "true");
  mask.setAttribute("aria-label", title || "对话框");
  mask._lastFocus = document.activeElement;
  mask.innerHTML = `<div class="modal"><h3>${escapeHtml(title)}</h3><div class="modal-body stagger">${bodyHtml}</div></div>`;
  mask.addEventListener("click", (event) => { if (event.target === mask) closeModal(mask); });
  mask.addEventListener("keydown", (event) => trapFocus(mask, event));
  document.body.appendChild(mask);
  if (onMount) onMount(mask.querySelector(".modal-body"), mask);
  initSegments(mask);
  focusFirst(mask);
  syncScrollLock();
  return mask;
}

function openSheet(title, sub, bodyHtml, onMount) {
  const mask = document.createElement("div");
  mask.className = "modal-mask sheet";
  mask.setAttribute("role", "dialog");
  mask.setAttribute("aria-modal", "true");
  mask.setAttribute("aria-label", title || "操作面板");
  mask._lastFocus = document.activeElement;
  const backIcon = '<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 6l-6 6 6 6"/></svg>';
  mask.innerHTML = `<div class="modal">
    <div class="sheet-head">
      <div class="sheet-handle"></div>
      <button class="icon-btn sheet-back" id="sheetBack" aria-label="返回" data-skip-focus>${backIcon}</button>
      ${title ? `<h3>${escapeHtml(title)}</h3>` : ""}
      ${sub ? `<div class="sub">${escapeHtml(sub)}</div>` : ""}
    </div>
    <div class="modal-body stagger">${bodyHtml}</div>
    <div class="btnrow"><button class="btn ghost wide" id="sheetCancel">取消</button></div>
  </div>`;
  mask.addEventListener("click", (event) => { if (event.target === mask) closeModal(mask); });
  mask.addEventListener("keydown", (event) => trapFocus(mask, event));
  document.body.appendChild(mask);
  mask.querySelector("#sheetCancel").onclick = () => closeModal(mask);
  mask.querySelector("#sheetBack").onclick = () => closeModal(mask);
  enableSheetDrag(mask);
  const modal = mask.querySelector(".modal");
  if (modal) modal.addEventListener("animationend", () => { modal.style.willChange = "auto"; }, { once: true });
  if (onMount) onMount(mask.querySelector(".modal-body"), mask);
  initSegments(mask);
  focusFirst(mask);
  syncScrollLock();
  return mask;
}
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape") return;
  const masks = document.querySelectorAll(".modal-mask");
  if (masks.length) closeModal(masks[masks.length - 1]);
});

/* ------------------------------------------------------------ progress -- */
function switchRow(label, desc, checked, attrs = "") {
  return `<div class="setting-row">
    <div><div class="label">${escapeHtml(label)}</div>${desc ? `<div class="desc">${escapeHtml(desc)}</div>` : ""}</div>
    <div class="spacer"></div>
    <label class="switch"><input type="checkbox" ${checked ? "checked" : ""} ${attrs} /><span class="track"></span></label>
  </div>`;
}
function progressHtml(text) {
  return `<div class="progress-label"><span class="progress-step">${escapeHtml(text || "准备中")}</span><span class="progress-pct">0%</span></div>
    <div class="progress"><div class="bar" style="width:0%"></div></div>`;
}
function setProgress(step, percent) {
  // 不依赖固定 id，避免同页面并发挂载多个进度条时互相覆盖。
  const labels = document.querySelectorAll(".progress-label");
  const label = labels[labels.length - 1];
  if (label) {
    const stepNode = label.querySelector(".progress-step");
    const pctNode = label.querySelector(".progress-pct");
    if (stepNode && step) stepNode.textContent = step;
    if (pctNode) pctNode.textContent = Math.round(percent) + "%";
  }
  const bars = document.querySelectorAll(".progress .bar");
  const bar = bars[bars.length - 1];
  if (bar) bar.style.width = Math.round(percent) + "%";
}
async function pollTask(taskId, onUpdate, interval = 800, timeoutMs = 600000) {
  const deadline = performance.now() + timeoutMs;
  for (;;) {
    const task = await api(`/api/tasks/${taskId}`);
    if (onUpdate) onUpdate(task);
    if (task.status === "done" || task.status === "error") return task;
    if (performance.now() > deadline) throw new Error("任务处理超时，请稍后重试");
    await new Promise((resolve) => setTimeout(resolve, interval));
  }
}
function skeletonCards(count = 3) {
  return Array.from({ length: count }, (_, i) =>
    `<div class="glass skeleton card" style="height:168px;animation-delay:${i * 60}ms"></div>`).join("");
}

function prefersReducedMotion() {
  return typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function countUp(el, to, opts = {}) {
  if (!el) return;
  const target = Number(to) || 0;
  const duration = opts.duration || 780;
  const decimals = opts.decimals || 0;
  const render = (value) => {
    el.textContent = Number(value).toLocaleString("zh-CN", {
      minimumFractionDigits: decimals, maximumFractionDigits: decimals,
    });
  };
  if (prefersReducedMotion() || duration <= 0 || typeof requestAnimationFrame !== "function") {
    render(target);
    return;
  }
  const start = performance.now();
  const ease = (t) => 1 - Math.pow(1 - t, 3);
  const frame = (now) => {
    const progress = Math.min(1, (now - start) / duration);
    if (progress < 1) { render(target * ease(progress)); requestAnimationFrame(frame); }
    else render(target);
  };
  requestAnimationFrame(frame);
}

let _ringSeq = 0;
function creditRing(ratio, opts = {}) {
  const size = opts.size || 96;
  const stroke = opts.stroke || 9;
  const radius = (size - stroke) / 2;
  const circumference = 2 * Math.PI * radius;
  const value = Math.max(0, Math.min(1, Number(ratio) || 0));
  const dash = circumference * value;
  const gradientId = "creditGrad" + (_ringSeq++);
  const tone = opts.tone ? " ring-" + opts.tone : "";
  return `<div class="credit-ring${tone}" style="width:${size}px;height:${size}px;--circ:${circumference.toFixed(2)}">
    <svg viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">
      <defs><linearGradient id="${gradientId}" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="#7c5cff"/><stop offset="1" stop-color="#e879f9"/>
      </linearGradient></defs>
      <circle class="ring-track" cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none" stroke="rgba(120,120,150,.16)" stroke-width="${stroke}"/>
      <circle class="ring-arc" cx="${size / 2}" cy="${size / 2}" r="${radius}" fill="none" stroke="url(#${gradientId})" stroke-width="${stroke}" stroke-linecap="round"
        stroke-dasharray="${dash.toFixed(2)} ${(circumference - dash).toFixed(2)}" transform="rotate(-90 ${size / 2} ${size / 2})"/>
    </svg>
    ${(opts.label != null && opts.label !== "") || (opts.sub != null && opts.sub !== "")
      ? `<div class="credit-ring-label"><b>${escapeHtml(String(opts.label ?? ""))}</b><span>${escapeHtml(String(opts.sub ?? ""))}</span></div>`
      : ""}
  </div>`;
}

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}

window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason;
  const message = (reason && reason.message) || "";
  if (message === "未登录" || /abort/i.test((reason && reason.name) || "")) return;
  if (typeof toast === "function") toast(message || "操作失败，请重试");
  event.preventDefault();
});
