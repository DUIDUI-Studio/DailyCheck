"""多辅单位换算 — 单元测试与集成测试。"""
from blueprints._helpers import aux_to_base, base_to_aux, grams_to_stock

# --- 纯函数单元测试 ---

def test_aux_to_basic():
    assert aux_to_base(1440, 1000) == 1.44

def test_aux_to_base_disabled():
    assert aux_to_base(6, 0) == 6

def test_aux_to_base_negative_rate_disabled():
    assert aux_to_base(5, -1) == 5

def test_aux_to_base_another_rate():
    assert aux_to_base(24, 12) == 2.0  # 24 个 / (1 箱=12 个) = 2 箱

def test_aux_to_base_tiny():
    assert aux_to_base(10, 500) == 0.02

def test_aux_to_base_rounding():
    assert aux_to_base(1000, 3) == 333.33

def test_aux_to_base_zero():
    assert aux_to_base(0, 1000) == 0.0

def test_base_to_aux():
    assert base_to_aux(1.44, 1000) == 1440
def test_base_to_aux_pieces():
    assert base_to_aux(2, 12) == 24
def test_base_to_aux_disabled():
    assert base_to_aux(1.5, 0) == 1.5

def test_grams_to_stock_is_aux_to_base():
    assert grams_to_stock(1440, 1000) == 1.44
