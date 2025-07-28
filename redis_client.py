# redis_client.py
import redis.asyncio as redis
import orjson

redis_client = redis.Redis(host="localhost", port=6379, decode_responses=False)

async def salvar_preview(user_id, data: dict, ttl=3600):
    await redis_client.setex(f"preview:{user_id}", ttl, orjson.dumps(data))

async def obter_preview(user_id):
    data = await redis_client.get(f"preview:{user_id}")
    return orjson.loads(data) if data else None

async def remover_preview(user_id):
    await redis_client.delete(f"preview:{user_id}")
