use axum::{
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    middleware,
    response::{IntoResponse, Response},
    routing::{delete, get, post},
    Json, Router,
};
use once_cell::sync::Lazy;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value as JsonValue};
use sqlx::{postgres::PgPoolOptions, Pool, Postgres, Row};
use std::{env, net::SocketAddr, path::PathBuf, fs};
use tokio::net::TcpListener;
use tracing::{info, warn, error, debug, Level, instrument};
use tracing_subscriber::EnvFilter;
use std::time::Duration;
use reqwest::Client;
use bollard::Docker;
use bollard::query_parameters::{ListContainersOptionsBuilder, LogsOptionsBuilder};
use bollard::container::LogOutput;
use chrono::{NaiveDate, TimeZone, Utc};
use futures::StreamExt;

static AUTH_TOKEN: Lazy<String> = Lazy::new(|| env::var("AUTH_TOKEN").unwrap_or_default());
static APP_USER:  Lazy<String> = Lazy::new(|| env::var("APP_USER").unwrap_or_default());
static APP_PASS:  Lazy<String> = Lazy::new(|| env::var("APP_PASS").unwrap_or_default());
static STATIC_DIR: Lazy<PathBuf> = Lazy::new(|| {
    env::var("STATIC_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("/app/static"))
});
static WHATSAPP_BOT_URL: Lazy<String> = Lazy::new(|| {
    env::var("WHATSAPP_BOT_URL").unwrap_or_else(|_| "http://whatsapp_bot:8000".to_string())
});
static HTTP: Lazy<Client> = Lazy::new(|| {
    Client::builder().use_rustls_tls().build().expect("reqwest client")
});

#[derive(Clone, Debug)]
struct AppState {
    db: Pool<Postgres>,
}

#[derive(Serialize, Deserialize, Debug)]
struct User {
    id: Option<i32>,
    phone: String,
    email: String,
    name: Option<String>,
    role: String,
}

#[derive(Deserialize)]
struct LoginReq { username: String, password: String }
#[derive(Serialize)]
struct LoginResp { token: String }

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Logs
    let filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));
    tracing_subscriber::fmt().with_env_filter(filter).with_max_level(Level::INFO).init();
    info!("starting server bootstrap…");

    // --- DB: conexión perezosa + reintentos para disponibilidad ---
    let db_url = env::var("DATABASE_URL").expect("DATABASE_URL is required");
    let pool = PgPoolOptions::new()
        .max_connections(10)
        .connect_lazy(&db_url)?; // no conecta aún

    // Tocar la conexión con reintentos (hasta 20 s)
    let mut ready = false;
    for _ in 0..20 {
        match pool.acquire().await {
            Ok(_) => { info!("Postgres ready"); ready = true; break; }
            Err(e) => { tracing::warn!("Postgres not ready yet: {e}"); tokio::time::sleep(Duration::from_secs(1)).await; }
        }
    }
    if !ready {
        tracing::warn!("Proceeding without DB confirmed ready; will attempt table creation anyway.");
    }

    let state = AppState { db: pool };

    // Rutas públicas (frontend + login + health)
    let public = Router::new()
        .route("/", get(serve_index))
        .route("/index.html", get(serve_index))
        .route("/users", get(|| async { serve_static_path("users.html") }))
        .route("/logs",  get(|| async { serve_static_path("logs.html") }))  // <-- NUEVO
        .route("/styles/{*path}", get(serve_file))
        .route("/scripts/{*path}", get(serve_file))
        .route("/images/{*path}", get(serve_file))
        .route("/api/login", post(login))
        .route("/healthz", get(|| async { "ok" }));

    // Rutas protegidas (CRUD users) con middleware de auth
    let protected = Router::new()
        .route("/api/users", get(list_users).post(create_user))
        .route("/api/users/{id}", delete(delete_user))
        .route("/wabot/loginqr", post(wabot_loginqr))
        // --- logs vía Docker ---
        .route("/api/dlogs/services", get(dlogs_services))
        .route("/api/dlogs/view", get(dlogs_view))
        .layer(middleware::from_fn_with_state(state.clone(), auth_mw));

    let app = public.merge(protected).with_state(state);

    // Servir (BLOQUEA)
    let addr: SocketAddr = ([0, 0, 0, 0], 8080).into();
    let listener = TcpListener::bind(addr).await?;
    info!("listening on http://{}", addr);
    axum::serve(listener, app).await?;
    Ok(())
}

