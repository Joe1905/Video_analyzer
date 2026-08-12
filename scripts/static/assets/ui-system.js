(() => {
  "use strict";

  const NAV_KEY = "ui-nav-expanded";
  const GLOBAL_USER_PICKER_KEY = "ui-global-user-picker-completed";
  const DESKTOP_QUERY = window.matchMedia("(min-width: 861px)");
  const globalUserPromise = window.VideoAnalyzerGlobalUser || Promise.resolve({
    currentUser: { id: "public", name: "公共账户", kind: "public" }, users: [],
  });

  function userInitial(user) {
    return String(user?.name || "公").trim().slice(0, 1) || "公";
  }

  function setIdentityAvatar(avatar, user) {
    if (!avatar) return;
    const image = avatar.querySelector(".ui-nav__identity-avatar-image");
    const fallback = avatar.querySelector(".ui-nav__identity-avatar-fallback");
    if (!image || !fallback) {
      avatar.textContent = userInitial(user);
      return;
    }
    fallback.textContent = userInitial(user);
    image.hidden = true;
    image.removeAttribute("src");
    if (!user?.avatarUrl) return;
    image.onload = () => { image.hidden = false; fallback.hidden = true; };
    image.onerror = () => {
      image.hidden = true;
      fallback.hidden = false;
      image.removeAttribute("src");
    };
    fallback.hidden = false;
    image.src = user.avatarUrl;
  }

  function applyCurrentUserAvatar(root = document) {
    const current = window.VideoAnalyzerCurrentUser;
    if (!current || !root) return;
    if (root.matches?.("[data-current-user-avatar]")) setIdentityAvatar(root, current);
    root.querySelectorAll?.("[data-current-user-avatar]").forEach((avatar) => {
      setIdentityAvatar(avatar, current);
    });
  }

  window.VideoAnalyzerApplyCurrentUserAvatar = applyCurrentUserAvatar;

  function renderGlobalUser(payload) {
    const current = payload.currentUser || { id: "public", name: "公共账户", kind: "public" };
    window.VideoAnalyzerCurrentUser = current;
    document.querySelectorAll(".ui-nav__identity").forEach((button) => {
      setIdentityAvatar(button.querySelector(".ui-nav__identity-avatar"), current);
      button.querySelector(".ui-nav__identity-copy b").textContent = current.name;
      button.querySelector(".ui-nav__identity-copy small").textContent = current.kind === "feishu" ? "飞书账户" : "公共账户";
    });
    applyCurrentUserAvatar();
    const options = document.querySelector("[data-global-user-options]");
    if (!options) return;
    options.innerHTML = (payload.users || []).map((user) => {
      const avatar = user.avatarUrl
        ? `<img src="${String(user.avatarUrl).replace(/&/g, "&amp;").replace(/\"/g, "&quot;")}" alt="">`
        : userInitial(user);
      return `<button type="button" class="ui-global-user-option ${user.kind === "public" ? "is-public" : ""} ${user.id === current.id ? "is-current" : ""}" data-global-user-id="${String(user.id).replace(/\"/g, "&quot;")}"><span class="avatar">${avatar}</span><span class="copy"><b>${String(user.name || "公共账户")}</b><small>${user.kind === "feishu" ? "飞书账户" : "公共账户"}</small></span><span class="state" aria-hidden="true">${user.id === current.id ? "当前" : ""}</span></button>`;
    }).join("") || '<div class="ui-global-user-loading">暂无可用身份</div>';
    if (!options.closest(".ui-global-user-modal")?.hidden) revealGlobalUserOptions(options);
    options.querySelectorAll("[data-global-user-id]").forEach((button) => {
      button.addEventListener("click", async () => {
        const response = await fetch("/api/global-user/select", {
          method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: button.dataset.globalUserId }),
        });
        if (response.ok) {
          rememberGlobalUserPickerChoice();
          location.reload();
        }
      });
    });
  }

  function revealGlobalUserOptions(options) {
    if (!options?.querySelector(".ui-global-user-option")) return;
    options.classList.remove("is-revealing");
    window.requestAnimationFrame(() => {
      if (!options.closest(".ui-global-user-modal")?.hidden) options.classList.add("is-revealing");
    });
  }

  function globalUserPickerChoiceSaved() {
    try {
      return localStorage.getItem(GLOBAL_USER_PICKER_KEY) === "1";
    } catch (_) {
      return false;
    }
  }

  function rememberGlobalUserPickerChoice() {
    try {
      localStorage.setItem(GLOBAL_USER_PICKER_KEY, "1");
    } catch (_) {
      // Storage may be disabled; the picker will then appear again next time.
    }
  }

  function setupGlobalUser() {
    const modal = document.getElementById("ui-global-user-modal");
    if (!modal) return;
    const close = () => { modal.hidden = true; };
    const open = () => {
      modal.hidden = false;
      revealGlobalUserOptions(modal.querySelector("[data-global-user-options]"));
    };
    document.querySelectorAll("[data-global-user-trigger]").forEach((button) => button.addEventListener("click", open));
    modal.querySelectorAll("[data-global-user-close]").forEach((button) => button.addEventListener("click", close));
    globalUserPromise.then((payload) => {
      renderGlobalUser(payload);
      if (!globalUserPickerChoiceSaved()) open();
    });
  }

  function navCookieExpanded() {
    return document.cookie.split("; ").some((item) => item === `${NAV_KEY}=1`);
  }

  function navExpanded() {
    try {
      const stored = localStorage.getItem(NAV_KEY);
      return stored === null ? navCookieExpanded() : stored === "1";
    } catch (_) {
      return navCookieExpanded();
    }
  }

  function syncNavigation(expanded = navExpanded()) {
    const desktopExpanded = DESKTOP_QUERY.matches && expanded;
    const state = desktopExpanded ? "expanded" : "collapsed";
    document.documentElement.dataset.nav = state;
    document.body.dataset.nav = state;
    document.querySelectorAll(".ui-nav").forEach((nav) => {
      nav.classList.toggle("is-expanded", desktopExpanded);
      const toggle = nav.querySelector(".ui-nav__toggle");
      if (!toggle) return;
      toggle.setAttribute("aria-expanded", String(desktopExpanded));
      toggle.setAttribute("aria-label", desktopExpanded ? "收起导航" : "展开导航");
      toggle.title = desktopExpanded ? "收起导航" : "展开导航";
    });
  }

  function setMobileNavigation(open, restoreFocus = false) {
    const mobileOpen = !DESKTOP_QUERY.matches && Boolean(open);
    const state = mobileOpen ? "open" : "closed";
    document.documentElement.dataset.mobileNav = state;
    document.body.dataset.mobileNav = state;
    document.querySelectorAll(".ui-mobile-nav-trigger").forEach((trigger) => {
      trigger.setAttribute("aria-expanded", String(mobileOpen));
      trigger.setAttribute("aria-label", mobileOpen ? "关闭导航" : "打开导航");
      trigger.title = mobileOpen ? "关闭导航" : "打开导航";
      if (!mobileOpen && restoreFocus) trigger.focus();
    });
    if (mobileOpen) {
      requestAnimationFrame(() => {
        document.querySelector(".ui-nav__mobile-close")?.focus();
      });
    }
  }

  function enhanceTables(root = document) {
    root.querySelectorAll("table").forEach((table) => {
      const labels = [...table.querySelectorAll("thead th")].map((cell) => cell.textContent.trim());
      if (!labels.length) return;
      table.querySelectorAll("tbody tr").forEach((row) => {
        [...row.children].forEach((cell, index) => {
          if (!cell.dataset.label && labels[index]) cell.dataset.label = labels[index];
        });
      });
    });
  }

  function enhanceDialogs(root = document) {
    root.querySelectorAll(".modal, .modal-overlay, .modal-backdrop, .login-backdrop").forEach((modal) => {
      if (!modal.hasAttribute("role")) modal.setAttribute("role", "dialog");
      if (!modal.hasAttribute("aria-modal")) modal.setAttribute("aria-modal", "true");
      const open = modal.classList.contains("open") || modal.classList.contains("show");
      modal.setAttribute("aria-hidden", String(!open));
    });
  }

  function enhanceMessageActions(root = document) {
    const icons = {
      "一键复制": '<rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
      "回复": '<path d="m9 17-5-5 5-5"/><path d="M4 12h9a7 7 0 0 1 7 7"/>',
      "导出 PDF": '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h5"/><path d="M10 16h4M12 11v5m-2-2 2 2 2-2"/>',
    };
    root.querySelectorAll(".msg-actions button").forEach((button) => {
      if (button.classList.contains("msg-icon-action") && button.querySelector("svg")) return;
      const label = button.getAttribute("aria-label") || button.textContent.trim();
      if (!icons[label]) return;
      button.classList.add("msg-icon-action");
      button.setAttribute("aria-label", label);
      button.dataset.tooltip = label;
      button.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${icons[label]}</svg>`;
    });
  }

  function previewState() {
    const state = new URLSearchParams(location.search).get("ui_state");
    const allowed = new Set(["default", "loading", "empty", "success", "error"]);
    if (!state || !allowed.has(state)) return;
    document.body.dataset.uiState = state;
    if (state === "default") return;
    const main = document.querySelector(".ui-main");
    if (!main || main.querySelector(".ui-preview-state")) return;
    if (state === "success") {
      const banner = document.createElement("div");
      banner.className = "ui-preview-success";
      banner.setAttribute("role", "status");
      banner.innerHTML = '<span aria-hidden="true">✓</span><strong>测试数据已加载</strong>';
      main.appendChild(banner);
      return;
    }
    const copy = {
      loading: ["正在加载", "测试环境正在准备稳定的页面数据。"],
      empty: ["暂无内容", "当前测试状态没有可展示的数据。"],
      error: ["暂时无法加载", "这是用于界面验收的错误状态，不会发起真实业务操作。"],
    }[state];
    const panel = document.createElement("section");
    panel.className = `ui-preview-state ui-state-${state}`;
    panel.setAttribute("role", state === "error" ? "alert" : "status");
    panel.innerHTML = state === "loading"
      ? `<span class="ui-skeleton-mark" aria-hidden="true"></span><strong>${copy[0]}</strong><p>${copy[1]}</p>`
      : `<span class="ui-state-mark" aria-hidden="true"></span><strong>${copy[0]}</strong><p>${copy[1]}</p>`;
    main.appendChild(panel);
  }

  function bootstrap() {
    syncNavigation();
    setMobileNavigation(false);
    enhanceTables();
    enhanceDialogs();
    enhanceMessageActions();
    previewState();
    setupGlobalUser();

    document.addEventListener("click", (event) => {
      const mobileTrigger = event.target.closest(".ui-mobile-nav-trigger");
      if (mobileTrigger) {
        setMobileNavigation(true);
        return;
      }

      const mobileClose = event.target.closest(".ui-nav__mobile-close, .ui-nav__backdrop");
      if (mobileClose) {
        setMobileNavigation(false, true);
        return;
      }

      const mobileNavItem = event.target.closest(".ui-nav__item");
      if (mobileNavItem && !DESKTOP_QUERY.matches) {
        setMobileNavigation(false);
      }

      const navToggle = event.target.closest(".ui-nav__toggle");
      if (navToggle) {
        const expanded = document.body.dataset.nav !== "expanded";
        try {
          localStorage.setItem(NAV_KEY, expanded ? "1" : "0");
        } catch (_) {}
        document.cookie = `${NAV_KEY}=${expanded ? "1" : "0"}; Path=/; Max-Age=31536000; SameSite=Lax`;
        syncNavigation(expanded);
        return;
      }

    });

    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && document.body.dataset.mobileNav === "open") {
        setMobileNavigation(false, true);
      }
    });

    DESKTOP_QUERY.addEventListener("change", () => {
      setMobileNavigation(false);
      syncNavigation();
    });

    const observer = new MutationObserver((records) => {
      let tableChanged = false;
      let dialogChanged = false;
      let messagesChanged = false;
      records.forEach((record) => {
        if (record.type === "childList") {
          tableChanged = true;
          messagesChanged = true;
        }
        if (record.type === "attributes") {
          dialogChanged = true;
        }
      });
      if (tableChanged) enhanceTables();
      if (dialogChanged) enhanceDialogs();
      if (messagesChanged) enhanceMessageActions();
    });
    observer.observe(document.body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class"],
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap, { once: true });
  } else {
    bootstrap();
  }
})();
