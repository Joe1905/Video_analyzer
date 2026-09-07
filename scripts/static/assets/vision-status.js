// Refresh on user activity and existing queue updates, without a timer.
(() => {
  const toggle = document.getElementById("directMode");
  if (!toggle) return;
  const label = toggle.closest("label");
  if (label.lastChild.nodeType === Node.TEXT_NODE) label.lastChild.textContent = "视频直连分析（Qwen）";
  const notice = document.createElement("div");
  notice.id = "visionStatus";
  notice.setAttribute("role", "status");
  notice.style.cssText = "font-size:12px;line-height:1.6;color:var(--text2);overflow-wrap:anywhere";
  label.after(notice);
  toggle.disabled = true;
  notice.textContent = "正在检查视觉分析线路…";
  let pending;
  async function refreshVisionStatus() {
    if (pending) return pending;
    pending = (async () => {
      try {
        const response = await fetch("/api/vision-status", {cache: "no-store"});
        if (!response.ok) throw new Error("线路状态读取失败，请刷新重试");
        const state = await response.json();
        toggle.disabled = !state.direct_video_enabled;
        toggle.title = state.direct_video_enabled ? "直接发送视频至 Qwen" : state.message;
        if (!state.direct_video_enabled && toggle.checked) {
          toggle.checked = false;
          toggle.dispatchEvent(new Event("change"));
        }
        window.DEFAULT_ANALYSIS_MODE = "analyzer";
        notice.textContent = state.message + "。下次检查：" +
          new Date(state.next_check_at * 1000).toLocaleString("zh-CN") + "（到期后访问时检查）";
      } catch (error) {
        toggle.disabled = true;
        notice.textContent = error.message + "；视频直连暂不可选";
      }
    })().finally(() => { pending = null; });
    return pending;
  }
  const refreshFiles = refresh;
  refresh = async function () {
    await Promise.all([refreshFiles(), refreshVisionStatus()]);
  };
  const submit = submitAnalysis;
  submitAnalysis = async function (...args) {
    try { return await submit(...args); }
    finally { await refreshVisionStatus(); }
  };
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refreshVisionStatus();
  });
  refreshVisionStatus();
})();
