"""BƯỚC 3a — SINH VIÊN VIẾT. Health checker cho 2 region.

Yêu cầu (đọc §4 "Kiến Trúc Health-Check-Based Failover" + §2 "DNS Failover"):
  1. Poll /readyz của CẢ HAI region mỗi `interval` giây (mặc định 5s).
     Dùng /readyz, KHÔNG dùng /healthz. /healthz chỉ nói "process còn sống" —
     region có process sống nhưng vector DB rỗng thì vẫn không serve được.
  2. Chỉ đổi trạng thái sau `threshold` lần fail LIÊN TIẾP (mặc định 3).
     Một lần fail không phải outage. Đây là chống flapping (§4 Anti-Patterns).
  3. Ghi 1 dòng JSONL MỖI LẦN ĐỔI TRẠNG THÁI (không ghi mỗi lần poll — log sẽ ngập).
     Dòng bắt buộc có: ts, region, to (HEALTHY|UNHEALTHY), reason,
     interval_s, threshold. Thiếu interval_s/threshold thì tools/measure_rto.py
     không tính được detect floor -> mất điểm.

Chạy:  python dr/health_checker.py --interval 5 --threshold 3 --duration 300 \
              --out reports/health-events.jsonl

CÂU HỎI PHẢI TRẢ LỜI TRƯỚC KHI VIẾT (ghi câu trả lời vào reports/postmortem.md):
  interval=5s, threshold=3 -> sớm nhất bạn có thể phát hiện outage là bao nhiêu giây?
  Con số đó nằm TRONG RTO của bạn. Muốn RTO 5 phút thì được phép chọn interval bao nhiêu?
"""
import argparse
import json
import pathlib
import time

import httpx

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def probe(region: str, timeout: float) -> tuple[bool, str]:
    """Mot lan poll /readyz. Tra ve (ready, reason).

    timeout la BAT BUOC, khong phai tuy chon: che do netblock (SIGSTOP / iptables DROP)
    van cho TCP handshake xong roi im lang -- khong co timeout thi httpx doi vo han va
    vong lap poll dung han o day, health checker khong bao gio bao cao gi ca.
    """
    try:
        r = httpx.get(f"{URL[region]}/readyz", timeout=timeout)
    except Exception as e:
        # Ca hai deu la "khong serve duoc": connect refused (stop) va treo (netblock).
        return False, f"probe_error:{type(e).__name__}"
    if r.status_code == 200:
        return True, "ready"
    try:
        reasons = ",".join(r.json().get("reasons") or []) or "not_ready"
    except Exception:
        reasons = "not_ready"
    return False, f"http_{r.status_code}:{reasons}"


def run(interval: float, timeout: float, threshold: int, duration: float, out: pathlib.Path):
    """Poll ca 2 region, chi ghi log khi trang thai THAY DOI.

    State machine cho moi region:
      - dem so lan fail LIEN TIEP; mot lan ok bat ky reset ve 0.
      - HEALTHY -> UNHEALTHY chi khi consecutive_fails >= threshold (chong flapping, §4).
      - UNHEALTHY -> HEALTHY ngay lan poll thanh cong dau tien (phuc hoi thi khong can cho:
        cho them chi keo dai RTO ma khong giam rui ro nao).
    Trang thai xuat phat la HEALTHY: health checker chi bao khi co GI DO DOI, nen mot
    drill binh thuong (moi thu dang song) khong duoc de lai dong log nao ca. Neu khoi tao
    la None thi dong dau tien luon la "HEALTHY" vo nghia, lan vao giua log cua incident.
    """
    out.parent.mkdir(parents=True, exist_ok=True)
    st = {r: {"state": "HEALTHY", "fails": 0, "oks": 0} for r in URL}
    end = time.time() + duration
    # mode "a": health-events.jsonl tich luy qua nhieu lan chay; measure_rto.py cat theo
    # cua so thoi gian cua loadgen nen khong lan drill nay sang drill khac.
    with out.open("a") as f:
        def emit(**kw):
            rec = {"ts": time.time(), "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                   "interval_s": interval, "threshold": threshold, "timeout_s": timeout,
                   "detect_floor_s": round(interval * threshold, 1), **kw}
            f.write(json.dumps(rec) + "\n")
            f.flush()
            print("HEALTH", json.dumps(rec))
            return rec

        while time.time() < end:
            t0 = time.time()
            for region in URL:
                ready, reason = probe(region, timeout)
                s = st[region]
                if ready:
                    s["fails"], s["oks"] = 0, s["oks"] + 1
                    new = "HEALTHY"
                else:
                    s["fails"], s["oks"] = s["fails"] + 1, 0
                    # Chua du threshold thi GIU NGUYEN trang thai cu -- mot lan fail
                    # khong phai outage, do la ban chat cua chong flapping.
                    new = "UNHEALTHY" if s["fails"] >= threshold else s["state"]
                if new != s["state"]:
                    emit(event="state_change", region=region, **{"from": s["state"]}, to=new,
                         reason=reason, consecutive_fails=s["fails"], consecutive_oks=s["oks"])
                    s["state"] = new
            time.sleep(max(0.0, interval - (time.time() - t0)))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--timeout", type=float, default=2.0)
    p.add_argument("--threshold", type=int, default=3)
    p.add_argument("--duration", type=float, default=300)
    p.add_argument("--out", default="reports/health-events.jsonl")
    a = p.parse_args()
    run(a.interval, a.timeout, a.threshold, a.duration, pathlib.Path(a.out))
