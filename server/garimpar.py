#!/usr/bin/env python3
"""garimpar - baixa playlists/videos, evita duplicatas e organiza a biblioteca."""

import argparse
import base64
import json
import logging
import re
import shutil
import struct
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG = {
    "music_dir": "music",
    "inbox_dir": "inbox",
    "state_dir": "state",
    "log_dir": "logs",
    "video_dir": "videos",
    "fpcalc": "bin/fpcalc.exe",
    "min_free_gb": 8,
    "reserve_gb": 5,
    "max_downloads": 300,
    "audio_format": "mp3",
    "audio_quality": "0",
    "dup_threshold": 0.20,
    "duration_tolerance": 4,
    "ffmpeg_location": "C:\\ffmpeg\\bin",
    "cookies_file": "cookies.txt",
    "keep_info_json": False,
    "host": "0.0.0.0",
    "porta": 8787,
    "token": "",
}

JUNK = [
    r"official\s*(music\s*)?video", r"official\s*audio", r"official\s*lyric\s*video",
    r"lyric\s*video", r"lyrics?", r"visualizer", r"audio\s*only", r"\baudio\b",
    r"clipe\s*oficial", r"video\s*oficial", r"v[ií]deo\s*oficial", r"legendado",
    r"hd", r"hq", r"4k", r"8k", r"1080p", r"720p", r"remaster(ed)?(\s*\d{4})?",
    r"full\s*hd", r"m/?v", r"color\s*coded", r"clean", r"explicit",
]
VERSION_WORDS = [
    r"live", r"ao\s*vivo", r"acoustic", r"ac[uú]stico", r"remix", r"instrumental",
    r"cover", r"version", r"vers[aã]o", r"demo", r"edit", r"extended", r"sped\s*up",
    r"slowed", r"reverb", r"karaoke", r"unplugged", r"session", r"deluxe",
]

STOPWORDS = {
    "the", "a", "an", "o", "de", "da", "do", "dos", "das", "e", "and", "of", "in", "to",
    "feat", "ft", "official", "video", "audio", "lyrics", "remix", "by",
}

log = logging.getLogger("garimpo")


def carregar_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        return dict(DEFAULT_CONFIG)
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def caminho(cfg, chave):
    p = Path(cfg[chave])
    return p if p.is_absolute() else ROOT / p


def opcoes_cookies(cfg):
    nome = (cfg.get("cookies_file") or "").strip()
    if not nome:
        return {}
    p = Path(nome)
    if not p.is_absolute():
        p = ROOT / p
    return {"cookiefile": str(p)} if p.exists() else {}


def slug_ascii(texto):
    t = unicodedata.normalize("NFKD", texto or "")
    return "".join(c for c in t if not unicodedata.combining(c))


