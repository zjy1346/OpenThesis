use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use zeroize::Zeroizing;

pub trait CredentialVault: Send + Sync {
    fn put(&self, target: &str, secret: &str) -> Result<(), String>;
    #[allow(dead_code)]
    fn get(&self, target: &str) -> Result<Zeroizing<String>, String>;
    fn delete(&self, target: &str) -> Result<(), String>;
    fn has(&self, target: &str) -> Result<bool, String>;
}

pub fn credential_target(provider_id: &str, connection_id: &str, version: u32) -> String {
    format!("OpenThesis/2/provider/{provider_id}/connection/{connection_id}/version/{version}")
}
pub fn validate_secret(secret: &str) -> Result<(), String> {
    if secret.is_empty() || secret.len() > 4096 || secret.chars().any(char::is_control) {
        return Err("credential is invalid".to_string());
    }
    Ok(())
}

#[cfg(target_os = "windows")]
pub struct WindowsCredentialVault;
#[cfg(target_os = "windows")]
impl CredentialVault for WindowsCredentialVault {
    fn put(&self, target: &str, secret: &str) -> Result<(), String> {
        use std::ptr::null_mut;
        use windows_sys::Win32::Security::Credentials::{
            CredWriteW, CREDENTIALW, CRED_PERSIST_LOCAL_MACHINE, CRED_TYPE_GENERIC,
        };
        let target_w = wide(target);
        let bytes = Zeroizing::new(secret.as_bytes().to_vec());
        let mut credential = CREDENTIALW {
            Flags: 0,
            Type: CRED_TYPE_GENERIC,
            TargetName: target_w.as_ptr() as *mut u16,
            Comment: null_mut(),
            LastWritten: windows_sys::Win32::Foundation::FILETIME {
                dwLowDateTime: 0,
                dwHighDateTime: 0,
            },
            CredentialBlobSize: bytes.len() as u32,
            CredentialBlob: bytes.as_ptr() as *mut u8,
            Persist: CRED_PERSIST_LOCAL_MACHINE,
            AttributeCount: 0,
            Attributes: null_mut(),
            TargetAlias: null_mut(),
            UserName: null_mut(),
        };
        if unsafe { CredWriteW(&mut credential, 0) } == 0 {
            return Err("the Windows credential vault could not save the credential".to_string());
        }
        Ok(())
    }
    fn get(&self, target: &str) -> Result<Zeroizing<String>, String> {
        use std::ptr::null_mut;
        use windows_sys::Win32::Security::Credentials::{
            CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
        };
        let target_w = wide(target);
        let mut credential: *mut CREDENTIALW = null_mut();
        if unsafe { CredReadW(target_w.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) } == 0 {
            let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
            return if code == windows_sys::Win32::Foundation::ERROR_NOT_FOUND {
                Err("stored credential was not found".to_string())
            } else {
                Err("the Windows credential vault could not read the credential".to_string())
            };
        }
        let (blob_ptr, blob_len) = unsafe {
            (
                (*credential).CredentialBlob,
                (*credential).CredentialBlobSize as usize,
            )
        };
        let result = unsafe {
            let blob = std::slice::from_raw_parts(blob_ptr, blob_len);
            std::str::from_utf8(blob)
                .map(|secret| Zeroizing::new(secret.to_owned()))
                .map_err(|_| "stored credential is not valid UTF-8".to_string())
        };
        // CredReadW owns this buffer; wipe it before releasing it on both UTF-8 paths.
        unsafe {
            std::ptr::write_bytes(blob_ptr, 0, blob_len);
            CredFree(credential as *mut std::ffi::c_void);
        }
        result
    }
    fn delete(&self, target: &str) -> Result<(), String> {
        use windows_sys::Win32::Security::Credentials::{CredDeleteW, CRED_TYPE_GENERIC};
        let target_w = wide(target);
        let ok = unsafe { CredDeleteW(target_w.as_ptr(), CRED_TYPE_GENERIC, 0) };
        if ok == 0 {
            let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
            if code != windows_sys::Win32::Foundation::ERROR_NOT_FOUND {
                return Err(
                    "the Windows credential vault could not delete the credential".to_string(),
                );
            }
        }
        Ok(())
    }
    fn has(&self, target: &str) -> Result<bool, String> {
        use std::ptr::null_mut;
        use windows_sys::Win32::Security::Credentials::{
            CredFree, CredReadW, CREDENTIALW, CRED_TYPE_GENERIC,
        };
        let target_w = wide(target);
        let mut credential: *mut CREDENTIALW = null_mut();
        if unsafe { CredReadW(target_w.as_ptr(), CRED_TYPE_GENERIC, 0, &mut credential) } != 0 {
            unsafe { CredFree(credential as *mut std::ffi::c_void) };
            return Ok(true);
        }
        let code = unsafe { windows_sys::Win32::Foundation::GetLastError() };
        if code == windows_sys::Win32::Foundation::ERROR_NOT_FOUND {
            Ok(false)
        } else {
            Err("the Windows credential vault could not read the credential".to_string())
        }
    }
}
#[cfg(target_os = "windows")]
fn wide(value: &str) -> Vec<u16> {
    value.encode_utf16().chain(std::iter::once(0)).collect()
}

