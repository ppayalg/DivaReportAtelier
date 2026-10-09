# HTTP API

Start with `diva-atelier serve` (or `make serve`). JSON in, JSON out. Errors are always
`{"error": "..."}` with a useful status: 400 bad request, 401 missing token, 404 not found,
405 wrong method, 422 invalid configuration or inputs (with `issues` or `problems`), 502 model
provider failure.

If `DIVA_API_TOKEN` is set, send `Authorization: Bearer <token>` on every `/api` route except
`/api/health`. Request bodies are capped at 1 MB.

| Method and path | Purpose |
| --- | --- |
| `GET /api/health` | Liveness and active provider. |
| `GET /api/configs` | All configurations, keyed by name. |
| `GET /api/configs/<key>` | One configuration. |
| `PUT /api/configs/<key>` | Save a configuration. 422 with `issues` if it has errors. |
| `POST /api/configs/<key>/validate` | Validate a body without saving. |
| `POST /api/configs/<key>/reports` | Generate. Body: `{"variables": {...}, "author": "priya", "autoApprove": false}`. Returns 201 and the report. |
| `GET /api/reports` / `GET /api/reports/<id>` | List and fetch reports. |
| `GET /api/reports/<id>/render?format=html\|md` | Rendered document. |
| `POST /api/reports/<id>/approvals` | Body: `{"requestId", "user", "role", "action": "approve\|reject", "comment"}`. |
| `POST /api/configs/<key>/feedback` | Add `{"type": "comment", "section", "text", ...}` or `{"type": "edit", "section", "before", "after", ...}`. |
| `GET /api/configs/<key>/insights` | Open recommendations from stored feedback. |
| `POST /api/configs/<key>/insights/<rec-id>/apply` | Apply one recommendation to the configuration. |

```bash
curl -X POST localhost:8000/api/configs/bridal/reports \
  -H 'Content-Type: application/json' \
  -d "{\"variables\": $(cat examples/bridal_vars.json), \"author\": \"priya\"}"
```
