# Security Policy

## Supported version

Only the latest commit on the default branch is supported. This project is an alpha workflow and does not promise security fixes for older commits or unpublished local artifacts.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting flow from the repository **Security** tab. Do not open a public issue containing credentials, private media, unpublished provider endpoints, consent artifacts, customer data, or a working exploit.

Include, when safe:

- affected commit and command;
- minimal reproduction using synthetic data;
- expected and observed boundary behavior;
- whether external Provider calls, uploads, local file reads, FFmpeg, model execution, or publication can occur;
- logs with secrets, personal paths, media, tokens, and customer identifiers removed.

## Security boundaries

- Never commit API keys, cookies, OAuth tokens, `.env` files, consent artifacts, private media, model weights, or generated customer outputs.
- Online model calls and uploads require explicit authorization contracts; publication is never implied by a successful render.
- External source trees and executables are user supplied. Pin and verify their hashes and licenses before use.
- `videocut-kit` has no reviewed repository-level license and is disabled unless an explicit external path is supplied.
- Model and asset terms are separate from the Apache-2.0 code license; see `MODEL_ASSET_LICENSES.md`.
- Treat JSON artifacts and media paths as untrusted input. Keep outputs outside read-only source bundles and avoid shared writable directories.

## Incident response

If a secret or sensitive asset was committed, rotate/revoke it first, stop distribution, preserve evidence, and assess whether Git history and published release assets require coordinated removal. Making a repository private or reverting a commit does not remove existing clones.