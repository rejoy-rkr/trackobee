/* Trackobee embed. Add to CRM pages for logged-in users only:
   <script src="https://YOUR-SITE/embed.js" data-token="SIGNED_TOKEN" async></script>
   It detects the OS, offers the agent installer if the agent is missing, and links the website user to the PC. */
(function () {
  var s = document.currentScript, tok = s && s.getAttribute("data-token");
  if (!s || !tok) return;
  var base = new URL(".", s.src).href.replace(/\/$/, ""), LOCAL = "http://127.0.0.1:47821", banner = null, paired = false, cfg = null;
  function detectOS() {
    var ua = navigator.userAgent, p = (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || "";
    if (/android|iphone|ipad/i.test(ua)) return null;
    if (/win/i.test(p + ua)) return "windows";
    if (/mac/i.test(p)) return "mac";
    if (/linux|x11|cros/i.test(p + ua)) return "linux";
    return null;
  }
  var OSN = { windows: "Windows", mac: "macOS", linux: "Linux" };
  function agent(path, body) {
    var o = { mode: "cors" };
    if (body) { o.method = "POST"; o.headers = { "Content-Type": "application/json" }; o.body = JSON.stringify(body); }
    return fetch(LOCAL + path, o).then(function (r) { if (!r.ok) throw 0; return r.json(); });
  }
  function el(tag, css, text) { var e = document.createElement(tag); e.style.cssText = css || ""; if (text) e.textContent = text; return e; }
  function loadCfg() {
    if (cfg) return Promise.resolve(cfg);
    return fetch(base + "/config.json").then(function (r) { return r.json(); }).then(function (c) { cfg = c; return c; });
  }
  // builds the installer from this organization's settings, entirely in the browser
  function install(os) {
    return loadCfg().then(function (c) {
      return fetch(c.supabaseUrl + "/rest/v1/rpc/tb_public_installer_info", {
        method: "POST", headers: { "Content-Type": "application/json", apikey: c.anonKey, Authorization: "Bearer " + c.anonKey }, body: JSON.stringify({ p_token: tok })
      }).then(function (r) { return r.json(); }).then(function (info) {
        if (!info || !info.ok) throw new Error("This link has expired. Reload the page.");
        var files = { windows: "trackobee-setup.exe", mac: "trackobee-agent-macos", linux: "trackobee-agent-linux" };
        return fetch(base + "/installers/" + files[os], { method: "HEAD" }).then(function (r) { return r.ok; }, function () { return false; }).then(function (has) {
          var pk = {}; pk[os] = has;
          return import(base + "/js/installers.js").then(function (m) {
            var r = m.makeInstaller(os, { siteUrl: (c.siteUrl || base), ingestUrl: c.supabaseUrl + "/rest/v1/rpc/tb_ingest_heartbeat", anonKey: c.anonKey, agentToken: info.agent_token, allowedOrigins: info.allowed_origins }, pk);
            m.saveFile(r.name, r.body);
          });
        });
      });
    });
  }
  function show(kind, os) {
    if (banner) banner.remove();
    var b = "position:fixed;z-index:2147483647;font:13px system-ui,sans-serif;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.25);";
    if (kind === "ok") {
      banner = el("div", b + "right:12px;bottom:12px;background:#2e7d32;color:#fff;padding:6px 12px;", "● Trackobee connected");
    } else {
      banner = el("div", b + "left:50%;transform:translateX(-50%);bottom:16px;background:#1f2937;color:#fff;padding:12px 16px;max-width:92vw;display:flex;gap:12px;align-items:center;flex-wrap:wrap;");
      banner.appendChild(el("span", "", "Install the Trackobee agent for " + OSN[os] + " to record your work time on this computer."));
      var a = el("button", "background:#f5b301;color:#111;padding:6px 12px;border-radius:6px;border:0;cursor:pointer;font-weight:600;", "Install for " + OSN[os]);
      a.onclick = function () { a.disabled = true; install(os).then(function () { setTimeout(poll, 3000); }).catch(function (e) { alert(e.message || "Could not prepare the installer"); }).then(function () { a.disabled = false; }); };
      var x = el("button", "background:none;border:0;color:#cbd5e1;cursor:pointer;font-size:16px;", "✕");
      x.onclick = function () { banner.remove(); try { sessionStorage.tbHide = "1"; } catch (e) {} };
      banner.append(a, x);
    }
    document.body.appendChild(banner);
  }
  function check() {
    var os = detectOS(); if (!os) return;
    agent("/status").then(function () { return agent("/pair", { token: tok }); })
      .then(function () { paired = true; show("ok"); })
      .catch(function () {
        paired = false; var hidden = false; try { hidden = sessionStorage.tbHide === "1"; } catch (e) {}
        if (!hidden) show("install", os);
      });
  }
  var tries = 0;
  function poll() { if (paired || tries++ > 60) return; check(); setTimeout(poll, 5000); }
  window.Trackobee = { unpair: function () { return agent("/unpair", {}).catch(function () {}); } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", check); else check();
  setInterval(check, 10 * 60 * 1000);
})();
