from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks, Path
from fastapi.responses import JSONResponse
from uuid import UUID
from services.audio_service import processar_audios

app = FastAPI()

@app.post("/gerar-audios/{user_id}")
async def gerar_audios(
    user_id: UUID = Path(..., description="ID do usuário no formato UUID"),
    background_tasks: BackgroundTasks = None,
    nomes: str = Form(...),
    audio_base: UploadFile = File(...)
):
    nomes_lista = [n.strip() for n in nomes.split(",")]
    audio_base_bytes = await audio_base.read()
    nome_audio_base = audio_base.filename

    background_tasks.add_task(processar_audios, user_id, nomes_lista, audio_base_bytes, nome_audio_base)

    return JSONResponse(content={"message": "Processamento iniciado", "user_id": str(user_id)}, status_code=202)
