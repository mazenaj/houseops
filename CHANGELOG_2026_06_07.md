# Changelog — June 7, 2026

## 1. Tool Response Serialization Fix
* **Coercion Error Resolved:** Fixed a `ValueError: Unable to coerce value` crash on the `/tasks/process-inbound` endpoint. When the Gemini model executed the `get_schedule` tool, the tool returned a dictionary that included Firestore `DatetimeWithNanoseconds` / `datetime` objects. Vertex AI's `Part.from_function_response` requires JSON-serializable types and threw an error when attempting to convert these objects into a proto Struct.
* **Global Sanitization:** Implemented a global sanitization routine in `run_agent_turn` inside [app/vertex_client.py](file:///Users/terminal/houseops/app/vertex_client.py#L380-L407) that automatically converts any `datetime` or `DatetimeWithNanoseconds` objects in tool response dictionaries into ISO-formatted strings. This ensures compatibility with the Gemini API for all current and future tools.
* **Testing:** Added a unit test `test_run_agent_turn_sanitization` in [tests/test_main.py](file:///Users/terminal/houseops/tests/test_main.py) to assert that tool responses containing `datetime` objects are correctly sanitized and do not trigger protobuf serialization failures.
