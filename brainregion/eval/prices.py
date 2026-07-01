"""文档价注册：把智谱 / DeepSeek 官方定价注册进 litellm，让 eval 能量这些模型的 cost。

背景：glm-5.2 / glm-5-turbo / deepseek-v4-flash 等不在 litellm 内置价格表 → cost 记 $0 →
cost_ratio=None（除零已防 stats.py，但 cost 维度恒 INCONCLUSIVE，出不来 cost GO/NO_GO）。
注册文档价后 litellm 能从 usage 算出 cost。

**关键：cost_ratio 里 default/routed 两臂用同一模型 → 价格 P 在分子分母抵消** → cost_ratio 恒
正确（= token 效率比 routed vs default），与价格高低、USD/元换算偏差、coding plan 折扣都无关。
注册真实价只是让绝对 $ 显示也接近真实。

价格来源（元/百万 token，2026-06 拉取）：
- 智谱 https://bigmodel.cn/pricing
- DeepSeek https://api-docs.deepseek.com/quick_start/pricing

glm-4.7-Flash 官方免费（0,0）→ 不注册（cost 恒 0，cost_ratio 仍 None，只能当「真免费但量不出
cost」的测试 panel）。
"""
from __future__ import annotations

import logging

import litellm

logger = logging.getLogger("brainregion.eval.prices")

# USD/元 换算近似（2026-06 ~7.2 元/美元）。**不影响 cost_ratio**——价格在两臂间抵消。
YUAN_PER_USD = 7.2
_USD_PER_YUAN = 1.0 / YUAN_PER_USD

# model -> (输入 元/百万token, 输出 元/百万token, litellm provider)
DOC_PRICES_YUAN_PER_M: dict[str, tuple[float, float, str]] = {
    # 智谱（bigmodel.cn/pricing，0-32k 档）
    "glm-5.2": (8, 28, "anthropic"),
    "glm-5-turbo": (5, 22, "anthropic"),
    "glm-4.7": (2, 8, "anthropic"),
    # DeepSeek（api-docs.deepseek.com，缓存未命中档）
    "deepseek-v4-flash": (1, 2, "openai"),
    "deepseek-v4-pro": (3, 6, "openai"),
}


def register_doc_prices() -> dict[str, dict[str, float]]:
    """把文档价注册进 litellm.model_cost（按裸名——litellm 查 cost 时 strip provider 前缀，
    实测 anthropic/glm-5.2 / openai/deepseek-v4-flash 都落到裸名查找）。返回 {model: {...}}。"""
    out: dict[str, dict[str, float]] = {}
    for model, (in_yuan, out_yuan, provider) in DOC_PRICES_YUAN_PER_M.items():
        entry = {
            "input_cost_per_token": in_yuan * _USD_PER_YUAN / 1e6,
            "output_cost_per_token": out_yuan * _USD_PER_YUAN / 1e6,
            "litellm_provider": provider,
        }
        out[model] = entry
        litellm.register_model({model: entry})
    return out


_REGISTERED = False


def ensure_doc_prices_registered() -> None:
    """幂等注册（eval 编排入口调一次）。失败只 warn，不阻塞 eval。"""
    global _REGISTERED
    if _REGISTERED:
        return
    try:
        register_doc_prices()
        _REGISTERED = True
        logger.debug("已注册文档价: %s", list(DOC_PRICES_YUAN_PER_M))
    except Exception as e:  # noqa: BLE001
        logger.warning("注册文档价失败（cost 可能记 $0）: %s", e)
