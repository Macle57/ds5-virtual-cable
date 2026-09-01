# Focus gating: a foreground Chromium window silences the pad

**Date:** 2026-09-01
**Symptom:** the moment the dashboard's browser window gains focus, the
dashboard freezes and shows "Bluetooth link lost"; unfocus it and everything
resumes within a second.
**Result:** the cause is Windows' **GameInput service**, not anything in this
project — and the bridge now routes around it (`emulator/ds5emu/bridge.py`,
the Phase 5 control-channel fallback).

---

## The measurement

Everything below was driven by scripted `SetForegroundWindow` flips while an
**independent raw hidapi handle** — a separate process, opened read-only on
the Bluetooth pad, nothing of this project's in its path — counted reports
per second alongside the bridge's own telemetry.

1. **Any focused Google Chrome window kills the stream.** Not just the
   dashboard: a clean profile with no extensions, a *separate Chrome
   instance* whose only page was `about:blank`, and Chrome launched with
   `--disable-features=GameInput*` all reproduce it identically. The
   dashboard page itself (SSE + SVG, no Gamepad API, no focus handlers) is
   fully exonerated.
2. **The pad goes truly silent, below every process.** During the gate the
   independent handle reads *nothing* — every read times out empty, the
   handle stays valid, and a per-second report-id histogram shows zero
   reports of **any** id. It is not a fall-back to minimal `0x01` mode
   (which the bridge's reader skips); there is simply nothing arriving.
   Meanwhile the emulator kept serving USB interrupt IN at 250/s the whole
   time (`input_delivered` never dipped), so the virtual device and the
   USB/IP stack were healthy throughout.
3. **The control channel is untouched.** Mid-gate, feature reads answer
   normally — the bridge's reconnect path even completed a full
   `_open_device()` during a gate — and, decisively,
   `GET_REPORT(Input, 0x31)` answers with the pad's **current full extended
   state in 5–20 ms**: live stick bytes, advancing sequence nibble. Only the
   interrupt-IN delivery is gated.
4. **Stop `GameInputSvc` and the gate is gone.** With `GameInputSvc` and
   `GameInputRedistService` stopped (elevated, user-authorized), the same
   Chrome window held focus for 27 s and the stream ran clean at ~540–617
   reports/s, zero empty seconds. Restart the services and the same flip
   kills the stream within a second. 100 % discrimination, both directions.
5. **HidHide does not help.** The pad was cloaked throughout, and the gate
   survived a full pad power-cycle with the cloak active — so the session-0
   service is not subject to HidHide's per-image whitelist, and a
   sever-stale-handles approach (restart the devnode after cloaking) cannot
   work either: the service simply reopens.

Chrome 152 evidently registers with GameInput at browser startup, page
content notwithstanding; when any of its windows holds the foreground,
GameInput grants it gamepad focus and mutes interrupt-IN delivery to every
other handle on every DualSense in the system — the real Bluetooth pad *and*
the virtual wired one.

## The fix, and why it is in the bridge

Nothing this project ships can stop a system service from gating the stream,
and both machine-level outs are wrong as defaults: disabling `GameInputSvc`
breaks GameInput-API titles, and per-app Game-Mode exclusions are fragile
registry surgery on somebody else's product.

But finding 3 is an opening: the gate stops the interrupt queue and nothing
else. So the reader now watches for the stream going silent while the link
is nominally up (`POLL_AFTER_S`, 150 ms — above any normal Bluetooth burst
gap, below the 1 s neutralise threshold) and carries input over
`GET_REPORT(Input, 0x31)` polls at up to ~125 Hz, fed through the exact same
translate/intercept path as streamed reports. The moment a streamed report
arrives, the polls stop. A pad that is genuinely off is also stream-silent,
but its polls *fail*, so after `POLL_MAX_FAILURES` the fallback stands down
and the link watchdog keeps its old schedule.

What degrades during a gate, deliberately: input rate (~100 Hz polled vs
~476 Hz streamed — the USB host repeats the freshest state between arrivals,
as it already does for burst gaps) and microphone capture (audio payloads
cannot be polled). Input is the product; both trades are right.

Counters: `input_polled`, `input_poll_errors`, `poll_fallbacks` in
`BridgeBackend.stats`; a `bt.poll.*` trail in the black box. Tests:
`emulator/tests/test_bridge.py::ControlChannelFallbackTests`.
