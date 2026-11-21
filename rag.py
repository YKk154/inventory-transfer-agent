"""Policy retrieval with validity, scope and BM25 ranking."""
from collections import Counter
from datetime import date
from math import log
import re

QUERIES = {
    "donor_reserve": "调出仓 已预留 预计需求 安全库存",
    "arrival": "到货时限 截止时间 停运 线路",
    "budget": "运费 预算 线路 容量",
    "freshness": "库存快照 数据 时效",
    "approval": "人工审批 缺口 部分调拨 说明",
}


def tokenize(text):
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    for span in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(span[i:i + 2] for i in range(max(1, len(span) - 1)))
    return tokens


def retrieve(policies, destination, as_of):
    """Retrieve the applicable version for each decision rule."""
    day = date.fromisoformat(as_of[:10])
    eligible = [
        p for p in policies
        if p["destination"] in ("*", destination)
        and date.fromisoformat(p["effective"]) <= day
        and ("expires" not in p or day <= date.fromisoformat(p["expires"]))
    ]
    corpus = [Counter(tokenize(p["title"] + " " + p["body"])) for p in eligible]
    average = sum(sum(doc.values()) for doc in corpus) / max(1, len(corpus))
    found = {}
    for rule, query in QUERIES.items():
        candidates = []
        for policy, doc in zip(eligible, corpus):
            if policy["rule"] != rule:
                continue
            factor = 1.2 * (0.25 + 0.75 * sum(doc.values()) / average)
            score = 0.0
            for token in sorted(set(tokenize(query))):
                if token in doc:
                    df = sum(token in other for other in corpus)
                    score += log(1 + (len(corpus) - df + 0.5) / (df + 0.5)) * doc[token] * 2.2 / (doc[token] + factor)
            candidates.append({**policy, "query": query, "score": round(score, 4)})
        if candidates:
            found[rule] = max(candidates, key=lambda p: (
                p["destination"] == destination, p["effective"], p["score"], p["id"],
            ))
    return found
