# Changelog — June 4, 2026

## 1. Implemented Dedicated Performance Monitoring Bot (DQBotOpsBot)
* **Ops Bot Token Config:** Integrated `TELEGRAM_OPS_BOT_TOKEN` in [config.py](file:///Users/terminal/houseops/app/config.py#L40) and [deploy.sh](file:///Users/terminal/houseops/deploy.sh#L42) to support outbound messaging via `@DQBotOpsBot` API.
* **Systems Performance Reports:** Built [get_ops_status_report](file:///Users/terminal/houseops/app/ops_bot.py#L110-L198) in [ops_bot.py](file:///Users/terminal/houseops/app/ops_bot.py) performing twice-daily checks:
  * **Database Status:** Verifies Firestore reads and writes by updating a health document.
  * **Vertex AI API:** Verifies response token counts against the cachefloor.
  * **Ingress Webhook (Main Bot):** Checks webhook configuration, pending update count, and connection health using `getWebhookInfo`.
  * **Ops Bot API Connection:** Tests outbound connection.
* **Immediate Technical Alerts:** Registered a global unhandled exception handler in [main.py](file:///Users/terminal/houseops/main.py#L70-L86) to catch unexpected FastAPI runtime crashes and immediately dispatch a formatted traceback alert to Mazen.

## 2. Segregation of Operations & Technical Alerts
* **Main Channel Routing:** Delayed driver arrival warnings (exceeding 30 minutes) in [workflow.py](file:///Users/terminal/houseops/app/workflow.py#L458-L477) are routed to Tier 1 principals via the normal channel (`_notify_tier1_users` using the main bot token), keeping the Ops Bot channel strictly dedicated to technical performance.
* **Calendar Sync Conflicts:** Conflict alerts are also excluded from the Ops Bot channel and routed solely via the normal channel.

## 3. Test Suite Verification
* **Ops Bot Tests:** Created unit and integration tests in [test_ops_bot.py](file:///Users/terminal/houseops/tests/test_ops_bot.py) validating the separation of technical alerts (crashes) from house ops notifications (normal channel driver timeouts).
* **Execution:** Ran all **110 tests** successfully with zero errors.
