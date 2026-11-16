# Your certificates for app.tyagi.fun go here

Put these two files in this folder on the server, before docker compose up:

- fullchain.pem : your certificate, full chain, for app.tyagi.fun
- privkey.pem : your private key for that certificate

If your files are named differently, for example app.tyagi.fun.crt and
app.tyagi.fun.key, either rename them to fullchain.pem and privkey.pem
when you copy them here, or edit nginx/default.conf to use your names.

Do NOT send the private key in chat, and do NOT commit this folder to
a public git repo. The private key stays only on your server.
