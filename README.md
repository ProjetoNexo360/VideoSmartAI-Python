# 🗣️ Text-to-Speech Webhook API

Este é um projeto FastAPI que recebe uma lista de nomes e um áudio base, gera áudios personalizados com os nomes via TTS (Text-to-Speech), e envia cada arquivo para um endpoint externo de customização de voz.

## 🚀 Funcionalidades

- Recebe um áudio base (por exemplo, uma introdução gravada).
- Recebe uma lista de nomes.
- Gera arquivos de áudio com os nomes usando a biblioteca `gTTS`.
- Envia os áudios gerados e o áudio base para um webhook externo.
- Processamento assíncrono com `BackgroundTasks`.

---

## 📦 Estrutura do Projeto

```
textToSpeech/
├── main.py                # Ponto de entrada FastAPI (Controller)
├── services/
│   └── audio_service.py   # Lógica de geração e envio de áudio (Service)
└── README.md              # Este arquivo
```

---

## 📥 Requisitos

- Python 3.11
- FastAPI
- Uvicorn
- gTTS
- httpx

---

## 🔧 Instalação

```bash
# Clone o repositório
git clone https://github.com/ProjetoNexo360/VideoSmartAI-Python.git


# Crie e ative um ambiente virtual
python -m venv venv
source venv/bin/activate     # Linux/macOS
venv\Scripts\activate      # Windows

# Instale as dependências
pip install -r requirements.txt
```

Arquivo `requirements.txt` contem:

```
fastapi
uvicorn
httpx
pyttsx3
python-multipart
```

---

## ▶️ Como executar

```bash
uvicorn main:app --reload
```

Acesse a documentação interativa:
- http://localhost:8000/docs

---

## 📤 Endpoint: `POST /gerar-audios/{user_id}`

### 🔗 Exemplo de chamada via Swagger UI ou Postman

**URL:**  
```
POST http://localhost:8000/gerar-audios/{user_id}
```

**Parâmetros:**

- `user_id` (path): UUID do usuário
- `nomes` (form): Lista de nomes separados por vírgula (ex: `João,Maria,Carlos`)
- `audio_base` (file): Arquivo de áudio base (formato mp3 recomendado)

**Retorno:**
```json
{
  "message": "Processamento iniciado",
  "user_id": "6f150efb-6f8b-4df7-8889-342d8a2f4cb5"
}
```

---

## 📡 Webhook

O sistema envia para o webhook definido em `WEBHOOK_URL` no formato `multipart/form-data`, contendo:

- `audio_base`: Arquivo de áudio base
- `file`: Áudio gerado com o nome
- `nome_original`: Nome do arquivo
- `user_id`: UUID do usuário

---

## ✨ Futuras melhorias (sugestões)

- Dashboard para acompanhamento
- Notificações por e-mail e whatsapp
- Upload de nomes via `.csv`

---

## 📄 Licença

Este projeto é livre para uso e modificação. Licenciado sob MIT License.
