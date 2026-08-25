"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
HEALTH_LOG = pathlib.Path("reports/health-events.jsonl")
CHAOS_LOG = pathlib.Path("chaos/chaos-events.jsonl")


def _jsonl(p: pathlib.Path) -> list[dict]:
    if not p.exists():
        return []
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()]


def step(n, name, **kw):
    """Ghi 1 dong {ts, iso, step, name, ...} vao LOG.

    Dong log nay chinh la timeline cua postmortem. Viet no NGAY LUC su kien xay ra,
    khong phai viet lai tu tri nho sau khi incident da xong.
    """
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
           "step": n, "name": name, **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"RUNBOOK {n} {name}", json.dumps(kw, ensure_ascii=False))
    return rec


def confirm(auto: bool, msg: str) -> bool:
    """Ban tu dong: mac dinh PHAI co nguoi go 'y'.

    §4 Anti-Patterns: full-auto failover khong co circuit breaker se flap qua lai giua
    hai region moi khi health check nhay. Con nguoi o day CHINH LA circuit breaker.
    --auto chi danh cho CI / luc chay drill cham diem.
    """
    if auto:
        print(f"[auto] {msg} -> y")
        return True
    try:
        return input(f"{msg} [y/N] ").strip().lower() == "y"
    except EOFError:
        # Khong co TTY (chay trong pipeline) ma cung khong truyen --auto: mac dinh la
        # KHONG lam gi. Im lang cutover moi la hanh vi nguy hiem.
        print("khong co stdin va khong co --auto -> coi nhu tu choi")
        return False


def _probe(region: str, timeout: float = 2.0) -> bool:
    try:
        return httpx.get(f"{URL[region]}/readyz", timeout=timeout).status_code == 200
    except Exception:
        return False


def _t_outage(primary: str):
    """t_outage that = dong `action:kill` cuoi cung cho region chinh trong chaos log."""
    kills = [e for e in _jsonl(CHAOS_LOG)
             if e.get("action") == "kill" and e.get("region") == primary]
    return kills[-1]["ts"] if kills else None


