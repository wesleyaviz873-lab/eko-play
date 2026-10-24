# Segurança e autenticação

## Modelo

Dois papéis: **admin** e **usuário**.

- Cada aparelho tem uma **chave de 256 bits** (o servidor guarda só o SHA-256).
- O aparelho é ligado a uma **pessoa** (nome/e-mail) e a um **papel**.
- `admin` pode aprovar, promover, rebaixar e remover.
- O **primeiro admin** é criado no app com a **senha do servidor** (`token`).
- Proteções: não dá para remover/rebaixar o último admin; remover uma pessoa
  apaga também o e-mail da lista de autorizados (senão ela voltaria no login).

## Duas formas de entrar

### 1) Simples (senha do servidor)

- Primeiro acesso: `POST /api/setup` com nome, e-mail e `token`.
- Só funciona enquanto **não existe** nenhum admin (`GET /api/estado` informa).
- Depois, cada pessoa entra pelo app e fica **pendente** até o admin aprovar.

### 2) Google (recomendado)

- O dono cria um **Android OAuth client** (pacote do app + SHA-1 do certificado)
  e um **Web client** no mesmo projeto do Google Cloud.
- O app pede o ID token ao Google (Credential Manager, com nonce).
- O servidor valida a assinatura, `aud`, `email_verified` e o nonce antes de aceitar.
- E-mail na lista `autorizados` → entra como **admin**; já aprovado → **usuário**;
  senão → pedido pendente.

> Sem o cliente Android registrado o Google responde `28444`; com a tela de
> consentimento em "Testando", contas fora dos testes recebem `28433`.

## Camadas de proteção

- HTTPS no túnel (Cloudflare/ngrok) + cabeçalhos `nosniff`, `X-Frame-Options`,
  `Referrer-Policy`, HSTS.
- Limite de requisições por IP e bloqueio temporário após tentativas de senha.
- Corpo de requisição limitado, timeout de conexão, upload de links restrito a
  YouTube/TikTok/Instagram (evita SSRF).
- `/api/cookies` só para admin; download de arquivo exige token.
- O app não aceita HTTP fora da rede local e ignora URLs externas.

## O que NUNCA sai da sua máquina

- Acervo, catálogo, histórico, senhas (hash) e cookies ficam locais.
- O servidor só faz requisições ao YouTube (download) e ao Google (validação do
  login). Nenhum outro destino.
- Não há telemetria, anúncio ou código de terceiros no app além das bibliotecas
  listadas no `build.gradle.kts`.

## Checklist para publicar

- [ ] `config.json` fora do Git (token, senhas)
- [ ] `cookies.txt`, `state/`, `music/` fora do Git
- [ ] `local.properties`/`keystore.properties` fora do Git
- [ ] `google_client_id` no exemplo em branco
- [ ] revisar os logs (`logs/`) antes de anexar em issue
