#!/usr/bin/env python3
"""Packages the Trackobee browser extension into <site>/extension/ (CI runs this before publishing the website).

  python3 packaging/build_extension.py <_site dir> <site url>
  env  EXT_KEY_PEM          private key (secret). Without it no .crx is built, and installers fall back to "Load unpacked".
       CHROME_EXT_STORE_ID  Chrome Web Store id, if you publish the extension there (then browsers update from the store instead).

Writes: extension/trackobee.crx, extension/update.xml, extension/info.json, extension/trackobee-extension.zip (for a Web Store upload)."""
import base64, hashlib, json, os, shutil, subprocess, sys, tempfile, zipfile

site_dir, site_url = sys.argv[1], sys.argv[2].rstrip("/")
src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "site", "extension")
out = os.path.join(site_dir, "extension"); os.makedirs(out, exist_ok=True)
man = json.load(open(os.path.join(src, "manifest.json")))
der = base64.b64decode(man["key"])
ext_id = "".join(chr(ord("a") + int(c, 16)) for c in hashlib.sha256(der).hexdigest()[:32])
store = os.environ.get("CHROME_EXT_STORE_ID", "").strip()
info = {"id": store or "", "version": man["version"], "update_url": "", "store": bool(store)}

# store upload: same files without the pinned "key"
nokey = dict(man); nokey.pop("key", None)
with zipfile.ZipFile(os.path.join(out, "trackobee-extension.zip"), "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(os.listdir(src)):
        z.writestr(f, json.dumps(nokey, indent=2) if f == "manifest.json" else open(os.path.join(src, f), "rb").read())

if store:
    info["update_url"] = "https://clients2.google.com/service/update2/crx"
elif os.environ.get("EXT_KEY_PEM", "").strip():
    with tempfile.TemporaryDirectory() as t:
        shutil.copytree(src, os.path.join(t, "ext")); key = os.path.join(t, "key.pem")
        open(key, "w").write(os.environ["EXT_KEY_PEM"].strip() + "\n"); os.chmod(key, 0o600)
        chrome = next((c for c in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser") if shutil.which(c)), None)
        if not chrome: sys.exit("no Chrome/Chromium available to pack the extension")
        subprocess.run([chrome, "--headless=new", "--no-sandbox", "--disable-gpu", "--pack-extension=" + os.path.join(t, "ext"), "--pack-extension-key=" + key],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
        crx = os.path.join(t, "ext.crx")
        if not os.path.exists(crx): sys.exit("packing the extension failed")
        shutil.copy(crx, os.path.join(out, "trackobee.crx")); os.chmod(os.path.join(out, "trackobee.crx"), 0o644)
    open(os.path.join(out, "update.xml"), "w").write(
        "<?xml version='1.0' encoding='UTF-8'?>\n<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        "  <app appid='%s'><updatecheck codebase='%s/extension/trackobee.crx' version='%s' /></app>\n</gupdate>\n" % (ext_id, site_url, man["version"]))
    info.update(id=ext_id, update_url=site_url + "/extension/update.xml")
json.dump(info, open(os.path.join(out, "info.json"), "w"), indent=2)
print("extension", info)
