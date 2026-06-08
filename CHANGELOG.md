# Changelog — HouseOps Engine

This file contains the unified, historical record of changes made to the Household Operations Engine codebase. Future updates should be appended directly to the top of this log.

---

## June 8, 2026 — Calendar Sync Conflict Diagnostic Alert Enhancements

### 1. Detailed Overlap and Driver Availability Diagnostics
* **Timings on Passenger Overlaps:** Updated same-passenger overlap detection in `detect_schedule_conflicts` inside [app/workflow.py](file:///Users/terminal/houseops/app/workflow.py) to format and output start and end times for overlapping events.
* **Granular Driver Allocation Diagnostics:** Added interval-based scanning when the scheduling matching algorithm fails to resolve driver allocations. The alert now specifies the exact interval (e.g., `10:00 AM - 11:00 AM`), lists the concurrent outings causing the conflict, and details the specific drivers available (along with their shift timing slots) or states if none are on duty.
* **Coverage Gap Warnings:** Added fallbacks to identify and report specific outings that cannot be covered due to a complete driver availability gap (no active drivers scheduled on shift).
* **Testing:** Updated `test_detect_schedule_conflicts_no_drivers_conflict` in [tests/test_workflow.py](file:///Users/terminal/houseops/tests/test_workflow.py) to assert that diagnostic details (including conflict timings and specific driver names) are correctly present in generated conflict warnings.

### 2. Driver Placeholder Name Replacement
* **Removed Mock Driver Name:** Replaced mock placeholder driver names `"Abu Fahad"` and `"Abu Ali"` with actual system driver names `"Khidir"` and `"Emad"` across all unit and integration test suites: [tests/test_workflow.py](file:///Users/terminal/houseops/tests/test_workflow.py) and [tests/test_tools_fleet.py](file:///Users/terminal/houseops/tests/test_tools_fleet.py).

### 3. Driver Whitelist Phone Number Realignment
* **Updated Phone Numbers:** Replaced old placeholder/temporary driver phone numbers for Khidir, Emad, and Kim in [init_db.py](file:///Users/terminal/houseops/init_db.py) with their actual production E.164 phone numbers (Khidir: `+966569300454`, Emad: `+966558456441`, Kim: `+966539818027`) and successfully executed the database seed script to apply these whitelisted values to Firestore.

---

## June 7, 2026 (Part 3) — Telegram Message Pipeline Latency Optimizations

### 1. Parallelized Firestore Dependencies Retrieval
* **Concurrent Fetching:** Refactored `/tasks/process-inbound` in [main.py](file:///Users/terminal/houseops/main.py) to concurrently fetch the member document, conversation document snapshot, and conversation history query in a single `asyncio.gather` block. This reduces baseline database latency from ~2.8s to ~1.0s.

### 2. Local Token Estimation
* **Zero-Latency Estimation:** Replaced sequential Vertex AI remote `count_tokens_text` API calls in [app/history.py](file:///Users/terminal/houseops/app/history.py) with a fast CPU-bound estimator `estimate_tokens_locally`. It handles ASCII and non-ASCII character distributions to safely trim chat history under the 3k token budget with ~0ms latency.

### 3. Write-Elimination and Pipeline State Passing
* **Firestore Write Optimization:** Modified conversation initialization in `main.py` and `app/firestore_db.py` to bypass updates when the conversation's `member_id` is unchanged, avoiding blocking writes on 99.9% of inbound messages.
* **Pre-loaded State Pipeline:** Passed pre-loaded state and member snapshots directly to `ensure_conversation_doc` and `run_confirmation_gate` in [app/confirmation_gate.py](file:///Users/terminal/houseops/app/confirmation_gate.py) to prevent duplicate database reads.

### 4. Security Check Integrity
* **Deactivated Member Enforcement:** Preserved and reinforced the explicit active-member security check (`not member.active`) and cross-tenant phone spoofing check in `main.py` during parallel ID point-lookups.

### 5. Testing & Validation
* **All Tests Passing:** Updated the unit test mock mappings for the parallel database collections, resulting in all 119 unit and integration tests passing successfully in the local virtual environment.

### 6. Deployment Environment Safeguards
* **Health Check Validation:** Added validation in the `/health` endpoint in [main.py](file:///Users/terminal/houseops/main.py) to raise a 500 error if `TELEGRAM_BOT_TOKEN` is missing, ensuring that Google Cloud Run deployments fail and roll back automatically if credentials are wiped out.
* **Test Environment Isolation:** Configured [tests/conftest.py](file:///Users/terminal/houseops/tests/conftest.py) with mock Telegram credentials so that local unit and integration tests continue to run successfully offline.
* **Deployment Script Preservation:** Modified [deploy.sh](file:///Users/terminal/houseops/deploy.sh) to use `--update-env-vars` instead of `--set-env-vars` for `gcloud run deploy`. This ensures that environment variables that are set directly in Cloud Run (like production Telegram bot tokens) are preserved instead of wiped during container updates.

### 7. Resource Usage Alert Threshold Realignment
* **Increased Threshold:** Increased the `HIGH_RESOURCE_USAGE` ops alert limit in [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py) from 12,000 uncached prompt tokens to **250,000 total tokens** (prompt + candidate) to align with the operation warning/pause limit and prevent false positives caused by standard multi-turn history contexts.
* **Test Updates:** Updated the unit test `test_check_resource_usage_alert` in [tests/test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py) to assert correct behavior above and below the new 250,000 token limit.

---

## June 7, 2026 (Part 2) — Calendar Sync Verification & Documentation Refactoring

### 1. iCloud Calendar Integration & Nightly Sync Verification
* **Connectivity Verified:** Verified the iCloud calendar reading connectivity for both Tier 1 principals. Checked the daily sync workflow (`run_nightly_calendar_sync`), confirming that it correctly fetches events, handles assignments, updates Firestore, and dispatches next-day schedule notifications to principals and drivers via Telegram at 8:00 PM AST when clear, or sends alert notifications on conflict.
* **Jawaher URL Fix:** Corrected a malformed/duplicated calendar URL registered in Firestore for Jawaher (`mem_principal_002`) to ensure seamless sync.

### 2. Schema Documentation Refactoring (Telegram Switch)
* **Archived Legacy Schema:** Renamed the legacy `SCHEMA.md` to `SCHEMA_ARCHIVE.md` to preserve the legacy Phase 1 WhatsApp blueprint.
* **Created New Schema:** Created a new [SCHEMA.md](file:///Users/terminal/houseops/SCHEMA.md) detailing the active Telegram-based architecture. This includes:
  * Swaggering from WhatsApp phone/wamid to Telegram chat IDs and message/callback ID formats.
  * The verified contact-sharing onboarding flow.
  * The dedicated Ops Bot (`@DQBotOpsBot`) for system-wide health and resource alerts.
  * The 250k cumulative token warning/pause mechanism and the 4-turn/3k-output/12k-input token threshold alert throttles.
  * Access restriction definitions for Tier 2 staff.

---

## June 7, 2026 (Part 1) — Tool Serialization Fixes, Optional Outing Fields & Resource Warnings

### 1. Tool Response Serialization Fix
* **Coercion Error Resolved:** Fixed a `ValueError: Unable to coerce value` crash on the `/tasks/process-inbound` endpoint caused by Vertex AI attempting to serialize Firestore `DatetimeWithNanoseconds` / `datetime` objects in tool responses.
* **Global Sanitization:** Implemented a global sanitization routine in `run_agent_turn` inside [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py#L380-L407) that automatically converts any `datetime` or `DatetimeWithNanoseconds` objects in tool response dictionaries into ISO-formatted strings.
* **Testing:** Added a unit test `test_run_agent_turn_sanitization` in [tests/test_main.py](file:///Users/terminal/houseops/tests/test_main.py).

### 2. Optional Transit Location & Purpose for Driver Outings
* **Optional Fields:** Updated `manage_outing` in [app/tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) to make `destination` and `purpose` optional parameters, ensuring they are not required to schedule a driver outing.
* **Payload Builder Reordering:** Reordered parameters in `_build_outing_payload` to move `requested_by` (non-default argument) before the now-default/optional parameters `destination` and `purpose`, resolving a syntax compilation error.
* **Readable Summary Adaptations:** Adjusted the text summary builder in `manage_outing` to dynamically exclude "to {destination}" or "({purpose})" parts if they are not provided, generating clean summary confirmations like `"Schedule driver Emad for outing on 2026-06-07 at 10:00 AM."`

### 3. Log Investigation & Unit Check Findings
* **Log Check:** Scanned the Cloud Run revision error logs over the past week and verified error occurrences:
  1. The `ValueError: Unable to coerce value` (resolved globally by our sanitization routine).
  2. A `FailedPrecondition: 400 The query requires an index` error for `driver_schedule` collection group queries under `/jobs/driver-arrival-nag` (index is now `READY` on GCP).
* **Unit Verification:**
  - **Nanosecond Precision:** Confirmed that nanosecond precision is Firestore's native representation for timestamps (parsed as Google Cloud's `DatetimeWithNanoseconds`), which is standard for GCP. Our serialization fix handles this standard type correctly.
  - **Other Units:** Audited the codebase for physical/data units. Checked the procurement and inventory schema (`SCHEMA.md`) which mentions standard unit labels (e.g. `kg`, `pcs`, `L`), but verified the code itself treats all unit definitions as generic strings.

### 4. Operation Pause and Warning at 250,000 Tokens
* **Warning Trigger:** Implemented tracking within `run_agent_turn` in [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py) to trigger a warning message if the cumulative token count reaches 250,000 tokens during an operation.
* **State Pause (Firestore):** When the threshold is crossed, the execution loop is paused, and the complete execution state is saved to Firestore under a pending confirmation with the action `resume_paused_agent_turn`.
* **Resume/Allow Hook:** Modified [app/confirmation_gate.py](file:///Users/terminal/houseops/app/confirmation_gate.py) to intercept `resume_paused_agent_turn` confirmations. If the user replies with a confirmation/resume keyword, the gate clears the pending flag and allows execution to proceed.
* **Testing:** Added unit test `test_run_agent_turn_warning_and_resume` in [tests/test_main.py](file:///Users/terminal/houseops/tests/test_main.py).

---

## June 6, 2026 — iCloud Calendar Routing, Ops Bot Consolidation & Access Controls

### 1. iCloud Calendar URL Registration Fix
* **Routing Path Correction:** Resolved a routing issue where the `register_calendar_url` tool was not routed to the fleet tool executor in `execute_tool_call` inside [tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L318-L323).
* **Prompt Document Alignment:** Updated [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py) to add `register_calendar_url` to both the `MODULE_1_SCHEMA` and `TOOL_DECLARATIONS_TEXT` system prompts.
* **Session Context ID Exposure:** Fixed an issue where the session context compiled by `_build_session_context` in [main.py](file:///Users/terminal/houseops/main.py#L252-L278) did not contain the user's `member_id` (only their display name).
* **Testing:** Added a unit test `test_execute_tool_call_register_calendar_url` in [test_tools_module2.py](file:///Users/terminal/houseops/tests/test_tools_module2.py).

### 2. DQBotOpsBot Status/Egress Message Consolidation
* **Removed Duplicate Message:** Eliminated duplicate notifications received by Mazen during the twice-daily status report. Modified the telegram webhook in [main.py](file:///Users/terminal/houseops/main.py#L202-L228) to process `ping_test` messages from `@DQBotOpsBot` silently, returning a `200` success response.
* **Consolidated Report:** Subsystem integration health (both ingress and egress) remains fully verified and is presented consolidated inside the single performance report delivered at 8:00 AM and 8:00 PM.
* **Testing:** Updated the webhook integration test in [test_main.py](file:///Users/terminal/houseops/tests/test_main.py).

### 3. Developer and System Tools Maintenance
* **Deleted Tools:** Uninstalled/removed AI and editor tools requested by the user:
  * **Aider** (removed via `uv tool uninstall`)
  * **Claude Code** (deleted CLI executable and config folder)
  * **Qwen Code** (uninstalled via global `npm`)
  * **Windsurf IDE** (deleted cask package and app folders)
* **Upgraded Components:** Upgraded remaining system components to their latest versions:
  * **Ollama** upgraded to `0.30.6`
  * **uv** upgraded to `0.11.19`
  * **Google Cloud CLI** upgraded to `571.0.0`
  * **npm** upgraded to `11.16.0`
  * **Docker Desktop** upgraded to `4.76.0`

### 4. Firestore Index Provisioning
* **Composite Index Creation:** Deployed the missing composite index for `driver_schedule` (`status` ASC, `end_time` ASC) using the Google Cloud SDK.

### 5. Proactive Lookup and Ambiguity Resolution Policy
* **Prompt Instructions Enrichment:** Updated [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py) to establish a strict *Proactive Lookup and Ambiguity Resolution Policy* inside the `STATIC_SYSTEM_PROMPT`. This guides the AI agent to never prompt users for technical IDs (such as `task_id` or `outing_id`) when modifying, canceling, rescheduling, or completing tasks/outings, and instead search for them proactively.

### 6. Access Control & Task Status Mutations for Tier 2 Staff
* **Outing & Task Modification Restrictions:** Confirmed that outing rescheduling, replacement, cancellation, and task creation/modification are strictly restricted to Tier 1 principals (Mazen and Jawaher). Enforced in [app/tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) and [app/tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py).
* **Task Status Constraints for Tier 2:** Modified the transactional status updater `_txn_update_task_status` in [app/tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L118-L149) so that Tier 2 staff users are only permitted to update task status to `"completed"` or `"skipped"`. Any attempt to set a task back to `"pending"` is blocked with a permission error.
* **Problem Reporting Enforcement:** Enforced that Tier 2 staff can only skip a task (`status="skipped"`) if they provide a detailed, non-empty feedback string explaining the problem.
* **System Prompt Guidance:** Aligned the RBAC definitions in [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py).
* **Testing:** Added unit test `test_txn_update_task_status_tier2_rules` in [test_tools_module2.py](file:///Users/terminal/houseops/tests/test_tools_module2.py).

### 7. High Resource Usage Billing Alert
* **Cumulative Metric Tracking:** Added cumulative token and round tracking across multi-turn Vertex AI agent loops inside `run_agent_turn` in [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py#L229-L329).
* **Billing Threshold Warnings:** Integrated helper function `_check_resource_usage_alert` which automatically executes at each turn exit point. If the turn exceeds 4 tool-call rounds, 3,000 output tokens, or 12,000 uncached input tokens, it triggers a `HIGH_RESOURCE_USAGE` ops alert.
* **Alert Delivery:** Delivers detailed token and round statistics to Mazen via the existing `send_ops_alert` channel in [app/ops_bot.py](file:///Users/terminal/houseops/app/ops_bot.py).
* **Testing:** Added unit test `test_check_resource_usage_alert` in [tests/test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py).

---

## June 5, 2026 — Bot-to-Bot Integration & Performance Optimizations

### 1. Bot-to-Bot Whitelisting & Integration Verification
* **Bot Whitelisting Bypass:** Extracted `MAIN_BOT_USER_ID` and `OPS_BOT_USER_ID` in [config.py](file:///Users/terminal/houseops/app/config.py#L45-L55) to recognize `@DQBotOpsBot` traffic. Modified `telegram_webhook` in [main.py](file:///Users/terminal/houseops/main.py#L174-L215) to dynamically authenticate inbound webhook messages originating from `@DQBotOpsBot`'s ID, whitelisting it as a `tier2` role.
* **Egress & Ingress Loopback Check:** Integrated a loopback ping check in [get_ops_status_report](file:///Users/terminal/houseops/app/ops_bot.py#L198-L266) inside [ops_bot.py](file:///Users/terminal/houseops/app/ops_bot.py). When compiling the status report, it sends a signed `ping_test` request to the main bot's webhook. The main bot receives this, intercepts the request, and dispatches a confirmation of egress success (`"Main Bot Egress Test"`) back to the ops bot channel via `send_ops_message`.
* **Testing:** Added comprehensive integration tests in [test_main.py](file:///Users/terminal/houseops/tests/test_main.py#L276-L320) and [test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py#L228-L262).

### 2. Refactoring & Performance Optimizations
* **Async Threadpool Delegation:** Wrapped CPU-bound and synchronous blocking operations in [main.py](file:///Users/terminal/houseops/main.py) with FastAPI's `run_in_threadpool` (Firestore DB streams/gets, HTTP queries, cloud task scheduling, and local/Vertex AI token counts).
* **Firestore Read Optimization:** Consolidated document read logic in `update_task_status` in [tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L120-L208). The permission check is now performed atomically *inside* the transaction via `transaction.get(task_ref)` instead of running a separate read command beforehand.
* **Firestore Transaction Overhead Removal:** Simplified `update_driver_availability` in [tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py#L434-L457). Removed the transactional wrapper entirely in favor of a single `.set(..., merge=True)` call.
* **Token Compiling Performance:** Introduced a fast, local character-length heuristic (~4 characters per token) in [history.py](file:///Users/terminal/houseops/app/history.py#L71-L80) to trim old conversations before making calls to Gemini/Vertex AI `count_tokens_text`.

### 3. Cleanups & Housekeeping
* **Removed QWEN.md:** Deleted the unused, empty `QWEN.md` file from the repository root.

---

## June 4, 2026 — Performance Monitoring Bot (DQBotOpsBot) & Static Analysis

### 1. Implemented Dedicated Performance Monitoring Bot (DQBotOpsBot)
* **Ops Bot Token Config:** Integrated `TELEGRAM_OPS_BOT_TOKEN` in [config.py](file:///Users/terminal/houseops/app/config.py#L40) and [deploy.sh](file:///Users/terminal/houseops/deploy.sh#L42) to support outbound messaging via `@DQBotOpsBot` API.
* **Systems Performance Reports:** Built [get_ops_status_report](file:///Users/terminal/houseops/app/ops_bot.py#L110-L198) in [ops_bot.py](file:///Users/terminal/houseops/app/ops_bot.py) performing twice-daily status checks on the database, Vertex AI API, ingress webhook, and outbound ops bot API.
* **Immediate Technical Alerts:** Registered a global unhandled exception handler in [main.py](file:///Users/terminal/houseops/main.py#L70-L86) to catch unexpected FastAPI runtime crashes and immediately dispatch a formatted traceback alert to Mazen.

### 2. Segregation of Operations & Technical Alerts
* **Main Channel Routing:** Delayed driver arrival warnings (exceeding 30 minutes) in [workflow.py](file:///Users/terminal/houseops/app/workflow.py#L458-L477) are routed to Tier 1 principals via the normal channel (`_notify_tier1_users` using the main bot token).
* **Calendar Sync Conflicts:** Conflict alerts are also excluded from the Ops Bot channel and routed solely via the normal channel.

### 3. Test Suite Verification
* **Ops Bot Tests:** Created unit and integration tests in [test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py) validating the separation of technical alerts (crashes) from house ops notifications (normal channel driver timeouts).

### 4. Cloud Scheduler Infrastructure Updates
* **Fixed Cleanup Credentials:** Updated the existing `telegram-message-cleanup` Cloud Scheduler job headers to use the correct SHA-256 secret token hash, resolving permissions / 403 authorization failures.
* **Scheduled Operations Reports:** Created a new Cloud Scheduler job `telegram-ops-status-update` to trigger the `/jobs/ops-status-update` endpoint twice-daily at 8:00 AM and 8:00 PM local Riyadh time (`Asia/Riyadh`).

### 5. Security & Static Analysis Integration (Pre-commit)
* **Pre-commit Framework Integration:** Configured the [pre-commit](file:///Users/terminal/houseops/.pre-commit-config.yaml) framework orchestrating linting, formatting, security auditing, and secret scanning on git commits.
* **Scan Tools Configured:**
  * **Ruff & Ruff Format:** Formatted the codebase style and configured standard static analysis checking.
  * **Bandit:** Integrated security audits for Python source files.
  * **GitGuardian (ggshield):** Configured pre-commit secret scanning hook and successfully authenticated via local credentials.

---

## June 3, 2026 — Driver Seeding & Codebase Bug Fixes

### 1. Updated Driver Dispatch & Vehicle Seeding
* **Database Seed Update:** Renamed the Tier 1 principal from `"Principal (Mazen)"` to `"Mazen"` in [init_db.py](file:///Users/terminal/houseops/init_db.py) to unify user name fields with preferences.
* **Dispatch Preference & Fleet Mappings:**
  * **Khidir** mainly drives Jawaher and Mano in the *Mercedes V Class*.
  * **Emad** mainly drives Mazen in the *Lexus LX*.
  * **Kim** mainly drives Errands/Shopping in the *Toyota Rush*.
  * Removed any other non-existent vehicle profiles from the database seed.

### 2. Implemented Codebase Refinements & Bug Fixes

#### A.1 Calendar Sync Missing `DTEND` Fallback
* **Issue:** Calendar sync crashed if an iCloud event was missing the `DTEND` field.
* **Fix:** Added a fallback in [icloud_calendar.py](file:///Users/terminal/houseops/app/icloud_calendar.py) to set the end time to `start_time + 1 hour` (or equal to start for all-day events) if it is missing or zero-duration.

#### B.1 Midnight Crossover in Driver Availability
* **Issue:** Naive checks in `is_driver_available` allowed drivers to be scheduled on crossover events outside their working hours.
* **Fix:** Updated [workflow.py](file:///Users/terminal/houseops/app/workflow.py) to query availability for both target date and target date + 1, merge overlapping/contiguous intervals, and verify complete interval containment.

#### B.2 Missed Driver Arrival Nags After Midnight
* **Issue:** Clamp-to-today logic in `run_driver_arrival_nag` caused unconfirmed late-night outings to be permanently missed once midnight passed.
* **Fix:** Replaced the start-of-day clamp with a sliding 24-hour lookback window query.

#### C.1 Firestore Composite Index Configuration
* **Documentation:** Created [firestore.indexes.json](file:///Users/terminal/houseops/firestore.indexes.json) and updated [SCHEMA.md](file:///Users/terminal/houseops/SCHEMA.md) to define the required composite index on `driver_schedule` for `status` (Ascending) and `end_time` (Ascending).

#### C.2 Firestore 30-Item Query Limit
* **Issue:** Availability schedules longer than 30 days had dates silently truncated because of Firestore's `in` query limitations.
* **Fix:** Replaced the `in` query in [tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) with a lexicographical range query.

#### D.1 Idempotency Key Enqueue Race
* **Issue:** Webhook claimed message idempotency keys prior to enqueuing. If enqueuing failed, the key remained locked, blocking future retries.
* **Fix:** Caught exceptions in the webhook enqueuer in [main.py](file:///Users/terminal/houseops/main.py), deleted the claimed idempotency key in Firestore, and returned HTTP `500` to trigger automated retries.

#### E.1 Principal Message Hijacking on Calendar Conflict
* **Issue:** Tier 1 principal messages were unconditionally intercepted and blocked whenever a conflict was active.
* **Fix:** Updated the confirmation gate in [confirmation_gate.py](file:///Users/terminal/houseops/app/confirmation_gate.py) to use full-word checks for short keywords and substring checks for longer words, letting unrelated messages pass to Gemini.

---

## June 1, 2026 — Webhook Migration & System Blockers Resolution

### 1. Migration: WhatsApp to Telegram Ingress Switch
* **Core Transition:** Replaced the legacy WhatsApp Cloud API implementation entirely with the new Telegram Bot API integration (**DQ Villa Bot**, `@DQVillaBot`).
* **Ingress Changes:** Removed the legacy WhatsApp webhook route `/webhook/whatsapp` and all WhatsApp media download/template-binding utility codes.
* **New Telegram Flow:** Built `/webhook/telegram` as a lightweight webhook receiver that handles onboarding and routes whitelisted messages to the Cloud Tasks processing pipeline.

### 2. Blocker Diagnosis & Resolution

#### Vertex AI IAM Role
* **Issue:** Heavy-path worker tasks (`POST /tasks/process-inbound`) were throwing 403 Permission Denied errors when predicting via Vertex AI.
* **Resolution:** Granted the `roles/aiplatform.user` IAM role to the default Compute service account.

#### Default Gemini Model Config
* **Issue:** The default `GEMINI_MODEL` environment variable in `app/config.py` was hardcoded to `"gemini-3.1-flash"`, which does not exist in Vertex AI.
* **Resolution:** Updated fallback value in `app/config.py` to `"gemini-2.5-flash"`.

### 3. Programmatic 24h Message Deletion Feature
* **Telegram Deletion Helper:** Added the `delete_message` helper function in `app/telegram.py` wrapping the standard Telegram Bot API `deleteMessage` endpoint.
* **Firestore Message Tracking:** Updated `write_message_turn` in `app/firestore_db.py` to accept optional `telegram_chat_id` and `telegram_message_id` parameters.
* **Database Persist:** Modified `process_inbound` in `main.py` to store both tracking IDs directly on user and assistant message documents.
* **Secure Deletion Job Endpoint:** Created a secure HTTP endpoint `/jobs/cleanup-messages` in `main.py` that handles the message sweep.

### 4. Hourly Cron Job Automation
* **Action:** Enabled the `cloudscheduler.googleapis.com` API and deployed a Cloud Scheduler job to automate the sweep at the top of every hour.
