#!/usr/bin/env python3
"""AgentCore Runtime エントリポイント (Phase 3 方式①)。

M2 の自前 tool use ループ (agent_cli.py) を、コードほぼそのままマネージド
ランタイムに載せる。ローカル (方式②) との違い:
  - 認証: プロファイルではなく Runtime 実行ロール (ツールの鍵がクラウド側に移る)
  - 履歴: セッション (runtimeSessionId) ごとに分離された microVM の
    プロセス内メモリに保持される → セッション内マルチターンが成立するか観察
  - 観測: stdout (verbose 出力) が CloudWatch / AgentCore Observability に流れる

ローカルテスト: python agent_runtime.py で :8080 に起動し、
  curl -X POST localhost:8080/invocations -d '{"prompt": "..."}'
"""

from __future__ import annotations

from pathlib import Path

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

from agent_cli import AgentSession, build_clients, resolve_kb_id

app = BedrockAgentCoreApp()

# microVM (セッション単位) のプロセス内で初期化は 1 回。
# AgentSession.history がプロセスメモリに残るため、同一 runtimeSessionId の
# 連続呼び出しで会話文脈が維持される (はず — M4 の観察ポイント)
_agent: AgentSession | None = None


def _get_agent() -> AgentSession:
    global _agent
    if _agent is None:
        session = boto3.Session()  # Runtime 実行ロールの一時クレデンシャル
        _agent = AgentSession(
            clients=build_clients(session),
            kb_id=resolve_kb_id(session),
            verbose=True,  # ツール呼び出しの経過を stdout → CloudWatch へ
            log_path=Path("/tmp/agent_runtime_log.jsonl"),  # エフェメラル領域
        )
    return _agent


@app.entrypoint
def handler(payload: dict):
    prompt = (payload or {}).get("prompt", "")
    if not prompt:
        return {"error": "payload に prompt がありません。例: {\"prompt\": \"今月のコストは?\"}"}
    answer = _get_agent().ask(prompt)
    if answer is None:
        return {"error": "モデル呼び出しに失敗しました (Runtime ログを確認してください)"}
    return {"result": answer}


if __name__ == "__main__":
    app.run()
