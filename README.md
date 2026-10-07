# ai-panel — 収集・抽出・集計の最小構成

仕様書：`../debates/20261006-ai-panel-platform/draft-v3.md`（Codex 審査 PASS）

## 構成
- `liff/index.html` … 参加者の画面（登録・お題・提出・ポイント）。LINE の中（LIFF）でも、ブラウザ単体（開発モード）でも動く
- `server/app.py` … API。`server/process.py` … 提出1件の処理（取得→構造化→伏字→保存→受付→ポイント）
- `pipeline/fetch.py` … 共有ページの取得（Playwright）。`extract.py` … 質問一致・ブランド抽出・順位・言及の種類・個人化の手がかり。`redact.py` … 伏字。`llm.py` … LLM 分類（任意）
- `seed.py` … カテゴリ・お題・ブランド辞書の初期データ
- `data/panel.db` … SQLite。`data/raw/` … 回答の原文（`RAW_KEY` を設定すると暗号化）

## 動かす
```bash
.venv/bin/python seed.py
DEV_MODE=1 .venv/bin/uvicorn server.app:app --reload --port 8000
```
ブラウザで http://localhost:8000 を開く。開発モードでは「開発用ID」を自分で入れてログイン代わりにする。

## 環境変数
| 変数 | 用途 |
|---|---|
| `DEV_MODE` | 1 なら `X-Dev-Uid` ヘッダでログイン代わり（本番は 0） |
| `LINE_CHANNEL_ID` / `LIFF_ID` | LINE ログインの検証と LIFF 初期化 |
| `ANTHROPIC_API_KEY` | 言及の種類・個人化・人名検出を LLM で行う（未設定なら規則版） |
| `RAW_KEY` | 原文と LINE ユーザーIDの暗号化鍵（`cryptography` の Fernet 鍵） |
| `ADMIN_TOKEN` | 運営用 API（`/api/admin/...?token=`） |
| `DATABASE_URL` | 既定 `sqlite:///data/panel.db` |

## 運営用
- `/api/admin/summary?token=…` … 受付状況
- `/api/admin/unmapped?token=…` … 辞書に当たらなかった現使用ブランド（週1回の辞書更新）
- `/api/admin/export/{participants|responses|mentions|own_brand}?token=…&scope=main|wide` … 分析用 CSV（検証実験の analyze.py / dashboard と同じ形式）

## テスト
```bash
.venv/bin/python -m pytest -q tests
```

## LINE 側で必要なもの（ユーザーが用意）
1. LINE 公式アカウント（Messaging API 有効化）
2. LINE Developers でログインチャネル → LIFF アプリを追加（エンドポイント URL＝このサーバーの公開 URL、スコープ profile, openid）
3. `LINE_CHANNEL_ID`（ログインチャネルのID）と `LIFF_ID` をサーバーに設定
4. リッチメニュー：「今週のお題」「ポイント」「使い方」→ いずれも LIFF の URL
