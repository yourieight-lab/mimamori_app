"""
みまもり - 診察内容の文章化機能
統合サーバー (Cloud Run 単一サービス / 同期ポーリング版)
"""

from __future__ import annotations

import base64
import json
import os
import uuid
import threading
import traceback
import time  # 完了監視用
from datetime import timedelta

import firebase_admin
import google.auth
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from firebase_admin import auth as firebase_auth
from google.auth.transport import requests as gauth_requests
from google.cloud import firestore, storage
from pydantic import BaseModel

# ══════════════════════════════════════════════════════════════════
# 設定
# ══════════════════════════════════════════════════════════════════
PROJECT_ID        = os.environ["PROJECT_ID"]
AUDIO_BUCKET      = os.environ["AUDIO_BUCKET"]
STT_OUTPUT_BUCKET = os.environ["STT_OUTPUT_BUCKET"]
STT_LOCATION      = os.environ.get("STT_LOCATION",    "us-central1")
GEMINI_LOCATION   = os.environ.get("GEMINI_LOCATION", "us-central1")
GEMINI_MODEL      = os.environ.get("GEMINI_MODEL",    "gemini-2.5-flash")
ALLOW_DEV_AUTH    = os.environ.get("ALLOW_DEV_AUTH",  "false").lower() == "true"

# ══════════════════════════════════════════════════════════════════
# Firebase / GCP クライアント初期化
# ══════════════════════════════════════════════════════════════════
firebase_admin.initialize_app()
db = firestore.Client()

_credentials, _project = google.auth.default(
    scopes=["https://www.googleapis.com/auth/cloud-platform"]
)
_auth_request  = gauth_requests.Request()
_credentials.refresh(_auth_request)
storage_client = storage.Client(credentials=_credentials, project=_project)

app = FastAPI(title="みまもり 診察記録 API", version="1.0.0")

# ──────────────────────────────────────────────────────────────────
# 認証
# ──────────────────────────────────────────────────────────────────
async def get_current_uid(
    authorization: str = Header(default=None),
    x_debug_uid:   str = Header(default=None, alias="X-Debug-Uid"),
) -> str:
    return "demo-user"

@app.get("/healthz")
def healthz():
    return {"status": "ok"}

# ──────────────────────────────────────────────────────────────────
# GCS ヘルパー
# ──────────────────────────────────────────────────────────────────
def _generate_upload_url(blob_name: str, content_type: str = "audio/webm") -> str:
    _credentials.refresh(_auth_request)
    bucket = storage_client.bucket(AUDIO_BUCKET)
    blob   = bucket.blob(blob_name)
    return blob.generate_signed_url(
        version="v4",
        expiration=timedelta(minutes=15),
        method="PUT",
        content_type=content_type,
        service_account_email=_credentials.service_account_email,
        access_token=_credentials.token,
    )

def _read_json_from_gcs_uri(gcs_uri: str) -> dict:
    """Chirp変則命名対応版
    Chirpが生成するランダムなファイル名を自動検知して読み込みます。
    """
    path = gcs_uri.replace("gs://", "")
    bucket_name, object_name = path.split("/", 1)
    bucket = storage_client.bucket(bucket_name)
    
    for attempt in range(3):
        blobs = list(bucket.list_blobs(prefix=object_name))
        if blobs:
            break
        print(f"⏳ GCS上のファイル生成を待機中... (試行 {attempt + 1}/3)")
        time.sleep(3)

    if not blobs:
        raise FileNotFoundError(f"GCS上に出力ファイルが見つかりませんでした。パス: {object_name}")
    
    target_blob = None
    for blob in blobs:
        if blob.name.endswith(".json"):
            target_blob = blob
            break
            
    if not target_blob:
        raise FileNotFoundError("フォルダ内にJSONファイルが見つかりませんでした。")
        
    print(f"📖 実際の出力ファイルを読み込みます: {target_blob.name}")
    return json.loads(target_blob.download_as_text(encoding="utf-8"))

def _parse_batch_result(result_json: dict) -> tuple[str, list[dict]]:
    segments: list[dict] = []
    texts:    list[str]  = []
    
    results = result_json.get("results", [])
    if not results and "results" in result_json.get("response", {}):
        results = result_json["response"]["results"]

    for result in results:
        alts = result.get("alternatives") or []
        if not alts:
            continue
        text = (alts[0].get("transcript") or "").strip()
        if not text:
            continue
        words = alts[0].get("words") or []
        start = words[0]["startOffset"] if words else None
        end   = words[-1]["endOffset"]  if words else result.get("resultEndOffset")
        segments.append({"start": start, "end": end, "text": text})
        texts.append(text)
    return "".join(texts), segments

