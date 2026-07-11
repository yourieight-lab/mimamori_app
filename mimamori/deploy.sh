#!/usr/bin/env bash
# ============================================================
# みまもり 統合デプロイスクリプト
# Cloud Shell で実行: bash deploy.sh
#
# 初回は --setup フラグで GCP リソースも作成する:
#   bash deploy.sh --setup
#
# 必須環境変数:
#   FIREBASE_API_KEY   Firebase コンソール > プロジェクトの設定 > ウェブAPIキー
#
# オプション環境変数:
#   REGION             デフォルト: us-central1
#   SERVICE_NAME       デフォルト: mimamori
#   AUDIO_BUCKET       デフォルト: ${PROJECT_ID}-audio
#   STT_OUTPUT_BUCKET  デフォルト: ${PROJECT_ID}-stt-output
#   GEMINI_MODEL       デフォルト: gemini-2.5-flash
#   ALLOW_DEV_AUTH     true にするとデバッグ認証を有効化 (本番では false)
# ============================================================
set -euo pipefail

# ── 設定 ────────────────────────────────────────────────────────
PROJECT_ID=$(gcloud config get-value project)
REGION="${REGION:-asia-northeast1}"
SERVICE_NAME="${SERVICE_NAME:-mimamori}"
AUDIO_BUCKET="${AUDIO_BUCKET:-${PROJECT_ID}-audio}"
STT_OUTPUT_BUCKET="${STT_OUTPUT_BUCKET:-${PROJECT_ID}-stt-output}"
RECOGNIZER_ID="${RECOGNIZER_ID:-medical-ja}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
ALLOW_DEV_AUTH="${ALLOW_DEV_AUTH:-false}"

FIREBASE_API_KEY="${FIREBASE_API_KEY:-}"
FIREBASE_AUTH_DOMAIN="${FIREBASE_AUTH_DOMAIN:-${PROJECT_ID}.firebaseapp.com}"
FIREBASE_PROJECT_ID="${FIREBASE_PROJECT_ID:-${PROJECT_ID}}"

# ── バリデーション ───────────────────────────────────────────────
if [[ -z "$FIREBASE_API_KEY" ]]; then
  echo "ERROR: FIREBASE_API_KEY が未設定です。"
  echo "  Firebase コンソール > プロジェクトの設定 > ウェブAPIキー を確認してください。"
  echo ""
  echo "  実行例:"
  echo "    FIREBASE_API_KEY=AIzaSy... bash deploy.sh"
  exit 1
fi

echo "============================================================"
echo "  PROJECT_ID        = ${PROJECT_ID}"
echo "  REGION            = ${REGION}"
echo "  SERVICE_NAME      = ${SERVICE_NAME}"
echo "  AUDIO_BUCKET      = ${AUDIO_BUCKET}"
echo "  STT_OUTPUT_BUCKET = ${STT_OUTPUT_BUCKET}"
echo "  ALLOW_DEV_AUTH    = ${ALLOW_DEV_AUTH}"
echo "============================================================"
echo ""

# ── --setup フラグ: GCP リソース作成 ────────────────────────────
if [[ "${1:-}" == "--setup" ]]; then
  echo ">>> [SETUP] 必要なAPIを有効化"
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    speech.googleapis.com \
    aiplatform.googleapis.com \
    firestore.googleapis.com \
    storage.googleapis.com \
    eventarc.googleapis.com \
    pubsub.googleapis.com \
    iam.googleapis.com \
    identitytoolkit.googleapis.com

  echo ""
  echo ">>> [SETUP] Firestore (Native mode) を作成"
  gcloud firestore databases create --location="${REGION}" --type=firestore-native \
    || echo "  -> 既に存在します (無視)"

  echo ""
  echo ">>> [SETUP] GCS バケットを作成"
  gsutil mb -l "${REGION}" "gs://${AUDIO_BUCKET}"      || echo "  -> 既に存在します"
  gsutil mb -l "${REGION}" "gs://${STT_OUTPUT_BUCKET}" || echo "  -> 既に存在します"

  echo ""
  echo ">>> [SETUP] Speech-to-Text V2 Recognizer を作成 (ja-JP / long モデル)"
  python3 - <<PYEOF
import os
from google.api_core.client_options import ClientOptions
from google.api_core.exceptions import AlreadyExists
from google.cloud.speech_v2 import SpeechClient
from google.cloud.speech_v2.types import cloud_speech