def limpar_titulo(titulo):
    t = slug_ascii(titulo or "").lower()
    for _ in range(3):
        t = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", t)
    t = re.sub(r"\b(feat\.?|ft\.?|featuring)\b.*$", " ", t)
    for padrao in JUNK:
        t = re.sub(r"\b" + padrao + r"\b", " ", t)
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def normalizar(texto):
    t = slug_ascii(texto or "").lower()
    t = re.sub(r"[^\w\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def tokens_busca(texto):
    return {w for w in normalizar(texto).split() if len(w) > 2 and w not in STOPWORDS}


def limpar_consulta(titulo, uploader="", descricao=""):
    texto = " ".join(p for p in (titulo, uploader, descricao) if p)[:400]
    texto = re.sub(r"https?://\S+|www\.\S+", " ", texto)
    texto = re.sub(r"#\w+", " ", texto)
    texto = re.sub(r"@\w+", " ", texto)
    texto = re.sub(r"[^\w\s]", " ", slug_ascii(texto))
    texto = re.sub(r"\s+", " ", texto).strip()
    if len(texto) > 200:
        texto = texto[:200].rsplit(" ", 1)[0].strip()
    if len(tokens_busca(texto)) < 2:
        alt = re.sub(r"\s+", " ", slug_ascii(uploader or "")).strip()
        return alt[:200]
    return texto


def tem_versao(texto):
    t = normalizar(texto)
    return any(re.search(r"\b" + p + r"\b", t) for p in VERSION_WORDS)


def separar_artista(titulo, info):
    artista = info.get("artist") or info.get("creator") or ""
    faixa = info.get("track") or ""
    if artista and faixa:
        return artista.strip(), faixa.strip()
    canal = re.sub(r"\s*-\s*Topic$", "", info.get("uploader") or info.get("channel") or "")
    canal = re.sub(r"VEVO$", "", canal).strip()
    base = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", titulo or "")
    if " - " in base:
        esquerda, direita = base.split(" - ", 1)
        if 0 < len(esquerda) <= 60 and limpar_titulo(direita) and not re.search(r"\bby\b", direita, re.I):
            c = normalizar(canal)
            e = normalizar(esquerda)
            d = normalizar(direita)
            if c and d and (d == c or d in c or c in d):
                if not (e and (e == c or e in c or c in e)):
                    return direita.strip(), esquerda.strip()
            return esquerda.strip(), direita.strip()
    return canal or "Desconhecido", base.strip() or (titulo or "Sem titulo")


def nome_seguro(nome, limite=90):
    n = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", nome or "")
    n = re.sub(r"\s+", " ", n).strip(" .")
    return (n[:limite].strip(" .") or "Sem nome")


def espaco_livre_gb(pasta):
    return shutil.disk_usage(str(pasta)).free / (1024 ** 3)


def garantir_espaco(cfg, pasta, necessario_gb, forcar):
    livre = espaco_livre_gb(pasta)
    sobra = livre - necessario_gb
    if sobra < cfg["min_free_gb"] and not forcar:
        log.error(
            "Espaco insuficiente: livre %.1f GB, necessario ~%.1f GB, reserva minima %.1f GB.",
            livre, necessario_gb, cfg["min_free_gb"],
        )
        log.error("Abortado. Use --force para ignorar (nao recomendado).")
        return False
    log.info("Espaco: livre %.1f GB | estimativa do lote %.2f GB | sobra %.1f GB",
             livre, necessario_gb, max(sobra, 0))
    return True


def ler_jsonl(p):
    if not p.exists():
        return []
    itens = []
    for linha in p.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if linha:
            try:
                itens.append(json.loads(linha))
            except json.JSONDecodeError:
                pass
    return itens


def anexar_jsonl(p, obj):
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def fp_para_bytes(fingerprint):
    return struct.pack("<%dI" % len(fingerprint), *fingerprint)


def fp_de_bytes(bruto):
    n = len(bruto) // 4
    return list(struct.unpack("<%dI" % n, bruto[:n * 4]))


def calcular_fp(fpcalc, arquivo):
    try:
        saida = subprocess.run(
            [str(fpcalc), "-raw", "-json", "-length", "0", str(arquivo)],
            capture_output=True, text=True, timeout=300,
        )
        if saida.returncode != 0:
            return None
        dados = json.loads(saida.stdout)
        lista = dados.get("fingerprint") or []
        return {
            "duration": float(dados.get("duration") or 0),
            "list": lista,
            "packed": fp_para_bytes(lista),
        }
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        log.warning("fpcalc falhou em %s: %s", arquivo.name, exc)
        return None


def erro_fp(a, b, max_offset=16):
    if isinstance(a, (bytes, bytearray)):
        a = fp_de_bytes(a)
    if isinstance(b, (bytes, bytearray)):
        b = fp_de_bytes(b)
    if not a or not b:
        return 1.0
    if len(a) > len(b):
        a, b = b, a
    melhor = 1.0
    for off in range(0, min(max_offset, len(b) - len(a)) + 1):
        for sinal in ((1,) if off == 0 else (1, -1)):
            ini_b = off if sinal > 0 else 0
            ini_a = 0 if sinal > 0 else off
            n = min(len(a) - ini_a, len(b) - ini_b)
            if n <= 0:
                continue
            bits = 0
            for i in range(n):
                bits += (a[ini_a + i] ^ b[ini_b + i]).bit_count()
            erro = bits / (32.0 * n)
            if erro < melhor:
                melhor = erro
    return melhor


class Biblioteca:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state = caminho(cfg, "state_dir")
        self.state.mkdir(parents=True, exist_ok=True)
        self.catalogo_path = self.state / "catalogo.jsonl"
        self.arquivo_ids = self.state / "historico.txt"
        self.itens = ler_jsonl(self.catalogo_path)
        self.por_id = {i["yt_id"]: i for i in self.itens if i.get("yt_id")}
        self.fps = []
        for item in self.itens:
            if item.get("fp"):
                self.fps.append((item.get("duration") or 0, item, fp_de_bytes(base64.b64decode(item["fp"]))))

    def ja_existe_id(self, yt_id):
        if yt_id in self.por_id:
            return self.por_id[yt_id]
        if self.arquivo_ids.exists():
            alvo = "youtube " + yt_id
            with self.arquivo_ids.open("r", encoding="utf-8", errors="ignore") as f:
                for linha in f:
                    if linha.strip() == alvo:
                        return {"yt_id": yt_id, "status": "arquivo"}
        return None

    def ja_existe_meta(self, artista, titulo, duracao):
        alvo_art, alvo_tit = normalizar(artista), limpar_titulo(titulo)
        for item in self.itens:
            if item.get("status") == "duplicata":
                continue
            if abs((item.get("duration") or 0) - (duracao or 0)) > self.cfg["duration_tolerance"]:
                continue
            if normalizar(item.get("artist")) == alvo_art and limpar_titulo(item.get("title")) == alvo_tit:
                return item
        return None

    def duplicata_por_audio(self, fp, duracao, ignorar_id=None):
        candidatos = [t for t in self.fps if abs(t[0] - duracao) <= 8 and t[1].get("yt_id") != ignorar_id]
        for _, item, outro in candidatos:
            erro = erro_fp(fp, outro)
            if erro <= self.cfg["dup_threshold"]:
                item["_erro"] = round(erro, 3)
                return item
        return None

    def registrar(self, item):
        item = dict(item)
        if item.get("fp") is not None and not isinstance(item["fp"], str):
            item["fp"] = base64.b64encode(item["fp"]).decode("ascii")
        anexar_jsonl(self.catalogo_path, item)
        self.itens.append(item)
        if item.get("yt_id") and item.get("status") != "duplicata":
            self.por_id[item["yt_id"]] = item
            if item.get("fp"):
                self.fps.append((item.get("duration") or 0, item,
                                 fp_de_bytes(base64.b64decode(item["fp"]))))


ULTIMO_TITULO = ''


def expandir(url, cfg):
    global ULTIMO_TITULO
    import yt_dlp

    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": "in_playlist",
        "skip_download": True, "ignoreerrors": True, "noplaylist": False,
        **opcoes_cookies(cfg),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        return []
    ULTIMO_TITULO = str(info.get("title") or "")
    entradas = info.get("entries")
    if entradas is None:
        entradas = [info]
    saida = []
    for e in entradas:
        if not e:
            continue
        yt_id = e.get("id")
        if not yt_id:
            continue
        saida.append({
            "yt_id": yt_id,
            "title": e.get("title") or "",
            "duration": float(e.get("duration") or 0),
            "uploader": e.get("uploader") or e.get("channel") or "",
            "artist": e.get("artist") or "",
            "track": e.get("track") or "",
            "album": e.get("album") or "",
            "url": e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={yt_id}",
        })
    return saida


def baixar(item, cfg):
    import yt_dlp

    inbox = caminho(cfg, "inbox_dir")
    inbox.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bestaudio/best",
        "outtmpl": str(inbox / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "ignoreerrors": False,
        "download_archive": str(caminho(cfg, "state_dir") / "historico.txt"),
        "writethumbnail": True,
        "writeinfojson": True,
        "ffmpeg_location": cfg["ffmpeg_location"],
        "sleep_interval_requests": 1,
        **opcoes_cookies(cfg),
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": cfg["audio_format"],
             "preferredquality": cfg["audio_quality"]},
            {"key": "FFmpegMetadata", "add_metadata": True},
            {"key": "EmbedThumbnail"},
        ],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([item["url"]])
    arquivos = sorted(inbox.glob(item["yt_id"] + "." + cfg["audio_format"]))
    if not arquivos:
        candidatos = [p for p in inbox.glob(item["yt_id"] + ".*")
                      if p.suffix.lower() in (".mp3", ".m4a", ".opus", ".webm", ".flac")]
        if not candidatos:
            raise RuntimeError("download nao gerou arquivo de audio")
        arquivos = candidatos
    return arquivos[0]


