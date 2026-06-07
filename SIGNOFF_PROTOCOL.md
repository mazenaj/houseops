# HouseOps End-of-Day Signoff Protocol

This protocol defines the mandatory steps required to verify, document, commit, and deploy changes before concluding work for the day. Following this protocol guarantees database and code consistency, clean source history, and active service stability.

---

## Signoff Checklist

### 1. Verification & Testing
Before committing, ensure that the local codebase is clean and fully operational:
* [ ] **Run local test suite**: Verify that all unit and integration tests pass successfully.
  ```bash
  .venv/bin/pytest
  ```
* [ ] **Run pre-commit checks**: Run local pre-commit hooks to verify code style formatting, static security analysis, and secret scans.
  ```bash
  pre-commit run --all-files
  ```

### 2. Log Updates & Documentation
Document all issues raised, design discussions, and implementation updates:
* [ ] **Update Consolidated Changelog**: Append today's changes to the top of the consolidated [CHANGELOG.md](file:///Users/terminal/houseops/CHANGELOG.md) in the project root. The entry must follow the existing convention:
  - **Title**: `## Month DD, YYYY — <Brief Summary>`
  - **Sections**: Detailed breakdowns of issues resolved (referencing file paths and lines), technical enhancements, and system upgrades.
* [ ] **Check Schema (SCHEMA.md)**: If database models, tool schemas, or runtime constraints were altered, update the central [SCHEMA.md](file:///Users/terminal/houseops/SCHEMA.md) to preserve the source of truth.

### 3. Git Operations
Commit and push all local modifications:
* [ ] **Check Untracked/Modified Files**: Run `git status` to ensure no changes are left unstaged.
* [ ] **Stage & Commit**: Group related fixes into descriptive commit messages:
  ```bash
  git add <files>
  git commit -m "<Module>: <Brief description of fix/feature>"
  ```
* [ ] **Push Commits**: Push the commits to the remote repository:
  ```bash
  git push origin main
  ```

### 4. Cloud Deployment & Index Verification
Rebuild and deploy changes to Google Cloud Run:
* [ ] **Deploy to Cloud Run**: Run the deployment script pre-populated with active environment variables.
  ```bash
  GCP_PROJECT_ID="project-2977ce39-2c58-42f0-b2d" \
  GCP_REGION="us-central1" \
  SERVICE_URL="https://houseops-806670676346.us-central1.run.app" \
  GCS_BUCKET="houseops-media-bucket-prod" \
  INBOUND_QUEUE="inbound-message-processing" \
  TASKS_SERVICE_ACCOUNT="806670676346-compute@developer.gserviceaccount.com" \
  TELEGRAM_BOT_TOKEN="<YOUR_TELEGRAM_BOT_TOKEN>" \
  TELEGRAM_OPS_BOT_TOKEN="<YOUR_TELEGRAM_OPS_BOT_TOKEN>" \
  ./deploy.sh
  ```
* [ ] **Verify Live Service URL**: Perform a curl test on the `/health` endpoint of the newly deployed revision to verify it responds with 200 OK.
* [ ] **Check Database Indexes**: If any new compound queries were added, verify that the corresponding Firestore index is created and active.

### 5. Workspace Cleanup
Clean up the local development environment:
* [ ] **Kill Background Tasks**: Ensure all background tasks are completed or cancelled:
  - Use `manage_task` or check logs.
* [ ] **Clean Up Subagents**: Terminate any active subagents defined during the session:
  - Use `manage_subagents` list/kill commands.
* [ ] **Remove Ephemeral Scratch Files**: Delete temporary debugging scripts and text files that are not part of the repository, or move them to the `.gemini/antigravity-cli/scratch` directory.
