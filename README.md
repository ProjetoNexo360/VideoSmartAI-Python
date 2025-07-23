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
└── README.md
```

## 📥 Requisitos

- Python 3.11
- FFmpeg instalado no PATH
- Servidor local com endpoints:
  - `/speech-to-text`
  - `/text-to-speech`
  - `/add-voice`
  - `/convert-audio`
  - `/voices`

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

```bash
uvicorn main:app --reload
```

Acesse: [http://localhost:8000/docs](http://localhost:8000/docs)

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
