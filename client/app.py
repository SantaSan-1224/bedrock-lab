#!/usr/bin/env python3
"""bedrock-lab 簡易チャット UI (Phase 2 M6 / Streamlit)。

- 通常モード: Converse API で素の Claude と対話
- RAG モード: 方式② (KB Retrieve + 自前プロンプト合成)。
  会話履歴を自前管理するため、絞り込みフォローアップに追従できる (M4 の学びを反映)
- 追加の AWS リソース不要。ローカル起動のみ

起動:
    streamlit run app.py
"""

import boto3
import streamlit as st
from botocore.config import Config

from mini_rag import (
    DEFAULT_KB_NAME,
    MODEL_ID,
    RAG_SYSTEM_PROMPT,
    build_user_message,
    resolve_kb_id,
    search_kb,
)

REGION = "ap-northeast-1"
DEFAULT_PROFILE = "bedrock-lab"

MODEL_ALIASES = {
    "Sonnet 4.6 (既定)": "jp.anthropic.claude-sonnet-4-6",
    "Haiku 4.5 (軽量)": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "Sonnet 4.5": "jp.anthropic.claude-sonnet-4-5-20250929-v1:0",
}

st.set_page_config(page_title="bedrock-lab chat", page_icon="🧪", layout="centered")


@st.cache_resource
def get_clients(profile: str):
    session = boto3.Session(profile_name=profile or None, region_name=REGION)
    cfg = Config(retries={"max_attempts": 3, "mode": "adaptive"}, read_timeout=300)
    return {
        "runtime": session.client("bedrock-runtime", config=cfg),
        "agent": session.client("bedrock-agent-runtime", config=cfg),
        "kb_id": resolve_kb_id(session),
    }


# ---------- サイドバー ----------
with st.sidebar:
    st.title("🧪 bedrock-lab")
    profile = st.text_input("AWS プロファイル", value=DEFAULT_PROFILE)
    model_label = st.selectbox("モデル", list(MODEL_ALIASES.keys()))
    model_id = MODEL_ALIASES[model_label]
    rag_on = st.toggle("RAG モード (自分の記事を検索)", value=True)
    top_k = st.slider("検索件数 (top_k)", 1, 10, 5, disabled=not rag_on)
    if st.button("会話をクリア"):
        st.session_state.pop("messages", None)
        st.session_state.pop("display", None)
        st.rerun()
    st.caption(
        "RAG は方式② (KB Retrieve + 自前合成)。"
        "会話履歴を自前管理するため「〜だけ詳しく」に追従できます。"
    )

clients = get_clients(profile)
kb_id = clients["kb_id"]
if rag_on and not kb_id:
    st.error(f"Knowledge Base '{DEFAULT_KB_NAME}' が見つかりません。terraform apply 済みか確認してください。")
    st.stop()

# messages: Converse 用履歴 (資料抜き) / display: 画面表示用 (出典含む)
st.session_state.setdefault("messages", [])
st.session_state.setdefault("display", [])

st.title("bedrock-lab chat")
mode_badge = "🔎 RAG (出典付き)" if rag_on else "💬 素の Claude"
st.caption(f"モード: {mode_badge} / モデル: {model_id} / 国内完結 (東京⇔大阪)")

# ---------- 履歴表示 ----------
for item in st.session_state.display:
    with st.chat_message(item["role"]):
        st.markdown(item["text"])
        if item.get("sources"):
            with st.expander("出典"):
                for s in item["sources"]:
                    st.markdown(f"- [{s['title']}]({s['url']}) ({s['published']})")

# ---------- 入力・応答 ----------
if prompt := st.chat_input("質問を入力..."):
    with st.chat_message("user"):
        st.markdown(prompt)
    st.session_state.display.append({"role": "user", "text": prompt})

    sources = []
    send_messages = list(st.session_state.messages)

    if rag_on:
        with st.spinner("検索中..."):
            chunks = search_kb(clients, kb_id, prompt, top_k)
        seen = {}
        for c in chunks:
            seen[c["url"] or c["title"]] = {
                "title": c["title"], "published": c["published"], "url": c["url"],
            }
        sources = list(seen.values())
        send_messages.append(
            {"role": "user", "content": [{"text": build_user_message(prompt, chunks)}]}
        )
        system = [{"text": RAG_SYSTEM_PROMPT}]
    else:
        send_messages.append({"role": "user", "content": [{"text": prompt}]})
        system = []

    with st.chat_message("assistant"):
        def stream():
            kwargs = dict(
                modelId=model_id,
                messages=send_messages,
                inferenceConfig={"maxTokens": 4096},
            )
            if system:
                kwargs["system"] = system
            resp = clients["runtime"].converse_stream(**kwargs)
            for event in resp["stream"]:
                if "contentBlockDelta" in event:
                    yield event["contentBlockDelta"]["delta"].get("text", "")

        answer = st.write_stream(stream)
        if rag_on and sources:
            with st.expander("出典"):
                for s in sources:
                    st.markdown(f"- [{s['title']}]({s['url']}) ({s['published']})")

    # 履歴更新 (Converse 用は資料抜きの生質問)
    st.session_state.messages.append({"role": "user", "content": [{"text": prompt}]})
    st.session_state.messages.append({"role": "assistant", "content": [{"text": answer}]})
    st.session_state.display.append(
        {"role": "assistant", "text": answer, "sources": sources}
    )
