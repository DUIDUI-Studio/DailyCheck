"""Layer 1: grams_to_stock 纯函数单元测试。"""
from blueprints._helpers import grams_to_stock


def test_basic_conversion():
    # 1440 克，1 袋=1000 克 → 1.44 袋
    assert grams_to_stock(1440, 1000) == 1.44


def test_disabled_returns_grams_unchanged():
    # gram_per_unit=0 表示未启用克，原样返回（此时入参其实是库存单位量）
    assert grams_to_stock(6, 0) == 6


def test_disabled_negative_guard():
    # 负的 gram_per_unit 也按未启用处理
    assert grams_to_stock(5, -1) == 5


def test_another_rate():
    # 2880 克，1 罐=2000 克 → 1.44 罐
    assert grams_to_stock(2880, 2000) == 1.44


def test_tiny_quantity():
    # 10 克，1 瓶=500 克 → 0.02 瓶
    assert grams_to_stock(10, 500) == 0.02


def test_rounding_to_two_dp():
    # 1000 克，1 袋=3 克 → 333.33（量化 2 位）
    assert grams_to_stock(1000, 3) == 333.33


def test_zero_grams():
    assert grams_to_stock(0, 1000) == 0.0
