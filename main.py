# main.py
from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Path
from fastapi.responses import JSONResponse
from uuid import UUID
from services.audio_service import processar_video

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
