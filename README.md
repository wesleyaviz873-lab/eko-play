# EKO — música da família, num lugar só

Servidor de música **privado** para a sua casa: baixa músicas do YouTube para o seu
computador, organiza sozinho (sem duplicatas) e deixa todo mundo ouvir no celular,
com um app Android próprio.

Não é um serviço na nuvem: **roda na sua máquina**. Nada de assinatura, nada de
anúncio, e ninguém entra sem a sua autorização.

```
 celular (app EKO)                    seu computador
┌───────────────────┐   HTTPS   ┌────────────────────────────┐
│ navegador YouTube │ ────────► │ motor (servidor.py)        │
│ botão Baixar      │           │  ├─ yt-dlp (baixa)         │
│ player + downloads│ ◄──────── │  ├─ dedupe + catálogo      │
└───────────────────┘  streaming│  └─ Navidrome (acervo)     │
                                │       └─ music/  (2+ GB)   │
                                └────────────────────────────┘
```

## O que ele faz

- **Baixar música** (ou vídeo) direto do celular, pelo navegador do app.
- **Playlist inteira** de uma vez — e ela vira uma playlist de verdade no app.
- **Nunca baixa duas vezes**: trava por ID do vídeo + impressão digital do áudio.
- **Acervo organizado**: `Artista/Álbum/faixa.mp3` com capa e metadados.
- **Playlists automáticas**: tudo que a pessoa baixou aparece em
  `Baixadas - <nome>` (um `.m3u` que o Navidrome importa).
- **Administração no app**: aprovar quem entra, promover outro admin, remover.
- **Dois jeitos de entrar**: conta Google (recomendado) ou cadastro + aprovação.

## Segurança (o que garante que só a sua família usa)

- Cada aparelho recebe uma **chave aleatória de 256 bits**; o servidor guarda só o hash.
- Quem entra pela primeira vez fica **pendente** até você aprovar no app.
- Lista de e-mails autorizados, remoção de aparelho, proteção do último admin.
- Limite de tentativas, bloqueio por IP, HTTPS e cabeçalhos de segurança.
- Nada de anúncio, telemetria ou analytics. O app só fala com **o seu** servidor.

## Começando

- [Instalação no computador](docs/INSTALACAO.md) (Windows ou Linux)
- [App no celular](docs/APP.md)
- [Segurança e autenticação](docs/SEGURANCA.md)
- [Acesso de fora de casa (ngrok / Cloudflare / só local)](docs/TUNEL.md)
- [Instalar conversando com uma IA](docs/PROMPT-IA.md)

## Estrutura

```
server/    servidor.py (API + fila + auth) e garimpar.py (motor de download)
app/       app Android (Kotlin + Compose): navegador, download e player
docs/      instalação, segurança, túnel, prompt para IA
```

## Aviso

Baixar conteúdo do YouTube pode contrariar os Termos de Uso do serviço. Use apenas
com conteúdo que você tem direito de baixar (a sua própria biblioteca, domínio
público ou licenças permissivas). O projeto não hospeda nem distribui mídia.

## Licença

MIT — veja [LICENSE](LICENSE).
