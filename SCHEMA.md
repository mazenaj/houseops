# Blueprint: Household Operations Engine

## 1. Environment Constraints

* **Runtime:** Python 3.11 / FastAPI (stateless, deployed to Google Cloud Run).
* **Database:** Google Cloud Firestore (NoSQL).
* **Object Storage:** Google Cloud Storage (receipts, product manuals, guest attachments).
* **Core Engine:** Vertex AI SDK (Gemini 2.5 Flash with implicit context caching).
* **RAG:** Vertex AI Search over GCS-stored PDFs (product manuals, recipes).
* **Secrets:** Google Secret Manager (Telegram tokens, API keys).
* **Scheduling:** Google Cloud Scheduler (cron triggers for reminders and digests).
* **Async Delivery:** Google Cloud Tasks (enqueues inbound processing; avoids blocking webhooks).
* **Monitoring:** Cloud Monitoring + Error Reporting + uptime check on `/health`.
* **Boundary:** No UI dashboards, no ORMs, no self-managed background workers (Celery/RQ). Cloud Scheduler + Cloud Tasks replace task queues.
* **Channel:** Telegram only (via `@DQBotMain` for user interaction and `@DQBotOpsBot` for system operations/alerts).
* **Timezone:** `Asia/Riyadh`.

### Cloud Run Configuration

* **Min instances:** 1 (avoid cold-start on inbound webhooks).
* **Webhook handler:** Return `200` immediately; enqueue heavy work to Cloud Tasks.
* **Idempotency:** Dedupe by Telegram `message_id` (prefixed with `tg_msg_` or `tg_cb_`) via atomic Firestore `create()` with **24-hour TTL** and stale-key overwrite fallback (see §1 Idempotency Store).
* **Media ingest:** Cloud Tasks worker downloads binary from Telegram Bot API and uploads to GCS **before** invoking Gemini (see §9.1).
* **Prompt assembly:** Static prefix first, volatile suffix last — required for implicit context caching; prefix must meet 4,096-token floor; suffix history capped at 3,000 tokens (see §7, §9.2).
* **Confirmation interrupts:** Unrelated inbound messages preempt `pending_confirmation` rather than trapping the session (see §9.3).

### Idempotency Store (24-Hour Rolling Window)

**Problem:** Storing every `message_id` indefinitely in an `idempotency_keys` collection grows without bound and adds unnecessary Firestore read cost on duplicate checks.

**Collection:** `webhook_idempotency/{message_id}`

| Field | Type | Notes |
|-------|------|-------|
| `message_id` | string | Document ID — `tg_msg_{message_id}` or `tg_cb_{callback_id}` |
| `received_at` | timestamp | Webhook receipt time |
| `expires_at` | timestamp | `received_at + 24h` — **Firestore TTL policy field** |

**Rules:**

1. Webhook dedupe uses `create()` (not blind `set()`) on `webhook_idempotency/{message_id}`. On success → proceed. On existence conflict → run **TTL stale fallback** (rule 2). True duplicates within the live window → return `200`, do not enqueue.
2. **TTL stale fallback (mandatory):** Firestore TTL garbage collection can lag **minutes to 48 hours** after `expires_at`. A physically stale document still blocks `create()`. On `AlreadyExists`:
   * `get(webhook_idempotency/{message_id})`
   * If `now > expires_at` → **`set()` overwrite** with fresh `{received_at, expires_at}` → proceed (treat as new delivery).
   * If `now ≤ expires_at` → return `200` immediately (genuine duplicate within window).
3. Enable a **Firestore TTL policy** on `expires_at` so records are eventually purged. TTL is for storage reclamation only — **never rely on physical deletion for dedupe correctness**.
4. Do **not** query or scan the collection for dedupe — rely on document-ID `create()` plus the stale fallback read on conflict only.
5. Do not archive idempotency keys to cold storage; they are ephemeral coordination data, not business records.

**Dedupe pseudocode:**

```python
def claim_idempotency_key(message_id: str, now: datetime) -> bool:
    """Returns True if this webhook should be processed."""
    ref = db.collection("webhook_idempotency").document(message_id)
    payload = {"message_id": message_id, "received_at": now, "expires_at": now + timedelta(hours=24)}
    try:
        ref.create(payload)
        return True
    except AlreadyExists:
        doc = ref.get()
        if doc.exists and now > doc.get("expires_at"):
            ref.set(payload)  # stale TTL record — reclaim and process
            return True
        return False  # live duplicate — skip
```

---

## 2. Role-Based Access Control (RBAC)

### Tiers

