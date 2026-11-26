/* Trackobee embed. Add to the CRM page (logged-in users only):
   <script src="https://TRACKOBEE_HOST/embed.js" data-token="SIGNED_TOKEN" async></script>  */
(function () {
  var s = document.currentScript, tok = s && s.getAttribute("data-token");
  if (!s || !tok) return;
  var srv = new URL(s.src).origin, LOCAL = "http://127.0.0.1:47821", banner = null, paired = false;
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
  function show(kind, os) {
    if (banner) banner.remove();
    var base = "position:fixed;z-index:2147483647;font:13px system-ui,sans-serif;border-radius:8px;box-shadow:0 2px 10px rgba(0,0,0,.25);";
    if (kind === "ok") {
      banner = el("div", base + "right:12px;bottom:12px;background:#2e7d32;color:#fff;padding:6px 12px;", "● Trackobee connected");
    } else {
      banner = el("div", base + "left:50%;transform:translateX(-50%);bottom:16px;background:#1f2937;color:#fff;padding:12px 16px;max-width:92vw;display:flex;gap:12px;align-items:center;flex-wrap:wrap;");
      banner.appendChild(el("span", "", "Install the Trackobee agent for " + OSN[os] + " to record your work time on this computer."));
      var a = el("a", "background:#f5b301;color:#111;padding:6px 12px;border-radius:6px;text-decoration:none;font-weight:600;", "Install for " + OSN[os]);
      a.href = srv + "/download/" + os + "?token=" + encodeURIComponent(tok);
      a.onclick = function () { setTimeout(poll, 3000); };
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
  setInterval(check, 10 * 60 * 1000);   // keep the pairing fresh
})();