/* ---------- middleware de auth ---------- */
#[instrument(level = "info", skip_all, fields(path=%req.uri().path(), method=%req.method()))]
async fn auth_mw(
    State(_state): State<AppState>,
    req: axum::http::Request<axum::body::Body>,
    next: middleware::Next,
) -> axum::response::Response {
    let token_present = req.headers().get("x-auth").is_some();
    let ok = req
        .headers()
        .get("x-auth")
        .and_then(|v| v.to_str().ok())
        .map(|v| v == AUTH_TOKEN.as_str())
        .unwrap_or(false);

    if ok {
        debug!("auth ok (X-Auth presente: {token_present})");
        next.run(req).await
    } else {
        warn!("auth FAIL (X-Auth presente: {token_present})");
        (StatusCode::UNAUTHORIZED, "missing/invalid X-Auth").into_response()
    }
}


/* ---------- estáticos ---------- */
async fn serve_index() -> impl IntoResponse {
    serve_static_path("index.html")
}

#[instrument(level="info", skip_all)]
async fn serve_file(axum::extract::OriginalUri(uri): axum::extract::OriginalUri) -> impl IntoResponse {
    let path = uri.path().trim_start_matches('/');
    debug!(%path, "serve_file");
    serve_static_path(path)
}

#[instrument(level="info", skip_all, fields(rel=%rel))]
fn serve_static_path(rel: &str) -> axum::response::Response {
    let mut full = STATIC_DIR.join(rel);
    if rel.ends_with('/') || full.is_dir() {
        full = full.join("index.html");
    }
    let full_str = full.display().to_string();

    match fs::read(&full) {
        Ok(bytes) => {
            debug!(%full_str, size=bytes.len(), "static hit");
            let mime = mime_guess::from_path(&full).first_or_octet_stream();
            (
                [(axum::http::header::CONTENT_TYPE, mime.as_ref())],
                bytes,
            ).into_response()
        }
        Err(e) => {
            warn!(%full_str, error=%e, "static miss");
            (StatusCode::NOT_FOUND, "not found").into_response()
        }
    }
}

/* ---------- login ---------- */
#[instrument(level="info", skip_all, fields(user=%body.username))]
async fn login(Json(body): Json<LoginReq>) -> impl IntoResponse {
    if body.username == *APP_USER && body.password == *APP_PASS {
        info!("login ok");
        (StatusCode::OK, Json(LoginResp { token: AUTH_TOKEN.clone() })).into_response()
    } else {
        warn!("login fail");
        (StatusCode::UNAUTHORIZED, "invalid credentials").into_response()
    }
}

