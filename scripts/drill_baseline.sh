#!/usr/bin/env bash
# Step 1 cua GUIDE.md: baseline KHONG co DR.
# Phai nam trong MOT shell duy nhat: make chay moi dong recipe bang mot shell rieng,
# nen `... &` o dong 1 va `wait` o dong 3 khong con la cha-con -> wait tra ve ngay,
# make ket thuc trong ~8s trong khi loadgen con chay them 30s. Ai restore region-a
# ngay sau do se vo tinh restore GIUA drill 1 -> baseline tu phuc hoi -> test doi
# NO_RECOVERY se thay PASS.
set -u
cd "$(dirname "$0")/.."

python3 loadgen/traffic.py --duration 40 --rps 2 --out reports/drill-1-nodr.jsonl &
lg=$!
sleep 8
python3 chaos/kill_region.py --region a --mode netblock --mock
wait "$lg"
echo "drill 1 xong -- gio moi duoc restore region-a"