def _wait_for_alert(primary: str, since: float, budget: float):
    """Cho dong UNHEALTHY cua health checker cho region chinh.

    Vi sao runbook phai CHO alert chu khong tu quyet dinh: t_detect la con so do bang
    automation. Neu operator cutover TRUOC khi health check kip phat hien, RTO do duoc
    la do toc do bam tay cua nguoi truc, khong tai lap duoc -- tools/measure_rto.py se
    canh bao (t_cutover < t_detect) va drill khong con hop le.
    Khong co health log (chay runbook doc lap) -> bo qua, tu probe la du.
    """
    end = time.time() + budget
    while time.time() < end:
        for e in _jsonl(HEALTH_LOG):
            if (e.get("event") == "state_change" and e.get("to") == "UNHEALTHY"
                    and e.get("region") == primary and e.get("ts", 0) >= since):
                return e
        if not HEALTH_LOG.exists():
            return None
        time.sleep(0.5)
    return None


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    """7 buoc runbook §4 "Runbook: Region Chinh Down"."""
    t_outage = _t_outage(primary)
    out = {"primary": primary, "target": target, "backend": backend, "auto": auto,
           "t_outage": t_outage}

    # --- 1. Xac nhan outage: dung tin MOT lan fail (§4 chong flapping) ---
    fails, probes = 0, []
    for _ in range(3):
        ok = _probe(primary)
        probes.append(ok)
        fails = 0 if ok else fails + 1
        if not ok:
            time.sleep(1.0)
    target_alive = _probe(target)
    alert = _wait_for_alert(primary, t_outage or 0.0, budget=90)
    step(1, "xac_nhan_outage", region=primary, probes=probes, consecutive_fails=fails,
         primary_down=fails >= 3, target_alive=target_alive,
         health_alert_ts=(alert or {}).get("ts"),
         detect_floor_s=(alert or {}).get("detect_floor_s"),
         alert_lag_s=None if not (alert and t_outage) else round(alert["ts"] - t_outage, 2),
         note="cho health checker phat alert roi moi hanh dong -- t_cutover phai SAU "
              "t_detect, neu khong thi con so RTO do toc do go phim chu khong do he thong")
    if fails < 3:
        step(0, "abort", reason="region chinh van tra loi /readyz -> day khong phai outage")
        out.update(ok=False, aborted_at=1)
        return out

    # --- 2. Thong bao incident + bat dong ho RTO ---
    t_notify = time.time()
    step(2, "thong_bao_incident", severity="SEV1",
         summary=f"region-{primary} khong serve duoc, cutover sang region-{target}",
         t_outage=t_outage, t_notify=t_notify,
         notify_lag_s=None if not t_outage else round(t_notify - t_outage, 2),
         note="notify_lag_s = do tre giua luc he thong hong va luc con nguoi biet tin. "
              "Luon > 0: operator khong the biet ngay giay outage xay ra.")
    out["t_notify"] = t_notify

    # --- 3. Scale GPU pool + cutover: GOI failover MOT LAN DUY NHAT ---
    if not confirm(auto, f"Cutover traffic tu region-{primary} sang region-{target}?"):
        step(3, "scale_gpu_pool", confirmed=False,
             note="operator tu choi -> dung lai, khong dong vao edge/active_region")
        out.update(ok=False, aborted_at=3)
        return out
    fr = fo.failover(target, backend, wait=60)
    step(3, "scale_gpu_pool", confirmed=True, failover_ok=fr.get("ok"),
         wait_ready_s=fr.get("wait_ready_s"), failed_step=fr.get("failed_step"),
         elapsed_s=fr.get("elapsed_s"),
         note="5 buoc con nam o reports/failover-events.jsonl")
    out["failover"] = fr

    # --- 4. Verify state replica: CHI DOC lai ket qua buoc 3, khong goi lai failover ---
    after = fr.get("target_state_after") or {}
    step(4, "verify_state_replica", region=target, vectors=after.get("count"),
         weights=after.get("weights"), pool_state=after.get("pool_state"),
         rpo_seconds=fr.get("rpo_seconds"), docs_lost=fr.get("docs_lost"),
         embed_model_version=fr.get("embed_model_version"),
         note="RPO doc tu day, mot cho duy nhat -- postmortem khong phai mo 3 file")

    # --- 5. DNS cutover: cung chi doc lai ket qua ---
    active = pathlib.Path("edge/active_region")
    step(5, "dns_cutover", ok=bool(fr.get("ok")), **{"from": fr.get("cutover_from")},
         to=fr.get("cutover_to"),
         active_region=active.read_text().strip() if active.exists() else None)
    if not fr.get("ok"):
        step(0, "abort",
             reason=f"failover that bai o buoc {fr.get('failed_step')} -> KHONG cutover, "
                    f"traffic van o region-{primary}")
        out.update(ok=False, aborted_at=5)
        return out

    # --- 6. Golden signals: 10 request THAT, khong phai "nhin co ve on" ---
    lat, errs = [], 0
    for i in range(10):
        t0 = time.time()
        try:
            r = httpx.get(f"{URL[target]}/v1/infer", params={"q": f"hoa don thang {i + 1}"},
                          timeout=5.0)
            if r.status_code != 200 or r.json().get("error"):
                errs += 1
        except Exception:
            errs += 1
        lat.append(round((time.time() - t0) * 1000, 1))
    srt = sorted(lat)
    p95 = srt[min(len(srt) - 1, int(round(0.95 * (len(srt) - 1))))]
    step(6, "verify_golden_signals", requests=len(lat), errors=errs,
         error_rate=round(errs / len(lat), 3), p95_latency_ms=p95,
         max_latency_ms=max(lat), region=target)
    out.update(p95_latency_ms=p95, error_rate=round(errs / len(lat), 3))

    # --- 7. Post incident ---
    elapsed = round(time.time() - (t_outage or t_notify), 2)
    step(7, "post_incident", elapsed_since_outage_s=elapsed,
         rto_command="python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl "
                     "--target-rto 300",
         next_steps=["dien reports/rto-evidence.md", "dien reports/postmortem.md",
                     f"quyet dinh dieu kien failback ve region-{primary}"])
    out.update(ok=True, elapsed_since_outage_s=elapsed)
    return out


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2, ensure_ascii=False))
