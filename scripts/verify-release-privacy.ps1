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

# Public license notices must remain intact. Only these reviewed upstream bytes
# at their exact package paths may contain author contacts. This does not exempt
# license directories, changed files, credentials, or other personal data.
# Source: https://github.com/numpy/numpy/tree/v2.4.6/numpy
$reviewedLicenseHashes = @{
    'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/random/LICENSE.md' = 'B4BC2F4FA1C95778F1ED3DD8F14706BECE399475B978F9F34B5D3CD72521B48A'
    'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/random/src/mt19937/LICENSE.md' = '92C10699884321F1C2947FE2A70AB23B0FBF1507F756D89DD08F38C61FC3D4FA'
    'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/random/src/pcg64/LICENSE.md' = '41190663B77BEE5302386495A07B7EABFBFA571AA0AE2F11DA6764162062FC4F'
    'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/random/src/splitmix64/LICENSE.md' = '454EC7350E478E037061850B67395753B425B1CB0A34BBC26290B020BF89D763'
    'OpenThesis/bin/openthesis-sidecar/_internal/numpy-2.4.6.dist-info/licenses/numpy/_core/include/numpy/libdivide/LICENSE.txt' = 'D544761558B510866C21F7E8A2D5716FCA76E0B98117E3B6B86314DB7244D150'
}
$preservedPublicNotices = 0

$violations = [Collections.Generic.List[string]]::new()
$archiveFile = [IO.Compression.ZipFile]::OpenRead($resolvedArchive)
try {
    foreach ($entry in $archiveFile.Entries) {
        $entryName = $entry.FullName.Replace("\", "/")
        # Release archives are rooted at OpenThesis-X.Y.Z while the reviewed
        # public-notice allowlist is version-independent. Normalize only that
        # single archive-root segment; all package-relative paths and hashes
        # must still match exactly.
        $reviewedEntryName = $entryName -replace '^OpenThesis-[^/]+/', 'OpenThesis/'
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
                        if ($rule.Key -eq 'PersonalEmail' -and $reviewedLicenseHashes.ContainsKey($reviewedEntryName)) {
                            $hashStream = $entry.Open()
                            $hasher = [Security.Cryptography.SHA256]::Create()
                            try {
                                $digest = [BitConverter]::ToString($hasher.ComputeHash($hashStream)).Replace('-', '')
                            } finally {
                                $hasher.Dispose()
                                $hashStream.Dispose()
                            }
                            if ($digest -eq $reviewedLicenseHashes[$reviewedEntryName]) {
                                $preservedPublicNotices += 1
                                continue
                            }
                        }
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
    PreservedReviewedPublicLicenseNotices = $preservedPublicNotices
} | ConvertTo-Json -Compress
