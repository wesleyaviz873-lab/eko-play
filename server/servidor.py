#!/usr/bin/env python3
"""Servidor do EKO: recebe links pelo celular e coloca na fila de download."""

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import queue
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import garimpar as g

CFG = g.carregar_config()
PORT = int(os.environ.get("GARIMPO_PORTA") or CFG.get("porta", 8787))
HOST = os.environ.get("GARIMPO_HOST") or CFG.get("host", "0.0.0.0")
TOKEN = os.environ.get("GARIMPO_TOKEN") or CFG.get("token", "")
FILA_PATH = g.caminho(CFG, "state_dir") / "fila.json"
LOG_DIR = g.caminho(CFG, "log_dir")
DISPOSITIVOS_PATH = g.caminho(CFG, "state_dir") / "dispositivos.json"
PESSOAS_PATH = g.caminho(CFG, "state_dir") / "pessoas.json"
APK_DIR = ROOT / "apk"
GOOGLE_CLIENT_ID = str(CFG.get("google_client_id") or "")

_trava = threading.RLock()
_fila = []
_avisar = threading.Condition(_trava)
_tentativas = {}
_ultima_origem = ""


def _limitar(ip, chave, maximo=12, janela=60):
    agora = time.time()
    with _trava:
        marcas = _tentativas.setdefault((ip, chave), [])
        marcas[:] = [m for m in marcas if agora - m < janela]
        if len(marcas) >= maximo:
            return False
        marcas.append(agora)
        return True


_balde = {}
_falhas = {}
_bloqueados = {}


def _permitir(ip, maximo=300, janela=60):
    agora = time.time()
    with _trava:
        ate = _bloqueados.get(ip)
        if ate and ate > agora:
            return False
        marcas = _balde.setdefault(ip, [])
        marcas[:] = [m for m in marcas if agora - m < janela]
        if len(marcas) >= maximo:
            return False
        marcas.append(agora)
        return True


def _falhou_auth(ip):
    agora = time.time()
    with _trava:
        total, inicio = _falhas.get(ip, (0, agora))
        if agora - inicio > 600:
            total, inicio = 0, agora
        total += 1
        _falhas[ip] = (total, inicio)
        if total >= 25:
            _falhas.pop(ip, None)
            _bloqueados[ip] = agora + 900
            print(f" EKO: {ip} bloqueado 15 min por tentativas")


def sincronizar_mini():
    rclone = Path(r"D:\eko\tools\rclone\rclone-v1.75.2-windows-amd64\rclone.exe")
    config = Path(r"D:\eko\tools\rclone\rclone.conf")
    if not rclone.is_file() or not config.is_file():
        return
    try:
        subprocess.run(
            [str(rclone), "sync", str(g.caminho(CFG, "music_dir")),
             "minipc:/srv/eko/music", "--config", str(config),
             "--update", "--transfers", "4", "--checkers", "8",
             "--exclude", "*.tmp", "--exclude", "*.part"],
            timeout=180, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, check=False,
        )
    except Exception:
        pass


def escanear_navidrome():
    base = str(CFG.get("navidrome_url") or "").rstrip("/")
    if not base:
        return
    params = urlencode({
        "u": CFG.get("navidrome_usuario") or "admin",
        "p": CFG.get("navidrome_senha") or "",
        "v": "1.16.1",
        "c": "eko-servidor",
        "f": "json",
    })
    try:
        with urlopen(f"{base}/rest/startScan.view?{params}", timeout=10) as r:
            r.read(200)
    except Exception:  # noqa: BLE001
        pass


def carregar_fila():
    if FILA_PATH.exists():
        try:
            for job in json.loads(FILA_PATH.read_text(encoding="utf-8")):
                if job.get("status") in ("na_fila", "baixando"):
                    job["status"] = "interrompido"
                _fila.append(job)
        except (json.JSONDecodeError, OSError):
            pass


def salvar_fila():
    _gravar_json(FILA_PATH, _fila[-300:])


def novidades():
    with _trava:
        return [dict(j) for j in reversed(_fila)]


def _gravar_json(caminho, obj):
    """Grava JSON de forma segura: arquivo temporario + replace atomico."""
    try:
        caminho.parent.mkdir(parents=True, exist_ok=True)
        temp = caminho.with_suffix(caminho.suffix + ".tmp")
        temp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(temp, caminho)
    except OSError as erro:
        print(f" EKO: falha ao gravar {caminho.name}: {erro}")


def _hash(texto):
    return hashlib.sha256(texto.encode()).hexdigest()


def _carregar_dispositivos():
    try:
        dados = json.loads(DISPOSITIVOS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    saida = []
    for d in dados:
        if not isinstance(d, dict):
            continue
        if d.get("hash"):
            saida.append(d)
            continue
        antigo = d.get("token")
        if not antigo:
            continue
        saida.append({
            "id": d.get("id") or antigo[:8],
            "hash": _hash(antigo),
            "nome": d.get("nome") or d.get("email", ""),
            "email": d.get("email", ""),
            "criado": d.get("criado", ""),
            "visto": d.get("visto", ""),
        })
    return saida


def _salvar_dispositivos():
    _gravar_json(DISPOSITIVOS_PATH, _dispositivos)


_dispositivos = _carregar_dispositivos()
_salvar_dispositivos()




def _carregar_pessoas():
    try:
        dados = json.loads(PESSOAS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [p for p in dados if isinstance(p, dict) and p.get("id")]


def _salvar_pessoas():
    _gravar_json(PESSOAS_PATH, _pessoas)


_pessoas = _carregar_pessoas()


def pedir_acesso(nome, email, app):
    nome = " ".join(str(nome or "").split())[:50]
    if len(nome) < 2:
        return None, ""
    chave = secrets.token_urlsafe(24)
    with _trava:
        pessoa = {
            "id": uuid.uuid4().hex[:8],
            "nome": nome,
            "email": str(email or "").strip().lower()[:90],
            "app": (app or "eko")[:12],
            "estado": "pendente",
            "papel": "usuario",
            "chave": _hash(chave),
            "criado": datetime.now().isoformat(timespec="seconds"),
            "aprovado": "",
        }
        _pessoas.append(pessoa)
        _salvar_pessoas()
    print(f" EKO: pedido de {nome}")
    return pessoa["id"], chave


def pedido_por_chave(pid, chave):
    for p in _pessoas:
        if p.get("id") == pid and hmac.compare_digest(
            p.get("chave", ""), _hash(str(chave or ""))
        ):
            return p
    return None


def token_da_pessoa(pessoa, app):
    segredo = secrets.token_urlsafe(32)
    agora = datetime.now().isoformat(timespec="seconds")
    with _trava:
        for d in _dispositivos:
            if d.get("pessoa") == pessoa["id"] and d.get("app") == app:
                d["hash"] = _hash(segredo)
                d["visto"] = agora
                d["nome"] = pessoa["nome"]
                d["papel"] = pessoa.get("papel", "usuario")
                _salvar_dispositivos()
                return segredo
        _dispositivos.append({
            "id": uuid.uuid4().hex[:8],
            "hash": _hash(segredo),
            "nome": pessoa["nome"],
            "email": pessoa.get("email", ""),
            "pessoa": pessoa["id"],
            "app": app,
            "papel": pessoa.get("papel", "usuario"),
            "criado": agora,
            "visto": "",
        })
        _salvar_dispositivos()
    return segredo

CADASTROS_PATH = g.caminho(CFG, "state_dir") / "cadastros.json"


def _carregar_cadastros():
    try:
        dados = json.loads(CADASTROS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in dados if isinstance(c, dict) and c.get("id")]


def _salvar_cadastros():
    CADASTROS_PATH.parent.mkdir(parents=True, exist_ok=True)
    CADASTROS_PATH.write_text(
        json.dumps(_cadastros, ensure_ascii=False, indent=1), encoding="utf-8"
    )


_cadastros = _carregar_cadastros()


def cadastros_publicos():
    with _trava:
        return [
            {
                "id": c.get("id"),
                "nome": c.get("nome", ""),
                "usuario": c.get("usuario", ""),
                "email": c.get("email", ""),
                "criado": c.get("criado", ""),
            }
            for c in _cadastros
        ]


def cadastro_novo(nome, usuario, email, senha):
    nome = (nome or "").strip()[:60]
    usuario = (usuario or "").strip()
    email = (email or "").strip()[:120]
    senha = senha or ""
    if len(nome) < 2:
        return False, "diga seu nome"
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,30}", usuario):
        return False, "usuario invalido (3-30 letras/numeros/._-)"
    if len(senha) < 6:
        return False, "a senha precisa ter pelo menos 6 caracteres"
    with _trava:
        if any(c.get("usuario", "").lower() == usuario.lower() for c in _cadastros):
            return False, "esse usuario ja esta aguardando aprovacao"
        _cadastros.append({
            "id": uuid.uuid4().hex[:8],
            "nome": nome,
            "usuario": usuario,
            "email": email,
            "senha": senha,
            "criado": datetime.now().isoformat(timespec="seconds"),
        })
        _salvar_cadastros()
    return True, ""


def criar_usuario_navidrome(usuario, nome, email, senha):
    base = str(CFG.get("navidrome_url") or "").rstrip("/")
    admin_user = CFG.get("navidrome_usuario") or "admin"
    admin_pass = CFG.get("navidrome_senha") or ""
    try:
        corpo = json.dumps({"username": admin_user, "password": admin_pass}).encode()
        req = Request(
            base + "/auth/login", data=corpo, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=20) as r:
            token = json.loads(r.read().decode("utf-8", "replace"))["token"]
        dados = json.dumps({
            "userName": usuario,
            "name": nome,
            "password": senha,
            "isAdmin": False,
            "email": email or "",
        }).encode()
        req = Request(
            base + "/api/user", data=dados, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-ND-Authorization": "Bearer " + token,
            },
        )
        with urlopen(req, timeout=20) as r:
            r.read()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        texto = ""
        if hasattr(exc, "read"):
            try:
                texto = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                texto = ""
        return False, (texto or str(exc))[:200]


CONTAS_NAV_PATH = g.caminho(CFG, "state_dir") / "contas_navidrome.json"


def _carregar_contas_nav():
    try:
        dados = json.loads(CONTAS_NAV_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [c for c in dados if isinstance(c, dict) and c.get("email")]


def _salvar_contas_nav():
    CONTAS_NAV_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTAS_NAV_PATH.write_text(
        json.dumps(_contas_nav, ensure_ascii=False, indent=1), encoding="utf-8"
    )


_contas_nav = _carregar_contas_nav()


def nome_por_usuario(usuario):
    usuario = str(usuario or "").strip()
    if not usuario:
        return ""
    email = ""
    with _trava:
        for conta in _contas_nav:
            if conta.get("usuario") == usuario:
                email = str(conta.get("email") or "").strip().lower()
                break
        if email:
            for d in _dispositivos:
                if str(d.get("email") or "").strip().lower() == email and d.get("nome"):
                    return str(d.get("nome"))
            for p in _pessoas:
                if str(p.get("email") or "").strip().lower() == email and p.get("nome"):
                    return str(p.get("nome"))
    return ""

def _nav_ping(usuario, senha):
    base = str(CFG.get("navidrome_url") or "").rstrip("/")
    params = urlencode({
        "u": usuario, "p": senha, "v": "1.16.1", "c": "eko", "f": "json",
    })
    try:
        with urlopen(f"{base}/rest/ping.view?{params}", timeout=10) as r:
            dados = json.loads(r.read().decode("utf-8", "replace"))
        return dados.get("subsonic-response", {}).get("status") == "ok"
    except Exception:  # noqa: BLE001
        return False


def garantir_usuario_navidrome(email, nome):
    email = (email or "").strip().lower()
    if not email:
        return {}
    with _trava:
        conta = next((c for c in _contas_nav if c.get("email") == email), None)
    if conta and _nav_ping(conta.get("usuario", ""), conta.get("senha", "")):
        return {
            "navidrome_url": str(CFG.get("navidrome_public_url") or CFG.get("navidrome_url") or "").rstrip("/"),
            "navidrome_user": conta["usuario"],
            "navidrome_pass": conta["senha"],
        }
    if conta:
        with _trava:
            _contas_nav.remove(conta)
            _salvar_contas_nav()

    base_user = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())[:24] or "ouvinte"
    for tentativa in range(4):
        usuario = base_user if tentativa == 0 else f"{base_user[:20]}{secrets.token_hex(2)}"
        senha = secrets.token_urlsafe(18)
        ok, _erro = criar_usuario_navidrome(
            usuario, nome or usuario, email, senha
        )
        if not ok:
            continue
        with _trava:
            _contas_nav.append({
                "email": email,
                "usuario": usuario,
                "senha": senha,
                "criado": datetime.now().isoformat(timespec="seconds"),
            })
            _salvar_contas_nav()
        return {
            "navidrome_url": str(CFG.get("navidrome_public_url") or CFG.get("navidrome_url") or "").rstrip("/"),
            "navidrome_user": usuario,
            "navidrome_pass": senha,
        }
    return {}


