"""meta-eval：用知识库种子案例验证 review 召回率（需真 API key）。

业界无 review 召回率公开基准，用项目自己的真实踩坑案例集做自建评测：
为每条种子案例构造一个"含该 bug 的方案片段"探针，跑 review_plan（真 LLM），统计：
- 该 bug 是否被至少一个模型标出（召回）
- 是否引用对 case_ref
- consensus/majority 命中情况

需配 OPENAI_API_KEY / ANTHROPIC_API_KEY / ARK_API_KEY。手动跑：
    uv run --extra dev python scripts/meta_eval.py

结果用于迭代 prompt 模板 / reviewer checklist。v2 同源数据驱动 Review Memory + 模型可信度。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
UNITY_PROJECT = ROOT.parent  # My project

from design_review.adapters.unity import UnityAdapter
from design_review.core import ReviewDocument
from design_review.core.engine import ReviewEngine
from design_review.core.stages import build_default_pipeline
from design_review.knowledge import YamlKnowledgeProvider
from design_review.providers import LiteLLMBackend

# 每条种子案例的"含 bug 方案"探针（人工构造，模拟真实会写出的错误代码/方案）
PROBES = {
    "ECS-BURST-001": "在 ISystem.OnUpdate 里调 [BurstCompile] static void Foo(MyStruct s) 按值传 struct。",
    "ECS-BURST-002": "在 [BurstCompile] 方法里读 static bool EnableTiming 这个运行时开关决定是否计时。",
    "ECS-BURST-003": "在 Burst job 里用 Stopwatch.GetTimestamp() 做高精度计时。",
    "NET-001": "在 prediction system 里读 IInputComponentData.***REMOVED*** 组件值判断当前瞄准状态。",
    "NET-003": "ApplyDamage 里直接读 Health 组件判断实体是否可伤害。",
    "NET-005": "用 SystemAPI.Query<***REMOVED***>() 遍历带 [GhostEnabledBit] 的组件。",
    "ECS-STRUCT-001": "托管 OnUpdate 里 foreach 遍历 DynamicBuffer 的同时 EntityManager.CreateEntity。",
    "ECS-STRUCT-002": "if (query == null) query = GetEntityQuery(...); 然后 query.ToEntityArray()。",
    "ECS-STRUCT-004": "OnUpdate 里 GetSingleton<T>() 但 OnCreate 没配 RequireForUpdate<T>()。",
    "FF-001": "FlowField cost field 只从 GreedyRectBlockBuffer 取数据生成，不查物理 collider。",
}


async def main() -> None:
    a = UnityAdapter(str(UNITY_PROJECT))
    kp = YamlKnowledgeProvider(a.knowledge_dir())
    eng = ReviewEngine(
        adapter=a, backend=LiteLLMBackend(), knowledge=kp,
        pipeline=build_default_pipeline(),
    )
    panel = ["claude-opus-4-8", "gpt-5"]
    results = []
    for case_id, probe in PROBES.items():
        doc = ReviewDocument.markdown(probe)
        ctx = await eng.review(doc, panel=panel, dimensions=["ecs_perf", "netcode", "safety"])
        r = ctx.report
        all_c = r.consensus + r.majority
        # 召回：有 finding 的 case_ref 命中，或 consensus/majorory 非空（语义命中需人工复核）
        ref_hit = any(c.case_ref == case_id for c in all_c)
        results.append(
            {"case": case_id, "ref_hit": ref_hit, "consensus": len(r.consensus),
             "majority": len(r.majority), "summary": r.summary}
        )
        print(f"{case_id}: ref_hit={ref_hit} consensus={len(r.consensus)} majority={len(r.majority)} | {r.summary}")

    ref_recalled = sum(1 for x in results if x["ref_hit"])
    flagged = sum(1 for x in results if x["consensus"] or x["majority"])
    n = len(results)
    print(f"\n=== meta-eval 汇总 ({n} 探针) ===")
    print(f"case_ref 精确命中: {ref_recalled}/{n} = {ref_recalled/n:.0%}")
    print(f"有共识/多数发现(语义命中需人工复核): {flagged}/{n} = {flagged/n:.0%}")
    print("\n注：ref_hit 是精确指标（模型填对 case_ref）；语义命中需人工看 finding 是否对应 bug。")


if __name__ == "__main__":
    asyncio.run(main())
