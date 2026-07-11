# みまもり - 診察内容の文章化機能

バックエンドAPIとフロントエンドを **1つの Cloud Run サービス** に統合したアプリです。

```
mimamori/
├── main.py              # FastAPI: APIエンドポイント + 静的HTML配信
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── deploy.sh            # セットアップ & デプロイ (Cloud Shell 用)
└── static/
    ├── index.html       # ホーム画面 (服薬チェック・次の通院日)
    ├── recording.html   # 受診記録画面 (録音・波形・履歴・詳細)
    ├── login.html       # ログイン
    ├── register.html    # 新規登録
    └── config.js        # Firebase設定 (deploy.sh が自動生成)
```

## アーキテクチャ

```
[ブラウザ]
  ↓ GET /          → static/index.html, recording.html ...
  ↓ POST /recordings  → Firestore に録音セッション作成 + GCS 署名付きURL
  ↓ PUT <署名付きURL>  → GCS に音声を直接アップロード
  ↓ POST /recordings/{id}/start-processing → STT V2 BatchRecognize 起動

[Cloud Run (バックグラウンド処理)]
  ↳ STT (Chirp) の完了をバックエンド側でループ監視 (ポーリング)
  ↳ 完了を検知後、GCSからJSONを読み込み
  ↳ Gemini で要約・構造化 → Firestore 保存 → status="done"

[ブラウザ] 12秒ごとポーリング → GET /recordings/{id} → 要約を表示
```

## デプロイ手順 (Cloud Shell)

### 初回 (GCPリソースも作成)

```bash
unzip mimamori.zip && cd mimamori
chmod +x deploy.sh

# Firebase APIキー を指定して実行
# (Firebase コンソール > プロジェクトの設定 > 全般 > ウェブAPIキー)
FIREBASE_API_KEY="AIzaSy..." bash deploy.sh --setup
```

`--setup` フラグで以下を自動実行します:
- 必要なAPI有効化 (Cloud Run / STT / Vertex AI / Firestore / Eventarc など)
- Firestore (Native mode) 作成
- GCSバケット 2つ作成 (`${PROJECT_ID}-audio`, `${PROJECT_ID}-stt-output`)
- STT V2 Recognizer 作成 (ja-JP / long モデル)
- サービスアカウントへの権限付与

### 2回目以降 (コードのみ更新)

```bash
FIREBASE_API_KEY="AIzgcloud run deploy mimamori-service \
  --source . \
  --region=us-central1 \
  --platform=managed \
  --allow-unauthenticated \
  --timeout=900 \
  --set-env-vars PROJECT_ID=your-project-id,AUDIO_BUCKET=your-bucket-audio,STT_OUTPUT_BUCKET=your-bucket-stt-output,STT_LOCATION=us-central1,ALLOW_DEV_AUTH=trueaSy..." bash deploy.sh
```

### デプロイ後に必要な手作業

Firebase コンソールで **Authorized domains** に Cloud Run の URL を追加:
```
Authentication > Settings > Authorized domains
→ xxxx-uc.a.run.app を追加
```

## APIエンドポイント

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/healthz` | ヘルスチェック |
| POST | `/recordings` | 録音セッション作成 + 署名付きURL発行 |
| POST | `/recordings/{id}/start-processing` | STT BatchRecognize 起動 |
| GET | `/recordings` | 履歴一覧 |
| GET | `/recordings/{id}` | 詳細 (要約 + 文字起こし) |
| POST | `/events/stt-output` | Eventarc受信 → Gemini要約 → Firestore保存 |
| GET | `/*` | 静的HTML配信 |

## 環境変数

| 変数名 | 必須 | デフォルト | 説明 |
|---|---|---|---|
| `PROJECT_ID` | ✅ | — | GCP プロジェクトID |
| `AUDIO_BUCKET` | ✅ | — | 音声アップロードバケット |
| `STT_OUTPUT_BUCKET` | ✅ | — | STT出力バケット |
| `STT_LOCATION` | | us-central1 | STT/Cloud Run リージョン |
| `STT_RECOGNIZER_ID` | | medical-ja | STT Recognizer ID |
| `GEMINI_LOCATION` | | us-central1 | Gemini リージョン |
| `GEMINI_MODEL` | | gemini-2.5-flash | 使用モデル |
| `ALLOW_DEV_AUTH` | | false | `true` で X-Debug-Uid 認証バイパス (開発専用) |

## ローカル開発

```bash
# 1. 依存パッケージをインストール
pip install -r requirements.txt --break-system-packages

# 2. static/config.js を手動作成
cat > static/config.js <<'EOF'
window.__FIREBASE_CONFIG__ = {
  apiKey:     "AIzaSy...",
  authDomain: "your-project.firebaseapp.com",
  projectId:  "your-project-id",
};
EOF

# 3. 起動
export PROJECT_ID=your-project-id
export AUDIO_BUCKET=your-project-id-audio
export STT_OUTPUT_BUCKET=your-project-id-stt-output
export ALLOW_DEV_AUTH=true
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account-key.json

uvicorn main:app --reload --port 8080
# → http://localhost:8080 でアクセス
```

## Firestoreデータモデル

- `recordings/{id}` — `patient_id`, `started_at`, `ended_at`, `duration_sec`, `audio_storage_uri`, `status` (uploading/processing/done/error), `title`, `easy_summary`, `transcript_id`, `summary_id`
- `transcripts/{id}` — `recording_id`, `full_text`, `segments` ([{start, end, text}])
- `summaries/{id}` — `recording_id`, `title`, `diagnosis`, `lifestyle_notes`, `next_appointment`, `easy_summary`, `model_version`
