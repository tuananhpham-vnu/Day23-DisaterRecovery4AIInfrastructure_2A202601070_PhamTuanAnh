# Tren Windows, GNU Make mac dinh dung cmd.exe -> khong co printf/sleep/&/wait.
# Ep dung bash cua Git for Windows. Phai dung duong dan 8.3 (PROGRA~1) vi
# CreateProcess cua make khong xu ly duoc khoang trang trong SHELL, va "bash.exe"
# tran trui co the tro nham vao stub WSL trong WindowsApps.
ifeq ($(OS),Windows_NT)
GIT_BASH := $(firstword $(wildcard C:/PROGRA~1/Git/usr/bin/bash.exe C:/PROGRA~2/Git/usr/bin/bash.exe C:/PROGRA~1/Git/bin/bash.exe))
ifneq ($(GIT_BASH),)
SHELL := $(GIT_BASH)
.SHELLFLAGS := -c
endif
endif

.PHONY: seed up-bare down-bare drill-baseline drill-dr drill-full rto test clean

seed:
	python3 state/seed_vectors.py --region a --docs 200
	python3 state/seed_vectors.py --region b --docs 0 --weights-mb 0
	printf a > edge/active_region

up-bare:
	bash scripts/up_bare.sh

down-bare:
	bash scripts/down_bare.sh

# Bước 2: baseline không DR — dùng đúng script sinh viên sẽ chạy tay
drill-baseline:
	bash scripts/drill_baseline.sh

# Bước 4: replay attack sau khi contain xong
# replicate.py phai chay TRUOC va co it nhat 1 chu ky xong, khong thi failover.py
# se chet o buoc 2_restore_snapshot vi chua tung co snapshot nao duoc put.
drill-dr:
	python3 state/ingest.py --region a --rate 0.5 --duration 150 &
	python3 state/replicate.py --every 30 --duration 150 --backend fs &
	sleep 5
	python3 loadgen/traffic.py --duration 100 --rps 2 --out reports/drill-2-withdr.jsonl &
	python3 dr/health_checker.py --interval 5 --threshold 3 --duration 100 --out reports/health-events.jsonl &
	sleep 12; python3 chaos/kill_region.py --region a --mode netblock --mock

# Step 4 GUIDE.md day du: drill + runbook + do RTO (chay duoc tren Windows)
drill-full:
	bash scripts/drill_dr.sh

rto:
	python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300

# PYTHONUTF8=1: tests doc reports/*.md bang Path.read_text() khong truyen encoding ->
# tren Windows mac dinh cp1252 va no vo ngay o ky tu tieng Viet dau tien.
test:
	PYTHONUTF8=1 python3 -m pytest tests/ -v

clean:
	bash scripts/down_bare.sh 2>/dev/null || true
	rm -rf state/region-a state/region-b state/_replica run
	rm -f reports/*.jsonl reports/*.json chaos/chaos-events.jsonl
