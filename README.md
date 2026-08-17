# LinkedIn Engineering Auto-Poster

Lightweight, serverless automation that posts engineering portfolio updates to your LinkedIn profile on a schedule using Python + GitHub Actions.

Supports both **text-only** posts and **native document** posts (PDF, PPT/PPTX, DOC/DOCX). Native documents appear as swipeable carousels / in-app viewers on LinkedIn.

## Features

- Sequential content queue (`posts/` folder)
- **Native document posts** (PDF / PPT / DOC) with full upload + processing lifecycle
- Exponential-backoff polling until the document is `AVAILABLE`
- Automatic archival of both text and document files after successful publish
- Cron-scheduled (Tue/Thu 09:00 UTC by default) + manual trigger
- Structured logging (console + rotating file)
- Secrets-based credentials (no tokens in code)

## Prerequisites

1. A LinkedIn account
2. A GitHub account
3. Basic familiarity with GitHub Actions secrets

## Step 1: LinkedIn Developer App

1. Go to the [LinkedIn Developer Portal](https://www.linkedin.com/developers/) and create a new app.
2. In the app’s **Products** tab, add **Share on LinkedIn** (self-serve for posting to your own profile).
3. Generate an OAuth 2.0 access token (3-legged flow) that includes the `w_member_social` scope.
   - Tokens typically expire after ~60 days — rotate the secret or implement refresh-token logic for long-term use.
4. Obtain your Person ID:
   - Call `GET https://api.linkedin.com/v2/me` with the access token.
   - Use the `id` field (or the full `urn:li:person:…`). The script normalizes either form.

> Posting to your own profile does **not** require Marketing Developer Platform / partner approval.

## Step 2: Repository Structure

```
linkedin-engineering-automation/
├── .github/workflows/auto_post.yml
├── posts/
│   ├── post-01-c11-linter.txt
│   ├── post-02-architecture.txt      # text commentary
│   ├── post-02-architecture.pdf      # companion document → native document post
│   └── post-03-rag-stack.txt
├── archived/                         # successfully published files land here
├── logs/                             # rotating log files (git-ignored)
├── main.py
├── requirements.txt
└── README.md
```

## Step 3: Content Queue (`posts/`)

### Text-only posts

Just drop a `.txt` or `.md` file:

```
posts/post-01-c11-linter.txt
```

### Native document posts

Place a document that shares the **same stem** as the text file:

```
posts/post-02-architecture.txt
posts/post-02-architecture.pdf      ← automatically detected
```

Supported document extensions: `.pdf` `.ppt` `.pptx` `.doc` `.docx`

Limits (LinkedIn):
- Max file size: **100 MB**
- Max pages: **300**

The script will:
1. Initialize the document upload
2. Upload the binary
3. Poll with exponential backoff until status = `AVAILABLE`
4. Create the post referencing the document URN
5. Archive **both** the text file and the document

## Step 4: Environment Variables / Secrets

| Secret / Env Var | Required | Description |
|------------------|----------|-------------|-------------|
| `LINKEDIN_ACCESS_TOKEN` | Yes | Bearer token with `w_member_social` |
| `LINKEDIN_PERSON_ID` | Yes | Raw person ID or full `urn:li:person:…` |
| `LOG_LEVEL` | No | `DEBUG` / `INFO` / `WARNING` … (default `INFO`) |
| `LOG_DIR` | No | Directory for rotating logs (default `logs/`) |
| `LOG_MAX_BYTES` | No | Rotation threshold (default 5 MB) |
| `LOG_BACKUP_COUNT` | No | Number of backup files (default 5) |
| `DOCUMENT_POLL_TIMEOUT` | No | Max seconds to wait for document processing (default 180) |
| `DOCUMENT_POLL_INITIAL` | No | Initial poll interval in seconds (default 1.0) |
| `DOCUMENT_POLL_MAX` | No | Max poll interval (default 12.0) |
| `DOCUMENT_POLL_MULTIPLIER` | No | Backoff multiplier (default 2.0) |
| `DOCUMENT_POLL_JITTER` | No | Jitter factor 0–1 (default 0.25) |

## Step 5: GitHub Actions Workflow

Scheduled Tuesdays & Thursdays at 09:00 UTC + manual `workflow_dispatch`.

The workflow:

1. Checks out the repo
2. Sets up Python 3.11
3. Installs dependencies
4. Runs `main.py` with the secrets
5. Commits any archived files back to `main` (`[skip ci]`)

## Step 6: First Run

1. Add the two required secrets under **Settings → Secrets and variables → Actions**.
2. Go to the **Actions** tab → **Automated LinkedIn Engineering Posts** → **Run workflow**.
3. On success you will see the post on LinkedIn and the files moved into `archived/`.

## How Document Posting Works (under the hood)

```
1. POST /rest/documents?action=initializeUpload
   → returns uploadUrl + urn:li:document:…

2. PUT  {uploadUrl}   (raw binary)

3. Poll GET /rest/documents/{urn}
   with exponential backoff + jitter
   until status == AVAILABLE

4. POST /rest/posts
   with content.media = { id: documentUrn, title: "filename.pdf" }
```

## Important Notes

- **Token lifetime** – rotate the access token before it expires (~60 days).
- **Rate limits** – LinkedIn allows roughly 150 member posts per day; the schedule is intentionally sparse.
- **Permissions** – the token must belong to the same member as `LINKEDIN_PERSON_ID`.
- **Archival** – both the text file and its companion document (if any) are moved to `archived/`.

## License

MIT – feel free to adapt for your own portfolio automation.
