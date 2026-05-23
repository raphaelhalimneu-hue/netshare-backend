# MyApp Backend

Einfaches FastAPI Backend – ready to deploy auf Render.com 🚀

## Endpoints

| Methode | Route | Auth | Beschreibung |
|---------|-------|------|--------------|
| POST | `/api/auth/register` | ❌ | Neuen User anlegen (email, password, username) |
| POST | `/api/auth/login` | ❌ | Login – gibt JWT zurück |
| GET | `/api/dashboard` | ✅ JWT | User-Daten abrufen |
| POST | `/api/sharing/start` | ✅ JWT | Sharing starten |
| POST | `/api/sharing/stop` | ✅ JWT | Sharing stoppen |
| GET | `/health` | ❌ | Health Check für Render |

## Deployment auf Render.com

1. Erstelle ein **GitHub Repo** und pushe diese Dateien
2. Geh auf [render.com](https://render.com) → **New Web Service**
3. Verbinde dein GitHub Repo
4. Render erkennt `render.yaml` automatisch, oder manuell:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn server:app --host 0.0.0.0 --port 8000`
5. Füge unter **Environment** hinzu:
   - `SECRET_KEY` = ein beliebiger langer String
6. Deploy! ✅

Deine API ist dann erreichbar unter:
```
https://myapp-backend.onrender.com
```

## Lokal testen

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

Dann öffne http://localhost:8000/docs für die Swagger UI.

## Beispiel: Register + Login mit curl

```bash
# Register
curl -X POST http://localhost:8000/api/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email":"test@test.de","password":"geheim123","username":"testuser"}'

# Login (OAuth2 Form)
curl -X POST http://localhost:8000/api/auth/login \
  -d "username=test@test.de&password=geheim123"

# Dashboard (Token einsetzen)
curl http://localhost:8000/api/dashboard \
  -H "Authorization: Bearer DEIN_TOKEN"
```
