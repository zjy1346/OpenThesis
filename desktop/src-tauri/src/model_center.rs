use crate::credentials::{credential_target, platform_vault, validate_secret, CredentialVault};
use crate::provider_registry;
use crate::connection_contract::{CapabilityProfile, ConnectionTransportSettings};
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::sync::atomic::{AtomicBool, Ordering};
use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager, State};
use zeroize::Zeroizing;

#[derive(Clone)]
pub struct ModelCenter {
    db_path: PathBuf,
    vault: Arc<dyn CredentialVault>,
    database_lock: Arc<Mutex<()>>,
}

pub(crate) fn resolve_model_center_db_path(
    override_dir: Option<&str>,
    app_data_dir: &Path,
) -> Result<PathBuf, String> {
    match override_dir.map(str::trim) {
        None | Some("") => Ok(app_data_dir.join("model-center.db")),
        Some(value) => {
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                return Err("OPENTHESIS_DATA_DIR must be an absolute path".to_string());
            }
            Ok(path.join("model-center.db"))
        }
    }
}

impl ModelCenter {
    pub fn new(
        db_path: impl Into<PathBuf>,
        vault: Arc<dyn CredentialVault>,
    ) -> Result<Self, String> {
        let center = Self {
            db_path: db_path.into(),
            vault,
            database_lock: Arc::new(Mutex::new(())),
        };
        center.initialize()?;
        Ok(center)
    }
    fn initialize(&self) -> Result<(), String> {
        if let Some(parent) = self.db_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|_| "model center data directory is unavailable".to_string())?;
        }
        let connection = self.open()?;
        connection
            .execute_batch("PRAGMA foreign_keys = ON; PRAGMA secure_delete = ON;")
            .map_err(|_| "model center database could not be initialized".to_string())?;
        connection.execute_batch("CREATE TABLE IF NOT EXISTS model_center_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS provider_connections (connection_id TEXT PRIMARY KEY NOT NULL, provider_id TEXT NOT NULL, display_name TEXT NOT NULL, region TEXT NOT NULL, endpoint TEXT NOT NULL, credential_ref TEXT, credential_version INTEGER NOT NULL DEFAULT 0, has_secret INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())); CREATE TABLE IF NOT EXISTS configured_models (configured_model_id TEXT PRIMARY KEY NOT NULL, connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id) ON DELETE CASCADE, model_id TEXT NOT NULL, alias TEXT NOT NULL, free_tier INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, capabilities_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch()), UNIQUE(connection_id, model_id)); CREATE TABLE IF NOT EXISTS model_capability_checks (configured_model_id TEXT NOT NULL REFERENCES configured_models(configured_model_id) ON DELETE CASCADE, capability TEXT NOT NULL, model_configuration_version INTEGER NOT NULL, connection_configuration_version INTEGER NOT NULL, credential_version INTEGER NOT NULL, status TEXT NOT NULL, checked_at INTEGER NOT NULL, error_code TEXT, retry_after_seconds INTEGER, PRIMARY KEY(configured_model_id, capability, model_configuration_version, connection_configuration_version, credential_version)); INSERT INTO model_center_meta(key, value) VALUES ('schema_version', '1') ON CONFLICT(key) DO UPDATE SET value=excluded.value;").map_err(|_| "model center database could not be initialized".to_string())?;
        for (table, column, definition) in [
            ("provider_connections", "protocol", "TEXT"),
            ("provider_connections", "capability_profile", "TEXT"),
            ("provider_connections", "transport_settings", "TEXT NOT NULL DEFAULT '{}'"),
            (
                "provider_connections",
                "status",
                "TEXT NOT NULL DEFAULT 'untested'",
            ),
            ("provider_connections", "last_tested_at", "INTEGER"),
            ("provider_connections", "last_error_code", "TEXT"),
            (
                "provider_connections",
                "configuration_version",
                "INTEGER NOT NULL DEFAULT 1",
            ),
            (
                "configured_models",
                "billing_class",
                "TEXT NOT NULL DEFAULT 'unknown'",
            ),
            ("configured_models", "free_source_url", "TEXT"),
            ("configured_models", "free_verified_at", "INTEGER"),
            (
                "configured_models",
                "health_status",
                "TEXT NOT NULL DEFAULT 'unverified'",
            ),
            ("configured_models", "last_discovered_at", "INTEGER"),
            ("configured_models", "context_window_hint", "INTEGER"),
            ("configured_models", "temperature", "REAL"),
            (
                "configured_models",
                "timeout_seconds",
                "INTEGER NOT NULL DEFAULT 180",
            ),
            (
                "configured_models",
                "configuration_version",
                "INTEGER NOT NULL DEFAULT 1",
            ),
        ] {
            ensure_column(&connection, table, column, definition)?;
        }
        connection.execute("UPDATE provider_connections SET protocol=CASE WHEN provider_id='ollama' THEN 'ollama' ELSE 'chat_completions' END WHERE protocol IS NULL", []).map_err(|_| "Connection protocol migration failed")?;
        connection.execute_batch("CREATE TABLE IF NOT EXISTS model_directory_cache (id INTEGER PRIMARY KEY CHECK(id=1), snapshot TEXT NOT NULL);").map_err(|_| "Model directory cache unavailable")?;
        connection.execute("UPDATE configured_models SET billing_class='unknown',free_tier=0,free_source_url=NULL,free_verified_at=NULL WHERE billing_class='local_no_provider_fee' AND connection_id IN (SELECT connection_id FROM provider_connections WHERE provider_id='ollama')",[]).map_err(|_| "Ollama billing declaration migration failed")?;
        connection.execute_batch("CREATE TABLE IF NOT EXISTS model_probe_receipts (attempt_id TEXT PRIMARY KEY,parent_request_id TEXT NOT NULL,connection_id TEXT NOT NULL,configured_model_id TEXT NOT NULL,connection_version INTEGER NOT NULL,credential_version INTEGER NOT NULL,operation TEXT NOT NULL,reserved_calls INTEGER NOT NULL CHECK(reserved_calls=1),outcome TEXT NOT NULL,receipt TEXT,created_at INTEGER NOT NULL DEFAULT(unixepoch()));").map_err(|_| "Model test receipts unavailable")?;
        connection
            .execute(
                "INSERT INTO model_center_meta(key, value) VALUES ('schema_version', '3') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [],
            )
            .map_err(|_| "model center database could not be initialized".to_string())?;
        Ok(())
    }
    fn open(&self) -> Result<Connection, String> {
        let connection = Connection::open(&self.db_path)
            .map_err(|_| "model center database is unavailable".to_string())?;
        connection
            .busy_timeout(Duration::from_secs(5))
            .map_err(|_| "model center database is unavailable".to_string())?;
        Ok(connection)
    }
    fn with_connection<T>(
        &self,
        operation: impl FnOnce(&Connection) -> Result<T, String>,
    ) -> Result<T, String> {
        let _guard = self
            .database_lock
            .lock()
            .map_err(|_| "model center state is unavailable".to_string())?;
        let connection = self.open()?;
        connection
            .execute_batch("PRAGMA foreign_keys = ON; PRAGMA secure_delete = ON;")
            .map_err(|_| "model center database is unavailable".to_string())?;
        operation(&connection)
    }
    pub fn list_connections(&self) -> Result<Vec<ConnectionSummary>, String> {
        self.with_connection(|connection| {
            let mut query = connection
                .prepare(
                    "SELECT connection_id, provider_id, display_name, region, endpoint, credential_ref, credential_version, enabled, status, last_tested_at, last_error_code, configuration_version FROM provider_connections ORDER BY display_name COLLATE NOCASE, connection_id",
                )
                .map_err(|_| "model center database is unavailable".to_string())?;
            let mut rows = query
                .query([])
                .map_err(|_| "model center database is unavailable".to_string())?;
            let mut summaries = Vec::new();
            while let Some(row) = rows
                .next()
                .map_err(|_| "model center database is unavailable".to_string())?
            {
                let credential_ref: Option<String> = row
                    .get(5)
                    .map_err(|_| "model center database is unavailable".to_string())?;
                let has_secret = match credential_ref.as_deref() {
                    Some(target) => self.vault.has(target)?,
                    None => false,
                };
                let id: String = row.get(0).map_err(|_| "model center database is unavailable")?;
                let (protocol, capability_profile) = load_contract(&connection, &id)?;
                summaries.push(ConnectionSummary {
                    transport: load_transport(&connection, &id)?,
                    protocol, capability_profile,
                    connection_id: row.get(0).map_err(|_| "model center database is unavailable".to_string())?,
                    provider_id: row.get(1).map_err(|_| "model center database is unavailable".to_string())?,
                    display_name: row.get(2).map_err(|_| "model center database is unavailable".to_string())?,
                    region: row.get(3).map_err(|_| "model center database is unavailable".to_string())?,
                    endpoint: row.get(4).map_err(|_| "model center database is unavailable".to_string())?,
                    credential_version: row.get(6).map_err(|_| "model center database is unavailable".to_string())?,
                    has_secret,
                    enabled: row.get::<_, i64>(7).map_err(|_| "model center database is unavailable".to_string())? != 0,
                    status: row.get(8).map_err(|_| "model center database is unavailable".to_string())?,
                    last_tested_at: row.get(9).map_err(|_| "model center database is unavailable".to_string())?,
                    last_error_code: row.get(10).map_err(|_| "model center database is unavailable".to_string())?,
                    configuration_version: row.get(11).map_err(|_| "model center database is unavailable".to_string())?,
                });
            }
            Ok(summaries)
        })
    }
    pub fn model_directory(&self, refresh: bool) -> Result<crate::model_directory::DirectorySnapshot,String> {
        if refresh {
            let snapshot = crate::model_directory::refresh()?;
            let text = serde_json::to_string(&snapshot).map_err(|_| "Directory serialization failed")?;
            self.with_connection(|connection| { connection.execute("INSERT INTO model_directory_cache(id,snapshot) VALUES (1,?1) ON CONFLICT(id) DO UPDATE SET snapshot=excluded.snapshot",[text]).map_err(|_| "Directory cache write failed")?; Ok(()) })?;
            return Ok(snapshot);
        }
        let cached: Option<String> = self.with_connection(|connection| connection.query_row("SELECT snapshot FROM model_directory_cache WHERE id=1",[],|row| row.get(0)).optional().map_err(|_| "Directory cache unavailable".into()))?;
        if let Some(cached) = cached { if let Ok(snapshot) = serde_json::from_str(&cached) { return Ok(snapshot); } }
        crate::model_directory::bundled()
    }
    pub(crate) fn reserve_probe(&self, parent: &str, model: &ConfiguredModelRuntime, operation: &str) -> Result<String,String> {
        let attempt=format!("{}-{}",std::process::id(),SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "Clock unavailable")?.as_nanos());
        self.with_connection(|connection| {
            let changed=connection.execute("INSERT INTO model_probe_receipts(attempt_id,parent_request_id,connection_id,configured_model_id,connection_version,credential_version,operation,reserved_calls,outcome) SELECT ?1,?2,?3,?4,?5,?6,?7,1,'reserved' WHERE EXISTS (SELECT 1 FROM provider_connections WHERE connection_id=?3 AND configuration_version=?5 AND credential_version=?6) AND ((?8=0 AND ?4='connection-test.'||?3) OR EXISTS (SELECT 1 FROM configured_models WHERE connection_id=?3 AND configured_model_id=?4 AND configuration_version=?8)) AND (SELECT COUNT(*) FROM model_probe_receipts WHERE parent_request_id=?2)<2",params![attempt,parent,model.connection_id,model.configured_model_id,model.connection_configuration_version,model.credential_version,operation,model.configuration_version]).map_err(|_| "Model test reservation failed")?;
            if changed != 1 { return Err("Model test configuration changed or its two-request limit was reached before send".into()); }
            Ok(attempt)
        })
    }
    pub(crate) fn finish_probe(&self, id: &str, outcome: &str, receipt: &str) -> Result<(),String> {
        self.with_connection(|connection| { connection.execute("UPDATE model_probe_receipts SET outcome=?1,receipt=?2 WHERE attempt_id=?3 AND outcome='reserved'",params![outcome,receipt,id]).map_err(|_| "Model test receipt could not be saved")?; Ok(()) })
    }
    pub(crate) fn reserve_metadata_probe(&self, id: &str, version: i64, credential_version: i64) -> Result<String,String> {
        let attempt=format!("metadata-{}-{}",std::process::id(),SystemTime::now().duration_since(UNIX_EPOCH).map_err(|_| "Clock unavailable")?.as_nanos());
        self.with_connection(|connection| { let changed=connection.execute("INSERT INTO model_probe_receipts(attempt_id,parent_request_id,connection_id,configured_model_id,connection_version,credential_version,operation,reserved_calls,outcome) SELECT ?1,?1,?2,'',?3,?4,'metadata',1,'reserved' WHERE EXISTS (SELECT 1 FROM provider_connections WHERE connection_id=?2 AND configuration_version=?3 AND credential_version=?4)",params![attempt,id,version,credential_version]).map_err(|_| "Metadata reservation failed")?; if changed!=1 { return Err("Metadata connection changed".into()); } Ok(attempt) })
    }

    pub fn save_connection(&self, mut input: SaveConnectionInput) -> Result<ConnectionSummary, String> {
        provider_registry::validate_connection_id(&input.connection_id)?;
        provider_registry::validate_provider_id(&input.provider_id)?;
        let provider = provider_registry::find(&input.provider_id)
            .ok_or_else(|| "unknown provider".to_string())?;
        if input.display_name.len() > 256
            || input.display_name.trim().is_empty()
            || input.display_name.chars().any(char::is_control)
        {
            return Err("display name is invalid".to_string());
        }
        if input.region.len() > 64 || input.region.chars().any(char::is_control) {
            return Err("region is invalid".to_string());
        }
        let protocol = input.protocol.clone().unwrap_or_else(|| if input.provider_id == "ollama" { "ollama".into() } else { "chat_completions".into() });
        if !["chat_completions", "ollama"].contains(&protocol.as_str()) || (input.provider_id != "custom" && (protocol == "ollama") != (input.provider_id == "ollama")) { return Err("Unsupported connection protocol".into()); }
        if provider.provider_id != "custom" && input.endpoint.is_empty() {
            return Err("endpoint is required".to_string());
        }
        self.with_connection(|connection| {
            let existing: Option<(String, Option<String>, i64)> = connection.query_row("SELECT provider_id, credential_ref, has_secret FROM provider_connections WHERE connection_id=?1", [&input.connection_id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?))).optional().map_err(|_| "model connection could not be read".to_string())?;
            let transaction = connection.unchecked_transaction().map_err(|_| "Connection transaction unavailable")?;
            if let Some((existing_provider, credential_ref, has_secret)) = existing { if existing_provider != input.provider_id && (credential_ref.is_some() || has_secret != 0) { return Err("delete the existing connection before changing its provider".to_string()); } }
            let previous: Option<(String,String,i64,String,Option<String>,String)> = connection.query_row("SELECT provider_id,endpoint,enabled,protocol,capability_profile,transport_settings FROM provider_connections WHERE connection_id=?1", [&input.connection_id], |row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?))).optional().map_err(|_| "model connection could not be read")?;
            let protocol = if input.protocol.is_none() { previous.as_ref().map(|row| row.3.clone()).unwrap_or(protocol) } else { protocol };
            let transport = match input.transport.as_ref() {
                Some(value) => value.clone(),
                None => previous.as_ref().map(|row| serde_json::from_str(&row.5)).transpose().map_err(|_| "MODEL_TRANSPORT_INVALID")?.unwrap_or_default(),
            };
            input.endpoint = transport.normalize_endpoint(&input.endpoint,&protocol)?;
            provider_registry::validate_endpoint(&input.provider_id,&input.endpoint)?;
            transport.validate(&input.endpoint)?;
            let transport_json = serde_json::to_string(&transport).map_err(|_| "MODEL_TRANSPORT_INVALID")?;
            if previous.is_none() && input.provider_id == "custom" && input.capability_profile.is_none() {
                input.capability_profile = Some(CapabilityProfile::compatible_default(&protocol));
            }
            if let Some(profile)=&input.capability_profile { profile.validate(&protocol)?; }
            let profile_json = match &input.capability_profile {
                Some(profile) => Some(serde_json::to_string(profile).map_err(|_| "Invalid capability profile")?),
                None => previous.as_ref().and_then(|row| row.4.clone()),
            };
            let changed = previous.as_ref().is_some_and(|row| row.0 != input.provider_id || row.1 != input.endpoint || row.2 != input.enabled as i64 || row.3 != protocol || !profile_wire_equal(row.4.as_deref(),profile_json.as_deref()) || serde_json::from_str::<ConnectionTransportSettings>(&row.5).ok().as_ref() != Some(&transport));
            connection.execute("INSERT INTO provider_connections(connection_id,provider_id,display_name,region,endpoint,enabled,protocol,capability_profile,transport_settings,updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?10,unixepoch()) ON CONFLICT(connection_id) DO UPDATE SET provider_id=excluded.provider_id,display_name=excluded.display_name,region=excluded.region,endpoint=excluded.endpoint,enabled=excluded.enabled,protocol=excluded.protocol,capability_profile=excluded.capability_profile,transport_settings=excluded.transport_settings,status=CASE WHEN ?9 THEN 'untested' ELSE provider_connections.status END,last_tested_at=CASE WHEN ?9 THEN NULL ELSE provider_connections.last_tested_at END,last_error_code=CASE WHEN ?9 THEN NULL ELSE provider_connections.last_error_code END,configuration_version=provider_connections.configuration_version+?9,updated_at=unixepoch()",params![input.connection_id,input.provider_id,input.display_name,input.region,input.endpoint,input.enabled as i64,protocol,profile_json,changed as i64,transport_json]).map_err(|_| "model connection could not be saved")?;
            if changed { connection.execute("UPDATE configured_models SET health_status='unverified', configuration_version=configuration_version+1, updated_at=unixepoch() WHERE connection_id=?1", [&input.connection_id]).map_err(|_| "configured model health could not be invalidated")?; }
            let summary = self.connection_summary(connection, &input.connection_id)?;
            transaction.commit().map_err(|_| "Connection transaction could not be committed")?;
            Ok(summary)
        })
    }
    pub fn set_secret(
        &self,
        connection_id: &str,
        secret: &str,
    ) -> Result<ConnectionSummary, String> {
        let rotation = self.stage_secret(connection_id, secret)?;
        self.activate_staged_secret(&rotation)
    }

    pub(crate) fn stage_secret(
        &self,
        connection_id: &str,
        secret: &str,
    ) -> Result<SecretRotation, String> {
        provider_registry::validate_connection_id(connection_id)?;
        validate_secret(secret)?;
        let (provider_id, old_ref, old_version): (String, Option<String>, i64) = self
            .with_connection(|connection| {
                connection
                    .query_row(
                        "SELECT provider_id, credential_ref, credential_version FROM provider_connections WHERE connection_id=?1",
                        [connection_id],
                        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
                    )
                    .optional()
                    .map_err(|_| "model connection could not be read".to_string())?
                    .ok_or_else(|| "model connection was not found".to_string())
            })?;
        if provider_id == "ollama" {
            return Err("Ollama does not accept an API key".to_string());
        }
        let new_version = (old_version + 1).max(1);
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "credential rotation clock is unavailable".to_string())?
            .as_nanos();
        let new_ref = format!(
            "{}/stage/{}-{nonce}",
            credential_target(&provider_id, connection_id, new_version as u32),
            std::process::id()
        );
        self.vault.put(&new_ref, secret)?;
        Ok(SecretRotation {
            connection_id: connection_id.to_string(),
            old_ref,
            old_version,
            new_ref,
            new_version,
        })
    }

    pub(crate) fn staged_secret(
        &self,
        rotation: &SecretRotation,
    ) -> Result<Zeroizing<String>, String> {
        self.vault.get(&rotation.new_ref)
    }

    pub(crate) fn discard_staged_secret(&self, rotation: &SecretRotation) -> Result<(), String> {
        self.vault.delete(&rotation.new_ref)
    }

    pub(crate) fn activate_staged_secret(
        &self,
        rotation: &SecretRotation,
    ) -> Result<ConnectionSummary, String> {
        let result = self.with_connection(|connection| {
            let changed = connection
                .execute(
                    "UPDATE provider_connections SET credential_ref=?1, credential_version=?2, has_secret=1, status='untested', last_tested_at=NULL, last_error_code=NULL, configuration_version=configuration_version+1, updated_at=unixepoch() WHERE connection_id=?3 AND credential_version=?4 AND ((credential_ref IS NULL AND ?5 IS NULL) OR credential_ref=?5)",
                    params![rotation.new_ref, rotation.new_version, rotation.connection_id, rotation.old_version, rotation.old_ref],
                )
                .map_err(|_| "model connection metadata could not be saved".to_string())?;
            if changed != 1 {
                return Err("model connection changed during credential rotation".to_string());
            }
            connection.execute("UPDATE configured_models SET health_status='unverified',configuration_version=configuration_version+1 WHERE connection_id=?1",[&rotation.connection_id]).map_err(|_| "Model credential generation invalidation failed")?;
            self.connection_summary(connection, &rotation.connection_id)
        });
        if result.is_err() {
            let _ = self.vault.delete(&rotation.new_ref);
            return result;
        }
        if let Some(old_ref) = rotation.old_ref.as_deref() {
            if old_ref != rotation.new_ref {
                self.vault.delete(old_ref)?;
            }
        }
        result
    }

    pub(crate) fn first_enabled_model_id(
        &self,
        connection_id: &str,
    ) -> Result<Option<String>, String> {
        provider_registry::validate_connection_id(connection_id)?;
        self.with_connection(|connection| {
            connection
                .query_row(
                    "SELECT configured_model_id FROM configured_models WHERE connection_id=?1 AND enabled=1 ORDER BY configured_model_id LIMIT 1",
                    [connection_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| "configured model could not be read".to_string())
        })
    }
    pub fn set_enabled(
        &self,
        connection_id: &str,
        enabled: bool,
    ) -> Result<ConnectionSummary, String> {
        provider_registry::validate_connection_id(connection_id)?;
        self.with_connection(|connection| { let changed = connection.execute("UPDATE provider_connections SET enabled=?1, updated_at=unixepoch() WHERE connection_id=?2", params![enabled as i64, connection_id]).map_err(|_| "model connection could not be updated".to_string())?; if changed == 0 { return Err("model connection was not found".to_string()); } self.connection_summary(connection, connection_id) })
    }
    pub fn delete_connection(&self, connection_id: &str) -> Result<(), String> {
        provider_registry::validate_connection_id(connection_id)?;
        let row: Option<Option<String>> = self.with_connection(|connection| {
            connection
                .query_row(
                    "SELECT credential_ref FROM provider_connections WHERE connection_id=?1",
                    [connection_id],
                    |row| row.get::<_, Option<String>>(0),
                )
                .optional()
                .map_err(|_| "model connection could not be read".to_string())
        })?;
        let Some(reference) = row else {
            return Err("model connection was not found".to_string());
        };
        self.with_connection(|connection| { connection.execute("UPDATE provider_connections SET enabled=0, updated_at=unixepoch() WHERE connection_id=?1", [connection_id]).map_err(|_| "model connection could not be disabled".to_string()).map(|_| ()) })?;
        if let Some(reference) = reference.filter(|value: &String| !value.is_empty()) {
            self.vault.delete(&reference)?;
        }
        self.with_connection(|connection| {
            connection
                .execute(
                    "DELETE FROM provider_connections WHERE connection_id=?1",
                    [connection_id],
                )
                .map_err(|_| "model connection metadata could not be deleted".to_string())
                .map(|_| ())
        })
    }
    pub fn list_models(&self) -> Result<Vec<ConfiguredModelSummary>, String> {
        self.with_connection(|connection| {
            let mut query = connection
                .prepare(
                    "SELECT configured_model_id FROM configured_models ORDER BY alias COLLATE NOCASE, configured_model_id",
                )
                .map_err(|_| "model center database is unavailable".to_string())?;
            let rows = query
                .query_map([], |row| row.get::<_, String>(0))
                .map_err(|_| "model center database is unavailable".to_string())?;
            let ids: Result<Vec<String>, String> = rows
                .map(|row| row.map_err(|_| "model center database is unavailable".to_string()))
                .collect();
            ids?.into_iter()
                .map(|id| self.model_summary(connection, &id))
                .collect()
        })
    }

    pub fn save_model(
        &self,
        input: SaveConfiguredModelInput,
    ) -> Result<ConfiguredModelSummary, String> {
        provider_registry::validate_connection_id(&input.connection_id)?;
        provider_registry::validate_model_id(&input.model_id)?;
        validate_configured_model_id(&input.configured_model_id)?;
        if input.alias.len() > 256
            || input.alias.trim().is_empty()
            || input.alias.chars().any(char::is_control)
        {
            return Err("model alias is invalid".to_string());
        }
        if input.capabilities.len() > 32
            || input
                .capabilities
                .iter()
                .any(|value| value.len() > 64 || value.chars().any(char::is_control))
        {
            return Err("model capabilities are invalid".to_string());
        }
        let timeout_seconds = input.timeout_seconds.unwrap_or(180);
        if input.context_window_hint.is_some_and(|n| !(1..=2_147_483_647).contains(&n)) { return Err("Invalid declared context capacity".into()); }
        if !(5..=600).contains(&timeout_seconds) {
            return Err("model timeout must be between 5 and 600 seconds".to_string());
        }
        if input
            .temperature
            .is_some_and(|value| !value.is_finite() || !(0.0..=2.0).contains(&value))
        {
            return Err("model temperature is invalid".to_string());
        }
        let capabilities = serde_json::to_string(&input.capabilities)
            .map_err(|_| "model capabilities are invalid".to_string())?;
        self.with_connection(|connection| {
            let provider_id: Option<String> = connection
                .query_row(
                    "SELECT provider_id FROM provider_connections WHERE connection_id=?1",
                    [&input.connection_id],
                    |row| row.get(0),
                )
                .optional()
                .map_err(|_| "model connection could not be read".to_string())?;
            let provider_id =
                provider_id.ok_or_else(|| "model connection was not found".to_string())?;
            let billing_class =
                provider_registry::billing_class_for(&provider_id, &input.model_id).to_string();
            let free_source_url = provider_registry::free_source_url(&provider_id, &input.model_id);
            let free_tier = matches!(billing_class.as_str(), "free_tier" | "local_no_provider_fee");
            let transaction = connection.unchecked_transaction().map_err(|_| "Model transaction unavailable")?;
            let previous: Option<(String,String,i64,String,Option<i64>,Option<f64>,i64)> = connection.query_row("SELECT connection_id,model_id,enabled,capabilities_json,context_window_hint,temperature,timeout_seconds FROM configured_models WHERE configured_model_id=?1",[&input.configured_model_id],|row| Ok((row.get(0)?,row.get(1)?,row.get(2)?,row.get(3)?,row.get(4)?,row.get(5)?,row.get(6)?))).optional().map_err(|_| "configured model could not be read")?;
            let changed = previous.is_some_and(|row| row.0 != input.connection_id || row.1 != input.model_id || row.2 != input.enabled as i64 || row.3 != capabilities || row.4 != input.context_window_hint || row.5 != input.temperature || row.6 != timeout_seconds);
            connection
                .execute(
                    "INSERT INTO configured_models(configured_model_id, connection_id, model_id, alias, free_tier, enabled, capabilities_json, billing_class, free_source_url, free_verified_at, health_status, last_discovered_at, context_window_hint, temperature, timeout_seconds, configuration_version, updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,CASE WHEN ?9 IS NULL THEN NULL ELSE unixepoch() END,'unverified',unixepoch(),?10,?11,?12,1,unixepoch()) ON CONFLICT(configured_model_id) DO UPDATE SET connection_id=excluded.connection_id, model_id=excluded.model_id, alias=excluded.alias, free_tier=excluded.free_tier, enabled=excluded.enabled, capabilities_json=excluded.capabilities_json, billing_class=excluded.billing_class, free_source_url=excluded.free_source_url, free_verified_at=excluded.free_verified_at, health_status=CASE WHEN ?13 THEN 'unverified' ELSE configured_models.health_status END, last_discovered_at=excluded.last_discovered_at, context_window_hint=excluded.context_window_hint, temperature=excluded.temperature, timeout_seconds=excluded.timeout_seconds, configuration_version=configured_models.configuration_version+?13, updated_at=unixepoch()",
                    params![
                        input.configured_model_id,
                        input.connection_id,
                        input.model_id,
                        input.alias,
                        free_tier as i64,
                        input.enabled as i64,
                        capabilities,
                        billing_class,
                        free_source_url,
                        input.context_window_hint,
                        input.temperature,
                        timeout_seconds,
                        changed as i64,
                    ],
                )
                .map_err(|_| "configured model could not be saved".to_string())?;
            let summary = self.model_summary(connection, &input.configured_model_id)?;
            transaction.commit().map_err(|_| "Model transaction could not be committed")?;
            Ok(summary)
        })
    }

    pub(crate) fn connection_runtime(
        &self,
        connection_id: &str,
    ) -> Result<ConnectionRuntime, String> {
        provider_registry::validate_connection_id(connection_id)?;
        self.with_connection(|connection| {
            connection
                .query_row(
                    "SELECT provider_id, endpoint, credential_ref, enabled, protocol, capability_profile, status, configuration_version, credential_version,transport_settings FROM provider_connections WHERE connection_id=?1",
                    [connection_id],
                    |row| {
                        Ok(ConnectionRuntime {
                            transport: decode_transport(row.get(9)?)?,
                            provider_id: row.get(0)?,
                            endpoint: row.get(1)?,
                            credential_ref: row.get(2)?,
                            enabled: row.get::<_, i64>(3)? != 0,
                            protocol: row.get(4)?,
                            capability_profile: decode_profile(row.get(5)?)?,
                            status: row.get(6)?,
                            configuration_version: row.get(7)?, credential_version: row.get(8)?,
                        })
                    },
                )
                .optional()
                .map_err(|_| "model connection could not be read".to_string())?
                .ok_or_else(|| "model connection was not found".to_string())
        })
    }

    pub(crate) fn configured_model_runtime(
        &self,
        configured_model_id: &str,
    ) -> Result<ConfiguredModelRuntime, String> {
        validate_configured_model_id(configured_model_id)?;
        self.with_connection(|connection| {
            connection
                .query_row(
                    "SELECT m.configured_model_id, m.connection_id, c.provider_id, c.endpoint, c.credential_version, c.enabled, c.configuration_version, m.model_id, m.alias, m.enabled, m.capabilities_json, m.billing_class, m.free_tier, m.free_source_url, m.free_verified_at, m.temperature, m.timeout_seconds, m.configuration_version, c.protocol, c.capability_profile,c.transport_settings FROM configured_models m JOIN provider_connections c ON c.connection_id=m.connection_id WHERE m.configured_model_id=?1",
                    [configured_model_id],
                    |row| {
                        let capabilities_json: String = row.get(10)?;
                        let billing_class: String = row.get(11)?;
                        let legacy_free = row.get::<_, i64>(12)? != 0;
                        Ok(ConfiguredModelRuntime {
                            transport: decode_transport(row.get(20)?)?,
                            configured_model_id: row.get(0)?,
                            connection_id: row.get(1)?,
                            provider_id: row.get(2)?,
                            endpoint: row.get(3)?,
                            credential_version: row.get(4)?,
                            connection_enabled: row.get::<_, i64>(5)? != 0,
                            connection_configuration_version: row.get(6)?,
                            model_id: row.get(7)?,
                            alias: row.get(8)?,
                            model_enabled: row.get::<_, i64>(9)? != 0,
                            capabilities: serde_json::from_str(&capabilities_json).unwrap_or_default(),
                            billing_class: if billing_class == "unknown" && legacy_free {
                                "free_tier".to_string()
                            } else {
                                billing_class
                            },
                            free_source_url: row.get(13)?,
                            free_verified_at: row.get(14)?,
                            temperature: row.get(15)?,
                            timeout_seconds: row.get(16)?,
                            configuration_version: row.get(17)?,
                            protocol: row.get(18)?, capability_profile: decode_profile(row.get(19)?)?,
                        })
                    },
                )
                .optional()
                .map_err(|_| "configured model could not be read".to_string())?
                .ok_or_else(|| "configured model was not found".to_string())
        })
    }

    pub(crate) fn record_capability_check(
        &self,
        configured_model_id: &str,
        capability: &str,
        status: &str,
        error_code: Option<&str>,
        retry_after_seconds: Option<u64>,
    ) -> Result<(), String> {
        let model = self.configured_model_runtime(configured_model_id)?;
        self.record_capability_check_for_versions(configured_model_id, capability, status,
            error_code, retry_after_seconds,
            (model.configuration_version, model.connection_configuration_version, model.credential_version))
    }

    pub(crate) fn record_capability_check_for_versions(
        &self, configured_model_id: &str, capability: &str, status: &str,
        error_code: Option<&str>, retry_after_seconds: Option<u64>,
        versions: (i64, i64, i64),
    ) -> Result<(), String> {
        validate_configured_model_id(configured_model_id)?;
        validate_capability(capability)?;
        validate_capability_status(status)?;
        if let Some(value) = error_code {
            validate_capability_error_code(value)?;
        }
        if status == "ready" && (error_code.is_some() || retry_after_seconds.is_some()) {
            return Err("ready capability checks cannot carry an error".to_string());
        }
        let retry_after = retry_after_seconds
            .map(|value| i64::try_from(value).map_err(|_| "capability retry interval is invalid".to_string()))
            .transpose()?;
        if versions.0 < 1 || versions.1 < 1 || versions.2 < 0 {
            return Err("capability probe identity is invalid".to_string());
        }
        self.with_connection(|connection| {
            // Bind to the configuration that produced the response, never to
            // settings looked up after I/O. A stale probe remains historical
            // and the summary's current-version query will not reuse it.
            let (model_version, connection_version, credential_version) = versions;
            connection
                .execute(
                    "INSERT INTO model_capability_checks(configured_model_id, capability, model_configuration_version, connection_configuration_version, credential_version, status, checked_at, error_code, retry_after_seconds) VALUES (?1,?2,?3,?4,?5,?6,unixepoch(),?7,?8) ON CONFLICT(configured_model_id, capability, model_configuration_version, connection_configuration_version, credential_version) DO UPDATE SET status=excluded.status, checked_at=excluded.checked_at, error_code=excluded.error_code, retry_after_seconds=excluded.retry_after_seconds",
                    params![configured_model_id, capability, model_version, connection_version, credential_version, status, error_code, retry_after],
                )
                .map_err(|_| "capability check could not be saved".to_string())?;
            Ok(())
        })
    }

    pub(crate) fn credential_for_connection(
        &self,
        connection: &ConnectionRuntime,
    ) -> Result<Option<Zeroizing<String>>, String> {
        let provider = provider_registry::find(&connection.provider_id)
            .ok_or_else(|| "unknown provider".to_string())?;
        match connection.credential_ref.as_deref() {
            Some(reference) => self.vault.get(reference).map(Some),
            None if provider.requires_api_key => Err("MODEL_CREDENTIAL_MISSING".to_string()),
            None => Ok(None),
        }
    }

    pub(crate) fn mark_connection_status(
        &self,
        connection_id: &str,
        status: &str,
        error_code: Option<&str>,
    ) -> Result<(), String> {
        validate_health_status(status)?;
        if error_code.is_some_and(|value| value.len() > 64 || value.chars().any(char::is_control)) {
            return Err("model health error code is invalid".to_string());
        }
        self.with_connection(|connection| {
            let changed = connection
                .execute(
                    "UPDATE provider_connections SET status=?1, last_tested_at=unixepoch(), last_error_code=?2, updated_at=unixepoch() WHERE connection_id=?3",
                    params![status, error_code, connection_id],
                )
                .map_err(|_| "model connection status could not be saved".to_string())?;
            if changed == 0 {
                return Err("model connection was not found".to_string());
            }
            Ok(())
        })
    }
    pub(crate) fn mark_connection_status_for_versions(&self, id: &str, status: &str, code: Option<&str>, version: i64, credential_version: i64) -> Result<(),String> {
        validate_health_status(status)?;
        self.with_connection(|connection| { connection.execute("UPDATE provider_connections SET status=?1,last_tested_at=unixepoch(),last_error_code=?2 WHERE connection_id=?3 AND configuration_version=?4 AND credential_version=?5",params![status,code,id,version,credential_version]).map_err(|_| "Connection status unavailable")?; Ok(()) })
    }

    pub(crate) fn mark_probe_health_for_versions(
        &self, configured_model_id: &str, status: &str, error_code: Option<&str>,
        versions: (i64, i64, i64),
    ) -> Result<(), String> {
        validate_health_status(status)?;
        if let Some(code) = error_code { validate_capability_error_code(code)?; }
        self.with_connection(|connection| {
            // One transaction ties both summaries to the response-producing
            // model, endpoint and credential versions. Late probes remain
            // historical, never changing a newly configured model's health.
            connection.execute_batch("BEGIN IMMEDIATE").map_err(|_| "probe health transaction failed".to_string())?;
            let result = (|| {
                connection.execute(
                    "UPDATE configured_models SET health_status=?1, updated_at=unixepoch() WHERE configured_model_id=?2 AND configuration_version=?3 AND connection_id IN (SELECT connection_id FROM provider_connections WHERE configuration_version=?4 AND credential_version=?5)",
                    params![status, configured_model_id, versions.0, versions.1, versions.2],
                ).map_err(|_| "probe model health could not be saved".to_string())?;
                connection.execute(
                    "UPDATE provider_connections SET status=?1, last_tested_at=unixepoch(), last_error_code=?2, updated_at=unixepoch() WHERE configuration_version=?4 AND credential_version=?5 AND connection_id IN (SELECT connection_id FROM configured_models WHERE configured_model_id=?3 AND configuration_version=?6)",
                    params![status, error_code, configured_model_id, versions.1, versions.2, versions.0],
                ).map_err(|_| "probe connection health could not be saved".to_string())?;
                Ok(())
            })();
            if result.is_ok() { connection.execute_batch("COMMIT") }
            else { connection.execute_batch("ROLLBACK") }
                .map_err(|_| "probe health transaction could not finish".to_string())?;
            result
        })
    }

    pub fn delete_model(&self, configured_model_id: &str) -> Result<(), String> {
        validate_configured_model_id(configured_model_id)?;
        self.with_connection(|connection| {
            let changed = connection
                .execute(
                    "DELETE FROM configured_models WHERE configured_model_id=?1",
                    [configured_model_id],
                )
                .map_err(|_| "configured model could not be deleted".to_string())?;
            if changed == 0 {
                return Err("configured model was not found".to_string());
            }
            Ok(())
        })
    }

    fn connection_summary(
        &self,
        connection: &Connection,
        id: &str,
    ) -> Result<ConnectionSummary, String> {
        let (
            connection_id,
            provider_id,
            display_name,
            region,
            endpoint,
            credential_ref,
            credential_version,
            enabled,
            status,
            last_tested_at,
            last_error_code,
            configuration_version,
        ): (
            String,
            String,
            String,
            String,
            String,
            Option<String>,
            i64,
            i64,
            String,
            Option<i64>,
            Option<String>,
            i64,
        ) = connection
            .query_row(
                "SELECT connection_id, provider_id, display_name, region, endpoint, credential_ref, credential_version, enabled, status, last_tested_at, last_error_code, configuration_version FROM provider_connections WHERE connection_id=?1",
                [id],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                        row.get(6)?,
                        row.get(7)?,
                        row.get(8)?,
                        row.get(9)?,
                        row.get(10)?,
                        row.get(11)?,
                    ))
                },
            )
            .map_err(|_| "model connection was not found".to_string())?;
        let has_secret = match credential_ref.as_deref() {
            Some(target) => self.vault.has(target)?,
            None => false,
        };
        let (protocol, capability_profile) = load_contract(connection, &connection_id)?;
        Ok(ConnectionSummary {
            transport: load_transport(connection, &connection_id)?,
            protocol, capability_profile,
            connection_id,
            provider_id,
            display_name,
            region,
            endpoint,
            credential_version,
            has_secret,
            enabled: enabled != 0,
            status,
            last_tested_at,
            last_error_code,
            configuration_version,
        })
    }

    fn model_summary(
        &self,
        connection: &Connection,
        id: &str,
    ) -> Result<ConfiguredModelSummary, String> {
        let (provider_id, credential_ref): (String, Option<String>) = connection
            .query_row(
                "SELECT c.provider_id, c.credential_ref FROM configured_models m JOIN provider_connections c ON c.connection_id=m.connection_id WHERE m.configured_model_id=?1",
                [id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .map_err(|_| "configured model was not found".to_string())?;
        let has_secret = match credential_ref.as_deref() {
            Some(target) => self.vault.has(target)?,
            None => false,
        };
        let credential_available = provider_registry::find(&provider_id)
            .map(|definition| !definition.requires_api_key || has_secret)
            .unwrap_or(false);
        connection
            .query_row(
                "SELECT m.configured_model_id, m.connection_id, m.model_id, m.alias, m.free_tier, m.enabled, m.capabilities_json, m.billing_class, m.free_source_url, m.free_verified_at, m.health_status, m.last_discovered_at, m.context_window_hint, m.temperature, m.timeout_seconds, m.configuration_version, c.enabled, c.provider_id, c.credential_ref, c.configuration_version, c.credential_version FROM configured_models m JOIN provider_connections c ON c.connection_id=m.connection_id WHERE m.configured_model_id=?1",
                [id],
                |row| {
                    let capabilities: String = row.get(6)?;
                    let connection_enabled = row.get::<_, i64>(16)? != 0;
                    let checks = self.capability_checks(
                        connection,
                        id,
                        row.get(15)?,
                        row.get(19)?,
                        row.get(20)?,
                    )?;
                    Ok(ConfiguredModelSummary {
                        configured_model_id: row.get(0)?,
                        connection_id: row.get(1)?,
                        model_id: row.get(2)?,
                        alias: row.get(3)?,
                        free_tier: row.get::<_, i64>(4)? != 0,
                        enabled: row.get::<_, i64>(5)? != 0,
                        capabilities: serde_json::from_str(&capabilities).unwrap_or_default(),
                        billing_class: row.get(7)?,
                        free_source_url: row.get(8)?,
                        free_verified_at: row.get(9)?,
                        health_status: row.get(10)?,
                        last_discovered_at: row.get(11)?,
                        context_window_hint: row.get(12)?,
                        temperature: row.get(13)?,
                        timeout_seconds: row.get(14)?,
                        configuration_version: row.get(15)?,
                        connection_enabled,
                        credential_available,
                        capability_checks: checks,
                    })
                },
            )
            .map_err(|_| "configured model was not found".to_string())
    }

    fn capability_checks(
        &self,
        connection: &Connection,
        configured_model_id: &str,
        model_configuration_version: i64,
        connection_configuration_version: i64,
        credential_version: i64,
    ) -> Result<Vec<CapabilityCheckSummary>, rusqlite::Error> {
        let mut query = connection.prepare(
            "SELECT capability, status, checked_at, error_code, retry_after_seconds, model_configuration_version, connection_configuration_version, credential_version FROM model_capability_checks WHERE configured_model_id=?1 AND model_configuration_version=?2 AND connection_configuration_version=?3 AND credential_version=?4 ORDER BY capability",
        )?;
        let rows = query.query_map(
            params![configured_model_id, model_configuration_version, connection_configuration_version, credential_version],
            |row| {
                Ok(CapabilityCheckSummary {
                    capability: row.get(0)?,
                    status: row.get(1)?,
                    checked_at: row.get(2)?,
                    error_code: row.get(3)?,
                    retry_after_seconds: row.get(4)?,
                    model_configuration_version: row.get(5)?,
                    connection_configuration_version: row.get(6)?,
                    credential_version: row.get(7)?,
                })
            },
        )?;
        rows.collect()
    }
}

fn ensure_column(
    connection: &Connection,
    table: &str,
    column: &str,
    definition: &str,
) -> Result<(), String> {
    let mut statement = connection
        .prepare(&format!("PRAGMA table_info({table})"))
        .map_err(|_| "model center database could not be initialized".to_string())?;
    let names = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|_| "model center database could not be initialized".to_string())?;
    for name in names {
        if name.map_err(|_| "model center database could not be initialized".to_string())? == column
        {
            return Ok(());
        }
    }
    connection
        .execute(
            &format!("ALTER TABLE {table} ADD COLUMN {column} {definition}"),
            [],
        )
        .map_err(|_| "model center database could not be migrated".to_string())?;
    Ok(())
}

