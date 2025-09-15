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
import re

# =====================
# Config
# =====================

# ---- Eleven: base só com /api; namespace separado e configurável ----
API_BASE_ROOT = os.getenv("ELEVEN_NODE_API", "https://api-elevenlabs-nodejs.onrender.com/api").rstrip("/")
ELEVEN_API_NS = (os.getenv("ELEVEN_API_NAMESPACE", "/elevenlabs") or "").strip()
ELEVEN_AUTH_URL = os.getenv("ELEVEN_AUTH_URL", "https://api-elevenlabs-nodejs.onrender.com/api/auth/login").strip()
ELEVEN_USERNAME = os.getenv("ELEVEN_USERNAME", "").strip()
ELEVEN_PASSWORD = os.getenv("ELEVEN_PASSWORD", "").strip()

# ---- Heygen: pronto para uso futuro (mesma abordagem) ----
HEYGEN_BASE_ROOT = os.getenv("HEYGEN_NODE_API", "https://api-heygen-nodejs.onrender.com/api").rstrip("/")
HEYGEN_API_NS = (os.getenv("HEYGEN_API_NAMESPACE", "/heygen") or "").strip()
HEYGEN_AUTH_URL = os.getenv("HEYGEN_AUTH_URL", "https://api-heygen-nodejs.onrender.com/api/auth/login").strip()
HEYGEN_USERNAME = os.getenv("HEYGEN_USERNAME", "").strip()
HEYGEN_PASSWORD = os.getenv("HEYGEN_PASSWORD", "").strip()

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://webhook.site/150557f8-3946-478e-8013-d5fedf0e56f2")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "120.0"))
PALAVRAS_ANTES = int(os.getenv("PALAVRAS_ANTES", "2"))
PALAVRAS_DEPOIS = int(os.getenv("PALAVRAS_DEPOIS", "0"))
AJUSTE_MS = int(os.getenv("AJUSTE_MS", "150"))  # ms

# Evolution API
EVO_BASE_DEFAULT     = os.getenv("EVO_BASE", "http://localhost:8080").rstrip("/")
EVO_APIKEY_DEFAULT   = os.getenv("EVO_APIKEY", "")
EVO_INSTANCE_DEFAULT = os.getenv("EVO_INSTANCE", "default")
EVO_INTEGRATION      = os.getenv("EVO_INTEGRATION", "WHATSAPP-BAILEYS")  # exigido no create

# Rotas oficiais (sem /v1)
EVO_CREATE_PATH   = "instance/create"        # POST
EVO_CONNECT_PATH  = "instance/connect"       # GET /instance/connect/{instance}
EVO_STATUS_PATH   = "instance/connection"    # GET /instance/connection/{instance}
EVO_DELETE_PATH   = "instances"              # DELETE /instances/{instance}

WHATSAPP_VIDEO_SIZE_LIMIT_BYTES = 100 * 1024 * 1024  # ~100 MB
SEND_RETRIES = 2
SEND_BACKOFF_SEC = 2.0

# =====================
# Helpers
# =====================

def _evo_headers(include_json: bool = False) -> dict:
    # Doc exige 'apikey'; manter também Authorization funciona em algumas builds
    h = {
        "apikey": EVO_APIKEY_DEFAULT,
        "Authorization": f"Bearer {EVO_APIKEY_DEFAULT}",
    }
    if include_json:
        h["Content-Type"] = "application/json"
    return h

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

def sanitize_username(name: str) -> str:
    """
    Gera um slug seguro para o nome do usuário:
    - remove acentos
    - mantém [a-z0-9] e separa grupos por '-'
    - minúsculas
    """
    if not name:
        return "user"
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    # troca qualquer sequência não-alfanumérica por "-"
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return n or "user"

def make_instance_name(user_name: str, user_id: UUID) -> str:
    """
    Concatena (nomeUsuario_uuid) garantindo um nome seguro.
    - NÃO altera o UUID
    - Mantém o padrão pedido: nomeUsuario_id
    """
    slug = sanitize_username(user_name)
    return f"{slug}_{user_id}"

# =====================
# Evolution HTTP helpers
# =====================

async def _evo_post(path: str, payload: dict, evo_base: str | None = None):
    url = f"{(evo_base or EVO_BASE_DEFAULT).rstrip('/')}/{path.lstrip('/')}"
    headers = _evo_headers(include_json=True)
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(url, headers=headers, json=payload)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

async def _evo_get(path: str, evo_base: str | None = None):
    url = f"{(evo_base or EVO_BASE_DEFAULT).rstrip('/')}/{path.lstrip('/')}"
    headers = _evo_headers()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

