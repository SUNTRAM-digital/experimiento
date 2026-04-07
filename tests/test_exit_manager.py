"""
Tests: exit_manager.py
Prueba las tres reglas de salida: stop loss, edge exhausto y swing capture.
Todos unitarios — sin APIs.
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from exit_manager import evaluate_exit, evaluate_exit_batch


def make_position(entry_price=0.50, side="YES", token_id="tok1", condition_id="cid1"):
    return {
        "entry_price":  entry_price,
        "side":         side,
        "token_id":     token_id,
        "condition_id": condition_id,
        "market_title": "Test Market",
    }


# ── evaluate_exit ──────────────────────────────────────────────────────────────

class TestEvaluateExit:

    # --- STOP LOSS ---

    def test_stop_loss_triggered(self):
        # Entramos a 0.60, prob cayo a 0.40 (< 0.60-0.12=0.48), precio bajo a 0.45
        pos = make_position(entry_price=0.60)
        result = evaluate_exit(pos, current_price=0.45, estimated_prob=0.40)
        assert result["should_exit"] is True
        assert result["reason"] == "stop_loss"
        assert result["urgency"] == "high"

    def test_stop_loss_not_triggered_when_prob_ok(self):
        # Prob sigue alta, no hay razon para salir por stop
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.55, estimated_prob=0.70)
        assert result["reason"] != "stop_loss"

    def test_stop_loss_requires_both_conditions(self):
        # Prob cayo pero P&L no esta negativo todavia (-0.03 < -0.05 no se cumple)
        pos = make_position(entry_price=0.60)
        result = evaluate_exit(pos, current_price=0.58, estimated_prob=0.40)
        assert result["reason"] != "stop_loss"

    # --- EDGE EXHAUSTED ---

    def test_edge_exhausted_triggered(self):
        # Precio=0.88, prob estimada=0.90 → edge=0.02 (<0.05), P&L=+76%
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.88, estimated_prob=0.90)
        assert result["should_exit"] is True
        assert result["reason"] == "edge_exhausted"
        assert result["urgency"] == "medium"

    def test_edge_exhausted_not_triggered_if_no_profit(self):
        # Edge pequeno pero estamos en perdida → no salir por esta regla
        pos = make_position(entry_price=0.80)
        result = evaluate_exit(pos, current_price=0.75, estimated_prob=0.78)
        assert result["reason"] != "edge_exhausted"

    # --- SWING CAPTURE ---

    def test_swing_capture_triggered(self):
        # Entrada 0.40, prob 0.80, precio actual 0.75
        # max_gain = 0.80-0.40 = 0.40; capturado = (0.75-0.40)/0.40 = 87.5% > 85%
        pos = make_position(entry_price=0.40)
        result = evaluate_exit(pos, current_price=0.75, estimated_prob=0.80)
        assert result["should_exit"] is True
        assert result["reason"] == "swing_capture"
        assert result["urgency"] == "low"

    def test_swing_capture_not_triggered_below_85pct(self):
        # Capturado = (0.60-0.40)/0.40 = 50% → no se activa
        pos = make_position(entry_price=0.40)
        result = evaluate_exit(pos, current_price=0.60, estimated_prob=0.80)
        assert result["reason"] != "swing_capture"

    def test_swing_capture_requires_min_potential_gain(self):
        # max_possible_gain = 0.04 (<0.05) → regla no aplica
        pos = make_position(entry_price=0.76)
        result = evaluate_exit(pos, current_price=0.795, estimated_prob=0.80)
        assert result["reason"] != "swing_capture"

    # --- HOLD ---

    def test_hold_when_no_rule_triggers(self):
        # Posicion joven con buen edge todavia
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.58, estimated_prob=0.75)
        assert result["should_exit"] is False
        assert result["reason"] == "hold"

    # --- CAMPOS DE RETORNO ---

    def test_result_always_has_required_fields(self):
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.55, estimated_prob=0.70)
        for field in ["should_exit", "reason", "urgency", "details", "unrealized_pnl_pct", "remaining_edge"]:
            assert field in result, f"Falta campo: {field}"

    def test_pnl_calculation_correct(self):
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.60, estimated_prob=0.80)
        # P&L = (0.60-0.50)/0.50 = 20%
        assert result["unrealized_pnl_pct"] == pytest.approx(0.20, abs=0.001)

    def test_remaining_edge_calculation_correct(self):
        pos = make_position(entry_price=0.50)
        result = evaluate_exit(pos, current_price=0.65, estimated_prob=0.80)
        # Edge restante = 0.80 - 0.65 = 0.15
        assert result["remaining_edge"] == pytest.approx(0.15, abs=0.001)


# ── evaluate_exit_batch ────────────────────────────────────────────────────────

class TestEvaluateExitBatch:

    def test_empty_positions_returns_empty(self):
        result = evaluate_exit_batch([], {}, {})
        assert result == []

    def test_only_returns_positions_needing_exit(self):
        positions = [
            make_position(entry_price=0.50, token_id="tok1", condition_id="cid1"),
            make_position(entry_price=0.50, token_id="tok2", condition_id="cid2"),
        ]
        current_prices = {"tok1": 0.88, "tok2": 0.55}   # tok1 edge exhausto, tok2 ok
        estimated_probs = {"cid1": 0.90, "cid2": 0.75}

        exits = evaluate_exit_batch(positions, current_prices, estimated_probs)
        assert len(exits) == 1
        assert exits[0]["token_id"] == "tok1"

    def test_stop_loss_sorted_first(self):
        pos_stop  = make_position(entry_price=0.60, token_id="t1", condition_id="c1")
        pos_swing = make_position(entry_price=0.40, token_id="t2", condition_id="c2")

        current_prices  = {"t1": 0.45, "t2": 0.75}   # 0.75 → 87.5% capturado > 85%
        estimated_probs = {"c1": 0.40, "c2": 0.80}

        exits = evaluate_exit_batch(
            [pos_swing, pos_stop],  # swing primero en la lista
            current_prices, estimated_probs
        )
        assert len(exits) == 2
        assert exits[0]["exit_reason"] == "stop_loss"   # stop_loss debe ir primero

    def test_skips_positions_missing_price(self):
        pos = make_position(token_id="t1", condition_id="c1")
        exits = evaluate_exit_batch([pos], {}, {"c1": 0.70})
        assert exits == []

    def test_skips_positions_missing_prob(self):
        pos = make_position(token_id="t1", condition_id="c1")
        exits = evaluate_exit_batch([pos], {"t1": 0.60}, {})
        assert exits == []


# ── Ejecutar directo ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
