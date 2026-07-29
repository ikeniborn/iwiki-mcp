from __future__ import annotations

from dataclasses import replace

from .fixtures import BenchmarkCase
from .metrics import ndcg_at_k


_SEVERITY_WEIGHT = {
    "low": 1,
    "medium": 2,
    "high": 3,
}


def _first_ranks(identities: list[str]) -> dict[str, int]:
    ranks = {}
    for rank, identity in enumerate(identities, 1):
        ranks.setdefault(identity, rank)
    return ranks


def _relevant(case: BenchmarkCase, trace: dict) -> dict[str, int]:
    metrics_input = trace.get("metrics_input", {})
    if "relevant" in metrics_input:
        return dict(metrics_input["relevant"])
    return dict(case.relevant)


def _ranking(trace: dict) -> list[str]:
    return list(trace.get("metrics_input", {}).get("ranking", []))


def _fusion_candidates(trace: dict) -> tuple[list[str], bool]:
    fusion = trace.get("stages", {}).get("fusion", {})
    if "candidate_identities" not in fusion:
        return [], False
    return list(fusion.get("candidate_identities", [])), True


def _hydration(trace: dict) -> dict:
    return dict(trace.get("stages", {}).get("hydration", {}))


def _rerank(trace: dict) -> dict:
    return dict(trace.get("stages", {}).get("rerank", {}))


def _index_relevant(trace: dict, identity: str) -> dict | None:
    relevant = trace.get("stages", {}).get("index", {}).get("relevant", {})
    value = relevant.get(identity)
    return dict(value) if isinstance(value, dict) else None


def _signal_counts(trace: dict) -> dict:
    return dict(trace.get("stages", {}).get("signals", {}).get("counts", {}))


def _signal_presence(trace: dict, identity: str) -> list[str] | None:
    signals = trace.get("stages", {}).get("signals", {})
    presence = signals.get("relevant_presence", {})
    if identity in presence:
        return list(presence.get(identity, []))
    identities = signals.get("identities")
    if not isinstance(identities, dict):
        return None
    return [
        name for name, values in sorted(identities.items())
        if identity in set(values)
    ]


def _candidate_pool_loss(trace: dict, case: BenchmarkCase, identity: str,
                         grade: int, candidate_count: int) -> dict:
    index = _index_relevant(trace, identity)
    if index is not None:
        evidence = {
            "candidate_count": candidate_count,
            "relevance_grade": grade,
            "index": index,
        }
        if not index.get("chunk_present"):
            return {
                "case_id": case.case_id,
                "class": "missing_from_chunks",
                "severity": "high",
                "identity": identity,
                "evidence": evidence,
            }
        if not index.get("indexed"):
            return {
                "case_id": case.case_id,
                "class": "missing_from_index",
                "severity": "high",
                "identity": identity,
                "evidence": evidence,
            }
        if index.get("hash_matches") is False:
            return {
                "case_id": case.case_id,
                "class": "stale_index_chunk",
                "severity": "high",
                "identity": identity,
                "evidence": evidence,
            }
        if index.get("embedding_dim_matches") is False:
            return {
                "case_id": case.case_id,
                "class": "embedding_dimension_mismatch",
                "severity": "high",
                "identity": identity,
                "evidence": evidence,
            }

    presence = _signal_presence(trace, identity)
    if presence is not None:
        signal_evidence = {
            "candidate_count": candidate_count,
            "relevance_grade": grade,
            "mode": trace.get("mode"),
            "signal_counts": _signal_counts(trace),
            "signal_presence": presence,
        }
        return {
            "case_id": case.case_id,
            "class": (
                "lost_during_fusion" if presence else "signal_recall_miss"
            ),
            "severity": "high",
            "identity": identity,
            "evidence": signal_evidence,
        }

    return {
        "case_id": case.case_id,
        "class": "missing_from_candidate_pool",
        "severity": "high",
        "identity": identity,
        "evidence": {
            "candidate_count": candidate_count,
            "relevance_grade": grade,
        },
    }


