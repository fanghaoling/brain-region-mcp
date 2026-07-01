"""prices.py 单测：文档价注册正确性（元/M → USD/token）+ 裸名覆盖 + 幂等。"""
import litellm

from brainregion.eval.prices import (
    DOC_PRICES_YUAN_PER_M,
    YUAN_PER_USD,
    ensure_doc_prices_registered,
    register_doc_prices,
)


def test_register_doc_prices_correctness():
    register_doc_prices()
    # glm-5.2: 8 元/M in, 28 元/M out → ÷YUAN_PER_USD÷1e6 USD/token
    e = litellm.model_cost["glm-5.2"]
    assert abs(e["input_cost_per_token"] - 8 / YUAN_PER_USD / 1e6) < 1e-12
    assert abs(e["output_cost_per_token"] - 28 / YUAN_PER_USD / 1e6) < 1e-12
    # deepseek-v4-flash: 1/2 元/M
    d = litellm.model_cost["deepseek-v4-flash"]
    assert abs(d["input_cost_per_token"] - 1 / YUAN_PER_USD / 1e6) < 1e-12
    assert abs(d["output_cost_per_token"] - 2 / YUAN_PER_USD / 1e6) < 1e-12


def test_register_doc_prices_bare_names_suffice():
    # litellm 查 cost 时 strip provider 前缀，落到裸名 → 裸名注册即够（实测 live 验证）
    register_doc_prices()
    for model in DOC_PRICES_YUAN_PER_M:
        assert model in litellm.model_cost


def test_ensure_doc_prices_registered_idempotent():
    ensure_doc_prices_registered()
    ensure_doc_prices_registered()  # 二次调用不应报错或重复副作用
    assert "glm-5.2" in litellm.model_cost
