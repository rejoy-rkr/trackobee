"""Writes packaging/config.json (baked into the agent). Usage: TB_SERVER=.. TB_AGENT_TOKEN=.. TB_ALLOWED_ORIGINS=.. python make_config.py"""
import json, os, sys
need = {k: os.environ.get(v) for k, v in
        (("server", "TB_SERVER"), ("agent_token", "TB_AGENT_TOKEN"), ("allowed_origins", "TB_ALLOWED_ORIGINS"))}
miss = [k for k, v in need.items() if not v]
if miss: sys.exit("missing env for: " + ", ".join(miss))
json.dump(need, open(os.path.join(os.path.dirname(__file__), "config.json"), "w"))
print("config.json written")