def fonte_url(url):
    baixo = (url or "").lower()
    if "tiktok" in baixo:
        return "tiktok"
    if "instagram" in baixo:
        return "instagram"
    return "youtube"


def pontuar_candidato(consulta_tokens, cand, duracao, uploader):
    titulo_tokens = tokens_busca(cand.get("title"))
    uniao = consulta_tokens | titulo_tokens
    score = len(consulta_tokens & titulo_tokens) / len(uniao) if uniao else 0.0
    dur = float(cand.get("duration") or 0)
    if duracao and dur and abs(dur - duracao) <= 30:
        score += 0.15
    canal = normalizar(cand.get("channel") or cand.get("uploader") or "")
    origem = normalizar(uploader)
    if origem and origem in canal:
        score += 0.1
    return round(min(score, 1.0), 3)


def procurar(url, cfg):
    import yt_dlp

    fonte = fonte_url(url)
    vazio = {"encontrado": False, "url": url or "", "titulo": "", "fonte": fonte,
             "consulta": "", "candidatos": []}
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                               **opcoes_cookies(cfg)}) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception:
        return vazio
    if not info:
        return vazio
    titulo = info.get("title") or ""
    uploader = info.get("uploader") or info.get("channel") or ""
    duracao = float(info.get("duration") or 0)
    consulta = limpar_consulta(titulo, uploader, info.get("description") or "")
    base = {"encontrado": False, "url": info.get("webpage_url") or url, "titulo": titulo,
            "fonte": fonte, "consulta": consulta, "candidatos": []}
    if not consulta:
        return base
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                               "extract_flat": "in_playlist", "skip_download": True,
                               "ignoreerrors": True, **opcoes_cookies(cfg)}) as ydl:
            resultado = ydl.extract_info("ytsearch8:" + consulta, download=False)
    except Exception:
        return base
    consulta_tokens = tokens_busca(consulta)
    candidatos = []
    for e in (resultado or {}).get("entries") or []:
        if not e:
            continue
        candidatos.append({
            "url": e.get("webpage_url") or e.get("url")
            or f"https://www.youtube.com/watch?v={e.get('id')}",
            "titulo": e.get("title") or "",
            "canal": e.get("channel") or e.get("uploader") or "",
            "score": pontuar_candidato(consulta_tokens, e, duracao, uploader),
        })
    candidatos.sort(key=lambda c: c["score"], reverse=True)
    candidatos = candidatos[:5]
    return {**base, "encontrado": bool(candidatos), "candidatos": candidatos}


