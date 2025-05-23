import os
import uuid
import httpx
import tempfile
from uuid import UUID
import pyttsx3

ENDPOINT_DE_CUSTOMIZACAO_DE_VOZ = "https://webhook.site/4e3f611f-7ef1-4843-9fd1-6157a17d2bb4"

def gerar_audio_para_nome(nome: str, caminho: str):
    engine = pyttsx3.init()
    engine.setProperty('rate', 150)  # Velocidade da fala
    engine.setProperty('voice', 'brazil')  # Tenta usar voz em português, pode variar por sistema
    engine.save_to_file(nome, caminho)
    engine.runAndWait()


async def enviar_audio(user_id: UUID, audio_base_bytes: bytes, nome_audio_base: str, nome_arquivo: str, conteudo: bytes, tipo: str):
    async with httpx.AsyncClient() as client:
        files = {
            "audio_base": (nome_audio_base, audio_base_bytes, "audio/mp3"),
            "file": (nome_arquivo, conteudo, tipo)
        }
        await client.post(
            ENDPOINT_DE_CUSTOMIZACAO_DE_VOZ,
            files=files,
            data={
                "nome_original": nome_arquivo,
                "user_id": str(user_id)
            }
        )

async def processar_audios(user_id: UUID, nomes: list[str], audio_base_bytes: bytes, nome_audio_base: str):
    with tempfile.TemporaryDirectory() as pasta_temp:
        for nome in nomes:
            nome_uuid = f"{nome}_{uuid.uuid4()}.wav"
            caminho_audio = os.path.join(pasta_temp, nome_uuid)
            gerar_audio_para_nome(nome, caminho_audio)

            with open(caminho_audio, "rb") as f:
                conteudo = f.read()
                await enviar_audio(user_id, audio_base_bytes, nome_audio_base, nome_uuid, conteudo, "audio/wav")