def analyze_trace(case: BenchmarkCase, trace: dict) -> list[dict]:
    relevant = _relevant(case, trace)
    ranking = _ranking(trace)
    candidates, has_candidate_evidence = _fusion_candidates(trace)
    hydration = _hydration(trace)
    rerank = _rerank(trace)

    ranking_set = set(ranking)
    candidate_set = set(candidates)
    has_hydrated_identity_evidence = "hydrated_identities" in hydration
    hydrated_set = set(hydration.get("hydrated_identities", []))
    candidate_ranks = _first_ranks(candidates)
    ranking_ranks = _first_ranks(ranking)

    findings = []
    for identity, grade in sorted(relevant.items()):
        if has_candidate_evidence and identity not in candidate_set:
            findings.append(
                _candidate_pool_loss(trace, case, identity, grade, len(candidates))
            )
            continue

        candidate_rank = candidate_ranks.get(identity)
        if (
            has_candidate_evidence
            and identity not in ranking_set
            and candidate_rank > case.k
        ):
            findings.append({
                "case_id": case.case_id,
                "class": "lost_after_fusion_topk",
                "severity": "medium",
                "identity": identity,
                "evidence": {
                    "candidate_rank": candidate_rank,
                    "k": case.k,
                    "ranking_count": len(ranking),
                    "relevance_grade": grade,
                },
            })

        if (
            has_candidate_evidence
            and hydration.get("requested", 0) > 0
            and has_hydrated_identity_evidence
            and candidate_rank <= case.k
            and identity not in hydrated_set
            and identity not in ranking_set
        ):
            findings.append({
                "case_id": case.case_id,
                "class": "hydration_drop",
                "severity": "medium",
                "identity": identity,
                "evidence": {
                    "hydration_requested": hydration.get("requested", 0),
                    "hydration_hydrated": hydration.get("hydrated", 0),
                    "hydration_dropped": hydration.get("dropped", 0),
                    "relevance_grade": grade,
                },
            })

    metric_case = replace(case, relevant=relevant)
    fusion_ndcg = ndcg_at_k(candidates, metric_case, case.k)
    ranking_ndcg = ndcg_at_k(ranking, metric_case, case.k)
    if (
        has_candidate_evidence
        and rerank.get("applied")
        and ranking_ndcg < fusion_ndcg
    ):
        fusion_relevant = []
        seen_relevant = set()
        for identity in candidates:
            if identity in seen_relevant:
                continue
            if (
                identity in relevant
                and candidate_ranks[identity] <= case.k
                and (
                    hydration.get("requested", 0) <= 0
                    or (
                        has_hydrated_identity_evidence
                        and identity in hydrated_set
                    )
                )
            ):
                fusion_relevant.append(identity)
                seen_relevant.add(identity)
        for fusion_identity in fusion_relevant:
            fusion_rank = candidate_ranks[fusion_identity]
            ranking_rank = ranking_ranks.get(fusion_identity)
            if ranking_rank is None or ranking_rank > fusion_rank:
                findings.append({
                    "case_id": case.case_id,
                    "class": "rerank_worsened_order",
                    "severity": "medium",
                    "identity": fusion_identity,
                    "evidence": {
                        "fusion_rank": fusion_rank,
                        "ranking_rank": ranking_rank,
                        "fusion_ndcg_at_k": round(fusion_ndcg, 6),
                        "ranking_ndcg_at_k": round(ranking_ndcg, 6),
                        "rerank": rerank,
                    },
                })

    selected_relevant = set(relevant) & ranking_set
    finding_identities = {finding["identity"] for finding in findings}
    for identity, grade in sorted(relevant.items()):
        if identity not in ranking_set and identity not in finding_identities:
            findings.append({
                "case_id": case.case_id,
                "class": "unknown_quality_loss",
                "severity": "low",
                "identity": identity,
                "evidence": {
                    "ranking_count": len(ranking),
                    "candidate_count": (
                        len(candidates) if has_candidate_evidence else None
                    ),
                    "selected_relevant_count": len(selected_relevant),
                    "relevance_grade": grade,
                },
            })

    return sorted(
        findings,
        key=lambda finding: (
            finding["case_id"],
            finding["class"],
            finding["identity"],
        ),
    )


def ranked_backlog(findings: list[dict]) -> list[dict]:
    grouped = {}
    for finding in findings:
        class_name = finding["class"]
        severity = finding["severity"]
        entry = grouped.setdefault(
            class_name,
            {
                "class": class_name,
                "count": 0,
                "severity": severity,
            },
        )
        entry["count"] += 1
        if _SEVERITY_WEIGHT[severity] > _SEVERITY_WEIGHT[entry["severity"]]:
            entry["severity"] = severity

    return sorted(
        grouped.values(),
        key=lambda entry: (
            -_SEVERITY_WEIGHT[entry["severity"]],
            -entry["count"],
            entry["class"],
        ),
    )
