# RTO/RPO Evidence — Lab 23

Drill chạy bare mode (`--mock`), Windows 11 + Python 3.11, ngày 2026-08-25.
Mọi con số dưới đây đọc ra từ log của chính lần chạy này; cột Evidence là
`đường/dẫn:số_dòng` mở ra là thấy.

Cấu hình: `interval=5s`, `threshold=3`, `WARMUP_SECONDS=6`, `EDGE_TTL_SECONDS=5`,
replication `--every 30 --backend fs`, ingest `--rate 0.5 doc/s`.

## 1. Drill 1 — không có DR (baseline)

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | `2026-08-25T10:05:10` | chaos kill region a, mode netblock | `chaos/chaos-events.jsonl:9` |
| Request fail đầu tiên | +2.2s (ReadTimeout 2170.9ms) | dòng `ok:false` đầu tiên sau t_outage | `reports/drill-1-nodr.jsonl:18` |
| Tỉ lệ fail sau t_outage | 14/14 request | mọi dòng sau t_outage đều `ok:false` | `reports/drill-1-nodr.jsonl:18` |
| Request thành công sau đó | không có | không có dòng `ok:true` nào sau t_outage | `reports/measure-drill-1.json` |
| RTO | `NO_RECOVERY` | `tools/measure_rto.py` | `reports/measure-drill-1.json` |

Region A bị treo (netblock): cổng vẫn accept TCP nhưng không ai trả lời, nên client
treo tới timeout chứ không fail nhanh — request đang bay lúc kill vẫn ăn trọn 2.2s
timeout trước khi báo lỗi. Không có gì phát hiện, không có gì cutover — hệ thống nằm
im cho tới khi có người vào gõ tay. Đó là định nghĩa của `NO_RECOVERY`, không phải
"RTO dài".

## 2. Drill 2 — có DR

| Mốc | +giây từ t_outage | Cách đo | Evidence |
|---|---|---|---|
| t_outage (mốc 0) | 0 | `action:kill`, region a, `other_alive:true` | `chaos/chaos-events.jsonl:3` |
| User thấy lỗi đầu tiên | +0.1s | dòng `ok:false` đầu | `reports/drill-2-withdr.jsonl:25` |
| Health check phát hiện | +14.9s | `to:UNHEALTHY, region:a, consecutive_fails:3` | `reports/health-events.jsonl:1` |
| Operator xác nhận + mở incident | +15.3s | `step:2 thong_bao_incident`, `notify_lag_s:15.34` | `reports/runbook-run.jsonl:2` |
| Snapshot restore xong | +15.6s | `step:2_restore_snapshot` | `reports/failover-events.jsonl:2` |
| Pool warm→full | +15.6s | `step:3_scale_pool` | `reports/failover-events.jsonl:3` |
| Region phụ ready | +15.8s | `step:4_wait_ready`, `waited_s:0.21` | `reports/failover-events.jsonl:4` |
| DNS cutover | +15.8s | `step:5_dns_cutover`, `from:a to:b` | `reports/failover-events.jsonl:5` |
| **RTO đo được** | **+18.1s** | dòng `ok:true` đầu sau lỗi, `served_by:b` | `reports/drill-2-withdr.jsonl:33` |

| Chỉ số | Đo được | Mục tiêu (slide §1) | Verdict |
|---|---|---|---|
| RTO — Inference API | 18.1s | 300s (5 phút) | **PASS** (còn dư 281.9s) |
| RPO — Vector DB | 4.0s / 2 doc | 300s (5 phút) | **PASS** |

Recovery được serve bởi region B (`served_by:b`, `reports/drill-2-withdr.jsonl:33`),
không phải region A hồi sinh — 8 request fail trong cửa sổ outage, tổng
`reports/measure-drill-2.json`.

RPO 4.0s / 2 doc đọc ở `reports/failover-events.jsonl:2`: snapshot dùng để restore
chốt ở `latest_doc_ts` +10.5s (`reports/replication.jsonl:2`), trong khi
`state/ingest.py` vẫn ghi tiếp vào region A tới lúc restore ở +15.6s. 2 document
nằm giữa hai mốc đó không có trong bản restore. Đây là RPO đo bằng hiệu số dữ liệu,
không phải "tuổi của snapshot".

## 3. RTO của tôi gồm những gì

| Thành phần | Giây | Nó đến từ đâu | Giảm được bằng cách nào |
|---|---:|---|---|
| Health-check detect floor | 14.9s | `interval_s × threshold` = 5 × 3 = 15.0s, `reports/health-events.jsonl:1` | Hạ interval xuống 2s → floor 6s. Trả giá: probe dày hơn, một cú GC/ngắt mạng thoáng qua dễ đủ 3 lần fail liên tiếp → flap. |
| Snapshot restore | 0.7s | t_detect → `2_restore_snapshot`, `reports/failover-events.jsonl:2` | Gần như không giảm được nữa; ở quy mô thật (index GB) đây mới là phần đắt → cần continuous replication thay vì snapshot 30s. |
| GPU pool warm-up | 0.2s | `waited_s:0.21` ở `4_wait_ready`, `reports/failover-events.jsonl:4` | Đã bằng 0 trong lần chạy này: region B khởi đầu ở `pool_state:full` (`reports/failover-events.jsonl:1`), tức là đang trả tiền GPU idle 24/7 để mua lại 6s warm-up. |
| DNS/LB TTL cache | 2.3s | t_recovered − t_cutover, `reports/drill-2-withdr.jsonl:33` − `reports/failover-events.jsonl:5` | Hạ `EDGE_TTL_SECONDS`, hoặc dùng health-check-based LB thay vì DNS TTL. Trả giá: query DNS nhiều hơn. |
| **Tổng** | **18.1s** | khớp `rto_measured_s` trong `reports/measure-drill-2.json` | |

Detect floor chiếm 14.9/18.1 = **82.3%** RTO. Nói cách khác: hơn bốn phần năm thời gian
chết là hệ thống đang *chờ đủ bằng chứng để tin là đã chết*. Tối ưu restore hay
warm-up trước khi động vào con số này là tối ưu nhầm chỗ — lần chạy này đã mua warm
standby (warm-up còn 0.2s) và RTO chỉ nhúc nhích, đúng như dự đoán đó.