def aprovar_cadastro(cadastro_id, acao):
    with _trava:
        alvo = next((c for c in _cadastros if c.get("id") == cadastro_id), None)
    if not alvo:
        return False, "cadastro nao encontrado"
    if acao == "rejeitar":
        with _trava:
            _cadastros.remove(alvo)
            _salvar_cadastros()
        return True, "rejeitado"
    ok, erro = criar_usuario_navidrome(
        alvo.get("usuario", ""), alvo.get("nome", ""),
        alvo.get("email", ""), alvo.get("senha", ""),
    )
    if not ok:
        return False, erro or "nao consegui criar o usuario"
    with _trava:
        _cadastros.remove(alvo)
        _salvar_cadastros()
    return True, "aprovado"


CATALOGO_PATH = g.caminho(CFG, "state_dir") / "catalogo.jsonl"
CAPAS_DIR = g.caminho(CFG, "state_dir") / "capas"

_biblioteca = []
_biblioteca_por_id = {}
_biblioteca_versao = None


def _sem_acento(texto):
    import unicodedata

    t = unicodedata.normalize("NFKD", str(texto or ""))
    return "".join(c for c in t if not unicodedata.combining(c)).lower().strip()


def carregar_biblioteca():
    global _biblioteca, _biblioteca_por_id, _biblioteca_versao
    try:
        versao = CATALOGO_PATH.stat().st_mtime
    except OSError:
        versao = 0
    with _trava:
        if versao == _biblioteca_versao and _biblioteca:
            return _biblioteca
        por_id = {}
        linhas = []
        if CATALOGO_PATH.exists():
            try:
                linhas = CATALOGO_PATH.read_text(encoding="utf-8").splitlines()
            except OSError:
                linhas = []
        for linha in linhas:
            linha = linha.strip()
            if not linha:
                continue
            try:
                d = json.loads(linha)
            except json.JSONDecodeError:
                continue
            if d.get("status") not in (None, "ok"):
                continue
            rel = str(d.get("path") or "")
            if not rel:
                continue
            raiz = g.caminho(CFG, "music_dir").resolve()
            arquivo = (raiz / rel).resolve()
            if not str(arquivo).startswith(str(raiz) + os.sep):
                continue
            if not arquivo.is_file():
                continue
            faixa_id = str(d.get("yt_id") or arquivo.stem)
            por_id[faixa_id] = {
                "id": faixa_id,
                "artist": str(d.get("artist") or "Desconhecido"),
                "title": str(d.get("title") or arquivo.stem),
                "album": str(d.get("album") or ""),
                "duration": float(d.get("duration") or 0),
                "arquivo": rel,
                "_caminho": arquivo,
            }
        _biblioteca = sorted(
            por_id.values(),
            key=lambda f: (_sem_acento(f["artist"]), _sem_acento(f["title"])),
        )
        _biblioteca_por_id = por_id
        _biblioteca_versao = versao
        return _biblioteca


def buscar_biblioteca(consulta):
    itens = carregar_biblioteca()
    palavras = _sem_acento(consulta).split()
    if not palavras:
        return itens
    saida = []
    for f in itens:
        alvo = _sem_acento(f["artist"] + " " + f["title"] + " " + f["album"])
        if all(p in alvo for p in palavras):
            saida.append(f)
    return saida


def faixa_publica(f):
    return {
        "id": f["id"],
        "artist": f["artist"],
        "title": f["title"],
        "album": f["album"],
        "duration": f["duration"],
    }


def _nd_url(rota, **params):
    base = str(CFG.get("navidrome_url") or "").rstrip("/")
    dados = {
        "u": CFG.get("navidrome_usuario") or "admin",
        "p": CFG.get("navidrome_senha") or "",
        "v": "1.16.1",
        "c": "eko-radio",
        "f": "json",
    }
    dados.update(params)
    return f"{base}/rest/{rota}?" + urlencode(dados)


def _nd_json(rota, **params):
    try:
        with urlopen(_nd_url(rota, **params), timeout=20) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}


def _nd_buscar(titulo, artista):
    consulta = f"{artista} {titulo}".strip()
    if not consulta:
        return None
    dados = _nd_json(
        "search3.view", query=consulta, songCount=1, albumCount=0, artistCount=0
    )
    songs = (
        dados.get("subsonic-response", {})
        .get("searchResult3", {})
        .get("song", [])
    )
    return songs[0] if songs else None


def _nd_similares(nd_id, quantidade=20):
    dados = _nd_json("getSimilarSongs2.view", id=nd_id, count=quantidade)
    return (
        dados.get("subsonic-response", {})
        .get("similarSongs2", {})
        .get("song", [])
    )


def _radio_faixa(s):
    sid = str(s.get("id") or "")
    return {
        "id": "nd:" + sid,
        "nd_id": sid,
        "title": s.get("title") or "Sem titulo",
        "artist": s.get("artist") or "",
        "album": s.get("album") or "",
        "duration": float(s.get("duration") or 0),
        "url": "/api/nd/stream?id=" + quote(sid),
        "capa": "/api/nd/capa?id=" + quote(sid),
    }


def autorizados():
    return [str(x).strip().lower() for x in (CFG.get("autorizados") or []) if str(x).strip()]


