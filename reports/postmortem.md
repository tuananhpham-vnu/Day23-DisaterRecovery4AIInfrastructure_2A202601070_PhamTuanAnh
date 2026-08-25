# Postmortem — DR Drill Lab 23

**Incident:** SEV1 — region-a (primary) không serve được inference, cutover sang region-b.
**Ngày:** 2026-08-25 · **Loại:** game day có kế hoạch (chaos `--mode netblock --mock`).
**Blameless:** câu hỏi là "hệ thống/process nào cho phép chuyện này", không phải "ai làm sai".

## 1. Timeline

Giờ UTC, lấy nguyên từ trường `iso` của log — không viết lại từ trí nhớ.

| ISO time | Sự kiện | Evidence |
|---|---|---|
| 09:50:56 | outage bắt đầu — region-a bị netblock (treo process), `other_alive:true` | `chaos/chaos-events.jsonl:3` |
| 09:50:57 | user đầu tiên bị ảnh hưởng — ReadTimeout 2233.2ms qua edge | `reports/drill-2-withdr.jsonl:25` |
| 09:51:11 | health check alert — region-a `UNHEALTHY` sau 3 lần fail liên tiếp | `reports/health-events.jsonl:1` |
| 09:51:12 | operator confirm + mở incident, bấm giờ RTO (`notify_lag_s:15.34`) | `reports/runbook-run.jsonl:2` |
| 09:51:12 | snapshot restore xong (`rpo_seconds:4.0`, `docs_lost:2`) | `reports/failover-events.jsonl:2` |
| 09:51:12 | region-b ready sau warm-up 0.21s (đã ở `pool_state:full`), DNS cutover a→b | `reports/failover-events.jsonl:5` |
| 09:51:15 | resolved — request đầu tiên OK, `served_by:b` | `reports/drill-2-withdr.jsonl:33` |

## 2. RTO/RPO đo được vs mục tiêu — gap ở bước nào?

- RTO mục tiêu: 300s · đo được: **18.1s** · gap: **−281.9s** (dưới mục tiêu, PASS)
- RPO mục tiêu: 300s · đo được: **4.0s** (**2** doc bị mất) · gap: **−296.0s** (PASS)
- **Bước tốn nhiều giây nhất:** `health-check detect floor` — **14.9s / 18.1s = 82.3%**.

Vì sao: `interval=5s × threshold=3` là *sàn* phát hiện. Trước khi đủ 3 lần probe fail
liên tiếp, hệ thống chưa được phép kết luận là outage — và nó không được phép, vì
2 lần fail có thể chỉ là một cú GC pause hay một gói tin rơi. Toàn bộ phần automation
phía sau (restore 0.7s + warm-up 0.2s + TTL 2.3s = 3.2s) rẻ hơn riêng cái sàn này gần 5 lần.

Đáng chú ý: gap thật của drill này **không nằm ở RTO**. RTO 18.1s dư 281.9s so với mục
tiêu. Chỗ đáng lo là RPO — 4.0s nghe rất nhỏ chỉ vì `replicate.py --every 30` chạy dày
bất thường cho lab. Ở lịch backup thật của §3 (vector DB mỗi 6h), cùng kiến trúc này
cho RPO tới **6 giờ**, tức là mục tiêu 300s sẽ trượt gấp 72 lần trong khi RTO vẫn xanh.
RTO xanh không nói gì về RPO.

## 3. Root cause (5 whys)

Câu hỏi không phải "vì sao region-a chết" (vì tôi chạy chaos script). Câu hỏi là:
*nếu đây là outage thật, bước nào trong runbook của tôi sẽ thất bại?*

1. **Vì sao user mất 18.1s?** Vì traffic vẫn trỏ vào region-a suốt 15.8s đầu.
2. **Vì sao vẫn trỏ vào region-a?** Vì cutover chỉ chạy sau khi health check kết luận
   UNHEALTHY, và kết luận đó mất 14.9s.
3. **Vì sao mất 14.9s?** Vì `interval × threshold = 15s`, và không thể ngắn hơn nếu vẫn
   muốn chống flapping bằng số lần fail liên tiếp.
4. **Vì sao phải chống flapping bằng cách chờ?** Vì không có tín hiệu nào khác để phân
   biệt "region chết" với "một probe rơi". Chỉ có một loại bằng chứng: probe lặp lại.
5. **Vì sao chỉ có một loại bằng chứng?** Vì health checker chỉ nhìn `/readyz` từ bên
   ngoài. Nó không có tín hiệu độc lập (upstream LB error rate, số connection đang treo,
   loadgen error rate) để cross-check. → **Root cause: quan sát đơn nguồn.**

