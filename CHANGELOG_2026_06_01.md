# Changelog — June 1, 2026

## 1. Migration: WhatsApp to Telegram Ingress Switch
* **Core Transition:** Replaced the legacy WhatsApp Cloud API implementation entirely with the new, highly interactive Telegram Bot API integration (**DQ Villa Bot**, `@DQVillaBot`).
* **Ingress Changes:** Removed the legacy WhatsApp webhook route `/webhook/whatsapp` and all WhatsApp media download/template-binding utility codes.
* **New Telegram Flow:** Built `/webhook/telegram` as a lightweight webhook receiver that handles onboarding (requesting contact share authentication if the chat ID is unrecognized) and routes whitelisted messages to the Cloud Tasks processing pipeline.

---

## 2. Blocker Diagnosis & Resolution

### Vertex AI IAM Role
* **Issue:** Heavy-path worker tasks (`POST /tasks/process-inbound`) were throwing `google.api_core.exceptions.PermissionDenied` (403) when attempting to predict using Vertex AI.
* **Resolution:** Granted the `roles/aiplatform.user` IAM role to the default Compute service account (`806670676346-compute@developer.gserviceaccount.com`).
* **Command Executed:**
  ```bash
  gcloud projects add-iam-policy-binding project-2977ce39-2c58-42f0-b2d \
    --member="serviceAccount:806670676346-compute@developer.gserviceaccount.com" \
    --role="roles/aiplatform.user"
  ```

### Default Gemini Model Config
* **Issue:** The default `GEMINI_MODEL` environment variable in `app/config.py` was hardcoded to `"gemini-3.1-flash"`, which does not exist in Vertex AI and caused `404 Publisher Model... not found` errors.
* **Resolution:** Probed active models in the project workspace, identified `"gemini-2.5-flash"` as the correct active model name, and updated the default fallback value in `app/config.py`.
* **Config Edit:**
  ```python
  # app/config.py
  GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
  ```

---

## 3. Programmatic 24h Message Deletion Feature

### Telegram Deletion Helper
* **Changes:** Added the `delete_message` helper function in `app/telegram.py` wrapping the standard Telegram Bot API `deleteMessage` endpoint.
* **Code Implementation:**
  ```python
  def delete_message(chat_id: int, message_id: int) -> bool:
      if not TELEGRAM_BOT_TOKEN:
          raise RuntimeError("Telegram credentials not configured")
      url = f"{TELEGRAM_API_BASE}/deleteMessage"
      payload = {"chat_id": chat_id, "message_id": message_id}
      # Performs HTTP POST to Telegram Bot API with error handling
  ```

### Firestore Message Tracking & Integration
* **Changes:** Updated `write_message_turn` in `app/firestore_db.py` to accept optional `telegram_chat_id` (integer) and `telegram_message_id` (integer) parameters.
* **Database Persist:** Modified `process_inbound` in `main.py` to:
  * Parse incoming user Telegram message IDs from `inbound.message_id` (e.g. `tg_msg_<id>`).
  * Capture outgoing assistant message IDs returned by the `send_text_message` function.
  * Store both tracking IDs directly on user and assistant message documents in `conversations/{phone_e164}/messages/{message_id}`.

### Secure Deletion Job Endpoint
* **Changes:** Created a secure HTTP endpoint `/jobs/cleanup-messages` in `main.py` that handles the message sweep.
* **Business Logic:**
  1. Validates the signature header `X-HouseOps-Secret-Token` against a SHA-256 hash of your bot token.
  2. Queries all conversations and fetches subcollection message documents older than 24 hours.
  3. Checks the 48-hour API limitation on user messages; if a user message is older than 48 hours, it skips deletion and updates the document to avoid API validation errors.
  4. Deletes active messages from Telegram and sets the `telegram_deleted: true` flag in Firestore.
  5. **Note:** All transaction histories and conversation logs are preserved in your Firestore logs indefinitely for Gemini's contextual history; only the visual messages on the Telegram UI are deleted.

---

## 4. Hourly Cron Job Automation

* **Action:** Enabled the `cloudscheduler.googleapis.com` API and deployed a Cloud Scheduler job to automate the sweep at the top of every hour.
* **Job Properties:**
  * **Name:** `telegram-message-cleanup`
  * **Location:** `us-central1`
  * **Schedule:** `0 * * * *`
  * **Target URI:** `https://houseops-806670676346.us-central1.run.app/jobs/cleanup-messages`
  * **HTTP Method:** `POST`
  * **Auth Header:** `X-HouseOps-Secret-Token: 78b87fcfca1ecbf296b17a0225cc1ff64a132dd0d507119ff084b6eb2d68be41`
* **Command Executed:**
  ```bash
  gcloud scheduler jobs create http telegram-message-cleanup \
    --schedule="0 * * * *" \
    --uri="https://houseops-806670676346.us-central1.run.app/jobs/cleanup-messages" \
    --http-method=POST \
    --headers="X-HouseOps-Secret-Token=78b87fcfca1ecbf296b17a0225cc1ff64a132dd0d507119ff084b6eb2d68be41" \
    --location=us-central1
  ```

---

## 5. Test Suite & Deployment
* **Unit Tests:** Added 2 dedicated integration tests in `tests/test_main.py` covering the secret authorization success and failure paths. All 77/77 tests pass.
* **Cloud Run Release:** Rebuilt and deployed the service to Cloud Run on revision `houseops-00017-v9j`.
* **Git Repository:** Committed and pushed changes to remote `origin/main`.