def _salvar_autorizados(lista):
    CFG["autorizados"] = lista
    try:
        caminho = ROOT / "config.json"
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        dados["autorizados"] = lista
        caminho.write_text(
            json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except (OSError, json.JSONDecodeError):
        pass


def _adicionar_autorizado(email):
    email = (email or "").strip().lower()
    if not email:
        return
    lista = autorizados()
    if email in lista:
        return
    lista.append(email)
    _salvar_autorizados(lista)


def _remover_autorizado(email):
    email = (email or "").strip().lower()
    if not email:
        return
    lista = [x for x in autorizados() if x != email]
    _salvar_autorizados(lista)


def _achar_por_hash(h):
    for d in _dispositivos:
        if hmac.compare_digest(d.get("hash", ""), h):
            return d
    return None


def _marcar_visto(dev):
    agora = datetime.now()
    try:
        antes = datetime.fromisoformat(dev["visto"])
    except (KeyError, ValueError):
        antes = datetime.min
    if (agora - antes).total_seconds() < 15 * 60:
        return
    with _trava:
        dev["visto"] = agora.isoformat(timespec="seconds")
        _salvar_dispositivos()


def verificar_google(jwt):
    from google.auth.transport import requests as google_requests
    from google.oauth2 import id_token as google_id_token

    return google_id_token.verify_oauth2_token(
        jwt, google_requests.Request(), GOOGLE_CLIENT_ID
    )


def entrar_google(jwt, nonce):
    if not GOOGLE_CLIENT_ID:
        return None, "login Google nao configurado no servidor"
    try:
        info = verificar_google(jwt or "")
    except Exception as erro:  # noqa: BLE001
        return None, f"login do Google invalido ({erro})"
    email = str(info.get("email") or "").strip().lower()
    if not email:
        return None, "o Google nao mandou e-mail"
    if not info.get("email_verified"):
        return None, "e-mail nao verificado pelo Google"
    nonce_token = str(info.get("nonce") or "")
    if nonce_token:
        if not nonce or hashlib.sha256(nonce.encode()).hexdigest() != nonce_token:
            return None, "nonce nao confere"

    nome = str(info.get("name") or email.split("@")[0])[:50]

    if email in autorizados():
        segredo = secrets.token_urlsafe(32)
        agora = datetime.now().isoformat(timespec="seconds")
        with _trava:
            dev = next(
                (d for d in _dispositivos
                 if d.get("dono") and (d.get("email") or "") == email),
                None,
            )
            if dev is None:
                dev = {
                    "id": uuid.uuid4().hex[:8], "nome": nome, "email": email,
                    "app": "eko", "dono": True, "criado": agora, "visto": "",
                }
                _dispositivos.append(dev)
            dev["hash"] = _hash(segredo)
            dev["nome"] = nome
            dev["email"] = email
            dev["dono"] = True
            dev["papel"] = "admin"
            dev["visto"] = agora
            _salvar_dispositivos()
        print(f" EKO: dono entrou ({email})")
        return {
            "token": segredo, "nome": nome, "email": email, "dono": True,
            **garantir_usuario_navidrome(email, nome),
        }, ""

    aprovada = next(
        (p for p in _pessoas if p.get("email") == email and p.get("estado") == "aprovado"),
        None,
    )
    if aprovada is not None:
        return {
            "token": token_da_pessoa(aprovada, "eko"),
            "nome": aprovada.get("nome", nome),
            "email": email,
            "dono": False,
            **garantir_usuario_navidrome(email, aprovada.get("nome", nome)),
        }, ""

    chave = secrets.token_urlsafe(24)
    with _trava:
        pendente = next(
            (p for p in _pessoas if p.get("email") == email and p.get("estado") == "pendente"),
            None,
        )
        if pendente is not None:
            pendente["chave"] = _hash(chave)
            pendente["nome"] = nome
            pid = pendente["id"]
        else:
            pid = uuid.uuid4().hex[:8]
            _pessoas.append({
                "id": pid,
                "nome": nome,
                "email": email,
                "app": "eko",
                "estado": "pendente",
                "chave": _hash(chave),
                "criado": datetime.now().isoformat(timespec="seconds"),
                "aprovado": "",
            })
        _salvar_pessoas()
    print(f" EKO: pedido de {nome} ({email})")
    return {"pendente": True, "id": pid, "chave": chave, "nome": nome}, ""


def _novo_segredo(dev):
    segredo = secrets.token_urlsafe(32)
    with _trava:
        dev["hash"] = _hash(segredo)
        dev["visto"] = datetime.now().isoformat(timespec="seconds")
        _salvar_dispositivos()
    return segredo


def dispositivo_por_token(enviado):
    if not enviado:
        return None
    return _achar_por_hash(_hash(enviado))


def papel_do_token(enviado):
    if not enviado:
        return None
    if TOKEN and hmac.compare_digest(enviado, TOKEN):
        return "root"
    dev = _achar_por_hash(_hash(enviado))
    if not dev:
        return None
    if dev.get("papel") == "admin" or dev.get("dono"):
        return "admin"
    pessoa = next((p for p in _pessoas if p.get("id") == dev.get("pessoa")), None)
    if pessoa and pessoa.get("papel") == "admin":
        return "admin"
    return "usuario"


def _dono(enviado):
    return papel_do_token(enviado) in ("root", "admin")


def _ha_admin():
    if autorizados():
        return True
    if any(d.get("dono") or d.get("papel") == "admin" for d in _dispositivos):
        return True
    return any(p.get("papel") == "admin" for p in _pessoas)


def token_valido(enviado):
    if not enviado:
        return not TOKEN and not _dispositivos
    if TOKEN and hmac.compare_digest(enviado, TOKEN):
        return True
    dev = _achar_por_hash(_hash(enviado))
    if dev:
        _marcar_visto(dev)
        return True
    return False


def token_enviado(handler, query):
    cabecalho = handler.headers.get("X-Token")
    if cabecalho:
        return cabecalho
    rota = urlparse(getattr(handler, "path", "")).path
    if handler.command == "GET" and rota in ("/api/arquivo", "/api/faixa"):
        return (query.get("t") or [""])[0]
    return ""


def _host_permitido(url):
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    dominios = ("youtube.com", "youtu.be", "tiktok.com", "instagram.com")
    return any(host == d or host.endswith("." + d) for d in dominios)


def fila_publica():
    saida = []
    for j in novidades():
        arquivo = j.get("arquivo") or ""
        saida.append({
            "id": j.get("id"),
            "url": j.get("url"),
            "titulo": j.get("titulo"),
            "modo": j.get("modo"),
            "destino": j.get("destino"),
            "status": j.get("status"),
            "resumo": j.get("resumo"),
            "criado": j.get("criado"),
            "terminado": j.get("terminado"),
            "arquivo": Path(arquivo).name if arquivo else "",
        })
    return saida


def adicionar(urls, origem="", modo="audio", destino="servidor"):
    criados = []
    with _trava:
        pendentes = {j["url"] for j in _fila if j["status"] in ("na_fila", "baixando")}
        for url in urls:
            url = (url or "").strip()
            if not re.match(r"^https?://", url, re.I):
                continue
            if not _host_permitido(url):
                continue
            if url in pendentes:
                continue
            if len(criados) >= 10:
                break
            job = {
                "id": uuid.uuid4().hex[:8],
                "url": url,
                "titulo": "",
                "origem": origem[:40],
                "modo": "video" if modo == "video" else "audio",
                "destino": "telefone" if destino == "telefone" else "servidor",
                "arquivo": "",
                "status": "na_fila",
                "criado": datetime.now().isoformat(timespec="seconds"),
                "iniciado": "", "terminado": "", "resumo": "", "log": "",
            }
            if job["modo"] == "video":
                job["destino"] = "telefone"
            _fila.append(job)
            criados.append(job)
            pendentes.add(url)
        if criados:
            salvar_fila()
            _avisar.notify_all()
    return criados


def trabalhar():
    global _ultima_origem
    while True:
        with _trava:
            fila = [j for j in _fila if j["status"] == "na_fila"]
            if not fila:
                _avisar.wait(timeout=2)
                continue
            outros = [j for j in fila if (j.get("origem") or "") != _ultima_origem]
            job = (outros or fila)[0]
            _ultima_origem = job.get("origem") or ""
            job["status"] = "baixando"
            job["iniciado"] = datetime.now().isoformat(timespec="seconds")
            job["log"] = str(LOG_DIR / f"job-{job['id']}.log")
            salvar_fila()
        if "list=" in job["url"]:
            with _trava:
                job["resumo"] = "separando faixas da playlist..."
                salvar_fila()
            itens, titulo_playlist = expandir_url(job["url"])
            if len(itens) > 1:
                with _trava:
                    if titulo_playlist:
                        job["titulo"] = titulo_playlist
                    salvar_fila()
                criados, ja = criar_filhos(itens, job)
                with _trava:
                    job["status"] = "ok"
                    job["resumo"] = (f"{criados} faixas na fila"
                                     + (f" Â· {ja} ja no acervo" if ja else ""))
                    job["terminado"] = datetime.now().isoformat(timespec="seconds")
                    salvar_fila()
                continue
        host = (urlparse(job["url"]).netloc or "").lower()
        if "tiktok." in host or "instagram." in host:
            with _trava:
                job["resumo"] = "procurando no YouTube..."
                salvar_fila()
            achado = resolver_link(job["url"])
            if not achado:
                with _trava:
                    job["status"] = "erro"
                    job["resumo"] = "nao achei essa musica no YouTube"
                    job["terminado"] = datetime.now().isoformat(timespec="seconds")
                    salvar_fila()
                continue
            with _trava:
                job["origem_link"] = job["url"]
                job["url"] = achado["url"]
                job["titulo"] = achado.get("titulo") or ""
                job["resumo"] = "achado no YouTube"
                salvar_fila()

        try:
            saida_path = Path(job["log"])
            if job.get("modo") == "video":
                comando = [sys.executable, str(ROOT / "garimpar.py"),
                           "--video", job["url"]]
            else:
                comando = [sys.executable, str(ROOT / "garimpar.py"), job["url"]]
            with saida_path.open("w", encoding="utf-8", errors="replace") as saida:
                proc = subprocess.run(
                    comando,
                    stdout=saida, stderr=subprocess.STDOUT,
                    cwd=str(ROOT), timeout=60 * 60 * 6,
                )
            texto = saida_path.read_text(encoding="utf-8", errors="replace")
            achado_dup = re.search(r"DUP_ARQUIVO: (.+)", texto)
            if achado_dup and job.get("modo") != "video":
                caminho_dup = achado_dup.group(1).strip()
                with _trava:
                    job["arquivo"] = caminho_dup
                    salvar_fila()
                if job.get("origem"):
                    _anexar_downloads(job["origem"], caminho_dup)
            if job.get("modo") == "video":
                achado_arq = re.search(r"ARQUIVO: (.+)", texto)
                with _trava:
                    if proc.returncode == 0 and achado_arq:
                        job["arquivo"] = achado_arq.group(1).strip()
                        job["status"] = "pronto"
                        job["resumo"] = "video pronto para o telefone"
                    else:
                        job["status"] = "erro"
                        job["resumo"] = _ultimo_erro(texto)
                    job["terminado"] = datetime.now().isoformat(timespec="seconds")
                    salvar_fila()
                continue
            achado = re.search(r"Resumo: (.+)", texto)
            with _trava:
                job["resumo"] = achado.group(1).strip() if achado else ""
                if proc.returncode == 0 and achado:
                    job["status"] = "ok"
                elif proc.returncode == 0:
                    job["status"] = "ok"
                    job["resumo"] = job["resumo"] or "concluido"
                else:
                    job["status"] = "erro"
                    job["resumo"] = job["resumo"] or _ultimo_erro(texto)
                job["terminado"] = datetime.now().isoformat(timespec="seconds")
                salvar_fila()
            if job.get("status") == "ok":
                arq, titulo = caminho_do_acervo(job["url"])
                with _trava:
                    job["arquivo"] = arq
                    if titulo and not job.get("titulo"):
                        job["titulo"] = titulo
                    salvar_fila()
                if job.get("pai") and arq:
                    _anexar_na_playlist(job["pai"], arq)
                if arq:
                    _anexar_downloads(job.get("origem"), arq)
                    sincronizar_mini()
                escanear_navidrome()
        except subprocess.TimeoutExpired:
            with _trava:
                job["status"] = "erro"
                job["resumo"] = "tempo esgotado"
                job["terminado"] = datetime.now().isoformat(timespec="seconds")
                salvar_fila()
        except Exception as exc:
            with _trava:
                job["status"] = "erro"
                job["resumo"] = str(exc)[:300]
                job["terminado"] = datetime.now().isoformat(timespec="seconds")
                salvar_fila()


def resolver_link(url):
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "garimpar.py"), "--procurar", url],
            capture_output=True, text=True, timeout=180, cwd=str(ROOT),
            encoding="utf-8", errors="replace",
        )
        dados = json.loads(proc.stdout or "{}")
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return None
    if not isinstance(dados, dict) or not dados.get("encontrado"):
        return None
    return dados if dados.get("url") else None


def caminho_do_acervo(url):
    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{8,})", url)
    if not m:
        return "", ""
    alvo = m.group(1)
    cat = g.caminho(CFG, "state_dir") / "catalogo.jsonl"
    if not cat.exists():
        return "", ""
    for linha in reversed(cat.read_text(encoding="utf-8").splitlines()):
        try:
            d = json.loads(linha)
        except json.JSONDecodeError:
            continue
        if d.get("yt_id") != alvo:
            continue
        rel = d.get("path") or ""
        p = g.caminho(CFG, "music_dir") / rel
        titulo = " - ".join(x for x in (d.get("artist"), d.get("title")) if x)
        return (str(p) if p.exists() else ""), titulo
    return "", ""


def _ultimo_erro(texto):
    linhas = [l for l in texto.splitlines() if "ERROR" in l or "FALHOU" in l]
    return (linhas[-1][:300] if linhas else "falhou")


def expandir_url(url):
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "garimpar.py"), "--listar", url],
            capture_output=True, text=True, timeout=300, cwd=str(ROOT),
            encoding="utf-8", errors="replace",
        )
        dados = json.loads(proc.stdout or "{}")
        if isinstance(dados, dict):
            itens = dados.get("itens") or []
            titulo = str(dados.get("titulo") or "")
            return (itens if isinstance(itens, list) else []), titulo
        if isinstance(dados, list):
            return dados, ""
        return [], ""
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return [], ""


def _anexar_na_playlist(pai_id, arquivo):
    pai = next((j for j in _fila if j.get("id") == pai_id), None)
    if not pai:
        return
    nome = re.sub(r'[\\/:*?"<>|]+', " ", str(pai.get("titulo") or "")).strip()[:80]
    if not nome:
        return
    pasta = g.caminho(CFG, "music_dir")
    try:
        rel = Path(arquivo).resolve().relative_to(pasta.resolve())
    except (OSError, ValueError):
        return
    destino = pasta / "_playlists"
    destino.mkdir(parents=True, exist_ok=True)
    m3u = destino / f"{nome}.m3u"
    linha = os.path.relpath(Path(arquivo).resolve(), destino.resolve()).replace("\\", "/")
    try:
        with _trava:
            novo = not m3u.exists()
            if not novo and linha in m3u.read_text(encoding="utf-8").splitlines():
                return
            with m3u.open("a", encoding="utf-8") as f:
                if novo:
                    f.write("#EXTM3U\n")
                f.write(linha + "\n")
    except OSError:
        pass


def _anexar_downloads(nome, arquivo):
    nome = re.sub(r'[\\/:*?"<>|]+', " ", str(nome or "")).strip()[:60]
    if not nome or nome in ("post", "get"):
        return
    pasta = g.caminho(CFG, "music_dir")
    try:
        rel = Path(arquivo).resolve().relative_to(pasta.resolve())
    except (OSError, ValueError):
        return
    destino = pasta / "_playlists"
    destino.mkdir(parents=True, exist_ok=True)
    m3u = destino / f"Baixadas - {nome}.m3u"
    linha = os.path.relpath(Path(arquivo).resolve(), destino.resolve()).replace("\\", "/")
    try:
        with _trava:
            novo = not m3u.exists()
            if not novo and linha in m3u.read_text(encoding="utf-8").splitlines():
                return
            with m3u.open("a", encoding="utf-8") as f:
                if novo:
                    f.write("#EXTM3U\n")
                f.write(linha + "\n")
    except OSError:
        pass


def criar_filhos(itens, pai):
    bib = g.Biblioteca(CFG)
    criados, ja_tinha = 0, 0
    with _trava:
        pendentes = {j["url"] for j in _fila if j["status"] in ("na_fila", "baixando")}
        for it in itens[:500]:
            yt_id = it.get("id") or ""
            if not yt_id:
                continue
            url = "https://www.youtube.com/watch?v=" + yt_id
            if bib.ja_existe_id(yt_id) or url in pendentes:
                ja_tinha += 1
                existente = bib.por_id.get(yt_id) or {}
                rel = existente.get("path")
                if rel:
                    caminho_existente = g.caminho(CFG, "music_dir") / rel
                    if caminho_existente.is_file():
                        _anexar_na_playlist(pai["id"], str(caminho_existente))
                        _anexar_downloads(pai.get("origem"), str(caminho_existente))
                continue
            _fila.append({
                "id": uuid.uuid4().hex[:8], "url": url,
                "titulo": it.get("title") or "", "origem": pai.get("origem", ""),
                "pai": pai["id"], "status": "na_fila",
                "modo": "audio", "destino": "servidor", "arquivo": "",
                "criado": datetime.now().isoformat(timespec="seconds"),
                "iniciado": "", "terminado": "", "resumo": "", "log": "",
            })
            pendentes.add(url)
            criados += 1
        salvar_fila()
        _avisar.notify_all()
    return criados, ja_tinha


def token_ok(handler, query):
    return token_valido(token_enviado(handler, query))


def dono_ok(handler, query):
    enviado = token_enviado(handler, query)
    return bool(TOKEN) and hmac.compare_digest(enviado, TOKEN)


MANIFEST_TOCAR = {
    "name": "EKO Tocar", "short_name": "EKO Tocar", "start_url": "/tocar",
    "scope": "/", "display": "standalone", "orientation": "portrait",
    "background_color": "#101114", "theme_color": "#101114",
    "icons": [{"src": "/icone.svg", "sizes": "any", "type": "image/svg+xml"}],
}

