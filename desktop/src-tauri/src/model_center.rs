use crate::credentials::{credential_target, platform_vault, validate_secret, CredentialVault};
use crate::provider_registry;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
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
        connection.execute_batch("CREATE TABLE IF NOT EXISTS model_center_meta (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS provider_connections (connection_id TEXT PRIMARY KEY NOT NULL, provider_id TEXT NOT NULL, display_name TEXT NOT NULL, region TEXT NOT NULL, endpoint TEXT NOT NULL, credential_ref TEXT, credential_version INTEGER NOT NULL DEFAULT 0, has_secret INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch())); CREATE TABLE IF NOT EXISTS configured_models (configured_model_id TEXT PRIMARY KEY NOT NULL, connection_id TEXT NOT NULL REFERENCES provider_connections(connection_id) ON DELETE CASCADE, model_id TEXT NOT NULL, alias TEXT NOT NULL, free_tier INTEGER NOT NULL DEFAULT 0, enabled INTEGER NOT NULL DEFAULT 1, capabilities_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL DEFAULT (unixepoch()), updated_at INTEGER NOT NULL DEFAULT (unixepoch()), UNIQUE(connection_id, model_id)); INSERT INTO model_center_meta(key, value) VALUES ('schema_version', '1') ON CONFLICT(key) DO UPDATE SET value=excluded.value;").map_err(|_| "model center database could not be initialized".to_string())?;
        for (table, column, definition) in [
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
        connection
            .execute(
                "INSERT INTO model_center_meta(key, value) VALUES ('schema_version', '2') ON CONFLICT(key) DO UPDATE SET value=excluded.value",
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
                summaries.push(ConnectionSummary {
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

    pub fn save_connection(&self, input: SaveConnectionInput) -> Result<ConnectionSummary, String> {
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
        provider_registry::validate_endpoint(&input.provider_id, &input.endpoint)?;
        if provider.provider_id != "custom" && input.endpoint.is_empty() {
            return Err("endpoint is required".to_string());
        }
        self.with_connection(|connection| {
            let existing: Option<(String, Option<String>, i64)> = connection.query_row("SELECT provider_id, credential_ref, has_secret FROM provider_connections WHERE connection_id=?1", [&input.connection_id], |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?))).optional().map_err(|_| "model connection could not be read".to_string())?;
            if let Some((existing_provider, credential_ref, has_secret)) = existing { if existing_provider != input.provider_id && (credential_ref.is_some() || has_secret != 0) { return Err("delete the existing connection before changing its provider".to_string()); } }
            connection.execute("INSERT INTO provider_connections(connection_id, provider_id, display_name, region, endpoint, enabled, updated_at) VALUES (?1,?2,?3,?4,?5,?6,unixepoch()) ON CONFLICT(connection_id) DO UPDATE SET provider_id=excluded.provider_id, display_name=excluded.display_name, region=excluded.region, endpoint=excluded.endpoint, enabled=excluded.enabled, status='untested', last_tested_at=NULL, last_error_code=NULL, configuration_version=provider_connections.configuration_version+1, updated_at=unixepoch()", params![input.connection_id, input.provider_id, input.display_name, input.region, input.endpoint, input.enabled as i64]).map_err(|_| "model connection could not be saved".to_string())?;
            connection.execute("UPDATE configured_models SET health_status='unverified', configuration_version=configuration_version+1, updated_at=unixepoch() WHERE connection_id=?1", [&input.connection_id]).map_err(|_| "configured model health could not be invalidated".to_string())?;
            self.connection_summary(connection, &input.connection_id)
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
                    "SELECT configured_model_id, connection_id, model_id, alias, free_tier, enabled, capabilities_json, billing_class, free_source_url, free_verified_at, health_status, last_discovered_at, context_window_hint, temperature, timeout_seconds, configuration_version FROM configured_models ORDER BY alias COLLATE NOCASE, configured_model_id",
                )
                .map_err(|_| "model center database is unavailable".to_string())?;
            let rows = query
                .query_map([], |row| {
                    let capabilities: String = row.get(6)?;
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
                    })
                })
                .map_err(|_| "model center database is unavailable".to_string())?;
            rows.map(|row| row.map_err(|_| "model center database is unavailable".to_string()))
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
            connection
                .execute(
                    "INSERT INTO configured_models(configured_model_id, connection_id, model_id, alias, free_tier, enabled, capabilities_json, billing_class, free_source_url, free_verified_at, health_status, last_discovered_at, context_window_hint, temperature, timeout_seconds, configuration_version, updated_at) VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,CASE WHEN ?9 IS NULL THEN NULL ELSE unixepoch() END,'unverified',unixepoch(),?10,?11,?12,1,unixepoch()) ON CONFLICT(configured_model_id) DO UPDATE SET connection_id=excluded.connection_id, model_id=excluded.model_id, alias=excluded.alias, free_tier=excluded.free_tier, enabled=excluded.enabled, capabilities_json=excluded.capabilities_json, billing_class=excluded.billing_class, free_source_url=excluded.free_source_url, free_verified_at=excluded.free_verified_at, health_status='unverified', last_discovered_at=excluded.last_discovered_at, context_window_hint=excluded.context_window_hint, temperature=excluded.temperature, timeout_seconds=excluded.timeout_seconds, configuration_version=configured_models.configuration_version+1, updated_at=unixepoch()",
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
                    ],
                )
                .map_err(|_| "configured model could not be saved".to_string())?;
            self.model_summary(connection, &input.configured_model_id)
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
                    "SELECT provider_id, endpoint, credential_ref, enabled FROM provider_connections WHERE connection_id=?1",
                    [connection_id],
                    |row| {
                        Ok(ConnectionRuntime {
                            provider_id: row.get(0)?,
                            endpoint: row.get(1)?,
                            credential_ref: row.get(2)?,
                            enabled: row.get::<_, i64>(3)? != 0,
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
                    "SELECT m.configured_model_id, m.connection_id, c.provider_id, c.endpoint, c.credential_version, c.enabled, c.configuration_version, m.model_id, m.alias, m.enabled, m.capabilities_json, m.billing_class, m.free_tier, m.free_source_url, m.free_verified_at, m.temperature, m.timeout_seconds, m.configuration_version FROM configured_models m JOIN provider_connections c ON c.connection_id=m.connection_id WHERE m.configured_model_id=?1",
                    [configured_model_id],
                    |row| {
                        let capabilities_json: String = row.get(10)?;
                        let billing_class: String = row.get(11)?;
                        let legacy_free = row.get::<_, i64>(12)? != 0;
                        Ok(ConfiguredModelRuntime {
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
                        })
                    },
                )
                .optional()
                .map_err(|_| "configured model could not be read".to_string())?
                .ok_or_else(|| "configured model was not found".to_string())
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

    pub(crate) fn mark_model_health(
        &self,
        configured_model_id: &str,
        status: &str,
    ) -> Result<(), String> {
        validate_health_status(status)?;
        self.with_connection(|connection| {
            let changed = connection
                .execute(
                    "UPDATE configured_models SET health_status=?1, updated_at=unixepoch() WHERE configured_model_id=?2",
                    params![status, configured_model_id],
                )
                .map_err(|_| "configured model health could not be saved".to_string())?;
            if changed == 0 {
                return Err("configured model was not found".to_string());
            }
            Ok(())
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
        Ok(ConnectionSummary {
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
        connection
            .query_row(
                "SELECT configured_model_id, connection_id, model_id, alias, free_tier, enabled, capabilities_json, billing_class, free_source_url, free_verified_at, health_status, last_discovered_at, context_window_hint, temperature, timeout_seconds, configuration_version FROM configured_models WHERE configured_model_id=?1",
                [id],
                |row| {
                    let capabilities: String = row.get(6)?;
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
                    })
                },
            )
            .map_err(|_| "configured model was not found".to_string())
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
    pub(crate) provider_id: String,
    pub(crate) endpoint: String,
    pub(crate) credential_ref: Option<String>,
    pub(crate) enabled: bool,
}

#[derive(Clone, Debug)]
pub(crate) struct ConfiguredModelRuntime {
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
}
impl ModelCenterState {
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
}
#[tauri::command]
pub fn model_center_list_providers() -> Vec<provider_registry::ProviderDefinition> {
    provider_registry::all()
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
            connection_id: "primary".into(),
            provider_id: "openai".into(),
            display_name: "Primary".into(),
            region: "global".into(),
            endpoint: "https://api.openai.com/v1".into(),
            enabled: true,
        }
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
        center.set_secret("primary", "one").unwrap();
        let second = center.set_secret("primary", "two").unwrap();
        assert_eq!(second.credential_version, 2);
        assert!(second.has_secret);
        assert!(!serde_json::to_string(&second).unwrap().contains("two"));
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
}
