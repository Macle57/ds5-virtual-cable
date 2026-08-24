"""Prove the HD-haptics path on hardware, again with no hands needed.

The voice-coil actuators are loud enough for the controller's own microphone to
hear them. So: drive a pure sine into the 0x36 haptic subpacket (int8 stereo at
the actuators' native 3 kHz) with a SILENT Opus frame in the audio subpacket, and
check that the mic capture grows a peak at exactly that frequency. Sound in the
capture can then only have come from the actuators, since nothing was sent to the
speaker.

Measured: 250 Hz drive -> mic peak at 250.1 Hz, +25.8 dB over baseline.

Remember 3 kHz is the actuators' native rate, so anything above ~1500 Hz folds --
that is the hardware, not this code.

    python prototype/tools/haptic_test.py
"""

import sys, time, threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np
from ds5bridge import audio as A, device as D, protocol as P
from ds5bridge.pacing import Pacer, TimerResolution
from loopback_test import capture, spectrum, band_energy

FREQ=250.0; SECS=3.0
d = D.DualSense(D.pick("BT")).open()
d.send_mic_state(True); time.sleep(0.05); d.send_mic_control(True); time.sleep(0.4)
d.send_setstate(P.SetState().haptic_volume(0xFF)); time.sleep(0.05)

print(f"[base] {SECS}s baseline (no haptics)")
base,na,_ = capture(d, SECS); print(f"   {na} payloads {base.size} samples")

hf = A.sine_haptic_frames(FREQ, FREQ, SECS+1.5, 1.0)
print(f"[hapt] {len(hf)} haptic frames @ {FREQ}Hz, silent opus")
stop=threading.Event(); sent=[0]
def run():
    so=A.silent_opus_frame(); pc=Pacer(frame_ms=A.FRAME_MS,max_backlog=8,max_burst=4); fc=0
    with TimerResolution(1):
        pc.reset()
        while not stop.is_set() and pc.emitted < len(hf):
            due=pc.frames_due()
            if due==0: pc.sleep_until_next(); continue
            for _ in range(due):
                if pc.emitted>=len(hf): break
                d.send_report_36(so, hf[pc.emitted], fc, "speaker", 0, mic_active=True)
                fc=(fc+1)&0xFF; sent[0]+=1; pc.commit(1)
t=threading.Thread(target=run,daemon=True); t.start(); time.sleep(0.3)
tone,na2,_ = capture(d, SECS); stop.set(); t.join(timeout=2)
print(f"   {na2} payloads {tone.size} samples, {sent[0]} reports sent")
for _ in range(3):
    d.send_report_36(A.silent_opus_frame(), A.silent_haptic_frame(),0,"speaker",0,mic_active=True)
    time.sleep(A.FRAME_MS/1000)
d.send_mic_control(False); time.sleep(0.02); d.send_mic_state(False); d.close()

fb,mb=spectrum(base); ft,mt=spectrum(tone)
eb=band_energy(fb,mb,FREQ,40); et=band_energy(ft,mt,FREQ,40)
sel=ft>100; peak=float(ft[sel][int(np.argmax(mt[sel]))])
print(f"\n  baseline rms={np.sqrt(np.mean(base**2)):.6f} energy@{FREQ:.0f}={eb:.6f}")
print(f"  haptics  rms={np.sqrt(np.mean(tone**2)):.6f} energy@{FREQ:.0f}={et:.6f} peak={peak:.1f}Hz")
print(f"  gain at haptic frequency: {20*np.log10(et/eb):+.1f} dB")
