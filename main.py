# main.py
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Path
from fastapi.responses import JSONResponse
from uuid import UUID
from services.audio_service import processar_video
from io import BytesIO
from fastapi.responses import StreamingResponse
from services.audio_service import enviar_video_para_webhook


app = FastAPI()

@app.post("/gerar-videos/{user_id}")
async def gerar_videos(
    user_id: UUID = Path(..., description="ID do usuário no formato UUID"),
    background_tasks: BackgroundTasks = None,
    nomes: str = Form(...),
    palavra_chave: str = Form(...),
    video: UploadFile = File(...)
):
    nomes_lista = [n.strip() for n in nomes.split(",")]
    video_bytes = await video.read()
    nome_video = video.filename

    background_tasks.add_task(
        processar_video,
        user_id=user_id,
        nomes=nomes_lista,
        palavra_chave=palavra_chave,
        video_bytes=video_bytes,
        nome_video=nome_video
    )

    return JSONResponse(content={"message": "Processamento iniciado", "user_id": str(user_id)}, status_code=202)

# main.py (continuação)
from services.audio_service import (
    salvar_video_em_disco, extrair_audio_do_video, transcrever_audio_com_timestamps,
    verificar_ou_criar_voz, gerar_video_para_nome
)
from redis_client import salvar_preview, obter_preview, remover_preview
import tempfile

@app.post("/gerar-preview/{user_id}")
async def gerar_preview(
    user_id: UUID,
    nomes: str = Form(...),
    palavra_chave: str = Form(...),
    video: UploadFile = File(...)
):
    nomes_lista = [n.strip() for n in nomes.split(",")]
    primeiro_nome = nomes_lista[0]
    video_bytes = await video.read()
    nome_video = video.filename

    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_video = salvar_video_em_disco(video_bytes, nome_video, pasta_temp)
        caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)
        user_voice_id = await verificar_ou_criar_voz(f"user_{user_id}", caminho_audio, pasta_temp)

        caminho_saida_preview = await gerar_video_para_nome(
            nome=primeiro_nome,
            palavra_chave=palavra_chave,
            transcricao=transcricao,
            segmentos=segmentos,
            user_voice_id=user_voice_id,
            caminho_video=caminho_video,
            caminho_audio=caminho_audio,
            pasta_temp=pasta_temp,
            user_id=user_id,
            enviar_webhook=False  # 👈 NÃO envia o vídeo ainda
        )

        with open(caminho_saida_preview, "rb") as f:
            conteudo_video = f.read()


        # Salvar dados no Redis para o restante
        await salvar_preview(user_id, {
            "nomes": nomes_lista,
            "palavra_chave": palavra_chave,
            "transcricao": transcricao,
            "segmentos": segmentos,
            "voice_id": user_voice_id,
            "video_bytes": video_bytes.decode("latin1"),
            "nome_video": nome_video
        })



    return StreamingResponse(BytesIO(conteudo_video), media_type="video/mp4", headers={
        "Content-Disposition": f"attachment; filename=preview_{primeiro_nome}.mp4"
    })


@app.post("/confirmar-envio/{user_id}")
async def confirmar_envio(user_id: UUID, background_tasks: BackgroundTasks):
    from redis_client import obter_preview, remover_preview
    from services.audio_service import (
        salvar_video_em_disco, extrair_audio_do_video,
        gerar_video_para_nome
    )

    dados = await obter_preview(user_id)
    if not dados:
        return JSONResponse(status_code=404, content={"error": "Nenhum preview encontrado ou expirado."})

    async def gerar_restante():
        import httpx
        with tempfile.TemporaryDirectory() as pasta_temp:
            caminho_video = salvar_video_em_disco(
                dados["video_bytes"].encode("latin1"),
                dados["nome_video"],
                pasta_temp
            )
            caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)

            nomes_todos = dados["nomes"]
            nomes_unicos = list(set(n.strip().lower() for n in nomes_todos))

            videos_gerados = {}

            for nome in nomes_unicos:
                try:
                    caminho = await gerar_video_para_nome(
                        nome=nome,
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
                    videos_gerados[nome] = caminho
                except Exception as e:
                    print(f"Erro ao gerar vídeo para {nome}: {e}")

            for nome in nomes_todos:
                nome_normalizado = nome.strip().lower()
                caminho = videos_gerados.get(nome_normalizado)
                if not caminho:
                    continue
                await enviar_video_para_webhook(caminho, nome, user_id)

        await remover_preview(user_id)

    background_tasks.add_task(gerar_restante)
    return JSONResponse(content={"message": "Processamento dos vídeos restantes iniciado."})
