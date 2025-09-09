# services/audio_service.py
import os
import tempfile
import httpx
import subprocess
from typing import List, Dict, Any, Tuple
from uuid import UUID
import json
import unicodedata
import base64
import time

# =====================
# Config
# =====================
API_BASE_URL = os.getenv("ELEVEN_NODE_API", "https://api-elevenlabs-nodejs.onrender.com")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://webhook.site/150557f8-3946-478e-8013-d5fedf0e56f2")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "120.0"))
PALAVRAS_ANTES = int(os.getenv("PALAVRAS_ANTES", "2"))
PALAVRAS_DEPOIS = int(os.getenv("PALAVRAS_DEPOIS", "0"))
AJUSTE_MS = int(os.getenv("AJUSTE_MS", "150"))  # ms

# Evolution API (globais; por usuário passaremos apenas a INSTANCE)
EVO_BASE_DEFAULT     = os.getenv("EVO_BASE", "http://localhost:8080")
EVO_APIKEY_DEFAULT   = os.getenv("EVO_APIKEY", ">q-HN0pPZ#.#3l2rO+@NKQKmH-^7y)ZH.y0cOFKxbo%)}iLV1Wb1H*Qw{M!vd|<+")
EVO_INSTANCE_DEFAULT = os.getenv("EVO_INSTANCE", "default")

# Endpoints Evolution (v1 por padrão; com fallback automático para variante sem v1)
EVO_CREATE_PATH  = os.getenv("EVO_CREATE_PATH",  "v1/instance/create")
EVO_CONNECT_PATH = os.getenv("EVO_CONNECT_PATH", "v1/instance/connect")

# (mantidos para compat; usados no status/logout com fallback também)
EVO_START_PATH  = os.getenv("EVO_START_PATH",  "sessions/start")
EVO_STATUS_PATH = os.getenv("EVO_STATUS_PATH", "sessions/status")
EVO_LOGOUT_PATH = os.getenv("EVO_LOGOUT_PATH", "sessions/logout")

WHATSAPP_VIDEO_SIZE_LIMIT_BYTES = 100 * 1024 * 1024  # ~100 MB
SEND_RETRIES = 2
SEND_BACKOFF_SEC = 2.0

# =====================
# Helpers
# =====================

