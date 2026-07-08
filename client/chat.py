#!/usr/bin/env python3
"""Amazon Bedrock 上の Claude と対話する薄い CLI クライアント。

- Converse API (converse_stream) を使用。モデル差を吸収する統一 API のため、
  会話履歴を維持したままモデルを切り替えられる。
- 既定モデルは日本国内完結 (東京/大阪ルーティング) の JP cross-region
  inference profile。
- 会話ログは JSONL 形式で logs/ 配下にローカル保存する。

使い方:
    python chat.py                        # 対話モード (デフォルト: sonnet)
    python chat.py -m haiku               # モデル指定
    python chat.py --once "質問文"        # 単発実行
    python chat.py --profile bedrock-lab  # AWS プロファイル指定
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

# 日本国内完結 (東京/大阪) の cross-region inference profile
MODEL_ALIASES: dict[str, str] = {
    "sonnet": "jp.anthropic.claude-sonnet-4-6",
    "opus": "jp.anthropic.claude-opus-4-8",
    "haiku": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
    "sonnet45": "jp.anthropic.claude-sonnet-4-5-20250929-v1:0",
}
DEFAULT_ALIAS = "sonnet"
DEFAULT_REGION = "ap-northeast-1"
DEFAULT_MAX_TOKENS = 4096
LOG_DIR = Path(__file__).resolve().parent / "logs"

HELP_TEXT = """\
チャット内コマンド:
  /model          現在のモデルとエイリアス一覧を表示
  /model <name>   モデル切替 (エイリアス or 推論プロファイルID。会話履歴は維持)
  /clear          会話履歴をクリア
  /usage          このセッションの累計トークン数を表示
  /help           このヘルプ
  /quit           終了 (Ctrl-D でも可)