def baixar_video(url, cfg, saida=None):
    import yt_dlp

    pasta = caminho(cfg, "video_dir")
    pasta.mkdir(parents=True, exist_ok=True)
    base_nome = nome_seguro(saida, 90) if saida else "%(id)s"
    opts = {
        "format": ("bv*[height<=1080][ext=mp4]+ba[ext=m4a]/"
                   "b[height<=1080][ext=mp4]/b[ext=mp4]/best"),
        "merge_output_format": "mp4",
        "outtmpl": str(pasta / (base_nome + ".mp4")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": cfg["ffmpeg_location"],
        **opcoes_cookies(cfg),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise RuntimeError("nao foi possivel baixar o video")
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), None) or {}
    base_nome = nome_seguro(saida, 90) if saida else str(info.get("id") or "")
    if not base_nome:
        raise RuntimeError("nao foi possivel identificar o arquivo baixado")
    destino = pasta / f"{base_nome}.mp4"
    if not destino.exists():
        achados = sorted(pasta.glob(base_nome + ".*"))
        mp4 = [p for p in achados if p.suffix.lower() == ".mp4"]
        destino = (mp4 or achados or [destino])[0]
    if not destino.exists():
        raise RuntimeError("download nao gerou arquivo mp4")
    return destino.resolve()


def limpar_inbox(inbox, yt_id):
    for extra in inbox.glob(yt_id + ".*"):
        extra.unlink(missing_ok=True)


def gravar_tags(arquivo, artista, titulo, album, numero):
    try:
        from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK, error
    except Exception:  # noqa: BLE001
        return
    try:
        try:
            tag = ID3(arquivo)
        except error:
            tag = ID3()
        tag.setall("TIT2", [TIT2(encoding=3, text=titulo)])
        tag.setall("TPE1", [TPE1(encoding=3, text=artista)])
        tag.setall("TPE2", [TPE2(encoding=3, text=artista)])
        tag.setall("TALB", [TALB(encoding=3, text=album)])
        tag.setall("TRCK", [TRCK(encoding=3, text=str(numero))])
        tag.save(arquivo, v2_version=3)
    except Exception as exc:  # noqa: BLE001
        log.warning("nao consegui etiquetar %s: %s", arquivo.name, exc)


