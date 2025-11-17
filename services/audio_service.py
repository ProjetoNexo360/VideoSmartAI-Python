# services/audio_service.py
import os
import tempfile
import httpx
import subprocess
import asyncio
from typing import List, Dict, Any, Tuple, Optional, Callable
from uuid import UUID
import json
import unicodedata
import base64
import time
import re
from urllib.parse import quote
from difflib import get_close_matches

# =====================
# Config
# =====================

# ---- Eleven: base só com /api; namespace separado e configurável ----
API_BASE_ROOT = os.getenv("ELEVEN_NODE_API", "https://api-elevenlabs-nodejs.onrender.com/api").rstrip("/")
ELEVEN_API_NS = (os.getenv("ELEVEN_API_NAMESPACE", "/elevenlabs") or "").strip()
ELEVEN_AUTH_URL = os.getenv("ELEVEN_AUTH_URL", "https://api-elevenlabs-nodejs.onrender.com/api/auth/login").strip()
ELEVEN_USERNAME = os.getenv("ELEVEN_USERNAME", "").strip()
ELEVEN_PASSWORD = os.getenv("ELEVEN_PASSWORD", "").strip()

# ---- Heygen ----
HEYGEN_BASE_ROOT = os.getenv("HEYGEN_NODE_API", "https://api-heygen-nodejs.onrender.com/api").rstrip("/")
HEYGEN_API_NS = (os.getenv("HEYGEN_API_NAMESPACE", "") or "").strip()
HEYGEN_AUTH_URL = os.getenv("HEYGEN_AUTH_URL", "https://api-heygen-nodejs.onrender.com/api/auth/login").strip()
HEYGEN_USERNAME = os.getenv("HEYGEN_USERNAME", "").strip()
HEYGEN_PASSWORD = os.getenv("HEYGEN_PASSWORD", "").strip()

# Logs Heygen
HEYGEN_DEBUG = os.getenv("HEYGEN_DEBUG", "1").strip() not in ("0", "false", "False", "")

# API de Automação (importação de vozes)
AUTOMATION_API_BASE = os.getenv("AUTOMATION_API_BASE", "http://localhost:3000").rstrip("/")

WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://webhook.site/150557f8-3946-478e-8013-d5fedf0e56f2")
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "120.0"))
PALAVRAS_ANTES = int(os.getenv("PALAVRAS_ANTES", "2"))
PALAVRAS_DEPOIS = int(os.getenv("PALAVRAS_DEPOIS", "0"))
AJUSTE_MS = int(os.getenv("AJUSTE_MS", "150"))  # ms
HEYGEN_MIN_VIDEO_DURATION = float(os.getenv("HEYGEN_MIN_VIDEO_DURATION", "5.0"))  # segundos mínimos para o vídeo da Heygen

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
    if not name:
        return "user"
    n = unicodedata.normalize("NFKD", name)
    n = "".join(c for c in n if not unicodedata.combining(c))
    n = n.lower()
    n = re.sub(r"[^a-z0-9]+", "-", n).strip("-")
    return n or "user"

def make_instance_name(user_name: str, user_id: UUID) -> str:
    slug = sanitize_username(user_name)
    return f"{slug}_{user_id}"

# ===== Logs helpers (Heygen) =====

def _mask(s: str | None) -> str:
    if not s:
        return ""
    if len(s) <= 12:
        return "*" * len(s)
    return s[:6] + "..." + s[-4:]

def _safe_json_dump(obj: Any, max_len: int = 4000) -> str:
    try:
        out = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        out = str(obj)
    if len(out) > max_len:
        return out[:max_len] + f"\n... (truncado, {len(out)-max_len} chars)"
    return out

def _log_heygen_request(method: str, url: str, headers: dict | None, json_body: Any = None, data: Any = None, files: Any = None):
    if not HEYGEN_DEBUG:
        return
    print("\n[HEYGEN][REQUEST]")
    print(f"  {method} {url}")
    if headers:
        redacted = {**headers}
        if "Authorization" in redacted:
            tok = (redacted["Authorization"] or "")
            tok = tok.replace("Bearer", "").strip()
            redacted["Authorization"] = f"Bearer {_mask(tok)}"
        print("  headers:", _safe_json_dump(redacted))
    if json_body is not None:
        print("  json:", _safe_json_dump(json_body))
    if data is not None:
        print("  data:", _safe_json_dump(data))
    if files is not None:
        try:
            if isinstance(files, dict):
                names = {k: (v[0] if isinstance(v, (list, tuple)) else getattr(v, "name", None)) for k, v in files.items()}
            else:
                names = str(type(files))
        except Exception:
            names = "files=<unlogged>"
        print("  files:", _safe_json_dump(names))

def _log_heygen_response(resp: httpx.Response):
    if not HEYGEN_DEBUG:
        return
    print("[HEYGEN][RESPONSE]")
    try:
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            print(f"  status={resp.status_code}")
            print("  body:", _safe_json_dump(resp.json()))
        else:
            print(f"  status={resp.status_code}")
            print("  body(text):", _safe_json_dump(resp.text))
    except Exception as e:
        print("  <erro ao logar resposta>", e)

def _log_heygen_error(e: Exception, extra: dict | None = None):
    if not HEYGEN_DEBUG:
        return
    print("[HEYGEN][ERROR]", repr(e))
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
        try:
            print("  status:", e.response.status_code)
            _log_heygen_response(e.response)
        except Exception:
            pass
    if extra:
        print("  extra:", _safe_json_dump(extra))

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
    base = API_BASE_ROOT.rstrip("/")
    ns = (ELEVEN_API_NS or "").strip()
    if ns and not ns.startswith("/"):
        ns = "/" + ns
    return f"{base}{ns}/{path.lstrip('/')}"

