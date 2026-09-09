# Security Policy

## Reporting a vulnerability

Please report security or privacy issues privately through GitHub's **Report a vulnerability** feature rather than opening a public issue. Include reproduction steps and the affected version or commit when possible.

## Security model

Pi packages execute with the user's privileges and must be reviewed before installation. Pi Voice opens no network listener, does not auto-submit transcripts, and does not intentionally persist audio or transcript content. The Parakeet model may be downloaded from Hugging Face by its runtime dependencies.

Security support currently covers the latest commit on `main`.