def importar(item, arquivo, cfg, bib):
    info_path = arquivo.with_suffix(".info.json")
    info = {}
    if info_path.exists():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            info = {}
    artista, titulo = separar_artista(info.get("title") or item["title"], {
        "artist": info.get("artist") or item.get("artist"),
        "track": info.get("track") or item.get("track"),
        "uploader": info.get("uploader") or item["uploader"],
        "channel": info.get("channel") or "",
    })
    album = (info.get("album") or item.get("album") or "").strip()
    if tem_versao(titulo) and not re.search(r"[\[\(]", titulo):
        titulo = f"{titulo} [{_marcador_versao(titulo)}]"
    if not album or album in ("Acervo",):
        album = "Singles"

    duracao = float(info.get("duration") or item["duration"] or 0)
    fpcalc = caminho(cfg, "fpcalc")
    fp = calcular_fp(fpcalc, arquivo) if fpcalc.exists() else None
    if fp and not duracao:
        duracao = fp["duration"]

    if fp:
        dup = bib.duplicata_por_audio(fp["list"], duracao, ignorar_id=item["yt_id"])
        if dup:
            limpar_inbox(arquivo.parent, arquivo.stem)
            bib.registrar({"yt_id": item["yt_id"], "status": "duplicata", "dup_of": dup["yt_id"],
                           "title": info.get("title") or item["title"],
                           "erro_fp": dup.get("_erro", ""),
                           "data": datetime.now().isoformat(timespec="seconds")})
            log.info("DUPLICATA (audio): %s == %s (erro %.3f)", item["title"], dup.get("title"), dup.get("_erro", 0))
            if dup.get("path"):
                print("DUP_ARQUIVO: " + str(caminho(cfg, "music_dir") / dup["path"]))
            return "duplicata", dup

    music = caminho(cfg, "music_dir")
    destino_dir = music / nome_seguro(artista) / nome_seguro(album)
    destino_dir.mkdir(parents=True, exist_ok=True)
    numero = 1
    existentes = {p.name for p in destino_dir.glob("*.mp3")}
    while any(n.startswith(f"{numero:02d} - ") for n in existentes):
        numero += 1
    destino = destino_dir / f"{numero:02d} - {nome_seguro(titulo)}.{cfg['audio_format']}"
    shutil.move(str(arquivo), str(destino))
    gravar_tags(destino, artista, titulo, album, numero)
    if not cfg["keep_info_json"]:
        info_path.unlink(missing_ok=True)
    limpar_inbox(caminho(cfg, "inbox_dir"), arquivo.stem)

    bib.registrar({
        "yt_id": item["yt_id"], "status": "ok", "artist": artista, "title": titulo,
        "album": album, "duration": round(duracao, 1), "source": item["url"],
        "path": str(destino.relative_to(music)), "fp": fp["packed"] if fp else None,
        "data": datetime.now().isoformat(timespec="seconds"),
    })
    log.info("OK: %s - %s -> %s", artista, titulo, destino.relative_to(music))
    return "ok", destino


def _marcador_versao(texto):
    baixo = normalizar(texto)
    for chave, rotulo in (("live", "Live"), ("ao vivo", "Ao Vivo"), ("acoustic", "Acoustic"),
                          ("acustico", "Acustico"), ("remix", "Remix"), ("instrumental", "Instrumental"),
                          ("cover", "Cover"), ("demo", "Demo"), ("sped up", "Sped Up"),
                          ("slowed", "Slowed"), ("unplugged", "Unplugged")):
        if chave in baixo:
            return rotulo
    return "Versao"


def estimar_gb(itens):
    return sum((i.get("duration") or 240) * 40000 for i in itens) / (1024 ** 3) * 1.15


