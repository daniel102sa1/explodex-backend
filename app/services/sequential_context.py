from __future__ import annotations

from typing import Any

from app.services.sequential_microstructure import observe_sequential_microstructure


def apply_sequential_context(
    scored: dict[str, Any],
    snapshot: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    if not prediction:
        return prediction

    result = dict(prediction)
    context = dict(result.get("context_engine") or {})
    micro = dict(context.get("microstructure") or {})
    sequence = dict(result.get("sequence") or {})

    symbol = str(snapshot.get("symbol") or scored.get("symbol") or "UNKNOWN")
    current_price = float(scored.get("current_price") or micro.get("current_price") or 0.0)
    futures_delta = micro.get("futures_delta_ratio")
    sequential = observe_sequential_microstructure(
        symbol=symbol,
        book=dict(snapshot.get("order_book") or {}),
        current_price=current_price,
        futures_delta=float(futures_delta) if futures_delta is not None else None,
    )

    micro["ofi"] = sequential.get("ofi")
    micro["replenishment"] = sequential.get("replenishment")
    micro["replenishment_side"] = sequential.get("replenishment_side")
    micro["liquidity_speed"] = sequential.get("liquidity_speed")
    micro["imbalance_speed_per_sec"] = sequential.get("imbalance_speed_per_sec")
    micro["sequential_absorption"] = sequential.get("sequential_absorption")
    micro["sequential_absorption_label"] = sequential.get("sequential_absorption_label")
    micro["sequential_ready"] = sequential.get("ready")
    micro["sequential_snapshot_count"] = sequential.get("snapshot_count")
    micro["sequential_window_seconds"] = sequential.get("window_seconds")
    micro["data_note"] = sequential.get("data_note")

    direction = str(result.get("direction") or scored.get("direction") or "LONG")
    side = 1.0 if direction == "LONG" else -1.0
    sequential_score_delta = 0.0
    sequential_conflicts: list[str] = []
    sequential_confirmations: list[str] = []

    ofi = sequential.get("ofi")
    if ofi is not None:
        aligned_ofi = float(ofi) * side
        if aligned_ofi >= 0.18:
            sequential_score_delta += 9.0
            sequential_confirmations.append("OFI secuencial acompaña")
        elif aligned_ofi <= -0.18:
            sequential_score_delta -= 9.0
            sequential_conflicts.append("OFI secuencial contrario")

    replenishment = sequential.get("replenishment")
    if replenishment is not None:
        aligned_rep = float(replenishment) * side
        if aligned_rep > 0.5:
            sequential_score_delta += 6.0
            sequential_confirmations.append("reposición de liquidez acompaña")
        elif aligned_rep < -0.5:
            sequential_score_delta -= 6.0
            sequential_conflicts.append("reposición de liquidez contraria")

    absorption = sequential.get("sequential_absorption")
    if absorption is not None:
        aligned_absorption = float(absorption) * side
        if aligned_absorption > 0.5:
            sequential_score_delta += 8.0
            sequential_confirmations.append("absorción secuencial acompaña")
        elif aligned_absorption < -0.5:
            sequential_score_delta -= 8.0
            sequential_conflicts.append("absorción secuencial contraria")

    base_score = float(micro.get("score") or 50.0)
    if sequential.get("ready"):
        micro["score_before_sequential"] = round(base_score, 1)
        micro["sequential_score_delta"] = round(sequential_score_delta, 1)
        micro["score"] = round(max(0.0, min(100.0, base_score + sequential_score_delta)), 1)
        micro["aligned"] = micro["score"] >= 58.0
        micro["strong_conflict"] = micro["score"] <= 32.0 and int(micro.get("available_inputs") or 0) >= 2

    confirmations = list(result.get("confirmations") or []) + sequential_confirmations
    conflicts = list(result.get("conflicts") or []) + sequential_conflicts
    result["confirmations"] = list(dict.fromkeys(confirmations))[:16]
    result["conflicts"] = list(dict.fromkeys(conflicts))[:18]

    sequence.update({
        "sequential_microstructure_ready": bool(sequential.get("ready")),
        "sequential_snapshot_count": int(sequential.get("snapshot_count") or 0),
        "ofi": sequential.get("ofi"),
        "replenishment": sequential.get("replenishment"),
        "liquidity_speed": sequential.get("liquidity_speed"),
        "sequential_absorption": sequential.get("sequential_absorption"),
    })
    result["sequence"] = sequence

    context["version"] = "regime-micro-sequential-v1"
    context["microstructure"] = micro
    context["sequential_microstructure"] = sequential
    result["context_engine"] = context
    return result
