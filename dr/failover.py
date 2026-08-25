"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    """Append 1 dong JSONL vao LOG + in ra stdout.

    Moi buoc mot dong: log nay la thu duy nhat tools/measure_rto.py doc de biet
    t_cutover, rpo_seconds, docs_lost. Khong ghi = coi nhu failover chay tay.
    """
    LOG.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), **kw}
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")
    print("FAILOVER", json.dumps(rec))
    return rec


def state_of(region: str) -> dict:
    """/v1/state cua mot region. Khong nem exception ra ngoai -- buoc 1 la buoc CHAN DOAN,
    region phu khong tra loi cung la mot ket qua chan doan hop le, khong phai crash."""
    try:
        return httpx.get(f"{URL[region]}/v1/state", timeout=3.0).json()
    except Exception as e:
        return {"region": region, "error": type(e).__name__}


def failover(target: str, backend: str, wait: float) -> dict:
    """Cutover sang `target`, dung 5 buoc, dung thu tu do.

    Bat bien quan trong nhat: edge/active_region CHI duoc ghi o buoc 5, va chi khi buoc 4
    da xac nhan /readyz tra 200. Cutover som = user an 503 tu ca hai region.
    """
    t_start = time.time()
    result = {"ok": False, "target": target, "backend": backend, "started_at": t_start}

    # --- 1. verify_target: region phu dang co gi? (weights? vector count? pool_state?) ---
    before = state_of(target)
    emit(step="1_verify_target", region=target, state=before,
         note="chup lai truoc khi dong vao bat cu thu gi -- postmortem can biet region phu "
              "khoi dau rong den muc nao")
    result["target_state_before"] = before

    # --- 2. restore_snapshot: keo vector DB + model weights tu object store ve ---
    try:
        meta = snapshot.get(target, backend)
    except Exception as e:
        emit(step="2_restore_snapshot", ok=False, error=f"{type(e).__name__}: {e}",
             note="chua tung co snapshot nao duoc put -> khong co gi de restore. ABORT, "
                  "KHONG cutover: doi DNS sang mot region rong chi doi 503 lay 503.")
        result.update(ok=False, failed_step="2_restore_snapshot", error=str(e),
                      elapsed_s=round(time.time() - t_start, 2))
        return result
    # RPO do bang so lieu THAT: so sanh primary voi ban vua restore, khong phai "tuoi snapshot".
    primary = "a" if target == "b" else "b"
    r = snapshot.rpo(pathlib.Path(f"state/region-{primary}/vectors.sqlite"),
                     pathlib.Path(f"state/region-{target}/vectors.sqlite"))
    emit(step="2_restore_snapshot", ok=True,
         rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
         embed_model_version=meta.get("embed_model_version"),
         snapshot_at=meta.get("snapshot_at"), restored_at=meta.get("restored_at"),
         note="embed_model_version phai di kem index: restore index moi voi model cu = "
              "vector khong tuong thich (§3)")
    result.update(rpo_seconds=r["rpo_seconds"], docs_lost=r["docs_lost"],
                  embed_model_version=meta.get("embed_model_version"))

    # --- 3. scale_pool: warm -> full. Day la luc dong ho GPU warm-up bat dau chay. ---
    pool = pathlib.Path(f"state/region-{target}/pool_state")
    was = pool.read_text().strip() if pool.exists() else "cold"
    pool.write_text("full")
    emit(step="3_scale_pool", region=target, **{"from": was}, to="full",
         note="serving/app.py bat dau dem WARMUP_SECONDS TU DAY -- day chinh la GPU pool "
              "warm-up trong RTO breakdown")

    # --- 4. wait_ready: poll /readyz toi khi 200. Timeout -> ABORT. ---
    t4 = time.time()
    ready, last = False, None
    while time.time() - t4 < wait:
        try:
            resp = httpx.get(f"{URL[target]}/readyz", timeout=2.0)
            last = resp.status_code
            if resp.status_code == 200:
                ready = True
                break
        except Exception as e:
            last = type(e).__name__
        time.sleep(0.5)
    waited = round(time.time() - t4, 2)
    emit(step="4_wait_ready", region=target, ready=ready, waited_s=waited,
         last_probe=str(last), wait_budget_s=wait)
    result.update(wait_ready_s=waited, target_ready=ready)
    if not ready:
        emit(step="ABORT", reason="target_not_ready", waited_s=waited,
             note="KHONG ghi edge/active_region. Cutover sang region chua ready lam RTO DAI "
                  "hon chu khong ngan hon: user an 503 tu ca hai phia.")
        result.update(ok=False, failed_step="4_wait_ready",
                      elapsed_s=round(time.time() - t_start, 2))
        return result

    # --- 5. dns_cutover: chi bay gio moi doi "DNS". ---
    active = pathlib.Path("edge/active_region")
    prev = active.read_text().strip() if active.exists() else "a"
    active.write_text(target)
    emit(step="5_dns_cutover", **{"from": prev}, to=target,
         note="edge doc lai file moi request nhung co EDGE_TTL_SECONDS cache -> con them "
              "vai giay TTL nua user moi thay region moi (§2 DNS TTL)")
    result.update(ok=True, cutover_from=prev, cutover_to=target,
                  target_state_after=state_of(target),
                  elapsed_s=round(time.time() - t_start, 2))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))
