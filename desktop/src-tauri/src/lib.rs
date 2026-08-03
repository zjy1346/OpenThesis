mod backend;

use std::path::Path;
use tauri::AppHandle;
use tauri_plugin_dialog::DialogExt;
use tauri_plugin_opener::OpenerExt;

const MAX_EXPORT_BYTES: usize = 32 * 1024 * 1024;

fn sanitize_export_name(value: &str) -> String {
    let lower = value.to_ascii_lowercase();
    let extension = if lower.ends_with(".md") {
        ".md"
    } else if lower.ends_with(".txt") {
        ".txt"
    } else {
        ".html"
    };
    let stem = value
        .strip_suffix(extension)
        .or_else(|| value.strip_suffix(&extension.to_ascii_uppercase()))
        .unwrap_or(value);
    let mut safe = String::new();
    let mut separator_pending = false;
    for character in stem.chars() {
        if character.is_ascii_alphanumeric() || matches!(character, '-' | '_') {
            if separator_pending && !safe.is_empty() && !safe.ends_with('-') {
                safe.push('-');
            }
            separator_pending = false;
            if safe.len() < 80 {
                safe.push(character);
            }
        } else {
            separator_pending = true;
        }
    }
    let safe = safe.trim_matches('-');
    let stem = if safe.is_empty() {
        "OpenThesis-report"
    } else {
        safe
    };
    format!("{stem}{extension}")
}

fn content_for_export<'a>(path: &Path, markdown: &'a str, html: &'a str) -> &'a str {
    match path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("html" | "htm") => html,
        _ => markdown,
    }
}

fn is_allowed_external_url(value: &str) -> bool {
    if value.len() > 2048 {
        return false;
    }
    tauri::Url::parse(value).is_ok_and(|url| {
        url.scheme() == "https"
            && url.host_str().is_some()
            && url.username().is_empty()
            && url.password().is_none()
    })
}

#[tauri::command]
async fn export_report(
    app: AppHandle,
    suggested_name: String,
    markdown: String,
    html: String,
) -> Result<bool, String> {
    if markdown.len().saturating_add(html.len()) > MAX_EXPORT_BYTES {
        return Err("report is too large to export".to_string());
    }
    let file_name = sanitize_export_name(&suggested_name);
    tauri::async_runtime::spawn_blocking(move || {
        let selected = app
            .dialog()
            .file()
            .set_title("Export OpenThesis report")
            .set_file_name(file_name)
            .add_filter("HTML", &["html"])
            .add_filter("Markdown", &["md"])
            .add_filter("Text", &["txt"])
            .blocking_save_file();
        let Some(selected) = selected else {
            return Ok(false);
        };
        let path = selected
            .into_path()
            .map_err(|_| "the selected export location is unavailable".to_string())?;
        let content = content_for_export(&path, &markdown, &html);
        std::fs::write(path, content).map_err(|_| "the report could not be written".to_string())?;
        Ok(true)
    })
    .await
    .map_err(|_| "the export dialog stopped unexpectedly".to_string())?
}

#[tauri::command]
fn open_external_url(app: AppHandle, url: String) -> Result<(), String> {
    if !is_allowed_external_url(&url) {
        return Err("only secure external links are allowed".to_string());
    }
    app.opener()
        .open_url(url, None::<&str>)
        .map_err(|_| "the external link could not be opened".to_string())
}

#[cfg(test)]
mod tests {
    use super::{
        content_for_export, is_allowed_external_url, sanitize_export_name,
    };
    use std::path::PathBuf;

    #[test]
    fn export_format_is_selected_only_from_the_chosen_extension() {
        assert_eq!(
            content_for_export(PathBuf::from("report.html").as_path(), "md", "html"),
            "html"
        );
        assert_eq!(
            content_for_export(PathBuf::from("report.md").as_path(), "md", "html"),
            "md"
        );
        assert_eq!(
            content_for_export(PathBuf::from("report.txt").as_path(), "md", "html"),
            "md"
        );
    }

    #[test]
    fn export_name_and_external_urls_are_bounded() {
        assert_eq!(
            sanitize_export_name("AAPL:../../report.html"),
            "AAPL-report.html"
        );
        assert!(is_allowed_external_url("https://example.com/help"));
        assert!(!is_allowed_external_url("file:///private/data"));
        assert!(!is_allowed_external_url("javascript:alert(1)"));
    }
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(backend::BackendState::default())
        .invoke_handler(tauri::generate_handler![
            backend::backend_request,
            export_report,
            open_external_url
        ])
        .run(tauri::generate_context!())
        .expect("error while running OpenThesis desktop");
}
