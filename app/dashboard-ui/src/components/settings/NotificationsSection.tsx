import { ArrowRight, BatteryMedium } from "lucide-react";
import { useStore } from "../../lib/store";
import type { ConfigDoc, Json } from "../../lib/types";
import { HELP, KNOWN_NOTIFICATIONS, NOTIFICATION_FIELDS } from "./help";
import { Card, Row, Toggle } from "./controls";
import GenericRows from "./GenericRows";

const asObj = (v: unknown): Record<string, Json> =>
  v !== null && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, Json>) : {};

/* The `notifications` section: one switch per toast category (CONTRACT
   section 3), saved through the ordinary /api/config path. Absent keys show
   the engine's defaults, so a 0.5 config reads as it will behave once the
   1.0 tray loads it. */
export default function NotificationsSection() {
  const cfg = useStore((s) => s.cfg as ConfigDoc);
  const setTab = useStore((s) => s.setSettingsTab);
  const enabled = useStore((s) => (s.cfg?.notifications as Record<string, Json> | undefined)?.enabled !== false);
  const [master, ...rest] = NOTIFICATION_FIELDS;
  return (
    <Card title="Notifications" hint="The toasts the tray shows. Saved as the notifications section of config.json.">
      <Row label={master[1]} keyName={master[0]} root="notifications" help={HELP[master[3]]}>
        <Toggle path={["notifications", master[0]]} dflt={master[2]} />
      </Row>
      <div className={"transition-opacity " + (enabled ? "" : "opacity-50")}>
        {rest.map(([key, label, dflt, helpKey]) => (
          <Row key={key} label={label} keyName={key} root="notifications" help={HELP[helpKey]}>
            <Toggle path={["notifications", key]} dflt={dflt} />
          </Row>
        ))}
      </div>
      <GenericRows path={["notifications"]} obj={asObj(cfg.notifications)} known={KNOWN_NOTIFICATIONS} />
      {/* the one toast that is a shortcut rather than an event */}
      <div className="mt-1 flex items-start gap-3 rounded-xl border border-circle/40 bg-circle/[.06] px-3.5 py-3 text-[12.5px] leading-relaxed text-ink-2">
        <span className="grid h-8 w-8 flex-none place-items-center rounded-md border border-circle/50 bg-circle/10 text-circle">
          <BatteryMedium size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <b className="text-ink">Battery toast on demand.</b> {HELP.show_battery} Look for it under <b className="text-ink">Controller</b> in
          any action picker.
          <button type="button" onClick={() => setTab("chords")}
                  className="mt-1.5 flex items-center gap-1.5 text-[12.5px] text-circle hover:underline">
            Bind it under Chords <ArrowRight size={13} />
          </button>
        </div>
      </div>
    </Card>
  );
}
