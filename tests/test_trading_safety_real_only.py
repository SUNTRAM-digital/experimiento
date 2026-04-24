"""
Tests for point 8 — daily_loss + max_consec_losses + kill_switch
solo afectan al flujo REAL, nunca al phantom.

Verifica leyendo el source que `_check_real_safety` y `trading_real_killed`
únicamente se invocan dentro de bloques `if is_real`.
"""
import os, sys, ast
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

RUNNER = os.path.join(os.path.dirname(__file__), '..', 'trading_runner.py')


def _src():
    with open(RUNNER, 'r', encoding='utf-8') as f:
        return f.read()


def test_check_real_safety_only_in_real_branch():
    """`_check_real_safety(...)` debe estar dentro de `if is_real:`."""
    src = _src()
    tree = ast.parse(src)
    occurrences = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []  # stack de condiciones `if is_real`
        def visit_If(self, node):
            test = ast.unparse(node.test)
            is_real_branch = "is_real" in test and "not is_real" not in test
            self.stack.append(is_real_branch)
            self.generic_visit(node)
            self.stack.pop()
        def visit_Call(self, node):
            f = ast.unparse(node.func)
            if "_check_real_safety" in f:
                occurrences.append(any(self.stack))
            self.generic_visit(node)

    V().visit(tree)
    assert occurrences, "se esperaba al menos una llamada a _check_real_safety"
    assert all(occurrences), "alguna llamada está fuera de un bloque `if is_real`"


def test_trading_real_killed_set_only_in_real_branch_or_safety_fn():
    """Asignaciones a `trading_real_killed = True` solo dentro de bloque real
    o dentro de la función _check_real_safety (que solo se llama desde rama real)."""
    src = _src()
    tree = ast.parse(src)
    bad = []

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []
            self.in_safety_fn = False
        def visit_FunctionDef(self, node):
            prev = self.in_safety_fn
            if node.name == "_check_real_safety":
                self.in_safety_fn = True
            self.generic_visit(node)
            self.in_safety_fn = prev
        visit_AsyncFunctionDef = visit_FunctionDef
        def visit_If(self, node):
            test = ast.unparse(node.test)
            self.stack.append("is_real" in test and "not is_real" not in test)
            self.generic_visit(node)
            self.stack.pop()
        def visit_Assign(self, node):
            target = ast.unparse(node.targets[0]) if node.targets else ""
            if "trading_real_killed" in target and "True" in ast.unparse(node.value):
                if not (self.in_safety_fn or any(self.stack)):
                    bad.append(node.lineno)
            self.generic_visit(node)
    V().visit(tree)
    assert not bad, f"trading_real_killed=True asignado fuera de rama is_real en líneas {bad}"


def test_phantom_execution_block_does_not_call_real_safety():
    """El bloque que ejecuta apertura phantom no debe invocar safety real."""
    src = _src()
    marker = "# Phantom — simula EXACTAMENTE el path real"
    idx = src.find(marker)
    assert idx > 0, "no se encontró el marker exacto del bloque phantom de apertura"
    # Tomar hasta el final de la función (return pos final del bloque phantom)
    phantom_block = src[idx: idx + 3000]
    assert "_check_real_safety" not in phantom_block, "phantom no debe llamar safety real"
    # En este bloque no deben asignarse flags de kill-switch
    assert "trading_real_killed = True" not in phantom_block
