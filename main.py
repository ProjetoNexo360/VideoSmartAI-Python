# main.py
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Path, HTTPException, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordRequestForm
from uuid import UUID
from io import BytesIO
from typing import Optional
import json
import tempfile
import os
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
from models import User
from auth_utils import get_db, hash_password, verify_password, create_access_token, get_current_user

from services.audio_service import (
    processar_video, salvar_video_em_disco,
    salvar_imagem_em_disco, salvar_audio_em_wav,
    transcrever_audio_com_timestamps, verificar_ou_criar_voz, gerar_video_para_nome,
    enviar_video_para_webhook,
    enviar_texto_via_whatsapp, enviar_video_via_whatsapp,
    evo_start_session, evo_status, evo_logout,
    evo_create_user_instance, make_instance_name,
    heygen_verificar_ou_criar_avatar_do_usuario, heygen_group_train, heygen_verificar_status_treino,
    overlay_clip_on_interval, _ffmpeg_obter_duracao, _ffmpeg_obter_propriedades
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

async def salvar_group_id_no_banco(user_id: UUID, group_id: str):
    """Callback assíncrono para persistir o heygen_group_id no usuário."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.heygen_group_id = group_id
            db.add(user)
            db.commit()
    finally:
        db.close()


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
async def register(
    nome: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    email = email.strip().lower()
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(status_code=409, detail="E-mail já cadastrado.")

    # cria o usuário (o id já é UUID no seu modelo)
    user = User(name=nome.strip(), email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)

    # cria e vincula a instância Evolution com padrão nomeUsuario_uuid
    try:
        # cria remotamente (se já existir, tudo bem — tratamos o 403 na service)
        await evo_create_user_instance(user.name, user.id)
        # define o nome localmente e persiste
        user.evo_instance = make_instance_name(user.name, user.id)
        db.add(user)
        db.commit()
        db.refresh(user)
    except Exception as e:
        # se falhar criar a instância, ainda devolvemos o token,
        # mas avisamos o cliente para tentar o /evo/start depois
        print(f"[WARN] Falha ao criar instância Evolution para user={user.id}: {e}")

    token = create_access_token(user.id, user.email)
    return {
        "access_token": token,
        "token_type": "bearer",
        "user_id": user.id,
        "name": user.name,
        "evo_instance": user.evo_instance
    }


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
        "name": current_user.name,
        "evo_instance": current_user.evo_instance,
        "heygen_group_id": current_user.heygen_group_id,  # <- exposto p/ cliente
    }

# ==========================================================
# EVOLUTION API (QR / Status / Logout) - por usuário
# ==========================================================
@app.post("/evo/start")
async def evo_start(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    if not current_user.evo_instance:
        # fallback: caso cadastro não tenha conseguido criar a instância
        # criamos aqui para garantir o vínculo
        await evo_create_user_instance(current_user.name, current_user.id)
        current_user.evo_instance = make_instance_name(current_user.name, current_user.id)
        db.add(current_user)
        db.commit()
        db.refresh(current_user)

    data = await evo_start_session(current_user.evo_instance)  # só CONNECT/QR
    return {"instance": current_user.evo_instance, "qr": data["qr"]}


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
    foto: UploadFile = File(...),
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Vincule sua instância na Evolution API antes (POST /evo/start).")

    contatos_lista = parse_contatos(contatos)
    foto_bytes = await foto.read()
    nome_foto = foto.filename or "foto_usuario.jpg"
    audio_bytes = await audio.read()
    nome_audio = audio.filename or "audio_usuario.wav"

    background_tasks.add_task(
        processar_video,
        user_id=user_id,
        contatos=contatos_lista,
        palavra_chave=palavra_chave,
        foto_bytes=foto_bytes,
        nome_foto=nome_foto,
        audio_bytes=audio_bytes,
        nome_audio=nome_audio,
        evo_instance=current_user.evo_instance,
        evo_base=None,
        heygen_group_id=current_user.heygen_group_id,        # <- reusa o group_id do user se existir
        save_group_id_async=salvar_group_id_no_banco,        # <- persiste se criar
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
    foto: UploadFile = File(...),
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    if not current_user.evo_instance:
        raise HTTPException(status_code=400, detail="Vincule sua instância na Evolution API antes (POST /evo/start).")

    contatos_lista = parse_contatos(contatos)
    primeiro = contatos_lista[0]
    foto_bytes = await foto.read()
    nome_foto = foto.filename or "foto_usuario.jpg"
    audio_bytes = await audio.read()
    nome_audio = audio.filename or "audio_usuario.wav"

    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_foto = salvar_imagem_em_disco(foto_bytes, nome_foto, pasta_temp)
        caminho_audio = salvar_audio_em_wav(audio_bytes, nome_audio, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)
        user_voice_id = await verificar_ou_criar_voz(f"user_{user_id}", caminho_audio, pasta_temp)

        # Cria/verifica grupo de avatar e inicia treino assíncrono
        avatar_group_name = f"user_{user_id}"
        group_id = await heygen_verificar_ou_criar_avatar_do_usuario(
            user_group_name=avatar_group_name,
            source_image=caminho_foto,
            segmentos=segmentos,
            palavra_chave=palavra_chave,
            pasta_temp=pasta_temp,
            num_fotos=10,
            existing_group_id=current_user.heygen_group_id,
            user_id=user_id,
            save_group_id_async=salvar_group_id_no_banco,
        )
        
        # Inicia treino assíncrono (waitForCompleted=false)
        # Usa muitas tentativas (10) com delay maior (3s) para garantir que o treino seja iniciado
        train_response = await heygen_group_train(group_id, max_retries=10, retry_delay=3.0)

        caminho_saida_preview = await gerar_video_para_nome(
            nome=primeiro["nome"],
            palavra_chave=palavra_chave,
            transcricao=transcricao,
            segmentos=segmentos,
            user_voice_id=user_voice_id,
            caminho_audio=caminho_audio,
            caminho_foto=caminho_foto,
            pasta_temp=pasta_temp,
            user_id=user_id,
            group_id=group_id,
            enviar_webhook=False,
        )

        with open(caminho_saida_preview, "rb") as f:
            conteudo_video = f.read()

        # Salva estado para confirmar depois (inclui group_id e train_response)
        await salvar_preview(user_id, {
            "contatos": contatos_lista,
            "palavra_chave": palavra_chave,
            "transcricao": transcricao,
            "segmentos": segmentos,
            "voice_id": user_voice_id,
            "foto_bytes": foto_bytes.decode("latin1"),
            "nome_foto": nome_foto,
            "audio_bytes": audio_bytes.decode("latin1"),
            "nome_audio": nome_audio,
            "group_id": group_id,
            "train_response": train_response,  # Resposta do train para verificar depois
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
        import asyncio
        
        # Polling do status do treino (retry infinito até completar)
        group_id = dados.get("group_id")
        if group_id:
            print(f"[HEYGEN] Verificando status do treino para group_id={group_id}...")
            treino_completo = False
            tentativa = 0
            while not treino_completo:
                treino_completo = await heygen_verificar_status_treino(group_id)
                if treino_completo:
                    print(f"[HEYGEN] Treino completo após {tentativa + 1} tentativa(s)")
                    break
                
                # Define intervalo baseado no número da tentativa
                if tentativa == 0:
                    wait_time = 3.0  # Primeiro retry: 3 segundos
                elif tentativa == 1:
                    wait_time = 5.0  # Segundo retry: 5 segundos
                elif tentativa == 2:
                    wait_time = 10.0  # Terceiro retry: 10 segundos
                else:
                    wait_time = 20.0  # Do quarto em diante: 20 segundos
                
                tentativa += 1
                print(f"[HEYGEN] Tentativa {tentativa}: treino ainda não completo, aguardando {wait_time}s...")
                await asyncio.sleep(wait_time)
        else:
            print(f"[HEYGEN] WARNING: group_id não encontrado nos dados do preview")
        
        with tempfile.TemporaryDirectory() as pasta_temp:
            caminho_foto = salvar_imagem_em_disco(
                dados["foto_bytes"].encode("latin1"),
                dados["nome_foto"],
                pasta_temp
            )
            caminho_audio = salvar_audio_em_wav(
                dados["audio_bytes"].encode("latin1"),
                dados["nome_audio"],
                pasta_temp
            )

            # Garante group_id válido
            if not group_id:
                group_id = await heygen_verificar_ou_criar_avatar_do_usuario(
                    user_group_name=f"user_{user_id}",
                    source_image=caminho_foto,
                    segmentos=dados["segmentos"],
                    palavra_chave=dados["palavra_chave"],
                    pasta_temp=pasta_temp,
                    num_fotos=10,
                    user_id=user_id,
                    existing_group_id=current_user.heygen_group_id,
                    save_group_id_async=salvar_group_id_no_banco,
                )

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
                        caminho_audio=caminho_audio,
                        caminho_foto=caminho_foto,
                        pasta_temp=pasta_temp,
                        user_id=user_id,
                        group_id=group_id,  # Passa group_id do preview
                        enviar_webhook=False,
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

# ==========================================================
# POST /teste-overlay - Endpoint de teste para overlay sem gastar créditos
# ==========================================================
@app.post("/teste-overlay")
async def teste_overlay(
    video_original: UploadFile = File(..., description="Vídeo original"),
    video_inserir: UploadFile = File(..., description="Vídeo para inserir como overlay"),
    start_s: float = Form(5.0, description="Tempo de início em segundos"),
    end_s: float = Form(10.0, description="Tempo de fim em segundos"),
    overlay_x: str = Form("(W-w)/2", description="Posição X do overlay (padrão: centro)"),
    overlay_y: str = Form("H-h-40", description="Posição Y do overlay (padrão: rodapé)"),
    scale_w: Optional[int] = Form(720, description="Largura do overlay (padrão: 720)")
):
    """
    Endpoint de teste para testar a funcionalidade de overlay sem gastar créditos.
    Recebe dois vídeos e aplica overlay no intervalo especificado.
    """
    try:
        # Lê os vídeos
        video_original_bytes = await video_original.read()
        video_inserir_bytes = await video_inserir.read()
        
        with tempfile.TemporaryDirectory() as pasta_temp:
            # Salva os vídeos em disco
            caminho_original = salvar_video_em_disco(
                video_original_bytes,
                video_original.filename or "original.mp4",
                pasta_temp
            )
            caminho_inserir = salvar_video_em_disco(
                video_inserir_bytes,
                video_inserir.filename or "inserir.mp4",
                pasta_temp
            )
            
            # Obtém propriedades do vídeo original
            duracao_total = _ffmpeg_obter_duracao(caminho_original)
            props_original = _ffmpeg_obter_propriedades(caminho_original)
            width_original = props_original["width"]
            
            # Valida os tempos
            if start_s < 0:
                start_s = 0.0
            if end_s > duracao_total:
                end_s = duracao_total
            if end_s <= start_s:
                return JSONResponse(
                    status_code=400,
                    content={"error": f"end_s ({end_s}) deve ser maior que start_s ({start_s})"}
                )
            
            # Calcula scale_w automaticamente se não foi fornecido ou se é None
            # Usa 30% da largura do vídeo original, com limites mínimos e máximos
            if scale_w is None:
                scale_w_calculado = int(width_original * 0.3)
                # Limites: mínimo 360px, máximo 1280px
                if scale_w_calculado < 360:
                    scale_w_calculado = 360
                elif scale_w_calculado > 1280:
                    scale_w_calculado = 1280
                scale_w = scale_w_calculado
            
            # Aplica o overlay
            caminho_saida = os.path.join(pasta_temp, "video_teste_overlay.mp4")
            overlay_clip_on_interval(
                input_video=caminho_original,
                insert_clip=caminho_inserir,
                start_s=start_s,
                end_s=end_s,
                out_path=caminho_saida,
                overlay_x=overlay_x,
                overlay_y=overlay_y,
                scale_w=scale_w
            )
            
            # Retorna o vídeo processado
            with open(caminho_saida, "rb") as f:
                video_bytes = f.read()
            
            return StreamingResponse(
                BytesIO(video_bytes),
                media_type="video/mp4",
                headers={
                    "Content-Disposition": f"attachment; filename=teste_overlay_{start_s}_{end_s}.mp4"
                }
            )
    
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(
            status_code=500,
            content={"error": f"Erro ao processar overlay: {str(e)}"}
        )
 