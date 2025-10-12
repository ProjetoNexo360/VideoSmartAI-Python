# services/audio_service.py
import os
import tempfile
import httpx
import subprocess
from typing import List, Dict, Any, Tuple, Optional
from uuid import UUID
import json
import unicodedata
import base64
import time
import re
from urllib.parse import quote
from difflib import get_close_matches
from typing import Callable, Awaitable


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

# Logs
HEYGEN_DEBUG  = os.getenv("HEYGEN_DEBUG", "1").strip() not in ("0", "false", "False", "")
ELEVEN_DEBUG  = os.getenv("ELEVEN_DEBUG", "1").strip() not in ("0", "false", "False", "")

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

# ===== Logs helpers (masking) =====

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

# ===== Logs Eleven =====

def _log_eleven_request(method: str, url: str, headers: dict | None, json_body: Any = None, data: Any = None, files: Any = None):
    if not ELEVEN_DEBUG:
        return
    print("\n[ELEVEN][REQUEST]")
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

def _log_eleven_response(resp: httpx.Response):
    if not ELEVEN_DEBUG:
        return
    print("[ELEVEN][RESPONSE]")
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

def _log_eleven_error(e: Exception, extra: dict | None = None):
    if not ELEVEN_DEBUG:
        return
    print("[ELEVEN][ERROR]", repr(e))
    if isinstance(e, httpx.HTTPStatusError) and e.response is not None:
        try:
            print("  status:", e.response.status_code)
            _log_eleven_response(e.response)
        except Exception:
            pass
    if extra:
        print("  extra:", _safe_json_dump(extra))