async def _evo_delete(path: str, evo_base: str | None = None):
    url = f"{(evo_base or EVO_BASE_DEFAULT).rstrip('/')}/{path.lstrip('/')}"
    headers = _evo_headers()
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.delete(url, headers=headers)
        resp.raise_for_status()
        ct = resp.headers.get("content-type", "")
        return resp.json() if ct and "application/json" in ct else resp.text

# =====================
# Auth helpers (Eleven / Heygen)
# =====================

# ---- Eleven ----
_eleven_token: str | None = None
_eleven_token_expire_ts: float = 0.0  # epoch seconds

def _eleven_url(path: str) -> str:
    """
    Monta URL de recurso Eleven a partir do root /api e namespace configurável.
    Ex.: /speech-to-text => https://host/api/<ns>/speech-to-text
    """
    base = API_BASE_ROOT.rstrip("/")
    ns = (ELEVEN_API_NS or "").strip()
    if ns and not ns.startswith("/"):
        ns = "/" + ns
    return f"{base}{ns}/{path.lstrip('/')}"

async def _eleven_login(force: bool = False) -> str:
    """
    Faz login na API Eleven e devolve o token (cacheado). Renova se expirado/force.
    """
    global _eleven_token, _eleven_token_expire_ts
    now = time.time()
    SAFETY_TTL = 50 * 60  # 50 minutos

    if not force and _eleven_token and now < _eleven_token_expire_ts:
        return _eleven_token

    if not ELEVEN_USERNAME or not ELEVEN_PASSWORD:
        raise RuntimeError("Credenciais Eleven não configuradas (ELEVEN_USERNAME / ELEVEN_PASSWORD).")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            ELEVEN_AUTH_URL,
            json={"username": ELEVEN_USERNAME, "password": ELEVEN_PASSWORD},
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

    token = (
        data.get("token")
        or data.get("access_token")
        or data.get("accessToken")
        or data.get("jwt")
    )
    if not token:
        if isinstance(data, str) and data.strip():
            token = data.strip()
        else:
            raise RuntimeError(f"Login Eleven não retornou token. Resposta: {data!r}")

    _eleven_token = token
    _eleven_token_expire_ts = now + SAFETY_TTL
    return _eleven_token

async def _eleven_headers(include_json: bool = False) -> dict:
    token = await _eleven_login()
    h = {"Authorization": f"Bearer {token}"}
    if include_json:
        h["Content-Type"] = "application/json"
    return h

