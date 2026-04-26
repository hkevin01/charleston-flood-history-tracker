# Security Policy

## Reporting a vulnerability

This project does not process user authentication, payment data, or personally identifiable information. It is a static data-visualization tool.

If you discover a security concern (e.g. a dependency with a known CVE, or XSS in the front-end), please **open a GitHub issue** labelled `security` rather than a public disclosure.

Include:
- A description of the issue
- Steps to reproduce or proof-of-concept
- The affected file(s) / version

We aim to triage security reports within **7 days**.

## Supported versions

| Version | Supported |
|---------|-----------|
| `main` branch | ✅ Yes |
| Older tagged releases | ❌ No — please update to `main` |

## Dependencies

- Front-end dependencies are loaded via CDN and pinned to specific versions in `index.html`.
- Python dependencies should be pinned in `requirements.txt` to avoid supply-chain drift.
