# Instalação no computador

Testado em Windows 11 e Debian/Ubuntu. Você precisa de ~15 minutos.

## 1. O que instalar

| Item | Para quê | Onde |
|---|---|---|
| Python 3.11+ | rodar o servidor | python.org (marque "Add to PATH") |
| ffmpeg | converter o áudio | ffmpeg.org (Windows: pasta `C:\ffmpeg\bin`) |
| fpcalc | impressão digital (dedupe) | chromaprint (binário `fpcalc.exe`) |
| Navidrome | catálogo e streaming | navidrome.org |
| (opcional) Caddy/ngrok/Cloudflare | acesso de fora | veja `TUNEL.md` |

## 2. Código e dependências

```bash
git clone https://github.com/SEU-USUARIO/eko.git
cd eko/server
python -m venv .venv
# Windows:
.venv\Scripts\pip install -r requirements.txt
# Linux:
.venv/bin/pip install -r requirements.txt
```

## 3. Configuração

Copie `server/config.example.json` para `server/config.json` e preencha:

- **`token`**: invente uma senha longa (`openssl rand -hex 20`). É a senha do
  administrador e do primeiro acesso.
- **`ffmpeg_location`**: onde está o `ffmpeg.exe` (ex.: `C:\\ffmpeg\\bin`).
- **`fpcalc`**: caminho do `fpcalc` (ex.: `bin/fpcalc.exe`).
- **`navidrome_url` / `navidrome_usuario` / `navidrome_senha`**: o endereço do
  Navidrome e uma conta **admin** dele (o servidor usa para criar as contas das
  pessoas e disparar a varredura).
- **`google_client_id`** (opcional): o Web Client ID do seu projeto no Google
  Cloud, se quiser login com Google (veja `SEGURANCA.md`).
- **`autorizados`**: os e-mails que já entram como admin (pode deixar vazio e
  criar o primeiro admin pelo app).

> `config.json` contém segredos. Ele **não** vai para o Git (já está no
> `.gitignore`).

## 4. Subir o servidor

```bash
cd server
.venv\Scripts\python servidor.py     # Windows
# .venv/bin/python servidor.py       # Linux
```

Deve aparecer `EKO no ar` e a porta `8787`.

## 5. Navidrome

Aponte o `MusicFolder` do Navidrome para a pasta `music/` do EKO e suba o
Navidrome (porta sugerida: 8090). Para as playlists automáticas funcionarem, o
Navidrome precisa de:

```toml
MusicFolder = '/caminho/eko/music'
PlaylistsPath = '_playlists'
```

O servidor grava os `.m3u` em `music/_playlists/` e chama a varredura sozinho.

## 6. Primeiro administrador

Abra o app no celular (veja `APP.md`). Na primeira vez o app mostra
**"Primeiro acesso"**: nome, e-mail e a **senha do servidor** (o `token`). Pronto:
você é o administrador.

Depois disso, em **Ajustes → Administração**:

- **Gerenciar**: aprovar/negar quem pediu acesso, ver quem está autorizado,
  remover.
- **Compartilhar o EKO**: manda o link de instalação.
- **Login do YouTube**: necessário só para baixar playlists privadas.

## 7. Atualizações

O app avisa quando há versão nova (compara com `apk/versao.json`). Para publicar
uma versão: gere o APK e copie para `apk/eko.apk`, atualizando o `versao.json`.
