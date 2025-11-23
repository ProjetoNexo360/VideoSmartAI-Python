# Changelog - VideoSmartAI-Python

## [2025-01-XX] - Deploy Render + Ajustes WhatsApp

### ✨ Adicionado
- **Dockerfile** com FFmpeg instalado para deploy no Render
- **Dockerfile.evolution** para Evolution API
- **render.yaml** com configuração completa para Render.com
- **docker-compose.full.yaml** para stack completo local
- **DEPLOY_RENDER.md** com guia completo de deploy
- **check_environment.py** para verificar ambiente
- Suporte a variáveis de ambiente para Redis (REDIS_URL)
- Suporte a variáveis de ambiente para JWT_SECRET

### 🔧 Modificado
- **Removido envio de mensagem de texto** antes do vídeo no WhatsApp
- **Removido caption** do vídeo enviado via WhatsApp (apenas vídeo, sem texto)
- **redis_client.py**: Agora suporta REDIS_URL ou variáveis individuais
- **auth_utils.py**: JWT_SECRET agora vem de variável de ambiente
- **README.md**: Atualizado com informações sobre Docker e Render

### 🐛 Corrigido
- Configuração para funcionar no ambiente cloud do Render
- FFmpeg instalado automaticamente via Dockerfile

### 📝 Notas
- O projeto agora está pronto para deploy no Render.com
- Todos os serviços (API, Evolution API, PostgreSQL, Redis) podem subir juntos
- WhatsApp agora envia apenas o vídeo, sem mensagens de texto ou captions

