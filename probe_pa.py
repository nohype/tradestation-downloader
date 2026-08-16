"""Check @PA validity on symbols + barcharts endpoints, and what list_symbols returns."""
import yaml
import requests
import glob
import os
from tradestation.auth import TradeStationAuth

with open("config.yaml") as f:
    data = yaml.safe_load(f)
ts = data["tradestation"]
auth = TradeStationAuth(ts["client_id"], ts["client_secret"], ts["refresh_token"])

BASE = "https://api.tradestation.com/v3"
headers = {"Authorization": f"Bearer {auth.get_access_token()}"}

print("=== data dir: @PA files ===")
print(glob.glob("data/*PA*") or "NO @PA files in data/")

print()
print("=== /v3/marketdata/symbols/ endpoint ===")
for sym in ["@PA", "@PA=11INC", "@UB", "@UB=11INC", "@ES", "@ES=11INC"]:
    r = requests.get(f"{BASE}/marketdata/symbols/{sym}", headers=headers, timeout=30)
    print(f"  {sym:12s} -> HTTP {r.status_code}: {r.text[:150]}")

print()
print("=== /v3/marketdata/barcharts/ endpoint (recent date 2026-08-01) ===")
for sym in ["@PA", "@PA=11INC"]:
    r = requests.get(f"{BASE}/marketdata/barcharts/{sym}", headers=headers,
                     params={"interval": 1, "unit": "Minute", "barsback": 3, "lastdate": "2026-08-01T14:00:00Z"},
                     timeout=30)
    print(f"  {sym:12s} -> HTTP {r.status_code}: {r.text[:120]}")

print()
print("=== barcharts @PA at old date (2024-07-18) — plain only ===")
r = requests.get(f"{BASE}/marketdata/barcharts/@PA", headers=headers,
                 params={"interval": 1, "unit": "Minute", "barsback": 3, "lastdate": "2024-07-18T20:00:00Z"},
                 timeout=30)
print(f"  @PA -> HTTP {r.status_code}: {r.text[:120]}")

print()
print("=== storage.list_symbols() ===")
from tradestation.config import load_config
from tradestation.storage import create_storage, detect_storage_format
config = load_config("config.yaml")
sf = detect_storage_format(config.data_dir)
storage = create_storage(sf, config.data_dir)
syms = storage.list_symbols()
print(f"  {len(syms)} symbols: {syms}")
