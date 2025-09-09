# main.py
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Path, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from uuid import UUID
from io import BytesIO
import json
import tempfile
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
from models import User
from auth_utils import get_db, hash_password, verify_password, create_access_token, get_current_user

from services.audio_service import (
    processar_video, salvar_video_em_disco, extrair_audio_do_video,
    transcrever_audio_com_timestamps, verificar_ou_criar_voz, gerar_video_para_nome,
    enviar_video_para_webhook,
    enviar_texto_via_whatsapp, enviar_video_via_whatsapp,
    evo_start_session, evo_status, evo_logout
)
from redis_client import salvar_preview, obter_preview, remover_preview


app = FastAPI(
    title="VideoSmartAI",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)
Base.metadata.create_all(bind=engine)

@app.get("/")
def health():
    return {"status": "ok"}

# ---------------------------
# Validação simples de contatos
# ---------------------------
def parse_contatos(contatos_json: str):
    try:
        raw = json.loads(contatos_json)
        if not isinstance(raw, list) or not raw:
            raise ValueError("contatos deve ser uma lista não vazia.")
        contatos = []
        for i, c in enumerate(raw):
            if not isinstance(c, dict):
                raise ValueError(f"contato[{i}] inválido.")
            nome = (c.get("nome") or "").strip()
            telefone = (c.get("telefone") or "").strip()
            if not nome or not telefone:
                raise ValueError(f"contato[{i}] precisa de 'nome' e 'telefone'.")
            contatos.append({"nome": nome, "telefone": telefone})
        return contatos
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=422, detail=f"contatos inválido: {e}")

# ==========================================================
# AUTH
# ==========================================================
@app.post("/auth/register")
def register(email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    token = create_access_token(user.id, user.email)
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}

@app.post("/auth/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # OAuth2PasswordRequestForm usa "username" e "password"
    email = form_data.username.strip().lower()
    user = db.query(User).filter(User.email == email).first()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Credenciais inválidas.")
    token = create_access_token(user.id, user.email)
    return {"access_token": token, "token_type": "bearer", "user_id": user.id}

@app.get("/auth/me")
def me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "email": current_user.email,
        "evo_instance": current_user.evo_instance
    }

# ==========================================================
# EVOLUTION API (QR / Status / Logout) - por usuário
# ==========================================================
@app.post("/evo/start")
async def evo_start(instance: str = Form(...),
                    current_user: User = Depends(get_current_user),
                    db: Session = Depends(get_db)):
    # Dispara a criação da sessão e retorna QR
    data = await evo_start_session(instance)
    # Salva a instance no usuário
    current_user.evo_instance = instance
    db.add(current_user)
    db.commit()
    return {"instance": instance, "qr": data}

@app.get("/evo/status")
async def evo_get_status(current_user: User = Depends(get_current_user)):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Usuário ainda não vinculou uma instância (evo_instance).")
    data = await evo_status(current_user.evo_instance)
    return {"instance": current_user.evo_instance, "status": data}

