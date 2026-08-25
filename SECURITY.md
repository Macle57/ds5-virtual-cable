# Security policy

## Reporting a vulnerability

Please report security issues **privately** through GitHub's
[private vulnerability reporting](../../security/advisories/new) rather than in
a public issue.

Include what you did, what happened, and — if it involves the USB/IP path —
whether the emulator was reachable from outside `127.0.0.1`. A proof of concept
helps enormously. Expect an acknowledgement within a week; this is a hobby
project, so please be patient, and please do not publish before we have had a
chance to respond.

## What this software actually is, and what that means

This is a **local, privileged-adjacent tool**. Being clear about its shape is
more useful than a list of promises:

- **It is user-mode.** Nothing in this repository runs in the kernel. A bug here
  is a crashed Python process, not a bugcheck.
- **It listens on a TCP socket.** The emulator binds a loopback port (3241 by
  default) and serves the USB/IP protocol on it. **Anything that can connect to
  that socket can drive the emulated device**, which means it can write output
  reports to your controller and read its input. Keep it bound to `127.0.0.1`.
  Do not expose it to a network, and do not port-forward it. There is no
  authentication in the USB/IP protocol, by design — it was never meant to face
  a hostile network.
- **The kernel component is not ours.** The signed UDE driver that consumes the
  emulated device is [usbip-win2](https://github.com/vadimgrn/usbip-win2), a
  separate project with its own security posture and its own reporting process.
  Kernel-side issues belong there. Install it only from its official releases,
  and verify the Authenticode signature — the drivers are signed by Microsoft.
  **Do not install version 0.9.7.8**, whose own maintainer warns of a memory
  corruption / BSOD defect.
- **Attaching a USB/IP device requires administrator rights**, because it is
  creating a device on your machine. Running the emulator itself does not.
- **It talks to your controller over HID.** A compromised or malicious USB/IP
  client could send arbitrary output reports through the bridge. Two guardrails
  exist: feature *writes* — which are how a DualSense is re-paired and how its
  firmware is touched — are recorded and **never forwarded** to the physical
  controller; and feature *reads* are served from a small cache primed at
  startup rather than passed through.
- **It handles audio.** The microphone path decodes Opus frames that arrive from
  the controller. Malformed frames are a plausible fuzzing target.

## Things that are out of scope

- **Anti-cheat detection or bypass.** This project makes no evasion claims and
  implements nothing to hide what it is; see the FAQ in `README.md`. Reports
  along the lines of "anti-cheat X can detect this" are welcome as ordinary
  issues, not as security reports.
- **The fact that the emulated device presents Sony's VID/PID and descriptors.**
  That is the entire purpose of the software and is documented openly in
  `README.md` and `docs/provenance.md`.
- **usbip-win2's own driver behaviour.** Report it upstream.
- **Anything requiring physical access to an already-unlocked machine.**

## Supported versions

The `main` branch. This project has no long-lived release branches.