* **TIER 1 (PRINCIPAL):** Full read/write across all collections. Exclusive access to procurement approval, shopping digests, driver outing creation, cancellation, rescheduling, replacement, calendar registrations, and financial receipt attachments. Household members: Mazen (`mem_principal_001`) and Jawaher (`mem_principal_002`).
* **TIER 2 (STAFF):** Conditional read/write restricted to checking daily schedules (today's schedule only), updating own availability, completing tasks, logging consumption, reporting maintenance issues (incident logging), and recording pet logs. No access to receipts, weekly menu planning, or outings creation/modification. Staff are explicitly restricted from updating task statuses back to `"pending"` (they can only set `"completed"` or `"skipped"`).
* **CHILD:** Allowed to view details but restricted from modifying outings or system configuration.

### Access Enforcement

* Unknown Telegram accounts receive no access until they share a contact number whitelisted in the `members` collection.
* RBAC enforced at the **tool layer**: tools not permitted for a tier are not passed to the model (e.g. `manage_outing` creation is hidden from Tier 2).
* Multi-step writes (scheduling, shopping approval, tradesman escalation) require explicit confirmation before persisting.

### Multilingual Staff (Tier 2)

Household staff communicate in **Arabic, Urdu, Tagalog, and English** (voice notes and text). The agent replies in the sender's preferred language, but **tool arguments passed to Python must obey the Translation Boundary** (see §7).

### Collection: `members`

| Field | Type | Notes |
|-------|------|-------|
| `member_id` | string | Stable ID |
| `phone_e164` | string | Normalized E.164 number used for whitelist lookup during onboarding |
| `name` | string | Display name |
| `role` | enum | `"tier1"` \| `"tier2"` \| `"child"` |
| `capabilities` | array | e.g. `["driver"]`, `["chef"]`, `["nanny"]`, `["housemaid"]` |
| `active` | boolean | Soft-disable without deleting history |
| `telegram_chat_id` | number \| null | Telegram Chat ID linked during the contact sharing flow |
| `preferred_language` | string | `"ar"` \| `"en"` \| `"ur"` \| `"tl"` \| `"mixed"` — controls outbound reply language only |
| `created_at` | timestamp | |
| `updated_at` | timestamp | |

---

## 3. Telegram Integration

### Inbound Flow

**Webhook (`POST /webhook/telegram`) — fast path only:**

1. Telegram Bot API POSTs webhook payload (callback query or message).
2. Verify request signature header `X-Telegram-Bot-Api-Secret-Token` (validated against SHA256 of `TELEGRAM_BOT_TOKEN`).
3. Resolve Chat ID and format the `message_id`:
   * Callback query: `tg_cb_{callback_query.id}`
   * Message: `tg_msg_{message.message_id}`
4. Dedupe by `message_id` using `claim_idempotency_key()` with TTL stale fallback (see §1 Idempotency Store). Return `200` without enqueue if claim returns `False`.
5. Check if contact share payload is present. If user shared their contact:
   * Extract phone number, normalize to E.164.
   * Look up member in `members` collection by `phone_e164`. If found, link `telegram_chat_id` and send welcome message.
   * Return `200 OK` and skip enqueue.
6. Look up sender in `members` collection by `telegram_chat_id`.
7. If member not found or inactive:
   * Request contact share via `request_contact_share()` (sends a keyboard button to share phone number).
   * Return `200 OK` and skip enqueue.
8. Normalize payload to a uniform `InboundMessage` envelope (see below).
9. Enqueue to Cloud Tasks queue `inbound-message-processing`.
10. Return `200 OK` immediately.

### `InboundMessage` Envelope (Uniform Content Blocks)

Every inbound message — text-only, media-only, or mixed — uses the **identical top-level schema**. Downstream code never branches on message "type"; it iterates `content[]` uniformly.

```json
{
  "message_id": "tg_msg_123456",
  "phone_e164": "+9665xxxxxxxxx",
  "member_id": "mem_principal_001",
  "received_at": "2026-06-07T18:30:00+03:00",
  "content": [
    {"block_type": "text", "text": "Flat tire on Lexus"},
    {"block_type": "media", "media_id": "AgADBAAD...", "mime_type": "image/jpeg", "gcs_uri": null}
  ]
}
```

**Content block types:**

| `block_type` | Fields | Set by |
|--------------|--------|--------|
| `text` | `text` (string) | Webhook (from message text or caption) |
| `media` | `media_id`, `mime_type`, `gcs_uri` (null until worker resolves), `normalized_mime_type` (null until worker resolves) | Webhook sets `media_id` + raw `mime_type`; worker sets `gcs_uri` + `normalized_mime_type` (see §9.1) |

**Normalization rules (webhook):**

* Text-only message → `content: [{"block_type": "text", "text": "..."}]`
* Media without caption → `content: [{"block_type": "media", "media_id": "...", "mime_type": "...", "gcs_uri": null}]`
* Media with caption → text block first, then media block (preserve Telegram order).
* Never use top-level keys like `body`, `type: "text"`, or bare `media_id` outside `content[]`.

**Gemini mapping (worker — no conditionals on message shape):**

```python
for block in inbound.content:
    if block.block_type == "text":
        parts.append(Part(text=block.text))
    elif block.block_type == "media":
        parts.append(Part(file_data=FileData(
            file_uri=block.gcs_uri,
            mime_type=block.normalized_mime_type,  # never raw Telegram mime_type
        )))
```

**Cloud Tasks worker (`POST /tasks/process-inbound`) — heavy path:**

1. Iterate `content[]`; for each `block_type: "media"` where `gcs_uri` is null:
   * Resolve authenticated Telegram media download URL via `getFile` API.
   * Stream binary download from Telegram CDN.
   * Upload to GCS at `gs://{bucket}/inbound/{phone_e164}/{message_id}/{index}.{ext}`.
   * Set `gcs_uri` and `normalized_mime_type` on the block in-place.
2. Load session state from `conversations/{phone_e164}`; load history via subcollection query (see below).
3. Run confirmation-interrupt pre-check (see §9.3).
4. Assemble Gemini request with cache-safe ordering (see §7, §9.2).
5. Map `content[]` blocks to Gemini `Part` list — **never pass Telegram Bot API URLs to Vertex AI**.
6. Execute tool loop; persist state.
7. Write turn atomically: `create()` on `conversations/{phone_e164}/messages/{message_id}` (user) and `messages/{reply_id}` (assistant).
8. Send reply via Telegram Bot API `sendMessage` (or retry).

### Outbound Flow

* Outbound messages are sent directly to the linked `telegram_chat_id` using the Telegram Bot API (`sendMessage`).
* Unlike WhatsApp, Telegram does not restrict outbound sends to a 24-hour window, and does not require approved templates. Plain text layout is used for all outbound reminders, status reports, and notifications.

---

## 4. Google Cloud Storage Layout

| Path | Contents | Access |
|------|----------|--------|
| `gs://{bucket}/inbound/{phone_e164}/{message_id}/` | Raw Telegram media (images, voice notes, documents) ingested by Cloud Tasks worker | Agent read; ephemeral TTL optional |
| `gs://{bucket}/receipts/{year}/{incident_id\|queue_id}/` | Receipt photos (maintenance, grocery) | Tier 1 only |
| `gs://{bucket}/manuals/{category}/{item_name}.pdf` | Product manuals for RAG | Agent read |
| `gs://{bucket}/recipes/{recipe_id}/` | Recipe attachments | Agent read |
| `gs://{bucket}/guests/{guest_id}/` | Guest-related attachments | Tier 1 read/write |

**Media ingest rule:** Telegram media → Cloud Tasks worker downloads from Telegram Bot API → streams to GCS → Gemini reads `gs://` URI. The webhook and Gemini client never touch Telegram media URLs directly.

Receipt and incident photos follow the same ingest path; on resolution, move or reference from `inbound/` to `receipts/`.

---

## 5. Cloud Scheduler Jobs

| Job | Cron (Asia/Riyadh) | Endpoint | Recipients |
|-----|-------------------|----------|------------|
| `daily_task_reminders` | `0 6 * * *` | `/jobs/daily-tasks` | Each Tier 2 member (their tasks) |
| `end_of_shift_feedback` | `0 20 * * *` | `/jobs/task-feedback` | Each Tier 2 member |
| `driver_needs_prompt` | `0 7 * * *` | `/jobs/driver-prompt` | Tier 1 |
| `weekly_grocery_digest` | `0 8 * * 0` | `/jobs/weekly-grocery` | Tier 1 |
| `tier1_nightly_summary` | `0 21 * * *` | `/jobs/principal-digest` | Tier 1 (incomplete tasks, open incidents) |
| `pet_schedule_reminders` | `0 9 * * 1` | `/jobs/pet-reminders` | Tier 1 |
| `monthly_improvement_prompt` | `0 10 1 * *` | `/jobs/improvement-prompt` | All active members |
| `nightly_calendar_sync` | `0 20 * * *` | `/jobs/nightly-calendar-sync` | Tier 1 and Drivers (scheduled outing confirmations) |
| `calendar_onboarding_nag` | `0 10 * * *` | `/jobs/calendar-onboarding-nag` | Tier 1 (missing iCloud URLs) |

---

## 6. Persistent Firestore Schema & Tool Mapping

### Shared Operational State — Transactional Writes

**Problem:** Conversation history uses atomic subcollection `create()` to avoid contention, but shared operational documents (`staff_tasks`, `driver_schedule`, etc.) are discrete records updated by multiple staff concurrently. Plain asynchronous `update()` calls can lose writes when two workers read stale state and overwrite each other.

**Mandatory rule:** All status mutations on shared operational state documents **must** execute inside a Firestore **`transaction`** block (`firestore.transaction()` / `@firestore.transactional`):

1. `transaction.get(doc_ref)` — read current document state.
2. Validate preconditions (e.g. status is still `"pending"`, assigned driver slot is free).
3. `transaction.update(doc_ref, fields)` — apply mutation atomically.
4. On precondition failure → abort transaction; return structured error to agent (do not silently drop).

**Collections requiring transactional writes:**

| Collection | Mutating tools |
|------------|----------------|
| `staff_tasks` | `update_task_status`, `create_adhoc_task`, `create_weather_tasks` |
| `driver_schedule` | `manage_outing` |
| `driver_availability` | `update_driver_availability` |
| `incident_ledger` | `update_incident_troubleshooting`, `resolve_incident` |
| `inventory_state` | `log_consumption` (stock decrement) |

**Exempt (atomic create-only or single-writer):** `conversations/.../messages/`, `webhook_idempotency/`, append-only logs (`consumption_log`, `pet_logs`).

---

### MODULE 0: IDENTITY & SESSION (`core`)

* **Collection:** `members` — see §2.
* **Collection:** `conversations` — see §3 (parent session doc + `messages/` subcollection).
* **Collection:** `webhook_idempotency` — see §1 Idempotency Store.

---

### MODULE 1: FLEET & LOGISTICS (`fleet_operations`)

* **Collection:** `drivers`

  | Field | Type | Notes |
  |-------|------|-------|
  | `driver_id` | string | |
  | `member_id` | string | FK to `members` |
  | `name` | string | |
  | `roles` | array | `["driver"]` |
  | `default_vehicle` | string | Optional |
  | `active` | boolean | |

* **Collection:** `driver_availability`

  | Field | Type | Notes |
  |-------|------|-------|
  | `availability_id` | string | |
  | `driver_id` | string | |
  | `date` | string | ISO date |
  | `slots` | array | `{start_time, end_time, status: "available"\|"busy"\|"off"}` |
  | `notes` | string | Optional |
  | `updated_by` | string | `member_id` |

* **Collection:** `driver_schedule`

  | Field | Type | Notes |
  |-------|------|-------|
  | `outing_id` | string | |
  | `start_time` | timestamp | |
  | `end_time` | timestamp | |
  | `destination` | string | Optional |
  | `purpose` | string | Optional |
  | `assigned_driver` | string | `driver_id` |
  | `requested_by` | string | `member_id` |
  | `status` | enum | `"scheduled"` \| `"in_progress"` \| `"completed"` \| `"cancelled"` |
  | `passengers` | array | Optional names |
  | `notes` | string | |

* **Collection:** `incident_ledger`

  | Field | Type | Notes |
  |-------|------|-------|
  | `incident_id` | string | |
  | `category` | enum | `"car"` \| `"property"` |
  | `item_name` | string | Vehicle or asset name |
  | `description` | string | |
  | `reported_by` | string | `member_id` |
  | `troubleshooting_status` | enum | `"open"` \| `"tried_manual"` \| `"escalated_to_tradesman"` |
  | `troubleshooting_notes` | array | Steps attempted |
  | `resolution_status` | enum | `"open"` \| `"resolved"` |
  | `tradesman_contact` | string | Optional |
  | `receipt_url` | string | GCS path |
  | `created_at` | timestamp | |
  | `resolved_at` | timestamp | Optional |

* **Vertex AI Tools (Tier 1 + Tier 2 where noted):**
  * `get_schedule(date_range: string)` — today/tomorrow view (Tier 1 + Tier 2)
  * `manage_outing(...)` — Tier 1 only (create, update, cancel drivers/outings); **transactional write**
  * `update_driver_availability(...)` — Tier 2 (drivers only); **transactional write**
  * `log_maintenance_incident(...)` — Tier 2
  * `update_incident_troubleshooting(...)` — Tier 1 + Tier 2; **transactional write**
  * `escalate_to_tradesman(...)` — Tier 1 only
  * `resolve_incident(...)` — Tier 1 only; **transactional write**
  * `get_calendar_events(date_range: string)` — Tier 1 only (fetch public iCloud calendars)
  * `register_calendar_url(member_id: string, url: string)` — Tier 1 only (register shared iCloud calendar)
  * `get_pooling_suggestions(date: string)` — Tier 1 only (fetch ride pooling opportunities for a specific date)

---

### MODULE 2: PROPERTY & DUTIES (`property_management`)

* **Collection:** `task_templates`

  | Field | Type | Notes |
  |-------|------|-------|
  | `template_id` | string | |
  | `task_description` | string | |
  | `assigned_capability` | string | e.g. `"housemaid"`, `"groundskeeper"` |
  | `frequency` | enum | `"daily"` \| `"weekly"` |
  | `day_of_week` | number | 0–6 for weekly tasks; null for daily |
  | `active` | boolean | |

* **Collection:** `staff_tasks`

  | Field | Type | Notes |
  |-------|------|-------|
  | `task_id` | string | |
  | `template_id` | string | Optional FK |
  | `assigned_to` | string | `member_id` |
  | `task_description` | string | |
  | `due_date` | string | ISO date |
  | `frequency` | enum | `"daily"` \| `"weekly"` \| `"adhoc"` |
  | `status` | enum | `"pending"` \| `"completed"` \| `"skipped"` |
  | `feedback` | string | Post-completion notes — **free-form; any language preserved** |
  | `completed_at` | timestamp | Optional |

* **Vertex AI Tools:**
  * `list_tasks(member_id: string, date: string)` — Tier 1 (any member); Tier 2 (self only)
  * `update_task_status(task_id: string, status: string, feedback: string)` — Tier 2 (only `"completed"` or `"skipped"` allowed, `"skipped"` requires a non-empty feedback string reporting the problem); **transactional write**
  * `create_adhoc_task(assigned_to: string, task_description: string, due_date: string)` — Tier 1 only; **transactional write**
  * `create_weather_tasks(tasks: array)` — Tier 1 only; **batch write** weather-dependent tasks

---

### MODULE 3: PROCUREMENT & CULINARY (`household_inventory`)

* **Collection:** `inventory_state`

  | Field | Type | Notes |
  |-------|------|-------|
  | `item_id` | string | |
  | `category` | enum | `"grocery"` \| `"supply"` |
  | `item_name` | string | |
  | `current_stock` | number | |
  | `unit` | string | e.g. kg, pcs, L |
  | `par_level` | number | Reorder threshold |
  | `updated_at` | timestamp | |

* **Collection:** `consumption_log`

  | Field | Type | Notes |
  |-------|------|-------|
  | `log_id` | string | |
  | `item_id` | string | |
  | `quantity_used` | number | |
  | `logged_by` | string | `member_id` |
  | `timestamp` | timestamp | |

* **Collection:** `shopping_queue`

  | Field | Type | Notes |
  |-------|------|-------|
  | `queue_id` | string | |
  | `item_name` | string | |
  | `quantity` | string | |
  | `category` | enum | `"grocery"` \| `"supply"` |
  | `suggested_amazon_url` | string | Agent-suggested; human-approved |
  | `requested_by` | string | `member_id` |
  | `status` | enum | `"pending_digest"` \| `"approved"` \| `"ordered"` \| `"received"` |
  | `receipt_url` | string | GCS path; Tier 1 only |
  | `created_at` | timestamp | |

* **Collection:** `recipes`

  | Field | Type | Notes |
  |-------|------|-------|
  | `recipe_id` | string | |
  | `name` | string | |
  | `ingredients` | array | `{item_name, quantity, unit}` |
  | `instructions` | string | |
  | `servings` | number | |
  | `tags` | array | e.g. `"kid-friendly"`, `"guest"` |
  | `attachment_url` | string | Optional GCS path |

* **Collection:** `weekly_menu`

  | Field | Type | Notes |
  |-------|------|-------|
  | `menu_id` | string | |
  | `week_start` | string | ISO date (Sunday) |
  | `meals` | array | `{date, meal_type: "breakfast"\|"lunch"\|"dinner", recipe_id, notes}` |
  | `created_by` | string | `member_id` |
  | `status` | enum | `"draft"` \| `"approved"` |

* **Collection:** `guest_registry`

  | Field | Type | Notes |
  |-------|------|-------|
  | `guest_id` | string | |
  | `name` | string | |
  | `preferences_summary` | string | Dietary, room, transport, etc. |
  | `visit_start` | string | ISO date |
  | `visit_end` | string | ISO date |
  | `notes` | string | |
  | `prep_task_ids` | array | FK to `staff_tasks` or adhoc checklist |
  | `history` | array | Past visit summaries |

* **Collection:** `guest_prep_checklists`

  | Field | Type | Notes |
  |-------|------|-------|
  | `checklist_id` | string | |
  | `guest_id` | string | |
  | `items` | array | `{task_description, assigned_capability, status}` |
  | `visit_start` | string | |

* **Vertex AI Tools:**
  * `log_consumption(item_name: string, quantity_used: number)` — Tier 2
  * `get_inventory(category: string)` — Tier 1; Tier 2 read-only for groceries
  * `add_to_shopping_queue(item_name: string, quantity: string, amazon_url: string)` — Tier 2 request; Tier 1 approve
  * `approve_shopping_items(queue_ids: array)` — Tier 1 only
  * `suggest_amazon_item(item_name: string)` — Agent-assisted; returns URL for approval
  * `get_weekly_menu(week_start: string)` — Tier 1 + chef capability
  * `plan_weekly_menu(week_start: string, meals: array)` — Tier 1 + chef capability
  * `register_guest(name: string, preferences: string, visit_start: string, visit_end: string)` — Tier 1 only
  * `generate_guest_prep(guest_id: string)` — Tier 1 only
  * `update_guest_preferences(guest_id: string, preferences: string)` — Tier 1 only

---

### MODULE 4: ENTITY TELEMETRY (`entity_tracking`)

* **Collection:** `pets`

  | Field | Type | Notes |
  |-------|------|-------|
  | `pet_id` | string | |
  | `name` | string | |
  | `species` | string | |
  | `active` | boolean | |

* **Collection:** `pet_schedules`

  | Field | Type | Notes |
  |-------|------|-------|
  | `schedule_id` | string | |
  | `pet_id` | string | |
  | `activity_type` | enum | `"walk"` \| `"feed"` \| `"groom"` \| `"vet"` \| `"vaccine"` |
  | `cadence` | string | e.g. `"daily"`, `"weekly"`, `"annual"` |
  | `next_due` | string | ISO date |
  | `notes` | string | |

* **Collection:** `pet_logs`

  | Field | Type | Notes |
  |-------|------|-------|
  | `log_id` | string | |
  | `pet_id` | string | |
  | `activity_type` | enum | `"walk"` \| `"eat"` \| `"groom"` \| `"vet"` \| `"vaccine"` |
  | `logged_by` | string | `member_id` |
  | `timestamp` | timestamp | |
  | `notes` | string | |

* **Collection:** `system_meta_feedback`

  | Field | Type | Notes |
  |-------|------|-------|
  | `feedback_id` | string | |
  | `submitted_by` | string | `member_id` |
  | `timestamp` | timestamp | |
  | `user_input` | string | |
  | `proposed_improvement` | string | |
  | `status` | enum | `"new"` \| `"reviewed"` \| `"implemented"` \| `"declined"` |
  | `reviewed_by` | string | Tier 1 `member_id`; optional |

* **Collection:** `system`

  This collection stores system-wide metadata, daily schedule statuses, and runtime configuration settings.

  * **Document:** `system/config` (Runtime Settings)

    | Field | Type | Notes |
    |-------|------|-------|
    | `pooling_time_window_minutes` | number | Time proximity threshold for ride pooling suggestion engine (default: 30) |

  * **Document:** `system/schedule_{date}` (Daily Sync Logs)

    | Field | Type | Notes |
    |-------|------|-------|
    | `status` | enum | `"clear"` \| `"conflict"` |
    | `date` | string | ISO date string (YYYY-MM-DD) |
    | `conflicts` | array | Diagnostic conflict description lines if status is `"conflict"` |
    | `pooling_suggestions` | array | Ride pooling suggestion lines for outings close in time |
    | `updated_at` | timestamp | Last sync run time |

* **Vertex AI Tools:**
  * `log_pet_event(pet_id: string, activity_type: string, notes: string)` — Tier 1 + Tier 2
  * `get_pet_schedule(pet_id: string)` — Tier 1 only
  * `capture_system_feedback(proposed_improvement: string)` — All members
  * `list_improvement_backlog()` — Tier 1 only

---

## 7. Agent Design

### Prompt Assembly (Cache-Safe Ordering)

Google Implicit Context Caching discounts repeated **prefix** tokens. Volatile data must never appear before static definitions.

**Request payload order (strict):**

```
┌─ PREFIX — identical across requests (cached) ─────────────────────────┐
│ 1. Static system prompt (identity, tone, safety rules)               │
│ 2. Household operational rules (confirmation policy, timezone name)  │
│ 3. Module definitions and collection schemas (from this document)    │
│ 4. Full tool declarations (all tools; RBAC filters at runtime)     │
│ 5. Static RBAC tier descriptions                                     │
└──────────────────────────────────────────────────────────────────────┘
┌─ SUFFIX — unique per request (not cached) ───────────────────────────┐
│ 6. Session context block:                                            │
│    - Current date/time (Asia/Riyadh)                                 │
│    - Speaker profile: name, role, capabilities, member_id            │
│    - active_module, pending_confirmation summary (if any)            │
│    - Open records snapshot (today's schedule, open incidents)        │
│ 7. Conversation history (≤20 docs, trimmed to ≤3,000 tokens — see §7) │
│ 8. Current user message (content_blocks[] from InboundMessage)       │
└──────────────────────────────────────────────────────────────────────┘
```

**Implementation rules:**

* Never inject `datetime`, speaker identity, or open records into the system prompt string at the top.
* Use a labeled `--- SESSION CONTEXT ---` delimiter before volatile blocks so assembly code enforces ordering.
* Tool schemas in the prefix must remain stable; filter which tools are *executable* in application code, not by mutating the cached schema text per user.
* Conversation history and current message use the same `content_blocks[]` schema as `InboundMessage`; Gemini parts are built by iterating blocks uniformly.
* Load history from `conversations/{phone}/messages/` subcollection — never from a parent-doc array.

### Suffix Token Budget Ceiling

**Problem:** The suffix includes up to 20 conversation turns. During peak periods — long multi-turn exchanges, verbose feedback, media metadata — the history slice alone can exceed 10,000 tokens. This does not break prefix caching but causes severe latency for the user.

**Hard limit:** `MAX_SUFFIX_HISTORY_TOKENS = 3000` — conversation history in the suffix must never exceed this allocation.

**Compilation algorithm (`compile_conversation_history()`):**

1. Query `messages/` subcollection: `orderBy("timestamp", DESC).limit(20)`.
2. Reverse to chronological order (oldest → newest).
3. Serialize turns to text; run `count_tokens(history_text)`.
4. **While** `token_count > 3000` **and** turns remain → drop the **oldest** turn; re-count.
5. If a single turn exceeds 3,000 tokens alone → truncate that turn's text blocks to 800 tokens with `"…[truncated]"` suffix before dropping other turns.
6. Log `{turns_loaded, turns_dropped, final_token_count}` on every request; alert if `turns_dropped > 5` consistently.

**Budget allocation (suffix zone total guidance):**

| Suffix component | Token budget |
|------------------|--------------|
| Session context block | ~500 |
| Conversation history | **≤ 3,000** (enforced) |
| Current user message + media | ~1,500 (soft cap; truncate text if needed) |

Current user message is **never** dropped to satisfy history budget — shrink history first.

### Translation Boundary (Multilingual Input → English Tool Args)

**Problem:** Tier 2 staff send voice notes and text in Arabic, Urdu, Tagalog, or English. If Gemini passes translated task descriptions or enum values in those languages to Python tool functions, ID lookups and string matching against English database records will fail silently.

**Rule:** The LLM interprets in any language but **writes** structural tool arguments in English. Free-form human content stays in the original language.

| Argument class | Language | Examples |
|----------------|----------|----------|
| **Structural** (English only) | Canonical English | `task_id`, `status: "completed"`, `category: "car"`, `activity_type: "walk"`, `action: "create"`, `item_name` matching `inventory_state` keys, ISO dates/times |
| **Free-form** (preserve original) | User's spoken/written language | `feedback`, `notes`, `description`, `user_input`, `proposed_improvement`, `troubleshooting_notes` |

**Implementation:**

1. Prefix includes explicit instruction: *"All tool call arguments for IDs, enums, status values, and catalog item names MUST be English. Preserve the user's original language only in free-form text fields."*
2. Python tool handlers validate structural enums against allowed literals; reject non-English enum values with a retry prompt to the model.
3. Task matching uses **`task_id`** (returned by `list_tasks`), never fuzzy match on `task_description` strings.
4. Inventory consumption resolves items by **`item_id`** or canonical English `item_name` from `inventory_state` — not by translated colloquial names.
5. Store detected inbound language on the user message doc (`source_language`) for analytics; outbound replies use `members.preferred_language`.

---

### Implicit Cache Activation Floor (Gemini 2.5 Flash)

Google's Implicit Context Caching requires the static prefix to reach a **minimum of 4,096 tokens**. If the prefix falls below this floor, caching is **completely disabled** for that request and the full 100% prefill rate applies.

**Mandatory prefix sizing:**

1. At deploy time and on every prefix change, run `count_tokens(prefix)` via the Vertex AI tokenizer.
2. If `token_count(prefix) < 4096`, **pad the prefix** with static, inert content until the floor is met:
   * Extended module documentation (full SCHEMA excerpts).
   * Static few-shot examples (sample tool call/response pairs).
   * Detailed operational playbooks (confirmation flows, escalation paths).
   * A marked `CACHE_PADDING_BLOCK` section — content that never changes between requests.
3. Padding must remain in the **prefix zone only** — never append padding after volatile suffix data.

---

### Billing & Resource Alerts

To avoid unexpected API billing surprises, two protection thresholds are implemented inside `run_agent_turn`:

1. **Cumulative Warning/Pause (250k Token Limit):** If the cumulative token count (prompt + candidate) reaches **250,000 tokens** within an operation, the loop is paused. The execution state (including history and current round index) is saved to Firestore under a pending confirmation with the action `resume_paused_agent_turn`. The user must reply with a confirmation keyword ("yes", "ok", "resume") to resume the turn from the saved round index.
2. **Subsystem Resource Alerts:** If a single message turn exceeds **4 tool rounds**, **3,000 output tokens**, or **12,000 uncached input tokens**, a `HIGH_RESOURCE_USAGE` operational alert is triggered and sent to Mazen via the Ops Bot (`DQBotOpsBot`).

---

### Tool Filtering by Tier

| Tool group | Tier 1 | Tier 2 |
|------------|--------|--------|
| Fleet: schedule read | ✓ | ✓ (today only) |
| Fleet: create/cancel outing | ✓ | ✗ |
| Fleet: update availability | ✓ | ✓ (self) |
| Fleet: log incident | ✓ | ✓ |
| Fleet: escalate/resolve | ✓ | ✗ |
| Tasks: list | ✓ (all) | ✓ (self) |
| Tasks: update status | ✓ | ✓ (self) |
| Inventory: log consumption | ✓ | ✓ |
| Shopping: add to queue | ✓ | ✓ |
| Shopping: approve | ✓ | ✗ |
| Guests | ✓ | ✗ |
| Pets: log | ✓ | ✓ |
| Improvement: submit | ✓ | ✓ |
| Improvement: backlog | ✓ | ✗ |
| Receipts | ✓ | ✗ |

### Confirmation Required Before Write

* Create or cancel driver outings.
* Approve shopping queue items.
* Escalate maintenance to tradesman.
* Publish weekly menu.
* Generate guest prep tasks.

---

## 8. Phased Rollout

| Phase | Scope |
|-------|-------|
| **1** | Telegram webhook, `members` whitelist & contact-linking onboarding flow, `conversations`, staff tasks + daily reminders |
| **2** | Driver scheduling, `driver_availability`, basic maintenance logging, iCloud calendar sync |
| **3** | Shopping queue, consumption logging, inventory, weekly digest to Tier 1 |
| **4** | Manual RAG for troubleshooting, tradesman escalation |
| **5** | Recipes, weekly menu, automated grocery list generation |
| **6** | Guest hosting, pet tracking, improvement prompts |

---

## 9. Runtime Execution Patterns (Structural Bottlenecks)

### 9.1 Async Media Ingest — Cloud Tasks Before Gemini

**Problem:** Telegram delivers `file_id` for media. The webhook must return `200` instantly, but Gemini needs the actual binary.

**Pipeline (mandatory sequence in Cloud Tasks worker):**

```
Inbound webhook
  → enqueue InboundMessage {message_id, phone_e164, content[]}
    → Cloud Tasks worker:
         1. FOR EACH block in content[] WHERE block_type == "media" AND gcs_uri is null:
              GET media URL from Telegram getFile API
              Stream download with size/duration guards (see Voice Note Limits below)
              Sniff magic bytes → normalize MIME (see table below)
              PUT gs://{bucket}/inbound/{phone_e164}/{message_id}/{index}.{ext}
              SET block.gcs_uri AND block.normalized_mime_type
         2. Map content[] → Gemini Part[] using normalized_mime_type (never raw Telegram mime_type)
         3. THEN invoke Vertex AI Gemini
         4. On tool write requiring permanent storage (receipt, incident photo):
              copy/move to receipts/ or incident path; store gs:// on record
```

**MIME normalization (mandatory before Gemini call):**

| Inbound signal | Normalized `normalized_mime_type` | File extension |
|----------------|-----------------------------------|----------------|
| Telegram voice note / audio | `audio/ogg; codecs=opus` | `.ogg` |
| Image JPEG | `image/jpeg` | `.jpg` |
| Image PNG | `image/png` | `.png` |
| Image WebP | `image/webp` | `.webp` |
| PDF document | `application/pdf` | `.pdf` |

**Sanitization rules:**

1. **Never** pass Telegram's raw `mime_type` to Gemini — always use `normalized_mime_type` from the worker.
2. Sniff file magic bytes after download; override declared MIME when container bytes contradict the header.
3. All voice notes resolve to **`audio/ogg; codecs=opus`** regardless of raw headers.
4. If normalization fails (unknown container) → do not call Gemini with the media part; reply via Telegram: *"Could not process that audio file — please retry or type your message."*

**Voice note & audio limits (streaming downloader):**

| Limit | Value | Enforcement |
|-------|-------|-------------|
| Max file size (audio/voice) | **15 MB** | Abort stream when cumulative bytes exceed limit; delete partial GCS object |
| Max duration (audio/voice) | **5 minutes** | Probe container metadata after download; reject before Gemini if exceeded |
| Max file size (image) | **10 MB** | Same streaming byte guard |
| Max file size (document/PDF) | **20 MB** | Same streaming byte guard |

---

### 9.2 Implicit Context Caching — Prefix/Suffix Discipline

**Problem:** Injecting fluctuating date/time or open records at the top of the prompt invalidates the cached prefix on every message, forfeiting ~90% input cost savings.

**Solution:** Treat the prompt as two zones. The prefix must meet the **4,096-token activation floor** (pad if under); the suffix history slice must stay within the **3,000-token ceiling**.

| Zone | Contents | Changes per request? | Token guard |
|------|----------|----------------------|-------------|
| **Prefix** | System prompt, schemas, tool definitions, static rules | No | ≥ 4,096 (pad if under) |
| **Suffix** | Clock, speaker, open records, history, user text, `gs://` media | Yes | History ≤ 3,000 |

---

### 9.3 `pending_confirmation` — Interrupt & Preemption

**Problem:** A user with an active outing confirmation who sends "The car has a flat tire" must not be trapped in a Yes/No loop.

**Pre-agent gate (runs in Cloud Tasks worker before Gemini call):**

```
1. Load pending_confirmation from conversations/{phone}
2. If null or status != "active" → proceed normally
3. If expires_at < now → mark "expired", clear, proceed normally
4. Classify inbound message intent:
     a. CONFIRM  — affirmative response to pending action ("yes", "confirm", "نعم")
     b. REJECT   — explicit cancel ("no", "cancel", "لا")
     c. UNRELATED — anything else (new topic, emergency, question)
5. Route:
     CONFIRM  → execute stored payload via tool handler; clear pending; skip Gemini
     REJECT   │ clear pending; send brief acknowledgment; optionally invoke Gemini
     UNRELATED │ preempt (see below)
```

**Preemption rules (UNRELATED while confirmation active):**

1. **Pause** (default): Move `pending_confirmation` to `paused_confirmations` stack with `pause_reason: "user_pivot"`. Clear `pending_confirmation`. Proceed to Gemini with full new message. Gemini session context includes: *"Previous confirmation paused: {summary}. Handle the new request first."*
2. **Auto-expire on priority topics:** If emergency keywords (`flat tire`, `accident`, `leak`, `urgent`) or module `maintenance_incident` are detected, **discard** the paused confirmation unless user explicitly resumes.
3. **Never re-prompt blindly:** Do not reply with only "Please confirm Yes or No" when the user's message is clearly unrelated. At most, append a single line: *"Your pending request is on hold — reply 'resume' to continue."*
4. **Resume command:** User sends `"resume"` / `"continue"` → pop most recent item from `paused_confirmations`, restore as active `pending_confirmation`, re-send summary.

---

### 9.4 Additional Concurrency & Data Lifecycle Constraints

| Constraint | Location | Rule |
|------------|----------|------|
| Message history writes | §3 `messages/` subcollection | Atomic `create()` per turn; never array-append on parent doc |
| Idempotency key lifecycle | §1 `webhook_idempotency` | 24h TTL + stale `expires_at` overwrite on `create()` conflict |
| Tool argument language | §7 Translation Boundary | Structural args English; free-form fields preserve original language |
| Shared state mutations | §6 Transactional Writes | Firestore `transaction` for `staff_tasks`, `driver_schedule`, etc. |
| Media MIME normalization | §9.1 | Voice notes → `audio/ogg; codecs=opus`; never pass raw Telegram MIME to Gemini |
| Voice/audio size limits | §9.1 | 15 MB / 5 min hard cutoff during streaming download |
| Suffix history budget | §7 Suffix Token Budget | Drop oldest turns until history ≤ 3,000 tokens |

---

## 9.5 Firestore Indexes Configuration

| Collection | Fields | Sort Order | Query Purpose |
|------------|--------|------------|---------------|
| `driver_schedule` | `status` | Ascending | Driver arrival nagging (find scheduled outings ending before now) |
| | `end_time` | Ascending | |