async def _eleven_login(force: bool = False) -> str:
    global _eleven_token, _eleven_token_expire_ts
    now = time.time()
    SAFETY_TTL = 50 * 60
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
    token = data.get("token") or data.get("access_token") or data.get("accessToken") or data.get("jwt")
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
    Faz requisição para a API Eleven com retry para ReadError e timeout configurável.
    """
    # Para uploads/processamento, usa timeout maior (connect + read separados)
    is_upload = kwargs.get("files") is not None
    if is_upload:
        # Timeout maior para uploads: 60s connect + 300s read (5min para processar)
        timeout = httpx.Timeout(60.0, read=300.0, write=60.0, connect=60.0)
    else:
        timeout = HTTP_TIMEOUT
    
    max_retries = 3
    last_error = None
    
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, **kwargs)
                if resp.status_code == 401:
                    await _eleven_login(force=True)
                    headers = kwargs.get("headers", {}) or {}
                    headers["Authorization"] = f"Bearer {_eleven_token}"
                    kwargs["headers"] = headers
                    resp = await client.request(method, url, **kwargs)
                resp.raise_for_status()
                return resp
        except (httpx.ReadError, httpx.ConnectError, httpx.NetworkError) as e:
            last_error = e
            if attempt < max_retries - 1:
                wait_time = (attempt + 1) * 2  # 2s, 4s, 6s
                print(f"[ELEVEN] Erro de conexão (tentativa {attempt + 1}/{max_retries}): {e}. Aguardando {wait_time}s...")
                await asyncio.sleep(wait_time)
            else:
                print(f"[ELEVEN] Falha após {max_retries} tentativas: {e}")
                raise
        except httpx.HTTPStatusError as e:
            # Erros HTTP não devem ser retentados
            raise
    
    # Se chegou aqui, todas as tentativas falharam
    if last_error:
        raise last_error
    raise RuntimeError("Falha desconhecida na requisição Eleven")

# ---- Heygen ----
_heygen_token: str | None = None
_heygen_token_expire_ts: float = 0.0

def _heygen_url(path: str) -> str:
    """
    Monta a URL da Heygen.
    Os endpoints públicos do Swagger ficam diretamente em /api/<path> (sem namespace extra).
    Se HEYGEN_API_NS estiver vazio, usamos direto.
    Se vier um namespace por engano, ignoramos para rotas conhecidas.
    """
    base = HEYGEN_BASE_ROOT.rstrip("/")  # ex: https://api-heygen-nodejs.onrender.com/api
    p = path.lstrip("/")
    ns = (HEYGEN_API_NS or "").strip()

    # Rotas que NÃO usam namespace (conforme Swagger)
    # Adicionado "voices" para usar a rota correta: /api/voices
    no_ns_prefixes = ("photo-avatar", "videos", "auth", "voices")
    if not ns or p.startswith(no_ns_prefixes):
        return f"{base}/{p}"

    if not ns.startswith("/"):
        ns = "/" + ns
    return f"{base}{ns}/{p}"

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
    token = data.get("token") or data.get("access_token") or data.get("accessToken") or data.get("jwt")
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
    """
    Wrapper com logs detalhados + refresh de token em 401.
    """
    try:
        _log_heygen_request(
            method=method,
            url=url,
            headers=kwargs.get("headers"),
            json_body=kwargs.get("json"),
            data=kwargs.get("data"),
            files=kwargs.get("files"),
        )
    except Exception:
        pass

    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        try:
            resp = await client.request(method, url, **kwargs)
            if resp.status_code == 401:
                await _heygen_login(force=True)
                headers = kwargs.get("headers", {}) or {}
                headers["Authorization"] = f"Bearer {(_heygen_token or '').strip()}"
                kwargs["headers"] = headers

                _log_heygen_request(
                    method=method,
                    url=url,
                    headers=kwargs.get("headers"),
                    json_body=kwargs.get("json"),
                    data=kwargs.get("data"),
                    files=kwargs.get("files"),
                )

                resp = await client.request(method, url, **kwargs)

            _log_heygen_response(resp)
            resp.raise_for_status()
            return resp

        except Exception as e:
            _log_heygen_error(e, extra={"url": url, "method": method})
            raise

# =====================
# Automação: importação de vozes
# =====================

async def importar_voz_para_heygen(voice_name: str) -> None:
    """
    Chama o endpoint de automação para importar a voz do ElevenLabs para a Heygen.
    """
    url = f"{AUTOMATION_API_BASE}/import-voice"
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json={"voiceName": voice_name})
            resp.raise_for_status()
            print(f"[AUTOMATION] Voz '{voice_name}' importada para Heygen com sucesso")
    except httpx.HTTPStatusError as e:
        print(f"[AUTOMATION] Erro ao importar voz '{voice_name}': {e.response.status_code} - {e.response.text}")
        # Não levanta exceção para não quebrar o fluxo, mas loga o erro
    except Exception as e:
        print(f"[AUTOMATION] Erro ao chamar endpoint de importação: {e}")
        # Não levanta exceção para não quebrar o fluxo

async def heygen_listar_vozes() -> List[Dict[str, Any]]:
    """
    Lista todas as vozes disponíveis na Heygen.
    Estrutura esperada: {"error": null, "data": {"voices": [...]}}
    """
    url = _heygen_url("voices")
    headers = await _heygen_headers()
    try:
        resp = await _heygen_request("GET", url, headers=headers)
        raw = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        
        # Estrutura: {"error": null, "data": {"voices": [...]}}
        items = []
        if isinstance(raw, dict):
            if "data" in raw and isinstance(raw["data"], dict):
                if "voices" in raw["data"] and isinstance(raw["data"]["voices"], list):
                    items = raw["data"]["voices"]
            # Fallback para outras estruturas
            elif "voices" in raw and isinstance(raw["voices"], list):
                items = raw["voices"]
            elif "items" in raw and isinstance(raw["items"], list):
                items = raw["items"]
            elif "data" in raw and isinstance(raw["data"], list):
                items = raw["data"]
        
        print(f"[HEYGEN] Listou {len(items)} vozes")
        return items if isinstance(items, list) else []
    except Exception as e:
        _log_heygen_error(e, extra={"url": url})
        return []

async def heygen_buscar_voz_por_nome(voice_name: str) -> Optional[str]:
    """
    Busca uma voz na Heygen pelo campo 'name' e retorna o voice_id.
    Estrutura esperada: {"voice_id": "...", "name": "..."}
    """
    vozes = await heygen_listar_vozes()
    voice_name_norm = (voice_name or "").strip().casefold()
    
    for voz in vozes:
        # Busca pelo campo 'name' (conforme estrutura do JSON)
        nome_voz = (voz.get("name") or "").strip()
        if nome_voz and nome_voz.casefold() == voice_name_norm:
            voice_id = (
                voz.get("voice_id") 
                or voz.get("voiceId") 
                or voz.get("id")
            )
            if voice_id:
                print(f"[HEYGEN] Voz encontrada pelo nome: '{voice_name}' -> voice_id={voice_id}")
                return voice_id
    
    print(f"[HEYGEN] Voz '{voice_name}' não encontrada na Heygen (total de {len(vozes)} vozes verificadas)")
    return None

# =====================
# Evolution: resolução de instância
# =====================

async def _evo_list_instances(evo_base: str | None = None) -> list[str]:
    try:
        data = await _evo_get("instances", evo_base=evo_base)
        if isinstance(data, list):
            out: list[str] = []
            for it in data:
                if isinstance(it, str):
                    out.append(it)
                elif isinstance(it, dict):
                    out.append(it.get("instanceName") or it.get("name") or it.get("id") or "")
            return [x for x in out if x]
    except Exception:
        pass
    return []

async def _evo_resolve_instance_name(candidate: str, evo_base: str | None = None) -> str | None:
    cand = (candidate or "").strip()
    if not cand:
        return None
    names = await _evo_list_instances(evo_base=evo_base)
    if not names:
        return None
    if cand in names:
        return cand
    lower_map = {n.lower(): n for n in names if isinstance(n, str)}
    if cand.lower() in lower_map:
        return lower_map[cand.lower()]
    close = get_close_matches(cand.lower(), list(lower_map.keys()), n=1, cutoff=0.8)
    if close:
        return lower_map[close[0]]
    return None

def _escape_instance(instance: str) -> str:
    return quote(instance, safe="")

# =====================
# Evolution: criação/ conexão / status / logout
# =====================

async def evo_create_user_instance(user_name: str, user_id: UUID, evo_base: str | None = None) -> Dict[str, Any]:
    instance_name = make_instance_name(user_name, user_id)
    payload = {"instanceName": instance_name, "integration": EVO_INTEGRATION, "qrcode": False}
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
    esc = _escape_instance(instance_name)
    try:
        return await _evo_get(f"{EVO_CONNECT_PATH}/{esc}", evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            resolved = await _evo_resolve_instance_name(instance_name, evo_base=evo_base)
            if resolved and resolved != instance_name:
                return await _evo_get(f"{EVO_CONNECT_PATH}/{_escape_instance(resolved)}", evo_base=evo_base)
            names = await _evo_list_instances(evo_base=evo_base)
            raise RuntimeError(json.dumps({
                "message": "Instância não encontrada ao tentar connect(). Verifique o nome/casing.",
                "instanceName": instance_name,
                "availableInstances": names
            }, ensure_ascii=False)) from e
        raise

async def evo_start_session(instance: str, evo_base: str | None = None):
    return {"instance": instance, "qr": await evo_connect(instance, evo_base=evo_base)}

async def evo_status(instance: str, evo_base: str | None = None):
    instance_name = (instance or "").strip()
    if not instance_name:
        raise ValueError("instance inválida.")
    esc = _escape_instance(instance_name)
    try:
        return await _evo_get(f"{EVO_STATUS_PATH}/{esc}", evo_base=evo_base)
    except httpx.HTTPStatusError as e:
        if e.response is not None and e.response.status_code == 404:
            resolved = await _evo_resolve_instance_name(instance_name, evo_base=evo_base)
            if resolved and resolved != instance_name:
                return await _evo_get(f"{EVO_STATUS_PATH}/{_escape_instance(resolved)}", evo_base=evo_base)
            names = await _evo_list_instances(evo_base=evo_base)
            raise RuntimeError(json.dumps({
                "message": "Instância não encontrada no status(). Verifique o nome exato (case-sensitive).",
                "instanceName": instance_name,
                "availableInstances": names
            }, ensure_ascii=False)) from e
        raise

async def evo_logout(instance: str, evo_base: str | None = None):
    instance_name = (instance or "").strip()
    if not instance_name:
        raise ValueError("instance inválida.")
    return await _evo_delete(f"{EVO_DELETE_PATH}/{_escape_instance(instance_name)}", evo_base=evo_base)

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
        payload = {**dst, "text": texto, "options": {"delay": 0, "presence": "composing", "linkPreview": False}}
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
    foto_bytes: bytes,
    nome_foto: str,
    audio_bytes: bytes,
    nome_audio: str,
    evo_instance: str | None = None,
    evo_base: str | None = None,
    heygen_group_id: Optional[str] = None,
    save_group_id_async: Optional[Callable[[UUID, str], Any]] = None,
):
    voz_padrao_nome = f"user_{user_id}"  # nome da voice Eleven
    avatar_group_name = f"{voz_padrao_nome}"  # manter mesmo padrão "nome_id"
    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_foto = salvar_imagem_em_disco(foto_bytes, nome_foto, pasta_temp)
        caminho_audio = salvar_audio_em_wav(audio_bytes, nome_audio, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)

        if not transcricao:
            raise ValueError("Transcrição retornou vazia. Verifique o áudio original.")

        # 1) voice Eleven (mantemos criação/checar — Heygen usa voice_id daqui)
        user_voice_id = await verificar_ou_criar_voz(voz_padrao_nome, caminho_audio, pasta_temp)

        # 2) avatar Heygen (group_id) — cria/usa existente (AGORA: 10 fotos)
        group_id = await heygen_verificar_ou_criar_avatar_do_usuario(
            user_group_name=avatar_group_name,
            source_image=caminho_foto,
            segmentos=segmentos,
            palavra_chave=palavra_chave,
            pasta_temp=pasta_temp,
            num_fotos=10,
            existing_group_id=heygen_group_id,
            user_id=user_id,
            save_group_id_async=save_group_id_async,
        )

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
                    user_voice_id=user_voice_id,   # usado na Heygen
                    caminho_audio=caminho_audio,
                    caminho_foto=caminho_foto,
                    pasta_temp=pasta_temp,
                    user_id=user_id,
                    group_id=group_id
                )

                await enviar_video_via_whatsapp(
                    caminho, telefone, caption=f"{nome}, seu vídeo personalizado.",
                    evo_instance=evo_instance, evo_base=evo_base
                )

            except (httpx.HTTPStatusError, ValueError, httpx.HTTPError, RuntimeError, subprocess.CalledProcessError) as e:
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

def _nome_seguro(nome: Optional[str], padrao: str) -> str:
    base = (nome or "").strip() or padrao
    base = os.path.basename(base)
    return base or padrao

def salvar_imagem_em_disco(foto_bytes: bytes, nome_foto: Optional[str], pasta_temp: str) -> str:
    nome = _nome_seguro(nome_foto, "foto_usuario.jpg")
    if "." not in os.path.basename(nome):
        nome = f"{nome}.jpg"
    caminho_foto = os.path.join(pasta_temp, nome)
    with open(caminho_foto, "wb") as f:
        f.write(foto_bytes)
    return caminho_foto

def salvar_audio_em_wav(audio_bytes: bytes, nome_audio: Optional[str], pasta_temp: str) -> str:
    nome = _nome_seguro(nome_audio, "audio_usuario")
    caminho_original = os.path.join(pasta_temp, nome)
    with open(caminho_original, "wb") as f:
        f.write(audio_bytes)

    caminho_audio = os.path.join(pasta_temp, "original_audio.wav")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", caminho_original, "-ac", "1", "-ar", "16000", caminho_audio
    ], check=True)
    return caminho_audio

# =====================
# STT / Voz (Eleven)
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
        
        # Após criar a voz, importa para a Heygen
        if vid:
            await importar_voz_para_heygen(voz_padrao_nome)
            # Aguarda 3 segundos para garantir que a importação seja processada antes de buscar
            print(f"[AUTOMATION] Aguardando 3 segundos após importação para processamento...")
            await asyncio.sleep(3.0)
        
        return vid

# =====================
# HEYGEN – Avatar + Trecho de Vídeo
# =====================

def _norm_token(s: str) -> str:
    import unicodedata as _ud, re as _re
    s = _ud.normalize("NFKD", s or "")
    s = "".join(c for c in s if not _ud.combining(c))
    return _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def _extrair_intervalo_por_palavra(segmentos: List[dict], palavra_chave: str, min_duration: Optional[float] = None) -> Tuple[float, float, str]:
    """
    Extrai intervalo de tempo ao redor da palavra-chave.
    Se min_duration for fornecido (ou HEYGEN_MIN_VIDEO_DURATION configurado),
    garante que o intervalo tenha pelo menos essa duração mínima.
    """
    if not segmentos:
        raise ValueError("Lista de segmentos vazia.")
    
    # Usa min_duration fornecido ou a variável de ambiente
    if min_duration is None:
        min_duration = HEYGEN_MIN_VIDEO_DURATION
    
    # Encontra a palavra-chave nos segmentos
    alvo = _norm_token(palavra_chave)
    idx = None
    for i, w in enumerate(segmentos):
        if str(w.get("type")) != "word":
            continue
        if _norm_token(w.get("text") or "") == alvo:
            idx = i
            break
    if idx is None:
        raise ValueError(f"Palavra-chave '{palavra_chave}' não encontrada nos segmentos.")
    
    # Calcula intervalo inicial baseado nas palavras antes e depois
    inicio = max(0.0, float(segmentos[max(0, idx - PALAVRAS_ANTES)]["start"]) - (AJUSTE_MS / 1000))
    fim = float(segmentos[min(len(segmentos)-1, idx + PALAVRAS_DEPOIS)]["end"]) + (AJUSTE_MS / 1000)
    
    # Obtém a duração total do vídeo (último segmento com "end")
    duracao_total = 0.0
    for seg in reversed(segmentos):
        end_val = seg.get("end")
        if end_val is not None:
            try:
                duracao_total = float(end_val)
                break
            except (ValueError, TypeError):
                continue
    
    # Se não encontrou duração total, tenta usar o último "end" ou "start" disponível
    if duracao_total == 0.0:
        for seg in reversed(segmentos):
            start_val = seg.get("start")
            if start_val is not None:
                try:
                    # Assume que o vídeo termina um pouco depois do último segmento
                    duracao_total = float(start_val) + 1.0
                    break
                except (ValueError, TypeError):
                    continue
    
    # Calcula o tempo central da palavra-chave (meio do intervalo da palavra)
    palavra_start = float(segmentos[idx].get("start", inicio))
    palavra_end = float(segmentos[idx].get("end", fim))
    tempo_central = (palavra_start + palavra_end) / 2.0
    
    # Verifica se precisa expandir o intervalo para atingir a duração mínima
    duracao_atual = fim - inicio
    if duracao_atual < min_duration and duracao_total > 0:
        # Expande simetricamente ao redor do tempo central da palavra-chave
        # Calcula quanto expandir de cada lado
        expansao_total = min_duration - duracao_atual
        expansao_por_lado = expansao_total / 2.0
        
        # Expande para trás (início) e para frente (fim)
        novo_inicio = max(0.0, tempo_central - (min_duration / 2.0))
        novo_fim = min(duracao_total, tempo_central + (min_duration / 2.0))
        
        # Se chegou no limite do vídeo em um lado, expande mais no outro lado
        if novo_inicio <= 0.0:
            # Não pode expandir mais para trás, expande tudo para frente
            novo_inicio = 0.0
            novo_fim = min(duracao_total, min_duration)
        elif novo_fim >= duracao_total:
            # Não pode expandir mais para frente, expande tudo para trás
            novo_fim = duracao_total
            novo_inicio = max(0.0, duracao_total - min_duration)
        
        # Garante que não ultrapasse os limites
        novo_inicio = max(0.0, novo_inicio)
        novo_fim = min(duracao_total, novo_fim)
        
        # Atualiza apenas se a nova duração for maior que a atual
        if (novo_fim - novo_inicio) > duracao_atual:
            inicio = novo_inicio
            fim = novo_fim
            print(f"[VIDEO] Intervalo expandido para garantir duração mínima: {min_duration}s (de {duracao_atual:.2f}s para {fim - inicio:.2f}s)")
        else:
            print(f"[VIDEO] WARNING: Não foi possível expandir intervalo para {min_duration}s (duração total do vídeo: {duracao_total:.2f}s)")
    elif duracao_total == 0.0:
        print(f"[VIDEO] WARNING: Não foi possível determinar duração total do vídeo. Usando intervalo original: {duracao_atual:.2f}s")

    # Coleta palavras do contexto expandido
    palavras_contexto = [
        (w.get("text") or "") for w in segmentos
        if str(w.get("type")) == "word" and inicio <= float(w.get("start",0)) and float(w.get("end",0)) <= fim
    ]
    texto_original = " ".join(palavras_contexto)
    palavra_alvo_literal = segmentos[idx]["text"]
    return inicio, fim, texto_original.replace(palavra_alvo_literal, ". {nome}.", 1)

def _ffmpeg_obter_duracao(input_video: str) -> float:
    """
    Obtém a duração total do vídeo em segundos usando ffprobe.
    """
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", input_video
        ], capture_output=True, text=True, check=True)
        duracao = float(result.stdout.strip())
        return duracao
    except (subprocess.CalledProcessError, ValueError) as e:
        print(f"[FFMPEG] Erro ao obter duração do vídeo: {e}")
        # Fallback: retorna uma duração padrão ou usa outro método
        raise RuntimeError(f"Não foi possível obter a duração do vídeo: {e}")

def _ffmpeg_obter_propriedades(input_video: str) -> Dict[str, Any]:
    """
    Obtém propriedades do vídeo (largura, altura, fps) usando ffprobe.
    """
    try:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "json", input_video
        ], capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        width = stream.get("width", 1920)
        height = stream.get("height", 1080)
        r_frame_rate = stream.get("r_frame_rate", "30/1")
        # Calcula fps
        if "/" in r_frame_rate:
            num, den = map(int, r_frame_rate.split("/"))
            fps = num / den if den > 0 else 30.0
        else:
            fps = float(r_frame_rate) if r_frame_rate else 30.0
        return {"width": width, "height": height, "fps": fps}
    except Exception as e:
        print(f"[FFMPEG] Erro ao obter propriedades do vídeo: {e}, usando padrão 1920x1080@30fps")
        return {"width": 1920, "height": 1080, "fps": 30.0}

def _ffmpeg_extrair_frame_meio(input_video: str, pasta: str, qualidade: int = 2) -> str:
    """
    Extrai um frame de alta qualidade do meio do vídeo.
    Retorna o caminho do arquivo de imagem gerado.
    """
    duracao = _ffmpeg_obter_duracao(input_video)
    tempo_meio = duracao / 2.0
    
    out = os.path.join(pasta, "avatar_frame_meio.jpg")
    subprocess.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{tempo_meio:.3f}",
        "-i", input_video,
        "-frames:v", "1",
        "-q:v", str(qualidade),  # qualidade: 2 = alta qualidade (escala 1-31, menor = melhor)
        "-vf", "scale=1920:-2",  # escala para alta resolução mantendo aspect ratio
        out
    ], check=True)
    
    print(f"[FFMPEG] Frame do meio extraído em {tempo_meio:.2f}s (duração total: {duracao:.2f}s)")
    return out

def _ffmpeg_pegar_frames(input_video: str, start_s: float, end_s: float, num: int, pasta: str) -> List[str]:
    """
    Extrai N frames uniformemente no intervalo [start_s, end_s] em JPGs.
    """
    dur = max(0.01, end_s - start_s)
    caminhos: List[str] = []
    for i in range(num):
        t = start_s + (dur * (i + 0.5) / num)
        out = os.path.join(pasta, f"avatar_frame_{i+1:02d}.jpg")
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-ss", f"{t:.3f}","-i", input_video,"-frames:v","1","-q:v","2", out
        ], check=True)
        caminhos.append(out)
    return caminhos

def _unwrap_data(j: Any) -> Any:
    """Se a resposta vier como {'data': {...}}, devolve o conteúdo de data; caso contrário, devolve j."""
    try:
        if isinstance(j, dict) and "data" in j and j["data"] is not None:
            return j["data"]
    except Exception:
        pass
    return j

async def heygen_upload_photo(image_path: str) -> str:
    url = _heygen_url("photo-avatar/upload")
    headers = await _heygen_headers()
    try:
        with open(image_path, "rb") as f:
            files = {"image": (os.path.basename(image_path), f, "image/jpeg")}
            resp = await _heygen_request("POST", url, headers=headers, files=files)
        try:
            j = resp.json()
        except Exception:
            j = {}
        d = _unwrap_data(j)
        key = (d.get("image_key") if isinstance(d, dict) else None) or j.get("image_key") or j.get("key") or j.get("id") or resp.text.strip()
        if not key:
            raise RuntimeError(f"[Heygen] upload photo falhou: {j or resp.text[:200]!r}")
        print(f"[HEYGEN] upload OK -> image_key={key}")
        return key
    except Exception as e:
        _log_heygen_error(e, extra={"image_path": image_path})
        raise

async def heygen_create_group(name: str, image_key: str) -> str:
    url = _heygen_url("photo-avatar/group")
    headers = await _heygen_headers(include_json=True)
    body = {"name": name, "image_key": image_key}
    try:
        resp = await _heygen_request("POST", url, headers=headers, json=body)
        j = resp.json()
        d = _unwrap_data(j)
        gid = (d.get("group_id") if isinstance(d, dict) else None) or (d.get("id") if isinstance(d, dict) else None) or j.get("group_id") or j.get("id")
        reused = (d.get("reused") if isinstance(d, dict) else None) or j.get("reused", False)
        
        if not gid:
            raise RuntimeError(f"[Heygen] create group sem id: {j!r}")
        
        if reused:
            print(f"[HEYGEN] group reutilizado -> id={gid}, name={name}")
            # Se foi reutilizado, verifica se tem looks válidos
            avatars = await heygen_group_avatars(gid)
            has_valid = False
            for av in avatars or []:
                if (av.get("status") or "").lower() == "completed":
                    has_valid = True
                    break
            if not has_valid:
                print(f"[HEYGEN] Grupo reutilizado não tem looks válidos. Deletando e criando novo com nome único...")
                try:
                    await heygen_delete_group(gid)
                    # Aguarda um pouco para garantir que foi deletado
                    await asyncio.sleep(1.0)
                except Exception as e:
                    print(f"[HEYGEN] Erro ao deletar grupo reutilizado: {e}")
                
                # Cria com nome único (adiciona timestamp)
                unique_name = f"{name}_{int(time.time())}"
                body_unique = {"name": unique_name, "image_key": image_key}
                resp_unique = await _heygen_request("POST", url, headers=headers, json=body_unique)
                j_unique = resp_unique.json()
                d_unique = _unwrap_data(j_unique)
                gid = (d_unique.get("group_id") if isinstance(d_unique, dict) else None) or (d_unique.get("id") if isinstance(d_unique, dict) else None) or j_unique.get("group_id") or j_unique.get("id")
                if not gid:
                    raise RuntimeError(f"[Heygen] create group (único) sem id: {j_unique!r}")
                print(f"[HEYGEN] group OK (novo) -> id={gid}, name={unique_name}")
        else:
            print(f"[HEYGEN] group OK -> id={gid}, name={name}")
        return gid
    except Exception as e:
        _log_heygen_error(e, extra={"body": body})
        raise

async def heygen_group_add(group_id: str, image_keys: List[str]) -> None:
    url = _heygen_url(f"photo-avatar/group/{group_id}/add")
    headers = await _heygen_headers(include_json=True)
    body = {"image_keys": image_keys}
    try:
        resp = await _heygen_request("POST", url, headers=headers, json=body)
        _ = resp.json() if resp.headers.get("content-type","").startswith("application/json") else None
        print(f"[HEYGEN] group add OK -> group_id={group_id}, added={len(image_keys)}")
    except Exception as e:
        _log_heygen_error(e, extra={"group_id": group_id, "body": body})
        raise

async def heygen_group_train(group_id: str, max_retries: int = 10, retry_delay: float = 3.0) -> Dict[str, Any]:
    """
    Inicia o treinamento do grupo de forma assíncrona (waitForCompleted=false).
    Retorna a resposta completa (pode ser job_id, status, etc) para guardar no Redis.
    Se der erro 409 (fotos não processadas), tenta novamente após aguardar.
    Continua tentando até conseguir iniciar o treino ou esgotar todas as tentativas.
    Levanta exceção apenas se não conseguir iniciar após todas as tentativas.
    """
    url = _heygen_url(f"photo-avatar/group/{group_id}/train?waitForCompleted=true")
    headers = await _heygen_headers()
    
    for attempt in range(max_retries):
        try:
            resp = await _heygen_request("POST", url, headers=headers)
            try:
                j = resp.json()
            except Exception:
                j = {}
            
            d = _unwrap_data(j)
            # Retorna a resposta completa (pode ser dict ou string)
            result = d if isinstance(d, dict) else (j if isinstance(j, dict) else {"raw": resp.text})
            
            print(f"[HEYGEN] train iniciado -> group_id={group_id}, resposta={result} (tentativa {attempt + 1}/{max_retries})")
            return result
        except httpx.HTTPStatusError as e:
            # Erro 409: fotos ainda não processadas
            if e.response.status_code == 409:
                if attempt < max_retries - 1:
                    print(f"[HEYGEN] Fotos ainda não processadas (tentativa {attempt + 1}/{max_retries}). Aguardando {retry_delay}s...")
                    await asyncio.sleep(retry_delay)
                    continue
                else:
                    error_body = {}
                    try:
                        error_body = e.response.json()
                    except:
                        error_body = {"error": str(e)}
                    error_msg = error_body.get("error", "Erro desconhecido")
                    print(f"[HEYGEN] ERROR: Não foi possível iniciar treino após {max_retries} tentativas: {error_msg}")
                    raise RuntimeError(f"Não foi possível iniciar treino após {max_retries} tentativas: {error_msg}")
            # Outros erros HTTP são propagados
            _log_heygen_error(e, extra={"group_id": group_id})
            raise
        except Exception as e:
            # Se não for HTTPStatusError, verifica se é o último attempt
            if attempt < max_retries - 1:
                print(f"[HEYGEN] Erro ao iniciar treino (tentativa {attempt + 1}/{max_retries}): {e}. Aguardando {retry_delay}s...")
                await asyncio.sleep(retry_delay)
                continue
            _log_heygen_error(e, extra={"group_id": group_id})
            raise
    
    # Se chegou aqui, esgotou todas as tentativas
    raise RuntimeError(f"Não foi possível iniciar treino após {max_retries} tentativas")

async def heygen_group_avatars(group_id: str) -> List[dict]:
    url = _heygen_url(f"photo-avatar/group/{group_id}/avatars")
    headers = await _heygen_headers()
    try:
        resp = await _heygen_request("GET", url, headers=headers)
        raw = resp.json() if resp.headers.get("content-type","").startswith("application/json") else []
        items = _unwrap_data(raw)
        if not isinstance(items, list) and isinstance(items, dict) and "items" in items:
            items = items["items"]
        print(f"[HEYGEN] avatars OK -> group_id={group_id}, total={len(items) if isinstance(items, list) else 'n/a'}")
        return items if isinstance(items, list) else []
    except Exception as e:
        _log_heygen_error(e, extra={"group_id": group_id})
        raise

async def heygen_verificar_status_treino(group_id: str) -> bool:
    """
    Verifica se o treino do grupo está completo consultando os avatares.
    Retorna True se algum avatar tem status "completed", False caso contrário.
    """
    try:
        avatars = await heygen_group_avatars(group_id)
        for av in avatars or []:
            status = (av.get("status") or "").lower()
            if status == "completed":
                print(f"[HEYGEN] Treino completo para group_id={group_id}")
                return True
        print(f"[HEYGEN] Treino ainda não completo para group_id={group_id}")
        return False
    except Exception as e:
        _log_heygen_error(e, extra={"group_id": group_id})
        return False

async def heygen_find_group_by_name(name: str) -> Optional[str]:
    """
    Melhor esforço: se o backend expuser lista de grupos (/photo-avatar/groups) filtramos por nome.
    """
    try:
        url = _heygen_url("photo-avatar/groups")
        headers = await _heygen_headers()
        resp = await _heygen_request("GET", url, headers=headers)
        raw = resp.json() if resp.headers.get("content-type","").startswith("application/json") else []
        items = _unwrap_data(raw)
        if not isinstance(items, list) and isinstance(items, dict) and "items" in items:
            items = items["items"]
        for g in items or []:
            if (g.get("name") or "").strip() == name:
                gid = g.get("group_id") or g.get("id")
                print(f"[HEYGEN] group found by name -> {name} = {gid}")
                return gid
    except Exception as e:
        _log_heygen_error(e, extra={"name": name})
    return None

async def heygen_delete_group(group_id: str) -> None:
    """
    Deleta um grupo de avatares da Heygen.
    """
    url = _heygen_url(f"photo-avatar/group/{group_id}")
    headers = await _heygen_headers()
    try:
        resp = await _heygen_request("DELETE", url, headers=headers)
        print(f"[HEYGEN] group deleted -> group_id={group_id}")
    except Exception as e:
        _log_heygen_error(e, extra={"group_id": group_id})
        raise

async def heygen_verificar_ou_criar_avatar_do_usuario(
    user_group_name: str,
    source_video: Optional[str] = None,
    segmentos: Optional[List[dict]] = None,
    palavra_chave: Optional[str] = None,
    pasta_temp: Optional[str] = None,
    num_fotos: int = 10,
    source_image: Optional[str] = None,
    existing_group_id: Optional[str] = None,
    user_id: Optional[UUID] = None,
    save_group_id_async: Optional[Callable[[UUID, str], Any]] = None,
) -> str:
    """
    Retorna group_id do avatar do usuário.
    Se o grupo existir e tiver avatares válidos (status="completed"), retorna o group_id.
    Se não existir ou não tiver avatares válidos, cria um novo grupo e retorna o group_id.
    """
    group_id = existing_group_id or await heygen_find_group_by_name(user_group_name)

    # Se grupo existe, verificar se tem avatares válidos
    if group_id:
        avatars = await heygen_group_avatars(group_id)
        has_valid_avatar = False
        
        for av in avatars or []:
            status = (av.get("status") or "").lower()
            if status == "completed":
                has_valid_avatar = True
                print(f"[HEYGEN] Grupo {group_id} tem avatar válido (status=completed)")
                break
        
        # Se encontrou avatar válido, retorna o group_id
        if has_valid_avatar:
            return group_id
        
        # Se não tem avatar válido, deleta o grupo antigo para criar um novo
        print(f"[HEYGEN] Grupo {group_id} existe mas não tem avatares válidos. Deletando e criando novo...")
        try:
            await heygen_delete_group(group_id)
        except Exception as e:
            print(f"[HEYGEN] Erro ao deletar grupo antigo (pode não existir): {e}")
        group_id = None

    # Se não existe grupo ou foi deletado, criar novo
    if not group_id:
        if source_image:
            image_path = source_image
        elif source_video and pasta_temp:
            image_path = _ffmpeg_extrair_frame_meio(source_video, pasta_temp)
        else:
            raise ValueError("É necessário fornecer source_image ou source_video para criar o avatar.")

        # Upload da imagem + cria grupo
        first_key = await heygen_upload_photo(image_path)
        group_id = await heygen_create_group(user_group_name, first_key)

        print(f"[HEYGEN] Grupo criado: {group_id}")
        if user_id and save_group_id_async:
            try:
                await save_group_id_async(user_id, group_id)
            except Exception as e:
                print(f"[HEYGEN] WARN: falha ao persistir group_id para usuário {user_id}: {e}")
        # Aguarda um pouco para a foto ser processada pela Heygen antes de tentar treinar
        print(f"[HEYGEN] Aguardando processamento da foto (5s)...")
        await asyncio.sleep(5.0)
        return group_id

    # Fallback: retorna o group_id existente
    return group_id

async def heygen_criar_video(group_id: str, voice_id: str, script: str, test: bool = True) -> str:
    """
    Cria job de vídeo e retorna jobId.
    Payload: {"talking_photo_id": "...", "voice_id": "...", "script": "..."}
    O campo talking_photo_id recebe o group_id (os IDs são os mesmos).
    
    Resposta esperada:
    {
      "message": "Job criado",
      "jobId": "ab3000cd-4202-4f0b-93fc-4443645f7c0c",
      "heygenVideoId": "faa5d078ea34401194049b9fd26a9a06"
    }
    
    Retorna o jobId para usar em GET /videos/{jobId}
    """
    url = _heygen_url("videos")
    headers = await _heygen_headers(include_json=True)
    
    # Payload em snake_case: talking_photo_id recebe o group_id
    payload = {
        "talking_photo_id": str(group_id).strip(),
        "voice_id": str(voice_id).strip(),
        "script": str(script).strip(),
        "test": False
    }
    try:
        resp = await _heygen_request("POST", url, headers=headers, json=payload)
        j = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
        d = _unwrap_data(j)
        # Extrai jobId da resposta (pode estar em data ou diretamente)
        job_id = (d.get("jobId") if isinstance(d, dict) else None) or j.get("jobId") or j.get("id")
        if not job_id:
            raise RuntimeError(f"[Heygen] POST /videos sem jobId: {j!r}")
        print(f"[HEYGEN] video job OK -> jobId={job_id}")
        return job_id
    except Exception as e:
        _log_heygen_error(e, extra={"payload": payload})
        raise

async def heygen_aguardar_video(job_id: str, sleep: float = 2.0) -> str:
    """
    Faz polling em GET /videos/{jobId} até COMPLETED e retorna a URL para download (video_url).
    Usa o jobId retornado por heygen_criar_video.
    """
    url = _heygen_url(f"videos/{job_id}")
    headers = await _heygen_headers()
    while True:
        try:
            resp = await _heygen_request("GET", url, headers=headers)
            j = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
            d = _unwrap_data(j)
            st = ((d.get("status") if isinstance(d, dict) else None) or j.get("status") or "").upper()
            print(f"[HEYGEN] poll job={job_id} status={st}")
            video_url = (d.get("video_url") if isinstance(d, dict) else None) or j.get("video_url")
            if st == "COMPLETED" and video_url:
                print(f"[HEYGEN] video pronto -> {video_url}")
                return video_url
            if st in ("FAILED","ERROR"):
                raise RuntimeError(f"[Heygen] job {job_id} falhou: {j!r}")
            time.sleep(sleep)
        except Exception as e:
            _log_heygen_error(e, extra={"job_id": job_id})
            raise

def overlay_clip_on_interval(
    input_video: str,
    insert_clip: str,
    start_s: float,
    end_s: float,
    out_path: str,
    overlay_x: str = "(W-w)/2",  # centro
    overlay_y: str = "H-h",      # rodapé
    scale_w: Optional[int] = 720,
    fade_ms: int = 0,  # Sem fade - transição instantânea
) -> str:
    """
    Insere um clip sobreposto no intervalo especificado do vídeo.
    Transição instantânea para parecer um vídeo único, não editado.
    Mantém o formato original do vídeo usando -c copy quando possível.
    """
    dur = max(0.01, end_s - start_s)
    
    with tempfile.TemporaryDirectory() as td:
        duracao_total = _ffmpeg_obter_duracao(input_video)
        
        before = os.path.join(td, "before.mp4")
        middle = os.path.join(td, "middle.mp4")
        after = os.path.join(td, "after.mp4")
        scaled = os.path.join(td, "insert_scaled.mp4")
        middle_overlay = os.path.join(td, "middle_overlay.mp4")
        
        # before - extrai até start_s
        if start_s > 0.01:
            subprocess.run([
                "ffmpeg","-y","-hide_banner","-loglevel","error",
                "-ss","0","-to", f"{start_s:.3f}","-i", input_video,
                "-c","copy",
                before
            ], check=True)
        else:
            before = None
        
        # middle - extrai o trecho onde será inserido o overlay
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-ss", f"{start_s:.3f}","-to", f"{end_s:.3f}","-i", input_video,
            "-c:v","libx264","-preset","medium","-crf","23",
            "-an",  # Remove áudio do vídeo original
            middle
        ], check=True)
        
        # after - extrai do end_s até o final
        if end_s < duracao_total - 0.01:
            subprocess.run([
                "ffmpeg","-y","-hide_banner","-loglevel","error",
                "-ss", f"{end_s:.3f}","-i", input_video,
                "-c","copy",
                after
            ], check=True)
        else:
            after = None
        
        # Obtém duração do middle para garantir que o scaled tenha a mesma duração
        dur_middle = _ffmpeg_obter_duracao(middle)
        
        # scale no clip inserido e ajusta duração para corresponder exatamente ao middle
        vf = []
        if scale_w:
            vf.append(f"scale={scale_w}:-2")
        vf_arg = ",".join(vf) if vf else "null"
        
        # Processa o insert_clip para ter exatamente a mesma duração do middle
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-stream_loop", "-1", "-i", insert_clip,  # Loop infinito para garantir que não acabe
            "-vf", vf_arg,
            "-c:v","libx264","-preset","medium","-crf","23",
            "-c:a","aac","-b:a","192k",
            "-t", f"{dur_middle:.3f}",  # Corta para duração exata do middle
            scaled
        ], check=True)
        
        # overlay - combina o vídeo original do trecho com o clip inserido
        # Ambos têm exatamente a mesma duração agora
        filter_complex = f"[0:v][1:v]overlay=x={overlay_x}:y={overlay_y}[outv]"
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", middle,"-i", scaled,
            "-filter_complex", filter_complex,
            "-map","[outv]","-map","1:a:0",
            "-c:v","libx264","-preset","medium","-crf","23",
            "-c:a","aac","-b:a","192k",
            "-vsync","cfr",  # Garante frame rate constante
            "-shortest",  # Para quando o mais curto terminar (ambos têm mesma duração)
            middle_overlay
        ], check=True)
        
        # Concatena - sempre re-encoda para garantir compatibilidade e evitar erros de NAL units
        concat_list = os.path.join(td, "list.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            if before:
                before_abs = os.path.abspath(before).replace("\\", "/")
                f.write(f"file '{before_abs}'\n")
            middle_abs = os.path.abspath(middle_overlay).replace("\\", "/")
            f.write(f"file '{middle_abs}'\n")
            if after:
                after_abs = os.path.abspath(after).replace("\\", "/")
                f.write(f"file '{after_abs}'\n")
        
        # Re-encoda tudo na concatenação final para garantir compatibilidade
        # Isso evita erros de NAL units e mantém formato consistente
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-f","concat","-safe","0","-i", concat_list,
            "-c:v","libx264","-preset","medium","-crf","23",
            "-c:a","aac","-b:a","192k",
            "-avoid_negative_ts","make_zero",
            out_path
        ], check=True)
    return out_path

# =====================
# Geração final (Heygen primeiro; TTS fallback)
# =====================

async def gerar_video_para_nome(
    nome: str,
    palavra_chave: str,
    transcricao: str,
    segmentos: List[dict],
    user_voice_id: str,
    caminho_audio: str,
    caminho_foto: str,
    pasta_temp: str,
    user_id: UUID,
    group_id: Optional[str] = None,
    enviar_webhook: bool = True,
):
    """
    Novo fluxo: gera o vídeo completo diretamente na Heygen a partir de uma foto estática.
    Se algo falhar, cai no fallback que monta um vídeo simples (foto + TTS).
    """
    try:
        # 1) texto com nome (mantém mesma lógica de captura da palavra-chave)
        _, _, texto_modelo = _extrair_intervalo_por_palavra(segmentos, palavra_chave)
        novo_texto = texto_modelo.format(nome=nome)

        # 2) já devemos ter group_id (criado/checado no processar_video), mas mantemos opção de receber None
        if not group_id:
            if not os.path.isfile(caminho_foto):
                raise RuntimeError("Foto base não encontrada para criar avatar na Heygen.")
            first_key = await heygen_upload_photo(caminho_foto)
            gname = f"user_{user_id}"
            group_id = await heygen_create_group(gname, first_key)

        # 3) Busca o voice_id da Heygen pelo nome (após importação)
        voice_name_heygen = f"user_{user_id}"
        heygen_voice_id = await heygen_buscar_voz_por_nome(voice_name_heygen)
        if not heygen_voice_id:
            # Se não encontrou, tenta importar e buscar novamente
            print(f"[HEYGEN] Voz '{voice_name_heygen}' não encontrada. Tentando importar...")
            await importar_voz_para_heygen(voice_name_heygen)
            # Aguarda 3 segundos após importação para processamento antes de buscar
            print(f"[HEYGEN] Aguardando 3 segundos após importação para processamento...")
            await asyncio.sleep(3.0)
            heygen_voice_id = await heygen_buscar_voz_por_nome(voice_name_heygen)
            if not heygen_voice_id:
                raise RuntimeError(f"Voz '{voice_name_heygen}' não encontrada na Heygen após tentativa de importação.")

        # 4) cria job do vídeo com voice_id DA HEYGEN e group_id como avatarId
        job_id = await heygen_criar_video(group_id, heygen_voice_id, novo_texto, test=True)
        video_url = await heygen_aguardar_video(job_id)

        # 5) baixa o clip
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            r = await client.get(video_url)
            r.raise_for_status()
            insert_clip = os.path.join(pasta_temp, f"heygen_{nome}.mp4")
            with open(insert_clip, "wb") as f:
                f.write(r.content)

        # 6) Usa diretamente o vídeo retornado pela Heygen
        caminho_saida_video = os.path.join(pasta_temp, f"video_{nome}.mp4")
        os.replace(insert_clip, caminho_saida_video)

        if enviar_webhook:
            await enviar_video_para_webhook(caminho_saida_video, nome, user_id)

        return caminho_saida_video

    except Exception as e:
        print(f"[HEYGEN FALLBACK] {e} — usando TTS antigo…")
        # Fallback antigo (áudio)
        return await gerar_video_para_nome_tts(
            nome=nome,
            palavra_chave=palavra_chave,
            transcricao=transcricao,
            segmentos=segmentos,
            user_voice_id=user_voice_id,
            caminho_audio=caminho_audio,
            caminho_foto=caminho_foto,
            pasta_temp=pasta_temp,
            user_id=user_id,
            enviar_webhook=enviar_webhook
        )

# ===== Fallback TTS antigo =====
async def gerar_video_para_nome_tts(
    nome: str,
    palavra_chave: str,
    transcricao: str,
    segmentos: List[dict],
    user_voice_id: str,
    caminho_audio: str,
    caminho_foto: str,
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
        "-loop", "1", "-i", caminho_foto, "-i", caminho_audio_final,
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        caminho_saida_video
    ], check=True)

    if enviar_webhook:
        await enviar_video_para_webhook(caminho_saida_video, nome, user_id)

    return caminho_saida_video

# =====================
# Webhook
# =====================

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