fn validate_configured_model_id(value: &str) -> Result<(), String> {
    if value.len() > 128
        || value.is_empty()
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-' | b'.'))
    {
        return Err("configured model id is invalid".to_string());
    }
    Ok(())
}

fn decode_profile(value: Option<String>) -> rusqlite::Result<Option<CapabilityProfile>> {
    value.map(|text| serde_json::from_str(&text).map_err(|error| rusqlite::Error::FromSqlConversionFailure(0,rusqlite::types::Type::Text,Box::new(error)))).transpose()
}
fn profile_wire_equal(left:Option<&str>,right:Option<&str>) -> bool {
    match (left,right) {
        (None,None)=>true,
        (Some(left),Some(right))=>match (serde_json::from_str::<CapabilityProfile>(left),serde_json::from_str::<CapabilityProfile>(right)) {
            (Ok(mut left),Ok(mut right))=>{left.source="legacy".into(); right.source="legacy".into(); left==right},
            _=>false,
        },
        _=>false,
    }
}
fn decode_transport(value: String) -> rusqlite::Result<ConnectionTransportSettings> {
    serde_json::from_str(&value).map_err(|error| rusqlite::Error::FromSqlConversionFailure(0,rusqlite::types::Type::Text,Box::new(error)))
}
fn load_transport(connection: &Connection,id: &str) -> Result<ConnectionTransportSettings,String> {
    connection.query_row("SELECT transport_settings FROM provider_connections WHERE connection_id=?1",[id],|row| decode_transport(row.get(0)?)).map_err(|_| "MODEL_TRANSPORT_INVALID".into())
}
fn load_contract(connection: &Connection, id: &str) -> Result<(String,Option<CapabilityProfile>),String> {
    connection.query_row("SELECT protocol,capability_profile FROM provider_connections WHERE connection_id=?1",[id],|row| Ok((row.get(0)?,decode_profile(row.get(1)?)?))).map_err(|_| "Connection contract could not be read".into())
}

