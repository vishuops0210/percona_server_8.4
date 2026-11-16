# Verified Restore: product mock website

A React mock of the Verified Restore product: a dashboard that proves
multi-database backups work by actually restoring them. All data in the
app is sample data. There is no backend and no real database connection.

## What is inside

- Landing hero with the product one-liner and the three pitch pillars.
- Interactive dashboard mock: sources list, verified stamp, RPO picker
  (5 min / 15 min / 1 hour / 24 hours), point-in-time restore timeline,
  restore target selector, and a live demo restore log.
- Engines covered with honest phasing (Phase 1, Phase 2, on demand).
- How verification works: the five-step proof flow.

## Run it with Docker (HTTPS, mock.tyagi.fun)

1. Put your SSL certificates in the `certs/` directory:
   - `certs/fullchain.pem`
   - `certs/privkey.pem`
2. Point the DNS for `mock.tyagi.fun` at this host.
3. Build and start:
   ```bash
   docker compose up -d --build
   ```
4. Open https://mock.tyagi.fun

## Run it locally without SSL (plain HTTP, for testing)

Option A: use the override file that serves HTTP only:

```bash
docker compose -f docker-compose.yml -f docker-compose.http.yml up -d --build
```

Then open http://localhost:8080

Option B: run the dev server directly (no Docker):

```bash
npm install
npm run dev
```

Then open the address it prints (usually http://localhost:5173).

## Notes

- The `certs/` directory is empty on purpose. Certificates are yours;
  never commit real certificate files to a repo.
- The Nginx config redirects HTTP to HTTPS. For plain-HTTP local
  testing, use the `docker-compose.http.yml` override, which swaps in
  the HTTP-only Nginx config.
- To rebuild after changing the app: `docker compose up -d --build`
- To stop: `docker compose down`
