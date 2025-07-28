import os
import tempfile
import httpx
import subprocess
import difflib
from typing import List
from uuid import UUID
import json

API_BASE_URL = "http://localhost:3000"
WEBHOOK_URL = "https://webhook.site/9db6623c-bc6a-4495-8089-e0299106283c"
HTTP_TIMEOUT = 120.0
CONTEXTO_PALAVRAS = 3
AJUSTE_MS = 150

async def processar_video(user_id: UUID, nomes: List[str], palavra_chave: str, video_bytes: bytes, nome_video: str):
    voz_padrao_nome = f"user_{user_id}"

    with tempfile.TemporaryDirectory() as pasta_temp:
        caminho_video = salvar_video_em_disco(video_bytes, nome_video, pasta_temp)
        caminho_audio = extrair_audio_do_video(caminho_video, pasta_temp)
        transcricao, segmentos = await transcrever_audio_com_timestamps(caminho_audio)

        if not transcricao:
            raise ValueError("Transcrição retornou vazia. Verifique o áudio original.")

        user_voice_id = await verificar_ou_criar_voz(voz_padrao_nome, caminho_audio, pasta_temp)

        for nome in nomes:
            try:
                await gerar_video_para_nome(nome, palavra_chave, transcricao, segmentos, user_voice_id, caminho_video, caminho_audio, pasta_temp, user_id)
            except (httpx.HTTPStatusError, ValueError) as e:
                print(f"Erro ao gerar áudio para {nome}: {str(e)}")

def salvar_video_em_disco(video_bytes: bytes, nome_video: str, pasta_temp: str) -> str:
    caminho_video = os.path.join(pasta_temp, nome_video)
    with open(caminho_video, "wb") as f:
        f.write(video_bytes)
    return caminho_video

def extrair_audio_do_video(caminho_video: str, pasta_temp: str) -> str:
    caminho_audio = os.path.join(pasta_temp, "original_audio.wav")
    subprocess.run([
        "ffmpeg", "-y", "-i", caminho_video,
        "-ac", "1", "-ar", "16000", "-vn", caminho_audio
    ], check=True)
    return caminho_audio

async def transcrever_audio_com_timestamps(caminho_audio: str):
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        with open(caminho_audio, "rb") as audio_file:
            files = {"file": ("original.wav", audio_file, "audio/wav")}
            response = await client.post(f"{API_BASE_URL}/speech-to-text", files=files)
            print("Resposta da transcrição:", response.text)  # DEBUG
            if response.status_code != 200:
                raise ValueError(f"Erro na transcrição: {response.status_code} - {response.text}")
            try:
                response_json = response.json()
            except json.JSONDecodeError:
                raise ValueError("Resposta da API de transcrição não é um JSON válido.")
            transcricao = response_json.get("text", "") or response_json.get("transcribed", {}).get("text", "")
            segmentos = response_json.get("transcribed", {}).get("words", [])
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
            with open(caminho_convertido, "wb") as out_file:
                out_file.write(response.content)

    with open(caminho_convertido, "rb") as converted_file:
        files = [("file", ("converted_audio.wav", converted_file, "audio/wav"))]
        data = {"name": voz_padrao_nome, "language": "pt-BR"}
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(f"{API_BASE_URL}/add-voice", data=data, files=files)
            response_json = response.json()
            return response_json.get("voiceId") or response_json.get("voice_id") or response_json.get("voice", {}).get("voiceId")

async def gerar_video_para_nome(nome: str, palavra_chave: str, transcricao: str, segmentos: List[dict], user_voice_id: str, caminho_video: str, caminho_audio: str, pasta_temp: str, user_id: UUID, enviar_webhook: bool = True ):
    palavra_chave_lower = palavra_chave.lower()

    palavra_alvo = None
    for i, w in enumerate(segmentos):
        if w["type"] == "word" and palavra_chave_lower in w["text"].lower():
            palavra_alvo = w
            idx = i
            break

    if not palavra_alvo:
        raise ValueError(f"Palavra-chave '{palavra_chave}' não encontrada na transcrição.")

    if not user_voice_id:
        raise ValueError("ID da voz do usuário está vazio.")

    inicio = max(0.0, segmentos[max(0, idx - CONTEXTO_PALAVRAS)]["start"] - (AJUSTE_MS / 1000))
    fim = segmentos[min(len(segmentos)-1, idx + CONTEXTO_PALAVRAS)]["end"] + (AJUSTE_MS / 1000)

    palavras_contexto = [w["text"] for w in segmentos if w["type"] == "word" and inicio <= w["start"] and w["end"] <= fim]
    texto_original = " ".join(palavras_contexto)

    # Escolha do formato de pausa
    #formato_pausa = ", {nome},"  # Pausa por vírgula (ativo)
    formato_pausa = ". {nome}."  # Pausa por ponto
    #formato_pausa = "<break time='500ms'/>{nome}<break time='500ms'/>"  # Pausa com SSML (se suportado)

    nome_formatado = formato_pausa.format(nome=nome)
    novo_texto = texto_original.replace(palavra_alvo["text"], nome_formatado)

    payload = {"voiceId": user_voice_id, "text": novo_texto}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        response = await client.post(f"{API_BASE_URL}/text-to-speech", json=payload)
        response.raise_for_status()
        caminho_trecho_ia = os.path.join(pasta_temp, f"ia_{nome}.mp3")
        with open(caminho_trecho_ia, "wb") as f:
            f.write(response.content)

    caminho_audio_antes = os.path.join(pasta_temp, "antes.wav")
    caminho_audio_depois = os.path.join(pasta_temp, "depois.wav")
    caminho_audio_final = os.path.join(pasta_temp, f"audio_final_{nome}.wav")
    caminho_saida_video = os.path.join(pasta_temp, f"video_{nome}.mp4")

    subprocess.run(["ffmpeg", "-y", "-i", caminho_audio, "-ss", "0", "-to", str(inicio), caminho_audio_antes], check=True)
    subprocess.run(["ffmpeg", "-y", "-i", caminho_audio, "-ss", str(fim), caminho_audio_depois], check=True)

    subprocess.run([
        "ffmpeg", "-y",
        "-i", caminho_audio_antes,
        "-i", caminho_trecho_ia,
        "-i", caminho_audio_depois,
        "-filter_complex", "[0:0][1:0][2:0]concat=n=3:v=0:a=1[out]",
        "-map", "[out]",
        caminho_audio_final
    ], check=True)

    subprocess.run([
        "ffmpeg", "-y", "-i", caminho_video, "-i", caminho_audio_final,
        "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-c:a", "aac", "-shortest", caminho_saida_video
    ], check=True)

    if enviar_webhook:
        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
                with open(caminho_saida_video, "rb") as final_video:
                    files = {"file": (f"video_{nome}.mp4", final_video, "video/mp4")}
                    data = {"user_id": str(user_id), "nome": nome}
                    await client.post(WEBHOOK_URL, data=data, files=files)
        except httpx.HTTPError as e:
            print(f"Falha ao enviar vídeo para {nome}: {e}")


    print(f"Novo vídeo gerado e enviado para {nome}: {caminho_saida_video}")
    return caminho_saida_video 