fn validate_health_status(value: &str) -> Result<(), String> {
    if matches!(
        value,
        "untested" | "ready" | "unavailable" | "unauthorized" | "rate_limited" | "disabled"
    ) {
        Ok(())
    } else {
        Err("model health status is invalid".to_string())
    }
}

fn validate_capability(value: &str) -> Result<(), String> {
    if matches!(value, "text_chat" | "structured_json" | "vision") {
        Ok(())
    } else {
        Err("model capability is invalid".to_string())
    }
}

fn validate_capability_status(value: &str) -> Result<(), String> {
    if matches!(value, "ready" | "error" | "disabled" | "unavailable") {
        Ok(())
    } else {
        Err("capability check status is invalid".to_string())
    }
}

fn validate_capability_error_code(value: &str) -> Result<(), String> {
    if value.len() > 64
        || !value.starts_with("MODEL_")
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_uppercase() || byte.is_ascii_digit() || byte == b'_')
    {
        Err("capability error code is invalid".to_string())
    } else {
        Ok(())
    }
}

#[derive(Clone, Debug)]
pub(crate) struct SecretRotation {
    connection_id: String,
    old_ref: Option<String>,
    old_version: i64,
    new_ref: String,
    new_version: i64,
}

#[derive(Clone, Debug)]
pub(crate) struct ConnectionRuntime {
    pub(crate) transport: ConnectionTransportSettings,
    pub(crate) protocol: String,
    pub(crate) capability_profile: Option<CapabilityProfile>,
    pub(crate) status: String,
    pub(crate) configuration_version: i64,
    pub(crate) credential_version: i64,
    pub(crate) provider_id: String,
    pub(crate) endpoint: String,
    pub(crate) credential_ref: Option<String>,
    pub(crate) enabled: bool,
}

