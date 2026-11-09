{{- /* The boot page's nginx config and HTML (templates/memex-portal/boot-page.yaml). Defined here
because a template definition cannot sit inside that file's conditional, and named so the pod's
checksum covers exactly what is mounted. */ -}}
{{- define "memex.bootPage.conf" -}}
server {
  listen 8080 default_server;
  server_tokens off;
  # The portal Ingress admits bodies up to its own proxy-body-size; this backend must not answer
  # 413 for one of them, or an upload during a restart gets a final error instead of a retryable 503.
  client_max_body_size 0;
  # Resolved per request, never at start: a DNS hiccup must not keep the page from starting. The
  # image's entrypoint fills NGINX_LOCAL_RESOLVERS from /etc/resolv.conf (this file is a template).
  resolver ${NGINX_LOCAL_RESOLVERS} valid=10s ipv6=off;
  location = /boot.html { internal; root /usr/share/nginx/html; add_header Cache-Control "no-store" always; add_header Retry-After "5" always; }
  # The booting portal's own progress report (one source for this page and memex-local).
  location = /_boot/progress {
    set $portal http://memex-portal-boot.{{ .Release.Namespace }}.svc.cluster.local:8080;
    proxy_pass $portal;
    proxy_connect_timeout 2s;
    proxy_send_timeout 5s;
    proxy_read_timeout 5s;
    add_header Cache-Control "no-store" always;
    error_page 502 503 504 = @unreachable;
  }
  # No portal process answers yet (image pull, init containers, the database, a crash): say so.
  location @unreachable {
    default_type application/json;
    add_header Cache-Control "no-store" always;
    add_header Retry-After "5" always;
    return 503 '{"schema":1,"reachable":false}';
  }
  location / { error_page 503 /boot.html; return 503; }
}
{{- end }}
{{- define "memex.bootPage.html" -}}
<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<noscript><meta http-equiv="refresh" content="5"></noscript>
<title>{{ .Values.ingress.bootPage.title | default "Memex is starting" }}</title>
<style>
  :root { color-scheme: light dark; --bg:#f6f7f9; --fg:#1b1f24; --muted:#5b6470; --accent:#3b82f6; --line:#dfe3e8; --ok:#16a34a; --bad:#dc2626; --warn:#b45309; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0b0d10; --fg:#e8eaed; --muted:#9aa3ad; --accent:#7cc4fa; --line:#262b31; --ok:#4ade80; --bad:#f87171; --warn:#fbbf24; } }
  html,body { min-height:100%; margin:0; background:var(--bg); color:var(--fg); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  main { max-width:760px; margin:0 auto; padding:40px 16px; display:flex; flex-direction:column; gap:18px; }
  header { display:flex; align-items:center; gap:14px; }
  .spin { flex:none; width:28px; height:28px; border-radius:50%; border:3px solid color-mix(in srgb, var(--accent) 22%, transparent); border-top-color:var(--accent); animation:s 0.9s linear infinite; }
  @keyframes s { to { transform:rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration:3s; } }
  h1 { margin:0; font-size:21px; font-weight:600; }
  p { margin:0; color:var(--muted); }
  .bar { height:8px; border-radius:4px; background:var(--line); overflow:hidden; }
  .bar > i { display:block; height:100%; width:0; background:var(--accent); transition:width .6s ease; }
  .bar.indet > i { width:30%; animation:slide 1.4s ease-in-out infinite; }
  @keyframes slide { 0% { margin-left:-30%; } 100% { margin-left:100%; } }
  .meta { display:flex; justify-content:space-between; gap:12px; font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; }
  ol { list-style:none; margin:0; padding:0; border-top:1px solid var(--line); }
  ol li { display:grid; grid-template-columns:20px 1fr auto; gap:10px; padding:7px 0; border-bottom:1px solid var(--line); align-items:baseline; }
  .st { text-align:center; } .st.done { color:var(--ok); } .st.failed { color:var(--bad); } .st.running { color:var(--accent); } .st.pending, .st.skipped { color:var(--muted); }
  .d { font-size:13px; color:var(--muted); }
  .n { font-size:13px; color:var(--muted); font-variant-numeric:tabular-nums; white-space:nowrap; }
  h2 { font-size:14px; margin:6px 0 0; font-weight:600; }
  .mods { width:100%; border-collapse:collapse; font-size:13px; font-variant-numeric:tabular-nums; }
  .mods td { padding:3px 6px 3px 0; border-bottom:1px solid var(--line); overflow-wrap:anywhere; }
  .mods td.r { text-align:right; white-space:nowrap; color:var(--muted); }
  .mods tr.running td:first-child { color:var(--accent); font-weight:600; }
  .mods tr.failed td { color:var(--bad); }
  .bad { color:var(--bad); } .warn { color:var(--warn); }
  ul { margin:0; padding-left:18px; font-size:13px; }
  [hidden] { display:none !important; }
</style></head>
<body><main role="status" aria-live="polite">
  <header><div class="spin" aria-hidden="true"></div><div>
    <h1>{{ .Values.ingress.bootPage.title | default "Memex is starting" }}</h1>
    <p id="headline">It is starting up, or installing the apps you picked. A first start can take a few minutes. This page reloads by itself.</p>
  </div></header>
  <div id="progress" hidden>
    <div class="bar" id="bar"><i></i></div>
    <div class="meta"><span id="count"></span><span id="eta"></span></div>
  </div>
  <ol id="phases"></ol>
  <section id="checks" hidden><h2>Waiting for</h2><ul></ul></section>
  <section id="errors" hidden><h2 class="bad">Errors</h2><ul class="bad"></ul></section>
  <section id="modules" hidden><h2>Modules</h2><table class="mods"><tbody></tbody></table></section>
  <p id="previous" class="d" hidden></p>
</main>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var el = function (tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  var sawReady = false;
  var mark = { done: "✓", failed: "✗", running: "●", pending: "○", skipped: "–" };
  function dur(s) { if (s == null) return ""; s = Math.round(s); return s < 60 ? s + " s" : Math.floor(s / 60) + " min " + (s % 60) + " s"; }
  function size(b) { if (b == null) return ""; return b >= 1048576 ? (b / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB"; }
  function fill(list, items, make) { list.replaceChildren.apply(list, items.map(make)); }
  function render(r) {
    if (r.reachable === false) {
      $("headline").textContent = "The portal process is not answering yet — it is starting, or waiting for its database. This page updates by itself.";
      $("progress").hidden = true;
      return;
    }
    // Ready twice in a row before reloading: the ingress keeps serving this page until the readiness
    // probe passes and the endpoints propagate, and a reload on the first reading would loop.
    if (r.ready) { if (sawReady) { location.reload(); } sawReady = true; return; }
    sawReady = false;
    $("headline").textContent = r.headline + " — up " + dur(r.uptimeSeconds);
    var running = (r.phases || []).filter(function (p) { return p.state === "running"; }).pop();
    var counted = running && running.total != null;
    $("progress").hidden = false;
    $("bar").className = counted ? "bar" : "bar indet";
    $("bar").firstChild.style.width = counted && running.total ? Math.round(100 * (running.done || 0) / running.total) + "%" : "";
    $("count").textContent = counted ? (running.done || 0) + " of " + running.total + (running.failed ? " · " + running.failed + " failed" : "") + (running.concurrency > 1 ? " · " + running.concurrency + " at once" : "") : "";
    $("eta").textContent = r.etaSeconds != null ? "about " + dur(r.etaSeconds) + " left" : (counted ? "measuring the rate…" : "");
    fill($("phases"), r.phases || [], function (p) {
      var li = el("li");
      li.appendChild(el("span", "st " + p.state, mark[p.state] || "·"));
      var t = el("div", null, p.title); if (p.detail) t.appendChild(el("div", "d" + (p.state === "failed" ? " bad" : ""), p.detail)); li.appendChild(t);
      li.appendChild(el("span", "n", (p.total != null ? (p.done || 0) + "/" + p.total + "  " : "") + dur(p.seconds)));
      return li;
    });
    var checks = r.checks || [];
    $("checks").hidden = !checks.length;
    fill($("checks").querySelector("ul"), checks, function (c) { return el("li", c.status === "Unhealthy" ? "warn" : null, c.name + (c.description ? " — " + c.description : "")); });
    var errors = r.errors || [];
    $("errors").hidden = !errors.length;
    fill($("errors").querySelector("ul"), errors, function (e) { return el("li", null, e); });
    var mods = r.modules || [];
    $("modules").hidden = !mods.length;
    fill($("modules").querySelector("tbody"), mods, function (m) {
      var tr = el("tr", m.state);
      tr.appendChild(el("td", null, m.package));
      tr.appendChild(el("td", "r", m.version || ""));
      tr.appendChild(el("td", "r", size(m.bytes)));
      tr.appendChild(el("td", "r", m.downloadSeconds != null ? dur(m.downloadSeconds) : ""));
      tr.appendChild(el("td", "r", m.error ? m.state + ": " + m.error : m.state));
      return tr;
    });
    var prev = r.previousBoot;
    $("previous").hidden = !prev;
    if (prev) $("previous").textContent = "Before this start: " + prev.headline + " (" + (prev.phases || []).filter(function (p) { return p.seconds != null; }).map(function (p) { return p.title + " " + dur(p.seconds); }).join(", ") + ").";
  }
  function poll() {
    fetch("/_boot/progress", { cache: "no-store" })
      .then(function (res) { return res.json(); })
      .then(render)
      .catch(function () { /* keep the last reading; the next poll says more */ })
      .then(function () { setTimeout(poll, 2000); });
  }
  poll();
})();
</script>
</body></html>
{{- end }}
