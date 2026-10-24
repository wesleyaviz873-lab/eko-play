# Acessar de fora de casa

O app só precisa de um endereço **HTTPS** que chegue até a porta `8787` do seu
computador. Três opções, da mais simples à mais completa.

## 1. Só na sua casa (LAN) — para testar rápido

Não precisa de nada externo.

- Descubra o IP do computador (ex.: `192.168.1.50`).
- No app, use `http://192.168.1.50:8787`.
- Funciona só na mesma Wi-Fi.

O app aceita HTTP apenas para endereços privados (`192.168.*`, `10.*`, `100.*`,
`127.0.0.1`).

> Para usar de verdade, prefira HTTPS — o Android mostra o app como seguro e o
> Chrome não bloqueia o download do APK.

## 2. Cloudflare Tunnel (recomendado)

Grátis, com domínio próprio e HTTPS de verdade.

1. Instale o `cloudflared` e rode `cloudflared tunnel login`.
2. `cloudflared tunnel create eko`
3. `~/.cloudflared/config.yml`:
   ```yaml
   tunnel: SEU-TUNNEL-ID
   credentials-file: /caminho/SEU-TUNNEL-ID.json
   ingress:
     - hostname: eko.seudominio.com
       service: http://localhost:8787
     - service: http_status:404
   ```
4. `cloudflared tunnel route dns SEU-TUNNEL-ID eko.seudominio.com`
5. `cloudflared tunnel run`

Pronto: `https://eko.seudominio.com`. O APK baixa sem bloqueio do Chrome.

## 3. ngrok

Bom para demonstração; o endereço grátis muda quando reinicia (a não ser que você
tenha um domínio estático no plano).

```bash
ngrok http 8787
```

Use a URL `https://....ngrok-free.app` no app.

> Se usar ngrok, o endereço muda: atualize `EKO_SERVER_URL` no app, ou use um
> domínio reservado.

## Dica de segurança

- Deixe o servidor acessível **só** pelo túnel: ele pode continuar escutando em
  `127.0.0.1` e o túnel aponta para `localhost` (ninguém da rede entra direto).
- Se expor na internet, mantenha o `token` longo e revise a lista `autorizados`.
- O Cloudflare permite ainda "Zero Trust / Access" (login por e-mail) por cima,
  sem mudar o app.
