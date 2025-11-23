# 🧠 Custom Audio Generator - Substituição de Voz por IA

Este projeto usa FastAPI para substituir trechos de áudio de um vídeo por vozes geradas por IA, com base em uma palavra-chave e uma lista de nomes. Ideal para vídeos personalizados com entonação natural.

## 🚀 Funcionalidades

- Recebe vídeo (.mp4) com áudio embutido.
- Extrai e transcreve o áudio com timestamps.
- Localiza a palavra-chave no áudio e substitui com voz IA.
- Substituição feita com precisão temporal e naturalidade.
- Suporte a diferentes estilos de pausa: vírgula, ponto ou SSML.
- Envio automático do vídeo final via Webhook.

## 🗂 Estrutura do Projeto

```
.
├── main.py
├── services/
│   └── audio_service.py
├── requirements.txt
├── Dockerfile                    # Para VideoSmartAI API
├── Dockerfile.evolution          # Para Evolution API
├── docker-compose.yaml           # Apenas Evolution API (legado)
├── docker-compose.full.yaml      # Stack completo (recomendado)
├── render.yaml                   # Configuração para Render.com
├── .dockerignore
├── .env.example
├── DEPLOY_RENDER.md
├── check_environment.py
└── README.md
```

## 📥 Requisitos

### Local
- Python 3.11
- FFmpeg instalado no PATH
- PostgreSQL (ou use Docker Compose)
- Redis (ou use Docker Compose)

### Docker/Render
- Docker (para build local)
- Render.com account (para deploy)
- Todas as dependências são instaladas automaticamente via Dockerfile

### APIs Externas Necessárias
- ElevenLabs API (endpoints: `/speech-to-text`, `/text-to-speech`, `/add-voice`, `/convert-audio`, `/voices`)
- Heygen API
- Evolution API (opcional, para WhatsApp)

## 🛠 Instalação

```bash
# Clone o projeto
git clone https://github.com/ProjetoNexo360/VideoSmartAI-Python.git
cd VideoSmartAI-Python

# Crie o ambiente virtual
python -m venv venv
venv\Scripts\activate  # Windows
# source venv/bin/activate  # Linux/macOS

# Instale as dependências
pip install -r requirements.txt
```

## ▶️ Execução

### Local (Desenvolvimento)

```bash
uvicorn main:app --reload
```

Acesse: [http://localhost:8000/docs](http://localhost:8000/docs)

### Docker

#### Opção 1: Apenas a API

```bash
# Build da imagem
docker build -t videosmartai .

# Executar container
docker run -p 8000:8000 --env-file .env videosmartai
```

#### Opção 2: Stack Completo (Recomendado para desenvolvimento)

Inclui: VideoSmartAI API + Evolution API + PostgreSQL + Redis

```bash
# Subir todos os serviços
docker-compose -f docker-compose.full.yaml up

# Ou em background
docker-compose -f docker-compose.full.yaml up -d

# Parar todos os serviços
docker-compose -f docker-compose.full.yaml down
```

Acesse:
- VideoSmartAI API: [http://localhost:8000/docs](http://localhost:8000/docs)
- Evolution API: [http://localhost:8080](http://localhost:8080)

### Render.com (Produção)

Para fazer deploy no Render.com, consulte o guia completo em [DEPLOY_RENDER.md](./DEPLOY_RENDER.md).

Resumo rápido:
1. Conecte seu repositório Git no Render
2. Crie um Web Service usando Docker
3. Configure as variáveis de ambiente
4. O deploy será automático a cada push

## 📤 Endpoint: `POST /processar-video`

### Parâmetros:

- `user_id`: UUID do usuário (query)
- `nomes`: Lista de nomes (form)
- `palavra_chave`: Palavra a ser substituída (form)
- `video`: Arquivo de vídeo .mp4 (form)

## 🔁 Personalização de Pausa na Voz

Dentro de `audio_service.py`:

```python
# Escolha o formato da pausa ao redor do nome:
formato_pausa = ", {nome},"  # (ativo - vírgula)
# formato_pausa = ". {nome}."  # ponto
# formato_pausa = "<break time='500ms'/>{nome}<break time='500ms'/>"  # SSML
```

## 📡 Webhook de Entrega

O vídeo final é enviado automaticamente para `WEBHOOK_URL`, contendo:

- `file`: Arquivo final `.mp4`
- `nome`: Nome substituído
- `user_id`: UUID do usuário

## ✨ Melhorias Futuras

- Preview web do vídeo gerado
- Suporte a múltiplas palavras-chave
- Detecção automática de entonação
- Ajuste visual na timeline de corte

## 📄 Licença

Este projeto é licenciado sob a licença MIT.