#[derive(Clone, Debug)]
pub(crate) struct ConfiguredModelRuntime {
    pub(crate) transport: ConnectionTransportSettings,
    pub(crate) protocol: String,
    pub(crate) capability_profile: Option<CapabilityProfile>,
    pub(crate) configured_model_id: String,
    pub(crate) connection_id: String,
    pub(crate) provider_id: String,
    pub(crate) endpoint: String,
    pub(crate) credential_version: i64,
    pub(crate) connection_enabled: bool,
    pub(crate) connection_configuration_version: i64,
    pub(crate) model_id: String,
    pub(crate) alias: String,
    pub(crate) model_enabled: bool,
    pub(crate) capabilities: Vec<String>,
    pub(crate) billing_class: String,
    pub(crate) free_source_url: Option<String>,
    pub(crate) free_verified_at: Option<i64>,
    pub(crate) temperature: Option<f64>,
    pub(crate) timeout_seconds: i64,
    pub(crate) configuration_version: i64,
}

#[derive(Default)]
pub struct ModelCenterState {
    center: Mutex<Option<Arc<ModelCenter>>>,
    tests: Mutex<HashMap<String,(String,Arc<AtomicBool>)>>,
}
impl ModelCenterState {
    pub(crate) fn begin_test(&self, connection_id:&str, operation_id:&str) -> Result<Arc<AtomicBool>,String> {
        if operation_id.is_empty() || operation_id.len()>128 || operation_id.chars().any(char::is_control) { return Err("MODEL_TEST_ID_INVALID".into()); }
        let mut tests=self.tests.lock().map_err(|_| "MODEL_TEST_BUSY")?;
        if tests.contains_key(connection_id) || tests.values().any(|(id,_)|id==operation_id) { return Err("MODEL_TEST_BUSY".into()); }
        let cancel=Arc::new(AtomicBool::new(false));
        tests.insert(connection_id.into(),(operation_id.into(),cancel.clone()));
        Ok(cancel)
    }
    pub(crate) fn finish_test(&self,connection_id:&str,operation_id:&str) {
        if let Ok(mut tests)=self.tests.lock() {
            if tests.get(connection_id).is_some_and(|(id,_)|id==operation_id) { tests.remove(connection_id); }
        }
    }
    pub(crate) fn cancel_test(&self,operation_id:&str) {
        if let Ok(tests)=self.tests.lock() {
            if let Some((_,cancel))=tests.values().find(|(id,_)|id==operation_id) { cancel.store(true,Ordering::Release); }
        }
    }
    pub(crate) fn get(&self, app: &AppHandle) -> Result<Arc<ModelCenter>, String> {
        let mut slot = self
            .center
            .lock()
            .map_err(|_| "model center state is unavailable".to_string())?;
        if let Some(center) = slot.as_ref() {
            return Ok(center.clone());
        }
        let app_data_dir = app
            .path()
            .app_local_data_dir()
            .map_err(|_| "model center data directory is unavailable".to_string())?;
        let override_dir = std::env::var("OPENTHESIS_DATA_DIR").ok();
        let path = resolve_model_center_db_path(override_dir.as_deref(), &app_data_dir)?;
        let center = Arc::new(ModelCenter::new(path, platform_vault())?);
        *slot = Some(center.clone());
        Ok(center)
    }
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SaveConnectionInput {
    #[serde(default)]
    pub transport: Option<ConnectionTransportSettings>,
    #[serde(default)]
    pub protocol: Option<String>,
    #[serde(default)]
    pub capability_profile: Option<CapabilityProfile>,
    pub connection_id: String,
    pub provider_id: String,
    pub display_name: String,
    pub region: String,
    pub endpoint: String,
    pub enabled: bool,
}
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
pub struct SaveConfiguredModelInput {
    pub configured_model_id: String,
    pub connection_id: String,
    pub model_id: String,
    pub alias: String,
    pub enabled: bool,
    #[serde(default)]
    pub capabilities: Vec<String>,
    pub context_window_hint: Option<i64>,
    pub temperature: Option<f64>,
    pub timeout_seconds: Option<i64>,
}
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct ConnectionSummary {
    pub transport: ConnectionTransportSettings,
    pub protocol: String,
    pub capability_profile: Option<CapabilityProfile>,
    pub connection_id: String,
    pub provider_id: String,
    pub display_name: String,
    pub region: String,
    pub endpoint: String,
    pub credential_version: i64,
    pub has_secret: bool,
    pub enabled: bool,
    pub status: String,
    pub last_tested_at: Option<i64>,
    pub last_error_code: Option<String>,
    pub configuration_version: i64,
}
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub struct ConfiguredModelSummary {
    pub configured_model_id: String,
    pub connection_id: String,
    pub model_id: String,
    pub alias: String,
    pub free_tier: bool,
    pub enabled: bool,
    pub capabilities: Vec<String>,
    pub billing_class: String,
    pub free_source_url: Option<String>,
    pub free_verified_at: Option<i64>,
    pub health_status: String,
    pub last_discovered_at: Option<i64>,
    pub context_window_hint: Option<i64>,
    pub temperature: Option<f64>,
    pub timeout_seconds: i64,
    pub configuration_version: i64,
    pub connection_enabled: bool,
    pub credential_available: bool,
    pub capability_checks: Vec<CapabilityCheckSummary>,
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub struct CapabilityCheckSummary {
    pub capability: String,
    pub status: String,
    pub checked_at: i64,
    pub error_code: Option<String>,
    pub retry_after_seconds: Option<i64>,
    pub model_configuration_version: i64,
    pub connection_configuration_version: i64,
    pub credential_version: i64,
}
#[tauri::command]
pub fn model_center_list_providers() -> Vec<provider_registry::ProviderDefinition> {
    provider_registry::all()
}
#[tauri::command]
pub async fn model_center_model_directory(app: AppHandle, state: State<'_,ModelCenterState>, refresh: Option<bool>) -> Result<crate::model_directory::DirectorySnapshot,String> {
    let center = state.get(&app)?;
    tauri::async_runtime::spawn_blocking(move || center.model_directory(refresh.unwrap_or(false))).await.map_err(|_| "Directory worker unavailable")?
}
#[tauri::command]
pub fn model_center_list_connections(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
) -> Result<Vec<ConnectionSummary>, String> {
    state.get(&app)?.list_connections()
}
#[tauri::command]
pub fn model_center_save_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    input: SaveConnectionInput,
) -> Result<ConnectionSummary, String> {
    state.get(&app)?.save_connection(input)
}
#[tauri::command]
pub fn model_center_set_connection_secret(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    secret: String,
) -> Result<ConnectionSummary, String> {
    let secret = Zeroizing::new(secret);
    state.get(&app)?.set_secret(&connection_id, secret.as_str())
}
#[tauri::command]
pub fn model_center_set_connection_enabled(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
    enabled: bool,
) -> Result<ConnectionSummary, String> {
    state.get(&app)?.set_enabled(&connection_id, enabled)
}
#[tauri::command]
pub fn model_center_delete_connection(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    connection_id: String,
) -> Result<(), String> {
    state.get(&app)?.delete_connection(&connection_id)
}
#[tauri::command]
pub fn model_center_list_configured_models(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
) -> Result<Vec<ConfiguredModelSummary>, String> {
    state.get(&app)?.list_models()
}
#[tauri::command]
pub fn model_center_save_configured_model(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    input: SaveConfiguredModelInput,
) -> Result<ConfiguredModelSummary, String> {
    state.get(&app)?.save_model(input)
}

#[tauri::command]
pub fn model_center_delete_configured_model(
    app: AppHandle,
    state: State<'_, ModelCenterState>,
    configured_model_id: String,
) -> Result<(), String> {
    state.get(&app)?.delete_model(&configured_model_id)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::credentials::{CredentialVault, InMemoryVault};
    use tempfile::tempdir;
    fn center_with(vault: Arc<dyn CredentialVault>) -> ModelCenter {
        let directory = tempdir().unwrap();
        let path = directory.keep().join("model-center.db");
        ModelCenter::new(path, vault).unwrap()
    }
    fn center() -> ModelCenter {
        center_with(Arc::new(InMemoryVault::default()))
    }
    fn connection() -> SaveConnectionInput {
        SaveConnectionInput {
            transport: None,
            protocol: None, capability_profile: None,
            connection_id: "primary".into(),
            provider_id: "openai".into(),
            display_name: "Primary".into(),
            region: "global".into(),
            endpoint: "https://api.openai.com/v1".into(),
            enabled: true,
        }
    }
    #[test]
    fn new_custom_defaults_and_legacy_override_preservation_321() {
        let center=center(); let mut input=connection();
        input.provider_id="custom".into(); input.endpoint="https://customer.example".into();
        let saved=center.save_connection(input.clone()).unwrap();
        assert_eq!(saved.endpoint,"https://customer.example/v1");
        assert_eq!(saved.capability_profile.as_ref().unwrap().source,"compatible_default");
        let mut declared=saved.capability_profile.clone().unwrap(); declared.source="user".into();
        input.capability_profile=Some(declared); input.display_name="Renamed".into();
        assert_eq!(center.save_connection(input.clone()).unwrap().configuration_version,saved.configuration_version);
        center.with_connection(|db| {db.execute("UPDATE provider_connections SET capability_profile=NULL WHERE connection_id='primary'",[]).unwrap();Ok(())}).unwrap();
        input.capability_profile=None;
        assert!(center.save_connection(input).unwrap().capability_profile.is_none(),"legacy unknowns must not be silently migrated");
    }
    #[test]
    fn model_tests_are_isolated_by_connection_and_cancel_token_321() {
        let state=ModelCenterState::default();
        let first=state.begin_test("one","op1").unwrap();
        assert!(state.begin_test("one","other").is_err());
        let second=state.begin_test("two","op2").unwrap();
        state.cancel_test("op1");
        assert!(first.load(Ordering::Acquire)); assert!(!second.load(Ordering::Acquire));
        state.finish_test("one","op1"); assert!(state.begin_test("one","op3").is_ok());
    }
    #[test]
    fn cosmetic_connection_rename_preserves_wire_version_320() {
        let center = center();
        let saved = center.save_connection(connection()).unwrap();
        let mut renamed = connection();
        renamed.display_name = "Only a display rename".into();
        let after = center.save_connection(renamed).unwrap();
        assert_eq!(saved.configuration_version, after.configuration_version);
        assert_eq!(saved.connection_id, after.connection_id);
    }
    #[test]
    fn catalog_and_alias_updates_do_not_invalidate_capabilities_320() {
        let center=center(); center.save_connection(connection()).unwrap();
        let mut input=SaveConfiguredModelInput{configured_model_id:"kept-id".into(),connection_id:"primary".into(),model_id:"Private/Exact-ID".into(),alias:"Before".into(),enabled:true,capabilities:vec!["text_chat".into()],context_window_hint:Some(12345),temperature:None,timeout_seconds:Some(180)};
        let before=center.save_model(input.clone()).unwrap();
        center.record_capability_check("kept-id","text_chat","ready",None,None).unwrap();
        input.alias="After".into(); let after=center.save_model(input).unwrap();
        assert_eq!(after.configuration_version,before.configuration_version); assert_eq!(after.capability_checks.len(),1);
        let snapshot=center.model_directory(false).unwrap(); assert!(!snapshot.models.is_empty());
        assert_eq!(center.list_models().unwrap()[0].context_window_hint,Some(12345));
        assert_eq!(center.list_connections().unwrap()[0].configuration_version,1);
    }
    #[test]
    fn probe_reservation_is_durable_without_prompts_or_credentials_320() {
        let center=center(); center.save_connection(connection()).unwrap();
        let id=center.reserve_metadata_probe("primary",1,0).unwrap();
        center.finish_probe(&id,"response_received",r#"{"generation_calls":0}"#).unwrap();
        center.with_connection(|db| { let row:(String,String,i64)=db.query_row("SELECT operation,outcome,reserved_calls FROM model_probe_receipts WHERE attempt_id=?1",[id],|row|Ok((row.get(0)?,row.get(1)?,row.get(2)?))).map_err(|e|e.to_string())?; assert_eq!(row,("metadata".into(),"response_received".into(),1)); Ok(()) }).unwrap();
    }
    #[test]
    fn probe_parent_budget_is_atomic_and_configuration_bound_320() {
        let center=center(); center.save_connection(connection()).unwrap();
        center.save_model(SaveConfiguredModelInput{configured_model_id:"test-budget".into(),connection_id:"primary".into(),model_id:"Exact-ID".into(),alias:"Budget".into(),enabled:true,capabilities:vec!["text_chat".into()],context_window_hint:None,temperature:None,timeout_seconds:Some(180)}).unwrap();
        let model=center.configured_model_runtime("test-budget").unwrap();
        center.reserve_probe("one-test",&model,"text").unwrap();
        center.reserve_probe("one-test",&model,"vision").unwrap();
        assert!(center.reserve_probe("one-test",&model,"unexpected-third-wire").is_err());
        let mut stale=model; stale.configuration_version+=1;
        assert!(center.reserve_probe("new-test",&stale,"text").is_err());
    }
    struct FailingVault;
    impl CredentialVault for FailingVault {
        fn put(&self, _: &str, _: &str) -> Result<(), String> {
            Err("vault failure".into())
        }
        fn get(&self, _: &str) -> Result<Zeroizing<String>, String> {
            Err("vault failure".into())
        }
        fn delete(&self, _: &str) -> Result<(), String> {
            Err("vault failure".into())
        }
        fn has(&self, _: &str) -> Result<bool, String> {
            Err("vault failure".into())
        }
    }
    #[test]
    fn data_directory_override_is_absolute_and_scoped_to_database_file() {
        let default = resolve_model_center_db_path(None, Path::new("C:/app-data")).unwrap();
        assert_eq!(default, PathBuf::from("C:/app-data/model-center.db"));
        assert_eq!(
            resolve_model_center_db_path(Some("D:/OpenThesis/data"), Path::new("C:/app-data"))
                .unwrap(),
            PathBuf::from("D:/OpenThesis/data/model-center.db")
        );
        assert!(
            resolve_model_center_db_path(Some("relative/data"), Path::new("C:/app-data")).is_err()
        );
        assert_eq!(
            resolve_model_center_db_path(Some("  "), Path::new("C:/app-data")).unwrap(),
            default
        );
    }
    #[test]
    fn database_has_metadata_but_never_secret() {
        let center = center();
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "top-secret").unwrap();
        let raw = std::fs::read(&center.db_path).unwrap();
        assert!(!raw
            .windows(b"top-secret".len())
            .any(|window| window == b"top-secret"));
        let summary = center.list_connections().unwrap().remove(0);
        assert!(summary.has_secret);
    }
    #[test]
    fn rotation_advances_version_without_returning_secret() {
        let center = center();
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "rotation-first-secret-321").unwrap();
        let second = center.set_secret("primary", "rotation-second-secret-321").unwrap();
        assert_eq!(second.credential_version, 2);
        assert!(second.has_secret);
        assert!(!serde_json::to_string(&second).unwrap().contains("rotation-second-secret-321"));
    }
    #[test]
    fn delete_disables_then_removes_connection_and_models() {
        let center = center();
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "one").unwrap();
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: "m1".into(),
                connection_id: "primary".into(),
                model_id: "gpt-test".into(),
                alias: "Test".into(),
                enabled: true,
                capabilities: vec![],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();
        center.delete_connection("primary").unwrap();
        assert!(center.list_connections().unwrap().is_empty());
        assert!(center.list_models().unwrap().is_empty());
    }
    #[test]
    fn delete_no_secret_ollama_connection() {
        let center = center();
        center
            .save_connection(SaveConnectionInput {
                transport: None,
                protocol: None, capability_profile: None,
                connection_id: "local".into(),
                provider_id: "ollama".into(),
                display_name: "Local".into(),
                region: "local".into(),
                endpoint: "http://localhost:11434".into(),
                enabled: true,
            })
            .unwrap();
        center.delete_connection("local").unwrap();
        assert!(center.list_connections().unwrap().is_empty());
    }
    #[test]
    fn vault_has_error_is_propagated() {
        let center = center_with(Arc::new(FailingVault));
        center.save_connection(connection()).unwrap();
        center.with_connection(|connection| { connection.execute("UPDATE provider_connections SET credential_ref='opaque-ref', has_secret=1 WHERE connection_id='primary'", []).map(|_| ()).map_err(|error| error.to_string()) }).unwrap();
        assert_eq!(center.list_connections().unwrap_err(), "vault failure");
    }
    #[test]
    fn staged_rotation_keeps_the_old_secret_until_atomic_activation() {
        let vault = InMemoryVault::default();
        let center = center_with(Arc::new(vault.clone()));
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "old-secret").unwrap();
        let before = center.connection_runtime("primary").unwrap();
        let old_ref = before.credential_ref.clone().unwrap();

        let staged = center.stage_secret("primary", "new-secret").unwrap();
        assert_eq!(&*center.staged_secret(&staged).unwrap(), "new-secret");
        assert_eq!(center.list_connections().unwrap()[0].credential_version, 1);
        assert_eq!(&*vault.get(&old_ref).unwrap(), "old-secret");

        let activated = center.activate_staged_secret(&staged).unwrap();
        assert_eq!(activated.credential_version, 2);
        assert!(!vault.has(&old_ref).unwrap());
        assert_eq!(&*vault.get(&staged.new_ref).unwrap(), "new-secret");
    }

    #[test]
    fn stale_rotation_is_discarded_without_replacing_the_current_secret() {
        let vault = InMemoryVault::default();
        let center = center_with(Arc::new(vault.clone()));
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "version-one").unwrap();

        let stale = center.stage_secret("primary", "stale-secret").unwrap();
        let winner = center.stage_secret("primary", "winner-secret").unwrap();
        center.activate_staged_secret(&winner).unwrap();
        let error = center.activate_staged_secret(&stale).unwrap_err();

        assert!(error.contains("changed during credential rotation"));
        assert!(!vault.has(&stale.new_ref).unwrap());
        assert_eq!(&*vault.get(&winner.new_ref).unwrap(), "winner-secret");
        assert_eq!(center.list_connections().unwrap()[0].credential_version, 2);
    }
    #[test]
    fn list_ignores_stale_sqlite_secret_flag() {
        let vault = InMemoryVault::default();
        let center = center_with(Arc::new(vault.clone()));
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "secret").unwrap();
        let reference: String = center
            .with_connection(|connection| {
                connection
                    .query_row(
                        "SELECT credential_ref FROM provider_connections WHERE connection_id='primary'",
                        [],
                        |row| row.get(0),
                    )
                    .map_err(|error| error.to_string())
            })
            .unwrap();
        vault.delete(&reference).unwrap();
        assert!(!center.list_connections().unwrap()[0].has_secret);
    }

    #[test]
    fn capability_checks_are_version_bound_and_summary_never_exposes_secret() {
        let center = center();
        center.save_connection(connection()).unwrap();
        center.set_secret("primary", "model-center-secret").unwrap();
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: "probe-model".into(),
                connection_id: "primary".into(),
                model_id: "gpt-test".into(),
                alias: "Probe".into(),
                enabled: true,
                capabilities: vec!["text_chat".into(), "structured_json".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();
        center
            .record_capability_check("probe-model", "text_chat", "ready", None, None)
            .unwrap();
        let schema: String = center
            .with_connection(|connection| {
                connection
                    .query_row(
                        "SELECT value FROM model_center_meta WHERE key='schema_version'",
                        [],
                        |row| row.get(0),
                    )
                    .map_err(|error| error.to_string())
            })
            .unwrap();
        assert_eq!(schema, "3");
        let first = center.list_models().unwrap().remove(0);
        assert!(first.connection_enabled);
        assert!(first.credential_available);
        assert_eq!(first.capability_checks.len(), 1);
        assert_eq!(first.capability_checks[0].capability, "text_chat");
        assert!(serde_json::to_string(&first)
            .unwrap()
            .contains("capability_checks"));
        assert!(!serde_json::to_string(&first)
            .unwrap()
            .contains("model-center-secret"));

        // Editing the model creates a new configuration generation.  The old
        // probe remains audit data but cannot back the new summary.
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: "probe-model".into(),
                connection_id: "primary".into(),
                model_id: "gpt-test-v2".into(),
                alias: "Probe v2".into(),
                enabled: true,
                capabilities: vec!["text_chat".into(), "structured_json".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();
        assert!(center.list_models().unwrap()[0].capability_checks.is_empty());
        let before_stale_probe = center.list_models().unwrap().remove(0).health_status;
        center.mark_probe_health_for_versions("probe-model", "unavailable", Some("MODEL_TIMEOUT"),
            (1, 1, 1)).unwrap();
        assert_eq!(center.list_models().unwrap()[0].health_status, before_stale_probe);
        center
            .record_capability_check(
                "probe-model",
                "structured_json",
                "error",
                Some("MODEL_RATE_LIMITED"),
                Some(19),
            )
            .unwrap();
        let current = center.list_models().unwrap().remove(0);
        assert_eq!(current.capability_checks.len(), 1);
        assert_eq!(current.capability_checks[0].status, "error");
        assert_eq!(current.capability_checks[0].retry_after_seconds, Some(19));
        assert!(center
            .record_capability_check(
                "probe-model",
                "structured_json",
                "error",
                Some("https://not-an-error-code"),
                None,
            )
            .is_err());
    }

    #[test]
    fn credential_availability_uses_provider_requirement_without_reading_secret() {
        let center = center();
        center.save_connection(connection()).unwrap();
        center
            .save_model(SaveConfiguredModelInput {
                configured_model_id: "without-secret".into(),
                connection_id: "primary".into(),
                model_id: "gpt-test".into(),
                alias: "No secret".into(),
                enabled: true,
                capabilities: vec!["text_chat".into()],
                context_window_hint: None,
                temperature: None,
                timeout_seconds: None,
            })
            .unwrap();
        assert!(!center.list_models().unwrap()[0].credential_available);
    }
}
