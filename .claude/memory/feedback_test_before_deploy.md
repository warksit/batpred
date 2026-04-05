---
name: Test plugin API calls before deploying to live
description: Verify Predbat API methods (call_service_wrapper, get_history_wrapper, etc.) work correctly in tests BEFORE deploying to mum's live system
type: feedback
---

NEVER deploy iterative fix-test cycles to the live system. The user's mum depends on this for battery management.

**Why:** Multiple failed deploys in one session (wrong method names: get_history vs get_history_wrapper, call_service vs call_service_wrapper) caused unnecessary restarts on a live system.

**How to apply:**
1. Check Predbat's actual API by reading the source (predbat.py, ha.py, hass.py) — don't guess method names
2. Write a test that verifies the API call pattern works before deploying
3. Deploy once, verify once — not iterative fix cycles
4. If something fails on live, diagnose locally first, then deploy the fix
