/* Same-origin public URLs keep both noVNC and WebSocket behind authentik. */
function observationUrl(value) {
  if (!value) return "";
  const host = window.location.hostname.toLowerCase().replace(/^\[|\]$/g, "");
  const parts = host.split(".").map(Number);
  const ipv4 = parts.length === 4 && parts.every(n => Number.isInteger(n) && n >= 0 && n <= 255);
  const local = host === "localhost" || host === "::1" || host.endsWith(".local") ||
    /^(fc|fd)[0-9a-f]{2}:|^fe[89ab][0-9a-f]:/.test(host) ||
    (ipv4 && (parts[0] === 10 || parts[0] === 127 ||
      (parts[0] === 192 && parts[1] === 168) ||
      (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)));
  if (local) return value;
  const source = new URL(value, window.location.origin);
  if (!/^\d+$/.test(source.port)) throw new Error("观测通道端口无效");
  const prefix = `/proxy/observation/${source.port}/`;
  const target = new URL(prefix + "vnc.html", window.location.origin);
  target.search = new URLSearchParams({autoconnect: "1", resize: "scale", path: prefix.slice(1) + "websockify"});
  return target.href;
}
