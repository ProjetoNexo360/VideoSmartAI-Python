# Dockerfile para VideoSmartAI-Python com FFmpeg
FROM python:3.11-slim

# Instala dependências do sistema necessárias para FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    ffprobe \
    && rm -rf /var/lib/apt/lists/*

# Define o diretório de trabalho
WORKDIR /app

# Copia o arquivo de dependências
COPY requirements.txt .

# Instala as dependências Python
RUN pip install --no-cache-dir -r requirements.txt

# Copia o código da aplicação
COPY . .

# Expõe a porta (o Render vai definir a porta via variável de ambiente PORT)
EXPOSE 8000

# Comando para iniciar a aplicação
# O Render define a variável PORT automaticamente
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}