# ──────────────────────────────────────────────────────────────────
# 🎙️ Speech-to-Text v2 (Chirp) 心臓部関数（機能制限エラー修正版）
# ──────────────────────────────────────────────────────────────────
def _start_batch_recognize_op(audio_gcs_uri: str, output_gcs_json_uri: str):
    """Speech-to-Text v2 クライアントを初期化し、Chirpモデルで非同期バッチ文字起こしを開始します。"""
    from google.cloud import speech_v2
    from google.cloud.speech_v2.types import cloud_speech

    print(f"🎙️ Chirp起動リクエスト: {audio_gcs_uri} -> {output_gcs_json_uri}")
    
    api_endpoint = f"{STT_LOCATION}-speech.googleapis.com:443"
    client = speech_v2.SpeechClient(
        credentials=_credentials,
        client_options={"api_endpoint": api_endpoint}
    )
    
    recognizer_path = f"projects/{PROJECT_ID}/locations/{STT_LOCATION}/recognizers/_"
    
    # 違反が出た enable_word_confidence を False に設定変更 🚀
    config = cloud_speech.RecognitionConfig(
        features=cloud_speech.RecognitionFeatures(
            enable_word_time_offsets=True,
            enable_word_confidence=False
        ),
        auto_decoding_config={},
        language_codes=["ja-JP"],
        model="chirp"
    )
    
    files = [cloud_speech.BatchRecognizeFileMetadata(uri=audio_gcs_uri)]
    gcs_output_config = cloud_speech.GcsOutputConfig(uri=output_gcs_json_uri)
    output_config = cloud_speech.RecognitionOutputConfig(gcs_output_config=gcs_output_config)
    
    request = cloud_speech.BatchRecognizeRequest(
        recognizer=recognizer_path,
        config=config,
        files=files,
        recognition_output_config=output_config,
    )
    
    operation = client.batch_recognize(request=request)
    return operation

# ──────────────────────────────────────────────────────────────────
# Gemini ヘルパー
# ──────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """あなたは、医療機関での診察の文字起こしを読み、高齢の患者本人が後から
理解できるように要約・構造化するアシスタントです。

