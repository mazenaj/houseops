# Changelog — June 6, 2026

## 1. iCloud Calendar URL Registration Fix
* **Routing Path Correction:** Resolved a routing issue where the `register_calendar_url` tool, while declared in `tools_fleet.py` and registered with Gemini, was not routed to the fleet tool executor in `execute_tool_call` inside [tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L318-L323). This caused the assistant to return a "tool currently unavailable" error when Tier 1 users sent calendar URLs.
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