def processar(urls, cfg, args):
    bib = Biblioteca(cfg)
    inbox = caminho(cfg, "inbox_dir")
    inbox.mkdir(parents=True, exist_ok=True)

    candidatos, vistos = [], set()
    for url in urls:
        log.info("Lendo: %s", url)
        for e in expandir(url, cfg):
            if e["yt_id"] in vistos:
                continue
            vistos.add(e["yt_id"])
            artista, titulo = separar_artista(e["title"], e)
            e["_artista"], e["_titulo"] = artista, titulo
            candidatos.append(e)

    if not candidatos:
        log.error("Nenhum item encontrado nos links.")
        return 1
    log.info("Playlist/links: %d itens", len(candidatos))

    baixar_lista = []
    skip = {"id": 0, "meta": 0}
    for e in candidatos:
        if bib.ja_existe_id(e["yt_id"]):
            skip["id"] += 1
            log.info("ja baixado (id)   : %s", e["title"])
            continue
        igual = bib.ja_existe_meta(e["_artista"], e["_titulo"], e["duration"])
        if igual:
            skip["meta"] += 1
            log.info("ja temos (metadado): %s  ~  %s - %s", e["title"], igual.get("artist"), igual.get("title"))
            continue
        baixar_lista.append(e)

    if len(baixar_lista) > cfg["max_downloads"]:
        log.warning("Limitando a %d downloads (de %d). Use --max para mudar.",
                    cfg["max_downloads"], len(baixar_lista))
        baixar_lista = baixar_lista[:cfg["max_downloads"]]

    estimativa = estimar_gb(baixar_lista)
    log.info("A baixar: %d | ja tinham: %d | estimativa: %.2f GB",
             len(baixar_lista), skip["id"] + skip["meta"], estimativa)
    if not garantir_espaco(cfg, caminho(cfg, "music_dir"), estimativa, args.force):
        return 2
    if args.dry_run:
        for e in baixar_lista:
            log.info("  [dry-run] %s  (%s)", e["title"], e["url"])
        return 0

    resultados = {"ok": 0, "duplicata": 0, "erro": 0}
    for i, e in enumerate(baixar_lista, 1):
        livre = espaco_livre_gb(caminho(cfg, "music_dir"))
        if livre < cfg["min_free_gb"]:
            log.error("Espaco acabou (%.1f GB livres). Parando para nao lotar o disco.", livre)
            break
        log.info("[%d/%d] %s", i, len(baixar_lista), e["title"])
        try:
            arquivo = baixar(e, cfg)
            estado, _ = importar(e, arquivo, cfg, bib)
            resultados[estado] += 1
        except Exception as exc:
            resultados["erro"] += 1
            log.error("FALHOU: %s -> %s", e["title"], exc)
            with (caminho(cfg, "state_dir") / "falhas.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"yt_id": e["yt_id"], "title": e["title"], "erro": str(exc),
                                    "data": datetime.now().isoformat(timespec="seconds")},
                                   ensure_ascii=False) + "\n")

    log.info("=" * 60)
    log.info("Resumo: %d novas | %d duplicatas descartadas | %d falhas | %d ja tinhamos",
             resultados["ok"], resultados["duplicata"], resultados["erro"], skip["id"] + skip["meta"])
    log.info("Biblioteca: %d faixas | %.1f GB livres em %s",
             sum(1 for i in bib.itens if i.get("status") == "ok"),
             espaco_livre_gb(caminho(cfg, "music_dir")), caminho(cfg, "music_dir"))
    return 0


def status(cfg):
    bib = Biblioteca(cfg)
    ok = [i for i in bib.itens if i.get("status") == "ok"]
    total = sum(Path(caminho(cfg, "music_dir") / i["path"]).stat().st_size
                for i in ok if (caminho(cfg, "music_dir") / i["path"]).exists())
    print(f"Faixas na biblioteca : {len(ok)}")
    print(f"Duplicatas evitadas  : {sum(1 for i in bib.itens if i.get('status') == 'duplicata')}")
    print(f"Tamanho da biblioteca: {total / (1024**3):.2f} GB")
    print(f"Espaco livre         : {espaco_livre_gb(caminho(cfg, 'music_dir')):.1f} GB")
    print(f"Livro                : {bib.catalogo_path}")