# 出力ルール
- 出力は指定されたJSON形式のみとし、前置きや説明文は一切含めないこと。
- 「やさしい日本語要約」は200字程度を目安とし、難解な医療用語は言い換えること。
- title は録音履歴一覧に表示する15字以内の短いタイトルにすること。
- next_appointment について言及がない場合は「次回の予約についての指示はありませんでした」と記載。
"""

_RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "title":               {"type": "STRING"},
        "diagnosis":          {"type": "STRING"},
        "lifestyle_notes":    {"type": "STRING"},
        "next_appointment":   {"type": "STRING"},
        "easy_summary":       {"type": "STRING"},
    },
    "required": ["title","diagnosis","lifestyle_notes","next_appointment","easy_summary"],
}

def _summarize_transcript(full_text: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(vertexai=True, project=PROJECT_ID, location=GEMINI_LOCATION)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=full_text,
        config=types.GenerateContentConfig(
            system_instruction=_SYSTEM_PROMPT,
            response_mime_type="application/json",
            response_schema=_RESPONSE_SCHEMA,
            temperature=0.2,
        ),
    )
    return json.loads(response.text)

# ══════════════════════════════════════════════════════════════════
# API エンドポイント
# ══════════════════════════════════════════════════════════════════
class CreateRecordingRequest(BaseModel):
    duration_sec: int
    started_at: str
    ended_at:   str

@app.post("/recordings")
def create_recording(req: CreateRecordingRequest, uid: str = Depends(get_current_uid)):
    recording_id = str(uuid.uuid4())
    blob_name    = f"{uid}/{recording_id}.webm"
    audio_uri    = f"gs://{AUDIO_BUCKET}/{blob_name}"

    db.collection("recordings").document(recording_id).set({
        "patient_id":       uid,
        "started_at":       req.started_at,
        "ended_at":         req.ended_at,
        "duration_sec":     req.duration_sec,
        "audio_storage_uri": audio_uri,
        "status":           "uploading",
        "created_at":       firestore.SERVER_TIMESTAMP,
    })

    upload_url = _generate_upload_url(blob_name, content_type="audio/webm")
    return {
        "recording_id":     recording_id,
        "upload_url":       upload_url,
        "audio_storage_uri": audio_uri,
    }

# ──────────────────────────────────────────────────────────────────
# 非同期ジョブをスレッド内で同期監視して完結させる
# ──────────────────────────────────────────────────────────────────
def _wait_and_process_stt(op, output_json_uri: str, recording_id: str):
    ref = db.collection("recordings").document(recording_id)
    try:
        print(f"⏳ STTの完了を監視中... Operation: {op.operation.name}")
        
        for _ in range(90):  # 5秒おきに最大7.5分間待機
            time.sleep(5)
            if op.done():
                break
        
        if not op.done():
            print("⏳ op.result() で同期的に最終完了を待機します...")
            op.result(timeout=300)

        print("✅ STT完了！GCSから結果を読み込みます。")
        result_json         = _read_json_from_gcs_uri(output_json_uri)
        full_text, segments = _parse_batch_result(result_json)
        
        if not full_text or not full_text.strip():
            ref.update({"status": "error", "error_message": "音声から文字を検出できませんでした。"})
            return

        print("🤖 Geminiで要約を生成中...")
        summary = _summarize_transcript(full_text)

        transcript_id = str(uuid.uuid4())
        db.collection("transcripts").document(transcript_id).set({
            "recording_id": recording_id,
            "full_text":    full_text,
            "segments":     segments,
            "created_at":   firestore.SERVER_TIMESTAMP,
        })

        summary_id = str(uuid.uuid4())
        db.collection("summaries").document(summary_id).set({
            "recording_id":     recording_id,
            "title":            summary["title"],
            "diagnosis":        summary["diagnosis"],
            "lifestyle_notes":  summary["lifestyle_notes"],
            "next_appointment": summary["next_appointment"],
            "easy_summary":     summary["easy_summary"],
            "generated_at":     firestore.SERVER_TIMESTAMP,
            "model_version":    GEMINI_MODEL,
        })

        ref.update({
            "status":        "done",
            "transcript_id": transcript_id,
            "summary_id":    summary_id,
            "title":         summary["title"],
            "easy_summary":  summary["easy_summary"],
        })
        print("🎉 すべての処理が完全に成功しました！")

    except Exception as e:
        print(f"🔥 重大なエラーが発生: {e}")
        traceback.print_exc()
        ref.update({
            "status": "error",
            "error_message": f"処理エラー: {str(e)}"
        })

@app.post("/recordings/{recording_id}/start-processing")
def start_processing(recording_id: str, uid: str = Depends(get_current_uid)):
    ref  = db.collection("recordings").document(recording_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "recording not found")
    data = snap.to_dict()

    output_json_uri = f"gs://{STT_OUTPUT_BUCKET}/{uid}/{recording_id}/result.json"
    
    try:
        op = _start_batch_recognize_op(data["audio_storage_uri"], output_json_uri)
    except Exception as e:
        ref.update({"status": "error", "error_message": f"STT起動失敗: {str(e)}"})
        raise HTTPException(500, f"文字起こし開始失敗: {str(e)}")

    ref.update({
        "status":              "processing",
        "stt_operation_name": op.operation.name,
        "stt_output_prefix":  output_json_uri,
    })

    t = threading.Thread(target=_wait_and_process_stt, args=(op, output_json_uri, recording_id))
    t.start()

    return {"status": "processing", "operation_name": op.operation.name}

# ──────────────────────────────────────────────────────────────────
# 取得系エンドポイント
# ──────────────────────────────────────────────────────────────────
@app.get("/recordings")
def list_recordings(uid: str = Depends(get_current_uid)):
    docs = db.collection("recordings").where("patient_id", "==", uid).stream()
    recordings = [
        {
            "id":           doc.id,
            "started_at":   d.get("started_at"),
            "ended_at":     d.get("ended_at"),
            "status":       d.get("status"),
            "title":        d.get("title") or ( "診察（エラー発生）" if d.get("status") == "error" else "文字起こし中..." ),
            "easy_summary": d.get("easy_summary") or d.get("error_message") or "",
        }
        for doc in docs
        for d in [doc.to_dict()]
    ]
    recordings.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return recordings

@app.get("/recordings/{recording_id}")
def get_recording(recording_id: str, uid: str = Depends(get_current_uid)):
    ref  = db.collection("recordings").document(recording_id)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(404, "recording not found")
    data = snap.to_dict()

    result = {"id": recording_id, **data}
    if data.get("transcript_id"):
        t = db.collection("transcripts").document(data["transcript_id"]).get()
        if t.exists: result["transcript"] = t.to_dict()
    if data.get("summary_id"):
        s = db.collection("summaries").document(data["summary_id"]).get()
        if s.exists: result["summary"] = s.to_dict()
    return result

@app.post("/events/stt-output")
async def stt_output_event(request: Request):
    return {"status": "ok"}

app.mount("/", StaticFiles(directory="static", html=True), name="static")