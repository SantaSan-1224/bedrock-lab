"""AgentCore Gateway の Lambda ターゲット (Phase 3 M6)。

agent_cli.py のツール4種を、Gateway 経由の MCP ツールとして公開する。
ツール実装は agent_cli.py と同一 (zip に同梱して import) — エージェント専用
だったツールが、MCP を話す任意のクライアントから使える共用ツールになる。

Gateway からの入力:
  - event: ツールの引数 (dict)
  - context.client_context.custom["bedrockAgentCoreToolName"]:
    "targetName___toolName" 形式 (プレフィックスを剥がして使う)
"""

from __future__ import annotations

import os

import boto3

from agent_cli import (
    build_clients,
    tool_get_cost,
    tool_list_resources,
    tool_search_kb,
    tool_search_logs,
)

_clients = None


def _get_clients():
    global _clients
    if _clients is None:
        _clients = build_clients(boto3.Session())
    return _clients


def handler(event, context):
    raw_name = (context.client_context.custom or {}).get("bedrockAgentCoreToolName", "")
    tool_name = raw_name.split("___")[-1]
    inp = event or {}
    clients = _get_clients()

    if tool_name == "get_cost":
        return tool_get_cost(clients, inp)
    if tool_name == "search_logs":
        return tool_search_logs(clients, inp)
    if tool_name == "list_resources":
        return tool_list_resources(clients, inp)
    if tool_name == "search_kb":
        return tool_search_kb(clients, inp, os.environ.get("KB_ID"))
    return {"error": f"unknown tool: {raw_name}"}
