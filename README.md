# LinkedIn Engineering Auto-Poster

Lightweight, serverless automation that posts engineering portfolio updates to your LinkedIn profile on a schedule using Python + GitHub Actions.

This keeps your LinkedIn presence consistent without manual effort. Content lives as simple text files in the repo; the workflow picks the next one, publishes it via the official LinkedIn Posts API, and archives it.

## Features

- Sequential content queue (`posts/` folder)
- Automatic archival after successful publish
- Cron-scheduled (Tue/Thu 09:00 UTC by default) + manual trigger
- Uses official LinkedIn REST Posts API (`/rest/posts`)
- Secrets-based credentials (no tokens in code)

## Prerequisites

1. A LinkedIn account
2. A GitHub account
3. Basic familiarity with GitHub Actions secrets

## Step 1: LinkedIn Developer App

1. Go to the [LinkedIn Developer Portal](https://www.linkedin.com/developers/) and create a new app (you can associate it with a company page or use personal).
2. In the app’s **Products** tab, request / add **Share on LinkedIn** (self-serve for posting to your own profile).
3. Under **Auth**, note the Client ID / Secret if needed, but for this flow you mainly need a member access token with the `w_member_social` scope.
4. Generate an OAuth 2.0 access token (3-legged flow) that includes `w_member_social`.  
   - Tokens expire (typically 60 days). You will need to refresh periodically or implement a refresh-token flow for long-term unattended use.
5. Obtain your Person URN / ID:
   - Call `GET https://api.linkedin.com/v2/me` (or the current equivalent profile endpoint) with your access token.
   - The response contains an `id` field (e.g. `abc123XYZ`). Your author value will be `urn:li:person:abc123XYZ`.
   - Store **only the ID part** (or the full URN – the script normalizes it) as the secret.

> **Note**: Posting to your own profile via Share on LinkedIn does **not** require Marketing Developer Platform / partner approval.

## Step 2: Repository Structure

```
linkedin-engineering-automation/
├── .github/
│   └── workflows/
│       └── auto_post.yml
├── posts/
│   ├── post-01-c11-linter.txt
│   ├── post-02-embedded-ci.txt
│   └── post-03-rag-stack.txt
├── archived/                 # created automatically; posted files land here
├── main.py
├── requirements.txt
└── README.md
```

## Step 3: Content Queue (`posts/`)

Place each post as a plain `.txt` (or `.md`) file. The script always takes the **lexicographically first** remaining file, posts it, then moves it to `archived/`.

Example (`posts/post-01-c11-linter.txt`):

```
Just open-sourced a strict POSIX shell-based linter and test suite enforcing strict C11 compliance rules for embedded systems development.

Ensuring memory safety and MISRA-like discipline in resource-constrained environments shouldn't require heavy toolchains. Built this lightweight framework to run straight out of standard POSIX environments and mobile shells.

Check out the code and drop your thoughts below!
#EmbeddedSystems #CProgramming #Automation #DevOps #C11
```

Keep files named so they sort in the desired order (e.g. `post-01-...`, `post-02-...`).

## Step 4: Automation Script (`main.py`)

The script:

1. Finds the next pending post.
2. Posts it to LinkedIn via `POST https://api.linkedin.com/rest/posts`.
3. On success, moves the file into `archived/`.

It expects two environment variables (provided by GitHub Actions secrets):

- `LINKEDIN_ACCESS_TOKEN` – Bearer token with `w_member_social`
- `LINKEDIN_PERSON_ID` – either the raw ID or the full `urn:li:person:...`

## Step 5: GitHub Actions Workflow

The workflow runs on a cron schedule (Tuesdays & Thursdays 09:00 UTC) and also supports manual `workflow_dispatch`.

It:

- Checks out the repo
- Sets up Python 3.11
- Installs dependencies
- Runs `main.py` with the secrets
- Commits the archived file back to the repo

## Step 6: Secrets & First Run

1. Push this repository (or fork it).
2. In the repo: **Settings → Secrets and variables → Actions** → New repository secret:
   - `LINKEDIN_ACCESS_TOKEN`
   - `LINKEDIN_PERSON_ID`
3. Go to the **Actions** tab → select **Automated LinkedIn Engineering Posts** → **Run workflow** to test.
4. After a successful run you should see the post on your LinkedIn profile and the corresponding file moved under `archived/`.

## Important Notes & Limitations

- **Token lifetime**: LinkedIn member tokens expire. Plan to rotate the secret or add a refresh-token step.
- **Rate limits**: LinkedIn enforces daily post limits (around 150 for members). The schedule is intentionally sparse.
- **Content only**: This version posts text only. Media / multi-image / video support can be added later via the same Posts API.
- **Permissions**: The token must belong to the same member whose Person ID you supply.
- **Archival**: The commit step uses `[skip ci]` so the archive commit does not re-trigger the workflow.

## Customization Ideas

- Change the cron expression in `.github/workflows/auto_post.yml`.
- Add image support (upload media first, then reference the media URN in the post payload).
- Use a queue file or database instead of the filesystem for more complex scheduling.
- Add Slack / email notification on success or failure.

## License

MIT – feel free to adapt for your own portfolio automation.
