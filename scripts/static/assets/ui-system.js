(() => {
  "use strict";

  const NAV_KEY = "ui-nav-expanded";
  const DESKTOP_QUERY = window.matchMedia("(min-width: 861px)");

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

  function enhanceToolTree(root = document) {
    root.querySelectorAll("#toolModal .tree-children").forEach((children) => {
      if (children.firstElementChild?.classList.contains("ui-collapse__inner")) return;
      const inner = document.createElement("div");
      inner.className = "ui-collapse__inner";
      while (children.firstChild) inner.appendChild(children.firstChild);
      children.appendChild(inner);
    });
    root.querySelectorAll("#toolModal .tree-node").forEach((node) => {
      const toggle = node.querySelector(":scope > .tree-row .tree-toggle");
      if (!toggle) return;
      const expanded = !node.classList.contains("collapsed");
      const label = node.querySelector(":scope > .tree-row .tree-title")?.textContent.trim()
        || node.querySelector(":scope > .tree-row span:last-child")?.textContent.trim()
        || "工具分组";
      toggle.setAttribute("aria-expanded", String(expanded));
      toggle.setAttribute("aria-label", `${expanded ? "收起" : "展开"} ${label}`);
      toggle.title = `${expanded ? "收起" : "展开"} ${label}`;
    });
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
    enhanceToolTree();
    enhanceTables();
    enhanceDialogs();
    enhanceMessageActions();
    previewState();

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

      const treeToggle = event.target.closest("#toolModal .tree-toggle");
      if (treeToggle) {
        requestAnimationFrame(() => {
          enhanceToolTree();
        });
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
      let toolChanged = false;
      let messagesChanged = false;
      records.forEach((record) => {
        if (record.type === "childList") {
          tableChanged = true;
          toolChanged = true;
          messagesChanged = true;
        }
        if (record.type === "attributes") {
          dialogChanged = true;
          toolChanged = true;
        }
      });
      if (toolChanged) enhanceToolTree();
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