PAGINA_TOCAR = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101114">
<title>EKO Tocar</title>
<link rel="manifest" href="/tocar.webmanifest">
<link rel="icon" href="/icone.svg" type="image/svg+xml">
<style>
:root{color-scheme:dark;--azul:#3d8bff;--fundo:#101114;--card:#191b20;--borda:#24272e;--texto:#e8e8ea;--fraco:#8a8d96}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--fundo);color:var(--texto);
 font:15px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
body{padding-bottom:calc(150px + env(safe-area-inset-bottom))}
header{position:sticky;top:0;z-index:5;background:rgba(16,17,20,.94);
 backdrop-filter:blur(14px);padding:calc(14px + env(safe-area-inset-top)) 16px 10px;
 border-bottom:1px solid var(--borda)}
.titulo{display:flex;align-items:baseline;gap:10px}
h1{font-size:21px;margin:0;letter-spacing:.3px}
#total{color:var(--fraco);font-size:12px}
.acoes{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 10px}
.acoes button{flex:1;padding:11px;border:0;border-radius:12px;background:var(--azul);
 color:#fff;font-weight:600;font-size:14px}
.acoes button.sec{background:#23262d;color:#c9ccd4;font-weight:500}
.acoes button[aria-pressed="true"]{background:#12351f;color:#5bd68a}
#radio{flex-basis:100%;background:#2a1f3d;color:#d9c2ff}
#busca{width:100%;background:var(--card);border:1px solid #2a2d35;border-radius:12px;
 color:var(--texto);padding:11px 13px;font-size:14px}
#busca:focus{outline:none;border-color:var(--azul)}
main{padding:10px 10px 0}
.faixa{display:flex;align-items:center;gap:11px;width:100%;text-align:left;
 background:transparent;border:0;border-radius:12px;padding:8px;color:inherit}
.faixa:active{background:#191b20}
.faixa.ativo{background:#1b2536}
.capa{width:46px;height:46px;border-radius:10px;flex:0 0 46px;display:flex;
 align-items:center;justify-content:center;font-weight:700;font-size:18px;color:#fff}
.texto{flex:1;min-width:0}
.texto b{display:block;font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.texto small{display:block;color:var(--fraco);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tempo{color:#6a6d76;font-size:12px;font-variant-numeric:tabular-nums}
.vazio{color:#6a6d76;font-size:13px;text-align:center;padding:40px 16px;line-height:1.7}
.login{position:fixed;inset:0;z-index:20;display:none;align-items:center;justify-content:center;
 background:rgba(8,9,11,.92);padding:24px}
.login.aberto{display:flex}
.cartao{width:100%;max-width:360px;background:var(--card);border:1px solid var(--borda);
 border-radius:22px;padding:24px 20px;text-align:center}
.cartao h2{font-size:20px;margin:0 0 6px}
.cartao p{color:var(--fraco);font-size:13px;margin:0 0 16px}
.cartao input{width:100%;background:#101114;border:1px solid #2a2d35;border-radius:12px;
 color:var(--texto);padding:12px;font-size:15px;margin-bottom:10px;text-align:center}
.cartao button{width:100%;padding:13px;border:0;border-radius:12px;background:var(--azul);
 color:#fff;font-weight:600;font-size:15px}
.player{position:fixed;left:0;right:0;bottom:0;z-index:10;background:rgba(25,27,32,.97);
 backdrop-filter:blur(18px);border-top:1px solid #2a2d35;
 padding:0 14px calc(10px + env(safe-area-inset-bottom));
 transform:translateY(110%);transition:transform .22s ease}
.player.ativo{transform:translateY(0)}
#progresso{width:100%;margin:0;accent-color:var(--azul);height:22px}
.progresso{display:flex;align-items:center;gap:8px}
.progresso input{flex:1}
.tempos{display:flex;justify-content:space-between;color:#6a6d76;font-size:11px;
 font-variant-numeric:tabular-nums;margin-top:-4px}
.linha{display:flex;align-items:center;gap:10px;margin-top:2px}
#capa{width:48px;height:48px;border-radius:11px;object-fit:cover;background:#23262d;flex:0 0 48px}
.info{flex:1;min-width:0}
.info b{display:block;font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.info span{display:block;color:var(--fraco);font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.controles{display:flex;align-items:center;gap:2px}
.controles button{background:transparent;border:0;color:var(--texto);font-size:20px;
 width:38px;height:42px;border-radius:50%;display:flex;align-items:center;justify-content:center}
.controles button:active{background:#2a2d35}
#play{background:var(--azul);color:#fff;width:44px;height:44px;font-size:18px}
</style></head><body>

<header>
  <div class="titulo"><h1>EKO</h1><span id="total">biblioteca</span></div>
  <div class="acoes">
    <button id="tocarTudo">Tocar tudo</button>
    <button id="aleatorio" class="sec" aria-pressed="false">AleatÃ³rio</button>
    <button id="radio">RÃ¡dio inteligente</button>
  </div>
  <input id="busca" type="search" placeholder="Buscar mÃºsica, artista ou Ã¡lbum" autocomplete="off">
</header>

<main id="lista"><div class="vazio">Carregando...</div></main>

<div class="login" id="login">
  <div class="cartao">
    <h2>Entrar</h2>
    <p>Digite a senha do servidor EKO para ouvir a biblioteca.</p>
    <input id="senha" type="password" placeholder="senha do servidor" autocomplete="current-password">
    <button id="entrar">Entrar</button>
  </div>
</div>

<div class="player" id="player">
  <div class="progresso">
    <input id="progresso" type="range" min="0" max="1000" value="0" step="1" aria-label="posiÃ§Ã£o">
  </div>
  <div class="tempos"><span id="atual">0:00</span><span id="duracao">0:00</span></div>
  <div class="linha">
    <img id="capa" alt="">
    <div class="info"><b id="nome">â€”</b><span id="artista"></span></div>
    <div class="controles">
      <button id="anterior" aria-label="anterior">â®</button>
      <button id="play" aria-label="tocar">â–¶</button>
      <button id="proxima" aria-label="prÃ³xima">â­</button>
    </div>
  </div>
</div>

<audio id="audio" preload="metadata"></audio>
<script>
const CHAVE = 'garimpo_token';
const $ = (id) => document.getElementById(id);
const audio = $('audio');
let token = localStorage.getItem(CHAVE) || '';
const paramsIniciais = new URLSearchParams(location.search);
const daUrl = paramsIniciais.get('t');
const autoRadio = paramsIniciais.get('radio') === '1';
if (daUrl) {
  token = daUrl;
  localStorage.setItem(CHAVE, token);
  history.replaceState(null, '', autoRadio ? '/tocar?radio=1' : '/tocar');
}
let faixas = [];
let fila = [];
let idx = -1;
let atualId = '';
let aleatorio = false;
let repetir = true;
let radioAtivo = false;

function esc(t) {
  return String(t == null ? '' : t).replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmt(s) {
  s = Math.max(0, Math.floor(s || 0));
  return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
}
function cor(id) {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) % 360;
  return 'linear-gradient(135deg,hsl(' + h + ',55%,38%),hsl(' + ((h + 45) % 360) + ',60%,24%))';
}
function capaUrl(id) {
  return '/api/capa?id=' + encodeURIComponent(id) + (token ? '&t=' + encodeURIComponent(token) : '');
}
function faixaUrl(id) {
  return '/api/faixa?id=' + encodeURIComponent(id) + (token ? '&t=' + encodeURIComponent(token) : '');
}
function urlDe(f) {
  return f.url ? f.url + (token ? '&t=' + encodeURIComponent(token) : '') : faixaUrl(f.id);
}
function capaDe(f) {
  return f.capa ? f.capa + (token ? '&t=' + encodeURIComponent(token) : '') : capaUrl(f.id);
}
function mostrarLogin(aberto) {
  $('login').classList.toggle('aberto', aberto);
  if (aberto) setTimeout(() => $('senha').focus(), 80);
}

async function carregar() {
  if (!token) { mostrarLogin(true); return; }
  $('lista').innerHTML = '<div class="vazio">Carregando biblioteca...</div>';
  try {
    const r = await fetch('/api/biblioteca', {headers: {'X-Token': token}});
    if (r.status === 401) {
      token = '';
      localStorage.removeItem(CHAVE);
      mostrarLogin(true);
      $('lista').innerHTML = '<div class="vazio">Informe a senha para ver a biblioteca.</div>';
      return;
    }
    if (!r.ok) throw new Error('erro ' + r.status);
    const j = await r.json();
    faixas = j.faixas || [];
    $('total').textContent = faixas.length + ' faixas';
    mostrarLogin(false);
    render();
    if (autoRadio && faixas.length) setTimeout(() => iniciarRadio(), 500);
  } catch (e) {
    $('lista').innerHTML = '<div class="vazio">NÃ£o consegui falar com o servidor.<br>' + esc(e.message) + '</div>';
  }
}

function render() {
  const q = $('busca').value.trim().toLowerCase();
  const lista = q
    ? faixas.filter(f => (f.artist + ' ' + f.title + ' ' + f.album).toLowerCase().includes(q))
    : faixas;
  if (!faixas.length) {
    $('lista').innerHTML = '<div class="vazio">A biblioteca estÃ¡ vazia.<br>Baixe algo primeiro.</div>';
    return;
  }
  if (!lista.length) {
    $('lista').innerHTML = '<div class="vazio">Nada encontrado para â€œ' + esc(q) + 'â€.</div>';
    return;
  }
  $('lista').innerHTML = lista.map((f, i) => {
    const inicial = (f.artist || f.title || '?').trim().charAt(0).toUpperCase();
    const ativo = f.id === atualId ? ' ativo' : '';
    return '<button class="faixa' + ativo + '" data-i="' + i + '">' +
      '<span class="capa" style="background:' + cor(f.id) + '">' + esc(inicial) + '</span>' +
      '<span class="texto"><b>' + esc(f.title) + '</b><small>' +
      esc(f.artist) + (f.album ? ' Â· ' + esc(f.album) : '') + '</small></span>' +
      '<span class="tempo">' + fmt(f.duration) + '</span></button>';
  }).join('');
  document.querySelectorAll('.faixa').forEach(el => {
    el.onclick = () => tocarLista(lista, Number(el.dataset.i));
  });
}

function sessao(f) {
  if (!('mediaSession' in navigator)) return;
  navigator.mediaSession.metadata = new MediaMetadata({
    title: f.title, artist: f.artist, album: f.album,
    artwork: [{src: capaDe(f), sizes: '480x360', type: 'image/jpeg'}]
  });
}

function tocar(f) {
  if (!f) return;
  atualId = f.id;
  audio.src = urlDe(f);
  $('nome').textContent = f.title;
  $('artista').textContent = f.artist + (f.album ? ' Â· ' + f.album : '');
  const img = $('capa');
  img.src = capaDe(f);
  img.onerror = () => { img.style.visibility = 'hidden'; };
  img.onload = () => { img.style.visibility = 'visible'; };
  $('player').classList.add('ativo');
  sessao(f);
  const p = audio.play();
  if (p && p.catch) p.catch(() => {});
  document.querySelectorAll('.faixa').forEach(el => el.classList.remove('ativo'));
  render();
}

function tocarLista(lista, i) {
  radioAtivo = false;
  $('radio').textContent = 'RÃ¡dio inteligente';
  fila = lista.slice();
  idx = i;
  tocar(fila[idx]);
}

async function carregarMaisRadio() {
  const ultimo = fila[fila.length - 1];
  if (!ultimo) return false;
  const url = ultimo.nd_id
    ? '/api/radio?seed=' + encodeURIComponent(ultimo.nd_id) + '&n=20'
    : '/api/radio?titulo=' + encodeURIComponent(ultimo.title) + '&artista=' + encodeURIComponent(ultimo.artist) + '&n=20';
  try {
    const r = await fetch(url, {headers: {'X-Token': token}});
    if (!r.ok) return false;
    const j = await r.json();
    const novos = (j.faixas || []).filter(f => !fila.some(x => x.id === f.id));
    if (!novos.length) return false;
    fila = fila.concat(novos);
    return true;
  } catch (e) {
    return false;
  }
}

async function iniciarRadio() {
  const base = fila[idx] || faixas[0];
  if (!base) return;
  radioAtivo = true;
  aleatorio = false;
  $('aleatorio').setAttribute('aria-pressed', 'false');
  $('radio').textContent = 'RÃ¡dio ligada';
  try {
    const r = await fetch('/api/radio?titulo=' + encodeURIComponent(base.title) + '&artista=' + encodeURIComponent(base.artist) + '&n=20', {headers: {'X-Token': token}});
    if (!r.ok) throw new Error('erro ' + r.status);
    const j = await r.json();
    if (j.faixas && j.faixas.length) {
      fila = j.faixas;
      idx = 0;
      tocar(fila[0]);
      return;
    }
    throw new Error('sem similares');
  } catch (e) {
    fila = faixas.slice().sort(() => Math.random() - 0.5);
    idx = 0;
    if (fila.length) tocar(fila[0]);
  }
}

async function proxima() {
  if (!fila.length) return;
  if (radioAtivo && idx >= fila.length - 2) {
    await carregarMaisRadio();
  }
  if (aleatorio) {
    let n = idx;
    while (fila.length > 1 && n === idx) n = Math.floor(Math.random() * fila.length);
    idx = n;
  } else if (idx + 1 < fila.length) {
    idx += 1;
  } else if (repetir) {
    idx = 0;
  } else {
    audio.pause();
    return;
  }
  tocar(fila[idx]);
}

function anterior() {
  if (!fila.length) return;
  if (audio.currentTime > 3) { audio.currentTime = 0; return; }
  if (idx > 0) idx -= 1;
  else if (repetir) idx = fila.length - 1;
  tocar(fila[idx]);
}

function pintarPlay() {
  $('play').textContent = audio.paused ? 'â–¶' : 'â¸';
  $('play').setAttribute('aria-label', audio.paused ? 'tocar' : 'pausar');
  if ('mediaSession' in navigator) {
    navigator.mediaSession.playbackState = audio.paused ? 'paused' : 'playing';
  }
}

audio.addEventListener('play', pintarPlay);
audio.addEventListener('pause', pintarPlay);
audio.addEventListener('ended', () => { proxima(); });
audio.addEventListener('loadedmetadata', () => {
  $('duracao').textContent = fmt(audio.duration);
  $('progresso').value = 0;
});
audio.addEventListener('timeupdate', () => {
  $('atual').textContent = fmt(audio.currentTime);
  if (audio.duration) {
    $('progresso').value = Math.round((audio.currentTime / audio.duration) * 1000);
    if ('mediaSession' in navigator && navigator.mediaSession.setPositionState) {
      try {
        navigator.mediaSession.setPositionState({
          duration: audio.duration, playbackRate: audio.playbackRate,
          position: audio.currentTime
        });
      } catch (e) {}
    }
  }
});
audio.addEventListener('error', () => {
  $('artista').textContent = 'nÃ£o consegui tocar esta faixa';
});

$('progresso').addEventListener('input', () => {
  if (audio.duration) audio.currentTime = ($('progresso').value / 1000) * audio.duration;
});
$('play').onclick = () => {
  if (!audio.src) { if (faixas.length) tocarLista(faixas, 0); return; }
  if (audio.paused) audio.play().catch(() => {});
  else audio.pause();
};
$('proxima').onclick = proxima;
$('anterior').onclick = anterior;
$('busca').addEventListener('input', render);
$('tocarTudo').onclick = () => {
  if (!faixas.length) return;
  aleatorio = false;
  $('aleatorio').setAttribute('aria-pressed', 'false');
  const q = $('busca').value.trim().toLowerCase();
  const lista = q
    ? faixas.filter(f => (f.artist + ' ' + f.title + ' ' + f.album).toLowerCase().includes(q))
    : faixas;
  if (lista.length) tocarLista(lista, 0);
};
$('aleatorio').onclick = () => {
  aleatorio = !aleatorio;
  $('aleatorio').setAttribute('aria-pressed', aleatorio ? 'true' : 'false');
  if (aleatorio && faixas.length) {
    const i = Math.floor(Math.random() * faixas.length);
    tocarLista(faixas, i);
  }
};
$('radio').onclick = () => { iniciarRadio(); };
$('entrar').onclick = () => {
  const v = $('senha').value.trim();
  if (!v) return;
  token = v;
  localStorage.setItem(CHAVE, v);
  $('senha').value = '';
  carregar();
};
$('senha').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('entrar').click(); });

if ('mediaSession' in navigator) {
  navigator.mediaSession.setActionHandler('play', () => audio.play().catch(() => {}));
  navigator.mediaSession.setActionHandler('pause', () => audio.pause());
  navigator.mediaSession.setActionHandler('previoustrack', anterior);
  navigator.mediaSession.setActionHandler('nexttrack', proxima);
  try {
    navigator.mediaSession.setActionHandler('seekto', (d) => {
      if (d.seekTime != null) audio.currentTime = d.seekTime;
    });
  } catch (e) {}
}

carregar();
</script>
</body></html>
"""


def _pagina_instalar():
    dados = _versao()
    nome = str(dados.get("version_name") or "")
    texto = f"versao {nome}" if nome else ""
    return PAGINA_INICIO.replace("{{VERSAO}}", texto)


NOTICIAS_AUDIO = ROOT / "noticias_audio"


def _noticias_indice() -> dict:
    arquivo = NOTICIAS_AUDIO / "indice.json"
    try:
        return json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _noticias_indice_gravar(dados: dict) -> None:
    try:
        NOTICIAS_AUDIO.mkdir(exist_ok=True)
        (NOTICIAS_AUDIO / "indice.json").write_text(
            json.dumps(dados, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass


def _noticias_lista(categoria: str, cidade: str) -> dict:
    import noticias as motor

    brutas = motor.carregar(categoria, cidade)
    escolhidas = motor.curar(brutas, 8)
    indice = _noticias_indice()
    itens = []
    for noticia in escolhidas:
        ident = hashlib.sha1(noticia.link.encode("utf-8")).hexdigest()[:14]
        manchete = noticia.manchete or noticia.titulo
        fala = noticia.fala or ""
        indice[ident] = {
            "manchete": manchete,
            "fala": fala,
            "fonte": noticia.fonte,
            "link": noticia.link,
        }
        itens.append({
            "id": ident,
            "manchete": manchete,
            "fala": fala,
            "fonte": noticia.fonte,
            "link": noticia.link,
            "quando": noticia.quando,
        })
    if itens:
        _noticias_indice_gravar(indice)
    return {"categoria": categoria, "cidade": cidade, "itens": itens}


def _noticia_audio(ident: str):
    dados = _noticias_indice().get(ident)
    if not dados:
        return None
    destino = NOTICIAS_AUDIO / f"{ident}.mp3"
    if destino.is_file() and destino.stat().st_size > 1000:
        return destino
    import noticias as motor

    texto = motor.para_fala(
        f"{dados.get('manchete', '')}. {dados.get('fala', '')}"
    )
    try:
        ok = asyncio.run(motor.gerar_audio(texto, destino))
    except Exception:
        ok = False
    return destino if ok and destino.is_file() else None


def _versao(admin=False):
    if admin:
        arquivo = APK_DIR / "versao-admin.json"
        if not arquivo.is_file():
            arquivo = APK_DIR / "versao.json"
    else:
        arquivo = APK_DIR / "versao-publico.json"
        if not arquivo.is_file():
            arquivo = APK_DIR / "versao.json"
    try:
        dados = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        def vazia():
            return {"version_code": 0, "version_name": "", "apk_url": "", "notas": ""}

        return vazia()
    return dados


PAGINA_INICIO = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101114">
<title>EKO</title>
<style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#101114;color:#e8e8ea;padding:24px}
.card{max-width:380px;width:100%;text-align:center;background:#191b20;border:1px solid #24272e;
border-radius:24px;padding:30px 22px}
.logo{width:84px;height:84px;margin:0 auto 18px;border-radius:50%;background:#26324a;
display:flex;align-items:center;justify-content:center;font-size:36px;color:#bcd2ff}
h1{font-size:26px;margin:0 0 4px;letter-spacing:3px}
p{color:#9aa0ac;font-size:14px;margin:0 0 22px}
a.btn{display:block;background:#3d8bff;color:#fff;text-decoration:none;font-weight:600;
padding:15px;border-radius:16px;margin-bottom:12px}
.logo-img{width:112px;height:112px;margin:0 auto 16px;border-radius:50%;display:block}
.versao{color:#767b86;font-size:12px;margin-top:2px}
a.btn.alt{background:#23262d;color:#c9ccd4}
.passo{text-align:left;color:#8a8d96;font-size:13px;margin:18px 0 0;padding-left:18px}
.passo li{margin-bottom:6px}
</style></head>
<body>
<div class="card">
  <img class="logo-img" src="/logo.png" alt="EKO Play">
  <h1>EKO Play</h1>
  <a class="btn" href="/apk/eko.apk">Baixar</a>
  <div class="versao">{{VERSAO}}</div>
</div>
</body></html>
"""

PAGINA_CADASTRO = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101114"><title>EKO - criar conta</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:#101114;color:#e8e8ea;padding:24px}
.card{width:100%;max-width:420px;background:#191b20;border:1px solid #24272e;
border-radius:24px;padding:26px 22px}
.logo{width:64px;height:64px;border-radius:50%;background:#26324a;color:#bcd2ff;
display:flex;align-items:center;justify-content:center;font-size:30px;margin:0 auto 14px}
h1{font-size:22px;margin:0 0 6px;text-align:center}
p{color:#9aa0ac;font-size:13px;margin:0 0 18px;text-align:center}
input{width:100%;background:#101114;border:1px solid #2a2d35;border-radius:12px;
color:#e8e8ea;padding:12px;font-size:15px;margin-bottom:10px}
input:focus{outline:none;border-color:#3d8bff}
button{width:100%;padding:14px;border:0;border-radius:12px;background:#3d8bff;
color:#fff;font-weight:600;font-size:15px}
button:disabled{opacity:.6}
#msg{margin-top:14px;font-size:13px;text-align:center;min-height:18px}
.erro{color:#ff7b81}.ok{color:#5bd68a}
.dica{font-size:11px;color:#6a6d76;margin:16px 0 0}
</style></head><body>
<div class="card">
  <div class="logo">â™ª</div>
  <h1>Criar conta</h1>
  <p>PeÃ§a acesso. O dono aprova antes de vocÃª entrar.</p>
  <form id="f">
    <input id="nome" placeholder="Seu nome" autocomplete="name" required>
    <input id="usuario" placeholder="UsuÃ¡rio (sem espaÃ§os)" autocomplete="username" required>
    <input id="email" type="email" placeholder="E-mail (opcional)" autocomplete="email">
    <input id="senha" type="password" placeholder="Senha (mÃ­nimo 6)" autocomplete="new-password" required>
    <input id="senha2" type="password" placeholder="Repita a senha" autocomplete="new-password" required>
    <button type="submit" id="btn">Pedir acesso</button>
  </form>
  <div id="msg"></div>
  <p class="dica">Depois da aprovaÃ§Ã£o, entre no app ou na web com o mesmo usuÃ¡rio e senha.</p>
</div>
<script>
const $ = (id) => document.getElementById(id);
function mostrar(t, ok) { const m = $('msg'); m.textContent = t; m.className = ok ? 'ok' : 'erro'; }
$('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  const nome = $('nome').value.trim();
  const usuario = $('usuario').value.trim();
  const email = $('email').value.trim();
  const senha = $('senha').value;
  const senha2 = $('senha2').value;
  if (!nome || !usuario || !senha) { mostrar('Preencha nome, usuÃ¡rio e senha.'); return; }
  if (senha !== senha2) { mostrar('As senhas nÃ£o conferem.'); return; }
  if (senha.length < 6) { mostrar('A senha precisa ter pelo menos 6 caracteres.'); return; }
  $('btn').disabled = true; $('btn').textContent = 'Enviando...';
  try {
    const r = await fetch('/api/cadastro', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({nome, usuario, email, senha})});
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.erro || ('erro ' + r.status));
    $('f').style.display = 'none';
    mostrar('Pedido enviado! Aguarde a aprovaÃ§Ã£o do dono.', true);
  } catch (err) { mostrar(err.message); }
  finally { $('btn').disabled = false; $('btn').textContent = 'Pedir acesso'; }
});
</script></body></html>
"""

PAGINA_APROVACOES = r"""<!doctype html>
<html lang="pt-BR"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#101114"><title>EKO - aprovaÃ§Ãµes</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;background:#101114;color:#e8e8ea;padding:18px;
font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;max-width:640px;margin:0 auto}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#8a8d96;font-size:13px;margin-bottom:18px}
input{width:100%;background:#191b20;border:1px solid #2a2d35;border-radius:12px;
color:#e8e8ea;padding:12px;font:14px ui-monospace,monospace;margin-bottom:10px}
button{width:100%;padding:14px;border:0;border-radius:12px;background:#3d8bff;color:#fff;
font-weight:600;font-size:15px;margin-bottom:10px}
button.sec{background:#23262d;color:#c9ccd4;font-weight:500}
.item{background:#191b20;border:1px solid #24272e;border-radius:14px;padding:14px;margin:10px 0}
.item b{font-size:15px}
.meta{color:#8a8d96;font-size:12px;margin:4px 0 10px;word-break:break-all}
.acoes{display:flex;gap:8px}
.acoes button{margin:0;padding:10px;font-size:13px}
.vazio{color:#6a6d76;font-size:13px;text-align:center;padding:24px 0}
</style></head><body>
<h1>AprovaÃ§Ãµes</h1>
<div class="sub">cadastros pendentes â€” aprove sÃ³ quem vocÃª conhece</div>
<input id="tok" type="password" placeholder="senha do dono">
<button id="conectar" class="sec">Conectar</button>
<div id="lista"><div class="vazio">Informe a senha do dono.</div></div>
<script>
const $ = (id) => document.getElementById(id);
let T = new URLSearchParams(location.search).get('t') || localStorage.getItem('eko_master') || '';
if (T) { $('tok').value = T; localStorage.setItem('eko_master', T); carregar(); }
$('conectar').onclick = () => {
  T = $('tok').value.trim();
  if (!T) return;
  localStorage.setItem('eko_master', T);
  carregar();
};
async function carregar() {
  try {
    const r = await fetch('/api/cadastros', {headers: {'X-Token': T}});
    if (r.status === 401 || r.status === 403) { $('lista').innerHTML = '<div class="vazio">Senha do dono invÃ¡lida.</div>'; return; }
    const j = await r.json();
    if (!j.length) { $('lista').innerHTML = '<div class="vazio">Nenhum cadastro pendente.</div>'; return; }
    $('lista').innerHTML = j.map((c) =>
      '<div class="item"><b>' + esc(c.nome) + '</b>' +
      '<div class="meta">' + esc(c.usuario) + (c.email ? ' Â· ' + esc(c.email) : '') +
      ' Â· ' + esc(c.criado) + '</div><div class="acoes">' +
      '<button onclick="acao(\'' + c.id + '\',\'aprovar\')">Aprovar</button>' +
      '<button class="sec" onclick="acao(\'' + c.id + '\',\'rejeitar\')">Rejeitar</button>' +
      '</div></div>').join('');
  } catch (e) {
    $('lista').innerHTML = '<div class="vazio">Erro ao carregar.</div>';
  }
}
async function acao(id, a) {
  const r = await fetch('/api/cadastro/aprovar', {method: 'POST',
    headers: {'Content-Type': 'application/json', 'X-Token': T},
    body: JSON.stringify({id, acao: a})});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { alert(j.erro || 'erro'); return; }
  carregar();
}
function esc(t) {
  return String(t == null ? '' : t).replace(/[&<>"']/g, (c) =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script></body></html>
"""


ASSETLINKS = [{
    "relation": ["delegate_permission/common.handle_all_urls"],
    "target": {
        "namespace": "android_app",
        "package_name": "eco.c",
        "sha256_cert_fingerprints": [
            "FF:89:64:16:C3:A1:62:91:CB:2A:A7:2E:7E:4F:AC:FC:"
            "04:F0:2F:35:51:CF:A6:FE:37:35:B4:08:AB:0F:69:86"
        ],
    },
}]


class Handler(BaseHTTPRequestHandler):
    server_version = "EKO/1.0"
    timeout = 15

    def log_message(self, formato, *args):
        pass

    def _responder(self, codigo, corpo, tipo="application/json; charset=utf-8", extra=None):
        dados = corpo.encode("utf-8") if isinstance(corpo, str) else corpo
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(dados)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=31536000")
        if not extra or "Cache-Control" not in extra:
            self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(dados)

    def _ip_cliente(self):
        return (
            (self.headers.get("CF-Connecting-IP") or "").strip()
            or (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            or self.client_address[0]
        )

    def _json(self, codigo, obj):
        self._responder(codigo, json.dumps(obj, ensure_ascii=False))

    def _enviar_logo(self):
        caminho = APK_DIR / "logo.png"
        if not caminho.is_file():
            return self._json(404, {"erro": "logo nao publicada"})
        dados = caminho.read_bytes()
        return self._responder(200, dados, "image/png")

    def _enviar_apk(self, nome="eko.apk"):
        caminho = APK_DIR / nome
        if not caminho.is_file():
            return self._json(404, {"erro": "apk ainda nao publicado"})
        tamanho = caminho.stat().st_size
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.android.package-archive")
            self.send_header("Content-Length", str(tamanho))
            self.send_header("Content-Disposition", f'attachment; filename="{nome}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with caminho.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 256)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _enviar_noticia_audio(self, ident):
        if not ident:
            return self._json(404, {"erro": "audio nao encontrado"})
        caminho = _noticia_audio(ident)
        if not caminho:
            return self._json(404, {"erro": "audio indisponivel"})
        try:
            dados = caminho.read_bytes()
        except OSError:
            return self._json(404, {"erro": "audio indisponivel"})
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(dados)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(dados)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _enviar_arquivo(self, caminho):
        try:
            caminho = caminho.resolve()
            raizes = [
                g.caminho(CFG, "music_dir").resolve(),
                g.caminho(CFG, "video_dir").resolve(),
            ]
            if not any(str(caminho).startswith(str(r)) for r in raizes):
                return self._json(403, {"erro": "fora do acervo"})
            if not caminho.is_file():
                return self._json(404, {"erro": "arquivo nao encontrado"})
            tamanho = caminho.stat().st_size
        except OSError:
            return self._json(404, {"erro": "arquivo nao encontrado"})
        tipo = mimetypes.guess_type(caminho.name)[0] or "application/octet-stream"
        try:
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(tamanho))
            self.send_header("Content-Disposition",
                             f'attachment; filename="{caminho.name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            with caminho.open("rb") as f:
                shutil.copyfileobj(f, self.wfile, 1024 * 256)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _enviar_audio(self, caminho):
        try:
            tamanho = caminho.stat().st_size
        except OSError:
            return self._json(404, {"erro": "faixa nao encontrada"})
        if tamanho <= 0:
            return self._json(404, {"erro": "faixa vazia"})
        inicio, fim = 0, tamanho - 1
        parcial = False
        bruto = (self.headers.get("Range") or "").strip()
        if bruto:
            m = re.match(r"bytes=(\d*)-(\d*)$", bruto)
            if not m:
                return self._responder(
                    416, b"", "text/plain",
                    {"Content-Range": f"bytes */{tamanho}"},
                )
            if m.group(1):
                inicio = int(m.group(1))
                fim = int(m.group(2)) if m.group(2) else tamanho - 1
            elif m.group(2):
                inicio = max(0, tamanho - int(m.group(2)))
            if inicio >= tamanho or fim < inicio:
                return self._responder(
                    416, b"", "text/plain",
                    {"Content-Range": f"bytes */{tamanho}"},
                )
            fim = min(fim, tamanho - 1)
            parcial = True
        comprimento = fim - inicio + 1
        self.send_response(206 if parcial else 200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(comprimento))
        self.send_header("Cache-Control", "private, max-age=3600")
        self.send_header(
            "Content-Disposition",
            f"inline; filename*=UTF-8''{quote(caminho.name)}",
        )
        if parcial:
            self.send_header("Content-Range", f"bytes {inicio}-{fim}/{tamanho}")
        self.end_headers()
        if getattr(self, "command", "") == "HEAD":
            return
        try:
            with caminho.open("rb") as f:
                f.seek(inicio)
                restante = comprimento
                while restante > 0:
                    bloco = f.read(min(256 * 1024, restante))
                    if not bloco:
                        break
                    self.wfile.write(bloco)
                    restante -= len(bloco)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _capa_da_faixa(self, faixa_id):
        faixa = _biblioteca_por_id.get(faixa_id)
        if not faixa:
            return None
        seguro = re.sub(r"[^A-Za-z0-9_-]", "", faixa_id)[:60]
        if not seguro:
            return None
        try:
            CAPAS_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        alvo = CAPAS_DIR / (seguro + ".jpg")
        if alvo.is_file() and alvo.stat().st_size > 0:
            return alvo
        try:
            proc = subprocess.run(
                ["C:\\ffmpeg\\bin\\ffmpeg.exe", "-y", "-loglevel", "error",
                 "-i", str(faixa["_caminho"]), "-an", "-vcodec", "copy", str(alvo)],
                capture_output=True, timeout=25,
            )
        except (subprocess.SubprocessError, OSError):
            proc = None
        if proc is not None and proc.returncode == 0 and alvo.is_file() and alvo.stat().st_size > 0:
            return alvo
        alvo.unlink(missing_ok=True)
        return None

    def _enviar_capa(self, faixa_id):
        faixa = _biblioteca_por_id.get(faixa_id)
        if not faixa:
            return self._json(404, {"erro": "faixa nao encontrada"})
        capa = self._capa_da_faixa(faixa_id)
        if capa:
            try:
                dados = capa.read_bytes()
            except OSError:
                dados = b""
            if dados:
                return self._responder(
                    200, dados, "image/jpeg",
                    {"Cache-Control": "public, max-age=604800"},
                )
        if re.fullmatch(r"[A-Za-z0-9_-]{6,20}", faixa_id):
            return self._responder(
                302, b"", "text/plain",
                {"Location": f"https://i.ytimg.com/vi/{faixa_id}/hqdefault.jpg"},
            )
        return self._json(404, {"erro": "sem capa"})

    def _nd_stream(self, nd_id):
        if not nd_id:
            return self._json(404, {"erro": "faixa nao informada"})
        headers = {}
        faixa = self.headers.get("Range")
        if faixa:
            headers["Range"] = faixa
        try:
            req = Request(_nd_url("stream.view", id=nd_id), headers=headers)
            with urlopen(req, timeout=30) as r:
                self.send_response(r.status)
                for chave in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
                    valor = r.headers.get(chave)
                    if valor:
                        self.send_header(chave, valor)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                while True:
                    bloco = r.read(256 * 1024)
                    if not bloco:
                        break
                    self.wfile.write(bloco)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            self._json(404, {"erro": "faixa indisponivel"})

    def _nd_capa(self, nd_id):
        if not nd_id:
            return self._json(404, {"erro": "faixa nao informada"})
        try:
            with urlopen(
                _nd_url("getCoverArt.view", id=nd_id, size=300), timeout=20
            ) as r:
                dados = r.read()
                tipo = r.headers.get("Content-Type") or "image/jpeg"
            return self._responder(
                200, dados, tipo, {"Cache-Control": "public, max-age=604800"}
            )
        except Exception:  # noqa: BLE001
            return self._json(404, {"erro": "sem capa"})

    def _receber_cookies(self, bruto):
        try:
            corpo = json.loads(bruto or "{}")
        except json.JSONDecodeError:
            return self._json(400, {"erro": "json invalido"})
        texto = str((corpo or {}).get("cookies") or "").strip()
        if not texto:
            return self._json(400, {"erro": "cookies vazios"})
        linhas = ["# Netscape HTTP Cookie File", ""]
        total = 0
        for par in texto.split(";"):
            if "=" not in par:
                continue
            nome, valor = par.split("=", 1)
            nome, valor = nome.strip(), valor.strip()
            if not nome or not valor:
                continue
            if any(c in nome or c in valor for c in ("\t", "\r", "\n")):
                continue
            if any(c in nome for c in (";", "=", " ")):
                continue
            linhas.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", "0", nome, valor]))
            total += 1
        if not total:
            return self._json(400, {"erro": "nenhum cookie valido"})
        destino = ROOT / "cookies.txt"
        temp = ROOT / f"cookies.tmp.{uuid.uuid4().hex[:8]}.txt"
        temp.write_text("\n".join(linhas) + "\n", encoding="utf-8")
        ok, erro = _validar_cookies(temp)
        if not ok:
            temp.unlink(missing_ok=True)
            return self._json(400, {"erro": f"cookies nao servem: {erro}"})
        temp.replace(destino)
        return self._json(200, {"ok": True, "cookies": total})

    def do_GET(self):
        ip = self._ip_cliente()
        if not _permitir(ip):
            return self._json(429, {"erro": "muitas requisicoes; espere um pouco"})
        partes = urlparse(self.path)
        rota, query = partes.path, parse_qs(partes.query)
        if rota in ("/apk", "/apk/"):
            extra = ""
            return self._responder(
                200, _pagina_instalar(), "text/html; charset=utf-8"
            )
        if rota in ("/instalar", "/instalar/"):
            extra = ""
            if (APK_DIR / "player.apk").is_file():
                extra = '<a class="btn alt" href="/apk/player.apk">Baixar o player</a>'
            return self._responder(
                200, _pagina_instalar(), "text/html; charset=utf-8"
            )
        if rota in ("/", "/index.html"):
            extra = ""
            if (APK_DIR / "player.apk").is_file():
                extra = '<a class="btn alt" href="/apk/player.apk">Baixar o player</a>'
            return self._responder(
                200, _pagina_instalar(), "text/html; charset=utf-8"
            )
        if rota in ("/fila", "/fila/"):
            return self._json(404, {"erro": "rota desconhecida"})
        if rota == "/painel" or rota.startswith("/convite/"):
            return self._json(404, {"erro": "rota desconhecida"})
        if rota == "/icone.svg":
            return self._responder(200, ICONE, "image/svg+xml")
        if rota == "/health":
            return self._json(200, {
                "ok": True,
                "fila": len(novidades()),
                "faixas": len(carregar_biblioteca()),
            })
        if rota == "/.well-known/assetlinks.json":
            return self._json(200, ASSETLINKS)
        if rota == "/api/versao":
            papel = papel_do_token(token_enviado(self, query))
            return self._json(200, _versao(admin=papel in ("admin", "root")))
        if rota == "/logo.png":
            return self._enviar_logo()
        if rota == "/apk/eko.apk":
            return self._enviar_apk("eko.apk")
        if rota == "/apk/eko8.apk":
            return self._enviar_apk("eko-android8.apk")
        if rota == "/apk/player.apk":
            return self._enviar_apk("player.apk")
        if rota.startswith("/apk/player-") and rota.endswith(".apk"):
            return self._enviar_apk(rota.rsplit("/", 1)[-1])
        if rota == "/api/estado":
            return self._json(200, {
                "precisa_setup": not _ha_admin(),
                "versao": _versao().get("version_name", ""),
            })
        if rota == "/api/pedido":
            pid = (query.get("id") or [""])[0]
            chave = (query.get("chave") or [""])[0]
            app = (query.get("app") or ["eko"])[0][:12]
            pessoa = pedido_por_chave(pid, chave)
            if not pessoa:
                return self._json(404, {"erro": "pedido nao encontrado"})
            if pessoa.get("estado") == "aprovado":
                return self._json(200, {
                    "estado": "aprovado",
                    "nome": pessoa.get("nome", ""),
                    "token": token_da_pessoa(pessoa, app),
                    **garantir_usuario_navidrome(
                        pessoa.get("email", ""), pessoa.get("nome", "")
                    ),
                })
            return self._json(200, {
                "estado": pessoa.get("estado", "pendente"),
                "nome": pessoa.get("nome", ""),
            })
        if rota == "/tocar":
            return self._responder(200, PAGINA_TOCAR, "text/html; charset=utf-8")
        if rota == "/tocar.webmanifest":
            return self._json(200, MANIFEST_TOCAR)
        if rota == "/criar-conta":
            return self._responder(200, PAGINA_CADASTRO, "text/html; charset=utf-8")
        if rota == "/aprovacoes":
            return self._responder(200, PAGINA_APROVACOES, "text/html; charset=utf-8")
        if rota == "/api/entrar":
            self._json(405, {"erro": "use POST"})
            return
        if rota == "/api/cadastros":
            if not dono_ok(self, query):
                return self._json(403, {"erro": "so o dono"})
            return self._json(200, cadastros_publicos())
        if rota == "/api/baixadas":
            usuario = (query.get("usuario") or [""])[0]
            nome = nome_por_usuario(usuario)
            if not nome:
                return self._json(404, {"erro": "usuario sem playlist"})
            return self._json(200, {
                "usuario": usuario,
                "nome": nome,
                "playlist": f"Baixadas - {nome}",
            })
        if not token_ok(self, query):
            _falhou_auth(ip)
            return self._json(401, {"erro": "senha invalida"})
        if rota == "/api/baixadas":
            usuario = (query.get("usuario") or [""])[0]
            nome = nome_por_usuario(usuario)
            if not nome:
                return self._json(404, {"erro": "usuario sem playlist"})
            return self._json(200, {
                "usuario": usuario,
                "nome": nome,
                "playlist": f"Baixadas - {nome}",
            })
        if rota == "/api/quem":
            enviado = token_enviado(self, query)
            dev = dispositivo_por_token(enviado) or {}
            return self._json(200, {
                "nome": dev.get("nome", ""),
                "email": dev.get("email", ""),
                "dono": _dono(enviado),
                "papel": papel_do_token(enviado) or "",
                "precisa_setup": not _ha_admin(),
            })
        if rota == "/api/pedidos":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono ve os pedidos"})
            fila = [
                {
                    "id": p["id"],
                    "nome": p.get("nome", ""),
                    "email": p.get("email", ""),
                    "app": p.get("app", "eko"),
                    "criado": p.get("criado", ""),
                }
                for p in _pessoas
                if p.get("estado") == "pendente"
            ]
            return self._json(200, fila)
        if rota == "/api/pessoas":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono ve as pessoas"})
            saida = []
            for p in _pessoas:
                if p.get("estado") == "negado":
                    continue
                aparelhos = [
                    {
                        "id": d.get("id"),
                        "app": d.get("app", "eko"),
                        "criado": d.get("criado", ""),
                        "visto": d.get("visto", ""),
                    }
                    for d in _dispositivos
                    if d.get("pessoa") == p["id"]
                ]
                saida.append({
                    "id": p["id"],
                    "nome": p.get("nome", ""),
                    "email": p.get("email", ""),
                    "estado": p.get("estado", ""),
                    "papel": p.get("papel", "usuario"),
                    "criado": p.get("criado", ""),
                    "aprovado": p.get("aprovado", ""),
                    "aparelhos": aparelhos,
                })
            return self._json(200, saida)
        if rota == "/api/fila":
            return self._json(200, fila_publica())
        if rota == "/api/biblioteca":
            carregar_biblioteca()
            consulta = (query.get("q") or [""])[0]
            return self._json(200, {
                "total": len(_biblioteca),
                "faixas": [faixa_publica(f) for f in buscar_biblioteca(consulta)],
            })
        if rota == "/api/faixa":
            carregar_biblioteca()
            faixa = _biblioteca_por_id.get((query.get("id") or [""])[0])
            if not faixa:
                return self._json(404, {"erro": "faixa nao encontrada"})
            return self._enviar_audio(faixa["_caminho"])
        if rota == "/api/capa":
            carregar_biblioteca()
            return self._enviar_capa((query.get("id") or [""])[0])
        if rota == "/api/radio":
            seed = (query.get("seed") or [""])[0].strip()
            titulo = (query.get("titulo") or [""])[0].strip()
            artista = (query.get("artista") or [""])[0].strip()
            try:
                n = int((query.get("n") or ["20"])[0])
            except ValueError:
                n = 20
            n = max(1, min(n, 50))
            semente = {"id": seed} if seed else _nd_buscar(titulo, artista)
            if not semente:
                aleatorias = _nd_json("getRandomSongs.view", size=n)
                faixas = (
                    aleatorias.get("subsonic-response", {})
                    .get("randomSongs", {})
                    .get("song", [])
                )
                return self._json(200, {
                    "ok": True, "seed_nd": "",
                    "faixas": [_radio_faixa(s) for s in faixas],
                })
            similares = [
                s for s in _nd_similares(semente["id"], n)
                if str(s.get("id") or "") != str(semente["id"])
            ]
            if not similares:
                aleatorias = _nd_json("getRandomSongs.view", size=n)
                similares = (
                    aleatorias.get("subsonic-response", {})
                    .get("randomSongs", {})
                    .get("song", [])
                )
            return self._json(200, {
                "ok": True,
                "seed_nd": semente["id"],
                "faixas": [_radio_faixa(s) for s in similares],
            })
        if rota == "/api/nd/stream":
            return self._nd_stream((query.get("id") or [""])[0])
        if rota == "/api/nd/capa":
            return self._nd_capa((query.get("id") or [""])[0])
        if rota == "/api/noticias":
            if not token_ok(self, query):
                return self._json(401, {"erro": "senha invalida"})
            categoria = (query.get("categoria") or query.get("cat") or ["tecnologia"])[0]
            cidade = (query.get("cidade") or CFG.get("cidade_noticias") or ["Jundiai"])[0]
            return self._json(200, _noticias_lista(categoria, cidade))
        if rota == "/api/noticia_audio":
            if not token_ok(self, query):
                return self._json(401, {"erro": "senha invalida"})
            return self._enviar_noticia_audio((query.get("i") or [""])[0])
        if rota == "/api/arquivo":
            jid = (query.get("job") or [""])[0]
            job = next((j for j in novidades() if j["id"] == jid), None)
            if not job or not job.get("arquivo"):
                return self._json(404, {"erro": "arquivo nao pronto"})
            return self._enviar_arquivo(Path(job["arquivo"]))
        if rota == "/baixar":
            urls = query.get("url") or query.get("u") or []
            modo = "video" if (query.get("modo") or [""])[0] == "video" else "audio"
            destino = "telefone" if (query.get("destino") or [""])[0] == "telefone" else "servidor"
            criados = adicionar(urls, "get", modo, destino)
            return self._json(200, {"criados": [c["id"] for c in criados], "total": len(criados)})
        return self._json(404, {"erro": "rota desconhecida"})

    def do_HEAD(self):
        partes = urlparse(self.path)
        rota, query = partes.path, parse_qs(partes.query)
        if rota == "/api/faixa":
            if not token_ok(self, query):
                return self._json(401, {"erro": "senha invalida"})
            carregar_biblioteca()
            faixa = _biblioteca_por_id.get((query.get("id") or [""])[0])
            if not faixa:
                return self._json(404, {"erro": "faixa nao encontrada"})
            caminho = Path(faixa["_caminho"])
            if not caminho.is_file():
                return self._json(404, {"erro": "faixa nao encontrada"})
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(caminho.stat().st_size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        global _dispositivos
        partes = urlparse(self.path)
        rota, query = partes.path, parse_qs(partes.query)
        ip = self._ip_cliente()
        if not _permitir(ip):
            return self._json(429, {"erro": "muitas requisicoes; espere um pouco"})
        tamanho = int(self.headers.get("Content-Length") or 0)
        limite = 2 * 1024 * 1024 if rota == "/api/cookies" else 64 * 1024
        if tamanho > limite:
            return self._json(413, {"erro": "corpo grande demais"})
        bruto = self.rfile.read(max(0, min(tamanho, limite))).decode("utf-8", "replace")
        if rota == "/api/setup":
            if _ha_admin():
                return self._json(403, {"erro": "o servidor ja tem administrador"})
            if not _limitar(ip, "setup", 6, 300):
                return self._json(429, {"erro": "muitas tentativas; espere"})
            if not TOKEN:
                return self._json(400, {"erro": "servidor sem senha configurada"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                corpo = {}
            senha = str((corpo or {}).get("senha") or "")
            nome = " ".join(str((corpo or {}).get("nome") or "").split())[:50]
            email = str((corpo or {}).get("email") or "").strip().lower()[:90]
            if not hmac.compare_digest(senha, TOKEN):
                _falhou_auth(ip)
                return self._json(401, {"erro": "senha do servidor errada"})
            if len(nome) < 2:
                return self._json(400, {"erro": "diga seu nome"})
            agora = datetime.now().isoformat(timespec="seconds")
            with _trava:
                pessoa = {
                    "id": uuid.uuid4().hex[:8],
                    "nome": nome,
                    "email": email,
                    "app": "eko",
                    "estado": "aprovado",
                    "papel": "admin",
                    "chave": "",
                    "criado": agora,
                    "aprovado": agora,
                }
                _pessoas.append(pessoa)
                _salvar_pessoas()
            _adicionar_autorizado(email)
            segredo = token_da_pessoa(pessoa, "eko")
            print(f" EKO: primeiro admin criado ({nome})")
            return self._json(200, {
                "ok": True, "token": segredo, "nome": nome,
                "email": email, "papel": "admin",
                **garantir_usuario_navidrome(email, nome),
            })
        if rota == "/api/dono/entrar":
            if not _limitar(ip, "dono", 8, 300):
                return self._json(429, {"erro": "muitas tentativas; espere"})
            if not TOKEN:
                return self._json(400, {"erro": "servidor sem senha de dono"})
            try:
                senha = str(json.loads(bruto or "{}").get("senha") or "")
            except (json.JSONDecodeError, AttributeError):
                senha = ""
            if not hmac.compare_digest(senha, TOKEN):
                _falhou_auth(ip)
                return self._json(401, {"erro": "senha do dono errada"})
            segredo = secrets.token_urlsafe(32)
            agora = datetime.now().isoformat(timespec="seconds")
            with _trava:
                alvo = {
                    "id": uuid.uuid4().hex[:8],
                    "nome": "Dono",
                    "email": "",
                    "app": "eko",
                    "dono": True,
                    "criado": agora,
                    "visto": "",
                    "hash": _hash(segredo),
                }
                _dispositivos.append(alvo)
                antigos = [d for d in _dispositivos if d.get("dono")]
                if len(antigos) > 4:
                    for velho in antigos[:-4]:
                        _dispositivos.remove(velho)
                _salvar_dispositivos()
            print(" EKO: dono entrou")
            return self._json(200, {"ok": True, "token": segredo, "nome": "Dono"})
        if rota == "/api/pedir":
            if not _limitar(ip, "pedir", 6, 600):
                return self._json(429, {"erro": "muitos pedidos; tente mais tarde"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"erro": "json invalido"})
            pid, chave = pedir_acesso(
                (corpo or {}).get("nome"),
                (corpo or {}).get("email"),
                (corpo or {}).get("app"),
            )
            if not pid:
                return self._json(400, {"erro": "diga seu nome"})
            return self._json(200, {"ok": True, "id": pid, "chave": chave})
        if rota == "/api/entrar":
            if not _limitar(ip, "entrar", 20, 300):
                return self._json(429, {"erro": "muitas tentativas, espere um pouco"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"erro": "json invalido"})
            jwt = str((corpo or {}).get("id_token") or "")
            nonce = str((corpo or {}).get("nonce") or "")
            dados, erro = entrar_google(jwt, nonce)
            if not dados:
                return self._json(401, {"erro": erro})
            if dados.get("token"):
                print(f" EKO: entrou {dados.get('nome')} ({dados.get('email')})")
            return self._json(200, {"ok": True, **dados})
        if rota == "/api/cadastro":
            if not _limitar(ip, "cadastro", 5, 300):
                return self._json(429, {"erro": "muitos pedidos; tente mais tarde"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"erro": "json invalido"})
            ok, erro = cadastro_novo(
                (corpo or {}).get("nome"),
                (corpo or {}).get("usuario"),
                (corpo or {}).get("email"),
                (corpo or {}).get("senha"),
            )
            if not ok:
                return self._json(400, {"erro": erro})
            return self._json(200, {"ok": True})
        if rota == "/api/cadastro/aprovar":
            if not dono_ok(self, query):
                return self._json(403, {"erro": "so o dono aprova"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                return self._json(400, {"erro": "json invalido"})
            ok, msg = aprovar_cadastro(
                str((corpo or {}).get("id") or ""),
                str((corpo or {}).get("acao") or "aprovar"),
            )
            if not ok:
                return self._json(400, {"erro": msg})
            return self._json(200, {"ok": True, "resultado": msg})
        if not token_ok(self, query):
            _falhou_auth(ip)
            return self._json(401, {"erro": "senha invalida"})
        if rota == "/api/autorizar":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono autoriza"})
            try:
                email = str(json.loads(bruto or "{}").get("email") or "").strip().lower()
            except (json.JSONDecodeError, AttributeError):
                email = ""
            if "@" not in email:
                return self._json(400, {"erro": "e-mail invalido"})
            lista = list(CFG.get("autorizados") or [])
            if email not in [str(x).strip().lower() for x in lista]:
                lista.append(email)
                CFG["autorizados"] = lista
                caminho = ROOT / "config.json"
                dados = json.loads(caminho.read_text(encoding="utf-8"))
                dados["autorizados"] = lista
                caminho.write_text(
                    json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8"
                )
            return self._json(200, {"ok": True, "autorizados": lista})
        if rota == "/api/dono":
            if not TOKEN or not hmac.compare_digest(token_enviado(self, query), TOKEN):
                return self._json(403, {"erro": "so o dono atual escolhe"})
            try:
                alvo = str(json.loads(bruto or "{}").get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                alvo = ""
            with _trava:
                achou = False
                for d in _dispositivos:
                    d["dono"] = d.get("id") == alvo
                    achou = achou or d["dono"]
                if achou:
                    _salvar_dispositivos()
            if not achou:
                return self._json(404, {"erro": "aparelho nao encontrado"})
            print(f" EKO: dono agora e {alvo}")
            return self._json(200, {"ok": True})
        if rota == "/api/aprovar":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o admin aprova"})
            try:
                corpo = json.loads(bruto or "{}")
            except json.JSONDecodeError:
                corpo = {}
            alvo = str((corpo or {}).get("id") or "")
            papel = "admin" if (corpo or {}).get("papel") == "admin" else "usuario"
            for p in _pessoas:
                if p.get("id") == alvo and p.get("estado") == "pendente":
                    p["estado"] = "aprovado"
                    p["papel"] = papel
                    p["aprovado"] = datetime.now().isoformat(timespec="seconds")
                    _salvar_pessoas()
                    for d in _dispositivos:
                        if d.get("pessoa") == alvo:
                            d["papel"] = papel
                            d["dono"] = papel == "admin"
                    _salvar_dispositivos()
                    if papel == "admin":
                        _adicionar_autorizado(p.get("email"))
                    print(f" EKO: aprovado {p.get('nome')} ({papel})")
                    return self._json(200, {"ok": True, "papel": papel})
            return self._json(404, {"erro": "pedido nao encontrado"})
        if rota == "/api/admin/promover":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o admin promove"})
            try:
                alvo = str(json.loads(bruto or "{}").get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                alvo = ""
            with _trava:
                pessoa = next((p for p in _pessoas if p.get("id") == alvo), None)
                if pessoa is None:
                    return self._json(404, {"erro": "pessoa nao encontrada"})
                pessoa["papel"] = "admin"
                for d in _dispositivos:
                    if d.get("pessoa") == alvo:
                        d["papel"] = "admin"
                        d["dono"] = True
                _salvar_pessoas()
                _salvar_dispositivos()
            _adicionar_autorizado(pessoa.get("email"))
            print(f" EKO: admin promovido {pessoa.get('nome')}")
            return self._json(200, {"ok": True})
        if rota == "/api/admin/rebaixar":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o admin rebaixa"})
            try:
                alvo = str(json.loads(bruto or "{}").get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                alvo = ""
            with _trava:
                pessoa = next((p for p in _pessoas if p.get("id") == alvo), None)
                if pessoa is None:
                    return self._json(404, {"erro": "pessoa nao encontrada"})
                admins = [p for p in _pessoas if p.get("papel") == "admin"]
                if pessoa.get("papel") == "admin" and len(admins) <= 1 and not autorizados():
                    return self._json(400, {"erro": "precisa de outro admin"})
                pessoa["papel"] = "usuario"
                for d in _dispositivos:
                    if d.get("pessoa") == alvo:
                        d["papel"] = "usuario"
                        d["dono"] = False
                _salvar_pessoas()
                _salvar_dispositivos()
            _remover_autorizado(pessoa.get("email"))
            return self._json(200, {"ok": True})
        if rota == "/api/negar":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono nega"})
            try:
                alvo = str(json.loads(bruto or "{}").get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                alvo = ""
            for p in _pessoas:
                if p.get("id") == alvo:
                    p["estado"] = "negado"
                    _salvar_pessoas()
                    return self._json(200, {"ok": True})
            return self._json(404, {"erro": "pedido nao encontrado"})
        if rota == "/api/revogar":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono pode revogar"})
            try:
                alvo = str(json.loads(bruto or "{}").get("id") or "")
            except (json.JSONDecodeError, AttributeError):
                alvo = ""
            with _trava:
                pessoa = next((p for p in _pessoas if p.get("id") == alvo), None)
                if pessoa is not None:
                    admins = [p for p in _pessoas if p.get("papel") == "admin"]
                    if pessoa.get("papel") == "admin" and len(admins) <= 1 and not autorizados():
                        return self._json(400, {"erro": "precisa de outro admin"})
                    _pessoas.remove(pessoa)
                    removidos = len(_dispositivos)
                    _dispositivos = [d for d in _dispositivos if d.get("pessoa") != alvo]
                    _salvar_dispositivos()
                    _salvar_pessoas()
                    _remover_autorizado(pessoa.get("email"))
                    return self._json(200, {"removidos": removidos - len(_dispositivos)})
                antes = len(_dispositivos)
                _dispositivos = [d for d in _dispositivos if d.get("id") != alvo]
                _salvar_dispositivos()
            return self._json(200, {"removidos": antes - len(_dispositivos)})
        if rota == "/api/cookies":
            if not _dono(token_enviado(self, query)):
                return self._json(403, {"erro": "so o dono envia cookies"})
            return self._receber_cookies(bruto)
        urls = []
        modo, destino = "audio", "servidor"
        tipo = self.headers.get("Content-Type", "")
        if "application/json" in tipo:
            try:
                corpo = json.loads(bruto or "{}")
                if isinstance(corpo, dict):
                    urls = corpo.get("urls") or ([corpo["url"]] if corpo.get("url") else [])
                    modo = "video" if corpo.get("modo") == "video" else "audio"
                    destino = "telefone" if corpo.get("destino") == "telefone" else "servidor"
                elif isinstance(corpo, list):
                    urls = corpo
            except json.JSONDecodeError:
                urls = []
        else:
            urls = [l for l in bruto.split() if l.strip()]

        if rota in ("/api/enviar", "/api/baixar", "/baixar"):
            quem = (dispositivo_por_token(token_enviado(self, query)) or {}).get("nome") or "convidado"
            criados = adicionar([str(u) for u in urls], quem, modo, destino)
            print(
                f" EKO: enviar modo={modo} urls={len(urls)} criados={len(criados)}"
                + (f" primeiro={str(urls[0])[:90]}" if urls else "")
            )
            if not criados and urls:
                return self._json(202, {"criados": [], "aviso": "ja estava na fila"})
            if not criados:
                return self._json(400, {"erro": "nenhum link valido (use YouTube)"})
            return self._json(200, {"criados": [c["id"] for c in criados], "total": len(criados)})
        return self._json(404, {"erro": "rota desconhecida"})


def _validar_cookies(arquivo):
    teste = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    try:
        r = subprocess.run(
            [sys.executable, "-m", "yt_dlp", "--simulate", "--no-warnings",
             "--cookies", str(arquivo), "--print", "%(id)s", teste],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90,
        )
    except subprocess.TimeoutExpired:
        return False, "teste demorou demais"
    if r.returncode == 0 and r.stdout.strip():
        return True, ""
    linhas = [l for l in (r.stderr or "").strip().splitlines() if l.strip()]
    return False, (linhas[-1] if linhas else "nao funcionaram")[:180]


def ips_locais():
    saida = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith(("127.", "169.254.")) and ip not in saida:
                saida.append(ip)
    except OSError:
        pass
    return saida


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass
    if not TOKEN:
        print(" ERRO: defina \"token\" no config.json antes de subir o servidor.")
        return
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    carregar_fila()
    threading.Thread(target=trabalhar, daemon=True).start()

    servidor = ThreadingHTTPServer((HOST, PORT), Handler)
    print("=" * 58)
    print(" EKO no ar")
    print(f" Neste PC : http://localhost:{PORT}")
    for ip in ips_locais():
        print(f" Celular  : http://{ip}:{PORT}")
    if not TOKEN:
        print(" Senha    : (nenhuma) use apenas na rede local")
    else:
        print(" Senha    : definida (config.json)")
    print("=" * 58)
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nEncerrando...")
    finally:
        servidor.server_close()


if __name__ == "__main__":
    main()


