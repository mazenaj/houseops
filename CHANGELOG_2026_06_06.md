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

## 5. Proactive Lookup and Ambiguity Resolution Policy
* **Prompt Instructions Enrichment:** Updated [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py) to establish a strict *Proactive Lookup and Ambiguity Resolution Policy* inside the `STATIC_SYSTEM_PROMPT`. This guides the AI agent to never prompt users for technical IDs (such as `task_id` or `outing_id`) when tasked with modifying, canceling, rescheduling, or completing tasks/outings.
* **Lookup & Action Orchestration:** Directed the AI to proactively query current schedules (via `get_schedule`) or task lists (via `list_tasks`) first to retrieve the target ID. On matching:
  - If a single match is found, apply the change (e.g. canceling the old outing and creating a new outing to swap drivers, keeping other metadata intact).
  - If multiple matches are found (ambiguous requests), present the alternatives and request clarification.
  - If no matches are found, explain what was searched and request clarification.

## 6. Access Control & Task status mutations for Tier 2 Staff
* **Outing & Task Modification Restrictions:** Confirmed that outing rescheduling, replacement, cancellation, and task creation/modification are strictly restricted to Tier 1 principals (Mazen and Jawaher). Enforced in [app/tools_fleet.py](file:///Users/terminal/houseops/app/tools_fleet.py) and [app/tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py).
* **Task Status Constraints for Tier 2:** Modified the transactional status updater `_txn_update_task_status` in [app/tools_module2.py](file:///Users/terminal/houseops/app/tools_module2.py#L118-L149) so that Tier 2 staff users are only permitted to update task status to `"completed"` or `"skipped"`. Any attempt to set a task back to `"pending"` is blocked with a permission error.
* **Problem Reporting Enforcement:** Enforced that Tier 2 staff can only skip a task (`status="skipped"`) if they provide a detailed, non-empty feedback string explaining the problem. This maps skip actions directly to reporting/notifying of problems.
* **System Prompt Guidance:** Aligned the RBAC definitions in [app/prompts.py](file:///Users/terminal/houseops/app/prompts.py) so the AI model behaves in accordance with these constraints.
* **Testing:** Added unit test `test_txn_update_task_status_tier2_rules` in [test_tools_module2.py](file:///Users/terminal/houseops/tests/test_tools_module2.py) verifying all Tier 2 status change constraints.

## 7. High Resource Usage Billing Alert
* **Cumulative Metric Tracking:** Added cumulative token and round tracking across multi-turn Vertex AI agent loops inside `run_agent_turn` in [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py#L229-L329).
* **Billing Threshold Warnings:** Integrated helper function `_check_resource_usage_alert` which automatically executes at each turn exit point. If the turn exceeds 4 tool-call rounds, 3,000 output (candidate) tokens, or 12,000 uncached input (prompt) tokens, it triggers a `HIGH_RESOURCE_USAGE` ops alert.
* **Alert Delivery:** Delivers detailed token and round statistics to Mazen via the existing `send_ops_alert` channel in [app/ops_bot.py](file:///Users/terminal/houseops/app/ops_bot.py) to prevent unexpected API billing surprises.
* **Testing:** Added unit test `test_check_resource_usage_alert` in [tests/test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py) verifying alert trigger conditions across all threshold metrics.