"""


def resolve_model(name: str) -> str:
    return MODEL_ALIASES.get(name, name)


def explain_client_error(err: ClientError) -> str:
    """代表的なエラーに日本語のヒントを付ける。"""
    code = err.response.get("Error", {}).get("Code", "")
    hints = {
        "AccessDeniedException": (
            "モデルアクセスが未有効化、または IAM 権限不足の可能性。\n"
            "  - Bedrock コンソール > Model access で対象モデルを有効化 (東京/大阪)\n"
            "  - assume しているロールに bedrock:InvokeModel* 権限があるか確認"
        ),
        "ResourceNotFoundException": (
            "モデル ID / 推論プロファイル ID がこのリージョンに存在しない。\n"
            "  - リージョン (--region) とモデル ID の組み合わせを確認"
        ),
        "ThrottlingException": "スロットリング発生。時間をおいて再実行。",
        "ValidationException": "リクエスト形式エラー。モデル ID や max-tokens を確認。",
        "ExpiredTokenException": "一時クレデンシャルの期限切れ。SSO ログイン / assume-role をやり直す。",
    }
    hint = hints.get(code, "")
    return f"[{code}] {err.response.get('Error', {}).get('Message', str(err))}" + (
        f"\nヒント: {hint}" if hint else ""
    )


class ChatSession:
    def __init__(
        self,
        client,
        model_id: str,
        system_prompt: str | None,
        max_tokens: int,
        log_path: Path,
    ) -> None:
        self.client = client
        self.model_id = model_id
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.log_path = log_path
        self.messages: list[dict] = []
        self.total_input_tokens = 0
        self.total_output_tokens = 0

    # ---- Converse API 呼び出し ----
    def send(self, user_text: str) -> None:
        self.messages.append({"role": "user", "content": [{"text": user_text}]})

        kwargs: dict = {
            "modelId": self.model_id,
            "messages": self.messages,
            "inferenceConfig": {"maxTokens": self.max_tokens},
        }
        if self.system_prompt:
            kwargs["system"] = [{"text": self.system_prompt}]

        parts: list[str] = []
        usage: dict = {}
        stop_reason = ""
        try:
            response = self.client.converse_stream(**kwargs)
            for event in response["stream"]:
                if "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"]["delta"].get("text", "")
                    print(delta, end="", flush=True)
                    parts.append(delta)
                elif "messageStop" in event:
                    stop_reason = event["messageStop"].get("stopReason", "")
                elif "metadata" in event:
                    usage = event["metadata"].get("usage", {})
            print()
        except KeyboardInterrupt:
            # ストリーム中断: このターンは履歴に残さない
            print("\n(中断しました。このターンは履歴に含めません)")
            self.messages.pop()
            return
        except (ClientError, BotoCoreError) as err:
            self.messages.pop()
            if isinstance(err, ClientError):
                print(f"\nエラー: {explain_client_error(err)}", file=sys.stderr)
            else:
                print(f"\n接続エラー: {err}", file=sys.stderr)
            return

        assistant_text = "".join(parts)
        self.messages.append(
            {"role": "assistant", "content": [{"text": assistant_text}]}
        )
        self.total_input_tokens += usage.get("inputTokens", 0)
        self.total_output_tokens += usage.get("outputTokens", 0)
        self._write_log(user_text, assistant_text, usage, stop_reason)

    # ---- ローカル会話ログ (JSONL) ----
    def _write_log(
        self, user_text: str, assistant_text: str, usage: dict, stop_reason: str
    ) -> None:
        record = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "model": self.model_id,
            "user": user_text,
            "assistant": assistant_text,
            "usage": usage,
            "stop_reason": stop_reason,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    # ---- チャット内コマンド ----
    def show_usage(self) -> None:
        print(
            f"累計トークン: input={self.total_input_tokens} "
            f"output={self.total_output_tokens}"
        )

    def switch_model(self, name: str) -> None:
        self.model_id = resolve_model(name)
        print(f"モデルを切り替えました: {self.model_id} (会話履歴は維持)")

    def show_model(self) -> None:
        print(f"現在のモデル: {self.model_id}")
        print("エイリアス一覧:")
        for alias, model_id in MODEL_ALIASES.items():
            print(f"  {alias:10s} -> {model_id}")

    def clear(self) -> None:
        self.messages = []
        print("会話履歴をクリアしました。")


def build_client(region: str, profile: str | None):
    session = boto3.Session(profile_name=profile, region_name=region)
    return session.client(
        "bedrock-runtime",
        config=Config(
            retries={"max_attempts": 3, "mode": "adaptive"},
            read_timeout=300,
        ),
    )


def interactive_loop(chat: ChatSession) -> None:
    print(f"Bedrock Claude チャット (model: {chat.model_id})")
    print("/help でコマンド一覧、/quit で終了")
    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue

        if user_input.startswith("/"):
            cmd, _, arg = user_input.partition(" ")
            arg = arg.strip()
            if cmd in ("/quit", "/exit"):
                break
            elif cmd == "/help":
                print(HELP_TEXT)
            elif cmd == "/model":
                chat.switch_model(arg) if arg else chat.show_model()
            elif cmd == "/clear":
                chat.clear()
            elif cmd == "/usage":
                chat.show_usage()
            else:
                print(f"不明なコマンド: {cmd} (/help 参照)")
            continue

        chat.send(user_input)

    chat.show_usage()
    print(f"会話ログ: {chat.log_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Amazon Bedrock 上の Claude と対話する CLI (Converse API)"
    )
    parser.add_argument(
        "-m",
        "--model",
        default=DEFAULT_ALIAS,
        help=f"モデルエイリアス or 推論プロファイルID (default: {DEFAULT_ALIAS})",
    )
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--profile", default=None, help="AWS CLI プロファイル名")
    parser.add_argument("--system", default=None, help="システムプロンプト")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--once", metavar="PROMPT", help="単発実行して終了")
    parser.add_argument(
        "--log-dir", type=Path, default=LOG_DIR, help="会話ログの出力先"
    )
    args = parser.parse_args()

    model_id = resolve_model(args.model)
    log_path = (
        args.log_dir
        / f"session_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )

    try:
        client = build_client(args.region, args.profile)
    except (ClientError, BotoCoreError) as err:
        print(f"AWS クライアント初期化に失敗: {err}", file=sys.stderr)
        return 1

    chat = ChatSession(client, model_id, args.system, args.max_tokens, log_path)

    if args.once:
        chat.send(args.once)
        return 0

    interactive_loop(chat)
    return 0


if __name__ == "__main__":
    sys.exit(main())
