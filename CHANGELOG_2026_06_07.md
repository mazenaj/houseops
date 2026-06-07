# Changelog — June 7, 2026

## 1. Tool Response Serialization Fix
* **Coercion Error Resolved:** Fixed a `ValueError: Unable to coerce value` crash on the `/tasks/process-inbound` endpoint. When the Gemini model executed the `get_schedule` tool, the tool returned a dictionary that included Firestore `DatetimeWithNanoseconds` / `datetime` objects. Vertex AI's `Part.from_function_response` requires JSON-serializable types and threw an error when attempting to convert these objects into a proto Struct.
* **Global Sanitization:** Implemented a global sanitization routine in `run_agent_turn` inside [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py#L380-L407) that automatically converts any `datetime` or `DatetimeWithNanoseconds` objects in tool response dictionaries into ISO-formatted strings. This ensures compatibility with the Gemini API for all current and future tools.
* **Testing:** Added a unit test `test_run_agent_turn_sanitization` in [tests/test_main.py](file:///Users/terminal/houseops/tests/test_main.py) to assert that tool responses containing `datetime` objects are correctly sanitized and do not trigger protobuf serialization failures.

## 2. Optional Transit Location & Purpose for Driver Outings
* **Optional Fields:** Updated `manage_outing` in [app/tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) to make `destination` and `purpose` optional parameters, ensuring they are not required to schedule a driver outing.
* **Payload Builder Reordering:** Reordered parameters in `_build_outing_payload` to move `requested_by` (non-default argument) before the now-default/optional parameters `destination` and `purpose`, resolving a syntax compilation error.
* **Readable Summary Adaptations:** Adjusted the text summary builder in `manage_outing` to dynamically exclude "to {destination}" or "({purpose})" parts if they are not provided, generating clean summary confirmations like `"Schedule driver Emad for outing on 2026-06-07 at 10:00 AM."`

## 3. Log Investigation & Unit Check Findings
* **Log Check:** Scanned the Cloud Run revision error logs over the past week. Found that the only error events were:
  1. The `ValueError: Unable to coerce value` due to `DatetimeWithNanoseconds` in tool responses (now resolved globally by our sanitization routine).
  2. A `FailedPrecondition: 400 The query requires an index` error for `driver_schedule` collection group queries under `/jobs/driver-arrival-nag`. Confirmed that this index is now in a `READY` state on GCP, meaning it has successfully finished building and future nag jobs will execute correctly.
* **Unit Verification:**
  - **Nanosecond Precision:** Confirmed that nanosecond precision is Firestore's native representation for timestamps (parsed as Google Cloud's `DatetimeWithNanoseconds`), which is standard for GCP. Our serialization fix handles this standard type correctly.
  - **Other Units:** Audited the codebase for physical/data units. Checked the procurement and inventory schema (`SCHEMA.md`) which mentions standard unit labels (e.g. `kg`, `pcs`, `L`), but verified the code itself treats all unit definitions as generic strings without non-standard units or hardcoded assumptions.

## 4. Daily Billing Usage Alert Calibration
* **Accumulated Cost Threshold:** Replaced the sensitive per-turn warning thresholds with an accumulated daily cost trigger set to $10.00 to align with actual Google Cloud billing rates and prevent false-positive alerts.
* **Pricing Rates Configured:** Defined standard Gemini 2.5 Flash pricing constants in [app/config.py](file:///Users/terminal/houseops/app/config.py):
  * Input tokens (uncached): $0.30 per 1 million tokens.
  * Input tokens (cached): $0.075 per 1 million tokens.
  * Output tokens: $2.50 per 1 million tokens.
* **Daily Tracker (Firestore Transactional):** Modified `_check_resource_usage_alert` in [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py) to atomically record daily token usage and cost under the `system_usage` collection (indexed by the current date in Riyadh timezone).
* **Single Daily Notification:** Configured the billing alert to execute exactly once per day when the cumulative cost first crosses $10.00 (managed via an `alert_sent` flag in the daily usage document).
* **Unit Testing:** Updated the resource usage alert tests in [tests/test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py) to validate cost accumulation, crossing the $10.00 threshold, and preventing duplicate alerts when the threshold has already been crossed.