PROJECT_ID    = "${PROJECT_ID}"
STT_LOCATION  = "${REGION}"
RECOGNIZER_ID = "${RECOGNIZER_ID}"

client = SpeechClient(client_options=ClientOptions(api_endpoint=f"{STT_LOCATION}-speech.googleapis.com"))
try:
    op = client.create_recognizer(request=cloud_speech.CreateRecognizerRequest(
        parent=f"projects/{PROJECT_ID}/locations/{STT_LOCATION}",
        recognizer_id=RECOGNIZER_ID,
        recognizer=cloud_speech.Recognizer(
            default_recognition_config=cloud_speech.RecognitionConfig(
                language_codes=["ja-JP"], model="long",
                auto_decoding_config=cloud_speech.AutoDetectDecodingConfig(),
                features=cloud_speech.RecognitionFeatures(
                    enable_automatic_punctuation=True,
                    enable_word_time_offsets=True,
                ),
            )
        ),
    ))
    print(f"  Recognizer created: {op.result(timeout=300).name}")
except AlreadyExists:
    print(f"  -> 既に存在します: projects/{PROJECT_ID}/locations/{STT_LOCATION}/recognizers/{RECOGNIZER_ID}")
PYEOF

  echo ""
  echo ">>> [SETUP] サービスアカウントに権限を付与"
  PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
  SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
  echo "  対象: ${SA_EMAIL}"

  gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/iam.serviceAccountTokenCreator" --condition=None

  for ROLE in roles/datastore.user roles/storage.objectAdmin roles/speech.editor roles/aiplatform.user; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
      --member="serviceAccount:${SA_EMAIL}" --role="${ROLE}" --condition=None
  done

  echo ""
  echo ">>> [SETUP] 完了。デプロイを続けます…"
  echo ""
fi

# ── config.js 生成 (static/に配置) ──────────────────────────────
mkdir -p static
cat > static/config.js <<CONFIG
// 自動生成ファイル (deploy.sh によって生成) - 編集しないこと
window.__FIREBASE_CONFIG__ = {
  apiKey:     "${FIREBASE_API_KEY}",
  authDomain: "${FIREBASE_AUTH_DOMAIN}",
  projectId:  "${FIREBASE_PROJECT_ID}",
};
CONFIG
echo ">>> static/config.js を生成しました"

# ── コンテナビルド & Cloud Run デプロイ ─────────────────────────
IMAGE="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"
echo ""
echo ">>> コンテナイメージをビルド"
gcloud builds submit --tag "${IMAGE}"

echo ""
echo ">>> Cloud Run にデプロイ"
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --platform=managed \
  --allow-unauthenticated \
  --set-env-vars="\
PROJECT_ID=${PROJECT_ID},\
AUDIO_BUCKET=${AUDIO_BUCKET},\
STT_OUTPUT_BUCKET=${STT_OUTPUT_BUCKET},\
STT_LOCATION=${REGION},\
STT_RECOGNIZER_ID=${RECOGNIZER_ID},\
GEMINI_LOCATION=${REGION},\
GEMINI_MODEL=${GEMINI_MODEL},\
ALLOW_DEV_AUTH=${ALLOW_DEV_AUTH}"

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" --region="${REGION}" --format="value(status.url)")
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")
SA_EMAIL="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# ── Eventarc トリガー (STT出力 → /events/stt-output) ────────────
echo ""
echo ">>> Eventarc トリガーを作成"
gcloud eventarc triggers create "${SERVICE_NAME}-stt-trigger" \
  --location="${REGION}" \
  --destination-run-service="${SERVICE_NAME}" \
  --destination-run-region="${REGION}" \
  --destination-run-path="/events/stt-output" \
  --event-filters="type=google.cloud.storage.object.v1.finalized" \
  --event-filters="bucket=${STT_OUTPUT_BUCKET}" \
  --service-account="${SA_EMAIL}" \
  || echo "  -> 既に存在します (無視)"

# ── Eventarc から Cloud Run を呼び出す権限 ───────────────────────
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  || true

WEB_HOST=$(echo "${SERVICE_URL}" | sed 's|https://||')

cat <<EOF

============================================================
デプロイ完了！

  アプリ URL: ${SERVICE_URL}

【重要】Firebase コンソールで Authorized domains に追加してください:
  Authentication > Settings > Authorized domains
  追加するドメイン: ${WEB_HOST}

動作確認:
  curl ${SERVICE_URL}/healthz
============================================================
EOF
