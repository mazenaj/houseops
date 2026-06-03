# Changelog — June 3, 2026

## 1. Updated Driver Dispatch & Vehicle Seeding
* **Database Seed Update:** Renamed the Tier 1 principal from `"Principal (Mazen)"` to `"Mazen"` in [init_db.py](file:///Users/terminal/houseops/init_db.py) to unify user name fields with preferences.
* **Dispatch Preference & Fleet Mappings:**
  * **Khidir** mainly drives Jawaher and Mano in the *Mercedes V Class*.
  * **Emad** mainly drives Mazen in the *Lexus LX*.
  * **Kim** mainly drives Errands/Shopping in the *Toyota Rush*.
  * Removed any other non-existent vehicle profiles from the database seed.
* **DB Seeding Execution:** Successfully executed the Firestore seeder against the project.

---

## 2. Implemented Codebase Refinements & Bug Fixes
The following 7 issues identified in the QA audit and subagent debate were resolved:

### A.1 Calendar Sync Missing `DTEND` Fallback
* **Issue:** Calendar sync crashed if an iCloud event was missing the `DTEND` field.
* **Fix:** Added a fallback in [icloud_calendar.py](file:///Users/terminal/houseops/app/icloud_calendar.py) to set the end time to `start_time + 1 hour` (or equal to start for all-day events) if it is missing or zero-duration.

### B.1 Midnight Crossover in Driver Availability
* **Issue:** Naive time checks in `is_driver_available` allowed drivers to be scheduled on crossover events outside their working hours (e.g. crossing midnight).
* **Fix:** Updated [workflow.py](file:///Users/terminal/houseops/app/workflow.py) to query availability for both target date and target date + 1, convert slots to timezone-aware datetime ranges in `RIYADH_TZ`, merge overlapping/contiguous intervals, and verify complete interval containment.

### B.2 Missed Driver Arrival Nags After Midnight
* **Issue:** Clamp-to-today logic in `run_driver_arrival_nag` caused unconfirmed late-night outings to be permanently missed once the clock rolled over to the next day.
* **Fix:** Replaced the start-of-day clamp with a sliding 24-hour lookback window query (`now - timedelta(hours=24)`).

### C.1 Firestore Composite Index Configuration
* **Documentation:** Created [firestore.indexes.json](file:///Users/terminal/houseops/firestore.indexes.json) and updated [SCHEMA.md](file:///Users/terminal/houseops/SCHEMA.md) to define the required composite index on `driver_schedule` for `status` (Ascending) and `end_time` (Ascending).

### C.2 Firestore 30-Item Query Limit
* **Issue:** Availability schedules longer than 30 days had dates silently truncated because of Firestore's `in` query limitations.
* **Fix:** Replaced the `in` query in [tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) with a lexicographical single-field range query (`>= start_date` and `<= end_date`).

### D.1 Idempotency Key Enqueue Race
* **Issue:** Webhook claimed message idempotency keys prior to enqueuing. If enqueuing failed, the key remained locked, blocking any future Telegram delivery retries.
* **Fix:** Caught exceptions in the webhook enqueuer in [main.py](file:///Users/terminal/houseops/main.py), deleted the claimed idempotency key in Firestore, and returned HTTP `500` to Telegram to trigger automated retries.

### E.1 Principal Message Hijacking on Calendar Conflict
* **Issue:** Tier 1 principal messages were unconditionally intercepted and blocked whenever a conflict was active.
* **Fix:** Updated the confirmation gate in [confirmation_gate.py](file:///Users/terminal/houseops/app/confirmation_gate.py) to use full-word checks for short keywords (e.g. `yes`, `done`, `clear`) and substring checks for longer words (e.g. `calendar`, `recheck`, `update`). Unrelated messages now pass to Gemini.

---

## 3. Test Suite Verification
* **Unit Tests Added:**
  * Added `test_fetch_icloud_events_missing_dtend` in `tests/test_icloud_calendar.py`.
  * Added `test_release_idempotency_key` in `tests/test_idempotency.py`.
  * Added `test_run_confirmation_gate_tier1_calendar_conflict_handling` in `tests/test_confirmation_gate.py`.
* **Execution:** Ran `.venv/bin/pytest`. All **103 tests pass successfully**.
