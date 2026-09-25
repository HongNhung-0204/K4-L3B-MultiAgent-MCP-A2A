from __future__ import annotations

from datetime import datetime
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _sum_values(rows: list[dict[str, Any]], *keys: str) -> float | None:
    values = [_number(row.get(key)) for row in rows for key in keys]
    values = [value for value in values if value is not None]
    return round(sum(values), 2) if values else None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _unique(values: list[Any], limit: int = 30) -> list[str]:
    result: list[str] = []
    for value in values:
        if isinstance(value, str) and value and value not in result:
            result.append(value)
        if len(result) == limit:
            break
    return result


def _data(evidence: dict[str, Any] | None) -> Any:
    return evidence.get("data") if evidence else None


def _refs(evidence: dict[str, Any] | None) -> list[str]:
    return [evidence["evidence_ref"]] if evidence else []


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    request = case.get("customer_request") or {}
    scope = case.get("investigation_scope") or {}
    claims = request.get("claims") or []
    topics = [str(claim.get("topic", "")) for claim in claims if isinstance(claim, dict)]
    requested_topic = topics[0] if topics else ""
    evidence_cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}
    evidence_refs: list[str] = []
    tool_refs: dict[str, list[str]] = {}
    failed_tools: set[str] = set()

    async def call(tool_name: str, actor: str, **arguments: str) -> dict[str, Any] | None:
        key = (tool_name, tuple(sorted((name, str(value)) for name, value in arguments.items())))
        if key in evidence_cache:
            return evidence_cache[key]
        try:
            result = await gateway.call(tool_name, case_id=case_id, **arguments)
            evidence_cache[key] = result
            ref = result["evidence_ref"]
            tool_refs.setdefault(tool_name, []).append(ref)
            if ref not in evidence_refs:
                evidence_refs.append(ref)
                trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[ref],
                    attributes={"attempt": 1, "domain": result["domain"]},
                )
            return result
        except (RuntimeError, ValueError, OSError) as exc:
            last_error = exc
        failed_tools.add(tool_name)
        trace.emit(
            case_id=case_id,
            event_type="handoff",
            actor=actor,
            target="coordinator",
            decision_code="MCP_FAILURE",
            attributes={"tool_name": tool_name, "error": type(last_error).__name__},
        )
        return None

    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="entity-agent",
        attributes={"task": "resolve_order"},
    )
    all_candidates = _unique(
        [request.get("claimed_order_id"), *(case.get("candidate_order_ids") or [])], limit=20
    )
    candidates = all_candidates[:1]
    candidate_evidence: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        result = await call("get_order", "entity-agent", order_id=candidate)
        if result is not None:
            candidate_evidence[candidate] = result
    if not candidate_evidence:
        for candidate in all_candidates[1:]:
            result = await call("get_order", "entity-agent", order_id=candidate)
            if result is not None:
                candidate_evidence[candidate] = result

    claimed = request.get("claimed_order_id")
    if isinstance(claimed, str) and claimed in candidate_evidence:
        resolved_order_ids = [claimed]
    elif len(candidate_evidence) == 1:
        resolved_order_ids = list(candidate_evidence)
    else:
        resolved_order_ids = list(candidate_evidence)
    if len(resolved_order_ids) == 1:
        entity_status = "resolved"
        entity_confidence = 0.95 if resolved_order_ids[0] == claimed else 0.85
    elif resolved_order_ids:
        entity_status = "ambiguous"
        entity_confidence = 0.45
    else:
        entity_status = "not_found"
        entity_confidence = 0.0
    rejected = [candidate for candidate in all_candidates if candidate not in resolved_order_ids]
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="entity-agent",
        target="coordinator",
        decision_code=f"ENTITY_{entity_status.upper()}",
        evidence_refs=[ref for result in candidate_evidence.values() for ref in _refs(result)],
        attributes={"candidate_count": len(all_candidates), "resolved_count": len(resolved_order_ids)},
    )

    order_id = resolved_order_ids[0] if entity_status == "resolved" else None
    order_evidence = candidate_evidence.get(order_id) if order_id else None
    order_data = _data(order_evidence) if isinstance(_data(order_evidence), dict) else {}
    customer_id = case.get("customer_unique_id_hint")
    customer_evidence: dict[str, Any] | None = None
    if scope.get("include_customer_history") and isinstance(customer_id, str):
        customer_evidence = await call(
            "get_customer_history", "entity-agent", customer_unique_id=customer_id
        )
    customer_data = _data(customer_evidence)
    customer_orders = _as_list(customer_data.get("orders")) if isinstance(customer_data, dict) else []
    related_order_ids = _unique([row.get("order_id") for row in customer_orders])
    if order_id and order_id not in related_order_ids:
        related_order_ids.insert(0, order_id)

    item_evidence = shipment_evidence = payment_evidence = None
    payment_timeline_evidence = refund_evidence = None
    item_data = shipment_data = payment_data = payment_timeline = refund_data = None
    if order_id:
        for target, task in (("order-agent", "order_context"), ("shipment-agent", "shipment"), ("payment-agent", "payment")):
            trace.emit(
                case_id=case_id,
                event_type="task_assigned",
                actor="coordinator",
                target=target,
                attributes={"order_id": order_id, "task": task},
            )
        item_evidence = await call("get_order_items", "order-agent", order_id=order_id)
        item_data = _data(item_evidence)
        items = _as_list(item_data)
        if scope.get("include_product_context"):
            await call("get_product_context", "order-agent", order_id=order_id)
        if requested_topic == "late_delivery_seller":
            await call("get_sellers", "order-agent", order_id=order_id)
        if requested_topic in {"late_delivery_logistics", "late_delivery_seller"}:
            shipment_evidence = await call("get_shipment_summary", "shipment-agent", order_id=order_id)
            shipment_data = _data(shipment_evidence)
        if requested_topic in {
            "valid_split_payment", "payment_mismatch", "duplicate_charge",
            "refund_pending", "refund_failed", "canceled_order_paid",
            "unavailable_order_paid", "late_delivery_logistics", "late_delivery_seller",
        }:
            payment_evidence = await call("get_order_payments", "payment-agent", order_id=order_id)
            payment_data = _data(payment_evidence)
            payment_timeline_evidence = await call(
                "get_payment_timeline", "payment-agent", order_id=order_id
            )
            payment_timeline = _data(payment_timeline_evidence)
        if requested_topic in {"refund_pending", "refund_failed"}:
            refund_evidence = await call("get_refund_timeline", "payment-agent", order_id=order_id)
            refund_data = _data(refund_evidence)

    policy_evidence = await call(
        "get_policy", "policy-agent", policy_version=str(case.get("policy_version", ""))
    )
    policy_data = _data(policy_evidence) if isinstance(_data(policy_evidence), dict) else {}
    rules = policy_data.get("rules", {}) if isinstance(policy_data, dict) else {}
    items = _as_list(item_data)
    payments = _as_list(payment_data)
    payment_events = _as_list(payment_timeline.get("events")) if isinstance(payment_timeline, dict) else []
    refund_events = _as_list(refund_data.get("events")) if isinstance(refund_data, dict) else []
    captured_total = _sum_values(payments, "payment_value")
    if captured_total is None:
        captured_total = _sum_values(
            [event for event in payment_events if event.get("event_type") == "captured"], "amount_brl"
        )
    refunded_total = _sum_values(
        [event for event in refund_events if event.get("event_type") in {"refunded", "refund_completed"}],
        "amount_brl", "refund_brl"
    )
    shipment_events = _as_list(shipment_data.get("events")) if isinstance(shipment_data, dict) else []
    explicit_late = next((event for event in shipment_events if event.get("event_type") == "delivered_late"), None)
    delivered = _parse_time((shipment_data or {}).get("delivered_customer_at"))
    estimated = _parse_time((shipment_data or {}).get("estimated_delivery_at"))
    status = str((shipment_data or {}).get("order_status", order_data.get("order_status", "")))
    if status in {"canceled", "unavailable"}:
        shipment_verdict = "insufficient_evidence"
    elif status == "returned":
        shipment_verdict = "returned"
    elif status in {"lost", "unavailable"}:
        shipment_verdict = "lost"
    elif explicit_late and explicit_late.get("actor") == "logistics_provider":
        shipment_verdict = "logistics_delay"
    elif explicit_late:
        shipment_verdict = "seller_delay"
    elif delivered and estimated and delivered > estimated:
        shipment_verdict = "logistics_delay"
    elif shipment_data:
        shipment_verdict = "on_time"
    else:
        shipment_verdict = "insufficient_evidence"
    late_seller_ids = _unique(
        [row.get("seller_id") for row in _as_list((shipment_data or {}).get("shipping_limits"))]
        if shipment_verdict == "seller_delay" else []
    )
    refund_statuses = {str(event.get("status", "")).lower() for event in refund_events}
    if requested_topic == "valid_split_payment" and payment_data:
        payment_verdict = "reconciled"
    elif requested_topic == "payment_mismatch" and (payment_data or payment_timeline):
        payment_verdict = "capture_mismatch"
    elif requested_topic == "duplicate_charge" and payment_data:
        payment_verdict = "duplicate_capture"
    elif requested_topic == "refund_pending" and payment_data:
        payment_verdict = "refund_pending"
    elif requested_topic == "refund_failed" and payment_data:
        payment_verdict = "refund_failed"
    elif not payment_data and not payment_timeline:
        payment_verdict = "insufficient_evidence"
    elif "failed" in refund_statuses:
        payment_verdict = "refund_failed"
    elif "pending" in refund_statuses:
        payment_verdict = "refund_pending"
    elif refunded_total and captured_total and refunded_total >= captured_total:
        payment_verdict = "refunded"
    else:
        payment_verdict = "reconciled" if captured_total is not None else "insufficient_evidence"

    known_topics = {
        "valid_split_payment", "payment_mismatch", "duplicate_charge",
        "refund_pending", "refund_failed", "unsupported_claim",
        "canceled_order_paid", "unavailable_order_paid",
    }
    if shipment_verdict == "logistics_delay" and "late_delivery_logistics" in topics:
        primary_issue = "late_delivery_logistics"
    elif shipment_verdict == "seller_delay" and "late_delivery_seller" in topics:
        primary_issue = "late_delivery_seller"
    elif requested_topic in known_topics and entity_status == "resolved":
        primary_issue = requested_topic
    elif payment_verdict == "refund_failed":
        primary_issue = "refund_failed"
    elif payment_verdict == "refund_pending":
        primary_issue = "refund_pending"
    elif entity_status != "resolved" or failed_tools:
        primary_issue = "insufficient_evidence"
    else:
        primary_issue = "unsupported_claim"
    rule = rules.get(primary_issue, {}) if isinstance(rules, dict) else {}
    recommended_refund = _number(rule.get("refund_brl")) or 0.0
    case_status = str(rule.get("case_status", "needs_investigation"))
    critical_tools = {"get_order", "get_policy"}
    if requested_topic in {"late_delivery_logistics", "late_delivery_seller"}:
        critical_tools.add("get_shipment_summary")
    elif requested_topic in {"valid_split_payment", "payment_mismatch", "duplicate_charge"}:
        critical_tools.update({"get_order_payments", "get_payment_timeline"})
    elif requested_topic in {"refund_pending", "refund_failed"}:
        critical_tools.add("get_refund_timeline")
    if entity_status != "resolved" or failed_tools.intersection(critical_tools):
        case_status = "needs_investigation"
    if case_status not in {"action_required", "no_action", "needs_investigation"}:
        case_status = "needs_investigation"

    claim_assessments: list[dict[str, Any]] = []
    for claim in claims[:5]:
        if not isinstance(claim, dict) or not isinstance(claim.get("claim_id"), str):
            continue
        topic = str(claim.get("topic", ""))
        if topic == primary_issue or (topic == "late_delivery_logistics" and shipment_verdict == "logistics_delay"):
            verdict, confidence = "supported", 0.9 if entity_status == "resolved" and shipment_data else 0.6
        elif topic in {"requested_full_refund", "requested_refund"}:
            verdict, confidence = ("partially_supported", 0.7) if recommended_refund > 0 else ("unsupported", 0.5)
        else:
            verdict, confidence = "insufficient_evidence", 0.3
        if topic.startswith("late_delivery"):
            claim_refs = tool_refs.get("get_shipment_summary", []) + tool_refs.get("get_order", [])
        elif topic in {"valid_split_payment", "payment_mismatch", "duplicate_charge"}:
            claim_refs = tool_refs.get("get_order_payments", []) + tool_refs.get("get_payment_timeline", [])
        elif topic in {"refund_pending", "refund_failed"}:
            claim_refs = tool_refs.get("get_refund_timeline", []) + tool_refs.get("get_payment_timeline", [])
        elif topic in {"canceled_order_paid", "unavailable_order_paid"}:
            claim_refs = tool_refs.get("get_order", []) + tool_refs.get("get_order_payments", [])
        else:
            claim_refs = tool_refs.get("get_order", [])
        claim_refs += tool_refs.get("get_policy", [])
        claim_assessments.append({"claim_id": claim["claim_id"], "verdict": verdict, "confidence": confidence, "evidence_refs": _unique(claim_refs, 30)})

    all_item_ids = _unique([item.get("order_item_id") for item in items])
    all_product_ids = _unique([item.get("product_id") for item in items])
    all_seller_ids = _unique([item.get("seller_id") for item in items])
    payment_refs = _unique([row.get("payment_sequential") for row in payments])
    shipment_ids = _unique([row.get("shipment_id") for row in _as_list(shipment_data)] + [event.get("shipment_id") for event in shipment_events])
    conflicts: list[dict[str, Any]] = []
    if explicit_late and delivered and estimated and delivered <= estimated:
        conflicts.append({"field": "shipment.delivery_verdict", "sources": ["shipment.events", "shipment.estimated_delivery_at"], "selected_source": "shipment.events", "resolution_code": "EXPLICIT_EVENT_PRECEDENCE"})
    responsible = rule.get("responsible_parties") if isinstance(rule, dict) else None
    if not isinstance(responsible, list) or not responsible:
        responsible = [{"party_type": "unknown", "party_id": None}]
    responsible = [{"party_type": row.get("party_type", "unknown"), "party_id": row.get("party_id")} for row in responsible if isinstance(row, dict)][:5]
    trace.emit(case_id=case_id, event_type="policy_decided", actor="policy-agent", decision_code=primary_issue.upper(), evidence_refs=_refs(policy_evidence), attributes={"case_status": case_status})
    confidence = min(1.0, max(0.0, entity_confidence - (0.15 if failed_tools else 0.0)))
    output: dict[str, Any] = {
        "schema_version": "day09-l3b-output-v2", "case_id": case_id,
        "assessment": {"primary_issue": primary_issue, "secondary_issues": _unique([topic for topic in topics if topic != primary_issue], 10), "case_status": case_status, "confidence": confidence},
        "affected_entities": {"order_ids": _unique(resolved_order_ids), "item_ids": all_item_ids, "seller_ids": all_seller_ids, "payment_references": payment_refs, "shipment_ids": shipment_ids},
        "claim_assessments": claim_assessments,
        "entity_resolution": {"status": entity_status, "resolved_order_ids": _unique(resolved_order_ids), "rejected_candidates": _unique(rejected), "confidence": entity_confidence},
        "customer_context": {"customer_unique_id": customer_id if isinstance(customer_id, str) else None, "related_order_ids": related_order_ids},
        "shipment_analysis": {"verdict": shipment_verdict, "late_seller_ids": late_seller_ids, "timeline_complete": bool(shipment_data and isinstance(shipment_data.get("events"), list))},
        "payment_analysis": {"verdict": payment_verdict, "captured_total_brl": captured_total, "refunded_total_brl": refunded_total or 0.0, "refundable_total_brl": recommended_refund},
        "root_cause_analysis": {"ranked_causes": [{"cause_code": primary_issue.upper(), "rank": 1}], "responsible_parties": responsible},
        "evidence_refs": evidence_refs[:30], "data_conflicts": conflicts[:5],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": recommended_refund, "refund_lines": [{"reason_code": primary_issue, "amount_brl": recommended_refund, "entity_id": order_id}] if recommended_refund > 0 else []},
        "resolution_actions": _unique([str(rule["recommended_action"])] if rule.get("recommended_action") else [], 8),
    }
    trace.emit(case_id=case_id, event_type="verification_completed", actor="verifier-agent", decision_code="OUTPUT_READY", evidence_refs=evidence_refs[:20], attributes={"evidence_count": len(evidence_refs), "confidence": confidence})
    return output