#[cfg(not(target_os = "windows"))]
pub struct WindowsCredentialVault;
#[cfg(not(target_os = "windows"))]
impl CredentialVault for WindowsCredentialVault {
    fn put(&self, _target: &str, _secret: &str) -> Result<(), String> {
        Err("the platform credential vault is unavailable".to_string())
    }
    fn get(&self, _target: &str) -> Result<Zeroizing<String>, String> {
        Err("the platform credential vault is unavailable".to_string())
    }
    fn delete(&self, _target: &str) -> Result<(), String> {
        Err("the platform credential vault is unavailable".to_string())
    }
    fn has(&self, _target: &str) -> Result<bool, String> {
        Err("the platform credential vault is unavailable".to_string())
    }
}

#[derive(Clone, Default)]
pub struct InMemoryVault {
    secrets: Arc<Mutex<HashMap<String, String>>>,
}
impl CredentialVault for InMemoryVault {
    fn put(&self, target: &str, secret: &str) -> Result<(), String> {
        let mut secrets = self
            .secrets
            .lock()
            .map_err(|_| "credential vault is unavailable".to_string())?;
        secrets.insert(target.to_string(), secret.to_string());
        Ok(())
    }
    fn get(&self, target: &str) -> Result<Zeroizing<String>, String> {
        let secrets = self
            .secrets
            .lock()
            .map_err(|_| "credential vault is unavailable".to_string())?;
        secrets
            .get(target)
            .cloned()
            .map(Zeroizing::new)
            .ok_or_else(|| "stored credential was not found".to_string())
    }
    fn delete(&self, target: &str) -> Result<(), String> {
        let mut secrets = self
            .secrets
            .lock()
            .map_err(|_| "credential vault is unavailable".to_string())?;
        secrets.remove(target);
        Ok(())
    }
    fn has(&self, target: &str) -> Result<bool, String> {
        let secrets = self
            .secrets
            .lock()
            .map_err(|_| "credential vault is unavailable".to_string())?;
        Ok(secrets.contains_key(target))
    }
}
pub fn platform_vault() -> Arc<dyn CredentialVault> {
    Arc::new(WindowsCredentialVault)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn target_is_versioned_and_does_not_contain_the_secret() {
        let target = credential_target("openai", "primary", 2);
        assert_eq!(
            target,
            "OpenThesis/2/provider/openai/connection/primary/version/2"
        );
        assert!(!target.contains("secret"));
    }
    #[test]
    fn memory_vault_roundtrip_and_delete_are_secret_free_at_the_interface() {
        let vault = InMemoryVault::default();
        let target = credential_target("ollama", "local", 1);
        vault.put(&target, "secret-value").unwrap();
        assert!(vault.has(&target).unwrap());
        assert_eq!(&*vault.get(&target).unwrap(), "secret-value");
        vault.delete(&target).unwrap();
        assert!(!vault.has(&target).unwrap());
    }
    #[test]
    fn secrets_are_bounded() {
        assert!(validate_secret("abc").is_ok());
        assert!(validate_secret("").is_err());
        assert!(validate_secret("line\nsecret").is_err());
    }
}
