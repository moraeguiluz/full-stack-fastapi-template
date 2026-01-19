#!/usr/bin/env python3
import os
import time
import json
import hashlib
import subprocess
import requests

API_BASE = os.getenv("NAVE_API_BASE", "__API_BASE__").rstrip("/")
TOKEN = os.getenv("NAVE_AGENT_TOKEN", "").strip()
AGENT_ID = os.getenv("NAVE_AGENT_ID", "").strip()

if not TOKEN:
    raise SystemExit("NAVE_AGENT_TOKEN requerido")

HEADERS = {"X-Agent-Token": TOKEN}


def register():
    global AGENT_ID
    if AGENT_ID:
        return AGENT_ID
    payload = {
        "vm_name": os.getenv("NAVE_VM_NAME"),
        "public_ip": os.getenv("NAVE_PUBLIC_IP"),
    }
    resp = requests.post(
        f"{API_BASE}/nave/infra/agents/register",
        json=payload,
        headers=HEADERS,
        timeout=15,
    )
    resp.raise_for_status()
    AGENT_ID = str(resp.json().get("agent_id"))
    os.environ["NAVE_AGENT_ID"] = AGENT_ID
    return AGENT_ID


def apply_wg(conf_text):
    if not conf_text:
        return False
    os.makedirs("/etc/wireguard", exist_ok=True)
    with open("/etc/wireguard/wg0.conf", "w", encoding="utf-8") as f:
        f.write(conf_text)
    subprocess.run(["wg-quick", "down", "wg0"], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["wg-quick", "up", "wg0"], check=True)
    return True


def main():
    last_hash = ""
    agent_id = register()
    while True:
        try:
            resp = requests.get(
                f"{API_BASE}/nave/infra/agents/{agent_id}/desired",
                headers=HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            desired = resp.json().get("desired_json") or {}
            conf = desired.get("wg_conf", "")
            conf_hash = hashlib.sha256(conf.encode("utf-8")).hexdigest() if conf else ""
            changed = conf_hash and conf_hash != last_hash
            applied = False
            if changed:
                applied = apply_wg(conf)
                last_hash = conf_hash if applied else last_hash
            status = {
                "ts": time.time(),
                "wg_conf_hash": last_hash,
                "applied": applied,
            }
            requests.post(
                f"{API_BASE}/nave/infra/agents/{agent_id}/status",
                json={"status_json": status},
                headers=HEADERS,
                timeout=10,
            )
        except Exception as e:
            err = {"ts": time.time(), "error": str(e)}
            try:
                requests.post(
                    f"{API_BASE}/nave/infra/agents/{agent_id}/status",
                    json={"status_json": err},
                    headers=HEADERS,
                    timeout=10,
                )
            except Exception:
                pass
        time.sleep(10)


if __name__ == "__main__":
    main()