def autoteste(cfg):
    falhas = 0

    def checar(nome, obtido, esperado):
        nonlocal falhas
        ok = obtido == esperado
        print(f"  {'ok   ' if ok else 'FALHA'} {nome}" + ("" if ok else f": {obtido!r} != {esperado!r}"))
        if not ok:
            falhas += 1

    checar("junk no titulo", limpar_titulo("Evidencias (Official Music Video) [HD]"), "evidencias")
    checar("acentos", limpar_titulo("Evidências"), "evidencias")
    checar("mantem versao", tem_versao("Evidencias (Ao Vivo)"), True)
    checar("remaster nao e versao", tem_versao("Evidencias (Remastered 2011)"), False)
    checar("artista - titulo", separar_artista("Legiao Urbana - Tempo Perdido (Clipe Oficial)", {})[0],
           "Legiao Urbana")
    checar("descricao com by", separar_artista("Sintel - Open Movie by Blender Foundation", {})[1],
           "Sintel - Open Movie by Blender Foundation")
    checar("canal Topic", separar_artista("Tempo Perdido", {"uploader": "Legiao Urbana - Topic"})[0],
           "Legiao Urbana")
    checar("nome seguro", nome_seguro('AC/DC: Back?*'), "ACDC Back")
    checar("consulta limpa",
           limpar_consulta("Musica #tag @user https://x.com/a 🔥", "Canal"),
           "Musica Canal")
    checar("consulta vazia usa uploader", limpar_consulta("", "Artista Y"), "Artista Y")
    a = [1, 2, 3, 4, 5, 6, 7, 8]
    checar("fp igual", round(erro_fp(a, a), 3), 0.0)
    checar("fp deslocado", round(erro_fp(a, [0, 0] + a), 3), 0.0)
    checar("fp diferente", round(erro_fp(a, [0xFFFFFFFF ^ x for x in a]), 3), 1.0)
    checar("fp bytes", round(erro_fp(fp_para_bytes(a), fp_para_bytes(a)), 3), 0.0)
    checar("arquivo ida e volta", fp_de_bytes(fp_para_bytes(a)), a)
    print("Tudo certo." if not falhas else f"{falhas} falha(s).")
    return 0 if not falhas else 1


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except AttributeError:
        pass

    ap = argparse.ArgumentParser(
        description="Garimpa playlists/videos, evita duplicatas e organiza a biblioteca.")
    ap.add_argument("urls", nargs="*", help="links de video ou playlist")
    ap.add_argument("--arquivo", help="arquivo .txt com um link por linha")
    ap.add_argument("--dry-run", action="store_true", help="so analisa, nao baixa")
    ap.add_argument("--max", type=int, default=0, help="limite de downloads nesta rodada")
    ap.add_argument("--force", action="store_true", help="ignora a checagem de espaco")
    ap.add_argument("--status", action="store_true", help="mostra o estado da biblioteca")
    ap.add_argument("--autoteste", action="store_true", help="testa as funcoes internas")
    ap.add_argument("--listar", metavar="URL", help="so lista as faixas do link em JSON")
    ap.add_argument("--procurar", metavar="URL", help="busca o video no YouTube sem baixar")
    ap.add_argument("--video", metavar="URL", help="baixa o video em mp4 para a pasta videos/")
    ap.add_argument("--saida", metavar="NOME", help="nome do arquivo de video (sem extensao)")
    args = ap.parse_args()

    cfg = carregar_config()
    for chave in ("music_dir", "inbox_dir", "state_dir", "log_dir"):
        caminho(cfg, chave).mkdir(parents=True, exist_ok=True)

    log_path = caminho(cfg, "log_dir") / f"garimpo-{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler(log_path, encoding="utf-8")],
    )

    if args.autoteste:
        return autoteste(cfg)
    if args.status:
        status(cfg)
        return 0
    if args.listar:
        itens = expandir(args.listar, cfg)
        print(json.dumps({"titulo": ULTIMO_TITULO, "itens": itens}, ensure_ascii=False))
        return 0
    if args.procurar:
        print(json.dumps(procurar(args.procurar, cfg), ensure_ascii=False))
        return 0
    if args.video:
        try:
            arquivo = baixar_video(args.video, cfg, args.saida)
        except Exception as exc:
            print(f"Erro: {exc}", file=sys.stderr)
            return 1
        print(f"ARQUIVO: {arquivo}")
        return 0

    urls = list(args.urls)
    if args.arquivo:
        urls += [l.strip() for l in Path(args.arquivo).read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
    if not urls:
        ap.print_help()
        return 1
    return processar(urls, cfg, args)


if __name__ == "__main__":
    sys.exit(main())