@app.post("/evo/logout")
async def evo_do_logout(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Usuário não possui instância vinculada.")
    data = await evo_logout(current_user.evo_instance)
    # Opcional: limpar do usuário
    current_user.evo_instance = None
    db.add(current_user)
    db.commit()
    return {"message": "logout solicitado", "resp": data}

# ==========================================================
# POST /gerar-videos/{user_id}  -> processa TUDO em background (usa instância do usuário logado)
# ==========================================================
@app.post("/gerar-videos/{user_id}")
async def gerar_videos(
    user_id: UUID = Path(..., description="ID do usuário no formato UUID"),
    background_tasks: BackgroundTasks = None,
    contatos: str = Form(..., description='JSON: [{"nome":"...","telefone":"..."}, ...]'),
    palavra_chave: str = Form(...),
    video: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Vincule sua instância na Evolution API antes (POST /evo/start).")

    contatos_lista = parse_contatos(contatos)
    video_bytes = await video.read()
    nome_video = video.filename

    background_tasks.add_task(
        processar_video,
        user_id=user_id,
        contatos=contatos_lista,
        palavra_chave=palavra_chave,
        video_bytes=video_bytes,
        nome_video=nome_video,
        evo_instance=current_user.evo_instance,   # usa a instância do usuário
        evo_base=None
    )
    return JSONResponse(content={
        "message": "Processamento iniciado",
        "user_id": str(user_id),
        "qtd_contatos": len(contatos_lista),
        "palavra_chave": palavra_chave,
        "evo_instance": current_user.evo_instance
    }, status_code=202)

# ==========================================================
# POST /gerar-preview/{user_id}  -> gera SÓ o primeiro contato (download), salva estado no Redis
# ==========================================================
@app.post("/gerar-preview/{user_id}")
async def gerar_preview(
    user_id: UUID,
    contatos: str = Form(..., description='JSON: [{"nome":"...","telefone":"..."}, ...]'),
    palavra_chave: str = Form(...),
    video: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Vincule sua instância na Evolution API antes (POST /evo/start).")

    contatos_lista = parse_contatos(contatos)
    primeiro = contatos_lista[0]
    video_bytes = await video.read()
    nome_video = video.filename

    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_video = salvar_video_em_disco(video_bytes, nome_video, pasta_temp)
        caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)
        user_voice_id = await verificar_ou_criar_voz(f"user_{user_id}", caminho_audio, pasta_temp)

        caminho_saida_preview = await gerar_video_para_nome(
            nome=primeiro["nome"],
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

        with open(caminho_saida_preview, "rb") as f:
            conteudo_video = f.read()

        # Salva estado para confirmar depois
        await salvar_preview(user_id, {
            "contatos": contatos_lista,
            "palavra_chave": palavra_chave,
            "transcricao": transcricao,
            "segmentos": segmentos,
            "voice_id": user_voice_id,
            "video_bytes": video_bytes.decode("latin1"),
            "nome_video": nome_video,
            # guardo a instância do usuário no momento do preview
            "evo_instance": current_user.evo_instance
        })

    return StreamingResponse(BytesIO(conteudo_video), media_type="video/mp4", headers={
        "Content-Disposition": f"attachment; filename=preview_{primeiro['nome']}.mp4"
    })

# ==========================================================
# POST /confirmar-envio/{user_id}  -> gera para todos e ENVIA pelo WhatsApp do usuário
# ==========================================================
@app.post("/confirmar-envio/{user_id}")
async def confirmar_envio(user_id: UUID, background_tasks: BackgroundTasks, current_user: User = Depends(get_current_user)):
    dados = await obter_preview(user_id)
    if not dados:
        return JSONResponse(status_code=404, content={"error": "Nenhum preview encontrado ou expirado."})

    evo_instance = current_user.evo_instance or dados.get("evo_instance")
    if not evo_instance:
        raise HTTPException(status_code=400, detail="Usuário não possui instância Evolution vinculada.")

    async def gerar_restante():
        import tempfile
        with tempfile.TemporaryDirectory() as pasta_temp:
            caminho_video = salvar_video_em_disco(
                dados["video_bytes"].encode("latin1"),
                dados["nome_video"],
                pasta_temp
            )
            caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)

            contatos_todos = dados["contatos"]

            def _norm(s: str) -> str: return s.strip().casefold()
            uniq_map = {}
            for c in contatos_todos:
                k = (_norm(c["nome"]), _norm(c["telefone"]))
                if k not in uniq_map:
                    uniq_map[k] = c

            videos = {}
            for k, contato in uniq_map.items():
                try:
                    caminho = await gerar_video_para_nome(
                        nome=contato["nome"],
                        palavra_chave=dados["palavra_chave"],
                        transcricao=dados["transcricao"],
                        segmentos=dados["segmentos"],
                        user_voice_id=dados["voice_id"],
                        caminho_video=caminho_video,
                        caminho_audio=caminho_audio,
                        pasta_temp=pasta_temp,
                        user_id=user_id,
                        enviar_webhook=False
                    )
                    videos[k] = caminho
                except Exception as e:
                    print(f"[ERR] Gerar vídeo para {contato['nome']} ({contato['telefone']}): {e}")

            for c in contatos_todos:
                k = (_norm(c["nome"]), _norm(c["telefone"]))
                caminho = videos.get(k)
                if not caminho:
                    continue
                try:
                    await enviar_texto_via_whatsapp(
                        c["telefone"], f"Olá {c['nome']}! (confirmação automática) 👍",
                        evo_instance=evo_instance
                    )
                    await enviar_video_via_whatsapp(
                        caminho, c["telefone"], caption=f"{c['nome']}, seu vídeo personalizado.",
                        evo_instance=evo_instance
                    )
                except Exception as e:
                    print(f"[ERR] Envio WA para {c['nome']} ({c['telefone']}): {e}")

        await remover_preview(user_id)

    background_tasks.add_task(gerar_restante)
    return JSONResponse(content={"message": "Processamento e envios iniciados.", "evo_instance": evo_instance})