**Bước sẽ thất bại trong outage thật:** `2_restore_snapshot`. Ở lab, snapshot chỉ là
copy file trong 0.04s. Trong outage thật, region-a mất hẳn nghĩa là object store
cross-region phải còn sống *và* index phải tương thích với `embed_model_version`
(`embed-model=vi-e5-base@v3`, `reports/failover-events.jsonl:2`). Runbook hiện tại
không có bước nào *xác minh* version của model weights ở region-b khớp với version
đã sinh ra index — nó chỉ ghi lại version rồi đi tiếp.

## 4. Action items

| # | Action | Owner | Deadline | Giảm RTO/RPO bao nhiêu giây |
|---|---|---|---|---|
| 1 | Giữ region-b ở `pool_state=full` (warm standby) — lần chạy này đã ở trạng thái đó, cần biến nó thành cấu hình chuẩn chứ không phải tình cờ | Infra on-call | 2026-09-08 | đã hiện thực: warm-up 6.3s → 0.2s, đổi lại GPU idle 24/7 |
| 2 | Cross-check detect bằng error rate ở edge, không chỉ probe `/readyz`; hạ `threshold` xuống 2 khi cả hai nguồn cùng báo | SRE | 2026-09-15 | −5s RTO (floor 15s → 10s) mà không tăng nguy cơ flap |
| 3 | Thêm bước `verify_embed_model_version` vào `dr/failover.py` giữa bước 2 và 3: abort nếu VERSION của weights ≠ version trong MANIFEST | Tác giả lab | 2026-09-08 | 0s RTO, nhưng chặn một lần restore hỏng âm thầm |
| 4 | Chuyển vector DB sang continuous replication (WAL ship) thay vì snapshot chu kỳ | Data platform | 2026-10-01 | RPO 4.0s → < 1s; ở lịch thật 6h → phút |
| 5 | Định nghĩa + diễn tập failback a←b (hiện chỉ diễn tập một chiều) | Infra on-call | 2026-09-22 | 0s, nhưng bỏ được rủi ro kẹt vĩnh viễn ở region phụ |

## 5. Ba câu hỏi bắt buộc trả lời

**1. `interval × threshold` của bạn là bao nhiêu giây? Nó chiếm bao nhiêu % RTO?**
5s × 3 = 15.0s (`detect_floor_s` ghi ngay trong `reports/health-events.jsonl:1`). Thực
đo 14.9s vì cú kill rơi vào giữa hai lần poll. 14.9 / 18.1 = **82.3% RTO**. Đây là thành
phần lớn nhất, gấp gần 5 lần tổng của cả ba thành phần còn lại (3.2s).

**2. Nếu hạ interval xuống 1s, RTO giảm mấy giây — và bạn trả giá gì?**
Floor còn 1 × 3 = 3s, RTO ước tính còn ~6.1s (giảm ~12s). Giá phải trả: cửa sổ bằng
chứng co từ 15s xuống 3s, nên bất kỳ sự cố thoáng qua nào dài hơn 3s — GC pause, một
đợt request nặng làm `/readyz` chậm hơn timeout 2s, một nhịp mạng — đều đủ để kết luận
"outage" và kích hoạt cutover. Đó chính là flapping ở §4 Anti-Patterns: hệ thống failover
sang B, B nhận toàn bộ tải rồi cũng chậm, health check lại thấy B `UNHEALTHY` và lật
ngược về A. Mỗi lần lật là một lần restore snapshot + warm-up + TTL, tức là mỗi lần
chống flapping thất bại lại *cộng* thêm downtime chứ không trừ. Muốn hạ interval an
toàn thì phải mua bằng thứ khác: nguồn tín hiệu thứ hai (action item #2) hoặc circuit
breaker giới hạn số lần cutover mỗi giờ — không phải bằng cách vặn nhỏ con số.

**3. Nếu outage kéo dài 6 giờ và region chính mất dữ liệu vĩnh viễn, `docs_lost` của
bạn có nghĩa gì với khách hàng?**
`docs_lost:2` (`reports/failover-events.jsonl:2`) không phải "2 dòng trong SQLite". Đó
là 2 ticket khách hàng đã gửi, hệ thống đã trả lời "đã nhận", và bây giờ không tồn tại
ở đâu nữa. Không ai báo cho khách biết, vì chính chúng ta cũng không biết đó là ticket
nào — RPO tính bằng giây không cho biết *ai* mất gì. Với 2 doc, việc phải làm là dump
khoảng `ingested_at` giữa `latest_doc_ts` của snapshot (`reports/replication.jsonl:2`)
và thời điểm outage, đối chiếu với log của tầng ingest phía trước để liên hệ lại từng
khách. Nếu chạy lịch backup thật 6h thay vì 30s, cùng tốc độ ingest 0.5 doc/s, con số
đó là ~10.800 document — quá lớn để liên hệ tay, và lúc đó `docs_lost` không còn là
một chỉ số kỹ thuật mà là một sự cố mất dữ liệu phải công bố.
