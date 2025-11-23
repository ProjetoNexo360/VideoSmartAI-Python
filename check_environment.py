#!/usr/bin/env python3
"""
Script para verificar se o ambiente está configurado corretamente.
Útil para testar antes do deploy no Render.
"""

import subprocess
import sys
import os

def check_ffmpeg():
    """Verifica se FFmpeg está instalado e acessível."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            print(f"✅ FFmpeg encontrado: {version_line}")
            return True
        else:
            print("❌ FFmpeg não encontrado ou com erro")
            return False
    except FileNotFoundError:
        print("❌ FFmpeg não encontrado no PATH")
        return False
    except Exception as e:
        print(f"❌ Erro ao verificar FFmpeg: {e}")
        return False

def check_ffprobe():
    """Verifica se FFprobe está instalado e acessível."""
    try:
        result = subprocess.run(
            ["ffprobe", "-version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            version_line = result.stdout.split('\n')[0]
            print(f"✅ FFprobe encontrado: {version_line}")
            return True
        else:
            print("❌ FFprobe não encontrado ou com erro")
            return False
    except FileNotFoundError:
        print("❌ FFprobe não encontrado no PATH")
        return False
    except Exception as e:
        print(f"❌ Erro ao verificar FFprobe: {e}")
        return False

def check_python_packages():
    """Verifica se os pacotes Python necessários estão instalados."""
    required_packages = [
        "fastapi",
        "uvicorn",
        "httpx",
        "sqlalchemy",
        "psycopg",
        "redis",
        "python-dotenv",
        "python-jose",
        "passlib",
        "orjson",
    ]
    
    missing_packages = []
    for package in required_packages:
        try:
            __import__(package.replace("-", "_"))
            print(f"✅ {package} instalado")
        except ImportError:
            print(f"❌ {package} NÃO instalado")
            missing_packages.append(package)
    
    if missing_packages:
        print(f"\n⚠️  Pacotes faltando: {', '.join(missing_packages)}")
        print("Execute: pip install -r requirements.txt")
        return False
    return True

def check_env_vars():
    """Verifica se as variáveis de ambiente essenciais estão configuradas."""
    essential_vars = [
        "DATABASE_URL",
        "REDIS_URL",
        "JWT_SECRET",
    ]
    
    optional_vars = [
        "ELEVEN_NODE_API",
        "HEYGEN_NODE_API",
        "EVO_BASE",
    ]
    
    print("\n📋 Variáveis de Ambiente Essenciais:")
    missing_essential = []
    for var in essential_vars:
        value = os.getenv(var)
        if value:
            # Mascara valores sensíveis
            if "SECRET" in var or "PASSWORD" in var or "KEY" in var:
                masked = value[:4] + "..." + value[-4:] if len(value) > 8 else "***"
                print(f"✅ {var}={masked}")
            else:
                print(f"✅ {var} configurada")
        else:
            print(f"❌ {var} NÃO configurada")
            missing_essential.append(var)
    
    print("\n📋 Variáveis de Ambiente Opcionais:")
    for var in optional_vars:
        value = os.getenv(var)
        if value:
            print(f"✅ {var} configurada")
        else:
            print(f"⚠️  {var} não configurada (opcional)")
    
    if missing_essential:
        print(f"\n⚠️  Variáveis essenciais faltando: {', '.join(missing_essential)}")
        print("Configure-as no arquivo .env ou nas variáveis de ambiente do Render")
        return False
    return True

def main():
    """Executa todas as verificações."""
    print("🔍 Verificando ambiente...\n")
    
    results = []
    
    print("=" * 50)
    print("1. Verificando FFmpeg")
    print("=" * 50)
    results.append(check_ffmpeg())
    
    print("\n" + "=" * 50)
    print("2. Verificando FFprobe")
    print("=" * 50)
    results.append(check_ffprobe())
    
    print("\n" + "=" * 50)
    print("3. Verificando Pacotes Python")
    print("=" * 50)
    results.append(check_python_packages())
    
    print("\n" + "=" * 50)
    print("4. Verificando Variáveis de Ambiente")
    print("=" * 50)
    results.append(check_env_vars())
    
    print("\n" + "=" * 50)
    print("📊 Resumo")
    print("=" * 50)
    
    if all(results):
        print("✅ Ambiente configurado corretamente!")
        return 0
    else:
        print("❌ Algumas verificações falharam. Corrija os problemas acima.")
        return 1

if __name__ == "__main__":
    sys.exit(main())