# ===== Logs Heygen =====

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
    try:
        _log_eleven_request(
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
                await _eleven_login(force=True)
                headers = kwargs.get("headers", {}) or {}
                headers["Authorization"] = f"Bearer {_eleven_token}"
                kwargs["headers"] = headers
                _log_eleven_request(
                    method=method, url=url,
                    headers=kwargs.get("headers"),
                    json_body=kwargs.get("json"), data=kwargs.get("data"), files=kwargs.get("files"),
                )
                resp = await client.request(method, url, **kwargs)

            _log_eleven_response(resp)
            resp.raise_for_status()
            return resp
        except Exception as e:
            _log_eleven_error(e, extra={"url": url, "method": method})
            raise

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

    no_ns_prefixes = ("photo-avatar", "videos", "auth")
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
    video_bytes: bytes,
    nome_video: str,
    evo_instance: str | None = None,
    evo_base: str | None = None,
    heygen_group_id: Optional[str] = None,
    save_group_id_async: Optional[Callable[[UUID, str], Awaitable[None]]] = None,
):
    voz_padrao_nome = f"user_{user_id}"
    avatar_group_name = f"{voz_padrao_nome}"

    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_video = salvar_video_em_disco(video_bytes, nome_video, pasta_temp)
        caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)

        if not transcricao:
            raise ValueError("Transcrição retornou vazia. Verifique o áudio original.")

        user_voice_id = await verificar_ou_criar_voz(voz_padrao_nome, caminho_audio, pasta_temp)

        # obtém/garante o group_id (compat com o nome que o main importa)
        group_id = await heygen_verificar_ou_criar_avatar_do_usuario(
            user_group_name=avatar_group_name,
            source_video=caminho_video,
            segmentos=segmentos,
            palavra_chave=palavra_chave,
            pasta_temp=pasta_temp,
            num_fotos=10,
            user_id=user_id,
            existing_group_id=heygen_group_id,
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
                    user_voice_id=user_voice_id,
                    caminho_video=caminho_video,
                    caminho_audio=caminho_audio,
                    pasta_temp=pasta_temp,
                    user_id=user_id,
                    group_id=group_id,   # usamos group_id
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
        return vid

# =====================
# HEYGEN – helpers de avatar/grupo
# =====================

def _norm_token(s: str) -> str:
    import unicodedata as _ud, re as _re
    s = _ud.normalize("NFKD", s or "")
    s = "".join(c for c in s if not _ud.combining(c))
    return _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

def _extrair_intervalo_por_palavra(segmentos: List[dict], palavra_chave: str) -> Tuple[float, float, str]:
    if not segmentos:
        raise ValueError("Lista de segmentos vazia.")
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
    inicio = max(0.0, float(segmentos[max(0, idx - PALAVRAS_ANTES)]["start"]) - (AJUSTE_MS / 1000))
    fim = float(segmentos[min(len(segmentos)-1, idx + PALAVRAS_DEPOIS)]["end"]) + (AJUSTE_MS / 1000)

    palavras_contexto = [
        (w.get("text") or "") for w in segmentos
        if str(w.get("type")) == "word" and inicio <= float(w.get("start",0)) and float(w.get("end",0)) <= fim
    ]
    texto_original = " ".join(palavras_contexto)
    palavra_alvo_literal = segmentos[idx]["text"]
    return inicio, fim, texto_original.replace(palavra_alvo_literal, ". {nome}.", 1)

def _ffmpeg_pegar_frames(input_video: str, start_s: float, end_s: float, num: int, pasta: str) -> List[str]:
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
    try:
        if isinstance(j, dict) and "data" in j and j["data"] is not None:
            return j["data"]
    except Exception:
        pass
    return j

async def heygen_upload_photo(image_path: str) -> str:
    url = _heygen_url("photo-avatar/upload")
    headers = await _heygen_headers()
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

async def heygen_create_group(name: str, image_key: str) -> str:
    url = _heygen_url("photo-avatar/group")
    headers = await _heygen_headers(include_json=True)
    body = {"name": name, "image_key": image_key}
    resp = await _heygen_request("POST", url, headers=headers, json=body)
    j = resp.json()
    d = _unwrap_data(j)
    gid = (d.get("group_id") if isinstance(d, dict) else None) or (d.get("id") if isinstance(d, dict) else None) or j.get("group_id") or j.get("id")
    if not gid:
        raise RuntimeError(f"[Heygen] create group sem id: {j!r}")
    print(f"[HEYGEN][GROUP] criado -> id={gid}, name={name}")
    return gid

async def heygen_group_add(group_id: str, image_keys: List[str]) -> None:
    url = _heygen_url(f"photo-avatar/group/{group_id}/add")
    headers = await _heygen_headers(include_json=True)
    body = {"image_keys": image_keys}
    try:
        await _heygen_request("POST", url, headers=headers, json=body)
        print(f"[HEYGEN] group add OK -> group_id={group_id}, added={len(image_keys)}")
    except Exception as e:
        _log_heygen_error(e, extra={"group_id": group_id, "body": body})
        print(f"[HEYGEN] group add FALHOU -> seguindo fluxo")

# --- Garante/obtém somente o GROUP_ID (sem talking_photo) ---
async def heygen_garantir_grupo_do_usuario(
    user_group_name: str,
    source_video: str,
    segmentos: List[dict],
    palavra_chave: str,
    pasta_temp: str,
    num_fotos: int = 10,
    user_id: Optional[UUID] = None,
    existing_group_id: Optional[str] = None,
    save_group_id_async: Optional[Callable[[UUID, str], Awaitable[None]]] = None,
) -> str:
    if existing_group_id:
        return existing_group_id

    inicio, fim, _ = _extrair_intervalo_por_palavra(segmentos, palavra_chave)
    frames = _ffmpeg_pegar_frames(source_video, max(0.0, inicio - 0.8), fim + 0.8, num_fotos, pasta_temp)
    frames = frames[:3]

    if not frames:
        raise RuntimeError("Não foi possível extrair frames para o avatar.")

    first_key = await heygen_upload_photo(frames[0])
    group_id = await heygen_create_group(user_group_name, first_key)

    if user_id is not None and save_group_id_async is not None:
        try:
            await save_group_id_async(user_id, group_id)
        except Exception as e:
            print(f"[HEYGEN] WARN persist group_id: {e}")

    if len(frames) > 1:
        extras = []
        for p in frames[1:]:
            try:
                extras.append(await heygen_upload_photo(p))
            except Exception as e:
                print(f"[Heygen] upload extra falhou: {e}")
        if extras:
            await heygen_group_add(group_id, extras)

    return group_id

# --- COMPATIBILIDADE: mantém o nome que o main.py importa ---
async def heygen_verificar_ou_criar_avatar_do_usuario(
    user_group_name: str,
    source_video: str,
    segmentos: List[dict],
    palavra_chave: str,
    pasta_temp: str,
    num_fotos: int = 10,
    user_id: Optional[UUID] = None,
    existing_group_id: Optional[str] = None,
    save_group_id_async: Optional[Callable[[UUID, str], Awaitable[None]]] = None,
) -> str:
    """
    **Compat:** mantém o nome antigo, mas retorna **group_id** (não talking_photo_id).
    Não usa custom-avatar; apenas garante/retorna o ID do grupo.
    """
    return await heygen_garantir_grupo_do_usuario(
        user_group_name=user_group_name,
        source_video=source_video,
        segmentos=segmentos,
        palavra_chave=palavra_chave,
        pasta_temp=pasta_temp,
        num_fotos=num_fotos,
        user_id=user_id,
        existing_group_id=existing_group_id,
        save_group_id_async=save_group_id_async,
    )

# =====================
# Render de vídeo Heygen (/videos com group_id em talking_photo_id)
# =====================

async def heygen_criar_video(talking_photo_id: str, voice_id: str, script: str, test: bool = True) -> str:
    """
    Cria job de vídeo e retorna jobId.
    Aqui, 'talking_photo_id' recebe o **group_id**.
    """
    url = _heygen_url("videos")
    headers = await _heygen_headers(include_json=True)
    payload = {"talking_photo_id": talking_photo_id, "voice_id": voice_id, "script": script, "test": bool(test)}
    resp = await _heygen_request("POST", url, headers=headers, json=payload)
    j = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
    d = _unwrap_data(j)
    job_id = (d.get("jobId") if isinstance(d, dict) else None) or j.get("jobId") or j.get("id")
    if not job_id:
        raise RuntimeError(f"[Heygen] POST /videos sem jobId: {j!r}")
    print(f"[HEYGEN] video job OK -> jobId={job_id}")
    return job_id

async def heygen_aguardar_video(job_id: str, sleep: float = 2.0) -> str:
    url = _heygen_url(f"videos/{job_id}")
    headers = await _heygen_headers()
    while True:
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

def overlay_clip_on_interval(
    input_video: str,
    insert_clip: str,
    start_s: float,
    end_s: float,
    out_path: str,
    overlay_x: str = "(W-w)/2",
    overlay_y: str = "H-h",
    scale_w: Optional[int] = 720,
    fade_ms: int = 120,
) -> str:
    dur = max(0.01, end_s - start_s)
    with tempfile.TemporaryDirectory() as td:
        before = os.path.join(td, "before.mp4")
        middle = os.path.join(td, "middle.mp4")
        after = os.path.join(td, "after.mp4")
        scaled = os.path.join(td, "insert_scaled.mp4")
        middle_overlay = os.path.join(td, "middle_overlay.mp4")
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-ss","0","-to", f"{start_s:.3f}","-i", input_video,"-c","copy", before
        ], check=True)
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-ss", f"{start_s:.3f}","-to", f"{end_s:.3f}","-i", input_video,
            "-c:v","libx264","-an", middle
        ], check=True)
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-ss", f"{end_s:.3f}","-i", input_video,"-c","copy", after
        ], check=True)
        vf = []
        if scale_w:
            vf.append(f"scale={scale_w}:-2")
        if fade_ms > 0:
            vf.append(f"fade=t=in:st=0:d={fade_ms/1000:.3f},fade=t=out:st={max(0.0,dur - fade_ms/1000):.3f}:d={fade_ms/1000:.3f}")
        vf_arg = ",".join(vf) if vf else "null"
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", insert_clip,"-vf", vf_arg,"-c:v","libx264","-c:a","aac","-shortest", scaled
        ], check=True)
        filter_complex = f"[0:v][1:v]overlay=x={overlay_x}:y={overlay_y}:eof_action=pass[outv]"
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-i", middle,"-i", scaled,
            "-filter_complex", filter_complex,
            "-map","[outv]","-map","1:a:0",
            "-c:v","libx264","-c:a","aac","-shortest", middle_overlay
        ], check=True)
        concat_list = os.path.join(td, "list.txt")
        with open(concat_list, "w", encoding="utf-8") as f:
            f.write(f"file '{before}'\n")
            f.write(f"file '{middle_overlay}'\n")
            f.write(f"file '{after}'\n")
        subprocess.run([
            "ffmpeg","-y","-hide_banner","-loglevel","error",
            "-f","concat","-safe","0","-i", concat_list,"-c","copy", out_path
        ], check=True)
    return out_path

