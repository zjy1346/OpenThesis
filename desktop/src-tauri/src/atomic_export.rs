//! One export-byte seam for reports, diagnostics and OT packages.
use std::fs::File;
use std::io::{self, Read, Seek, SeekFrom, Write};
use std::path::Path;

pub(crate) fn write_export(path: &Path, bytes: &[u8]) -> io::Result<()> {
    write_with(path, bytes, |file, content| file.write_all(content))
}

fn write_with<F>(path: &Path, bytes: &[u8], writer: F) -> io::Result<()>
where
    F: FnOnce(&mut File, &[u8]) -> io::Result<()>,
{
    let parent = path
        .parent()
        .filter(|value| !value.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    // Same directory avoids cross-filesystem moves. The final user file is
    // never opened for writing until the validated staging object replaces it.
    let mut staged = tempfile::Builder::new()
        .prefix(".openthesis-export-")
        .tempfile_in(parent)?;
    writer(staged.as_file_mut(), bytes)?;
    staged.as_file_mut().flush()?;
    staged.as_file().sync_all()?;
    verify_bytes(staged.as_file_mut(), bytes)?;
    staged
        .persist(path)
        .map(|_| ())
        .map_err(|error| error.error)
}

fn verify_bytes(file: &mut File, expected: &[u8]) -> io::Result<()> {
    if file.metadata()?.len() != expected.len() as u64 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "export length mismatch",
        ));
    }
    file.seek(SeekFrom::Start(0))?;
    let mut buffer = [0u8; 8192];
    for chunk in expected.chunks(buffer.len()) {
        file.read_exact(&mut buffer[..chunk.len()])?;
        if &buffer[..chunk.len()] != chunk {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "export content mismatch",
            ));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::{write_export, write_with};
    use std::fs;
    use std::io::{self, Write};

    #[test]
    fn successful_export_keeps_exact_complete_bytes() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("report.html");
        fs::write(&target, b"old committed export").unwrap();
        let bytes = "<article>人民币 1,234.50；全部证据</article>".as_bytes();
        write_export(&target, bytes).unwrap();
        assert_eq!(fs::read(&target).unwrap(), bytes);
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn partial_disk_write_failure_preserves_previous_export() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("report.html");
        let previous = b"old committed export with evidence";
        fs::write(&target, previous).unwrap();
        let error = write_with(&target, b"new complete export", |file, bytes| {
            file.write_all(&bytes[..3])?;
            Err(io::Error::other("injected disk write failure"))
        })
        .unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::Other);
        assert_eq!(fs::read(&target).unwrap(), previous);
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn incomplete_successful_writer_is_rejected_before_replacement() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("diagnostics.json");
        let previous = b"old verified diagnostic export";
        fs::write(&target, previous).unwrap();
        let result = write_with(&target, b"new complete bytes", |file, bytes| {
            file.write_all(&bytes[..3])
        });
        assert!(result.is_err());
        assert_eq!(fs::read(&target).unwrap(), previous);
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn same_length_corruption_cannot_replace_previous_export() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("report.md");
        fs::write(&target, b"previous source-backed report").unwrap();
        let result = write_with(&target, b"abcdef", |file, _| file.write_all(b"abcxef"));
        assert_eq!(result.unwrap_err().kind(), io::ErrorKind::InvalidData);
        assert_eq!(fs::read(&target).unwrap(), b"previous source-backed report");
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn failed_new_export_does_not_leave_a_partial_final_file() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("package.ot");
        assert!(write_with(&target, b"PK complete archive", |file, _| {
            file.write_all(b"PK")?;
            Err(io::Error::other("injected write failure"))
        })
        .is_err());
        assert!(!target.exists());
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
    }

    #[test]
    fn unavailable_destination_allows_retry_to_another_path() {
        let directory = tempfile::tempdir().unwrap();
        let unavailable = directory.path().join("missing").join("report.html");
        assert!(write_export(&unavailable, b"complete content").is_err());
        assert!(!unavailable.exists());
        let available = directory.path().join("report.html");
        write_export(&available, b"complete content").unwrap();
        assert_eq!(fs::read(&available).unwrap(), b"complete content");
    }

    #[test]
    fn replacement_failure_preserves_existing_destination_and_removes_staging() {
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("existing_directory");
        fs::create_dir(&target).unwrap();
        let child = target.join("previous.md");
        fs::write(&child, b"previous export").unwrap();
        assert!(write_export(&target, b"new export").is_err());
        assert_eq!(fs::read(child).unwrap(), b"previous export");
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[cfg(windows)]
    #[test]
    fn windows_locked_destination_is_not_deleted_or_overwritten() {
        use std::os::windows::fs::OpenOptionsExt;
        let directory = tempfile::tempdir().unwrap();
        let target = directory.path().join("report.html");
        fs::write(&target, b"previous complete export").unwrap();
        let locked = fs::OpenOptions::new()
            .read(true)
            .share_mode(1)
            .open(&target)
            .unwrap();
        assert!(write_export(&target, b"new export").is_err());
        assert_eq!(fs::read(&target).unwrap(), b"previous complete export");
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
        drop(locked);
        write_export(&target, b"new export").unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"new export");
    }
}
