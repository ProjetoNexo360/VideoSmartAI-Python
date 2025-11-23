# 🚀 Guia de Deploy no Render.com

Este guia explica como fazer deploy do VideoSmartAI-Python na plataforma Render.com.

## 📋 Pré-requisitos

1. Conta no [Render.com](https://render.com/)
2. Repositório Git (GitHub, GitLab ou Bitbucket) com o código
3. Todas as variáveis de ambiente configuradas

## 🔧 Passo a Passo

### 1. Preparar o Repositório

Certifique-se de que os seguintes arquivos estão no repositório:
- `Dockerfile`
- `render.yaml` (opcional, mas recomendado)
- `requirements.txt`
- Todo o código da aplicação

### 2. Deploy Automático com render.yaml (Recomendado)

O arquivo `render.yaml` já está configurado para criar todos os serviços automaticamente:

1. No dashboard do Render, vá em **New +** → **Blueprint**
2. Conecte seu repositório Git
3. O Render detectará o `render.yaml` e criará:
   - **VideoSmartAI API** (aplicação principal)
   - **Evolution API** (WhatsApp)
   - **PostgreSQL** (banco de dados)
   - **Redis** (cache - precisa criar manualmente)

### 3. Criar Serviços Manualmente (Alternativa)

#### 3.1. Banco de Dados PostgreSQL

1. No dashboard do Render, vá em **New +** → **PostgreSQL**
2. Configure:
   - **Name**: `videosmartai-db`
   - **Database**: `videosmartai`
   - **User**: `videosmartai`
   - **Plan**: Escolha conforme sua necessidade (Starter, Standard, Pro)
3. Anote a **Internal Database URL** (será usada como `DATABASE_URL`)

#### 3.2. Serviço Redis

**Opção A: Render Key Value (Recomendado)**
1. No dashboard do Render, vá em **New +** → **Key Value Store**
2. Configure:
   - **Name**: `videosmartai-redis`
   - **Plan**: Escolha conforme sua necessidade
3. Anote a **Connection String**

**Opção B: Redis Externo**
- Use um serviço Redis gerenciado (Upstash, Redis Cloud, etc.)
- Configure a `REDIS_URL` manualmente

#### 3.3. Evolution API (WhatsApp)

1. No dashboard do Render, vá em **New +** → **Web Service**
2. Conecte seu repositório Git
3. Configure:
   - **Name**: `evolution-api`
   - **Environment**: `Docker`
   - **Dockerfile Path**: `./Dockerfile.evolution`
   - **Docker Context**: `.`
   - **Plan**: Starter (pode aumentar se necessário)
4. Configure as variáveis de ambiente (veja seção abaixo)

#### 3.4. VideoSmartAI API (Aplicação Principal)

1. No dashboard do Render, vá em **New +** → **Web Service**
2. Conecte seu repositório Git
3. Configure:
   - **Name**: `videosmartai-api`
   - **Environment**: `Docker`
   - **Region**: Escolha a mais próxima dos seus usuários
   - **Branch**: `main` (ou sua branch principal)
   - **Root Directory**: Deixe vazio (ou `.` se necessário)
   - **Dockerfile Path**: `./Dockerfile`
   - **Docker Context**: `.`
   - **Plan**: Escolha conforme sua necessidade

### 4. Configurar Variáveis de Ambiente

#### 4.1. VideoSmartAI API

No painel do Web Service `videosmartai-api`, vá em **Environment** e adicione:

#### Database
```
DATABASE_URL=postgresql+psycopg://user:password@host:port/dbname
```

#### Redis
```
REDIS_URL=redis://host:port
# Ou
REDIS_HOST=host
REDIS_PORT=6379
```

#### ElevenLabs
```
ELEVEN_NODE_API=https://api-elevenlabs-nodejs.onrender.com/api
ELEVEN_API_NAMESPACE=/elevenlabs
ELEVEN_AUTH_URL=https://api-elevenlabs-nodejs.onrender.com/api/auth/login
ELEVEN_USERNAME=seu_usuario
ELEVEN_PASSWORD=sua_senha
```

#### Heygen
```
HEYGEN_NODE_API=https://api-heygen-nodejs.onrender.com/api
HEYGEN_API_NAMESPACE=
HEYGEN_AUTH_URL=https://api-heygen-nodejs.onrender.com/api/auth/login
HEYGEN_USERNAME=seu_usuario
HEYGEN_PASSWORD=sua_senha
HEYGEN_DEBUG=1
```

#### Evolution API
```
EVO_BASE=http://evolution-api:8080
# Ou use a URL externa se os serviços não estiverem na mesma rede privada
# EVO_BASE=https://evolution-api.onrender.com
EVO_APIKEY=sua_chave_api_forte
EVO_INSTANCE=default
EVO_INTEGRATION=WHATSAPP-BAILEYS
```

**⚠️ IMPORTANTE**: 
- Se os serviços estiverem na mesma rede privada do Render, use `http://evolution-api:8080`
- Se não, use a URL externa do serviço Evolution API
- A `EVO_APIKEY` deve ser a mesma configurada no serviço Evolution API

#### 4.2. Evolution API

No painel do Web Service `evolution-api`, vá em **Environment** e adicione:

```
# API
SERVER_PORT=8080
AUTHENTICATION_API_KEY=sua_chave_api_forte_aqui

# Database (use a Internal Database URL do PostgreSQL)
DATABASE_ENABLED=true
DATABASE_PROVIDER=postgresql
DATABASE_CONNECTION_URI=postgresql://user:password@host:port/dbname

# Redis (use a Connection String do Redis)
CACHE_LOCAL_ENABLED=false
CACHE_REDIS_ENABLED=true
CACHE_REDIS_URI=redis://host:port/2
CACHE_REDIS_TTL=604800
CONFIG_SESSION_PHONE_VERSION=2.3000.1026354025

NODE_ENV=production
PORT=8080
```

#### Outras Configurações
```
AUTOMATION_API_BASE=http://seu-automation-api:3000
WEBHOOK_URL=https://seu-webhook-url
HTTP_TIMEOUT=120.0
PALAVRAS_ANTES=2
PALAVRAS_DEPOIS=0
AJUSTE_MS=150
HEYGEN_MIN_VIDEO_DURATION=5.0
JWT_SECRET=sua_chave_secreta_forte_aqui
```

**⚠️ IMPORTANTE**: Gere uma chave JWT_SECRET forte e segura!

### 5. Rede Privada (Importante para Evolution API)

Para que os serviços se comuniquem internamente:

1. No dashboard do Render, vá em **Settings** → **Private Networking**
2. Certifique-se de que todos os serviços estão na mesma rede privada
3. Use URLs internas nas variáveis de ambiente:
   - `EVO_BASE=http://evolution-api:8080` (ao invés da URL externa)
   - `DATABASE_URL` use a **Internal Database URL**
   - `REDIS_URL` use a **Internal Connection String**

### 6. Deploy Automático

Após configurar tudo:
1. Render detectará automaticamente os Dockerfiles
2. Fará o build das imagens Docker
3. Iniciará os serviços na ordem correta (PostgreSQL → Redis → Evolution API → VideoSmartAI API)
4. O deploy será automático a cada push na branch configurada

### 7. Verificar o Deploy

1. Acesse a URL fornecida pelo Render (ex: `https://videosmartai-api.onrender.com`)
2. Teste o endpoint de health: `GET /`
3. Acesse a documentação: `GET /docs`

## 🔍 Troubleshooting

### Erro: FFmpeg não encontrado
- Verifique se o Dockerfile está instalando o FFmpeg corretamente
- Verifique os logs do build no Render

### Erro: Conexão com banco de dados
- Verifique se o `DATABASE_URL` está correto
- Use a **Internal Database URL** do Render (não a externa)
- Verifique se o banco está na mesma região do serviço

### Erro: Timeout nas requisições
- Render tem um timeout padrão de 30 segundos para requests
- Para processamentos longos, considere usar Background Workers
- Aumente o `HTTP_TIMEOUT` se necessário

### Erro: Memória insuficiente
- Processamento de vídeo/áudio consome muita memória
- Considere usar um plano maior (Standard ou Pro)
- Monitore o uso de memória nos logs

## 📊 Monitoramento

- **Logs**: Acesse os logs em tempo real no dashboard do Render
- **Métricas**: Monitore CPU, memória e rede no dashboard
- **Health Checks**: Configure health checks em `/` endpoint

## 🔄 Atualizações

O Render faz deploy automático a cada push na branch configurada. Para deploy manual:
1. Vá no dashboard do serviço
2. Clique em **Manual Deploy**
3. Escolha a branch/commit desejado

## 💰 Custos

- **Starter Plan**: $7/mês (512MB RAM, 0.1 CPU)
- **Standard Plan**: $25/mês (2GB RAM, 1 CPU)
- **Pro Plan**: $85/mês (4GB RAM, 2 CPU)

Para processamento de vídeo/áudio, recomenda-se pelo menos **Standard Plan**.

## 📚 Recursos Adicionais

- [Documentação Render](https://render.com/docs)
- [Docker no Render](https://render.com/docs/docker)
- [Environment Variables](https://render.com/docs/environment-variables)