# =====================
# Geração final — /videos com group_id
# =====================

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
    group_id: Optional[str] = None,
    talking_photo_id: Optional[str] = None,  # <- compat: main.py pode enviar com este nome
    enviar_webhook: bool = True
):
    """
    Gera o clipe na Heygen usando /videos (sem custom-avatar).
    Observação: por compatibilidade, aceitamos tanto `group_id` quanto `talking_photo_id`
    (onde `talking_photo_id` também conterá o ID do grupo).
    """
    # 1) intervalo + texto com nome
    inicio, fim, texto_modelo = _extrair_intervalo_por_palavra(segmentos, palavra_chave)
    novo_texto = texto_modelo.format(nome=nome)

    # 2) compat: usamos o que vier
    actual_id = group_id or talking_photo_id
    if not actual_id:
        raise RuntimeError("group_id/talking_photo_id ausente; não foi possível determinar o grupo do avatar.")

    # 3) cria job do vídeo (campo talking_photo_id recebe o ID do GRUPO, conforme requisito)
    job_id = await heygen_criar_video(actual_id, user_voice_id, novo_texto, test=True)
    video_url = await heygen_aguardar_video(job_id)

    # 4) baixa o clip
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        r = await client.get(video_url)
        r.raise_for_status()
        insert_clip = os.path.join(pasta_temp, f"heygen_{nome}.mp4")
        with open(insert_clip, "wb") as f:
            f.write(r.content)

    # 5) overlay no intervalo
    caminho_saida_video = os.path.join(pasta_temp, f"video_{nome}.mp4")
    overlay_clip_on_interval(
        input_video=caminho_video,
        insert_clip=insert_clip,
        start_s=inicio,
        end_s=fim,
        out_path=caminho_saida_video,
        overlay_x="(W-w)/2",
        overlay_y="H-h-40",
        scale_w=720
    )

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
