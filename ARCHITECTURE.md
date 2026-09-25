# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ input/candidate resolution đến MCP investigation, specialist agents, conflict resolver, verifier, output và trace.

```text
Input → Entity Resolver → Coordinator → Specialists → Policy/Conflict Resolver → Verifier → Output
            │                              │                         │                 │
            └──────────────────────────── MCP evidence ─────────────┴───────────────── Trace
```

`solve_case()` là một async state machine theo từng `case_id`. Coordinator nhận
case, kiểm tra candidate scope và giao các nhiệm vụ độc lập. Specialist chỉ giao
tiếp bằng kết quả có cấu trúc và danh sách `evidence_ref`; không truyền prompt,
chain-of-thought hoặc dữ liệu của case khác. Coordinator hợp nhất kết quả,
Policy/Conflict Resolver áp dụng precedence, rồi Verifier chỉ tạo output sau khi
mọi invariant được kiểm tra.

MCP là nguồn dữ liệu điều tra duy nhất cho order, customer, item, shipment,
payment, refund, product, seller và policy. Mọi response phải được gateway
validate theo `mcp-evidence-response-v1`; `evidence_ref` và `result_hash` được
giữ nguyên từ gateway.

## 2. Agent ownership

| Actor             | Input                                | Trách nhiệm                                                                                | Tool permission                                                           | Output/handoff                                                                                                      |
| ----------------- | ------------------------------------ | ------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| Entity/customer   | `candidate_order_ids`, hints, claims | Xếp hạng candidate; xác định order/customer scope; loại candidate không đủ bằng chứng      | `get_order`, `get_customer_history`                                       | Entity result gồm `resolved_order_ids`, `rejected_candidates`, customer id, confidence và refs; handoff Coordinator |
| Coordinator       | Case và entity result                | Tạo plan, giới hạn query, gom specialist results, điều phối timeout và finalize            | Discovery/listing; chỉ gọi fallback domain tool khi cần                   | Task assignments, handoffs, merged investigation state                                                              |
| Order/product     | Resolved order ids                   | Kiểm tra order status, items, seller và product context                                    | `get_order`, `get_order_items`, `get_product_context`, `get_sellers`      | Order/item facts, entity ids, refs; handoff Coordinator                                                             |
| Shipment          | Resolved order ids                   | Dựng timeline giao hàng và phân biệt seller delay, logistics delay, lost, returned         | `get_shipment_summary`                                                    | Shipment verdict, late sellers, completeness, refs                                                                  |
| Payment/refund    | Resolved order ids                   | Đối soát capture, payment timeline, refund và số tiền BRL                                  | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline`       | Payment verdict, totals, refund facts, refs                                                                         |
| Policy            | Policy version và specialist facts   | Đọc policy, đánh giá điều kiện hỗ trợ/refund và resolution action                          | `get_policy`                                                              | Policy decision code, selected refs, handoff Conflict Resolver                                                      |
| Conflict resolver | Các specialist results               | Phát hiện khác biệt nguồn, áp dụng precedence; không tự bịa dữ liệu khi thiếu              | Không gọi thêm tool nếu đã đủ evidence; chỉ Coordinator cấp query bổ sung | `data_conflicts`, selected source, unresolved status, decision refs                                                 |
| Verifier          | Merged state và policy decision      | Kiểm tra schema, scope, claims, totals, evidence linkage, confidence và action consistency | Không gọi MCP; yêu cầu Coordinator bổ sung evidence nếu thiếu             | Validated L3B output hoặc `needs_investigation` khi không đủ evidence                                               |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

Tool permission là logic của workflow, không phải quyền bảo mật thay thế cho MCP
server. Coordinator phải lấy danh sách tool bằng discovery trước khi chạy và chỉ
thực thi tên tool đã được discover.

## 3. Entity resolution và A2A protocol

Entity Resolver bắt đầu từ các candidate trong input, không quét toàn bộ dữ liệu.
Mỗi candidate được kiểm tra bằng `get_order` với đúng `case_id`. Candidate được
giữ khi response xác nhận các định danh liên quan đến claim; candidate bị loại
phải được ghi trong `rejected_candidates`. `claimed_order_id` là tín hiệu ưu tiên,
không phải bằng chứng tự thân.

Quy tắc quyết định:

- `resolved`: chỉ một order có evidence phù hợp và confidence >= 0.80;
- `ambiguous`: từ hai candidate trở lên còn phù hợp, hoặc điểm cao nhất dưới
  0.80 và chưa có cách phân biệt an toàn;
- `not_found`: không candidate nào có evidence hợp lệ.

Confidence chỉ phản ánh evidence quan sát được. Khi entity không resolved,
workflow không gọi các specialist theo order và output phải giữ trạng thái
`needs_investigation` với evidence đã thu được.

Handoff dùng envelope nội bộ tối thiểu:

```text
case_id, task_id, source_actor, target_actor, status, entity_scope,
evidence_refs, facts, requested_checks
```

Envelope này không được ghi vào output nếu field không có trong schema. Mọi
handoff và task assignment observable được ghi bằng event `handoff` hoặc
`task_assigned`, chỉ chứa metadata scalar trong `attributes`. `case_id` là khóa
correlation bắt buộc; mỗi actor xử lý đúng một case context.

Handoff chỉ xảy ra khi đầu vào và evidence refs đã được validate. Mỗi task có
timeout theo call gateway 30 giây ở workflow và tối đa 2 lần retry; task không
được gửi lại nếu đã có kết quả thành công. Coordinator đánh dấu visited task
theo `(case_id, actor, task_type)` để tránh vòng lặp.

## 4. Evidence và conflict lifecycle

`EvidenceGateway.call()` luôn truyền `case_id`, validate envelope và trả về
response có `schema_version`, `evidence_ref`, `result_hash`, `domain`, `data` và
optional `warnings`. Workflow lưu response trong state của case bằng
`evidence_ref`; không sửa ref, hash hoặc data để tạo evidence mới.

Ngay sau khi specialist tiêu thụ một response, trace một event
`tool_result_consumed` với `actor`, `tool_name` và `evidence_refs`. Evidence
được liên kết vào claim assessments, root cause hoặc financial resolution bằng
chính ref đã nhận. Mỗi case có evidence registry riêng; ref từ case khác bị
từ chối.

Conflict resolver xử lý theo thứ tự: policy version hiện trong case, dữ liệu
chuyên biệt đúng domain, rồi timeline có `result_hash` hợp lệ. Không tự chọn
nguồn chỉ vì nó xuất hiện trước. Mỗi conflict output phải có `field`, ít nhất
hai `sources`, `selected_source` hoặc `null`, và `resolution_code`. Nếu không
thể giải quyết, giữ `selected_source: null`, giảm confidence và chuyển case
sang `needs_investigation` thay vì suy đoán.

## 5. Failure and efficiency policy

| Failure                    |                                       Retry budget | Fallback                                                                            | Trace event/code                                                    |
| -------------------------- | -------------------------------------------------: | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------- |
| MCP timeout                |                                          2 retries | Exponential backoff ngắn; nếu vẫn lỗi dùng evidence đã có và đánh dấu thiếu dữ liệu | `tool_result_consumed` nếu có kết quả; decision code `MCP_TIMEOUT`  |
| Entity not found/ambiguous | 0 cho cùng query; tối đa 1 query phân biệt bổ sung | Không đoán order; output `not_found`/`ambiguous`, status `needs_investigation`      | `handoff`, decision code `ENTITY_UNRESOLVED`                        |
| Source conflict            |                                            0 retry | Áp dụng precedence; nếu chưa đủ thì unresolved conflict                             | `policy_decided`, decision code `SOURCE_CONFLICT`                   |
| Invalid specialist result  |                        1 local repair/revalidation | Bỏ result lỗi, giữ refs hợp lệ; nếu thiếu field bắt buộc thì `needs_investigation`  | `verification_completed`, decision code `INVALID_SPECIALIST_RESULT` |

Query budget mặc định cho mỗi case là 1 lần cho mỗi cặp
`(tool_name, normalized_arguments)`, ngoại lệ tối đa 2 retries khi timeout
hoặc transport failure. Cache chỉ tồn tại trong case context và key luôn chứa
`case_id`; không dùng cache chéo case. Discovery được gọi một lần mỗi run.
Không gọi tool nếu specialist trước đã trả đủ domain facts và evidence refs.
Retry phải idempotent và missing evidence không bao giờ được biến thành giá trị
giả định.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Workflow thuần Python async state machine, không phụ thuộc model ngẫu nhiên.
Dependency và version nằm trong `pyproject.toml`; concurrency mặc định tuần tự
theo case để giữ trace deterministic và giới hạn tải MCP. Chạy bằng:

```text
day09 validate-inputs
day09 mcp-tools
day09 run
day09 validate
day09 package --output dist/submission.zip
```

Không ghi API key, prompt bí mật, chain-of-thought hoặc raw debug payload vào
`ARCHITECTURE.md`, trace hay submission ZIP.