/* ---------- users CRUD ---------- */
#[instrument(level="info", skip(state))]
async fn list_users(State(state): State<AppState>) -> impl IntoResponse {
    info!("/api/users GET");
    match sqlx::query(r#"SELECT id, phone, email, name, role FROM users ORDER BY id ASC"#)
        .fetch_all(&state.db).await
    {
        Ok(rows) => {
            info!(count=rows.len(), "users fetched");
            let users: Vec<User> = rows.into_iter().map(|r| User {
                id: Some(r.get::<i32, _>("id")),
                phone: r.get("phone"),
                email: r.get("email"),
                name: r.try_get("name").ok(),
                role: r.get("role"),
            }).collect();
            axum::Json(users).into_response()
        }
        Err(e) => {
            error!(error=%e, "list_users error");
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
    }
}

#[derive(Deserialize)]
struct CreateUser {
    phone: String,
    email: String,
    name: Option<String>,
    role: String,
}

#[instrument(level="info", skip(state, u), fields(phone=%u.phone, email=%u.email, role=%u.role))]
async fn create_user(State(state): State<AppState>, Json(u): Json<CreateUser>) -> impl IntoResponse {
    let phone = u.phone.trim().to_string();
    let email = u.email.trim().to_lowercase();
    let name  = u.name.as_ref().map(|s| s.trim().to_string()).filter(|s| !s.is_empty());
    let role  = u.role.trim().to_lowercase();

    if phone.is_empty() || email.is_empty() || role.is_empty() {
        warn!("create_user validation fail: required fields missing");
        return (StatusCode::BAD_REQUEST, "phone, email y role son obligatorios").into_response();
    }
    if role != "admin" && role != "user" {
        warn!(%role, "create_user invalid role");
        return (StatusCode::BAD_REQUEST, "role debe ser 'admin' o 'user'").into_response();
    }

    let inserted = sqlx::query(
        r#"
        INSERT INTO users (phone, email, name, role)
        VALUES ($1,$2,$3,$4)
        ON CONFLICT DO NOTHING
        RETURNING id
        "#
    )
    .bind(&phone).bind(&email).bind(&name).bind(&role)
    .fetch_optional(&state.db).await;

    match inserted {
        Ok(Some(row)) => {
            let id: i32 = row.get("id");
            info!(%id, "user created");
            (StatusCode::CREATED, axum::Json(json!({ "id": id }))).into_response()
        }
        Ok(None) => {
            // conflicto → averiguar cuál
            warn!("create_user conflict");
            let (p, e): (bool, bool) = match sqlx::query_as::<_, (Option<i64>, Option<i64>)>(r#"
                SELECT
                  (SELECT 1 FROM users WHERE phone=$1 LIMIT 1),
                  (SELECT 1 FROM users WHERE LOWER(email)=LOWER($2) LIMIT 1)
            "#).bind(&phone).bind(&email).fetch_one(&state.db).await {
                Ok((pp, ee)) => (pp.is_some(), ee.is_some()),
                Err(err) => { warn!(error=%err, "conflict check failed"); (false, false) }
            };
            let msg = match (p, e) {
                (true, true)   => "phone y email ya existen",
                (true, false)  => "phone ya existe",
                (false, true)  => "email ya existe",
                (false, false) => "conflicto de unicidad",
            };
            (StatusCode::CONFLICT, msg).into_response()
        }
        Err(e) => {
            error!(error=%e, "create_user db error");
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
    }
}

#[derive(Deserialize)]
struct DevicesJson { devices: Vec<DeviceItem> }

#[derive(Deserialize)]
struct DeviceItem { jid: String }

#[instrument(level="info", skip(state, headers), fields(id=%id))]
async fn delete_user(
    State(state): State<AppState>,
    headers: HeaderMap,
    Path(id): Path<i32>,
) -> impl IntoResponse {
    // 1) Obtener phone del usuario
    let row = match sqlx::query(r#"SELECT phone FROM users WHERE id = $1"#)
        .bind(id).fetch_optional(&state.db).await
    {
        Ok(r) => r,
        Err(e) => {
            error!(error=%e, "db select failed");
            return (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response();
        }
    };

    let Some(row) = row else {
        warn!("user not found");
        return (StatusCode::NOT_FOUND, "not found").into_response();
    };
    let phone: String = row.get::<String, _>("phone");
    info!(%phone, "user phone loaded");

    fn digits_only(s: &str) -> String { s.chars().filter(|c| c.is_ascii_digit()).collect() }
    let phone_norm = digits_only(&phone);
    debug!(%phone_norm, "normalized phone");

    // 2) Consultar dispositivos en whatsapp_bot
    let mut fwd_headers = reqwest::header::HeaderMap::new();
    if let Some(val) = headers.get("x-auth").and_then(|v| v.to_str().ok()) {
        if let Ok(hv) = reqwest::header::HeaderValue::from_str(val) {
            fwd_headers.insert("x-auth", hv);
        }
    }
    let devices_url = format!("{}/devices", *WHATSAPP_BOT_URL);
    debug!(%devices_url, "fetching devices");

    let devices_resp = match HTTP.get(&devices_url).headers(fwd_headers.clone()).send().await {
        Ok(r) => r,
        Err(e) => {
            error!(error=%e, "whatsapp_bot /devices request error");
            return (StatusCode::BAD_GATEWAY, format!("whatsapp_bot /devices error: {e}")).into_response();
        }
    };
    debug!(status=?devices_resp.status(), "devices response");

    if !devices_resp.status().is_success() {
        warn!(status=?devices_resp.status(), "whatsapp_bot /devices non-2xx; will delete only DB");
        // seguiremos borrando solo en DB
    }

    let mut matched_jid: Option<String> = None;
    if devices_resp.status().is_success() {
        let devices_json: DevicesJson = match devices_resp.json().await {
            Ok(v) => v,
            Err(e) => {
                warn!(error=%e, "parse /devices failed; delete only DB");
                DevicesJson { devices: vec![] }
            }
        };
        info!(count=devices_json.devices.len(), "devices listed");
        for d in devices_json.devices {
            let jid_norm = digits_only(&d.jid);
            if !jid_norm.is_empty() && !phone_norm.is_empty() {
                if jid_norm.contains(&phone_norm) || phone_norm.contains(&jid_norm) {
                    info!(jid=%d.jid, "matched device");
                    matched_jid = Some(d.jid);
                    break;
                }
            }
        }
    }

    // 4) Si hay match → primero borrar en whatsapp_bot
    if let Some(jid) = matched_jid {
        let del_url = format!("{}/devices/{}", *WHATSAPP_BOT_URL, jid);
        info!(%jid, %del_url, "deleting device in whatsapp_bot first");
        let del_resp = match HTTP.delete(&del_url).headers(fwd_headers).send().await {
            Ok(r) => r,
            Err(e) => {
                error!(error=%e, "whatsapp_bot /delete request error");
                return (StatusCode::BAD_GATEWAY, format!("whatsapp_bot /delete error: {e}")).into_response();
            }
        };
        debug!(status=?del_resp.status(), "delete device response");
        if !del_resp.status().is_success() {
            warn!(status=?del_resp.status(), "whatsapp_bot /delete non-2xx; abort DB delete");
            return (StatusCode::BAD_GATEWAY, format!("whatsapp_bot /delete status {}", del_resp.status()))
                .into_response();
        }
    } else {
        info!("no device matched; will delete only from DB");
    }

    // 5) Borrar en DB
    info!("deleting user in DB");
    let res = sqlx::query(r#"DELETE FROM users WHERE id = $1"#)
        .bind(id).execute(&state.db).await;

    match res {
        Ok(done) if done.rows_affected() > 0 => {
            info!("user deleted from DB");
            StatusCode::NO_CONTENT.into_response()
        }
        Ok(_) => {
            warn!("user not found on delete");
            (StatusCode::NOT_FOUND, "not found").into_response()
        }
        Err(e) => {
            error!(error=%e, "db delete failed");
            (StatusCode::INTERNAL_SERVER_ERROR, e.to_string()).into_response()
        }
    }
}

#[derive(Deserialize, Debug)]
struct LoginQrForwardReq { to: String }

#[instrument(level="info", skip(headers), fields(to=%body.to))]
async fn wabot_loginqr(
    State(_state): State<AppState>,
    headers: HeaderMap,
    Json(body): Json<LoginQrForwardReq>,
) -> Result<Response, (StatusCode, String)> {
    let url = format!("{}/loginqr", *WHATSAPP_BOT_URL);
    debug!(%url, "forwarding to whatsapp_bot");

    let mut fwd_headers = reqwest::header::HeaderMap::new();
    if let Some(val) = headers.get("x-auth").and_then(|v| v.to_str().ok()) {
        if let Ok(hv) = reqwest::header::HeaderValue::from_str(val) {
            fwd_headers.insert("x-auth", hv);
        }
    }

    let resp = HTTP.post(&url).headers(fwd_headers).json(&json!({ "to": body.to }))
        .send().await.map_err(|e| {
            error!(error=%e, "forward request error");
            (StatusCode::BAD_GATEWAY, e.to_string())
        })?;

    let status = resp.status();
    debug!(?status, "forward response status");

    let ct = resp.headers()
        .get(reqwest::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok()).unwrap_or("");

    if ct.starts_with("application/json") {
        let v: JsonValue = resp.json().await.map_err(|e| {
            error!(error=%e, "forward parse json failed");
            (StatusCode::BAD_GATEWAY, e.to_string())
        })?;
        info!("forward ok (json)");
        Ok((status, Json(v)).into_response())
    } else {
        let body = resp.bytes().await.map_err(|e| {
            error!(error=%e, "forward read body failed");
            (StatusCode::BAD_GATEWAY, e.to_string())
        })?;
        info!("forward ok (bytes)");
        Ok((status, body).into_response())
    }
}

// ------- Lista servicios (por label com.docker.compose.service) -------
async fn dlogs_services() -> Result<impl IntoResponse, (StatusCode, String)> {
    let docker = Docker::connect_with_local_defaults()
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;

    let opts = ListContainersOptionsBuilder::default()
        .all(true)
        .build();

    let containers = docker
        .list_containers(Some(opts))
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;

    let mut services: Vec<String> = Vec::new();
    for c in containers {
        if let Some(labels) = c.labels {
            if let Some(svc) = labels.get("com.docker.compose.service") {
                if !services.contains(svc) {
                    services.push(svc.clone());
                }
            }
        }
    }
    Ok(Json(serde_json::json!({ "services": services })))
}

#[derive(Deserialize)]
struct DLogsQuery {
    service: String,          // ej: "web", "whatsapp_bot"
    date: String,             // YYYY-MM-DD
    pattern: Option<String>,  // substring (case-sensitive)
    limit: Option<usize>,     // por defecto 1000
}

// ------- Logs por servicio + fecha + patrón -------
async fn dlogs_view(Query(q): Query<DLogsQuery>) -> Result<impl IntoResponse, (StatusCode, String)> {
    let date = NaiveDate::parse_from_str(&q.date, "%Y-%m-%d")
        .map_err(|_| (StatusCode::BAD_REQUEST, "date debe ser YYYY-MM-DD".to_string()))?;
    let start = Utc.from_utc_datetime(&date.and_hms_opt(0, 0, 0).unwrap());
    let end   = Utc.from_utc_datetime(&date.and_hms_opt(23, 59, 59).unwrap());

    let docker = Docker::connect_with_local_defaults()
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;

    let list_opts = ListContainersOptionsBuilder::default()
        .all(true)
        .build();

    let containers = docker
        .list_containers(Some(list_opts))
        .await
        .map_err(|e| (StatusCode::BAD_GATEWAY, e.to_string()))?;

    let mut container_id: Option<String> = None;
    for c in containers {
        if let Some(labels) = c.labels {
            if let Some(svc) = labels.get("com.docker.compose.service") {
                if svc == &q.service {
                    container_id = c.id;
                    break;
                }
            }
        }
    }
    let Some(id) = container_id else {
        return Err((StatusCode::NOT_FOUND, "service not found".to_string()));
    };

    // Opciones nuevas (OpenAPI). No uses el campo 'details' (no existe aquí).
    let log_opts = LogsOptionsBuilder::default()
        .follow(false)
        .stdout(true)
        .stderr(true)
        .since(start.timestamp().try_into().unwrap())
        .until(end.timestamp().try_into().unwrap())
        .timestamps(true)
        .tail("all")
        .build();

    // docker.logs devuelve un Stream (no un Result); no intentes .map_err() aquí.
    let mut stream = docker.logs(&id, Some(log_opts));

    let needle = q.pattern.unwrap_or_default();
    let limit = q.limit.unwrap_or(1000).min(20_000);
    let mut lines: Vec<String> = Vec::with_capacity(limit);

    while let Some(chunk) = stream.next().await {
        match chunk {
            Ok(LogOutput::StdOut { message })
            | Ok(LogOutput::StdErr { message })
            | Ok(LogOutput::Console { message }) => {
                let s = String::from_utf8_lossy(&message);
                for l in s.split('\n') {
                    if l.is_empty() { continue; }
                    if needle.is_empty() || l.contains(&needle) {
                        lines.push(l.to_string());
                        if lines.len() >= limit { break; }
                    }
                }
                if lines.len() >= limit { break; }
            }
            Err(e) => {
                // puedes ignorar errores de chunks individuales o cortar
                return Err((StatusCode::BAD_GATEWAY, e.to_string()));
            }
            _ => {}
        }
    }

    Ok(Json(serde_json::json!({
        "service": q.service,
        "date": q.date,
        "count": lines.len(),
        "lines": lines,
    })))
}
