"""Opt-in DeepSeek analysis and review grounded in retrieved evidence."""
import json
import os
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import Request, urlopen


def chat(instruction, context):
    request = Request(
        "https://api.deepseek.com/chat/completions",
        headers={"Authorization": "Bearer " + os.environ["DEEPSEEK_API_KEY"],
                 "Content-Type": "application/json"},
        data=json.dumps({
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            "messages": [{"role": "system", "content": instruction}, {
                "role": "user", "content": json.dumps(context, ensure_ascii=False),
            }],
            "max_tokens": 550,
        }).encode("utf-8"),
    )
    with urlopen(request, timeout=15) as response:
        content = json.load(response)["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("Model returned no explanation")
        return content.strip()


def narrate(result):
    evidence = [{k: row[k] for k in ("id", "title", "body")} for row in result["evidence"]]
    context = {k: result[k] for k in ("status", "target", "proposal", "service_risk", "baseline_review")}
    context["evidence"] = evidence
    notes = {}
    roles = {
        "规划分析员": "用程序提供的数字和检索条款解释方案。只引用 evidence 内的编号。保持程序数量、成本与判定，150 字以内。",
        "审批审阅员": "独立查看程序结果和条款，核对分析员解读，指出剩余缺口和审批注意项。保持程序判定，120 字以内。",
    }
    for role, instruction in roles.items():
        try:
            notes[role] = chat(instruction, {**context, "分析员解读": notes.get("规划分析员")})
        except (OSError, HTTPException, URLError, KeyError, ValueError, TypeError, IndexError):
            notes[role] = "模型说明暂不可用，请参阅程序结果和原始条款。"
    return notes
