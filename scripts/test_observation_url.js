const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(__dirname + "/static/assets/observation-url.js", "utf8");
const original = "http://192.168.1.254:6082/vnc.html?autoconnect=1&resize=scale";
function resolve(origin) {
  const context = {window: {location: new URL(origin)}, URL, URLSearchParams};
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.observationUrl(original);
}
for (const origin of ["http://192.168.1.254:4002", "http://192.168.1.254:4003", "http://10.0.0.2", "http://172.16.0.2", "http://localhost", "http://[::1]"]) {
  assert.equal(resolve(origin), original);
}
for (const origin of ["https://apps.ruitech.hk", "https://apps.example", "http://172.32.0.1"]) {
  const result = new URL(resolve(origin));
  assert.equal(result.origin, origin);
  assert.equal(result.pathname, "/proxy/observation/6082/vnc.html");
  // Installed noVNC resolves path against location.href when host is unset.
  const websocket = new URL(result.searchParams.get("path"), result.href);
  websocket.protocol = result.protocol === "https:" ? "wss:" : "ws:";
  assert.equal(websocket.pathname, "/proxy/observation/6082/websockify");
  assert.equal(websocket.host, result.host);
  assert.equal(websocket.protocol, result.protocol === "https:" ? "wss:" : "ws:");
}
console.log("Observation URL routing passed");
