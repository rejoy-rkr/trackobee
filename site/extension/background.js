// Trackobee Browser Sites. Reports the DOMAIN of the active tab (never the path, query or page content) to the Trackobee agent
// running on this same computer (http://127.0.0.1:47821). Private/incognito windows are reported only as "(private window)".
const AGENT = "http://127.0.0.1:47821/tab";

function domainOf(url) {
  try {
    const u = new URL(url);
    if (u.protocol === "http:" || u.protocol === "https:") return u.hostname.replace(/^www\./, "").toLowerCase().slice(0, 100);
    if (u.protocol === "file:") return "(local file)";
    return "(browser page)";
  } catch (e) { return ""; }
}

async function report() {
  let body = { active: false };
  try {
    const win = await chrome.windows.getLastFocused();
    if (win && win.focused) {
      const [tab] = await chrome.tabs.query({ active: true, windowId: win.id });
      if (tab) body = { active: true, domain: tab.incognito ? "(private window)" : domainOf(tab.url || tab.pendingUrl || "") };
    }
  } catch (e) {}
  try { await fetch(AGENT, { method: "POST", headers: { "Content-Type": "text/plain" }, body: JSON.stringify(body) }); } catch (e) {}   // agent not running: ignore
}

chrome.tabs.onActivated.addListener(report);
chrome.tabs.onUpdated.addListener((id, change) => { if (change.url || change.status === "complete") report(); });
chrome.windows.onFocusChanged.addListener(report);
chrome.runtime.onStartup.addListener(report);
chrome.runtime.onInstalled.addListener(() => { chrome.alarms.create("tb", { periodInMinutes: 0.5 }); report(); });
chrome.alarms.onAlarm.addListener(report);
chrome.alarms.create("tb", { periodInMinutes: 0.5 });   // the agent treats a report older than 70 s as "unknown"
report();
