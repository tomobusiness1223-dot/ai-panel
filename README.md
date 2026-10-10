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
- `POST /api/admin/backfill?token=…&all=0|1` … 読み取り規則や辞書を直したあと、受付済みの提出を作り直す（起動時にも extract_version が古いものは自動で作り直す。共有ページが混雑時に会話データ無しを返した分は飛ばされるので、あとでもう一度叩く）
- `POST /api/admin/resume?token=…` … サーバー再起動で pending のまま残った提出を処理し直す

## テスト
```bash
.venv/bin/python -m pytest -q tests
```

## LINE 側で必要なもの（ユーザーが用意）
1. LINE 公式アカウント（Messaging API 有効化）
2. LINE Developers でログインチャネル → LIFF アプリを追加（エンドポイント URL＝このサーバーの公開 URL、スコープ profile, openid）
3. `LINE_CHANNEL_ID`（ログインチャネルのID）と `LIFF_ID` をサーバーに設定
4. リッチメニュー：「今週のお題」「ポイント」「使い方」→ いずれも LIFF の URL

## クラウドワークスで集めた回答の取り込み

クラウドワークスは外部（LINE など）への誘導ができないため、回答は作業フォームで受け取り、出力した CSV を取り込む。設問の文面は `../debates/20261006-ai-panel-platform/recruit/crowdworks-task-A.md` / `-B.md`。

```bash
.venv/bin/python tools/import_crowdworks.py A <タスクAのCSV>     # お題型（前提＋3カテゴリ）
.venv/bin/python tools/import_crowdworks.py B <タスクBのCSV>     # 実会話型
.venv/bin/python tools/import_crowdworks.py status                 # work/cw_status.csv（承認判断用）
```

取り込んだ回答は LINE 経由と同じ処理に乗る。同じ作業IDの同じお題は二重に入らない。`--dry` で送らずに読み取り内容だけ確認できる。


## カテゴリ別レポート（見本）

```
.venv/bin/python tools/build_category_report.py toner --html <ひな形.html> <出力.html>
```

本番から出力を取り直し、会話を「前提 → 最初の推薦 → AI の質問 → 答え → 最終の推薦」に分けた1人1行の記録（`work/report_toner.json`）と、それを埋め込んだ HTML を作る。集計は画面側で行うので、ブランドを切り替えて見られる。
辞書は `pipeline/report_dict.json`（商品ライン、理由の分類、質問の分類、参照元の種類、前提の分類）。参加者の答えは `tools/annotate.py push` で条件の注釈として本番に入れておく（項目と値は `pipeline/conditions.json`）。実会話の事例は `work/cases_<カテゴリ>.json` に手で書く（参加者の言葉はそのまま載せる）。
読み取り規則を変えたときは `EXTRACT_VERSION` を上げてデプロイすると、起動時に全件を作り直す（共有リンクが消えていても、受付時に保存した会話から作り直す）。
