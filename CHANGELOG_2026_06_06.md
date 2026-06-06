# Changelog — June 6, 2026

## 1. iCloud Calendar URL Registration Fix
* **Routing Path Correction:** Resolved a routing issue where the `register_calendar_url` tool, while declared in `tools_fleet.py` and registered with Gemini, was not routed to the fleet tool executor in `execute_tool_call` inside [tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L318-L323). This caused the assistant to return a "tool currently unavailable" error when Tier 1 users sent calendar URLs.
* **Prompt Document Alignment:** Updated [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py) to add `register_calendar_url` to both the `MODULE_1_SCHEMA` and `TOOL_DECLARATIONS_TEXT` system prompts. Previously, the tool was excluded from these text schemas, leading the model to believe the tool was unavailable and refuse to call it.
* **Session Context ID Exposure:** Fixed an issue where the session context compiled by `_build_session_context` in [main.py](file:///Users/terminal/houseops/main.py#L252-L278) did not contain the user's `member_id` (only their display name). This caused Gemini to pass the name string `'Mazen'` instead of the database identifier `'mem_principal_001'` as the `member_id` parameter to `register_calendar_url`, failing with a `'member_not_found'` error. Exposed the `member_id` parameter to `_build_session_context` and outputted it to the prompt.
* **Testing:** Added a unit test `test_execute_tool_call_register_calendar_url` in [test_tools_module2.py](file:///Users/terminal/houseops/tests/test_tools_module2.py) verifying that calls to `register_calendar_url` are correctly dispatched to `execute_fleet_tool_call`.

## 2. DQBotOpsBot Status/Egress Message Consolidation
* **Removed Duplicate Message:** Eliminated duplicate notifications received by Mazen during the twice-daily status report. Modified the telegram webhook in [main.py](file:///Users/terminal/houseops/main.py#L202-L228) to process `ping_test` messages from `@DQBotOpsBot` silently, returning a `200` success response without sending a separate "Main Bot Egress Test: Success!" alert.
* **Consolidated Report:** Subsystem integration health (both ingress and egress) remains fully verified and is presented consolidated inside the single performance report delivered at 8:00 AM and 8:00 PM.
* **Testing:** Updated the webhook integration test in [test_main.py](file:///Users/terminal/houseops/tests/test_main.py) to assert that no separate message is dispatched to the user during loopback testing.

## 3. Developer and System Tools Maintenance
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
  * **Docker Desktop** upgraded to `4.76.0` (completed manually by user)

## 4. Firestore Index Provisioning
* **Composite Index Creation:** Deployed the missing composite index for `driver_schedule` (`status` ASC, `end_time` ASC) using the Google Cloud SDK to prevent `FailedPrecondition` exceptions during driver arrival automated checks.