async def _eleven_request(method: str, url: str, **kwargs) -> httpx.Response:
    """
    Faz requisição autenticada; se 401, renova token e tenta de novo (1x).
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code == 401:
            await _eleven_login(force=True)
            headers = kwargs.get("headers", {}) or {}
            headers["Authorization"] = f"Bearer {_eleven_token}"
            kwargs["headers"] = headers
            resp = await client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

# ---- Heygen (preparado para uso futuro) ----
_heygen_token: str | None = None
_heygen_token_expire_ts: float = 0.0

def _heygen_url(path: str) -> str:
    base = HEYGEN_BASE_ROOT.rstrip("/")
    ns = (HEYGEN_API_NS or "").strip()
    if ns and not ns.startswith("/"):
        ns = "/" + ns
    return f"{base}{ns}/{path.lstrip('/')}"

async def _heygen_login(force: bool = False) -> str:
    global _heygen_token, _heygen_token_expire_ts
    now = time.time()
    SAFETY_TTL = 50 * 60

    if not force and _heygen_token and now < _heygen_token_expire_ts:
        return _heygen_token

    if not HEYGEN_USERNAME or not HEYGEN_PASSWORD:
        raise RuntimeError("Credenciais Heygen não configuradas (HEYGEN_USERNAME / HEYGEN_PASSWORD).")

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.post(
            HEYGEN_AUTH_URL,
            json={"username": HEYGEN_USERNAME, "password": HEYGEN_PASSWORD},
            headers={"Content-Type": "application/json"}
        )
        resp.raise_for_status()
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}

    token = (
        data.get("token")
        or data.get("access_token")
        or data.get("accessToken")
        or data.get("jwt")
    )
    if not token:
        if isinstance(data, str) and data.strip():
            token = data.strip()
        else:
            raise RuntimeError(f"Login Heygen não retornou token. Resposta: {data!r}")

    _heygen_token = token
    _heygen_token_expire_ts = now + SAFETY_TTL
    return _heygen_token

async def _heygen_headers(include_json: bool = False) -> dict:
    token = await _heygen_login()
    h = {"Authorization": f"Bearer {token}"}
    if include_json:
        h["Content-Type"] = "application/json"
    return h

async def _heygen_request(method: str, url: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        resp = await client.request(method, url, **kwargs)
        if resp.status_code == 401:
            await _heygen_login(force=True)
            headers = kwargs.get("headers", {}) or {}
            headers["Authorization"] = f"Bearer {_heygen_token}"
            kwargs["headers"] = headers
            resp = await client.request(method, url, **kwargs)
        resp.raise_for_status()
        return resp

# =====================
# Evolution: criação/ conexão / status / logout
# =====================

async def evo_create_user_instance(user_name: str, user_id: UUID, evo_base: str | None = None) -> Dict[str, Any]:
    instance_name = make_instance_name(user_name, user_id)
    payload = {
        "instanceName": instance_name,
        "integration": EVO_INTEGRATION,
        "qrcode": False,
    }
    try:
        resp = await _evo_post(EVO_CREATE_PATH, payload, evo_base=evo_base)
        return {"instance": instance_name, "create": resp}
    except httpx.HTTPStatusError as e:
        status = e.response.status_code if e.response else None
        if status == 403:
            try:
                detail = e.response.json()
            except Exception:
                detail = {"raw": e.response.text if e.response else str(e)}
            return {"instance": instance_name, "create": {"__status__": 403, "__error__": True, "response": detail}}
        elif status == 400:
            try:
                detail = e.response.json()
            except Exception:
                detail = {"raw": e.response.text if e.response else str(e)}
            raise RuntimeError(json.dumps({
                "message": "Falha ao criar instância (verifique EVO_INTEGRATION).",
                "instanceName": instance_name,
                "integration_sent": EVO_INTEGRATION,
                "create_return": detail
            }, ensure_ascii=False))
        raise

async def evo_connect(instance: str, evo_base: str | None = None) -> Dict[str, Any] | str:
    instance_name = (instance or "").strip()
    if not instance_name:
        raise ValueError("instance inválida.")
    try:
        return await _evo_get(f"{EVO_CONNECT_PATH}/{instance_name}", evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        detail = None
        try:
            detail = e.response.json()
        except Exception:
            pass
        raise RuntimeError(json.dumps({
            "message": "Instância não encontrada ao tentar connect(). Verifique o nome/casing.",
            "instanceName": instance_name,
            "connect_return": detail or (e.response.text if e.response else str(e))
        }, ensure_ascii=False)) from e

async def evo_start_session(instance: str, evo_base: str | None = None):
    return {"instance": instance, "qr": await evo_connect(instance, evo_base=evo_base)}

async def evo_status(instance: str, evo_base: str | None = None):
    instance_name = (instance or "").strip()
    if not instance_name:
        raise ValueError("instance inválida.")
    try:
        return await _evo_get(f"{EVO_STATUS_PATH}/{instance_name}", evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            raise RuntimeError(json.dumps({
                "message": "Instância não encontrada no status(). Verifique se o nome está correto (case-sensitive).",
                "instanceName": instance_name
            }, ensure_ascii=False)) from e
        raise

async def evo_logout(instance: str, evo_base: str | None = None):
    instance_name = (instance or "").strip()
    if not instance_name:
        raise ValueError("instance inválida.")
    return await _evo_delete(f"{EVO_DELETE_PATH}/{instance_name}", evo_base=evo_base)

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
# STT / Voz / Vídeo (Eleven)
# =====================

async def transcrever_audio_com_timestamps(caminho_audio: str) -> Tuple[str, List[Dict[str, Any]]]:
    with open(caminho_audio, "rb") as audio_file:
        files = {"file": ("original.wav", audio_file, "audio/wav")}
        headers = await _eleven_headers()
        response = await _eleven_request(
            "POST",
            _eleven_url("speech-to-text?detailed=true"),
            files=files,
            headers=headers
        )
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
    headers = await _eleven_headers()
    response = await _eleven_request("GET", _eleven_url("voices"), headers=headers)
    vozes = response.json()
    for voz in vozes:
        if voz.get("name") == voz_padrao_nome:
            return voz.get("voiceId") or voz.get("voice_id")

    caminho_convertido = os.path.join(pasta_temp, "converted_audio.wav")
    with open(caminho_audio, "rb") as audio_file:
        files = {"file": ("original.wav", audio_file, "audio/wav")}
        response = await _eleven_request("POST", _eleven_url("convert-audio"), files=files, headers=headers)
        with open(caminho_convertido, "wb") as out_file:
            out_file.write(response.content)

    with open(caminho_convertido, "rb") as converted_file:
        files = [("file", ("converted_audio.wav", converted_file, "audio/wav"))]
        data = {"name": voz_padrao_nome, "language": "pt-BR"}
        response = await _eleven_request(
            "POST",
            _eleven_url("add-voice"),
            data=data,
            files=files,
            headers=headers
        )
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
    headers = await _eleven_headers(include_json=True)
    tts_resp = await _eleven_request("POST", _eleven_url("text-to-speech"), json=payload, headers=headers)
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
