# Runbook 1 trang — Region chính down

**Khi nào dùng:** alert `region-a UNHEALTHY` từ `dr/health_checker.py`, hoặc error rate ở
edge vượt 50% quá 15s. **Mục tiêu:** RTO ≤ 300s. **Đo được lần gần nhất: 18.1s.**

**Trước khi bắt đầu:** `cd` vào thư mục lab, stack đang chạy (`bash scripts/up_bare.sh`,
Windows: `powershell -File scripts/up_win.ps1`). Mọi lệnh dưới đây copy-paste chạy thẳng
**trong bash** — trên Windows mở Git Bash, đừng dán vào PowerShell: `printf`, `curl`,
`for i in $(seq 10)` ở bước 4-6 là cú pháp POSIX, PowerShell sẽ báo lỗi cú pháp chứ
không phải lỗi hệ thống.

**Đường tắt:** cả 7 bước đã được tự động hoá — `python3 dr/runbook.py --primary a --target b
--backend fs` (hỏi confirm y/N ở bước 3, đúng như thiết kế). Bảng dưới là bản thủ công để
người trực hiểu *chuyện gì đang xảy ra* và để chạy tay khi automation hỏng.

| # | Bước | Lệnh | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | `a.alive=false` (hoặc treo tới timeout) 3 lần liên tiếp, **và** `b.alive=true`. Nếu b cũng chết → **DỪNG**, đây là double outage, failover không cứu được gì | on-call primary |
| 2 | Mở incident + bấm giờ RTO | `python3 dr/runbook.py --primary a --target b --backend fs` (dừng ở prompt confirm) | Có dòng `step:2 thong_bao_incident` trong `reports/runbook-run.jsonl`, trường `notify_lag_s` > 0 | on-call primary |
| 3 | Restore state ở region phụ | `python3 state/snapshot.py get --region b --backend fs` | In ra JSON có `embed_model_version` và `restored_at`. Nếu báo "khong tim thay MANIFEST.json" → **DỪNG**, chưa từng có snapshot, cutover sẽ chỉ đổi 503 lấy 503 | on-call primary |
| 4 | Scale pool warm→full | `printf full > state/region-b/pool_state` | `curl -s -o /dev/null -w '%{http_code}' localhost:8002/readyz` trả `200` (mất ~6s warm-up, kiên nhẫn — **đừng** sang bước 5 khi còn 503) | on-call primary |
| 5 | DNS/LB cutover | `printf b > edge/active_region` | `curl localhost:8080/edge/state` cho `active_region=b`; sau đó `curl localhost:8080/v1/infer` trả `"region":"b"` (chờ tối đa `EDGE_TTL_SECONDS`=5s cho cache hết hạn) | on-call primary |
| 6 | Verify golden signals | `for i in $(seq 10); do curl -s -o /dev/null -w '%{http_code} %{time_total}\n' localhost:8080/v1/infer; done` | 10/10 trả `200`, p95 < 1000ms, error rate = 0. Lần chạy gần nhất: p95 380.2ms, error rate 0.0 (`reports/runbook-run.jsonl:6`) | on-call secondary |
| 7 | Đo RTO + postmortem | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | `"valid":true`, `"warnings":[]`, `rto_verdict` = `PASS`. Điền `reports/rto-evidence.md` + `reports/postmortem.md` trong 24h | incident commander |

**Escalate khi:** bước 3 không tìm thấy snapshot, bước 4 quá 60s vẫn 503, hoặc bước 6 có
bất kỳ request nào fail → gọi Infra lead, **không** tự lặp lại failover.

## Rollback (failback b→a)

**Điều kiện — phải đủ CẢ BA, không được đủ hai:**

1. `curl localhost:8001/readyz` trả 200 liên tục ≥ 10 phút (không phải 1 lần thành công).
2. Đã xác định và ghi lại root cause của outage region-a trong postmortem — chưa biết vì
   sao nó chết thì trả traffic về là mời nó chết lần nữa.
3. Dữ liệu ingest vào region-b trong lúc failover đã được replicate ngược về region-a
   (`python3 state/snapshot.py put --region b --backend fs` rồi `get --region a`), xác nhận
   bằng `python3 state/snapshot.py lag --backend fs` cho `rpo_seconds` gần 0. Bỏ bước này =
   mất toàn bộ dữ liệu phát sinh trong thời gian chạy ở region phụ.

**Ai quyết định:** **Infra lead** (không phải on-call). On-call có quyền failover *đi* một
mình vì hệ thống đang chết và chờ xin phép chỉ làm RTO dài thêm; nhưng failback là thao
tác *tự nguyện* trên một hệ thống đang lành — không có lý do gì để làm nó lúc 3h sáng.

**Lệnh:** `python3 dr/runbook.py --primary b --target a --backend fs` (KHÔNG dùng `--auto`
khi failback — `--auto` chỉ dành cho CI và drill chấm điểm).

**Circuit breaker:** tối đa **1 lần cutover / giờ**. Nếu đã failover a→b rồi b→a trong
cùng một giờ, dừng mọi automation và chuyển sang xử lý tay — hệ thống đang flap, và mỗi
lần lật thêm là thêm ~18s downtime chứ không phải bớt (§4 Anti-Patterns).
