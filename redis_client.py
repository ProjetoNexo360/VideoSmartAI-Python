# redis_client.py
import redis.asyncio as redis
import orjson
import os
from urllib.parse import urlparse

# Suporta REDIS_URL (formato: redis://host:port ou redis://:password@host:port)
# ou variáveis individuais REDIS_HOST e REDIS_PORT
REDIS_URL = os.getenv("REDIS_URL")
if REDIS_URL:
    # Parse da URL do Redis
    parsed = urlparse(REDIS_URL)
    redis_host = parsed.hostname or "localhost"
    redis_port = parsed.port or 6379
    redis_password = parsed.password
    redis_db = int(parsed.path.lstrip("/")) if parsed.path else 0
    
    if redis_password:
        redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            password=redis_password,
            db=redis_db,
            decode_responses=False
        )
    else:
        redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            db=redis_db,
            decode_responses=False
        )
else:
    # Fallback para variáveis individuais ou localhost
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    redis_client = redis.Redis(
        host=redis_host,
        port=redis_port,
        decode_responses=False
    )

async def salvar_preview(user_id, data: dict, ttl=3600):
    await redis_client.setex(f"preview:{user_id}", ttl, orjson.dumps(data))

async def obter_preview(user_id):
    data = await redis_client.get(f"preview:{user_id}")
    return orjson.loads(data) if data else None

async def remover_preview(user_id):
    await redis_client.delete(f"preview:{user_id}")