def _normalize_token(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = "".join(c for c in s if unicodedata.category(c)[0] != "P")
    return s.casefold().strip()

def _to_seconds(v: Any) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x / 1000.0 if x > 10000 else x

def _strip_data_uri(s: str) -> str:
    if not isinstance(s, str):
        return s
    marker = ";base64,"
    idx = s.find(marker)
    return s[idx + len(marker):] if idx != -1 else s

def _file_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        raw = f.read()
    b64 = base64.b64encode(raw).decode("ascii")
    b64 = _strip_data_uri(b64)
    return b64

def _toggle_v1(path: str) -> str:
    """Se começa com v1/ remove; senão, prefixa v1/."""
    p = path.lstrip("/")
    return p[3:] if p.startswith("v1/") else f"v1/{p}"

async def _evo_post(path: str, payload: dict, evo_base: str | None = None):
    """
    POST com fallback: tenta 'path'. Se der 404, tenta a variante com/sem v1.
    Obs.: para 'create', alguns ambientes retornam 400 quando a instância já existe.
    """
    headers = {"apikey": EVO_APIKEY_DEFAULT, "Content-Type": "application/json"}
    base = (evo_base or EVO_BASE_DEFAULT).rstrip("/")
    p = path.lstrip("/")
    url = f"{base}/{p}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code == 404:
            # tenta variante com/sem v1
            alt = _toggle_v1(p)
            if alt != p:
                resp2 = await client.post(f"{base}/{alt}", headers=headers, json=payload)
                resp2.raise_for_status()
                ct2 = resp2.headers.get("content-type", "")
                return resp2.json() if ct2 and "application/json" in ct2 else resp2.text
        # fora do 404, sobe status normalmente
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

async def _evo_get(path: str, evo_base: str | None = None):
    """
    GET com fallback: tenta 'path'. Se der 404, tenta a variante com/sem v1.
    """
    headers = {"apikey": EVO_APIKEY_DEFAULT}
    base = (evo_base or EVO_BASE_DEFAULT).rstrip("/")
    p = path.lstrip("/")
    url = f"{base}/{p}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code == 404:
            alt = _toggle_v1(p)
            if alt != p:
                resp2 = await client.get(f"{base}/{alt}", headers=headers)
                resp2.raise_for_status()
                ct2 = resp2.headers.get("content-type", "")
                return resp2.json() if ct2 and "application/json" in ct2 else resp2.text
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

# =====================
# Evolution: sessão (create + connect/QR)
# =====================

async def evo_start_session(instance: str, evo_base: str | None = None):
    """
    Fluxo 'oficial' v1:
      - POST /v1/instance/create { instanceName }
      - GET  /v1/instance/connect/{instance}
    Com fallback automático para variantes sem /v1.
    Trata 400 na criação como "possível já existente" e segue para o connect.
    """
    create_payload = {"instanceName": instance}
    create_resp = None
    try:
        create_resp = await _evo_post(EVO_CREATE_PATH, create_payload, evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        # Alguns ambientes retornam 400 mesmo para instância nova (variação da API).
        # Se 400, seguimos adiante para o connect (pairing/qr).
        if e.response is None or e.response.status_code != 400:
            raise
        try:
            create_resp = e.response.json()
        except Exception:
            create_resp = {"error": "create_failed_400", "detail": e.response.text if e.response else str(e)}

    # Busca o QR/pairing (connect)
    connect_resp = await _evo_get(f"{EVO_CONNECT_PATH}/{instance}", evo_base=evo_base)
    return {"instance": instance, "create": create_resp, "connect": connect_resp}

async def evo_status(instance: str, evo_base: str | None = None):
    """
    Tenta: /sessions/status/{instance} (v1 antigo)
           /v1/instance/connection/{instance} (v1 atual)
    """
    # primeiro tenta o "novo" v1
    try:
        return await _evo_get(f"v1/instance/connection/{instance}", evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        if e.response is None or e.response.status_code != 404:
            raise
    # fallback para builds legadas
    return await _evo_get(f"{EVO_STATUS_PATH}/{instance}", evo_base=evo_base)

async def evo_logout(instance: str, evo_base: str | None = None):
    """
    Tenta: POST /sessions/logout (legacy) e DELETE /v1/instances/{instance} (v1 atual)
    """
    # tenta legacy
    try:
        return await _evo_post(EVO_LOGOUT_PATH, {"instanceName": instance}, evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        if e.response is None or e.response.status_code != 404:
            raise
    # tenta v1 atual
    headers = {"apikey": EVO_APIKEY_DEFAULT}
    base = (evo_base or EVO_BASE_DEFAULT).rstrip("/")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.delete(f"{base}/v1/instances/{instance}", headers=headers)
        if resp.status_code == 404:
            # fallback sem v1
            resp2 = await client.delete(f"{base}/instances/{instance}", headers=headers)
            resp2.raise_for_status()
            ct2 = resp2.headers.get("content-type", "")
            return resp2.json() if ct2 and "application/json" in ct2 else resp2.text
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

# =====================
# Evolution: Mensagens (Texto & Mídia)
# =====================

def _mk_candidates(num: str) -> list[dict]:
    digits = "".join(ch for ch in (num or "") if ch.isdigit())
    seen = set()
    cands: list[dict] = []
    if digits:
        plus = f"+{digits}"
        for n in (plus, digits):
            if n not in seen:
                cands.append({"number": n})
                seen.add(n)
    return cands or [{"number": num}]

async def enviar_texto_via_whatsapp(
    telefone: str,
    texto: str,
    evo_instance: str | None = None,
    evo_base: str | None = None
):
    destinos = _mk_candidates(telefone)
    for dst in destinos:
        payload = {
            **dst,
            "text": texto,
            "options": {"delay": 0, "presence": "composing", "linkPreview": False}
        }
        for attempt in range(SEND_RETRIES + 1):
            try:
                # tenta sem v1; se 404, _evo_post reenvia com v1 automaticamente
                return await _evo_post(
                    f"message/sendText/{(evo_instance or EVO_INSTANCE_DEFAULT)}",
                    payload, evo_base=evo_base
                )
            except httpx.HTTPError:
                if attempt < SEND_RETRIES:
                    time.sleep(SEND_BACKOFF_SEC)
    raise RuntimeError("Falha ao enviar texto via WhatsApp")

async def _send_media_video(numero: str, caminho_video: str, caption: str,
                            evo_instance: str | None, evo_base: str | None):
    file_name = os.path.basename(caminho_video)
    b64 = _file_to_b64(caminho_video)
    payload = {
        "number": numero,
        "mediatype": "video",
        "fileName": file_name,
        "caption": caption or "",
        "media": b64,
        "mimetype": "video/mp4",
        "isBase64": True,
        "options": {"delay": 0, "presence": "composing"}
    }
    return await _evo_post(f"message/sendMedia/{(evo_instance or EVO_INSTANCE_DEFAULT)}",
                           payload, evo_base=evo_base)

async def _send_media_document(numero: str, caminho_video: str, caption: str,
                               evo_instance: str | None, evo_base: str | None):
    file_name = os.path.basename(caminho_video)
    b64 = _file_to_b64(caminho_video)
    payload = {
        "number": numero,
        "mediatype": "document",
        "fileName": file_name,
        "caption": caption or "",
        "media": b64,
        "mimetype": "video/mp4",
        "isBase64": True,
        "options": {"delay": 0, "presence": "composing"}
    }
    return await _evo_post(f"message/sendMedia/{(evo_instance or EVO_INSTANCE_DEFAULT)}",
                           payload, evo_base=evo_base)

async def enviar_video_via_whatsapp(
    caminho_video: str,
    telefone: str,
    caption: str = "",
    evo_instance: str | None = None,
    evo_base: str | None = None
):
    tamanho = os.path.getsize(caminho_video)
    prefer_video = tamanho <= WHATSAPP_VIDEO_SIZE_LIMIT_BYTES
    destinos = _mk_candidates(telefone)
    last_exc = None
    for dst in destinos:
        numero = dst["number"]
        for attempt in range(SEND_RETRIES + 1):
            try:
                if prefer_video:
                    return await _send_media_video(numero, caminho_video, caption, evo_instance, evo_base)
                else:
                    return await _send_media_document(numero, caminho_video, caption, evo_instance, evo_base)
            except httpx.HTTPError as e:
                last_exc = e
                if prefer_video:
                    prefer_video = False
                elif attempt < SEND_RETRIES:
                    time.sleep(SEND_BACKOFF_SEC)
    raise last_exc or RuntimeError("Falha ao enviar mídia via WhatsApp")

# =====================
# Pipeline principal
# =====================

async def processar_video(
    user_id: UUID,
    contatos: List[Dict[str, str]],
    palavra_chave: str,
    video_bytes: bytes,
    nome_video: str,
    evo_instance: str | None = None,
    evo_base: str | None = None
):
    voz_padrao_nome = f"user_{user_id}"
    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_video = salvar_video_em_disco(video_bytes, nome_video, pasta_temp)
        caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)

        if not transcricao:
            raise ValueError("Transcrição retornou vazia. Verifique o áudio original.")

        user_voice_id = await verificar_ou_criar_voz(voz_padrao_nome, caminho_audio, pasta_temp)

        def _norm(s: str) -> str: return (s or "").strip().casefold()
        seen = set()

        for c in contatos:
            nome = (c.get("nome") or "").strip()
            telefone = (c.get("telefone") or "").strip()
            if not nome or not telefone:
                continue

            key = (_norm(nome), _norm(telefone))
            if key in seen:
                continue
            seen.add(key)

            try:
                await enviar_texto_via_whatsapp(
                    telefone, f"Olá {nome}! (teste automático) 🚀",
                    evo_instance=evo_instance, evo_base=evo_base
                )

                caminho = await gerar_video_para_nome(
                    nome=nome,
                    palavra_chave=palavra_chave,
                    transcricao=transcricao,
                    segmentos=segmentos,
                    user_voice_id=user_voice_id,
                    caminho_video=caminho_video,
                    caminho_audio=caminho_audio,
                    pasta_temp=pasta_temp,
                    user_id=user_id,
                    enviar_webhook=False
                )

                await enviar_video_via_whatsapp(
                    caminho, telefone, caption=f"{nome}, seu vídeo personalizado.",
                    evo_instance=evo_instance, evo_base=evo_base
                )

            except (httpx.HTTPStatusError, ValueError, httpx.HTTPError, RuntimeError) as e:
                print(f"[ERR] Falha com contato '{nome}' ({telefone}): {e}")

def salvar_video_em_disco(video_bytes: bytes, nome_video: str, pasta_temp: str) -> str:
    caminho_video = os.path.join(pasta_temp, nome_video)
    with open(caminho_video, "wb") as f:
        f.write(video_bytes)
    return caminho_video

def extrair_audio_do_video(caminho_video: str, pasta_temp: str) -> str:
    caminho_audio = os.path.join(pasta_temp, "original_audio.wav")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_video, "-ac", "1", "-ar", "16000", "-vn", caminho_audio
    ], check=True)
    return caminho_audio

# =====================
# STT / Voz / Vídeo
# =====================

async def transcrever_audio_com_timestamps(caminho_audio: str) -> Tuple[str, List[Dict[str, Any]]]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        with open(caminho_audio, "rb") as audio_file:
            files = {"file": ("original.wav", audio_file, "audio/wav")}
            response = await client.post(f"{API_BASE_URL}/speech-to-text?detailed=true", files=files)
            response.raise_for_status()
            try:
                response_json = response.json()
            except json.JSONDecodeError:
                raise ValueError("Resposta da API de transcrição não é um JSON válido.")

    transcricao = (
        response_json.get("text", "")
        or response_json.get("transcribed", {}).get("text", "")
        or ""
    )

    brutos = (
        response_json.get("words")
        or response_json.get("transcribed", {}).get("words")
        or response_json.get("segments")
        or []
    )

    segmentos: List[Dict[str, Any]] = []
    for w in brutos:
        if not isinstance(w, dict):
            continue
        wtype = w.get("type")
        if wtype not in (None, "word", "token"):
            continue
        text = w.get("text") or w.get("word") or w.get("token")
        start = _to_seconds(w.get("start") or w.get("startTime") or w.get("start_sec"))
        end = _to_seconds(w.get("end") or w.get("endTime") or w.get("end_sec"))
        if not text or start is None or end is None:
            continue
        segmentos.append({"type": "word", "text": str(text), "start": float(start), "end": float(end)})

    return transcricao, segmentos

async def verificar_ou_criar_voz(voz_padrao_nome: str, caminho_audio: str, pasta_temp: str) -> str:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.get(f"{API_BASE_URL}/voices")
        response.raise_for_status()
        vozes = response.json()
        for voz in vozes:
            if voz.get("name") == voz_padrao_nome:
                return voz.get("voiceId") or voz.get("voice_id")

    caminho_convertido = os.path.join(pasta_temp, "converted_audio.wav")
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        with open(caminho_audio, "rb") as audio_file:
            files = {"file": ("original.wav", audio_file, "audio/wav")}
            response = await client.post(f"{API_BASE_URL}/convert-audio", files=files)
            response.raise_for_status()
            with open(caminho_convertido, "wb") as out_file:
                out_file.write(response.content)

    with open(caminho_convertido, "rb") as converted_file:
        files = [("file", ("converted_audio.wav", converted_file, "audio/wav"))]
        data = {"name": voz_padrao_nome, "language": "pt-BR"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(f"{API_BASE_URL}/add-voice", data=data, files=files)
            response.raise_for_status()
            response_json = response.json()
            vid = (
                response_json.get("voiceId")
                or response_json.get("voice_id")
                or response_json.get("voice", {}).get("voiceId")
            )
            return vid

async def gerar_video_para_nome(
    nome: str,
    palavra_chave: str,
    transcricao: str,
    segmentos: List[dict],
    user_voice_id: str,
    caminho_video: str,
    caminho_audio: str,
    pasta_temp: str,
    user_id: UUID,
    enviar_webhook: bool = True
):
    if not segmentos:
        raise ValueError("Transcrição não retornou palavras com timestamps (lista vazia).")

    alvo_norm = _normalize_token(palavra_chave)
    palavra_alvo = None
    idx = None
    for i, w in enumerate(segmentos):
        if w.get("type") != "word":
            continue
        token_norm = _normalize_token(w.get("text", ""))
        if token_norm == alvo_norm:
            palavra_alvo = w
            idx = i
            break

    if not palavra_alvo:
        raise ValueError(f"Palavra-chave '{palavra_chave}' não encontrada na transcrição.")

    if not user_voice_id:
        raise ValueError("ID da voz do usuário está vazio.")

    inicio = max(0.0, segmentos[max(0, idx - PALAVRAS_ANTES)]["start"] - (AJUSTE_MS / 1000))
    fim = segmentos[min(len(segmentos) - 1, idx + PALAVRAS_DEPOIS)]["end"] + (AJUSTE_MS / 1000)

    palavras_contexto = [
        w["text"] for w in segmentos
        if w.get("type") == "word" and inicio <= w["start"] and w["end"] <= fim
    ]
    texto_original = " ".join(palavras_contexto)

    formato_pausa = ". {nome}."
    nome_formatado = formato_pausa.format(nome=nome)
    novo_texto = texto_original.replace(palavra_alvo["text"], nome_formatado)

    payload = {"voiceId": user_voice_id, "text": novo_texto}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        tts_resp = await client.post(f"{API_BASE_URL}/text-to-speech", json=payload)
        tts_resp.raise_for_status()
        caminho_trecho_ia = os.path.join(pasta_temp, f"ia_{nome}.mp3")
        with open(caminho_trecho_ia, "wb") as f:
            f.write(tts_resp.content)

    caminho_audio_antes = os.path.join(pasta_temp, "antes.wav")
    caminho_audio_depois = os.path.join(pasta_temp, "depois.wav")
    caminho_audio_final = os.path.join(pasta_temp, f"audio_final_{nome}.wav")
    caminho_saida_video = os.path.join(pasta_temp, f"video_{nome}.mp4")

    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_audio, "-ss", "0", "-to", f"{inicio:.3f}", caminho_audio_antes
    ], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_audio, "-ss", f"{fim:.3f}", caminho_audio_depois
    ], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_audio_antes, "-i", caminho_trecho_ia, "-i", caminho_audio_depois,
        "-filter_complex", "[0:0][1:0][2:0]concat=n=3:v=0:a=1[out]",
        "-map", "[out]", caminho_audio_final
    ], check=True)
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_video, "-i", caminho_audio_final,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-c:a", "aac", "-shortest",
        caminho_saida_video
    ], check=True)

    if enviar_webhook:
        await enviar_video_para_webhook(caminho_saida_video, nome, user_id)

    return caminho_saida_video

async def enviar_video_para_webhook(caminho_video: str, nome: str, user_id: UUID, telefone: str | None = None):
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            with open(caminho_video, "rb") as final_video:
                files = {"file": (f"video_{nome}.mp4", final_video, "video/mp4")}
                data = {"user_id": str(user_id), "nome": nome}
                if telefone:
                    data["telefone"] = telefone
                await client.post(WEBHOOK_URL, data=data, files=files)
    except httpx.HTTPError as e:
        print(f"[WEBHOOK ERR] nome={nome} tel={telefone or '-'} err={e}")
