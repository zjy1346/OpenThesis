param(
    [Parameter(Mandatory = $true)][string]$Archive
)

$ErrorActionPreference = "Stop"

$resolvedArchive = (Resolve-Path -LiteralPath $Archive).Path
if ([IO.Path]::GetExtension($resolvedArchive) -ne ".zip") {
    throw "Privacy verification accepts only a ZIP release archive."
}

Add-Type -AssemblyName System.IO.Compression.FileSystem

$forbiddenEntryPatterns = @(
    '(?i)(^|/)(openthesis\.db|.+\.sqlite3?|\.env(?:\..+)?|settings\.json|preferences\.json|.+\.log)$',
    '(?i)(^|/)(sec-cache|filings|research-history|user-data)(/|$)'
)
$textExtensions = @(".cfg", ".ini", ".json", ".md", ".toml", ".txt", ".yaml", ".yml")
$forbiddenContentPatterns = [ordered]@{
    PrivateKey = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
    GitHubToken = '\bgh[pousr]_[A-Za-z0-9_]{20,}\b'
    ApiSecret = '\bsk-[A-Za-z0-9_-]{20,}\b'
    WindowsUserProfile = '(?i)C:\\Users\\[^\\\r\n]+'
    OtherUserProfile = '(?i)[D-Z]:\\Users\\[^\\\r\n]+'
    PersonalEmail = '(?i)\b[A-Z0-9._%+-]+@(?!(?:example\.(?:com|org)|localhost)\b)[A-Z0-9.-]+\.[A-Z]{2,}\b'
}

$violations = [Collections.Generic.List[string]]::new()
$archiveFile = [IO.Compression.ZipFile]::OpenRead($resolvedArchive)
try {
    foreach ($entry in $archiveFile.Entries) {
        $entryName = $entry.FullName.Replace("\", "/")
        foreach ($pattern in $forbiddenEntryPatterns) {
            if ($entryName -match $pattern) {
                $violations.Add("forbidden data entry: $entryName")
            }
        }

        $extension = [IO.Path]::GetExtension($entryName).ToLowerInvariant()
        if ($entry.Length -le 2MB -and $extension -in $textExtensions) {
            $stream = $entry.Open()
            $reader = [IO.StreamReader]::new($stream, [Text.Encoding]::UTF8, $true)
            try {
                $content = $reader.ReadToEnd()
                foreach ($rule in $forbiddenContentPatterns.GetEnumerator()) {
                    if ($content -match $rule.Value) {
                        $violations.Add("$($rule.Key) material in: $entryName")
                    }
                }
            } finally {
                $reader.Dispose()
                $stream.Dispose()
            }
        }
    }
} finally {
    $archiveFile.Dispose()
}

if ($violations.Count -gt 0) {
    throw "Release privacy verification failed:`n$($violations -join [Environment]::NewLine)"
}

[pscustomobject]@{
    Archive = $resolvedArchive
    ForbiddenDataEntries = 0
    CredentialOrPersonalDataMatches = 0
} | ConvertTo-Json -Compress
