#!/usr/bin/env bash
# Step 4 cua GUIDE.md: drill co DR + do RTO.
# Chay bang bash (PowerShell khong hieu '&' background va line-continuation '\').
set -u
cd "$(dirname "$0")/.."

python3 state/ingest.py --region a --rate 0.5 --duration 150 &
python3 state/replicate.py --every 30 --duration 150 --backend fs &
sleep 5   # cho chu ky replication dau tien xong truoc khi lam gi khac

python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 \
    --out reports/health-events.jsonl &
sleep 12
python3 chaos/kill_region.py --region a --mode netblock --mock
python3 dr/runbook.py --primary a --target b --backend fs --auto

wait   # doi loadgen/ingest/replicate/health_checker ket thuc roi moi do RTO
python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300
