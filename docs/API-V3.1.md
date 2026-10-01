# V3.1 API additions

`GET /api/rooms/{room_id}/games/{game_id}/export` requires the usual Bearer session token. Only the historical game's original host may export a completed game, including after rematch. Response is `application/x-ndjson`, `Cache-Control: no-store`, schema 1.0.0. Missing session: 401; another identity: 403; unavailable completed game: 404. No active-game private export is provided.

The stream includes metadata, role/model assignment, stored public/private events, calls/outcomes and result. It explicitly omits keys, ownership/credential references, password/session/recovery data and provider hidden reasoning. Old games have null seed and cannot retrospectively provide prompts not captured at play time. CLI-created experiments capture model prompts and outputs directly in their private JSONL.

`GET /benchmark` serves a separate local-file dashboard; its JSON import never uploads a file. Experiment creation/run/cancel remain CLI-only in V3.1. Local CLI artifacts are not exposed by a public experiment API.

Existing `/analysis`, `/replay`, `/games`, room actions, credentials and WebSockets stay compatible. `/replay` retains public audience filtering; research export does not broaden it. `/api/health.version` is 3.1.0.
